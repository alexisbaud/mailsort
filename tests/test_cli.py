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


def test_stub_commands_fail_cleanly():
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "Pas encore implémenté" in result.output


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
