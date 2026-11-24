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
