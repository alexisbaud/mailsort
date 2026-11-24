"""Nettoyage du texte des mails avant l'envoi à Jev, avec du code classique (sans IA).

Étapes, dans l'ordre de la spec :

1. choisir la version du mail : texte brut s'il a du contenu, sinon HTML converti en texte
   (sans styles, scripts ni blocs cachés comme les pré-en-têtes invisibles) ;
2. retirer les caractères invisibles ;
3. retirer la mise en forme : URL (le texte du lien est gardé), barres de tableaux, lignes vides
   en série ;
4. retirer l'historique des réponses ;
5. garder le contenu transféré ;
6. retirer la signature quand elle est repérable ;
7. limiter à 5 000 tokens en gardant le début.

L'ordre compte : certains mails commencent par des milliers de caractères invisibles, et
tronquer avant de nettoyer ne laisserait voir à Jev que ces caractères.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, Tag
from bs4.element import ProcessingInstruction

MAX_TOKENS = 5000
CHARS_PER_TOKEN = 4
"""Approximation prudente pour du texte européen ; le tokeniseur exact de Jev n'est pas connu."""
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN
TRUNCATION_MARK = " […]"

Source = Literal["plain", "html", "empty"]


@dataclass(frozen=True)
class CleanText:
    text: str
    source: Source
    """Version du mail utilisée."""
    forwarded: bool
    """Le mail a été reconnu comme un transfert."""
    truncated: bool


# ==================================================================== étape 1
# Choix de la version, conversion HTML → texte

_DROP_TAGS = frozenset(
    {
        "script", "style", "head", "title", "meta", "link", "noscript", "template",
        "svg", "img", "object", "iframe", "button", "input", "select", "textarea",
    }
)  # fmt: skip
_LINE_TAGS = frozenset(
    {
        "address", "article", "aside", "center", "dd", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form", "header", "li", "main", "nav",
        "section", "tbody", "thead", "tfoot", "tr",
    }
)  # fmt: skip
_PARAGRAPH_TAGS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "hr", "ol", "p", "table", "ul"}
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})
# Marqueurs provisoires : fin de ligne (bloc) et fin de paragraphe. Plusieurs blocs imbriqués
# ou consécutifs ne doivent donner qu'un seul saut de ligne.
_LINE = "\x00"
_PARAGRAPH = "\x01"
_BREAK_RUN = re.compile(r"[ \t]*[\x00\x01][\x00\x01\n \t]*")
_SKIPPED_STRINGS = (Comment, Declaration, Doctype, ProcessingInstruction)
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden"
    r"|(?:max-height|opacity)\s*:\s*0(?:\.0+)?(?:px|em|%)?\s*(?:;|!|$)",
    re.IGNORECASE,
)
_HTML_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
# Sous ce nombre de caractères, une version texte peut n'être qu'un message du type
# « Ouvrez ce mail dans un navigateur » : on la compare alors à la version HTML.
_PLACEHOLDER_MAX_CHARS = 300
_PLACEHOLDER_RATIO = 5


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if _HIDDEN_STYLE.search(str(tag.get("style") or "")):
        return True
    classes = tag.get("class") or []
    return any("preheader" in c.lower() for c in classes)


def _render(node: Tag, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, Tag):
            name = child.name
            if name in _DROP_TAGS or _is_hidden(child):
                continue
            if name == "br":
                out.append("\n")
            elif name == "blockquote":
                inner = _render_fragment(child)
                quoted = "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
                out.append(f"{_PARAGRAPH}{quoted}{_PARAGRAPH}")
            elif name == "pre":
                out.append(f"{_PARAGRAPH}{child.get_text()}{_PARAGRAPH}")
            elif name in _CELL_TAGS:
                out.append(" ")
                _render(child, out)
                out.append(" ")
            elif name in _LINE_TAGS or name in _PARAGRAPH_TAGS:
                mark = _PARAGRAPH if name in _PARAGRAPH_TAGS else _LINE
                out.append(mark)
                _render(child, out)
                out.append(mark)
            else:
                _render(child, out)
        elif isinstance(child, _SKIPPED_STRINGS):
            continue
        elif isinstance(child, NavigableString):
            out.append(_HTML_WHITESPACE.sub(" ", str(child)))


def _collapse_breaks(match: re.Match[str]) -> str:
    run = match.group()
    # Un paragraphe, ou un <br> explicite entre deux blocs (`<div><br></div>`) : ligne vide.
    return "\n\n" if _PARAGRAPH in run or "\n" in run else "\n"


def _render_fragment(node: Tag) -> str:
    out: list[str] = []
    _render(node, out)
    return _tidy_lines(_BREAK_RUN.sub(_collapse_breaks, "".join(out)))


def html_to_text(html: str) -> str:
    """Convertit du HTML en texte : blocs sur des lignes, liens réduits à leur texte, citations
    (`<blockquote>`) préfixées par « > », éléments cachés, styles et scripts retirés."""
    return _render_fragment(BeautifulSoup(html, "html.parser"))


def _has_content(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def choose_version(plain: str | None, html: str | None) -> tuple[Source, str]:
    """Texte brut s'il a du contenu, sinon le HTML converti en texte."""
    plain_text = _tidy_lines(remove_invisible(plain)) if plain else ""
    if _has_content(plain_text):
        if html and len(plain_text) < _PLACEHOLDER_MAX_CHARS:
            html_text = _tidy_lines(remove_invisible(html_to_text(html)))
            if len(html_text) >= _PLACEHOLDER_RATIO * len(plain_text):
                return "html", html_text
        return "plain", plain
    if html:
        return "html", html_to_text(html)
    return "empty", ""


# ==================================================================== étape 2
# Caractères invisibles

_SPACE_LIKE = {ord(ch): " " for ch in "               　"}
# Caractères qui ne s'affichent pas mais ne sont pas de catégorie « Cf » (format).
_INVISIBLE_EXTRA = frozenset("͏ᅟᅠ឴឵ㅤﾠ⠀")
_KEPT_CONTROLS = frozenset("\n\t")


def remove_invisible(text: str) -> str:
    """Retire espaces de largeur nulle, traits d'union conditionnels, caractères de remplissage
    et autres caractères Unicode qui ne s'affichent pas ; normalise les espaces spéciales."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_SPACE_LIKE)
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_CONTROLS
        or (ch not in _INVISIBLE_EXTRA and unicodedata.category(ch) not in ("Cf", "Cc"))
    )


# ==================================================================== étape 3
# Mise en forme

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((?:https?://|mailto:)[^)\s]*\)")
_BRACKETED_URL = re.compile(r"[<\[](?:https?://|mailto:|www\.)[^>\]\s]*[>\]]")
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+")
_EMPTY_PARENS = re.compile(r"\(\s*\)")
_SEPARATOR_LINE = re.compile(r"^[\s|+\-=_*~#.·•─━│┃┄┅┈┉┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]{3,}$")
_TABLE_BARS = re.compile(r"[|│┃║]")
_SPACES = re.compile(r"[ \t]+")


def _tidy_lines(text: str) -> str:
    """Espaces réduits, lignes nettoyées, au plus une ligne vide de suite."""
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def strip_formatting(text: str) -> str:
    """Retire URL (en gardant le texte des liens), barres et séparateurs de tableaux, lignes
    vides en série."""
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _BRACKETED_URL.sub("", text)
    text = _BARE_URL.sub("", text)
    text = text.replace("mailto:", "")
    text = _EMPTY_PARENS.sub("", text)
    lines = []
    for line in text.split("\n"):
        if _SEPARATOR_LINE.match(line):
            continue
        lines.append(_TABLE_BARS.sub(" ", line))
    return _tidy_lines("\n".join(lines))


# ============================================================== étapes 4 à 6
# Historique des réponses, transferts, signature

_REPLY_ATTRIBUTION = re.compile(
    r"^(?:on|le|am|el)\b.{0,250}\b(?:wrote|a [ée]crit|schrieb|escribió)\s*:?$", re.IGNORECASE
)
_ORIGINAL_MESSAGE = re.compile(
    r"^-{2,}\s*(?:original message|message d['’]origine|message original|mensaje original"
    r"|ursprüngliche nachricht)\s*-{2,}$",
    re.IGNORECASE,
)
_FORWARD_MARKER = re.compile(
    r"^(?:-{2,}\s*(?:forwarded message|message transf[ée]r[ée]|mensaje reenviado"
    r"|weitergeleitete nachricht)\s*-{2,}"
    r"|begin forwarded message\s*:|d[ée]but du message r[ée]exp[ée]di[ée]\s*:)$",
    re.IGNORECASE,
)
_HEADER_LINE = re.compile(
    r"^(?:de|from|envoy[ée]|sent|date|[àa]|to|cc|objet|subject)\s?:", re.IGNORECASE
)
_FROM_LINE = re.compile(r"^(?:de|from)\s?:", re.IGNORECASE)
_SUBJECT_KEYS = frozenset({"objet", "subject"})
_FORWARD_SUBJECT = re.compile(r"^\s*(?:fwd?|tr|transf|wg)\s*:", re.IGNORECASE)
_REPLY_SUBJECT = re.compile(r"^\s*(?:re|r[ée]f|aw|sv|antw)\s*:", re.IGNORECASE)
_MOBILE_SIGNATURE = re.compile(
    r"^(?:(?:envoy[ée]|sent)\s+(?:de|depuis|from|via)\b.{0,60}"
    r"(?:iphone|ipad|android|outlook|mail|samsung|galaxy|mobile|appareil)\b.{0,30}"
    r"|(?:get|t[ée]l[ée]charger)\s+outlook\s+(?:for|pour)\s+(?:ios|android))$",
    re.IGNORECASE,
)
_SIGNATURE_DELIMITER = "--"
_SIGNATURE_MAX_LINES = 15


def _header_key(line: str) -> str:
    return line.split(":", 1)[0].strip().lower()


def _header_block_end(lines: list[str], start: int) -> int:
    """Fin d'un bloc d'en-têtes (De :, Date :, À :, Objet :…) qui commence à `start`.

    Une ligne qui n'est pas un en-tête est acceptée si la suivante en est un (valeur coupée sur
    deux lignes, par exemple une longue liste de destinataires)."""
    end = start
    while end < len(lines):
        is_header = bool(_HEADER_LINE.match(lines[end]))
        is_wrapped_value = (
            bool(lines[end]) and end + 1 < len(lines) and bool(_HEADER_LINE.match(lines[end + 1]))
        )
        if not (is_header or is_wrapped_value):
            break
        end += 1
    return end


def _is_header_block(lines: list[str], start: int) -> bool:
    """Un bloc « De : / Envoyé : / À : / Objet : » d'au moins 3 lignes, avec un objet."""
    if not _FROM_LINE.match(lines[start]):
        return False
    end = _header_block_end(lines, start)
    keys = {_header_key(line) for line in lines[start:end] if _HEADER_LINE.match(line)}
    return len(keys) >= 3 and bool(keys & _SUBJECT_KEYS)


def _first_reply_marker(lines: list[str], start: int = 0) -> int | None:
    """Première ligne qui ouvre l'historique d'une réponse."""
    for i in range(start, len(lines)):
        line = lines[i]
        if _REPLY_ATTRIBUTION.match(line) or _ORIGINAL_MESSAGE.match(line):
            return i
        if (
            i + 1 < len(lines)
            and re.match(r"^(?:on|le|am|el)\b", line, re.IGNORECASE)
            and _REPLY_ATTRIBUTION.match(f"{line} {lines[i + 1]}")
        ):
            return i  # attribution coupée sur deux lignes
        if _is_header_block(lines, i):
            return i
    return None


def _forward_start(lines: list[str], subject: str) -> int | None:
    """Début du message transféré, ou None si le mail n'est pas un transfert."""
    marker = next((i for i, line in enumerate(lines) if _FORWARD_MARKER.match(line)), None)
    if marker is None and _FORWARD_SUBJECT.match(subject):
        marker = next((i for i in range(len(lines)) if _is_header_block(lines, i)), None)
    if marker is None:
        return None
    reply = _first_reply_marker(lines)
    if reply is not None and reply < marker:
        return None  # réponse à un transfert : le transfert fait partie de l'historique
    return marker


def _unquote_if_quoted(lines: list[str]) -> list[str]:
    """Retire un niveau de « > » si la plupart des lignes non vides sont citées."""
    filled = [line for line in lines if line]
    if not filled or sum(line.startswith(">") for line in filled) < 0.8 * len(filled):
        return lines
    return [line[1:].lstrip() if line.startswith(">") else line for line in lines]


def _drop_quoted(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith(">")]


def strip_signature(text: str) -> str:
    """Retire les lignes « Envoyé de mon iPhone » et la signature qui suit un « -- » final."""
    lines = [line for line in text.split("\n") if not _MOBILE_SIGNATURE.match(line)]
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == _SIGNATURE_DELIMITER:
            tail = [line for line in lines[i + 1 :] if line]
            if len(tail) <= _SIGNATURE_MAX_LINES:
                lines = lines[:i]
            break
    return _tidy_lines("\n".join(lines))


def strip_reply_history(text: str, *, subject: str = "") -> tuple[str, bool]:
    """Retire l'historique des réponses et les signatures, garde le contenu transféré.

    Renvoie le texte et un booléen qui indique si le mail est un transfert."""
    lines = text.split("\n")

    forward = _forward_start(lines, subject)
    if forward is not None:
        note = strip_signature("\n".join(_drop_quoted(lines[:forward])))
        rest = lines[forward:]
        if _FORWARD_MARKER.match(rest[0]):
            rest = rest[1:]
        rest = _unquote_if_quoted(rest)  # Apple Mail cite le message transféré
        while rest and not rest[0]:
            rest = rest[1:]
        header_end = _header_block_end(rest, 0)
        header, body = rest[:header_end], rest[header_end:]
        cut = _first_reply_marker(body)
        if cut is not None:
            body = body[:cut]
        body_text = strip_signature("\n".join(_drop_quoted(body)))
        parts = [note, "\n".join(header), body_text]
        return _tidy_lines("\n\n".join(p for p in parts if p)), True

    cut = _first_reply_marker(lines)
    if cut is not None:
        lines = lines[:cut]
    if cut is not None or _REPLY_SUBJECT.match(subject):
        lines = _drop_quoted(lines)
    else:
        # Pas une réponse : une citation (« > ») fait partie du contenu.
        lines = [line[1:].lstrip() if line.startswith(">") else line for line in lines]
    return strip_signature("\n".join(lines)), False


# ==================================================================== étape 7


def truncate(text: str, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """Garde le début du texte, coupé sur un blanc, avec une marque de troncature."""
    if len(text) <= max_chars:
        return text, False
    limit = max_chars - len(TRUNCATION_MARK)
    cut = text[:limit]
    blank = max(cut.rfind(" "), cut.rfind("\n"))
    if blank > limit * 0.8:
        cut = cut[:blank]
    return cut.rstrip() + TRUNCATION_MARK, True


# ================================================================== pipeline


def clean_email_text(
    plain: str | None, html: str | None, *, subject: str = "", max_chars: int = MAX_CHARS
) -> CleanText:
    """Nettoie le corps d'un mail dans l'ordre de la spec."""
    source, text = choose_version(plain, html)
    text = remove_invisible(text)
    text = strip_formatting(text)
    text, forwarded = strip_reply_history(text, subject=subject)
    text, truncated = truncate(text, max_chars)
    return CleanText(text=text, source=source, forwarded=forwarded, truncated=truncated)


# ========================================================== lecture des .eml


def load_eml(path: str | Path) -> EmailMessage:
    with open(path, "rb") as f:
        return BytesParser(policy=policy.default).parse(f)  # type: ignore[return-value]


def _decode(part: Message) -> str:
    try:
        return part.get_content()  # type: ignore[attr-defined]
    except (LookupError, UnicodeDecodeError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def bodies_from_message(message: Message) -> tuple[str | None, str | None]:
    """Premières parties texte et HTML d'un mail, pièces jointes exclues."""
    plain = html = None
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and plain is None:
            plain = _decode(part)
        elif content_type == "text/html" and html is None:
            html = _decode(part)
    return plain, html


def attachment_names(message: Message) -> list[str]:
    """Noms des pièces jointes (le contenu n'est jamais lu)."""
    return [
        part.get_filename()  # type: ignore[misc]
        for part in message.walk()
        if not part.is_multipart() and part.get_filename()
    ]
