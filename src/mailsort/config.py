"""Chargement, validation et empreintes de `labels.yaml`.

Le programme s'arrête sans rien toucher si le fichier est invalide : `load_config` lève alors
`ConfigError` avec la liste complète des erreurs trouvées.

Deux sortes d'empreintes permettent de savoir ce qu'il faut refaire après une modification :

- l'empreinte de **question** (nom et description) : si elle change, la question est reposée à
  Jev. Les types forment une seule question à choix, donc ils partagent une seule empreinte
  (`TYPE_QUESTION`), qui couvre aussi la description de « None of the above » ;
- l'empreinte des **règles** (seuils, `enabled`, `never_with`, `protects_from`) : si elle change,
  les règles sont recalculées à partir des probabilités enregistrées, sans appeler Jev.
"""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Family = Literal["type", "domain", "action", "cleanup", "system"]
FAMILIES: tuple[str, ...] = ("type", "domain", "action", "cleanup", "system")
YES_NO_FAMILIES = frozenset({"domain", "action", "cleanup"})

REVIEW_LABEL_ID = "to_review"
TYPE_QUESTION = "@types"
"""Clé de l'empreinte partagée par tous les types (un `id` de label ne peut pas contenir `@`)."""

Probability = Annotated[float, Field(ge=0, le=1)]


class ConfigError(Exception):
    """`labels.yaml` est illisible ou invalide."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        details = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"labels.yaml invalide ({len(errors)} erreur(s)) :\n{details}")


def _normalize_text(value: str) -> str:
    """Réduit les blancs : reformater une description dans le YAML ne change pas l'empreinte."""
    return " ".join(value.split())


class _Strict(BaseModel):
    # `extra="forbid"` : une faute de frappe dans un nom de champ est une erreur, pas un oubli.
    model_config = ConfigDict(extra="forbid", frozen=True)


class TypeSettings(_Strict):
    primary_min: Probability
    secondary_trigger: Probability
    secondary_min: Probability
    none_description: str = Field(min_length=1)

    @field_validator("none_description")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _normalize_text(value)


class Visibility(_Strict):
    label_list: Literal["show", "show_if_unread", "hide"]
    message_list: Literal["show", "hide"]


class Settings(_Strict):
    types: TypeSettings
    yes_no_min: Probability
    doubt_min: Probability
    parents: dict[Family, str]
    visibility: dict[Family, Visibility]


class Label(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    family: Family
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True
    threshold: Probability | None = None
    never_with: tuple[str, ...] = ()
    protects_from: tuple[str, ...] = ()
    description: str = ""

    @field_validator("description")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _normalize_text(value)

    @field_validator("name")
    @classmethod
    def _no_slash(cls, value: str) -> str:
        if "/" in value:
            raise ValueError("ne doit pas contenir « / » (Gmail en ferait un sous-label)")
        return value.strip()

    @property
    def is_yes_no(self) -> bool:
        return self.family in YES_NO_FAMILIES


class Config(_Strict):
    settings: Settings
    labels: tuple[Label, ...]

    # ------------------------------------------------------------------ accès

    @cached_property
    def by_id(self) -> dict[str, Label]:
        return {label.id: label for label in self.labels}

    @property
    def types(self) -> list[Label]:
        """Types évalués (activés), dans l'ordre du fichier."""
        return [lb for lb in self.labels if lb.family == "type" and lb.enabled]

    @property
    def yes_no_labels(self) -> list[Label]:
        """Labels posés par une question oui/non (activés), dans l'ordre du fichier."""
        return [lb for lb in self.labels if lb.is_yes_no and lb.enabled]

    def threshold_for(self, label: Label) -> float:
        """Seuil oui/non effectif : celui du label, sinon `settings.yes_no_min`."""
        return label.threshold if label.threshold is not None else self.settings.yes_no_min

    def full_name(self, label: Label) -> str:
        """Nom complet du label dans Gmail, par exemple « Types/💳 Payments »."""
        return f"{self.settings.parents[label.family]}/{label.name}"

    # -------------------------------------------------------------- empreintes

    def question_fingerprints(self) -> dict[str, str]:
        """Empreinte de chaque question posée à Jev.

        Clés : `TYPE_QUESTION` pour le choix de type, puis l'`id` de chaque label oui/non activé.
        """
        fingerprints = {}
        if self.types:
            fingerprints[TYPE_QUESTION] = _hash(
                {
                    "none": self.settings.types.none_description,
                    "options": [[t.id, t.name, t.description] for t in self.types],
                }
            )
        for label in self.yes_no_labels:
            fingerprints[label.id] = _hash([label.id, label.name, label.description])
        return fingerprints

    def rules_fingerprint(self) -> str:
        """Empreinte de tout ce qui sert aux règles de décision, sans les questions."""
        settings = self.settings
        return _hash(
            {
                "types": [
                    settings.types.primary_min,
                    settings.types.secondary_trigger,
                    settings.types.secondary_min,
                ],
                "yes_no_min": settings.yes_no_min,
                "doubt_min": settings.doubt_min,
                "labels": [
                    [
                        lb.id,
                        lb.family,
                        lb.enabled,
                        lb.threshold,
                        sorted(lb.never_with),
                        sorted(lb.protects_from),
                    ]
                    for lb in self.labels
                ],
            }
        )

    # -------------------------------------------------------------- cohérence

    def consistency_errors(self) -> list[str]:
        """Erreurs que le schéma seul ne peut pas voir (liens entre labels, seuils)."""
        errors: list[str] = []
        settings = self.settings

        if settings.doubt_min >= settings.yes_no_min:
            errors.append(
                f"settings : doubt_min ({settings.doubt_min}) doit être inférieur "
                f"à yes_no_min ({settings.yes_no_min})"
            )

        used_families = {lb.family for lb in self.labels}
        for family in sorted(used_families - set(settings.parents)):
            errors.append(f"settings.parents : aucun parent pour la famille « {family} »")
        for family in sorted(used_families - set(settings.visibility)):
            errors.append(f"settings.visibility : aucune visibilité pour la famille « {family} »")

        seen_ids: set[str] = set()
        seen_names: dict[str, str] = {}
        for label in self.labels:
            where = f"label « {label.id} »"

            if label.id in seen_ids:
                errors.append(f"{where} : id en double")
            seen_ids.add(label.id)

            if label.family in settings.parents:
                full = self.full_name(label).casefold()
                if full in seen_names and seen_names[full] != label.id:
                    errors.append(
                        f"{where} : même nom Gmail que « {seen_names[full]} » "
                        f"({self.full_name(label)})"
                    )
                seen_names.setdefault(full, label.id)

            if label.family != "system" and not label.description:
                errors.append(f"{where} : description manquante (c'est le texte envoyé à Jev)")

            if label.threshold is not None:
                if not label.is_yes_no:
                    errors.append(
                        f"{where} : threshold n'a de sens que pour un label oui/non "
                        f"(famille domain, action ou cleanup), pas pour « {label.family} »"
                    )
                elif label.threshold <= settings.doubt_min:
                    errors.append(
                        f"{where} : threshold ({label.threshold}) doit être supérieur "
                        f"à doubt_min ({settings.doubt_min})"
                    )

            if label.never_with and label.family != "type":
                errors.append(f"{where} : never_with n'est permis que sur un type")
            for target in label.never_with:
                errors.extend(self._target_errors(label, "never_with", target, must_be_type=True))

            for target in label.protects_from:
                errors.extend(self._target_errors(label, "protects_from", target))

        review = self.by_id.get(REVIEW_LABEL_ID)
        if review is None:
            errors.append(f"label « {REVIEW_LABEL_ID} » manquant (posé par le code)")
        elif review.family != "system":
            errors.append(f"label « {REVIEW_LABEL_ID} » : la famille doit être « system »")

        return errors

    def _target_errors(
        self, label: Label, field: str, target: str, *, must_be_type: bool = False
    ) -> list[str]:
        where = f"label « {label.id} »"
        if target == label.id:
            return [f"{where} : {field} vise le label lui-même"]
        other = self.by_id.get(target)
        if other is None:
            return [f"{where} : {field} vise un label inexistant « {target} »"]
        if must_be_type and other.family != "type":
            return [f"{where} : {field} vise « {target} », qui n'est pas un type"]
        return []


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _format_validation_error(error: ValidationError, raw: Any) -> list[str]:
    """Traduit les erreurs Pydantic en lignes lisibles, en nommant le label concerné."""
    lines = []
    for item in error.errors():
        loc = list(item["loc"])
        if len(loc) >= 2 and loc[0] == "labels" and isinstance(loc[1], int):
            index = loc[1]
            label_id = None
            if isinstance(raw, dict) and isinstance(raw.get("labels"), list):
                entry = raw["labels"][index] if index < len(raw["labels"]) else None
                if isinstance(entry, dict):
                    label_id = entry.get("id")
            head = f"label « {label_id} »" if label_id else f"labels[{index}]"
            rest = ".".join(str(part) for part in loc[2:])
            where = f"{head}.{rest}" if rest else head
        else:
            where = ".".join(str(part) for part in loc) or "fichier"
        lines.append(f"{where} : {item['msg']}")
    return lines


def load_config(path: str | Path) -> Config:
    """Lit et valide `labels.yaml`. Lève `ConfigError` avec toutes les erreurs trouvées."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError([f"fichier introuvable : {path}"]) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML illisible : {exc}"]) from exc

    if not isinstance(raw, dict):
        raise ConfigError(["le fichier doit contenir les clés « settings » et « labels »"])

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, raw)) from exc

    errors = config.consistency_errors()
    if errors:
        raise ConfigError(errors)
    return config
