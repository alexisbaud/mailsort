"""Accès à Gmail : connexion OAuth, lecture des messages et des fils.

Le droit demandé est `gmail.modify` : gérer les labels, sans pouvoir supprimer définitivement
un mail. Cette étape ne fait que lire ; la pose des labels viendra ensuite.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DEFAULT_CLIENT_SECRET_FILE = "credentials.json"
DEFAULT_TOKEN_FILE = "token.json"

EXCLUDED_LABELS = frozenset({"SPAM", "TRASH", "DRAFT", "SENT"})
"""Spams, corbeille, brouillons et messages envoyés par l'utilisateur : jamais traités."""
DEFAULT_QUERY = "-in:sent -in:drafts"
"""La liste Gmail exclut déjà spams et corbeille (`includeSpamTrash` à faux)."""

_NUM_RETRIES = 5
"""Nouvelles tentatives de la bibliothèque Google (erreurs 429 et 5xx)."""

AUTH_HELP = (
    "Accès Gmail refusé : jeton expiré ou révoqué. Lancer `mailsort auth` sur le Mac, "
    "puis copier token.json sur le VPS."
)


class GmailError(Exception):
    """Échec d'un appel à Gmail."""


class GmailAuthError(GmailError):
    """Jeton absent, expiré ou révoqué : le passage doit s'arrêter."""


# ===================================================================== OAuth


def client_secret_path() -> Path:
    return Path(os.environ.get("GOOGLE_CLIENT_SECRET_FILE") or DEFAULT_CLIENT_SECRET_FILE)


def token_path() -> Path:
    return Path(os.environ.get("GOOGLE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)


def _write_token(credentials: Credentials, path: Path) -> None:
    path.write_text(credentials.to_json(), encoding="utf-8")
    path.chmod(0o600)


def run_auth_flow(secret: Path, token: Path) -> Credentials:
    """Ouvre le navigateur pour autoriser l'accès, puis écrit le jeton (droits 600)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not secret.exists():
        raise GmailAuthError(
            f"{secret} introuvable : télécharger le client OAuth « Desktop app » depuis la "
            "console Google Cloud et l'enregistrer sous ce nom (modèle : credentials.json.example)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    _write_token(credentials, token)
    return credentials


def load_credentials(token: Path) -> Credentials:
    """Charge le jeton et le rafraîchit si besoin. Lève `GmailAuthError` s'il est inutilisable."""
    if not token.exists():
        raise GmailAuthError(f"{token} introuvable. Lancer `mailsort auth` sur le Mac.")
    try:
        # Sans `scopes` en argument : on lit les droits réellement accordés, enregistrés dans le
        # fichier, pour pouvoir les vérifier.
        credentials = Credentials.from_authorized_user_file(str(token))
    except (ValueError, KeyError) as exc:
        raise GmailAuthError(f"{token} illisible ({exc}). Relancer `mailsort auth`.") from exc
    if not credentials.has_scopes(SCOPES):
        raise GmailAuthError(f"{token} n'a pas le droit gmail.modify. Relancer `mailsort auth`.")
    if not credentials.valid:
        if not credentials.refresh_token:
            raise GmailAuthError(AUTH_HELP)
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise GmailAuthError(AUTH_HELP) from exc
        _write_token(credentials, token)
    return credentials


# ========================================================= lecture des messages


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str
    label_ids: frozenset[str]
    internal_date: int
    headers: Message
    plain: str | None
    html: str | None
    attachments: tuple[str, ...]

    @property
    def subject(self) -> str:
        return str(self.headers.get("Subject") or "")


@dataclass(frozen=True)
class ThreadInfo:
    user_replied: bool
    """L'utilisateur a envoyé au moins un message dans le fil."""
    last_from_user: bool
    """Le dernier message du fil (brouillons exclus) vient de l'utilisateur."""


def headers_to_message(headers: Sequence[Mapping[str, str]]) -> Message:
    """En-têtes Gmail ([{name, value}], déjà décodés) → objet `Message` du module email."""
    message = Message()
    for header in headers:
        message[header["name"]] = header["value"]
    return message


def _decode_body(data: str, content_type: str | None) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    if content_type:
        part = Message()
        part["Content-Type"] = content_type
        charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


@dataclass
class ParsedPayload:
    plain: str | None = None
    html: str | None = None
    attachments: list[str] | None = None
    pending: list[tuple[str, str, str | None]] | None = None
    """Parties texte dont Gmail n'a pas inclus le contenu : (type, attachmentId, Content-Type)."""


def parse_payload(payload: Mapping[str, Any]) -> ParsedPayload:
    """Extrait du format `full` de Gmail les premières parties texte et HTML et les noms des
    pièces jointes (dont le contenu n'est jamais téléchargé)."""
    result = ParsedPayload(attachments=[], pending=[])

    def walk(part: Mapping[str, Any]) -> None:
        mime = str(part.get("mimeType", "")).lower()
        if mime.startswith("multipart/"):
            for child in part.get("parts") or []:
                walk(child)
            return
        filename = part.get("filename") or ""
        if filename:
            result.attachments.append(filename)  # type: ignore[union-attr]
            return
        if mime not in ("text/plain", "text/html"):
            return
        attr = "plain" if mime == "text/plain" else "html"
        if getattr(result, attr) is not None:
            return
        headers = {h["name"].lower(): h["value"] for h in part.get("headers") or []}
        body = part.get("body") or {}
        if body.get("data"):
            setattr(result, attr, _decode_body(body["data"], headers.get("content-type")))
        elif body.get("attachmentId"):
            result.pending.append((attr, body["attachmentId"], headers.get("content-type")))  # type: ignore[union-attr]

    walk(payload)
    return result


class Gmail:
    """Lecture de la boîte Gmail de l'utilisateur."""

    def __init__(self, service: Any):
        self._users = service.users()
        self._profile_email: str | None = None

    @classmethod
    def connect(cls, token: Path | None = None) -> Gmail:
        credentials = load_credentials(token or token_path())
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    def _execute(self, request: Any) -> Any:
        try:
            return request.execute(num_retries=_NUM_RETRIES)
        except RefreshError as exc:
            raise GmailAuthError(AUTH_HELP) from exc
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 401:
                raise GmailAuthError(AUTH_HELP) from exc
            raise GmailError(f"Erreur Gmail ({status}) : {exc.reason}") from exc

    def profile_email(self) -> str:
        if self._profile_email is None:
            profile = self._execute(self._users.getProfile(userId="me"))
            self._profile_email = str(profile["emailAddress"])
        return self._profile_email

    def list_messages(
        self, query: str | None = DEFAULT_QUERY, limit: int | None = None
    ) -> Iterator[tuple[str, str]]:
        """(id, thread_id) des messages, du plus récent au plus ancien."""
        page_token: str | None = None
        count = 0
        while True:
            page_size = 500 if limit is None else min(500, limit - count)
            response = self._execute(
                self._users.messages().list(
                    userId="me", q=query or None, maxResults=page_size, pageToken=page_token
                )
            )
            for item in response.get("messages") or []:
                yield item["id"], item["threadId"]
                count += 1
                if limit is not None and count >= limit:
                    return
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_message(self, message_id: str) -> GmailMessage:
        data = self._execute(self._users.messages().get(userId="me", id=message_id, format="full"))
        payload = data.get("payload") or {}
        parsed = parse_payload(payload)
        for attr, attachment_id, content_type in parsed.pending or []:
            attachment = self._execute(
                self._users.messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
            )
            setattr(parsed, attr, _decode_body(attachment.get("data", ""), content_type))
        return GmailMessage(
            id=data["id"],
            thread_id=data["threadId"],
            label_ids=frozenset(data.get("labelIds") or []),
            internal_date=int(data.get("internalDate") or 0),
            headers=headers_to_message(payload.get("headers") or []),
            plain=parsed.plain,
            html=parsed.html,
            attachments=tuple(parsed.attachments or ()),
        )

    def get_headers(self, message_id: str, names: Sequence[str]) -> Message:
        """Quelques en-têtes seulement, sans le corps (pour lister rapidement)."""
        data = self._execute(
            self._users.messages().get(
                userId="me", id=message_id, format="metadata", metadataHeaders=list(names)
            )
        )
        return headers_to_message((data.get("payload") or {}).get("headers") or [])

    def thread_info(self, thread_id: str) -> ThreadInfo:
        data = self._execute(self._users.threads().get(userId="me", id=thread_id, format="minimal"))
        messages = [
            m for m in data.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])
        ]
        sent = ["SENT" in (m.get("labelIds") or []) for m in messages]
        if not messages:
            return ThreadInfo(user_replied=False, last_from_user=False)
        last = max(range(len(messages)), key=lambda i: int(messages[i].get("internalDate") or 0))
        return ThreadInfo(user_replied=any(sent), last_from_user=sent[last])


def is_excluded(message: GmailMessage) -> bool:
    return bool(message.label_ids & EXCLUDED_LABELS)
