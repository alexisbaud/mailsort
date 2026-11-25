"""Passage de tri. Pour l'instant : le passage à blanc, qui classe sans rien modifier dans Gmail.

Pour chaque mail : lire le fil (réponse de l'utilisateur, dernier message), reprendre les réponses
de Jev déjà enregistrées si leur empreinte est à jour, sinon lire le mail et interroger Jev,
puis appliquer les règles de décision. Les réponses de Jev sont enregistrées dans la base : un
nouveau passage à blanc (par exemple après un changement de seuil) ne rappelle pas Jev.
Les décisions, elles, ne sont pas enregistrées, puisqu'aucun label n'est posé.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from mailsort.config import REVIEW_LABEL_ID, SECONDARY_TYPE_QUESTION, TYPE_QUESTION, Config
from mailsort.db import Database
from mailsort.engine import NONE_OPTION, Decision, decide, secondary_request
from mailsort.gmail import Gmail, GmailAuthError, GmailError, GmailMessage, ThreadInfo, is_excluded
from mailsort.jev import (
    JevAuthError,
    JevClient,
    JevError,
    ask,
    ask_secondary,
    stored_fingerprints,
    to_jev_answers,
)
from mailsort.prepare import summarize

TOKENS_PER_MAIL_ESTIMATE = 2000
"""Moyenne mesurée : environ 1 450 tokens pour un mail court, jusqu'à 6 200 pour le plus long."""


@dataclass
class DryRunReport:
    total: int = 0
    """Mails classés (hors mails ignorés et erreurs)."""
    label_counts: Counter[str] = field(default_factory=Counter)
    no_label: int = 0
    review_only: int = 0
    """Mails dont le seul label serait 🤔 To Review."""
    skipped: int = 0
    """Mails ignorés : envoyés, brouillons, spams, corbeille."""
    errors: list[tuple[str, str]] = field(default_factory=list)
    jev_calls: int = 0
    input_tokens: int = 0
    cached: int = 0
    """Mails classés sans appeler Jev, grâce aux réponses déjà enregistrées."""


@dataclass(frozen=True)
class Row:
    """Une ligne du CSV."""

    message_id: str
    date: str
    sender: str
    subject: str
    decision: Decision
    probabilities: dict[str, dict[str, float]]

    @property
    def in_review(self) -> bool:
        return REVIEW_LABEL_ID in self.decision.labels

    @property
    def short_date(self) -> str:
        try:
            return parsedate_to_datetime(self.date).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return self.date[:16]


@dataclass(frozen=True)
class Note:
    """Un doute ou l'absence de repère pour un mail, avec les scores utiles."""

    key: str
    """`type`, `doubt:<id du label>` ou `no_landmark` : sert à regrouper les mails."""
    detail: str
    """En clair, avec les scores et les seuils."""


NO_LANDMARK = "no_landmark"
TYPE_DOUBT = "type"


class DryRun:
    def __init__(
        self,
        box: Gmail,
        client: JevClient,
        db: Database,
        config: Config,
        *,
        user_addresses: Iterable[str] = (),
    ):
        self.box = box
        self.client = client
        self.db = db
        self.config = config
        self.fingerprints = stored_fingerprints(config, client.model)
        self.user_addresses = [box.profile_email(), *user_addresses]
        self.report = DryRunReport()
        self.rows: list[Row] = []
        self._threads: dict[str, ThreadInfo] = {}

    # ------------------------------------------------------------------ outils

    def list_messages(self, query: str | None, limit: int | None) -> list[tuple[str, str]]:
        return list(self.box.list_messages(query, limit))

    def uncached_count(self, messages: Iterable[tuple[str, str]]) -> int:
        """Mails pour lesquels au moins une question devra être posée à Jev."""
        return sum(1 for message_id, _ in messages if self._missing_questions(message_id))

    def _missing_questions(self, message_id: str) -> list[str]:
        answers = self.db.answers(message_id)
        return [
            question
            for question, fingerprint in self.fingerprints.items()
            if question not in answers or answers[question].fingerprint != fingerprint
        ]

    def _thread(self, thread_id: str) -> ThreadInfo:
        if thread_id not in self._threads:
            self._threads[thread_id] = self.box.thread_info(thread_id)
        return self._threads[thread_id]

    def _state(self, message: GmailMessage, thread: ThreadInfo) -> dict[str, Any]:
        return summarize(
            message.headers,
            message.plain,
            message.html,
            message.attachments,
            user_addresses=self.user_addresses,
            user_replied=thread.user_replied,
        ).to_state()

    # --------------------------------------------------------------- un mail

    def process(self, message_id: str, thread_id: str) -> None:
        """Classe un mail. Les erreurs d'accès (clé, jeton) remontent et arrêtent le passage ;
        les autres sont comptées et le passage continue."""
        try:
            self._process(message_id, thread_id)
        except (GmailAuthError, JevAuthError):
            raise
        except (GmailError, JevError) as exc:
            self.report.errors.append((message_id, str(exc)))

    def _process(self, message_id: str, thread_id: str) -> None:
        thread = self._thread(thread_id)
        missing = self._missing_questions(message_id)
        message: GmailMessage | None = None
        state: dict[str, Any] | None = None
        called_jev = False

        if missing:
            message = self.box.get_message(message_id)
            if is_excluded(message):
                self.report.skipped += 1
                return
            state = self._state(message, thread)
            result = ask(self.client, self.config, state, missing)
            with self.db.transaction():
                self.db.add_message(message_id, thread_id, message.internal_date)
                for question, probabilities in result.probabilities.items():
                    self.db.save_answer(
                        message_id, question, self.fingerprints[question], probabilities
                    )
            self._count_call(result.input_tokens)
            called_jev = True

        answers = self.db.answers(message_id)
        probabilities = {q: answers[q].probabilities for q in self.fingerprints}

        request = secondary_request(self.config, probabilities[TYPE_QUESTION])
        if request is not None:
            cached = answers.get(SECONDARY_TYPE_QUESTION)
            if cached is None or set(cached.probabilities) != set(request.options):
                if message is None:
                    message = self.box.get_message(message_id)
                if state is None:
                    state = self._state(message, thread)
                second = ask_secondary(self.client, self.config, state, request)
                with self.db.transaction():
                    self.db.save_answer(
                        message_id,
                        SECONDARY_TYPE_QUESTION,
                        self.fingerprints[TYPE_QUESTION],
                        second.probabilities[SECONDARY_TYPE_QUESTION],
                    )
                self._count_call(second.input_tokens)
                called_jev = True
                probabilities[SECONDARY_TYPE_QUESTION] = second.probabilities[
                    SECONDARY_TYPE_QUESTION
                ]
            else:
                probabilities[SECONDARY_TYPE_QUESTION] = cached.probabilities

        decision = decide(
            self.config, to_jev_answers(probabilities), user_replied_last=thread.last_from_user
        )
        self._tally(decision, called_jev)
        headers = (
            message.headers
            if message is not None
            else self.box.get_headers(message_id, ["From", "Subject", "Date"])
        )
        self.rows.append(
            Row(
                message_id=message_id,
                date=str(headers.get("Date") or ""),
                sender=str(headers.get("From") or ""),
                subject=str(headers.get("Subject") or ""),
                decision=decision,
                probabilities=probabilities,
            )
        )

    def _count_call(self, tokens: int) -> None:
        self.report.jev_calls += 1
        self.report.input_tokens += tokens

    def _tally(self, decision: Decision, called_jev: bool) -> None:
        report = self.report
        report.total += 1
        report.label_counts.update(decision.labels)
        if not decision.labels:
            report.no_label += 1
        elif decision.labels == {"to_review"}:
            report.review_only += 1
        if not called_jev:
            report.cached += 1

    # ------------------------------------------------------------- ensemble

    def run(
        self,
        messages: Iterable[tuple[str, str]],
        progress: Callable[[], None] | None = None,
    ) -> DryRunReport:
        for message_id, thread_id in messages:
            self.process(message_id, thread_id)
            if progress:
                progress()
        return self.report

    def write_csv(self, path: Path) -> None:
        """Une ligne par mail classé : identifiants, en-têtes, labels, raisons, probabilités."""
        config = self.config
        type_options = [t.id for t in config.types] + [NONE_OPTION]
        yes_no = [label.id for label in config.yes_no_labels]
        order = {label.id: i for i, label in enumerate(config.labels)}
        columns = (
            ["message_id", "date", "from", "subject", "labels", "review_reasons", "to_review"]
            + [f"type:{o}" for o in type_options]
            + ["secondary"]
            + yes_no
        )
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in self.rows:
                primary = row.probabilities[TYPE_QUESTION]
                secondary = row.probabilities.get(SECONDARY_TYPE_QUESTION)
                labels = sorted(row.decision.labels, key=lambda i: order.get(i, 999))
                writer.writerow(
                    [
                        row.message_id,
                        row.date,
                        row.sender,
                        row.subject,
                        " ".join(config.by_id[i].name for i in labels),
                        " | ".join(row.decision.review_reasons),
                        "yes" if row.in_review else "no",
                        *(f"{primary.get(o, 0):.3f}" for o in type_options),
                        " ".join(f"{k}={v:.2f}" for k, v in (secondary or {}).items()),
                        *(f"{row.probabilities[q]['yes']:.3f}" for q in yes_no),
                    ]
                )


# ------------------------------------------------------------- 🤔 To Review


def _option_name(config: Config, option: str) -> str:
    return "aucun type" if option == NONE_OPTION else config.by_id[option].name


def _top(scores: dict[str, float], count: int) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda item: -item[1])[:count]


def mail_notes(config: Config, row: Row) -> list[Note]:
    """Doutes du mail et, s'il n'a aucun repère, ce qui s'en approchait le plus.

    Seule l'absence de repère envoie le mail en 🤔 To Review ; un doute laisse simplement le
    label non posé.
    """
    types_settings = config.settings.types
    types = row.probabilities[TYPE_QUESTION]
    notes = []
    for doubt in row.decision.doubts:
        if doubt.family == "type":
            ranking = " · ".join(f"{_option_name(config, o)} {p:.2f}" for o, p in _top(types, 3))
            notes.append(
                Note(
                    TYPE_DOUBT,
                    f"type incertain : {ranking} (seuil {types_settings.primary_min:.2f}, "
                    f"ou {types_settings.primary_pair_sum:.2f} pour deux types)",
                )
            )
        else:
            label = config.by_id[doubt.label_id]
            notes.append(
                Note(
                    f"doubt:{label.id}",
                    f"doute sur {label.name} : {doubt.probability:.2f} "
                    f"(posé à partir de {config.threshold_for(label):.2f})",
                )
            )
    if row.decision.no_landmark:
        best, best_p = _top(types, 1)[0]
        landmarks = {
            label.id: row.probabilities[label.id]["yes"]
            for label in config.yes_no_labels
            if label.family in ("domain", "cleanup")
        }
        near = " · ".join(f"{config.by_id[i].name} {p:.2f}" for i, p in _top(landmarks, 3))
        notes.append(
            Note(
                NO_LANDMARK,
                f"aucun repère : meilleur type {_option_name(config, best)} {best_p:.2f} ; "
                f"meilleurs domaines/nettoyage {near}",
            )
        )
    return notes


def note_title(config: Config, key: str) -> str:
    """Intitulé d'un groupe de notes, avec le seuil en jeu."""
    settings = config.settings
    if key == TYPE_DOUBT:
        return (
            f"Type incertain (sous {settings.types.primary_min:.2f}, "
            f"ni deux types à {settings.types.primary_pair_sum:.2f})"
        )
    if key == NO_LANDMARK:
        return "Aucun repère (ni type, ni domaine, ni nettoyage)"
    label = config.by_id[key.removeprefix("doubt:")]
    return (
        f"Doute sur {label.name} "
        f"({settings.doubt_min:.2f} ≤ score < {config.threshold_for(label):.2f})"
    )


@dataclass(frozen=True)
class NoteCount:
    key: str
    mails: int
    in_review: int
    """Parmi ces mails, ceux qui partent en 🤔 To Review (faute de repère)."""


def notes_summary(config: Config, rows: Iterable[Row]) -> list[NoteCount]:
    """Nombre de mails par doute ou absence de repère, du plus fréquent au moins fréquent."""
    mails: Counter[str] = Counter()
    in_review: Counter[str] = Counter()
    for row in rows:
        keys = {note.key for note in mail_notes(config, row)}
        mails.update(keys)
        if row.in_review:
            in_review.update(keys)
    return [NoteCount(key, count, in_review[key]) for key, count in mails.most_common()]


def default_csv_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path(f"dry-run-{stamp}.csv")
