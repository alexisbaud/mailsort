"""Préparation du résumé structuré d'un mail, envoyé à Jev comme « état ».

Contenu (section « Données envoyées à Jev » de la spec) : expéditeur, place de l'utilisateur
parmi les destinataires, objet, date, signes d'envoi de masse, noms des pièces jointes,
réponse déjà faite dans le fil, texte nettoyé. Les clés sont en anglais, comme les descriptions
des labels que Jev lit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Literal

from mailsort.clean import CleanText, attachment_names, bodies_from_message, clean_email_text

UserPosition = Literal["direct", "cc", "indirect", "unknown"]

_POSITION_TEXT: dict[str, str] = {
    "direct": "direct recipient (To)",
    "cc": "in copy (Cc)",
    "indirect": "not addressed directly (mailing list, alias or Bcc)",
}
_UNSUBSCRIBE = re.compile(
    r"unsubscribe|d[ée]sinscri|d[ée]sabonn|se retirer de (?:la|cette) liste|opt[ -]?out",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


@dataclass(frozen=True)
class MailSummary:
    sender: str
    user_position: UserPosition
    subject: str
    date: str | None
    """Date ISO 8601, ou None si l'en-tête est absent ou illisible."""
    list_headers: bool
    """En-têtes de liste de diffusion (List-Id, List-Unsubscribe, Precedence: bulk…)."""
    unsubscribe_link: bool
    attachments: tuple[str, ...]
    user_replied: bool | None
    """L'utilisateur a déjà répondu dans le fil ; None si on ne le sait pas (fichier .eml)."""
    body: CleanText

    def to_state(self) -> dict[str, Any]:
        """L'« état » envoyé à Jev."""
        state: dict[str, Any] = {"from": self.sender}
        if self.user_position != "unknown":
            state["user_is"] = _POSITION_TEXT[self.user_position]
        state["subject"] = self.subject
        if self.date:
            state["date"] = self.date
        state["mailing_list_headers"] = self.list_headers
        state["unsubscribe_link"] = self.unsubscribe_link
        state["attachments"] = list(self.attachments)
        if self.user_replied is not None:
            state["user_already_replied_in_thread"] = self.user_replied
        state["body"] = self.body.text
        return state


def _addresses(message: Message, header: str) -> set[str]:
    values = message.get_all(header) or []
    return {addr.lower() for _, addr in getaddresses([str(v) for v in values]) if addr}


def user_position(message: Message, user_addresses: Iterable[str]) -> UserPosition:
    mine = {a.strip().lower() for a in user_addresses if a.strip()}
    if not mine:
        return "unknown"
    if _addresses(message, "To") & mine:
        return "direct"
    if _addresses(message, "Cc") & mine:
        return "cc"
    return "indirect"


def _iso_date(message: Message) -> str | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw)).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _has_list_headers(message: Message) -> bool:
    if message.get("List-Id") or message.get("List-Unsubscribe"):
        return True
    precedence = str(message.get("Precedence") or "").strip().lower()
    return precedence in _BULK_PRECEDENCE


def summarize_message(
    message: Message,
    *,
    user_addresses: Iterable[str] = (),
    user_replied: bool | None = None,
) -> MailSummary:
    plain, html = bodies_from_message(message)
    subject = str(message.get("Subject") or "")
    raw_bodies = " ".join(part for part in (plain, html) if part)
    return MailSummary(
        sender=str(message.get("From") or ""),
        user_position=user_position(message, user_addresses),
        subject=subject,
        date=_iso_date(message),
        list_headers=_has_list_headers(message),
        unsubscribe_link=bool(message.get("List-Unsubscribe"))
        or bool(_UNSUBSCRIBE.search(raw_bodies)),
        attachments=tuple(attachment_names(message)),
        user_replied=user_replied,
        body=clean_email_text(plain, html, subject=subject),
    )
