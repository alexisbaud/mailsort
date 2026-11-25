"""Tests du moteur de décision, sur le vrai labels.yaml.

Types : payments, deliveries, tickets_bookings, official_documents, accounts_security,
newsletters, marketing, support, subscriptions_services. Paires interdites : payments/deliveries,
newsletters/marketing, subscriptions_services/payments.
Seuils oui/non : domaines 0.70, deletable 0.55, to_reply 0.80 ; zone de doute à partir de 0.50.
Protègent de deletable : payments, tickets_bookings, official_documents, family_friends, to_reply.
"""

from pathlib import Path

import pytest
import yaml

from mailsort.config import load_config
from mailsort.engine import (
    NONE_OPTION,
    EngineError,
    JevAnswers,
    decide,
    secondary_request,
)

REPO_LABELS = Path(__file__).resolve().parent.parent / "labels.yaml"
CONFIG = load_config(REPO_LABELS)
TYPES = [t.id for t in CONFIG.types]
YES_NO = [lb.id for lb in CONFIG.yes_no_labels]
REVIEW = "to_review"


def primary(**probs: float) -> dict[str, float]:
    """Choix de type : les types non cités valent 0, « None » prend le reste."""
    result = {t: 0.0 for t in TYPES}
    result.update(probs)
    if NONE_OPTION not in probs:
        result[NONE_OPTION] = round(max(0.0, 1 - sum(probs.values())), 6)
    return result


def secondary(winner: str, **probs: float) -> dict[str, float]:
    result = {t: 0.0 for t in TYPES if t != winner}
    result.update(probs)
    if NONE_OPTION not in probs:
        result[NONE_OPTION] = round(max(0.0, 1 - sum(probs.values())), 6)
    return result


def yes_no(**probs: float) -> dict[str, float]:
    result = {lb: 0.0 for lb in YES_NO}
    result.update(probs)
    return result


def run(prim=None, yn=None, sec=None, config=CONFIG, **kwargs):
    answers = JevAnswers(
        primary=prim if prim is not None else primary(payments=0.9),
        yes_no=yn if yn is not None else yes_no(),
        secondary=sec,
    )
    return decide(config, answers, **kwargs)


def config_with(tmp_path, mutate):
    raw = yaml.safe_load(REPO_LABELS.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def raw_label(raw, label_id):
    return next(lb for lb in raw["labels"] if lb["id"] == label_id)


# ------------------------------------------------------- 1. type principal


def test_primary_type_is_set():
    d = run(primary(payments=0.9))
    assert d.primary_type == "payments"
    assert d.labels == {"payments"}
    assert d.review_reasons == ()


def test_primary_threshold_is_inclusive():
    assert run(primary(payments=0.6)).primary_type == "payments"


def test_none_of_the_above_gives_no_type():
    d = run(primary(), yes_no(housing=0.9))
    assert d.primary_type is None
    assert d.labels == {"housing"}


def test_primary_below_threshold_goes_to_review():
    d = run(primary(payments=0.55, deliveries=0.15))
    assert d.primary_type is None
    assert "payments" not in d.labels
    assert REVIEW in d.labels
    assert d.doubts[0].family == "type"
    assert any("type incertain" in r for r in d.review_reasons)


def test_type_pair_sum_posts_the_winner():
    d = run(primary(payments=0.55, official_documents=0.44))
    assert d.primary_type == "payments"
    assert d.doubts == ()


def test_type_pair_sum_is_inclusive():
    assert run(primary(payments=0.5, official_documents=0.3)).primary_type == "payments"
    assert run(primary(payments=0.5, official_documents=0.29)).primary_type is None


def test_none_as_runner_up_does_not_count_for_pair_sum():
    d = run(primary(payments=0.5, **{NONE_OPTION: 0.45}))
    assert d.primary_type is None
    assert d.doubts[0].family == "type"


def test_none_winning_with_pair_sum_gives_no_type():
    d = run(primary(payments=0.4, **{NONE_OPTION: 0.5}))
    assert d.primary_type is None
    assert d.labels == {REVIEW}


def test_uncertain_type_with_domain_is_not_in_review():
    d = run(primary(payments=0.40, **{NONE_OPTION: 0.45}), yes_no(housing=0.9))
    assert d.primary_type is None
    assert d.labels == {"housing"}
    assert any("type incertain" in r for r in d.review_reasons)


def test_tie_resolved_by_file_order():
    d = run(primary(payments=0.5, deliveries=0.5))
    assert d.primary_type == "payments"
    d = run(primary(payments=0.35, deliveries=0.35))
    assert d.doubts[0].label_id == "payments"


# --------------------------------------------- 2-3. second passage des types


def test_no_secondary_request_without_runner_up():
    assert secondary_request(CONFIG, primary(payments=0.8, official_documents=0.2)) is None


def test_secondary_request():
    request = secondary_request(CONFIG, primary(payments=0.62, official_documents=0.33))
    assert request is not None
    assert request.winner == "payments"
    assert request.candidate == "official_documents"
    assert "payments" not in request.options
    assert request.options[-1] == NONE_OPTION
    assert len(request.options) == len(TYPES)


def test_secondary_trigger_is_inclusive():
    assert secondary_request(CONFIG, primary(payments=0.65, official_documents=0.30))


@pytest.mark.parametrize(
    "probs",
    [
        {"payments": 0.62, "deliveries": 0.35},
        {"deliveries": 0.62, "payments": 0.35},
        {"newsletters": 0.6, "marketing": 0.4},
        {"marketing": 0.6, "newsletters": 0.4},
    ],
    ids=["payments-deliveries", "deliveries-payments", "newsletters-marketing", "reverse"],
)
def test_no_secondary_request_for_forbidden_pair(probs):
    assert secondary_request(CONFIG, primary(**probs)) is None


def test_no_secondary_request_when_primary_uncertain():
    assert secondary_request(CONFIG, primary(payments=0.45, official_documents=0.3)) is None


def test_secondary_request_after_pair_sum():
    request = secondary_request(CONFIG, primary(payments=0.55, official_documents=0.44))
    assert request is not None
    assert request.candidate == "official_documents"


def test_no_secondary_request_when_none_wins():
    probs = primary(official_documents=0.35, **{NONE_OPTION: 0.65})
    assert secondary_request(CONFIG, probs) is None


def test_secondary_type_is_added():
    d = run(
        primary(payments=0.62, official_documents=0.33),
        sec=secondary("payments", official_documents=0.75),
    )
    assert d.primary_type == "payments"
    assert d.secondary_type == "official_documents"
    assert d.labels == {"payments", "official_documents"}


def test_secondary_below_threshold_is_not_added_and_not_doubtful():
    d = run(
        primary(payments=0.62, official_documents=0.33),
        sec=secondary("payments", official_documents=0.65),
    )
    assert d.secondary_type is None
    assert d.labels == {"payments"}
    assert REVIEW not in d.labels


def test_secondary_other_type_wins_nothing_added():
    d = run(
        primary(payments=0.62, official_documents=0.33),
        sec=secondary("payments", tickets_bookings=0.8),
    )
    assert d.secondary_type is None
    assert d.labels == {"payments"}


def test_secondary_none_wins_nothing_added():
    d = run(
        primary(payments=0.62, official_documents=0.33),
        sec=secondary("payments", **{NONE_OPTION: 0.9}),
    )
    assert d.labels == {"payments"}


def test_secondary_ignored_when_not_requested():
    d = run(primary(payments=0.9), sec=secondary("payments", official_documents=0.9))
    assert d.labels == {"payments"}


def test_secondary_ignored_for_forbidden_pair():
    d = run(
        primary(payments=0.62, deliveries=0.35),
        sec=secondary("payments", deliveries=0.9),
    )
    assert d.labels == {"payments"}


def test_secondary_with_winner_in_options_is_an_error():
    bad = secondary("payments", official_documents=0.75)
    bad["payments"] = 0.0
    with pytest.raises(EngineError, match="secondary"):
        run(primary(payments=0.62, official_documents=0.33), sec=bad)


# ------------------------------------------------------- 4. labels oui/non


def test_yes_no_threshold_is_inclusive():
    assert run(yn=yes_no(housing=0.8)).labels == {"payments", "housing"}


def test_yes_no_below_doubt_is_no():
    d = run(yn=yes_no(housing=0.49))
    assert d.labels == {"payments"}
    assert d.doubts == ()


def test_several_domains():
    d = run(yn=yes_no(housing=0.9, banking=0.85, work=0.95))
    assert d.labels == {"payments", "housing", "banking", "work"}


def test_label_specific_threshold(tmp_path):
    config = config_with(tmp_path, lambda raw: raw_label(raw, "housing").update(threshold=0.9))
    d = run(yn=yes_no(housing=0.85), config=config)
    assert "housing" not in d.labels
    assert d.doubts[0].label_id == "housing"
    assert run(yn=yes_no(housing=0.9), config=config).labels == {"payments", "housing"}


# ---------------------------------------------------------- 5. protections


@pytest.mark.parametrize(
    ("prim", "yn"),
    [
        (primary(payments=0.9), yes_no(deletable=0.95)),
        (primary(tickets_bookings=0.9), yes_no(deletable=0.95)),
        (primary(official_documents=0.9), yes_no(deletable=0.95)),
        (primary(), yes_no(family_friends=0.9, deletable=0.95)),
        (primary(marketing=0.9), yes_no(to_reply=0.9, deletable=0.95)),
    ],
    ids=["payments", "tickets", "official", "family", "to_reply"],
)
def test_protections_block_deletable(prim, yn):
    d = run(prim, yn)
    assert "deletable" not in d.labels
    assert d.blocked == {"deletable"}


def test_deletable_without_protection():
    d = run(primary(marketing=0.9), yes_no(deletable=0.95))
    assert d.labels == {"marketing", "deletable"}
    assert d.blocked == frozenset()


def test_secondary_type_also_protects():
    d = run(
        primary(marketing=0.62, official_documents=0.33),
        yes_no(deletable=0.95),
        sec=secondary("marketing", official_documents=0.8),
    )
    assert d.labels == {"marketing", "official_documents"}


def test_doubt_on_protected_label_is_not_a_doubt():
    d = run(primary(payments=0.9), yes_no(deletable=0.65))
    assert d.doubts == ()
    assert REVIEW not in d.labels


# ---------------------------------------------------------- 6. déjà répondu


def test_to_reply_removed_when_user_replied_last():
    d = run(yn=yes_no(to_reply=0.95), user_replied_last=True)
    assert "to_reply" not in d.labels


def test_to_reply_kept_when_user_did_not_reply():
    assert "to_reply" in run(yn=yes_no(to_reply=0.95)).labels


def test_to_reply_doubt_ignored_when_user_replied_last():
    d = run(yn=yes_no(to_reply=0.65), user_replied_last=True)
    assert REVIEW not in d.labels


def test_to_reply_removed_no_longer_protects():
    d = run(primary(marketing=0.9), yes_no(to_reply=0.9, deletable=0.9), user_replied_last=True)
    assert d.labels == {"marketing", "deletable"}


# ------------------------------------------------------ 7. zone de doute


def test_yes_no_doubt_is_noted_but_not_in_review():
    d = run(yn=yes_no(housing=0.65))
    assert d.labels == {"payments"}
    assert d.doubts[0].label_id == "housing"
    assert d.review_reasons == ("doute sur housing (0.65)",)
    assert d.no_landmark is False


def test_doubt_lower_bound_is_inclusive():
    assert run(yn=yes_no(housing=0.5)).doubts[0].label_id == "housing"
    assert run(yn=yes_no(housing=0.49)).doubts == ()


def test_doubt_without_landmark_goes_to_review():
    d = run(primary(), yes_no(housing=0.65))
    assert d.labels == {REVIEW}
    assert d.review_reasons == (
        "doute sur housing (0.65)",
        "aucun repère : ni type, ni domaine, ni nettoyage",
    )


@pytest.mark.parametrize(
    ("label_id", "below", "at"),
    [("housing", 0.69, 0.70), ("deletable", 0.54, 0.55), ("to_reply", 0.79, 0.80)],
)
def test_family_and_label_thresholds(label_id, below, at):
    assert label_id not in run(primary(marketing=0.9), yes_no(**{label_id: below})).labels
    assert label_id in run(primary(marketing=0.9), yes_no(**{label_id: at})).labels


def test_subscriptions_never_with_payments():
    assert secondary_request(CONFIG, primary(subscriptions_services=0.62, payments=0.33)) is None


# ---------------------------------------------------- 8. mail sans repère


def test_no_landmark_goes_to_review():
    d = run(primary())
    assert d.labels == {REVIEW}
    assert any("aucun repère" in r for r in d.review_reasons)


def test_to_reply_alone_is_not_a_landmark():
    d = run(primary(), yes_no(to_reply=0.9))
    assert d.labels == {"to_reply", REVIEW}


@pytest.mark.parametrize(
    ("prim", "yn"),
    [
        (primary(newsletters=0.9), yes_no()),
        (primary(), yes_no(health=0.9)),
        (primary(), yes_no(deletable=0.9)),
    ],
    ids=["type", "domain", "deletable"],
)
def test_landmarks(prim, yn):
    assert REVIEW not in run(prim, yn).labels


# -------------------------------------------------------- verrous manuels


def test_locked_absent_removes_label():
    d = run(yn=yes_no(housing=0.9), locks={"housing": False})
    assert d.labels == {"payments"}


def test_locked_present_adds_label():
    d = run(locks={"work": True})
    assert d.labels == {"payments", "work"}


def test_locked_absent_type():
    d = run(primary(payments=0.9), yes_no(banking=0.9), locks={"payments": False})
    assert d.labels == {"banking"}


def test_lock_on_doubtful_label_is_not_a_doubt():
    d = run(yn=yes_no(housing=0.65), locks={"housing": False})
    assert d.doubts == ()
    assert REVIEW not in d.labels


def test_lock_in_same_family_resolves_doubt():
    d = run(yn=yes_no(housing=0.65), locks={"work": True})
    assert REVIEW not in d.labels
    assert d.doubts == ()
    assert d.labels == {"payments", "work"}


def test_lock_in_other_family_does_not_resolve_doubt():
    d = run(yn=yes_no(housing=0.65), locks={"to_reply": True})
    assert [doubt.label_id for doubt in d.doubts] == ["housing"]


def test_type_lock_resolves_type_doubt():
    d = run(primary(payments=0.45, deliveries=0.25), locks={"deliveries": True})
    assert d.labels == {"deliveries"}
    assert REVIEW not in d.labels


def test_lock_on_landmark_family_resolves_no_landmark():
    d = run(primary(), locks={"deletable": False})
    assert d.labels == frozenset()


def test_locked_present_survives_protection():
    d = run(primary(payments=0.9), locks={"deletable": True})
    assert d.labels == {"payments", "deletable"}
    assert d.blocked == frozenset()


def test_manual_label_protects():
    d = run(primary(marketing=0.9), yes_no(deletable=0.9), locks={"family_friends": True})
    assert d.labels == {"marketing", "family_friends"}


def test_locked_to_reply_survives_user_reply():
    d = run(locks={"to_reply": True}, user_replied_last=True)
    assert "to_reply" in d.labels


def test_review_locked_absent_is_never_set():
    d = run(primary(), locks={REVIEW: False})
    assert REVIEW not in d.labels
    assert d.review_reasons  # la raison reste visible pour explain


def test_review_locked_present_is_always_set():
    d = run(primary(payments=0.9), locks={REVIEW: True})
    assert REVIEW in d.labels


def test_locks_on_unknown_labels_are_ignored():
    assert run(locks={"old_label": True}).labels == {"payments"}


# ------------------------------------------------- labels désactivés


def test_disabled_labels_are_not_required_nor_set(tmp_path):
    def mutate(raw):
        raw_label(raw, "housing")["enabled"] = False
        raw_label(raw, "to_review")["enabled"] = False

    config = config_with(tmp_path, mutate)
    yn = {k: v for k, v in yes_no().items() if k != "housing"}
    d = run(primary(), yn, config=config, locks={"housing": True})
    assert d.labels == frozenset()


def test_extra_yes_no_keys_are_ignored():
    yn = yes_no()
    yn["old_label"] = 0.9
    assert run(yn=yn).labels == {"payments"}


# ----------------------------------------------------- réponses invalides


def test_missing_type_option_is_an_error():
    probs = primary(payments=0.9)
    del probs["marketing"]
    with pytest.raises(EngineError, match="manquantes"):
        run(probs)


def test_extra_type_option_is_an_error():
    probs = primary(payments=0.9)
    probs["old_type"] = 0.0
    with pytest.raises(EngineError, match="en trop"):
        run(probs)


def test_missing_yes_no_is_an_error():
    yn = yes_no()
    del yn["health"]
    with pytest.raises(EngineError, match="health"):
        run(yn=yn)


@pytest.mark.parametrize("value", [-0.1, 1.2])
def test_probability_out_of_range_is_an_error(value):
    with pytest.raises(EngineError, match="hors de"):
        run(yn=yes_no(housing=value))


def test_no_landmark_flag():
    assert run(primary()).no_landmark is True
    assert run(primary(newsletters=0.9)).no_landmark is False
