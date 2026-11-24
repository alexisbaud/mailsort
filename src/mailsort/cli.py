"""Interface en ligne de commande de mailsort.

Les commandes sont encore vides : chacune sera branchée à l'étape du plan indiquée.
"""

import os
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv

from mailsort import __version__, gmail, jev
from mailsort.clean import (
    CHARS_PER_TOKEN,
    attachment_names,
    bodies_from_message,
    clean_email_text,
    load_eml,
)
from mailsort.config import (
    FAMILIES,
    SECONDARY_TYPE_QUESTION,
    TYPE_QUESTION,
    Config,
    ConfigError,
    load_config,
)
from mailsort.db import Database
from mailsort.engine import NONE_OPTION
from mailsort.prepare import MailSummary, summarize, summarize_message
from mailsort.sorter import TOKENS_PER_MAIL_ESTIMATE, DryRun, DryRunReport, default_csv_path

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


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _load_config(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _user_addresses() -> list[str]:
    raw = os.environ.get("MAILSORT_USER_EMAILS", "")
    return [address.strip() for address in raw.split(",") if address.strip()]


EmlPath = Annotated[
    Path,
    typer.Argument(
        exists=True,
        dir_okay=False,
        help="Fichier .eml (dans Gmail : ⋮ → « Télécharger le message »).",
    ),
]


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
    # Les variables déjà définies dans l'environnement ne sont pas écrasées.
    load_dotenv(find_dotenv(usecwd=True))


@app.command()
def auth() -> None:
    """Connexion à Gmail dans le navigateur ; crée ou remplace token.json (à lancer sur le Mac)."""
    secret, token = gmail.client_secret_path(), gmail.token_path()
    typer.echo("Ouverture du navigateur pour autoriser l'accès à Gmail…")
    try:
        gmail.run_auth_flow(secret, token)
        email = gmail.Gmail.connect(token).profile_email()
    except gmail.GmailError as exc:
        _fail(str(exc))
    typer.echo(f"Accès autorisé pour {email}. Jeton écrit dans {token} (droits 600).")
    typer.echo("Sur le VPS : copier ce fichier à côté de .env (scp), sans le commiter.")


def _connect_gmail(*, authorize_if_missing: bool = False) -> gmail.Gmail:
    """Connexion à Gmail. Avec `authorize_if_missing`, ouvre le navigateur pour l'autorisation
    si aucun jeton n'existe encore (usage local, sur le Mac)."""
    token = gmail.token_path()
    try:
        if authorize_if_missing and not token.exists():
            typer.echo("Aucun jeton Gmail : ouverture du navigateur pour autoriser l'accès…")
            gmail.run_auth_flow(gmail.client_secret_path(), token)
        return gmail.Gmail.connect(token)
    except gmail.GmailError as exc:
        _fail(str(exc))
        raise  # pour le typage : _fail lève toujours


@app.command()
def messages(
    query: Annotated[
        str, typer.Option("--query", "-q", help="Recherche Gmail, comme dans la barre de Gmail.")
    ] = gmail.DEFAULT_QUERY,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Nombre de mails.")] = 10,
) -> None:
    """Liste les derniers mails (identifiant, date, expéditeur, objet), en lecture seule."""
    box = _connect_gmail()
    try:
        for message_id, _ in box.list_messages(query, limit):
            headers = box.get_headers(message_id, ["From", "Subject", "Date"])
            sender = str(headers.get("From") or "")[:30]
            subject = str(headers.get("Subject") or "")[:60]
            date = str(headers.get("Date") or "")[:16]
            typer.echo(f"{message_id}  {date:<16}  {sender:<30}  {subject}")
    except gmail.GmailError as exc:
        _fail(str(exc))


CONFIRM_ABOVE = 500
"""Au-delà de ce nombre de mails à envoyer à Jev, le passage à blanc demande confirmation."""


@app.command()
def run(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Passage à blanc : classe sans rien modifier dans Gmail, affiche le bilan "
            "et écrit un CSV.",
        ),
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Nombre de mails, du plus récent au plus ancien.")
    ] = 200,
    all_: Annotated[bool, typer.Option("--all", help="Toute la boîte (ignore --limit).")] = False,
    query: Annotated[
        str, typer.Option("--query", "-q", help="Recherche Gmail pour restreindre les mails.")
    ] = gmail.DEFAULT_QUERY,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Passage de tri : nouveaux mails et labels modifiés."""
    if not dry_run:
        _not_implemented("étape 10 ; utiliser --dry-run pour un passage à blanc")

    cfg = _load_config(config)
    box = _connect_gmail(authorize_if_missing=True)
    db_path = Path(os.environ.get("MAILSORT_DB") or "mailsort.db")
    try:
        client = jev.JevClient.from_env()
    except jev.JevError as exc:
        _fail(str(exc))

    with client, Database.open(db_path) as db:
        try:
            dry = DryRun(box, client, db, cfg, user_addresses=_user_addresses())
            typer.echo("Passage à blanc : rien n'est modifié dans Gmail.")
            typer.echo("Liste des mails…")
            messages = dry.list_messages(query, None if all_ else limit)
            to_ask = dry.uncached_count(messages)
            estimate = jev.cost_usd(to_ask * TOKENS_PER_MAIL_ESTIMATE)
            typer.echo(
                f"{len(messages)} mails à classer, dont {to_ask} à envoyer à Jev "
                f"(coût estimé ≈ {estimate:.2f} $)."
            )
            if to_ask > CONFIRM_ABOVE and not yes:
                typer.confirm("Continuer ?", abort=True)
            with typer.progressbar(length=len(messages), label="Classement") as bar:
                report = dry.run(messages, progress=lambda: bar.update(1))
        except (gmail.GmailError, jev.JevError) as exc:
            _fail(str(exc))

    csv_path = default_csv_path()
    dry.write_csv(csv_path)
    _print_dry_run(cfg, report, csv_path)


def _print_dry_run(cfg: Config, report: DryRunReport, csv_path: Path) -> None:
    total = report.total

    def share(count: int) -> str:
        return f"{100 * count / total:5.1f} %" if total else "    –"

    typer.echo("")
    details = [f"{total} mails classés"]
    if report.skipped:
        details.append(f"{report.skipped} ignorés (envoyés, brouillons)")
    if report.errors:
        details.append(f"{len(report.errors)} en erreur")
    typer.echo(" · ".join(details))
    typer.echo(
        f"Jev : {report.jev_calls} appel(s) · {report.input_tokens} tokens · "
        f"{jev.cost_usd(report.input_tokens):.4f} $ · {report.cached} mail(s) repris "
        "des réponses déjà enregistrées"
    )

    families = [
        ("Types", "type"),
        ("Domaines", "domain"),
        ("Actions", "action"),
        ("Nettoyage", "cleanup"),
        ("Technique", "system"),
    ]
    for title, family in families:
        labels = [lb for lb in cfg.labels if lb.family == family and lb.enabled]
        if not labels:
            continue
        typer.echo(f"\n{title}")
        for label in labels:
            count = report.label_counts[label.id]
            typer.echo(f"  {label.name:<28} {count:>6}  {share(count)}")

    typer.echo("")
    typer.echo(f"  {'Aucun label':<28} {report.no_label:>6}  {share(report.no_label)}")
    typer.echo(
        f"  {'Seulement 🤔 To Review':<28} {report.review_only:>6}  {share(report.review_only)}"
    )
    typer.echo(f"  {'Total trié':<28} {total:>6}")
    typer.echo("Un mail peut avoir plusieurs labels : la somme des lignes dépasse le total.")
    for message_id, error in report.errors[:5]:
        typer.echo(f"Erreur sur {message_id} : {error}", err=True)
    if len(report.errors) > 5:
        typer.echo(f"… et {len(report.errors) - 5} autre(s) erreur(s).", err=True)
    typer.echo(f"\nDétail par mail : {csv_path}")


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
def explain(
    message_id: Annotated[
        str, typer.Argument(help="Identifiant Gmail du mail (voir `mailsort messages`).")
    ],
    text_only: Annotated[
        bool, typer.Option("--text-only", help="N'appelle pas Jev : affiche seulement le texte.")
    ] = False,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Affiche ce que Jev reçoit pour un mail Gmail, puis ses probabilités et les labels décidés.
    Ne modifie rien dans Gmail."""
    cfg = _load_config(config)
    box = _connect_gmail()
    try:
        message = box.get_message(message_id)
        thread = box.thread_info(message.thread_id)
        addresses = [box.profile_email(), *_user_addresses()]
    except gmail.GmailError as exc:
        _fail(str(exc))

    summary = summarize(
        message.headers,
        message.plain,
        message.html,
        message.attachments,
        user_addresses=addresses,
        user_replied=thread.user_replied,
    )
    if gmail.is_excluded(message):
        excluded = sorted(message.label_ids & gmail.EXCLUDED_LABELS)
        typer.echo(f"⚠ Ce mail serait ignoré par le tri ({', '.join(excluded)}).")
    state = summary.to_state()
    for key, value in state.items():
        if key != "body":
            typer.echo(f"{key} : {value}")
    typer.echo(
        f"Dernier message du fil envoyé par toi : {'oui' if thread.last_from_user else 'non'}"
    )
    typer.echo("─" * 60)
    typer.echo(summary.body.text)
    typer.echo("─" * 60)
    if text_only:
        return

    try:
        with jev.JevClient.from_env() as client:
            result = jev.classify(client, cfg, state, user_replied_last=thread.last_from_user)
    except jev.JevError as exc:
        _fail(str(exc))
    _print_classification(cfg, summary, result)


@app.command()
def clean(eml: EmlPath) -> None:
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
def classify(eml: EmlPath, config: ConfigPath = Path("labels.yaml")) -> None:
    """Classe un fichier .eml avec Jev et affiche probabilités et labels (sans toucher Gmail)."""
    cfg = _load_config(config)
    message = load_eml(eml)
    summary = summarize_message(message, user_addresses=_user_addresses())
    try:
        with jev.JevClient.from_env() as client:
            result = jev.classify(client, cfg, summary.to_state())
    except jev.JevError as exc:
        _fail(str(exc))
    _print_classification(cfg, summary, result)


def _print_classification(cfg: Config, summary: MailSummary, result: jev.Classification) -> None:
    by_id = cfg.by_id
    decision = result.decision
    mark = {True: "✔", False: " "}

    typer.echo(f"Objet : {summary.subject}")
    typer.echo(
        f"Texte : version {summary.body.source} · {len(summary.body.text)} caractères"
        + (" · tronqué" if summary.body.truncated else "")
    )
    typer.echo(
        f"Jev ({result.model}) : {result.calls} appel(s) · {result.input_tokens} tokens"
        f" · {result.cost_usd:.6f} $"
    )

    types = cfg.settings.types
    typer.echo(f"\nType (seuil {types.primary_min:.2f})")
    for option, probability in sorted(
        result.probabilities[TYPE_QUESTION].items(), key=lambda item: -item[1]
    ):
        name = "None of the above" if option == NONE_OPTION else by_id[option].name
        typer.echo(f"  {mark[option == decision.primary_type]} {name:<28} {probability:.2f}")

    if result.secondary is None:
        typer.echo("Second passage : non demandé")
    else:
        candidate = result.secondary.candidate
        second = result.probabilities[SECONDARY_TYPE_QUESTION].get(candidate, 0.0)
        typer.echo(
            f"Second passage : {by_id[candidate].name} {second:.2f}"
            f" (seuil {types.secondary_min:.2f}) {mark[decision.secondary_type == candidate]}"
        )

    doubtful = {d.label_id for d in decision.doubts}
    typer.echo("\nQuestions oui/non")
    for label in cfg.yes_no_labels:
        probability = result.probabilities[label.id]["yes"]
        if label.id in decision.labels:
            flag = "✔"
        elif label.id in doubtful:
            flag = "?"
        elif label.id in decision.blocked:
            flag = "⊘"
        else:
            flag = " "
        typer.echo(
            f"  {flag} {label.name:<28} {probability:.2f} (seuil {cfg.threshold_for(label):.2f})"
        )

    names = [by_id[i].name for i in sorted(decision.labels, key=list(by_id).index)]
    typer.echo(f"\nLabels posés : {', '.join(names) if names else 'aucun'}")
    if decision.blocked:
        blocked = ", ".join(by_id[i].name for i in sorted(decision.blocked))
        typer.echo(f"Bloqués par une protection : {blocked}")
    for reason in decision.review_reasons:
        typer.echo(f"🤔 {reason}")


@app.command()
def corrections() -> None:
    """Liste les corrections manuelles par label."""
    _not_implemented("étape 10")


@labels_app.command("check")
def labels_check(config: ConfigPath = Path("labels.yaml")) -> None:
    """Valide labels.yaml sans rien toucher, et résume son contenu."""
    cfg = _load_config(config)
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
