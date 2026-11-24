import pytest


@pytest.fixture(autouse=True)
def _isolate_from_local_files(monkeypatch, tmp_path):
    """Les tests ne lisent jamais le vrai .env, credentials.json, token.json ni la vraie base :
    sans cette isolation, un test pourrait ouvrir le navigateur pour une autorisation Google
    ou appeler les vrais services."""
    monkeypatch.setattr("mailsort.cli.load_dotenv", lambda *args, **kwargs: False)
    for name in ("TYPESAFE_API_KEY", "JEV_MODEL", "TYPESAFE_BASE_URL", "MAILSORT_USER_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_FILE", str(secrets / "credentials.json"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(secrets / "token.json"))
    monkeypatch.setenv("MAILSORT_DB", str(tmp_path / "mailsort.db"))
