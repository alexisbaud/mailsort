"""Synchronisation des labels de `labels.yaml` avec Gmail (étape 1 d'un passage).

Pour chaque label du fichier, le programme garde dans la base le lien `id` → identifiant du
label Gmail. Ce lien survit aux renommages : changer `name` renomme le label dans Gmail, et les
mails déjà classés le gardent.

- label jamais vu : on reprend un label Gmail qui porte déjà ce nom complet, sinon on le crée,
  avec sa couleur et la visibilité de sa famille ;
- label connu, renommé dans le fichier (ou son parent) : renommé dans Gmail ;
- label connu mais supprimé à la main dans Gmail : recréé, et la base oublie qu'il était posé
  (plus aucun mail ne le porte) ;
- label retiré du fichier : laissé dans Gmail, signalé dans le journal
  (`mailsort labels delete <id> --confirm` le supprime) ;
- label désactivé (`enabled: false`) : gardé et renommé s'il existe, jamais créé.

Les parents (`Types`, `Domains`, `Actions`) sont créés s'ils manquent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mailsort.colors import label_color
from mailsort.config import Config
from mailsort.db import Database
from mailsort.gmail import Gmail, GmailConflictError, GmailError, GmailLabelInfo

log = logging.getLogger("mailsort")

LABEL_LIST_VISIBILITY = {
    "show": "labelShow",
    "show_if_unread": "labelShowIfUnread",
    "hide": "labelHide",
}
MESSAGE_LIST_VISIBILITY = {"show": "show", "hide": "hide"}

PARENT_KEY = "parent:{name}"


class LabelSyncError(GmailError):
    """Un label ne peut pas être créé ou renommé : le passage s'arrête sans rien poser."""


@dataclass
class SyncResult:
    gmail_ids: dict[str, str] = field(default_factory=dict)
    """`id` du label → identifiant du label Gmail, pour tous les labels gérés."""
    created: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    recreated: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    """Labels connus de la base mais retirés du fichier."""
    color_notes: list[str] = field(default_factory=list)


def sync_labels(box: Gmail, db: Database, config: Config, *, dry_run: bool = False) -> SyncResult:
    """Crée, reprend et renomme les labels Gmail pour qu'ils suivent `labels.yaml`.

    Avec `dry_run`, ne modifie rien : renvoie seulement les liens déjà connus et existants.
    """
    result = SyncResult()
    existing = {label.id: label for label in box.list_labels()}
    by_name = {label.name.casefold(): label for label in existing.values()}
    known = db.gmail_labels()

    def find_by_name(name: str) -> GmailLabelInfo | None:
        return by_name.get(name.casefold())

    def remember(key: str, info: GmailLabelInfo) -> None:
        existing[info.id] = info
        by_name[info.name.casefold()] = info
        if not dry_run:
            with db.transaction():
                db.set_gmail_label(key, info.id, info.name)

    # Parents ----------------------------------------------------------------
    parents = sorted({config.settings.parents[label.family] for label in config.labels})
    for parent in parents:
        key = PARENT_KEY.format(name=parent)
        if find_by_name(parent) is not None:
            remember(key, find_by_name(parent))  # type: ignore[arg-type]
            continue
        if dry_run:
            continue
        info = _create(box, parent, None, None, None)
        log.info("Label parent créé : %s", parent)
        result.created.append(parent)
        remember(key, info)

    # Labels du fichier --------------------------------------------------------
    for label in config.labels:
        full = config.full_name(label)
        link = known.get(label.id)
        info = existing.get(link.gmail_id) if link else None

        if link is not None and info is None:
            # Supprimé à la main dans Gmail : plus aucun mail ne le porte.
            if dry_run:
                continue
            with db.transaction():
                db.forget_label(label.id)
                db.delete_gmail_label(label.id)
            log.warning("Label %s supprimé dans Gmail : il sera recréé.", full)
            result.recreated.append(label.id)

        if info is not None:
            if info.name != full:
                if dry_run:
                    result.gmail_ids[label.id] = info.id
                    continue
                other = find_by_name(full)
                if other is not None and other.id != info.id:
                    raise LabelSyncError(
                        f"Impossible de renommer « {info.name} » en « {full} » : un autre label "
                        "Gmail porte déjà ce nom. Le renommer ou le supprimer dans Gmail."
                    )
                _rename(box, info, full)
                log.info("Label renommé : %s → %s", info.name, full)
                result.renamed.append((info.name, full))
                remember(label.id, GmailLabelInfo(info.id, full))
            result.gmail_ids[label.id] = info.id
            continue

        found = find_by_name(full)
        if found is not None:
            if not dry_run:
                remember(label.id, found)
                log.info("Label Gmail existant repris : %s", full)
                result.adopted.append(label.id)
            result.gmail_ids[label.id] = found.id
            continue

        if not label.enabled or dry_run:
            continue
        visibility = config.settings.visibility[label.family]
        color = label_color(label.color)
        if color.adjusted:
            note = (
                f"{full} : couleur {label.color} hors palette Gmail, remplacée par "
                f"{color.background}"
            )
            log.info(note)
            result.color_notes.append(note)
        created = _create(
            box,
            full,
            LABEL_LIST_VISIBILITY[visibility.label_list],
            MESSAGE_LIST_VISIBILITY[visibility.message_list],
            (color.background, color.text),
        )
        log.info("Label créé : %s", full)
        result.created.append(full)
        remember(label.id, created)
        result.gmail_ids[label.id] = created.id

    # Labels retirés du fichier ---------------------------------------------------
    for key, link in known.items():
        if key.startswith("parent:") or key in config.by_id:
            continue
        result.orphans.append(key)
        log.warning(
            "Label « %s » (%s) retiré de labels.yaml mais gardé dans Gmail. "
            "`mailsort labels delete %s --confirm` le supprime.",
            key,
            link.name,
            key,
        )
    return result


def _create(
    box: Gmail,
    name: str,
    label_list: str | None,
    message_list: str | None,
    color: tuple[str, str] | None,
) -> GmailLabelInfo:
    try:
        return box.create_label(
            name,
            label_list=label_list,
            message_list=message_list,
            background=color[0] if color else None,
            text=color[1] if color else None,
        )
    except GmailConflictError as exc:
        raise LabelSyncError(
            f"Impossible de créer « {name} » : un label Gmail porte déjà ce nom ({exc})."
        ) from exc
    except GmailError as exc:
        if color is None or "color" not in str(exc).lower():
            raise
        # Couleur refusée par Gmail (palette changée) : on crée le label sans couleur.
        log.warning("Couleur refusée par Gmail pour %s : label créé sans couleur.", name)
        return box.create_label(name, label_list=label_list, message_list=message_list)


def _rename(box: Gmail, info: GmailLabelInfo, name: str) -> None:
    try:
        box.rename_label(info.id, name)
    except GmailConflictError as exc:
        raise LabelSyncError(
            f"Impossible de renommer « {info.name} » en « {name} » : nom déjà pris ({exc})."
        ) from exc
