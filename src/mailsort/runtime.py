"""Ce qui entoure un passage : verrou (un seul passage à la fois), journal et arrêt propre.

- Le verrou est un fichier `<base>.lock` verrouillé par le système (`flock`) : il est libéré
  automatiquement si le programme s'arrête, même brutalement. Un second passage lancé pendant le
  premier s'arrête aussitôt sans rien toucher.
- Le journal (`MAILSORT_LOG_DIR/mailsort.log`) garde le bilan de chaque passage et les erreurs.
  Il ne contient que des identifiants de mails, jamais leur contenu. Il tourne sur 5 fichiers
  de 1 Mo.
- Un arrêt demandé par le système (SIGTERM, par exemple un redémarrage du serveur) est traité
  comme Ctrl+C : le mail en cours est abandonné proprement, la base reste cohérente.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType

LOG_FILE = "mailsort.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5


class AlreadyRunning(Exception):
    """Un autre passage tient le verrou."""


@contextmanager
def run_lock(db_path: Path) -> Iterator[None]:
    """Garantit qu'un seul passage utilise cette base à la fois."""
    path = db_path.with_name(db_path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(
                f"Un passage est déjà en cours (verrou {path}). Celui-ci s'arrête sans rien faire."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log_dir() -> Path:
    return Path(os.environ.get("MAILSORT_LOG_DIR") or "logs")


def setup_logging() -> logging.Logger:
    """Journal du logger « mailsort » dans `MAILSORT_LOG_DIR/mailsort.log` (une seule fois)."""
    logger = logging.getLogger("mailsort")
    directory = log_dir()
    target = (directory / LOG_FILE).resolve()
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            if Path(handler.baseFilename) == target:
                return logger
            logger.removeHandler(handler)  # autre dossier de journal : on le remplace
            handler.close()
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


@contextmanager
def stop_on_sigterm() -> Iterator[None]:
    """Transforme SIGTERM en KeyboardInterrupt le temps du bloc."""

    def handler(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # hors du fil principal (tests) : rien à faire
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
