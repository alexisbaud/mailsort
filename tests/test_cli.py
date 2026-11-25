from typer.testing import CliRunner

from mailsort import __version__
from mailsort.cli import app

runner = CliRunner()

EXPECTED_COMMANDS = ["auth", "run", "resort", "eval", "explain", "corrections", "labels"]


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in EXPECTED_COMMANDS:
        assert command in result.output


def test_labels_group_has_delete():
    result = runner.invoke(app, ["labels", "--help"])
    assert result.exit_code == 0
    assert "delete" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_run_without_credentials_explains_what_to_do():
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "credentials.json.example" in result.output


def test_resort_requires_all():
    result = runner.invoke(app, ["resort"])
    assert result.exit_code == 1
    assert "--all" in result.output


def test_dry_run_without_credentials_does_not_open_browser():
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 1
    assert "credentials.json.example" in result.output


def test_clean_command_on_fixture():
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "forward.eml"
    result = runner.invoke(app, ["clean", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "transfert" in result.output
    assert "attestation_loyer.pdf" in result.output
    assert "Veuillez trouver ci-joint" in result.output


def test_classify_command(monkeypatch):
    from pathlib import Path

    from fake_jev import FakeJev

    from mailsort import jev
    from mailsort.config import TYPE_QUESTION

    fake = FakeJev(
        choices={TYPE_QUESTION: {"official_documents": 0.85, jev.NONE_CHOICE: 0.15}},
        nouls={"housing": 0.93, "family_friends": 0.9, "deletable": 0.85, "studies": 0.6},
    )
    monkeypatch.setattr(
        jev.JevClient,
        "from_env",
        classmethod(lambda cls: cls("k", transport=fake.transport(), max_requests_per_second=0)),
    )
    fixture = Path(__file__).parent / "fixtures" / "forward.eml"
    result = runner.invoke(app, ["classify", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "Labels posés : 📄 Official Documents, 🏠 Housing, 👨‍👩‍👧 Family & Friends" in (
        result.output
    )
    assert "Bloqués par une protection : 🗑️ Deletable" in result.output
    assert "doute sur studies (0.60)" in result.output
    assert "Second passage : non demandé" in result.output


def test_classify_without_key_explains_what_to_do():
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "forward.eml"
    result = runner.invoke(app, ["classify", str(fixture)])
    assert result.exit_code == 1
    assert ".env.example" in result.output


def _fake_box(monkeypatch, **service_kwargs):
    from fake_gmail import FakeGmailService

    from mailsort import gmail

    service = FakeGmailService(**service_kwargs)
    monkeypatch.setattr(gmail.Gmail, "connect", classmethod(lambda cls, token=None: cls(service)))
    return service


def test_messages_command(monkeypatch):
    from fake_gmail import gmail_message

    _fake_box(monkeypatch, messages={"m1": gmail_message("m1"), "m2": gmail_message("m2")})
    result = runner.invoke(app, ["messages", "--limit", "1"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("m1")
    assert "Votre facture n° 42" in lines[0]


def test_explain_text_only(monkeypatch):
    from fake_gmail import gmail_message

    _fake_box(
        monkeypatch,
        messages={"m1": gmail_message()},
        threads={"t1": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]}},
    )
    result = runner.invoke(app, ["explain", "m1", "--text-only"])
    assert result.exit_code == 0, result.output
    assert "user_is : direct recipient (To)" in result.output
    assert "attachments : ['facture.pdf']" in result.output
    assert "Bonjour, voici la facture." in result.output
    assert "Type (seuil" not in result.output


def test_explain_with_jev_applies_rule_6(monkeypatch):
    from fake_gmail import gmail_message
    from fake_jev import FakeJev

    from mailsort import jev
    from mailsort.config import TYPE_QUESTION

    _fake_box(
        monkeypatch,
        messages={"m1": gmail_message()},
        threads={
            "t1": {
                "messages": [
                    {"labelIds": ["INBOX"], "internalDate": "1"},
                    {"labelIds": ["SENT"], "internalDate": "2"},
                ]
            }
        },
    )
    fake = FakeJev(
        choices={TYPE_QUESTION: {"payments": 0.95, jev.NONE_CHOICE: 0.05}},
        nouls={"to_reply": 0.9},
    )
    monkeypatch.setattr(
        jev.JevClient,
        "from_env",
        classmethod(lambda cls: cls("k", transport=fake.transport(), max_requests_per_second=0)),
    )
    result = runner.invoke(app, ["explain", "m1"])
    assert result.exit_code == 0, result.output
    assert fake.requests[0]["state"]["user_already_replied_in_thread"] is True
    assert "Dernier message du fil envoyé par toi : oui" in result.output
    assert "Labels posés : 💳 Payments" in result.output
    assert "To Reply" not in result.output.split("Labels posés")[1]


def test_explain_excluded_message_is_flagged(monkeypatch):
    from fake_gmail import gmail_message

    _fake_box(
        monkeypatch,
        messages={"m1": gmail_message(labels=("SENT",))},
        threads={"t1": {"messages": []}},
    )
    result = runner.invoke(app, ["explain", "m1", "--text-only"])
    assert "serait ignoré par le tri (SENT)" in result.output


def test_gmail_auth_error_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "token.json"))
    result = runner.invoke(app, ["messages"])
    assert result.exit_code == 1
    assert "mailsort auth" in result.output


def test_auth_without_client_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_FILE", str(tmp_path / "credentials.json"))
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "credentials.json.example" in result.output


def test_dry_run_command(monkeypatch, tmp_path):
    import os
    from pathlib import Path

    from fake_gmail import gmail_message
    from fake_jev import FakeJev

    from mailsort import jev
    from mailsort.config import TYPE_QUESTION

    labels = Path(__file__).resolve().parent.parent / "labels.yaml"
    Path(os.environ["GOOGLE_TOKEN_FILE"]).write_text("{}")  # le jeton existe : pas d'autorisation
    _fake_box(
        monkeypatch,
        messages={
            "m1": gmail_message("m1", "t1", Subject="Facture"),
            "m2": gmail_message("m2", "t2", Subject="Rien"),
        },
        threads={
            "t1": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]},
            "t2": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]},
        },
    )
    fake = FakeJev(
        by_subject={
            "Facture": {"choices": {TYPE_QUESTION: {"payments": 0.95, jev.NONE_CHOICE: 0.05}}},
            "Rien": {"choices": {TYPE_QUESTION: {jev.NONE_CHOICE: 1.0}}},
        }
    )
    monkeypatch.setattr(
        jev.JevClient,
        "from_env",
        classmethod(lambda cls: cls("k", transport=fake.transport(), max_requests_per_second=0)),
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run", "--dry-run", "--config", str(labels)])
    assert result.exit_code == 0, result.output
    assert "rien n'est modifié dans Gmail" in result.output
    assert "2 mails à classer, dont 2 à envoyer à Jev" in result.output
    counts = {}
    for line in result.output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "%" and parts[-3].isdigit():
            counts[" ".join(parts[:-3])] = int(parts[-3])
        elif parts[:2] == ["Total", "trié"]:
            counts["Total trié"] = int(parts[-1])
    assert counts["💳 Payments"] == 1
    assert counts["🤔 To Review"] == 1
    assert counts["Seulement 🤔 To Review"] == 1
    assert counts["Aucun label"] == 0
    assert counts["Total trié"] == 2
    assert len(list(tmp_path.glob("dry-run-*.csv"))) == 1

    review = result.output.split("Mails 🤔 To Review (1)")[1]
    assert "m2  2026-11-24  Boutique <shop@example.com>" in review
    assert "Objet  : Rien" in review
    assert "Labels : aucun autre label" in review
    assert "• aucun repère : meilleur type aucun type 1.00" in review
    assert "Facture" not in review.split("Types")[0]
    summary = result.output.split("Doutes et absence de repère")[1]
    assert "Aucun repère (ni type, ni domaine, ni nettoyage)" in summary

    quiet = runner.invoke(app, ["run", "--dry-run", "--no-review", "--config", str(labels)])
    assert quiet.exit_code == 0, quiet.output
    assert "Mails 🤔 To Review" not in quiet.output
    assert "Doutes et absence de repère" in quiet.output


# ================================================================ vrai passage


def _real_setup(monkeypatch, tmp_path, **service_kwargs):
    import os
    from pathlib import Path

    from fake_gmail import gmail_message
    from fake_jev import FakeJev

    from mailsort import jev
    from mailsort.config import TYPE_QUESTION

    Path(os.environ["GOOGLE_TOKEN_FILE"]).write_text("{}")
    service = _fake_box(
        monkeypatch,
        messages={
            "m1": gmail_message("m1", "t1", Subject="Facture"),
            "m2": gmail_message("m2", "t2", Subject="Rien"),
        },
        threads={
            "t1": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]},
            "t2": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]},
        },
        **service_kwargs,
    )
    fake = FakeJev(
        by_subject={
            "Facture": {
                "choices": {TYPE_QUESTION: {"payments": 0.95, jev.NONE_CHOICE: 0.05}},
                "nouls": {"banking": 0.9},
            },
            "Rien": {"choices": {TYPE_QUESTION: {jev.NONE_CHOICE: 1.0}}},
        }
    )
    monkeypatch.setattr(
        jev.JevClient,
        "from_env",
        classmethod(
            lambda cls: cls(
                "k", transport=fake.transport(), max_requests_per_second=0, sleep=lambda s: None
            )
        ),
    )
    monkeypatch.chdir(tmp_path)
    labels = Path(__file__).resolve().parent.parent / "labels.yaml"
    return service, fake, ["--config", str(labels)]


def _log_text():
    import logging
    import os
    from pathlib import Path

    for handler in logging.getLogger("mailsort").handlers:
        handler.flush()
    return (Path(os.environ["MAILSORT_LOG_DIR"]) / "mailsort.log").read_text(encoding="utf-8")


def test_real_run_command(monkeypatch, tmp_path):
    service, fake, config = _real_setup(monkeypatch, tmp_path)
    result = runner.invoke(app, ["run", "--csv", *config])
    assert result.exit_code == 0, result.output
    assert "Label créé : Types/💳 Payments" in result.output
    assert "2 mails dans le périmètre, dont 2 à envoyer à Jev" in result.output
    assert "2 mails triés · 2 nouveaux · 2 modifiés" in result.output
    assert service.names_on("m1") == {"Types/💳 Payments", "Domains/🏦 Banking & Investments"}
    assert service.names_on("m2") == {"Actions/🤔 To Review"}
    (csv_file,) = tmp_path.glob("run-*.csv")
    assert "Facture" in csv_file.read_text(encoding="utf-8")

    log = _log_text()
    assert "Bilan : 2 mails dans le périmètre, 2 triés (2 nouveaux)" in log
    assert "Facture" not in log  # le journal ne contient jamais le contenu des mails

    again = runner.invoke(app, ["run", *config])
    assert again.exit_code == 0, again.output
    assert "0 nouveaux · 0 modifiés" in again.output
    assert "dont 0 à envoyer à Jev" in again.output


def test_real_run_refused_while_another_is_running(monkeypatch, tmp_path):
    import os
    from pathlib import Path

    from mailsort.runtime import run_lock

    _, _, config = _real_setup(monkeypatch, tmp_path)
    with run_lock(Path(os.environ["MAILSORT_DB"])):
        result = runner.invoke(app, ["run", *config])
    assert result.exit_code == 1
    assert "déjà en cours" in result.output


def test_real_run_auth_error_is_logged(monkeypatch, tmp_path):
    from fake_gmail import http_error

    _, _, config = _real_setup(
        monkeypatch, tmp_path, flaky={"labels.list": [http_error(401, "Invalid Credentials")]}
    )
    result = runner.invoke(app, ["run", *config])
    assert result.exit_code == 1
    assert "mailsort auth" in result.output
    assert "ERROR Accès Gmail refusé" in _log_text()


def test_real_run_quota_exhausted_exits_2(monkeypatch, tmp_path):
    from fake_gmail import http_error

    _, _, config = _real_setup(
        monkeypatch,
        tmp_path,
        flaky={"messages.get": [http_error(403, "per day", reason="dailyLimitExceeded")]},
    )
    result = runner.invoke(app, ["run", *config])
    assert result.exit_code == 2
    assert "Quota journalier" in result.output


def test_resort_all_reasks_everything_but_keeps_corrections(monkeypatch, tmp_path):
    service, fake, config = _real_setup(monkeypatch, tmp_path)
    assert runner.invoke(app, ["run", *config]).exit_code == 0
    service.set_labels("m1", "Types/💳 Payments")  # retire 🏦 Banking à la main
    fake.requests.clear()
    result = runner.invoke(app, ["resort", "--all", *config])
    assert result.exit_code == 0, result.output
    assert len(fake.requests) == 2
    assert service.names_on("m1") == {"Types/💳 Payments"}
    assert "1 corrections manuelles" in result.output


def test_corrections_command(monkeypatch, tmp_path):
    service, _, config = _real_setup(monkeypatch, tmp_path)
    assert runner.invoke(app, ["corrections", *config]).output.startswith("Aucune")
    runner.invoke(app, ["run", *config])
    service.set_labels("m1", "Types/💳 Payments")
    runner.invoke(app, ["run", *config])
    result = runner.invoke(app, ["corrections", *config])
    assert result.exit_code == 0, result.output
    assert "🏦 Banking & Investments : 1 correction(s), 0 ajout(s), 1 retrait(s)" in result.output
    assert "retiré  Jev 0.90" in result.output
    assert "Facture" in result.output


def test_labels_delete_command(monkeypatch, tmp_path):
    from pathlib import Path

    import yaml

    service, _, config = _real_setup(monkeypatch, tmp_path)
    runner.invoke(app, ["run", *config])
    still = runner.invoke(app, ["labels", "delete", "banking", *config])
    assert still.exit_code == 1
    assert "encore dans labels.yaml" in still.output

    raw = yaml.safe_load(Path(config[1]).read_text(encoding="utf-8"))
    raw["labels"] = [lb for lb in raw["labels"] if lb["id"] != "banking"]
    trimmed = tmp_path / "labels.yaml"
    trimmed.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    dry = runner.invoke(app, ["labels", "delete", "banking", "--config", str(trimmed)])
    assert dry.exit_code == 1
    assert "--confirm" in dry.output
    assert "Domains/🏦 Banking & Investments" in service.user_labels.values()

    done = runner.invoke(
        app, ["labels", "delete", "banking", "--confirm", "--config", str(trimmed)]
    )
    assert done.exit_code == 0, done.output
    assert "Domains/🏦 Banking & Investments" not in service.user_labels.values()
    assert service.names_on("m1") == {"Types/💳 Payments"}
    assert "m1" in service.messages_by_id  # le mail reste


def test_eval_command(monkeypatch, tmp_path):
    service, fake, config = _real_setup(monkeypatch, tmp_path, labels={"L_pay": "💳 Paiements"})
    for message_id in ("m1", "m2"):  # référence posée sur « Facture » et sur « Rien »
        service.messages_by_id[message_id]["labelIds"].append("L_pay")
    result = runner.invoke(app, ["eval", *config])
    assert result.exit_code == 0, result.output
    assert "« 💳 Paiements » face à 💳 Payments" in result.output
    assert "Label introuvable dans Gmail" in result.output  # 📦 Réceptions
    # « Facture » : accord ; « Rien » : oubli de Jev.
    assert "2 mails de référence, 2 mails comparés · accord 50.0 % · rappel 50.0 % · " in (
        result.output
    )
    assert "précision 100.0 %" in result.output
    assert "Référence sans 💳 Payments (oubli) : 1" in result.output
    assert service.count("messages.modify") == 0


def test_eval_rejects_bad_pair(monkeypatch, tmp_path):
    _, _, config = _real_setup(monkeypatch, tmp_path)
    result = runner.invoke(app, ["eval", "--compare", "Paiements=inconnu", *config])
    assert result.exit_code == 1
    assert "label inconnu" in result.output
