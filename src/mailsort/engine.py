"""Moteur de décision : des probabilités de Jev aux labels à poser.

Tout est pur : aucune entrée-sortie, aucun appel réseau. Les règles sont celles de la section
« Règles de décision » de la spec, appliquées dans cet ordre :

1. type principal : posé s'il atteint `primary_min`, ou si, avec un autre type en deuxième
   position, la somme des deux atteint `primary_pair_sum` ;
2. type secondaire (second passage) ;
3. paires interdites ;
4. labels oui/non ;
5. règle « déjà répondu » (✉️ To Reply) ;
6. verrous manuels (la correction d'Alexis l'emporte toujours) ;
7. protections (`protects_from`) ;
8. 🤔 To Review seulement pour un mail sans repère (ni type, ni domaine, ni nettoyage). Les
   doutes (score dans la zone de doute) ne posent pas le label et sont seulement notés.

Le programme appelle d'abord `secondary_request` pour savoir s'il faut un second passage des
types, puis `decide` avec toutes les réponses de Jev.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mailsort.config import REPLY_LABEL_ID, REVIEW_LABEL_ID, Config

NONE_OPTION = "@none"
"""Option « None of the above » du choix de type (un `id` de label ne peut pas contenir `@`)."""

LANDMARK_FAMILIES = frozenset({"type", "domain", "cleanup"})
"""Familles qui donnent un repère au mail (règle 8)."""


class EngineError(ValueError):
    """Les réponses de Jev ne correspondent pas à la configuration (bug en amont)."""


@dataclass(frozen=True)
class SecondaryRequest:
    """Second passage des types à demander à Jev."""

    winner: str
    """Type principal, exclu des options."""
    candidate: str
    """Type qui a dépassé `secondary_trigger` au premier passage."""
    options: tuple[str, ...]
    """Options à proposer : les types sauf le gagnant, puis `NONE_OPTION`."""


@dataclass(frozen=True)
class JevAnswers:
    """Probabilités renvoyées par Jev pour un mail."""

    primary: Mapping[str, float]
    """Choix de type : une probabilité par type activé et pour `NONE_OPTION`."""
    yes_no: Mapping[str, float]
    """Une probabilité de « oui » par label oui/non activé (les clés en trop sont ignorées)."""
    secondary: Mapping[str, float] | None = None
    """Second passage des types, s'il a eu lieu."""


@dataclass(frozen=True)
class Doubt:
    """Une question dont la réponse est tombée dans la zone de doute."""

    family: str
    label_id: str
    """Label douteux ; pour le choix de type, l'option gagnante (éventuellement `NONE_OPTION`)."""
    probability: float


@dataclass(frozen=True)
class Decision:
    labels: frozenset[str]
    """Labels qui doivent être présents sur le mail, parmi les labels activés et 🤔 To Review."""
    primary_type: str | None
    secondary_type: str | None
    doubts: tuple[Doubt, ...]
    """Doutes réels : ni tranchés par un verrou, ni rendus sans objet par une protection."""
    blocked: frozenset[str]
    """Labels qui auraient été posés sans une protection."""
    review_reasons: tuple[str, ...]
    """Doutes et absence de repère, en clair pour le journal, `explain` et le CSV. Seule
    l'absence de repère pose 🤔 To Review : un mail peut avoir des doutes sans y aller."""
    no_landmark: bool = False
    """Aucun type, domaine ni nettoyage n'est posé (et aucune correction ne l'a tranché)."""


# --------------------------------------------------------------------- outils


def forbidden_pair(config: Config, a: str, b: str) -> bool:
    """Vrai si `a` et `b` ne peuvent pas être types ensemble (`never_with`, dans les deux sens)."""
    return b in config.by_id[a].never_with or a in config.by_id[b].never_with


def _check_probability(where: str, value: float) -> None:
    if not 0 <= value <= 1:
        raise EngineError(f"{where} : probabilité hors de [0, 1] ({value})")


def _check_options(where: str, probs: Mapping[str, float], expected: tuple[str, ...]) -> None:
    missing = [o for o in expected if o not in probs]
    extra = [o for o in probs if o not in expected]
    if missing or extra:
        raise EngineError(
            f"{where} : options attendues {list(expected)}, manquantes {missing}, en trop {extra}"
        )
    for option, value in probs.items():
        _check_probability(f"{where}[{option}]", value)


def _winner(probs: Mapping[str, float], order: tuple[str, ...]) -> tuple[str, float]:
    """Option la plus probable ; en cas d'égalité, la première dans l'ordre du fichier."""
    best = max(order, key=lambda o: (probs[o], -order.index(o)))
    return best, probs[best]


def _type_options(config: Config) -> tuple[str, ...]:
    return tuple(t.id for t in config.types) + (NONE_OPTION,)


def primary_confident(config: Config, primary: Mapping[str, float]) -> bool:
    """Le type gagnant est assez sûr pour être posé (règle 1).

    Il l'est s'il atteint `primary_min`, ou si les deux premières options sont des types et que
    leurs probabilités cumulées atteignent `primary_pair_sum` : Jev partage alors sa certitude
    entre deux types (par exemple une quittance, à la fois paiement et document officiel).
    Avec « None of the above » en première ou deuxième position, seul `primary_min` compte :
    le partage traduit alors un doute sur le type, pas un mail qui relève de deux types.
    """
    options = _type_options(config)
    settings = config.settings.types
    ranked = sorted(options, key=lambda o: (-primary[o], options.index(o)))
    best = primary[ranked[0]]
    if best >= settings.primary_min:
        return True
    pair = ranked[:2]
    return (
        len(pair) == 2
        and NONE_OPTION not in pair
        and best + primary[pair[1]] >= settings.primary_pair_sum
    )


# ------------------------------------------------------------ second passage


def secondary_request(config: Config, primary: Mapping[str, float]) -> SecondaryRequest | None:
    """Indique s'il faut reposer la question des types sans le gagnant (règles 2 et 3).

    Pas de second passage si le gagnant est « None of the above » ou n'est pas posé (règle 1), si
    aucun autre type n'atteint `secondary_trigger`, ou si ce type forme une paire interdite avec
    le gagnant (il ne pourrait de toute façon pas être posé).
    """
    options = _type_options(config)
    _check_options("primary", primary, options)
    settings = config.settings.types

    winner, _ = _winner(primary, options)
    if winner == NONE_OPTION or not primary_confident(config, primary):
        return None

    candidates = [
        t.id
        for t in config.types
        if t.id != winner
        and primary[t.id] >= settings.secondary_trigger
        and not forbidden_pair(config, winner, t.id)
    ]
    if not candidates:
        return None
    candidate = max(candidates, key=lambda c: primary[c])
    return SecondaryRequest(
        winner=winner,
        candidate=candidate,
        options=tuple(o for o in options if o != winner),
    )


# ---------------------------------------------------------------- décision


def decide(
    config: Config,
    answers: JevAnswers,
    *,
    user_replied_last: bool = False,
    locks: Mapping[str, bool] | None = None,
) -> Decision:
    """Applique les règles de décision.

    `user_replied_last` : le dernier message du fil vient de l'utilisateur (règle 6).
    `locks` : corrections manuelles, `id` → True (verrouillé présent) ou False (verrouillé absent).
    """
    locks = dict(locks or {})
    settings = config.settings
    by_id = config.by_id

    # 1. Type principal ------------------------------------------------------
    options = _type_options(config)
    _check_options("primary", answers.primary, options)
    winner, winner_probability = _winner(answers.primary, options)

    computed: set[str] = set()
    doubts: list[Doubt] = []
    primary_type: str | None = None
    if not primary_confident(config, answers.primary):
        doubts.append(Doubt("type", winner, winner_probability))
    elif winner != NONE_OPTION:
        primary_type = winner
        computed.add(winner)

    # 2-3. Type secondaire ---------------------------------------------------
    secondary_type: str | None = None
    request = secondary_request(config, answers.primary)
    if request is not None and answers.secondary is not None:
        _check_options("secondary", answers.secondary, request.options)
        second, second_probability = _winner(answers.secondary, request.options)
        if (
            second == request.candidate
            and second_probability >= settings.types.secondary_min
            and not forbidden_pair(config, request.winner, second)
        ):
            secondary_type = second
            computed.add(second)

    # 4. Labels oui/non ------------------------------------------------------
    yes_no_doubts: list[Doubt] = []
    for label in config.yes_no_labels:
        if label.id not in answers.yes_no:
            raise EngineError(f"yes_no : probabilité manquante pour « {label.id} »")
        probability = answers.yes_no[label.id]
        _check_probability(f"yes_no[{label.id}]", probability)
        if probability >= config.threshold_for(label):
            computed.add(label.id)
        elif probability >= settings.doubt_min:
            yes_no_doubts.append(Doubt(label.family, label.id, probability))

    # 5. Déjà répondu --------------------------------------------------------
    replied_out: set[str] = set()
    if user_replied_last and REPLY_LABEL_ID in by_id:
        replied_out.add(REPLY_LABEL_ID)
        computed.discard(REPLY_LABEL_ID)

    # 6. Verrous manuels -----------------------------------------------------
    evaluated = {t.id for t in config.types} | {lb.id for lb in config.yes_no_labels}
    locked_present = {i for i, present in locks.items() if present and i in evaluated}
    locked_absent = {i for i, present in locks.items() if not present and i in evaluated}
    final = (computed - locked_absent) | locked_present

    # 7. Protections ---------------------------------------------------------
    protected_out: set[str] = set()
    for label_id in final:
        protected_out.update(by_id[label_id].protects_from)
    blocked = (final & protected_out) - locked_present
    final -= blocked

    # 8. Doutes et 🤔 To Review ----------------------------------------------
    for doubt in yes_no_doubts:
        if doubt.label_id in locks or doubt.label_id in protected_out:
            continue
        if doubt.label_id in replied_out:
            continue
        doubts.append(doubt)

    locked_families = {by_id[i].family for i in locks if i in by_id}
    # Une correction manuelle sur un label d'une famille tranche les doutes de cette famille.
    doubts = [d for d in doubts if d.family not in locked_families]
    reasons: list[str] = []
    for doubt in doubts:
        if doubt.family == "type":
            reasons.append(f"type incertain ({doubt.label_id} à {doubt.probability:.2f})")
        else:
            reasons.append(f"doute sur {doubt.label_id} ({doubt.probability:.2f})")

    has_landmark = any(by_id[i].family in LANDMARK_FAMILIES for i in final)
    no_landmark = not has_landmark and not (locked_families & LANDMARK_FAMILIES)
    if no_landmark:
        reasons.append("aucun repère : ni type, ni domaine, ni nettoyage")

    review = by_id.get(REVIEW_LABEL_ID)
    if review is not None and review.enabled:
        wants_review = no_landmark
        if REVIEW_LABEL_ID in locks:
            wants_review = locks[REVIEW_LABEL_ID]
        if wants_review:
            final.add(REVIEW_LABEL_ID)

    return Decision(
        labels=frozenset(final),
        primary_type=primary_type,
        secondary_type=secondary_type,
        doubts=tuple(doubts),
        blocked=frozenset(blocked),
        review_reasons=tuple(reasons),
        no_landmark=no_landmark,
    )
