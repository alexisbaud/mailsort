import copy
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mailsort.cli import app
from mailsort.config import TYPE_QUESTION, ConfigError, load_config

REPO_LABELS = Path(__file__).resolve().parent.parent / "labels.yaml"

MINIMAL = {
    "settings": {
        "types": {
            "primary_min": 0.6,
            "secondary_trigger": 0.3,
            "secondary_min": 0.7,
            "none_description": "None of the other types applies.",
        },
        "yes_no_min": 0.8,
        "doubt_min": 0.5,
        "parents": {
            "type": "Types",
            "domain": "Domains",
            "action": "Actions",
            "cleanup": "Actions",
            "system": "Actions",
        },
        "visibility": {
            f: {"label_list": "show", "message_list": "hide"}
            for f in ("type", "domain", "action", "cleanup", "system")
        },
    },
    "labels": [
        {
            "id": "payments",
            "name": "💳 Payments",
            "family": "type",
            "color": "#17a765",
            "never_with": ["deliveries"],
            "protects_from": ["deletable"],
            "description": "Proof of payment.",
        },
        {
            "id": "deliveries",
            "name": "📦 Deliveries",
            "family": "type",
            "color": "#ff7537",
            "description": "Shipment tracking.",
        },
        {
            "id": "housing",
            "name": "🏠 Housing",
            "family": "domain",
            "color": "#ffc8af",
            "description": "The user's home.",
        },
        {
            "id": "deletable",
            "name": "🗑️ Deletable",
            "family": "cleanup",
            "color": "#d93025",
            "description": "No value once read.",
        },
        {"id": "to_review", "name": "🤔 To Review", "family": "system", "color": "#ffae47"},
    ],
}


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def data() -> dict:
    return copy.deepcopy(MINIMAL)


def label(d: dict, label_id: str) -> dict:
    return next(lb for lb in d["labels"] if lb["id"] == label_id)


def errors_for(tmp_path: Path, d: dict) -> list[str]:
    with pytest.raises(ConfigError) as exc_info:
        load_config(write(tmp_path, d))
    return exc_info.value.errors


# ---------------------------------------------------------------- vrai fichier


def test_repo_labels_file_is_valid():
    cfg = load_config(REPO_LABELS)
    assert len(cfg.labels) == 18
    assert [t.id for t in cfg.types] == [
        "payments",
        "deliveries",
        "tickets_bookings",
        "official_documents",
        "accounts_security",
        "newsletters",
        "marketing",
    ]
    assert len(cfg.yes_no_labels) == 10
    assert cfg.full_name(cfg.by_id["payments"]) == "Types/💳 Payments"
    assert cfg.full_name(cfg.by_id["to_review"]) == "Actions/🤔 To Review"
    assert cfg.by_id["payments"].never_with == ("deliveries",)
    protected = {lb.id for lb in cfg.labels if "deletable" in lb.protects_from}
    assert protected == {
        "payments",
        "official_documents",
        "tickets_bookings",
        "family_friends",
        "to_reply",
    }


def test_minimal_is_valid(tmp_path):
    cfg = load_config(write(tmp_path, data()))
    assert cfg.threshold_for(cfg.by_id["housing"]) == 0.8


def test_description_whitespace_is_normalized(tmp_path):
    d = data()
    label(d, "housing")["description"] = "The user's\n   home.\n"
    cfg = load_config(write(tmp_path, d))
    assert cfg.by_id["housing"].description == "The user's home."


def test_label_threshold_overrides_default(tmp_path):
    d = data()
    label(d, "housing")["threshold"] = 0.9
    cfg = load_config(write(tmp_path, d))
    assert cfg.threshold_for(cfg.by_id["housing"]) == 0.9


def test_disabled_labels_are_not_evaluated(tmp_path):
    d = data()
    label(d, "housing")["enabled"] = False
    label(d, "deliveries")["enabled"] = False
    cfg = load_config(write(tmp_path, d))
    assert [lb.id for lb in cfg.yes_no_labels] == ["deletable"]
    assert [t.id for t in cfg.types] == ["payments"]


# ------------------------------------------------------------------ erreurs


def mutate_duplicate_id(d):
    d["labels"].append({**label(d, "housing"), "name": "🏡 Home"})


def mutate_unknown_family(d):
    label(d, "housing")["family"] = "domaine"


def mutate_never_with_on_domain(d):
    label(d, "housing")["never_with"] = ["payments"]


def mutate_never_with_unknown(d):
    label(d, "payments")["never_with"] = ["deliverys"]


def mutate_never_with_not_a_type(d):
    label(d, "payments")["never_with"] = ["housing"]


def mutate_never_with_self(d):
    label(d, "payments")["never_with"] = ["payments"]


def mutate_protects_unknown(d):
    label(d, "payments")["protects_from"] = ["trash"]


def mutate_threshold_on_type(d):
    label(d, "payments")["threshold"] = 0.9


def mutate_threshold_below_doubt(d):
    label(d, "housing")["threshold"] = 0.4


def mutate_doubt_above_yes_no(d):
    d["settings"]["doubt_min"] = 0.85


def mutate_missing_review(d):
    d["labels"] = [lb for lb in d["labels"] if lb["id"] != "to_review"]


def mutate_review_wrong_family(d):
    label(d, "to_review")["family"] = "action"
    label(d, "to_review")["description"] = "x"


def mutate_slash_in_name(d):
    label(d, "housing")["name"] = "Home/Housing"


def mutate_missing_description(d):
    del label(d, "housing")["description"]


def mutate_bad_color(d):
    label(d, "housing")["color"] = "orange"


def mutate_typo_field(d):
    label(d, "housing")["treshold"] = 0.9


def mutate_duplicate_full_name(d):
    d["labels"].append({**label(d, "housing"), "id": "home"})


def mutate_missing_parent(d):
    del d["settings"]["parents"]["domain"]


def mutate_probability_out_of_range(d):
    d["settings"]["types"]["primary_min"] = 1.5


def mutate_bad_id(d):
    label(d, "housing")["id"] = "Housing-2"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (mutate_duplicate_id, "id en double"),
        (mutate_unknown_family, "family"),
        (mutate_never_with_on_domain, "never_with n'est permis que sur un type"),
        (mutate_never_with_unknown, "label inexistant « deliverys »"),
        (mutate_never_with_not_a_type, "n'est pas un type"),
        (mutate_never_with_self, "le label lui-même"),
        (mutate_protects_unknown, "label inexistant « trash »"),
        (mutate_threshold_on_type, "threshold n'a de sens"),
        (mutate_threshold_below_doubt, "doit être supérieur à doubt_min"),
        (mutate_doubt_above_yes_no, "doubt_min (0.85) doit être inférieur"),
        (mutate_missing_review, "« to_review » manquant"),
        (mutate_review_wrong_family, "la famille doit être « system »"),
        (mutate_slash_in_name, "ne doit pas contenir"),
        (mutate_missing_description, "description manquante"),
        (mutate_bad_color, "color"),
        (mutate_typo_field, "treshold"),
        (mutate_duplicate_full_name, "même nom Gmail"),
        (mutate_missing_parent, "aucun parent pour la famille « domain »"),
        (mutate_probability_out_of_range, "primary_min"),
        (mutate_bad_id, "id"),
    ],
    ids=lambda v: v.__name__.removeprefix("mutate_") if callable(v) else "",
)
def test_invalid_config_is_rejected(tmp_path, mutate, expected):
    d = data()
    mutate(d)
    errors = errors_for(tmp_path, d)
    assert any(expected in e for e in errors), errors


def test_schema_errors_name_the_label(tmp_path):
    d = data()
    label(d, "housing")["color"] = "orange"
    errors = errors_for(tmp_path, d)
    assert errors[0].startswith("label « housing »")


def test_all_consistency_errors_are_reported_at_once(tmp_path):
    d = data()
    mutate_never_with_unknown(d)
    mutate_protects_unknown(d)
    mutate_missing_review(d)
    assert len(errors_for(tmp_path, d)) == 3


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_path / "absent.yaml")


def test_invalid_yaml(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text("settings: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML illisible"):
        load_config(path)


def test_empty_file(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="settings"):
        load_config(path)


# --------------------------------------------------------------- empreintes


def fingerprints(tmp_path, d):
    cfg = load_config(write(tmp_path, d))
    return cfg.question_fingerprints(), cfg.rules_fingerprint()


def test_question_keys(tmp_path):
    questions, _ = fingerprints(tmp_path, data())
    assert set(questions) == {TYPE_QUESTION, "housing", "deletable"}


def test_domain_description_change_only_affects_that_label(tmp_path):
    before, rules_before = fingerprints(tmp_path, data())
    d = data()
    label(d, "housing")["description"] = "The user's home and flat."
    after, rules_after = fingerprints(tmp_path, d)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"housing"}
    assert rules_before == rules_after


def test_type_description_change_reasks_whole_type_question(tmp_path):
    before, _ = fingerprints(tmp_path, data())
    d = data()
    label(d, "deliveries")["description"] = "Shipment and return tracking."
    after, _ = fingerprints(tmp_path, d)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {TYPE_QUESTION}


def test_none_description_change_reasks_type_question(tmp_path):
    before, _ = fingerprints(tmp_path, data())
    d = data()
    d["settings"]["types"]["none_description"] = "Nothing else applies."
    after, _ = fingerprints(tmp_path, d)
    assert before[TYPE_QUESTION] != after[TYPE_QUESTION]


def test_rename_changes_question_fingerprint(tmp_path):
    before, _ = fingerprints(tmp_path, data())
    d = data()
    label(d, "housing")["name"] = "🏡 Home"
    after, _ = fingerprints(tmp_path, d)
    assert before["housing"] != after["housing"]


def test_reformatting_description_keeps_fingerprint(tmp_path):
    before, _ = fingerprints(tmp_path, data())
    d = data()
    label(d, "housing")["description"] = "  The user's\nhome.  "
    after, _ = fingerprints(tmp_path, d)
    assert before == after


@pytest.mark.parametrize(
    "change",
    [
        lambda d: label(d, "housing").__setitem__("threshold", 0.9),
        lambda d: d["settings"].__setitem__("yes_no_min", 0.85),
        lambda d: d["settings"]["types"].__setitem__("primary_min", 0.65),
        lambda d: label(d, "deliveries").__setitem__("protects_from", ["deletable"]),
        lambda d: label(d, "payments").__setitem__("never_with", []),
    ],
    ids=["label_threshold", "yes_no_min", "primary_min", "protects_from", "never_with"],
)
def test_rule_changes_only_affect_rules_fingerprint(tmp_path, change):
    questions_before, rules_before = fingerprints(tmp_path, data())
    d = data()
    change(d)
    questions_after, rules_after = fingerprints(tmp_path, d)
    assert questions_before == questions_after
    assert rules_before != rules_after


def test_disabling_a_type_changes_type_question_and_rules(tmp_path):
    q_before, r_before = fingerprints(tmp_path, data())
    d = data()
    label(d, "deliveries")["enabled"] = False
    q_after, r_after = fingerprints(tmp_path, d)
    assert q_before[TYPE_QUESTION] != q_after[TYPE_QUESTION]
    assert r_before != r_after


def test_disabling_a_yes_no_label_removes_its_question(tmp_path):
    d = data()
    label(d, "housing")["enabled"] = False
    questions, _ = fingerprints(tmp_path, d)
    assert "housing" not in questions


def test_parent_change_does_not_affect_fingerprints(tmp_path):
    before = fingerprints(tmp_path, data())
    d = data()
    d["settings"]["parents"]["domain"] = "Life"
    assert fingerprints(tmp_path, d) == before


# ---------------------------------------------------------------------- CLI

runner = CliRunner()


def test_cli_labels_check_ok():
    result = runner.invoke(app, ["labels", "check", "--config", str(REPO_LABELS)])
    assert result.exit_code == 0, result.output
    assert "18 labels" in result.output


def test_cli_labels_check_reports_errors(tmp_path):
    d = data()
    mutate_never_with_unknown(d)
    result = runner.invoke(app, ["labels", "check", "--config", str(write(tmp_path, d))])
    assert result.exit_code == 1
    assert "deliverys" in result.output
