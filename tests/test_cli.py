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
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 1
    assert "Pas encore implémenté" in result.output


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
