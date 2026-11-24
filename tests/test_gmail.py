import json
from datetime import UTC, datetime, timedelta

import pytest
from fake_gmail import FakeGmailService, b64, gmail_message, headers
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from mailsort import gmail
from mailsort.gmail import (
    AUTH_HELP,
    SCOPES,
    Gmail,
    GmailAuthError,
    GmailError,
    is_excluded,
    load_credentials,
    parse_payload,
)
from mailsort.prepare import summarize


def http_error(status: int) -> HttpError:
    return HttpError(Response({"status": status}), b'{"error": "x"}')


# ================================================================ format `full`


def test_parse_payload_alternative_and_attachment():
    parsed = parse_payload(gmail_message()["payload"])
    assert parsed.plain == "Bonjour, voici la facture."
    assert parsed.html == "<p>Bonjour, voici la <b>facture</b>.</p>"
    assert parsed.attachments == ["facture.pdf"]
    assert parsed.pending == []


def test_parse_payload_single_part_with_charset():
    payload = {
        "mimeType": "text/plain",
        "headers": headers(Content_Type="text/plain; charset=ISO-8859-1"),
        "body": {"data": b64("Réservation confirmée", "iso-8859-1")},
    }
    assert parse_payload(payload).plain == "Réservation confirmée"


def test_parse_payload_unknown_charset_falls_back_to_utf8():
    payload = {
        "mimeType": "text/plain",
        "headers": headers(Content_Type="text/plain; charset=x-inconnu"),
        "body": {"data": b64("Été")},
    }
    assert parse_payload(payload).plain == "Été"


def test_parse_payload_keeps_first_text_part_only():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("premier")}},
            {"mimeType": "text/plain", "body": {"data": b64("second")}},
        ],
    }
    assert parse_payload(payload).plain == "premier"


def test_text_attachment_is_not_read():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("corps")}},
            {"mimeType": "text/plain", "filename": "notes.txt", "body": {"data": b64("secret")}},
        ],
    }
    parsed = parse_payload(payload)
    assert parsed.plain == "corps"
    assert parsed.attachments == ["notes.txt"]


def test_large_text_body_is_fetched_separately():
    message = gmail_message(plain=None, html=None, attachment=None)
    message["payload"]["parts"] = [
        {"mimeType": "text/html", "filename": "", "body": {"attachmentId": "big", "size": 900000}}
    ]
    service = FakeGmailService(messages={"m1": message}, attachments={"big": b64("<p>Long</p>")})
    result = Gmail(service).get_message("m1")
    assert result.html == "<p>Long</p>"
    assert ("attachments.get", {"userId": "me", "messageId": "m1", "id": "big"}) in service.calls


# ==================================================================== lecture


def test_get_message():
    service = FakeGmailService(messages={"m1": gmail_message(labels=("INBOX", "UNREAD"))})
    message = Gmail(service).get_message("m1")
    assert message.id == "m1"
    assert message.thread_id == "t1"
    assert message.label_ids == {"INBOX", "UNREAD"}
    assert message.internal_date == 1795512600000
    assert message.subject == "Votre facture n° 42 — merci !"
    assert message.attachments == ("facture.pdf",)
    assert service.calls[-1][1]["format"] == "full"


def test_list_messages_paginates_and_limits():
    messages = {f"m{i}": gmail_message(f"m{i}", f"t{i}") for i in range(7)}
    service = FakeGmailService(messages=messages, page_size=3)
    box = Gmail(service)
    assert [m for m, _ in box.list_messages()] == [f"m{i}" for i in range(7)]
    assert [m for m, _ in box.list_messages(limit=4)] == ["m0", "m1", "m2", "m3"]
    list_calls = [kwargs for name, kwargs in service.calls if name == "messages.list"]
    assert list_calls[0]["q"] == gmail.DEFAULT_QUERY


def test_profile_email_is_cached():
    service = FakeGmailService(email="me@example.com")
    box = Gmail(service)
    assert box.profile_email() == "me@example.com"
    box.profile_email()
    assert [name for name, _ in service.calls].count("getProfile") == 1


def thread(*messages):
    return {
        "messages": [
            {"id": f"x{i}", "labelIds": list(labels), "internalDate": str(date)}
            for i, (labels, date) in enumerate(messages)
        ]
    }


@pytest.mark.parametrize(
    ("messages", "replied", "last_from_user"),
    [
        ([(["INBOX"], 1)], False, False),
        ([(["INBOX"], 1), (["SENT"], 2)], True, True),
        ([(["SENT"], 1), (["INBOX"], 2)], True, False),
        ([(["INBOX"], 1), (["DRAFT"], 3)], False, False),
        ([(["INBOX"], 2), (["SENT"], 1)], True, False),  # ordre de l'API ≠ ordre des dates
        ([], False, False),
    ],
    ids=["incoming", "replied-last", "replied-before", "draft-ignored", "by-date", "empty"],
)
def test_thread_info(messages, replied, last_from_user):
    service = FakeGmailService(threads={"t1": thread(*messages)})
    info = Gmail(service).thread_info("t1")
    assert (info.user_replied, info.last_from_user) == (replied, last_from_user)


@pytest.mark.parametrize(
    ("labels", "excluded"),
    [
        (("INBOX",), False),
        (("SENT",), True),
        (("DRAFT",), True),
        (("SPAM",), True),
        (("TRASH",), True),
        (("INBOX", "SENT"), True),  # mail envoyé à soi-même
    ],
)
def test_is_excluded(labels, excluded):
    service = FakeGmailService(messages={"m1": gmail_message(labels=labels)})
    assert is_excluded(Gmail(service).get_message("m1")) is excluded


# ==================================================================== erreurs


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(401), GmailAuthError),
        (RefreshError("invalid_grant"), GmailAuthError),
        (http_error(404), GmailError),
        (http_error(500), GmailError),
    ],
)
def test_errors_are_translated(error, expected):
    service = FakeGmailService(messages={"m1": error})
    with pytest.raises(expected) as exc_info:
        Gmail(service).get_message("m1")
    if expected is GmailAuthError:
        assert str(exc_info.value) == AUTH_HELP


# ====================================================================== jeton


def write_token(path, *, scopes=SCOPES, expired=False, refresh_token="refresh"):
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=-1 if expired else 1)
    credentials = Credentials(
        token="access",
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client",
        client_secret="secret",
        scopes=scopes,
        expiry=expiry,
    )
    path.write_text(credentials.to_json())
    return path


def test_missing_token(tmp_path):
    with pytest.raises(GmailAuthError, match="mailsort auth"):
        load_credentials(tmp_path / "token.json")


def test_unreadable_token(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"token": "x"}))
    with pytest.raises(GmailAuthError, match="mailsort auth"):
        load_credentials(path)


def test_valid_token(tmp_path):
    credentials = load_credentials(write_token(tmp_path / "token.json"))
    assert credentials.valid


def test_token_without_modify_scope(tmp_path):
    path = write_token(
        tmp_path / "token.json", scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )
    with pytest.raises(GmailAuthError, match="gmail.modify"):
        load_credentials(path)


def test_expired_token_is_refreshed_and_saved(tmp_path, monkeypatch):
    path = write_token(tmp_path / "token.json", expired=True)

    def fake_refresh(self, request):
        self.token = "new-access"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    credentials = load_credentials(path)
    assert credentials.token == "new-access"
    assert json.loads(path.read_text())["token"] == "new-access"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_revoked_token_gives_explicit_message(tmp_path, monkeypatch):
    path = write_token(tmp_path / "token.json", expired=True)

    def fail(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", fail)
    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(path)
    assert str(exc_info.value) == AUTH_HELP


def test_auth_flow_requires_client_secret(tmp_path):
    with pytest.raises(GmailAuthError, match="credentials.json.example"):
        gmail.run_auth_flow(tmp_path / "credentials.json", tmp_path / "token.json")


# ==================================================================== résumé


def test_summary_from_gmail_message():
    service = FakeGmailService(messages={"m1": gmail_message(List_Unsubscribe="<https://x>")})
    message = Gmail(service).get_message("m1")
    summary = summarize(
        message.headers,
        message.plain,
        message.html,
        message.attachments,
        user_addresses=["alexis@example.com"],
        user_replied=False,
    )
    assert summary.user_position == "direct"
    assert summary.subject == "Votre facture n° 42 — merci !"
    assert summary.date == "2026-11-24T09:30:00+00:00"
    assert summary.list_headers
    assert summary.attachments == ("facture.pdf",)
    assert summary.body.text == "Bonjour, voici la facture."
