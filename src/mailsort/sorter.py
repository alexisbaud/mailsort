"""Passages de tri : le passage à blanc (`DryRun`) et le vrai passage (`Runner`).

Pour chaque mail, les deux reprennent les réponses de Jev déjà enregistrées si leur empreinte est
à jour, sinon lisent le mail et interrogent Jev, puis appliquent les règles de décision. Les
réponses de Jev sont toujours enregistrées dans la base : relancer un passage (par exemple après un
changement de seuil) ne rappelle pas Jev.

Le passage à blanc ne modifie rien dans Gmail et n'enregistre pas les décisions. Le vrai passage :

1. synchronise les labels (création, renommage) ;
2. relit quels mails portent chacun de ses labels, et en déduit les corrections manuelles
   (label retiré par Alexis → verrouillé « absent », label ajouté → verrouillé « présent ») ;
3. classe chaque mail, puis applique les règles avec ces verrous et la règle « déjà répondu » ;
4. ajoute dans Gmail les labels gagnés et retire ceux qu'il avait posés et qui ne s'appliquent
   plus. Il ne touche ni aux labels verrouillés, ni aux labels qu'il n'a pas posés.

Chaque mail est enregistré d'un bloc. Entre l'enregistrement des labels voulus et leur écriture
dans Gmail, le mail est marqué `sync_pending` : si le passage s'interrompt, le suivant sait que
l'écart vient de lui et non d'une correction manuelle, et termine l'écriture.
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from mailsort.config import (
    REPLY_LABEL_ID,
    REVIEW_LABEL_ID,
    SECONDARY_TYPE_QUESTION,
    TYPE_QUESTION,
    Config,
)
from mailsort.db import Database, MessageRecord
from mailsort.engine import NONE_OPTION, Decision, EngineError, decide, secondary_request
from mailsort.gmail import (
    Gmail,
    GmailAuthError,
    GmailError,
    GmailMessage,
    GmailNotFoundError,
    GmailQuotaError,
    ThreadInfo,
    is_excluded,
)
from mailsort.jev import (
    JevAuthError,
    JevClient,
    JevError,
    ask,
    ask_secondary,
    stored_fingerprints,
    to_jev_answers,
)
from mailsort.labelsync import LabelSyncError, SyncResult, sync_labels
from mailsort.prepare import summarize

log = logging.getLogger("mailsort")

TOKENS_PER_MAIL_ESTIMATE = 2000
"""Moyenne mesurée : environ 1 450 tokens pour un mail court, jusqu'à 6 200 pour le plus long."""

MAX_CONSECUTIVE_ERRORS = 5
"""Au-delà de ce nombre d'erreurs d'affilée, le service en cause est sans doute en panne : le
passage s'arrête au lieu d'échouer sur chaque mail restant. Le suivant reprendra."""

STOPPING_ERRORS = (GmailAuthError, GmailQuotaError, JevAuthError, LabelSyncError)
"""Erreurs qui arrêtent le passage : réessayer mail par mail ne servirait à rien."""


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
    stopped: str | None = None
    """Raison de l'arrêt anticipé du passage (trop d'erreurs d'affilée), sinon None."""


@dataclass(frozen=True)
class Row:
    """Une ligne du CSV."""

    message_id: str
    date: str
    sender: str
    subject: str
    decision: Decision
    probabilities: dict[str, dict[str, float]]
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

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


@dataclass
class Classified:
    """Réponses de Jev pour un mail, et ce qu'il a fallu pour les obtenir."""

    probabilities: dict[str, dict[str, float]]
    message: GmailMessage | None
    """Le mail lu dans Gmail, s'il a fallu le lire (sinon None : réponses reprises)."""
    calls: int = 0
    input_tokens: int = 0


class Classifier:
    """Partagé par les deux passages : réponses de Jev (reprises ou demandées), fils, règles."""

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
        self._threads: dict[str, ThreadInfo] = {}
        self._sent_threads: set[str] | None = None

    def list_messages(self, query: str | None, limit: int | None) -> list[tuple[str, str]]:
        return list(self.box.list_messages(query, limit))

    def uncached_count(self, messages: Iterable[tuple[str, str]], *, force: bool = False) -> int:
        """Mails pour lesquels au moins une question devra être posée à Jev."""
        if force:
            return sum(1 for _ in messages)
        return sum(1 for message_id, _ in messages if self._missing_questions(message_id))

    def _missing_questions(self, message_id: str) -> list[str]:
        answers = self.db.answers(message_id)
        return [
            question
            for question, fingerprint in self.fingerprints.items()
            if question not in answers or answers[question].fingerprint != fingerprint
        ]

    def _user_wrote_in(self, thread_id: str) -> bool:
        """L'utilisateur a envoyé au moins un message dans ce fil.

        Une seule liste de ses messages envoyés (quelques unités de quota) remplace la lecture
        de chaque fil (10 unités chacune) : seuls les fils où il a écrit doivent être lus pour
        savoir si son message est le dernier (règle 6).
        """
        if self._sent_threads is None:
            self._sent_threads = {thread for _, thread in self.box.list_messages("in:sent")}
        return thread_id in self._sent_threads

    def _thread(self, thread_id: str) -> ThreadInfo:
        if thread_id not in self._threads:
            if self._user_wrote_in(thread_id):
                self._threads[thread_id] = self.box.thread_info(thread_id)
            else:
                self._threads[thread_id] = ThreadInfo(user_replied=False, last_from_user=False)
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

    def _classify(
        self, message_id: str, thread_id: str, *, force: bool = False
    ) -> Classified | None:
        """Réponses de Jev pour un mail : reprises de la base si elles sont à jour, sinon
        demandées (puis enregistrées). Renvoie None pour un mail à ignorer (envoyé, brouillon,
        spam, corbeille)."""
        missing = list(self.fingerprints) if force else self._missing_questions(message_id)
        classified = Classified(probabilities={}, message=None)
        state: dict[str, Any] | None = None

        if missing:
            message = self.box.get_message(message_id)
            if is_excluded(message):
                return None
            classified.message = message
            state = self._state(message, self._thread(thread_id))
            result = ask(self.client, self.config, state, missing)
            with self.db.transaction():
                self.db.add_message(message_id, thread_id, message.internal_date)
                for question, probabilities in result.probabilities.items():
                    self.db.save_answer(
                        message_id, question, self.fingerprints[question], probabilities
                    )
            classified.calls += 1
            classified.input_tokens += result.input_tokens

        answers = self.db.answers(message_id)
        probabilities = {q: answers[q].probabilities for q in self.fingerprints}

        request = secondary_request(self.config, probabilities[TYPE_QUESTION])
        if request is not None:
            cached = answers.get(SECONDARY_TYPE_QUESTION)
            if cached is None or set(cached.probabilities) != set(request.options):
                if classified.message is None:
                    classified.message = self.box.get_message(message_id)
                if state is None:
                    state = self._state(classified.message, self._thread(thread_id))
                second = ask_secondary(self.client, self.config, state, request)
                with self.db.transaction():
                    self.db.save_answer(
                        message_id,
                        SECONDARY_TYPE_QUESTION,
                        self.fingerprints[TYPE_QUESTION],
                        second.probabilities[SECONDARY_TYPE_QUESTION],
                    )
                classified.calls += 1
                classified.input_tokens += second.input_tokens
                probabilities[SECONDARY_TYPE_QUESTION] = second.probabilities[
                    SECONDARY_TYPE_QUESTION
                ]
            else:
                probabilities[SECONDARY_TYPE_QUESTION] = cached.probabilities
        classified.probabilities = probabilities
        return classified

    def _decide(
        self,
        probabilities: Mapping[str, Mapping[str, float]],
        thread_id: str,
        locks: Mapping[str, bool] | None = None,
    ) -> Decision:
        """Applique les règles. Le fil n'est lu (règle 6, « déjà répondu ») que si ✉️ To Reply
        est en jeu : posé ou en doute, et sans verrou."""
        answers = to_jev_answers(probabilities)
        locks = dict(locks or {})
        decision = decide(self.config, answers, locks=locks)
        reply = self.config.by_id.get(REPLY_LABEL_ID)
        if (
            reply is not None
            and reply.enabled
            and REPLY_LABEL_ID not in locks
            and REPLY_LABEL_ID in probabilities
            and probabilities[REPLY_LABEL_ID]["yes"] >= self.config.settings.doubt_min
            and self._thread(thread_id).last_from_user
        ):
            decision = decide(self.config, answers, user_replied_last=True, locks=locks)
        return decision


class DryRun(Classifier):
    """Classe sans rien modifier dans Gmail ; les décisions ne sont pas enregistrées."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.report = DryRunReport()
        self.rows: list[Row] = []

    def process(self, message_id: str, thread_id: str) -> None:
        """Classe un mail. Les erreurs d'accès (clé, jeton, quota du jour) remontent et arrêtent
        le passage ; les autres sont comptées et le passage continue."""
        try:
            self._process(message_id, thread_id)
        except STOPPING_ERRORS:
            raise
        except GmailNotFoundError:
            self.report.skipped += 1
        except (GmailError, JevError, EngineError) as exc:
            self.report.errors.append((message_id, str(exc)))
            raise

    def _process(self, message_id: str, thread_id: str) -> None:
        classified = self._classify(message_id, thread_id)
        if classified is None:
            self.report.skipped += 1
            return
        if classified.calls:
            self._count_call(classified.input_tokens, classified.calls)
        probabilities = classified.probabilities
        decision = self._decide(probabilities, thread_id)
        self._tally(decision, called_jev=bool(classified.calls))
        headers = (
            classified.message.headers
            if classified.message is not None
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

    def _count_call(self, tokens: int, calls: int = 1) -> None:
        self.report.jev_calls += calls
        self.report.input_tokens += tokens

    def _tally(self, decision: Decision, called_jev: bool) -> None:
        report = self.report
        report.total += 1
        report.label_counts.update(decision.labels)
        if not decision.labels:
            report.no_label += 1
        elif decision.labels == {REVIEW_LABEL_ID}:
            report.review_only += 1
        if not called_jev:
            report.cached += 1

    def run(
        self,
        messages: Iterable[tuple[str, str]],
        progress: Callable[[], None] | None = None,
    ) -> DryRunReport:
        consecutive = 0
        for message_id, thread_id in messages:
            try:
                self.process(message_id, thread_id)
                consecutive = 0
            except STOPPING_ERRORS:
                raise
            except (GmailError, JevError, EngineError):
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    self.report.stopped = (
                        f"{consecutive} erreurs d'affilée : passage arrêté, Gmail ou Jev semble "
                        "indisponible."
                    )
                    break
            if progress:
                progress()
        return self.report

    def write_csv(self, path: Path) -> None:
        write_rows_csv(self.config, self.rows, path)


def write_rows_csv(
    config: Config, rows: Iterable[Row], path: Path, *, changes: bool = False
) -> None:
    """Une ligne par mail : identifiants, en-têtes, labels, raisons, probabilités. Avec
    `changes`, deux colonnes de plus : labels ajoutés et retirés dans Gmail par ce passage."""
    type_options = [t.id for t in config.types] + [NONE_OPTION]
    yes_no = [label.id for label in config.yes_no_labels]
    order = {label.id: i for i, label in enumerate(config.labels)}

    def names(ids: Iterable[str]) -> str:
        return " ".join(config.by_id[i].name for i in sorted(ids, key=lambda i: order.get(i, 999)))

    columns = ["message_id", "date", "from", "subject", "labels"]
    if changes:
        columns += ["added", "removed"]
    columns += (
        ["review_reasons", "to_review"]
        + [f"type:{o}" for o in type_options]
        + ["secondary"]
        + yes_no
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            primary = row.probabilities[TYPE_QUESTION]
            secondary = row.probabilities.get(SECONDARY_TYPE_QUESTION)
            values = [row.message_id, row.date, row.sender, row.subject, names(row.decision.labels)]
            if changes:
                values += [names(row.added), names(row.removed)]
            values += [
                " | ".join(row.decision.review_reasons),
                "yes" if row.in_review else "no",
                *(f"{primary.get(o, 0):.3f}" for o in type_options),
                " ".join(f"{k}={v:.2f}" for k, v in (secondary or {}).items()),
                *(f"{row.probabilities[q]['yes']:.3f}" for q in yes_no),
            ]
            writer.writerow(values)


# ================================================================ vrai passage


@dataclass
class RunReport:
    listed: int = 0
    """Mails dans le périmètre du passage."""
    total: int = 0
    """Mails classés (hors mails ignorés et erreurs)."""
    new: int = 0
    """Mails classés pour la première fois."""
    asked: int = 0
    """Mails pour lesquels Jev a été interrogé (nouveaux ou questions modifiées)."""
    skipped: int = 0
    changed: int = 0
    """Mails modifiés dans Gmail."""
    added: Counter[str] = field(default_factory=Counter)
    removed: Counter[str] = field(default_factory=Counter)
    label_counts: Counter[str] = field(default_factory=Counter)
    """Labels présents après le passage, sur les mails du périmètre."""
    no_label: int = 0
    review_only: int = 0
    doubts: Counter[str] = field(default_factory=Counter)
    """Nombre de mails par label en doute (`type` pour un type incertain)."""
    corrections: list[tuple[str, str, str]] = field(default_factory=list)
    """Corrections manuelles repérées : (mail, label, « added » ou « removed »)."""
    errors: list[tuple[str, str]] = field(default_factory=list)
    jev_calls: int = 0
    input_tokens: int = 0
    stopped: str | None = None
    sync: SyncResult | None = None

    def as_stats(self) -> dict[str, object]:
        """Compteurs enregistrés dans la table `runs` (aucun texte de mail)."""
        return {
            "listed": self.listed,
            "total": self.total,
            "new": self.new,
            "asked": self.asked,
            "skipped": self.skipped,
            "changed": self.changed,
            "added": dict(self.added),
            "removed": dict(self.removed),
            "labels": dict(self.label_counts),
            "no_label": self.no_label,
            "review_only": self.review_only,
            "doubts": dict(self.doubts),
            "corrections": len(self.corrections),
            "errors": len(self.errors),
            "jev_calls": self.jev_calls,
            "input_tokens": self.input_tokens,
            "stopped": self.stopped,
        }


@dataclass
class _Change:
    message_id: str
    decision: Decision
    probabilities: dict[str, dict[str, float]]
    added: frozenset[str]
    removed: frozenset[str]
    message: GmailMessage | None


class Runner(Classifier):
    """Vrai passage : pose et retire les labels dans Gmail."""

    def __init__(self, *args: Any, force: bool = False, keep_rows: bool = False, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.force = force
        """`resort --all` : repose toutes les questions, même celles déjà à jour."""
        self.keep_rows = keep_rows
        self.report = RunReport()
        self.gmail_ids: dict[str, str] = {}
        self.present: dict[str, set[str]] = {}
        self._changes: list[_Change] = []

    # --------------------------------------------------------------- préparation

    @property
    def tracked(self) -> set[str]:
        """Labels que le programme gère sur les mails : les labels évalués et 🤔 To Review."""
        config = self.config
        ids = {t.id for t in config.types} | {lb.id for lb in config.yes_no_labels}
        review = config.by_id.get(REVIEW_LABEL_ID)
        if review is not None and review.enabled:
            ids.add(REVIEW_LABEL_ID)
        return {i for i in ids if i in self.gmail_ids}

    def prepare(self, query: str | None, limit: int | None) -> list[tuple[str, str]]:
        """Synchronise les labels, liste les mails du périmètre et relève quels mails portent
        chaque label géré."""
        sync = sync_labels(self.box, self.db, self.config)
        self.report.sync = sync
        self.gmail_ids = dict(sync.gmail_ids)
        missing = sorted(i for i in self._evaluated if i not in self.gmail_ids)
        if missing:
            raise LabelSyncError(f"Labels Gmail manquants après synchronisation : {missing}")
        messages = self.list_messages(query, limit)
        in_scope = {message_id for message_id, _ in messages}
        present: dict[str, set[str]] = {}
        for label_id in sorted(self.tracked):
            for message_id, _ in self.box.list_messages(
                None, None, label_ids=[self.gmail_ids[label_id]]
            ):
                if message_id in in_scope:
                    present.setdefault(message_id, set()).add(label_id)
        self.present = present
        self.report.listed = len(messages)
        return messages

    @property
    def _evaluated(self) -> set[str]:
        ids = {t.id for t in self.config.types} | {lb.id for lb in self.config.yes_no_labels}
        review = self.config.by_id.get(REVIEW_LABEL_ID)
        if review is not None and review.enabled:
            ids.add(REVIEW_LABEL_ID)
        return ids

    # ------------------------------------------------------------------ passage

    def run(
        self,
        messages: Iterable[tuple[str, str]],
        progress: Callable[[], None] | None = None,
    ) -> RunReport:
        records = self.db.messages()
        applied = self.db.all_applied_labels()
        locks = self.db.all_locks()
        consecutive = 0
        for message_id, thread_id in messages:
            try:
                self._process(
                    message_id,
                    thread_id,
                    records.get(message_id),
                    applied.get(message_id, set()),
                    locks.get(message_id, {}),
                )
                consecutive = 0
            except GmailQuotaError as exc:
                self.report.stopped = str(exc)
                log.error("%s", exc)
                break
            except STOPPING_ERRORS:
                raise
            except GmailNotFoundError:
                self.report.skipped += 1
            except (GmailError, JevError, EngineError) as exc:
                self.report.errors.append((message_id, str(exc)))
                log.error("Erreur sur %s : %s", message_id, exc)
                with self.db.transaction():
                    self.db.add_message(message_id, thread_id, None)
                    self.db.mark_error(message_id, str(exc))
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    self.report.stopped = (
                        f"{consecutive} erreurs d'affilée : passage arrêté, Gmail ou Jev semble "
                        "indisponible. Le prochain passage reprendra."
                    )
                    log.error("%s", self.report.stopped)
                    break
            if progress:
                progress()
        return self.report

    def _process(
        self,
        message_id: str,
        thread_id: str,
        record: MessageRecord | None,
        applied: set[str],
        locks: dict[str, bool],
    ) -> None:
        tracked = self.tracked
        present = self.present.get(message_id, set()) & tracked
        pending = record is not None and record.sync_pending

        # 1. Corrections manuelles : écart entre Gmail et ce que le programme a posé.
        new_locks = dict(locks)
        corrections: list[tuple[str, str]] = []
        if not pending:
            for label_id in sorted(tracked):
                is_present = label_id in present
                if label_id in locks:
                    if locks[label_id] != is_present:
                        new_locks[label_id] = is_present
                        corrections.append((label_id, "added" if is_present else "removed"))
                elif label_id in applied and not is_present:
                    new_locks[label_id] = False
                    corrections.append((label_id, "removed"))
                elif label_id not in applied and is_present:
                    new_locks[label_id] = True
                    corrections.append((label_id, "added"))

        # 2. Réponses de Jev, puis règles.
        classified = self._classify(message_id, thread_id, force=self.force)
        if classified is None:
            self.report.skipped += 1
            return
        probabilities = classified.probabilities
        decision = self._decide(probabilities, thread_id, new_locks)

        # 3. Écart à combler dans Gmail.
        desired = set(decision.labels) & tracked
        add = desired - present
        remove = (present & applied) - desired
        locked_present = {i for i, value in new_locks.items() if value}
        new_applied = (desired - locked_present) | (applied - tracked)

        first_time = record is None or record.status != "done"
        with self.db.transaction():
            if record is None and classified.message is None:
                self.db.add_message(message_id, thread_id, None)
            for label_id, action in corrections:
                self.db.set_lock(message_id, label_id, new_locks[label_id])
                self.db.record_correction(
                    message_id,
                    label_id,
                    action,  # type: ignore[arg-type]
                    probability=self._probability(probabilities, label_id),
                    question_fingerprint=self._question_fingerprint(label_id),
                )
            self.db.save_decision(
                message_id,
                new_applied,
                rules_fingerprint=self.config.rules_fingerprint(),
                review_reasons=decision.review_reasons,
            )
            if add or remove:
                self.db.set_sync_pending(message_id, True)

        if add or remove:
            self.box.modify_labels(
                message_id,
                [self.gmail_ids[i] for i in sorted(add)],
                [self.gmail_ids[i] for i in sorted(remove)],
            )
            with self.db.transaction():
                self.db.set_sync_pending(message_id, False)

        # 4. Bilan et journal (identifiants seulement, jamais le contenu du mail).
        report = self.report
        report.total += 1
        if first_time:
            report.new += 1
        if classified.calls:
            report.asked += 1
            report.jev_calls += classified.calls
            report.input_tokens += classified.input_tokens
        report.label_counts.update(decision.labels)
        if not decision.labels:
            report.no_label += 1
        elif decision.labels == {REVIEW_LABEL_ID}:
            report.review_only += 1
        for doubt in decision.doubts:
            report.doubts["type" if doubt.family == "type" else doubt.label_id] += 1
        for label_id, action in corrections:
            report.corrections.append((message_id, label_id, action))
            log.info("Correction manuelle sur %s : %s %s", message_id, label_id, action)
        if add or remove:
            report.changed += 1
            report.added.update(add)
            report.removed.update(remove)
            log.info(
                "%s : + %s / - %s",
                message_id,
                ", ".join(sorted(add)) or "rien",
                ", ".join(sorted(remove)) or "rien",
            )
        if decision.review_reasons and (classified.calls or add or remove):
            log.info("Doutes sur %s : %s", message_id, " ; ".join(decision.review_reasons))
        if self.keep_rows:
            self._changes.append(
                _Change(
                    message_id,
                    decision,
                    probabilities,
                    frozenset(add),
                    frozenset(remove),
                    classified.message,
                )
            )

    def _probability(
        self, probabilities: Mapping[str, Mapping[str, float]], label_id: str
    ) -> float | None:
        label = self.config.by_id.get(label_id)
        if label is None:
            return None
        if label.family == "type":
            return probabilities[TYPE_QUESTION].get(label_id)
        if label_id in probabilities:
            return probabilities[label_id].get("yes")
        return None

    def _question_fingerprint(self, label_id: str) -> str | None:
        label = self.config.by_id.get(label_id)
        if label is None:
            return None
        if label.family == "type":
            return self.fingerprints.get(TYPE_QUESTION)
        return self.fingerprints.get(label_id)

    def rows(self, *, changed_only: bool = True) -> list[Row]:
        """Lignes du CSV (avec `keep_rows`) : les en-têtes manquants sont lus dans Gmail."""
        rows = []
        for change in self._changes:
            if changed_only and not (change.added or change.removed):
                continue
            headers = (
                change.message.headers
                if change.message is not None
                else self.box.get_headers(change.message_id, ["From", "Subject", "Date"])
            )
            rows.append(
                Row(
                    message_id=change.message_id,
                    date=str(headers.get("Date") or ""),
                    sender=str(headers.get("From") or ""),
                    subject=str(headers.get("Subject") or ""),
                    decision=change.decision,
                    probabilities=change.probabilities,
                    added=tuple(sorted(change.added)),
                    removed=tuple(sorted(change.removed)),
                )
            )
        return rows


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
