"""Interface en ligne de commande de mailsort.

Les commandes sont encore vides : chacune sera branchée à l'étape du plan indiquée.
"""

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from mailsort import __version__
from mailsort.clean import (
    CHARS_PER_TOKEN,
    attachment_names,
    bodies_from_message,
    clean_email_text,
    load_eml,
)
from mailsort.config import FAMILIES, ConfigError, load_config

ConfigPath = Annotated[
    Path,
    typer.Option(
        "--config",
        envvar="MAILSORT_LABELS",
        help="Chemin de labels.yaml.",
        show_default=True,
    ),
]

app = typer.Typer(
    help="Tri automatique des e-mails Gmail par Jev.",
    no_args_is_help=True,
    add_completion=False,
)
labels_app = typer.Typer(help="Gestion des labels Gmail.", no_args_is_help=True)
app.add_typer(labels_app, name="labels")


def _not_implemented(step: str) -> None:
    typer.echo(f"Pas encore implémenté ({step}).", err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mailsort {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Affiche la version."
        ),
    ] = False,
) -> None:
    """Tri automatique des e-mails Gmail par Jev."""


@app.command()
def auth() -> None:
    """Connexion à Gmail dans le navigateur ; crée ou remplace token.json (à lancer sur le Mac)."""
    _not_implemented("étape 7")


@app.command()
def run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Ne touche pas Gmail, écrit le résultat en CSV.")
    ] = False,
) -> None:
    """Passage normal : nouveaux mails et labels modifiés."""
    _not_implemented("étapes 8 et 10")


@app.command()
def resort(
    all_: Annotated[
        bool, typer.Option("--all", help="Repose toutes les questions sur tous les mails.")
    ] = False,
) -> None:
    """Retri complet forcé."""
    _not_implemented("étape 10")


@app.command("eval")
def eval_() -> None:
    """Compare le classement aux labels existants."""
    _not_implemented("étape 9")


@app.command()
def explain(message_id: Annotated[str, typer.Argument(help="Identifiant Gmail du mail.")]) -> None:
    """Affiche le texte nettoyé et la probabilité de chaque label pour un mail."""
    _not_implemented("étape 7")


@app.command()
def clean(
    eml: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="Fichier .eml (dans Gmail : ⋮ → « Télécharger le message »).",
        ),
    ],
) -> None:
    """Affiche le texte nettoyé d'un fichier .eml, tel que Jev le recevrait (sans réseau)."""
    message = load_eml(eml)
    plain, html = bodies_from_message(message)
    subject = str(message["subject"] or "")
    result = clean_email_text(plain, html, subject=subject)

    details = [
        f"version {result.source}",
        f"{len(result.text)} caractères (≈ {len(result.text) // CHARS_PER_TOKEN} tokens)",
    ]
    if result.forwarded:
        details.append("transfert")
    if result.truncated:
        details.append("tronqué")
    typer.echo(f"Objet : {subject}")
    typer.echo(" · ".join(details))
    attachments = attachment_names(message)
    if attachments:
        typer.echo(f"Pièces jointes : {', '.join(attachments)}")
    typer.echo("─" * 60)
    typer.echo(result.text)


@app.command()
def corrections() -> None:
    """Liste les corrections manuelles par label."""
    _not_implemented("étape 10")


@labels_app.command("check")
def labels_check(config: ConfigPath = Path("labels.yaml")) -> None:
    """Valide labels.yaml sans rien toucher, et résume son contenu."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    counts = Counter(label.family for label in cfg.labels)
    summary = ", ".join(f"{counts[f]} {f}" for f in FAMILIES if counts[f])
    disabled = [label.id for label in cfg.labels if not label.enabled]
    typer.echo(f"{config} valide : {len(cfg.labels)} labels ({summary}).")
    if disabled:
        typer.echo(f"Désactivés : {', '.join(disabled)}.")


@labels_app.command("delete")
def labels_delete(
    label_id: Annotated[str, typer.Argument(help="Identifiant du label dans labels.yaml.")],
    confirm: Annotated[bool, typer.Option("--confirm", help="Confirme la suppression.")] = False,
) -> None:
    """Supprime de Gmail un label retiré de labels.yaml."""
    _not_implemented("étape 10")
