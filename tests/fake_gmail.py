"""Faux service Gmail (même enchaînement d'appels que googleapiclient), sans réseau."""

from __future__ import annotations

import base64
from typing import Any


def b64(text: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def headers(**values: str) -> list[dict[str, str]]:
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


class Request:
    def __init__(self, result: Any):
        self.result = result

    def execute(self, num_retries: int = 0) -> Any:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeGmailService:
    def __init__(
        self,
        *,
        email: str = "alexis@example.com",
        messages: dict[str, dict] | None = None,
        threads: dict[str, dict] | None = None,
        attachments: dict[str, str] | None = None,
        page_size: int | None = None,
    ):
        self.email = email
        self.messages_by_id = messages or {}
        self.threads_by_id = threads or {}
        self.attachments_by_id = attachments or {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []

    # Chaîne service.users().messages().get(...).execute()
    def users(self) -> FakeGmailService:
        return self

    def getProfile(self, **kwargs: Any) -> Request:  # noqa: N802 (nom de l'API Google)
        self.calls.append(("getProfile", kwargs))
        return Request({"emailAddress": self.email})

    def messages(self) -> _Messages:
        return _Messages(self)

    def threads(self) -> _Threads:
        return _Threads(self)


class _Messages:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def list(self, **kwargs: Any) -> Request:
        self.service.calls.append(("messages.list", kwargs))
        ids = list(self.service.messages_by_id)
        size = min(kwargs.get("maxResults") or 500, self.service.page_size or 500)
        start = int(kwargs.get("pageToken") or 0)
        page = ids[start : start + size]
        result: dict[str, Any] = {
            "messages": [
                {"id": i, "threadId": self.service.messages_by_id[i]["threadId"]} for i in page
            ]
        }
        if start + size < len(ids):
            result["nextPageToken"] = str(start + size)
        return Request(result)

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("messages.get", kwargs))
        message = self.service.messages_by_id.get(kwargs["id"])
        if isinstance(message, BaseException) or message is None:
            return Request(message or KeyError(kwargs["id"]))
        return Request(message)

    def attachments(self) -> _Attachments:
        return _Attachments(self.service)


class _Attachments:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("attachments.get", kwargs))
        return Request({"data": self.service.attachments_by_id[kwargs["id"]]})


class _Threads:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("threads.get", kwargs))
        return Request(self.service.threads_by_id[kwargs["id"]])


def gmail_message(
    message_id: str = "m1",
    thread_id: str = "t1",
    *,
    labels: tuple[str, ...] = ("INBOX",),
    plain: str | None = "Bonjour, voici la facture.",
    html: str | None = "<p>Bonjour, voici la <b>facture</b>.</p>",
    attachment: str | None = "facture.pdf",
    **header_values: str,
) -> dict:
    """Message au format `full` de l'API Gmail."""
    values = {
        "From": "Boutique <shop@example.com>",
        "To": "Alexis <alexis@example.com>",
        "Subject": "Votre facture n° 42 — merci !",
        "Date": "Tue, 24 Nov 2026 09:30:00 +0000",
    }
    values.update({name.replace("_", "-"): value for name, value in header_values.items()})
    alternatives = []
    if plain is not None:
        alternatives.append(
            {
                "mimeType": "text/plain",
                "filename": "",
                "headers": headers(Content_Type='text/plain; charset="UTF-8"'),
                "body": {"data": b64(plain)},
            }
        )
    if html is not None:
        alternatives.append(
            {
                "mimeType": "text/html",
                "filename": "",
                "headers": headers(Content_Type='text/html; charset="UTF-8"'),
                "body": {"data": b64(html)},
            }
        )
    parts: list[dict] = [{"mimeType": "multipart/alternative", "parts": alternatives}]
    if attachment:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": attachment,
                "body": {"attachmentId": "att-pdf", "size": 1234},
            }
        )
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "internalDate": "1795512600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": k, "value": v} for k, v in values.items()],
            "parts": parts,
        },
    }
