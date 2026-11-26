"""Faux service Gmail (même enchaînement d'appels que googleapiclient), sans réseau."""

from __future__ import annotations

import base64
from typing import Any


def b64(text: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def headers(**values: str) -> list[dict[str, str]]:
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


class Request:
    """Requête différée, comme dans googleapiclient : `execute()` peut être rappelé (nouvelle
    tentative). `failures` : erreurs levées aux premiers appels, avant le vrai résultat."""

    def __init__(
        self,
        result: Any,
        failures: list[BaseException] | None = None,
        action: Any = None,
    ):
        self.result = result
        self.failures = failures if failures is not None else []
        self.action = action

    def execute(self, num_retries: int = 0) -> Any:
        if self.failures:
            raise self.failures.pop(0)
        if isinstance(self.result, BaseException):
            raise self.result
        if self.action is not None:
            return self.action()
        return self.result


def http_error(status: int, message: str = "x", reason: str | None = None, **headers: str):
    """Erreur de l'API Google, au format JSON réel."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    error: dict[str, Any] = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"message": message, "domain": "usageLimits", "reason": reason}]
    import json

    return HttpError(Response({"status": status, **headers}), json.dumps({"error": error}).encode())


class FakeGmailService:
    def __init__(
        self,
        *,
        email: str = "alexis@example.com",
        messages: dict[str, dict] | None = None,
        threads: dict[str, dict] | None = None,
        attachments: dict[str, str] | None = None,
        page_size: int | None = None,
        errors: dict[str, BaseException] | None = None,
        labels: dict[str, str] | None = None,
        flaky: dict[str, list[BaseException]] | None = None,
    ):
        """`errors` : identifiant de message → erreur levée quand on le lit (il reste listé).
        `labels` : labels utilisateur déjà présents, identifiant → nom.
        `flaky` : nom d'appel (« messages.get »…) → erreurs à lever avant de réussir.
        """
        self.email = email
        self.errors = errors or {}
        self.user_labels: dict[str, str] = dict(labels or {})
        self.label_settings: dict[str, dict] = {}
        self.flaky = {name: list(errs) for name, errs in (flaky or {}).items()}
        self.modify_errors: dict[str, BaseException] = {}
        self._next_label = 1
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

    def labels(self) -> _Labels:
        return _Labels(self)

    def request(self, name: str, result: Any = None, action: Any = None) -> Request:
        return Request(result, self.flaky.get(name, []), action)

    # Outils pour les tests -------------------------------------------------
    def label_id(self, name: str) -> str:
        return next(i for i, n in self.user_labels.items() if n == name)

    def names_on(self, message_id: str) -> set[str]:
        """Noms des labels utilisateur portés par un message."""
        ids = self.messages_by_id[message_id]["labelIds"]
        return {self.user_labels[i] for i in ids if i in self.user_labels}

    def set_labels(self, message_id: str, *names: str) -> None:
        """Pose ou retire des labels « à la main » (comme Alexis dans Gmail)."""
        message = self.messages_by_id[message_id]
        system = [i for i in message["labelIds"] if i not in self.user_labels]
        message["labelIds"] = system + [self.label_id(n) for n in names]

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)


class _Messages:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def list(self, **kwargs: Any) -> Request:
        self.service.calls.append(("messages.list", kwargs))
        if kwargs.get("q") == "in:sent":
            # Messages envoyés par l'utilisateur, tirés des fils (comme la recherche Gmail).
            sent = [
                {"id": f"{thread_id}-sent{i}", "threadId": thread_id}
                for thread_id, thread in self.service.threads_by_id.items()
                for i, message in enumerate(thread.get("messages") or [])
                if "SENT" in (message.get("labelIds") or [])
            ]
            return self.service.request("messages.list", {"messages": sent})
        ids = list(self.service.messages_by_id)
        if kwargs.get("labelIds"):
            wanted = set(kwargs["labelIds"])
            ids = [
                i
                for i in ids
                if isinstance(self.service.messages_by_id[i], dict)
                and wanted <= set(self.service.messages_by_id[i].get("labelIds") or [])
            ]
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
        return self.service.request("messages.list", result)

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("messages.get", kwargs))
        if kwargs["id"] in self.service.errors:
            return Request(self.service.errors[kwargs["id"]])
        message = self.service.messages_by_id.get(kwargs["id"])
        if isinstance(message, BaseException) or message is None:
            return Request(message or http_error(404, "Requested entity was not found."))
        return self.service.request("messages.get", message)

    def modify(self, **kwargs: Any) -> Request:
        self.service.calls.append(("messages.modify", kwargs))
        service = self.service
        message_id = kwargs["id"]

        def apply() -> dict:
            if message_id in service.modify_errors:
                raise service.modify_errors[message_id]
            message = service.messages_by_id[message_id]
            body = kwargs["body"]
            for label in body.get("addLabelIds", []) + body.get("removeLabelIds", []):
                if label not in service.user_labels:
                    raise http_error(400, f"Invalid label: {label}")
            labels = [i for i in message["labelIds"] if i not in body.get("removeLabelIds", [])]
            labels += [i for i in body.get("addLabelIds", []) if i not in labels]
            message["labelIds"] = labels
            return {"id": message_id, "labelIds": labels}

        return service.request("messages.modify", action=apply)

    def attachments(self) -> _Attachments:
        return _Attachments(self.service)


class _Attachments:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("attachments.get", kwargs))
        return Request({"data": self.service.attachments_by_id[kwargs["id"]]})


class _Labels:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def list(self, **kwargs: Any) -> Request:
        self.service.calls.append(("labels.list", kwargs))
        system = [{"id": i, "name": i, "type": "system"} for i in ("INBOX", "SENT", "UNREAD")]
        user = [{"id": i, "name": n, "type": "user"} for i, n in self.service.user_labels.items()]
        return self.service.request("labels.list", {"labels": system + user})

    def create(self, **kwargs: Any) -> Request:
        self.service.calls.append(("labels.create", kwargs))
        service = self.service
        body = kwargs["body"]

        def apply() -> dict:
            name = body["name"]
            if any(n.casefold() == name.casefold() for n in service.user_labels.values()):
                raise http_error(409, "Label name exists or conflicts")
            label_id = f"Label_{service._next_label}"
            service._next_label += 1
            service.user_labels[label_id] = name
            service.label_settings[label_id] = body
            return {"id": label_id, "name": name}

        return service.request("labels.create", action=apply)

    def patch(self, **kwargs: Any) -> Request:
        self.service.calls.append(("labels.patch", kwargs))
        service = self.service

        def apply() -> dict:
            if kwargs["id"] not in service.user_labels:
                raise http_error(404, "Label not found")
            service.user_labels[kwargs["id"]] = kwargs["body"]["name"]
            return {"id": kwargs["id"], "name": kwargs["body"]["name"]}

        return service.request("labels.patch", action=apply)

    def delete(self, **kwargs: Any) -> Request:
        self.service.calls.append(("labels.delete", kwargs))
        service = self.service

        def apply() -> str:
            if kwargs["id"] not in service.user_labels:
                raise http_error(404, "Label not found")
            del service.user_labels[kwargs["id"]]
            for message in service.messages_by_id.values():
                if isinstance(message, dict):
                    message["labelIds"] = [i for i in message["labelIds"] if i != kwargs["id"]]
            return ""

        return service.request("labels.delete", action=apply)


class _Threads:
    def __init__(self, service: FakeGmailService):
        self.service = service

    def get(self, **kwargs: Any) -> Request:
        self.service.calls.append(("threads.get", kwargs))
        return self.service.request("threads.get", self.service.threads_by_id[kwargs["id"]])


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
