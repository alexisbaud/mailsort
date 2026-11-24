import pytest


@pytest.fixture(autouse=True)
def _ignore_local_dotenv(monkeypatch):
    """Les tests ne doivent jamais lire le vrai .env (clés, adresses) de la machine."""
    monkeypatch.setattr("mailsort.cli.load_dotenv", lambda *args, **kwargs: False)
    for name in ("TYPESAFE_API_KEY", "JEV_MODEL", "TYPESAFE_BASE_URL", "MAILSORT_USER_EMAILS"):
        monkeypatch.delenv(name, raising=False)
