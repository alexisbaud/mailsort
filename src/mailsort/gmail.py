"""Accès à Gmail : connexion OAuth, lecture des messages et des fils, gestion des labels.

Le droit demandé est `gmail.modify` : gérer les labels, sans pouvoir supprimer définitivement
un mail. Le programme ne supprime jamais de mail : il crée, renomme et pose des labels.

Robustesse face aux limites de Gmail :

- chaque appel consomme des « unités de quota » (5 pour lire un mail, 10 pour un fil…) ; Gmail
  limite les unités par minute et par utilisateur. Un compteur (`QuotaLimiter`) étale les appels
  pour rester sous un budget (`GMAIL_QUOTA_UNITS_PER_MINUTE`, 6 000 par défaut) ;
- si Gmail répond quand même « trop de requêtes » (429, ou 403 avec une limite par minute), une
  erreur serveur (5xx) ou une coupure réseau, l'appel est réessayé avec un délai croissant
  (jusqu'à environ 2 minutes au total, en suivant `Retry-After` s'il est fourni) ;
- un quota journalier épuisé (`GmailQuotaError`) ou un accès refusé (`GmailAuthError`) arrêtent
  le passage : réessayer ne servirait à rien ;
- chaque requête a un délai d'attente : un réseau bloqué ne fige pas le programme.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import socket
import ssl
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DEFAULT_CLIENT_SECRET_FILE = "credentials.json"
DEFAULT_TOKEN_FILE = "token.json"

EXCLUDED_LABELS = frozenset({"SPAM", "TRASH", "DRAFT", "SENT"})
"""Spams, corbeille, brouillons et messages envoyés par l'utilisateur : jamais traités."""
DEFAULT_QUERY = "-in:sent -in:drafts"
"""La liste Gmail exclut déjà spams et corbeille (`includeSpamTrash` à faux)."""

DEFAULT_QUOTA_UNITS_PER_MINUTE = 6000.0
"""Budget d'unités de quota par minute. Gmail en autorise 15 000 par utilisateur : on garde une
large marge, car d'autres applications (le client Gmail lui-même) consomment le même quota."""

QUOTA_COST: dict[str, int] = {
    "getProfile": 1,
    "labels.list": 1,
    "labels.get": 1,
    "labels.create": 5,
    "labels.patch": 5,
    "labels.delete": 5,
    "messages.list": 5,
    "messages.get": 5,
    "messages.modify": 5,
    "attachments.get": 5,
    "threads.get": 10,
}
"""Coût de chaque appel en unités de quota (documentation de l'API Gmail)."""

BURST_SECONDS = 2.0
"""Rafale autorisée, en secondes de budget."""

MAX_ATTEMPTS = 8
"""Tentatives par appel : 1 + 7 nouvelles tentatives, soit jusqu'à 2 minutes d'attente."""
MAX_BACKOFF_SECONDS = 64.0
REQUEST_TIMEOUT_SECONDS = 60.0

_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_DAILY_LIMIT_REASONS = frozenset({"dailyLimitExceeded", "quotaExceeded"})
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    socket.timeout,
    ConnectionError,
    ssl.SSLError,
    httplib2.HttpLib2Error,
    TransportError,
    OSError,
)

AUTH_HELP = (
    "Accès Gmail refusé : jeton expiré ou révoqué. Lancer `mailsort auth` sur le Mac, "
    "puis copier token.json sur le VPS."
)


class GmailError(Exception):
    """Échec d'un appel à Gmail."""


class GmailAuthError(GmailError):
    """Jeton absent, expiré ou révoqué : le passage doit s'arrêter."""


class GmailQuotaError(GmailError):
    """Quota journalier de Gmail épuisé : le passage s'arrête et reprendra au suivant."""


class GmailNotFoundError(GmailError):
    """Le mail ou le label n'existe plus (supprimé entre-temps)."""


class GmailConflictError(GmailError):
    """Un label de ce nom existe déjà dans Gmail."""


_log = logging.getLogger("mailsort")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class QuotaLimiter:
    """Étale les appels pour ne pas dépasser un budget d'unités de quota par minute.

    Seau à jetons : il se remplit au rythme du budget et contient au plus 2 secondes de budget.
    Gmail lisse sa limite sur quelques secondes : une rafale plus longue (mesuré : une centaine
    de lectures de fils d'affilée) est refusée même si le total de la minute reste sous la
    limite.
    """

    def __init__(
        self,
        units_per_minute: float = DEFAULT_QUOTA_UNITS_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ):
        self.rate = max(units_per_minute, 1.0) / 60.0
        self.capacity = max(self.rate * BURST_SECONDS, float(max(QUOTA_COST.values())))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep if sleep is not None else _sleep
        self._last = clock()
        self.waited = 0.0
        """Temps total passé à attendre, pour le bilan."""

    @classmethod
    def from_env(cls) -> QuotaLimiter:
        raw = os.environ.get("GMAIL_QUOTA_UNITS_PER_MINUTE") or ""
        try:
            value = float(raw) if raw else DEFAULT_QUOTA_UNITS_PER_MINUTE
        except ValueError:
            value = DEFAULT_QUOTA_UNITS_PER_MINUTE
        return cls(value if value > 0 else DEFAULT_QUOTA_UNITS_PER_MINUTE)

    def acquire(self, cost: float) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens < cost:
            wait = (cost - self._tokens) / self.rate
            self._sleep(wait)
            self.waited += wait
            self._tokens = cost
            self._last = self._clock()
        self._tokens -= cost

    def drain(self) -> None:
        """Après un refus de Gmail pour excès de débit : repartir d'un seau vide."""
        self._tokens = 0.0
        self._last = self._clock()


def _error_payload(exc: HttpError) -> tuple[set[str], str]:
    """Raisons (`reason`) et message d'une erreur de l'API Google."""
    reasons: set[str] = set()
    message = str(exc.reason or "")
    try:
        data = json.loads(exc.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return reasons, message
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or message)
        for item in error.get("errors") or []:
            if isinstance(item, dict) and item.get("reason"):
                reasons.add(str(item["reason"]))
        if error.get("status"):
            reasons.add(str(error["status"]))
    return reasons, message


def _retry_after(exc: HttpError) -> float | None:
    try:
        value = float(exc.resp.get("retry-after", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return max(value, 0.0)


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
    """Accès à la boîte Gmail de l'utilisateur, avec limite de débit et nouvelles tentatives."""

    def __init__(
        self,
        service: Any,
        *,
        limiter: QuotaLimiter | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        jitter: Callable[[], float] = random.random,
    ):
        self._users = service.users()
        self._profile_email: str | None = None
        self._sleep = sleep if sleep is not None else _sleep
        self.limiter = limiter if limiter is not None else QuotaLimiter(sleep=self._sleep)
        self._max_attempts = max(1, max_attempts)
        self._jitter = jitter
        self.retries = 0
        """Nombre de nouvelles tentatives faites depuis la connexion, pour le bilan."""

    @classmethod
    def connect(cls, token: Path | None = None) -> Gmail:
        credentials = load_credentials(token or token_path())
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS))
        service = build("gmail", "v1", http=http, cache_discovery=False)
        return cls(service, limiter=QuotaLimiter.from_env())

    def _backoff(self, attempt: int, exc: BaseException) -> float:
        if isinstance(exc, HttpError):
            retry_after = _retry_after(exc)
            if retry_after is not None:
                return min(retry_after, MAX_BACKOFF_SECONDS)
        return min(2.0**attempt + self._jitter(), MAX_BACKOFF_SECONDS)

    def _execute(self, request: Any, method: str) -> Any:
        """Exécute un appel : attend son tour (quota), puis réessaie ce qui peut l'être."""
        problem = ""
        for attempt in range(self._max_attempts):
            self.limiter.acquire(QUOTA_COST.get(method, 5))
            try:
                return request.execute(num_retries=0)
            except RefreshError as exc:
                raise GmailAuthError(AUTH_HELP) from exc
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                reasons, message = _error_payload(exc)
                lowered = message.lower()
                if status == 401:
                    raise GmailAuthError(AUTH_HELP) from exc
                if (reasons & _DAILY_LIMIT_REASONS) or "per day" in lowered:
                    raise GmailQuotaError(
                        f"Quota journalier de Gmail épuisé ({message}). Le passage reprendra "
                        "au suivant."
                    ) from exc
                rate_limited = status == 429 or (
                    status == 403
                    and (
                        bool(reasons & _RATE_LIMIT_REASONS)
                        or "RESOURCE_EXHAUSTED" in reasons
                        or "per minute" in lowered
                        or "rate limit" in lowered
                    )
                )
                if status == 403 and not rate_limited:
                    if "insufficientPermissions" in reasons or "insufficient" in lowered:
                        raise GmailAuthError(
                            "Gmail refuse l'opération : le jeton n'a pas le droit gmail.modify. "
                            "Relancer `mailsort auth` sur le Mac."
                        ) from exc
                    raise GmailError(f"Erreur Gmail (403) : {message}") from exc
                if status == 404:
                    raise GmailNotFoundError(f"Introuvable dans Gmail : {message}") from exc
                if status == 409:
                    raise GmailConflictError(f"Conflit Gmail : {message}") from exc
                if not (rate_limited or (status is not None and status >= 500)):
                    raise GmailError(f"Erreur Gmail ({status}) : {message}") from exc
                if rate_limited:
                    self.limiter.drain()
                problem = f"{status} : {message}"
                last: BaseException = exc
            except _NETWORK_ERRORS as exc:
                problem = f"erreur réseau ({type(exc).__name__} : {exc})"
                last = exc
            if attempt + 1 < self._max_attempts:
                self.retries += 1
                delay = self._backoff(attempt, last)
                _log.warning(
                    "Gmail %s : %s ; nouvelle tentative dans %.0f s", method, problem, delay
                )
                self._sleep(delay)
        raise GmailError(f"Gmail indisponible après {self._max_attempts} tentatives ({problem})")

    def profile_email(self) -> str:
        if self._profile_email is None:
            profile = self._execute(self._users.getProfile(userId="me"), "getProfile")
            self._profile_email = str(profile["emailAddress"])
        return self._profile_email

    def list_messages(
        self,
        query: str | None = DEFAULT_QUERY,
        limit: int | None = None,
        *,
        label_ids: Sequence[str] | None = None,
    ) -> Iterator[tuple[str, str]]:
        """(id, thread_id) des messages, du plus récent au plus ancien (spams et corbeille
        exclus). `label_ids` : seulement les messages qui portent ces labels."""
        page_token: str | None = None
        count = 0
        while True:
            page_size = 500 if limit is None else min(500, limit - count)
            params: dict[str, Any] = {
                "userId": "me",
                "q": query or None,
                "maxResults": page_size,
                "pageToken": page_token,
            }
            if label_ids:
                params["labelIds"] = list(label_ids)
            response = self._execute(self._users.messages().list(**params), "messages.list")
            for item in response.get("messages") or []:
                yield item["id"], item["threadId"]
                count += 1
                if limit is not None and count >= limit:
                    return
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_message(self, message_id: str) -> GmailMessage:
        data = self._execute(
            self._users.messages().get(userId="me", id=message_id, format="full"), "messages.get"
        )
        payload = data.get("payload") or {}
        parsed = parse_payload(payload)
        for attr, attachment_id, content_type in parsed.pending or []:
            attachment = self._execute(
                self._users.messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id),
                "attachments.get",
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
            ),
            "messages.get",
        )
        return headers_to_message((data.get("payload") or {}).get("headers") or [])

    def thread_info(self, thread_id: str) -> ThreadInfo:
        data = self._execute(
            self._users.threads().get(userId="me", id=thread_id, format="minimal"), "threads.get"
        )
        messages = [
            m for m in data.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])
        ]
        sent = ["SENT" in (m.get("labelIds") or []) for m in messages]
        if not messages:
            return ThreadInfo(user_replied=False, last_from_user=False)
        last = max(range(len(messages)), key=lambda i: int(messages[i].get("internalDate") or 0))
        return ThreadInfo(user_replied=any(sent), last_from_user=sent[last])

    # ------------------------------------------------------------------ écriture

    def list_labels(self) -> list[GmailLabelInfo]:
        """Labels créés par l'utilisateur (les labels système comme INBOX sont exclus)."""
        data = self._execute(self._users.labels().list(userId="me"), "labels.list")
        return [
            GmailLabelInfo(id=str(item["id"]), name=str(item["name"]))
            for item in data.get("labels") or []
            if item.get("type") == "user"
        ]

    def create_label(
        self,
        name: str,
        *,
        label_list: str | None = None,
        message_list: str | None = None,
        background: str | None = None,
        text: str | None = None,
    ) -> GmailLabelInfo:
        body: dict[str, Any] = {"name": name}
        if label_list:
            body["labelListVisibility"] = label_list
        if message_list:
            body["messageListVisibility"] = message_list
        if background and text:
            body["color"] = {"backgroundColor": background, "textColor": text}
        data = self._execute(self._users.labels().create(userId="me", body=body), "labels.create")
        return GmailLabelInfo(id=str(data["id"]), name=str(data["name"]))

    def rename_label(self, label_id: str, name: str) -> None:
        self._execute(
            self._users.labels().patch(userId="me", id=label_id, body={"name": name}),
            "labels.patch",
        )

    def delete_label(self, label_id: str) -> None:
        """Supprime un label de Gmail. Les mails restent : ils perdent seulement ce label."""
        self._execute(self._users.labels().delete(userId="me", id=label_id), "labels.delete")

    def modify_labels(self, message_id: str, add: Sequence[str], remove: Sequence[str]) -> None:
        """Ajoute et retire des labels sur un mail, en un seul appel."""
        if not add and not remove:
            return
        body = {"addLabelIds": list(add), "removeLabelIds": list(remove)}
        self._execute(
            self._users.messages().modify(userId="me", id=message_id, body=body),
            "messages.modify",
        )


@dataclass(frozen=True)
class GmailLabelInfo:
    id: str
    name: str


def is_excluded(message: GmailMessage) -> bool:
    return bool(message.label_ids & EXCLUDED_LABELS)
