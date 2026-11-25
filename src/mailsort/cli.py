"""Interface en ligne de commande de mailsort."""

import logging
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
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
from mailsort.evaluate import DEFAULT_PAIRS, Evaluation, parse_pair
from mailsort.labelsync import PARENT_KEY, LabelSyncError
from mailsort.prepare import MailSummary, summarize, summarize_message
from mailsort.runtime import AlreadyRunning, run_lock, setup_logging, stop_on_sigterm
from mailsort.sorter import (
    TOKENS_PER_MAIL_ESTIMATE,
    DryRun,
    DryRunReport,
    Row,
    Runner,
    RunReport,
    default_csv_path,
    mail_notes,
    note_title,
    notes_summary,
    write_rows_csv,
)

log = logging.getLogger("mailsort")

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


def _fail(message: str, *, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _abort(message: str) -> None:
    """Erreur qui arrête un passage : écrite dans le journal (ligne ERROR) puis affichée."""
    log.error("%s", message)
    _fail(message)


def _db_path() -> Path:
    return Path(os.environ.get("MAILSORT_DB") or "mailsort.db")


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _confirm_cost(to_ask: int, yes: bool) -> None:
    """Au-delà de CONFIRM_ABOVE mails à envoyer à Jev, demande confirmation (si quelqu'un est
    là pour répondre : lancé par le cron, le passage continue)."""
    if to_ask > CONFIRM_ABOVE and not yes and _interactive():
        typer.confirm("Continuer ?", abort=True)


@contextmanager
def _progress(length: int, label: str) -> Iterator[Callable[[], None]]:
    """Barre de progression dans un terminal ; rien quand le passage tourne sans terminal."""
    if not _interactive() or length == 0:
        yield lambda: None
        return
    with typer.progressbar(length=length, label=label, file=sys.stderr) as bar:
        yield lambda: bar.update(1)


def _jev_client() -> jev.JevClient:
    try:
        return jev.JevClient.from_env()
    except jev.JevError as exc:
        _abort(str(exc))
        raise  # pour le typage : _abort lève toujours


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
    si aucun jeton n'existe encore (usage local, sur le Mac, jamais sans terminal)."""
    token = gmail.token_path()
    try:
        if authorize_if_missing and not token.exists() and _interactive():
            typer.echo("Aucun jeton Gmail : ouverture du navigateur pour autoriser l'accès…")
            gmail.run_auth_flow(gmail.client_secret_path(), token)
        elif (
            authorize_if_missing and not token.exists() and not gmail.client_secret_path().exists()
        ):
            gmail.run_auth_flow(gmail.client_secret_path(), token)  # message explicite
        return gmail.Gmail.connect(token)
    except gmail.GmailError as exc:
        _abort(str(exc))
        raise  # pour le typage : _abort lève toujours


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
"""Au-delà de ce nombre de mails à envoyer à Jev, le passage demande confirmation."""
DRY_RUN_DEFAULT_LIMIT = 200


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
        int | None,
        typer.Option(
            "--limit",
            "-n",
            help="Nombre de mails, du plus récent au plus ancien (passage à blanc : 200 par "
            "défaut ; vrai passage : toute la boîte par défaut).",
        ),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Toute la boîte (ignore --limit).")] = False,
    query: Annotated[
        str, typer.Option("--query", "-q", help="Recherche Gmail pour restreindre les mails.")
    ] = gmail.DEFAULT_QUERY,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
    review: Annotated[
        bool,
        typer.Option(
            "--review/--no-review",
            help="Passage à blanc : lister les mails 🤔 To Review avec leurs scores et la raison.",
        ),
    ] = True,
    report_csv: Annotated[
        bool,
        typer.Option(
            "--csv",
            help="Vrai passage : écrire un CSV des mails modifiés (labels ajoutés, retirés).",
        ),
    ] = False,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Passage de tri : nouveaux mails, labels modifiés et corrections manuelles."""
    cfg = _load_config(config)
    if dry_run:
        _dry_run(cfg, query, None if all_ else (limit or DRY_RUN_DEFAULT_LIMIT), yes, review)
        return
    _real_run(cfg, query, None if all_ else limit, yes, force=False, report_csv=report_csv)


def _dry_run(cfg: Config, query: str, limit: int | None, yes: bool, review: bool) -> None:
    box = _connect_gmail(authorize_if_missing=True)
    client = _jev_client()
    db_path = _db_path()
    try:
        with run_lock(db_path), client, Database.open(db_path) as db:
            try:
                dry = DryRun(box, client, db, cfg, user_addresses=_user_addresses())
                typer.echo("Passage à blanc : rien n'est modifié dans Gmail.")
                typer.echo("Liste des mails…")
                messages = dry.list_messages(query, limit)
                to_ask = dry.uncached_count(messages)
                estimate = jev.cost_usd(to_ask * TOKENS_PER_MAIL_ESTIMATE)
                typer.echo(
                    f"{len(messages)} mails à classer, dont {to_ask} à envoyer à Jev "
                    f"(coût estimé ≈ {estimate:.2f} $)."
                )
                _confirm_cost(to_ask, yes)
                with _progress(len(messages), "Classement") as advance:
                    report = dry.run(messages, progress=advance)
            except (gmail.GmailError, jev.JevError) as exc:
                _fail(str(exc))
    except AlreadyRunning as exc:
        _fail(str(exc))

    csv_path = default_csv_path()
    dry.write_csv(csv_path)
    if review:
        _print_review_list(cfg, dry.rows)
    _print_dry_run(cfg, report)
    _print_notes_summary(cfg, dry.rows)
    typer.echo(f"\nDétail par mail : {csv_path}")


def _real_run(
    cfg: Config,
    query: str,
    limit: int | None,
    yes: bool,
    *,
    force: bool,
    report_csv: bool,
) -> None:
    """Vrai passage (ou retri forcé) : verrou, journal, labels posés dans Gmail, bilan."""
    setup_logging()
    mode = "resort" if force else "run"
    db_path = _db_path()
    try:
        with run_lock(db_path), stop_on_sigterm():
            log.info("Début du passage (%s, requête « %s », limite %s)", mode, query, limit)
            box = _connect_gmail(authorize_if_missing=True)
            client = _jev_client()
            with client, Database.open(db_path) as db:
                report = _run_passage(box, client, db, cfg, query, limit, yes, force, report_csv)
    except AlreadyRunning as exc:
        log.warning("%s", exc)
        _fail(str(exc))
    except (typer.Exit, typer.Abort, KeyboardInterrupt):
        raise
    except Exception:
        log.exception("Erreur inattendue : passage arrêté")
        raise
    if report.stopped:
        raise typer.Exit(code=2)


def _run_passage(
    box: gmail.Gmail,
    client: jev.JevClient,
    db: Database,
    cfg: Config,
    query: str,
    limit: int | None,
    yes: bool,
    force: bool,
    report_csv: bool,
) -> RunReport:
    mode = "resort" if force else "run"
    with db.transaction():
        run_id = db.start_run(mode)
    runner = Runner(
        box,
        client,
        db,
        cfg,
        user_addresses=_user_addresses(),
        force=force,
        keep_rows=report_csv,
    )
    try:
        typer.echo("Synchronisation des labels et liste des mails…")
        messages = runner.prepare(query, limit)
        _print_sync(runner.report)
        to_ask = runner.uncached_count(messages, force=force)
        estimate = jev.cost_usd(to_ask * TOKENS_PER_MAIL_ESTIMATE)
        typer.echo(
            f"{len(messages)} mails dans le périmètre, dont {to_ask} à envoyer à Jev "
            f"(coût estimé ≈ {estimate:.2f} $)."
        )
        _confirm_cost(to_ask, yes)
        with _progress(len(messages), "Tri") as advance:
            report = runner.run(messages, progress=advance)
    except typer.Abort:
        with db.transaction():
            db.finish_run(run_id, {}, "annulé avant le tri")
        raise
    except KeyboardInterrupt:
        with db.transaction():
            db.finish_run(run_id, runner.report.as_stats(), "interrompu")
        log.warning("Passage interrompu : le suivant reprendra là où il s'est arrêté.")
        _fail("Passage interrompu : le suivant reprendra là où il s'est arrêté.", code=130)
        raise
    except (gmail.GmailError, jev.JevError) as exc:
        with db.transaction():
            db.finish_run(run_id, runner.report.as_stats(), str(exc))
        _abort(_stop_message(exc))
        raise

    with db.transaction():
        db.finish_run(run_id, report.as_stats(), report.stopped)
    _log_report(cfg, report, box)
    _print_run(cfg, report)
    if report_csv:
        path = Path(f"{mode}-{datetime.now():%Y%m%d-%H%M%S}.csv")
        try:
            write_rows_csv(cfg, runner.rows(), path, changes=True)
            typer.echo(f"\nMails modifiés : {path}")
        except gmail.GmailError as exc:
            typer.echo(f"CSV non écrit : {exc}", err=True)
    return report


def _stop_message(exc: Exception) -> str:
    if isinstance(exc, LabelSyncError):
        return f"Synchronisation des labels impossible : {exc}"
    if isinstance(exc, jev.JevAuthError):
        return f"Accès Jev refusé : {exc}"
    return str(exc)


def _print_sync(report: RunReport) -> None:
    sync = report.sync
    if sync is None:
        return
    for name in sync.created:
        typer.echo(f"  Label créé : {name}")
    for old, new in sync.renamed:
        typer.echo(f"  Label renommé : {old} → {new}")
    for label_id in sync.recreated:
        typer.echo(f"  Label supprimé dans Gmail, recréé : {label_id}")
    for note in sync.color_notes:
        typer.echo(f"  {note}")
    for label_id in sync.orphans:
        typer.echo(
            f"  Label « {label_id} » retiré de labels.yaml mais gardé dans Gmail "
            f"(`mailsort labels delete {label_id} --confirm` pour le supprimer)."
        )


def _log_report(cfg: Config, report: RunReport, box: gmail.Gmail) -> None:
    names = {label.id: label.name for label in cfg.labels}

    def fmt(counter: Counter[str]) -> str:
        return ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(counter.items())) or "aucun"

    log.info(
        "Bilan : %d mails dans le périmètre, %d triés (%d nouveaux), %d ignorés, %d modifiés, "
        "%d corrections manuelles, %d erreurs, %d 🤔 To Review",
        report.listed,
        report.total,
        report.new,
        report.skipped,
        report.changed,
        len(report.corrections),
        len(report.errors),
        report.label_counts.get("to_review", 0),
    )
    log.info("Labels ajoutés : %s", fmt(report.added))
    log.info("Labels retirés : %s", fmt(report.removed))
    log.info("Labels présents : %s", fmt(report.label_counts))
    log.info("Doutes (label non posé) : %s", fmt(report.doubts))
    log.info(
        "Jev : %d appel(s), %d tokens, %.4f $ ; Gmail : %d nouvelle(s) tentative(s), %.0f s "
        "d'attente pour le quota",
        report.jev_calls,
        report.input_tokens,
        jev.cost_usd(report.input_tokens),
        box.retries,
        box.limiter.waited,
    )
    if report.stopped:
        log.error("Passage arrêté avant la fin : %s", report.stopped)


def _print_run(cfg: Config, report: RunReport) -> None:
    total = report.total

    def share(count: int) -> str:
        return f"{100 * count / total:5.1f} %" if total else "    –"

    typer.echo("")
    details = [f"{total} mails triés", f"{report.new} nouveaux", f"{report.changed} modifiés"]
    if report.skipped:
        details.append(f"{report.skipped} ignorés")
    if report.corrections:
        details.append(f"{len(report.corrections)} corrections manuelles")
    if report.errors:
        details.append(f"{len(report.errors)} en erreur")
    typer.echo(" · ".join(details))
    typer.echo(
        f"Jev : {report.jev_calls} appel(s) · {report.input_tokens} tokens · "
        f"{jev.cost_usd(report.input_tokens):.4f} $"
    )
    typer.echo(f"\n  {'Label':<28} {'présents':>8}  {'part':>7}  {'ajoutés':>7}  {'retirés':>7}")
    families = ["type", "domain", "action", "cleanup", "system"]
    for family in families:
        for label in cfg.labels:
            if label.family != family or not label.enabled:
                continue
            count = report.label_counts[label.id]
            typer.echo(
                f"  {label.name:<28} {count:>8}  {share(count)}  "
                f"{report.added[label.id]:>7}  {report.removed[label.id]:>7}"
            )
    typer.echo(f"\n  {'Aucun label':<28} {report.no_label:>8}  {share(report.no_label)}")
    typer.echo(
        f"  {'Seulement 🤔 To Review':<28} {report.review_only:>8}  {share(report.review_only)}"
    )
    if report.doubts:
        names = {label.id: label.name for label in cfg.labels}
        doubts = ", ".join(
            f"{'type incertain' if k == 'type' else names.get(k, k)} {v}"
            for k, v in report.doubts.most_common()
        )
        typer.echo(f"Doutes (label non posé) : {doubts}")
    for message_id, error in report.errors[:5]:
        typer.echo(f"Erreur sur {message_id} : {error}", err=True)
    if len(report.errors) > 5:
        typer.echo(f"… et {len(report.errors) - 5} autre(s) erreur(s).", err=True)
    if report.errors:
        typer.echo("Les mails en erreur seront repris au prochain passage.", err=True)
    if report.stopped:
        typer.echo(f"Passage arrêté avant la fin : {report.stopped}", err=True)


def _print_review_list(cfg: Config, rows: list[Row]) -> None:
    """Un bloc par mail 🤔 To Review (sans repère), avec ses doutes ; les mails aux doutes
    semblables se suivent."""
    rank = {count.key: i for i, count in enumerate(notes_summary(cfg, rows))}
    entries = [(row, mail_notes(cfg, row)) for row in rows if row.in_review]
    if not entries:
        return
    entries.sort(key=lambda e: (sorted(rank[c.key] for c in e[1]), e[0].date))
    order = list(cfg.by_id)
    typer.echo(f"\nMails 🤔 To Review ({len(entries)})")
    for row, notes in entries:
        sender = row.sender if len(row.sender) <= 40 else row.sender[:39] + "…"
        typer.echo(f"\n  {row.message_id}  {row.short_date}  {sender}")
        typer.echo(f"    Objet  : {row.subject or '(sans objet)'}")
        others = [i for i in sorted(row.decision.labels, key=order.index) if i != "to_review"]
        names = ", ".join(cfg.by_id[i].name for i in others) or "aucun autre label"
        typer.echo(f"    Labels : {names}")
        for note in notes:
            typer.echo(f"    • {note.detail}")


def _print_dry_run(cfg: Config, report: DryRunReport) -> None:
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
    if report.stopped:
        typer.echo(f"Passage arrêté avant la fin : {report.stopped}", err=True)


def _print_notes_summary(cfg: Config, rows: list[Row]) -> None:
    summary = notes_summary(cfg, rows)
    if not summary:
        return
    typer.echo("\nDoutes et absence de repère                           mails  dont To Review")
    for count in summary:
        typer.echo(f"  {note_title(cfg, count.key):<51} {count.mails:>5}  {count.in_review:>8}")
    typer.echo(
        "Un doute laisse seulement le label non posé ; seule l'absence de repère envoie en "
        "🤔 To Review."
    )


@app.command()
def resort(
    all_: Annotated[
        bool, typer.Option("--all", help="Repose toutes les questions sur tous les mails.")
    ] = False,
    query: Annotated[
        str, typer.Option("--query", "-q", help="Recherche Gmail pour restreindre les mails.")
    ] = gmail.DEFAULT_QUERY,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Retri complet forcé : repose toutes les questions à Jev, puis met Gmail à jour.
    Les corrections manuelles (verrous) sont respectées."""
    if not all_:
        _fail(
            "Ajouter --all pour confirmer : toutes les questions seront reposées à Jev sur tous "
            "les mails (coût affiché avant de commencer)."
        )
    cfg = _load_config(config)
    _real_run(cfg, query, None, yes, force=True, report_csv=False)


@app.command("eval")
def eval_(
    compare: Annotated[
        list[str] | None,
        typer.Option(
            "--compare",
            "-c",
            help="Paire « Nom du label Gmail=id_du_label » (répétable). Par défaut : "
            "« 💳 Paiements=payments » et « 📦 Réceptions=deliveries ».",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
    show: Annotated[
        int, typer.Option("--show", help="Désaccords affichés par label (avec objet).")
    ] = 15,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Compare le classement de Jev aux labels Gmail existants, sans rien modifier."""
    cfg = _load_config(config)
    try:
        pairs = [parse_pair(text, cfg) for text in compare] if compare else list(DEFAULT_PAIRS)
    except ValueError as exc:
        _fail(str(exc))
    box = _connect_gmail()
    client = _jev_client()
    db_path = _db_path()
    try:
        with run_lock(db_path), client, Database.open(db_path) as db:
            try:
                evaluation = Evaluation(
                    box, client, db, cfg, pairs, user_addresses=_user_addresses()
                )
                messages = evaluation.messages_to_classify()
                to_ask = evaluation.dry.uncached_count(messages)
                if messages:
                    estimate = jev.cost_usd(to_ask * TOKENS_PER_MAIL_ESTIMATE)
                    typer.echo(
                        f"{len(messages)} mails de référence, dont {to_ask} à envoyer à Jev "
                        f"(coût estimé ≈ {estimate:.2f} $)."
                    )
                    _confirm_cost(to_ask, yes)
                with _progress(len(messages), "Classement") as advance:
                    report = evaluation.classify(progress=advance)
                results = evaluation.compare()
                _print_eval(cfg, box, results, show)
            except (gmail.GmailError, jev.JevError) as exc:
                _fail(str(exc))
    except AlreadyRunning as exc:
        _fail(str(exc))
    if report.errors:
        typer.echo(f"{len(report.errors)} mail(s) en erreur, non comptés.", err=True)


def _describe(box: gmail.Gmail, message_id: str) -> str:
    try:
        headers = box.get_headers(message_id, ["From", "Subject"])
    except gmail.GmailError:
        return message_id
    sender = str(headers.get("From") or "")[:30]
    subject = str(headers.get("Subject") or "(sans objet)")[:60]
    return f"{message_id}  {sender:<30}  {subject}"


def _print_eval(cfg: Config, box: gmail.Gmail, results: list, show: int) -> None:
    for result in results:
        name = cfg.by_id[result.label_id].name
        typer.echo(f"\n« {result.gmail_name} » face à {name}")
        if not result.found:
            typer.echo("  Label introuvable dans Gmail : rien à comparer.")
            continue
        typer.echo(
            f"  {result.reference} mails de référence, {result.population} mails comparés · "
            f"accord {100 * result.agreement:.1f} % · rappel {100 * result.recall:.1f} % · "
            f"précision {100 * result.precision:.1f} %"
        )
        for title, ids in (
            (f"Référence sans {name} (oubli)", result.missed),
            (f"{name} sans la référence (ajout)", result.extra),
        ):
            if not ids:
                continue
            typer.echo(f"  {title} : {len(ids)}")
            for message_id in ids[:show]:
                typer.echo(f"    {_describe(box, message_id)}")
            if len(ids) > show:
                typer.echo(f"    … et {len(ids) - show} autre(s)")


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
    typer.echo(
        f"\nType (seuil {types.primary_min:.2f}, ou {types.primary_pair_sum:.2f} pour deux types)"
    )
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
    reasons = list(decision.review_reasons)
    landmark = reasons.pop() if decision.no_landmark else None
    for reason in reasons:
        typer.echo(f"Doute, label non posé : {reason}")
    if landmark is not None:
        typer.echo(f"🤔 To Review : {landmark}")


@app.command()
def corrections(
    show: Annotated[
        int, typer.Option("--show", help="Corrections affichées par label (avec objet).")
    ] = 10,
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Liste les corrections manuelles par label, avec la probabilité donnée par Jev."""
    cfg = _load_config(config)
    db_path = _db_path()
    if not db_path.exists():
        typer.echo("Aucune base : aucun passage n'a encore eu lieu.")
        return
    with Database.open(db_path) as db:
        grouped = db.corrections_by_label()
    if not grouped:
        typer.echo("Aucune correction manuelle enregistrée.")
        return
    box: gmail.Gmail | None = None
    try:
        box = gmail.Gmail.connect(gmail.token_path())
    except gmail.GmailError:
        typer.echo("(Gmail inaccessible : objets des mails non affichés.)", err=True)
    for label_id, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        label = cfg.by_id.get(label_id)
        name = label.name if label else f"{label_id} (retiré du fichier)"
        added = sum(1 for c in items if c.action == "added")
        typer.echo(
            f"\n{name} : {len(items)} correction(s), {added} ajout(s), "
            f"{len(items) - added} retrait(s)"
        )
        for item in items[:show]:
            action = "ajouté " if item.action == "added" else "retiré "
            probability = "–" if item.probability is None else f"{item.probability:.2f}"
            where = _describe(box, item.message_id) if box else item.message_id
            typer.echo(f"  {item.corrected_at[:10]}  {action} Jev {probability:>4}  {where}")
        if len(items) > show:
            typer.echo(f"  … et {len(items) - show} autre(s)")


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
    config: ConfigPath = Path("labels.yaml"),
) -> None:
    """Supprime de Gmail un label retiré de labels.yaml (les mails restent, sans ce label)."""
    cfg = _load_config(config)
    if label_id in cfg.by_id:
        _fail(
            f"« {label_id} » est encore dans labels.yaml : le retirer du fichier d'abord "
            "(ou le désactiver avec enabled: false pour le garder sans l'évaluer)."
        )
    db_path = _db_path()
    with Database.open(db_path) as db:
        link = db.gmail_labels().get(label_id)
        if link is None or label_id.startswith(PARENT_KEY.split("{")[0]):
            _fail(f"Aucun label Gmail connu pour « {label_id} ».")
        if not confirm:
            _fail(
                f"Supprimerait le label Gmail « {link.name} ». Les mails restent, ils perdent "
                "seulement ce label. Relancer avec --confirm pour le faire."
            )
        box = _connect_gmail()
        setup_logging()
        try:
            with run_lock(db_path):
                with suppress(gmail.GmailNotFoundError):  # déjà supprimé dans Gmail
                    box.delete_label(link.gmail_id)
                with db.transaction():
                    db.forget_label(label_id)
                    db.delete_gmail_label(label_id)
        except AlreadyRunning as exc:
            _fail(str(exc))
        except gmail.GmailError as exc:
            _fail(str(exc))
    log.info("Label supprimé de Gmail : %s (%s)", link.name, label_id)
    typer.echo(f"Label « {link.name} » supprimé de Gmail.")
