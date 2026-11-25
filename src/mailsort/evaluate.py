"""Mesure de qualité (`mailsort eval`) : le classement de Jev face à des labels Gmail posés à la
main ou par des filtres.

Pour chaque paire « label Gmail de référence » → « label de labels.yaml » :

- les mails qui portent la référence sont classés (réponses de Jev reprises si elles existent,
  sinon demandées, sans rien modifier dans Gmail) ;
- la population comparée réunit ces mails et tous ceux déjà classés dans la base ;
- on compte les accords et les désaccords : référence sans le label (oubli de Jev), label sans
  la référence (ajout de Jev).

Rien n'est modifié dans Gmail ; la règle « déjà répondu » n'entre pas en jeu (elle ne concerne
que ✉️ To Reply).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from mailsort.config import SECONDARY_TYPE_QUESTION, Config
from mailsort.db import Database
from mailsort.engine import decide
from mailsort.gmail import DEFAULT_QUERY, Gmail
from mailsort.jev import JevClient, to_jev_answers
from mailsort.sorter import DryRun, DryRunReport

DEFAULT_PAIRS: tuple[tuple[str, str], ...] = (
    ("💳 Paiements", "payments"),
    ("📦 Réceptions", "deliveries"),
)
"""Labels classés à la main et par filtres avant mailsort (voir la spec, « Mise en route »)."""


@dataclass
class Comparison:
    gmail_name: str
    label_id: str
    found: bool
    reference: int = 0
    population: int = 0
    agree_yes: int = 0
    agree_no: int = 0
    missed: list[str] = field(default_factory=list)
    """Mails qui portent la référence, mais pas le label selon Jev."""
    extra: list[str] = field(default_factory=list)
    """Mails qui ont le label selon Jev, mais pas la référence."""

    @property
    def agreement(self) -> float:
        return (self.agree_yes + self.agree_no) / self.population if self.population else 0.0

    @property
    def recall(self) -> float:
        return self.agree_yes / self.reference if self.reference else 0.0

    @property
    def precision(self) -> float:
        predicted = self.agree_yes + len(self.extra)
        return self.agree_yes / predicted if predicted else 0.0


def parse_pair(text: str, config: Config) -> tuple[str, str]:
    """« Nom du label Gmail=id » → (nom, id). Lève ValueError si la forme ou l'id est invalide."""
    name, sep, label_id = text.rpartition("=")
    name, label_id = name.strip(), label_id.strip()
    if not sep or not name or not label_id:
        raise ValueError(f"« {text} » : attendu « Nom du label Gmail=id_du_label »")
    if label_id not in config.by_id:
        raise ValueError(f"« {text} » : label inconnu dans labels.yaml : {label_id}")
    return name, label_id


class Evaluation:
    def __init__(
        self,
        box: Gmail,
        client: JevClient,
        db: Database,
        config: Config,
        pairs: Iterable[tuple[str, str]],
        *,
        user_addresses: Iterable[str] = (),
    ):
        self.box = box
        self.db = db
        self.config = config
        self.pairs = list(pairs)
        self.dry = DryRun(box, client, db, config, user_addresses=user_addresses)
        labels = {label.name.casefold(): label for label in box.list_labels()}
        self.references: dict[str, list[tuple[str, str]]] = {}
        for name, _ in self.pairs:
            found = labels.get(name.casefold())
            if found is not None and name not in self.references:
                self.references[name] = list(
                    box.list_messages(DEFAULT_QUERY, None, label_ids=[found.id])
                )

    def messages_to_classify(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for messages in self.references.values():
            for message_id, thread_id in messages:
                seen.setdefault(message_id, thread_id)
        return list(seen.items())

    def classify(self, progress: Callable[[], None] | None = None) -> DryRunReport:
        return self.dry.run(self.messages_to_classify(), progress=progress)

    def compare(self) -> list[Comparison]:
        predicted = self._predictions()
        results = []
        for name, label_id in self.pairs:
            if name not in self.references:
                results.append(Comparison(name, label_id, found=False))
                continue
            reference = {m for m, _ in self.references[name] if m in predicted}
            comparison = Comparison(
                name, label_id, found=True, reference=len(reference), population=len(predicted)
            )
            for message_id, labels in predicted.items():
                has = label_id in labels
                if message_id in reference:
                    if has:
                        comparison.agree_yes += 1
                    else:
                        comparison.missed.append(message_id)
                elif has:
                    comparison.extra.append(message_id)
                else:
                    comparison.agree_no += 1
            results.append(comparison)
        return results

    def _predictions(self) -> dict[str, frozenset[str]]:
        """Labels décidés pour chaque mail classé avec les questions actuelles."""
        fingerprints = self.dry.fingerprints
        predictions: dict[str, frozenset[str]] = {}
        for message_id in self.db.message_ids():
            answers = self.db.answers(message_id)
            if any(
                q not in answers or answers[q].fingerprint != fp for q, fp in fingerprints.items()
            ):
                continue
            probabilities = {q: answers[q].probabilities for q in fingerprints}
            if SECONDARY_TYPE_QUESTION in answers:
                probabilities[SECONDARY_TYPE_QUESTION] = answers[
                    SECONDARY_TYPE_QUESTION
                ].probabilities
            try:
                decision = decide(self.config, to_jev_answers(probabilities))
            except ValueError:
                continue  # second passage d'une ancienne liste de types : ignoré
            predictions[message_id] = decision.labels
        return predictions
