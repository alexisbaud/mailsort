"""Base SQLite de mailsort.

Elle garde, pour chaque message : les réponses de Jev (probabilités et empreinte de la question),
les labels posés par le programme, l'empreinte des règles utilisée, les verrous et les
corrections manuelles. Elle garde aussi le lien entre chaque label du fichier et son label Gmail.

Confidentialité : **aucun texte de mail** n'y est stocké (ni corps, ni objet, ni expéditeur),
seulement des identifiants Gmail, des probabilités et des labels.

Les méthodes d'écriture ne valident rien elles-mêmes : on les regroupe dans
`with db.transaction():`, qui valide tout ou annule tout.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mailsort.config import SECONDARY_TYPE_QUESTION, TYPE_QUESTION

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE messages (
        id                TEXT PRIMARY KEY,         -- identifiant Gmail du message
        thread_id         TEXT NOT NULL,
        internal_date     INTEGER,                  -- date Gmail en ms, pour trier
        first_seen_at     TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'done', 'error')),
        processed_at      TEXT,
        rules_fingerprint TEXT,                     -- règles utilisées pour la dernière décision
        review_reasons    TEXT NOT NULL DEFAULT '[]',  -- JSON : raisons de 🤔 To Review
        last_error        TEXT
    );
    CREATE INDEX messages_thread ON messages (thread_id);
    CREATE INDEX messages_status ON messages (status);

    -- Réponses de Jev : une ligne par message et par question.
    -- question : '@types', '@types:secondary' ou l'id d'un label oui/non.
    CREATE TABLE answers (
        message_id    TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        question      TEXT NOT NULL,
        fingerprint   TEXT NOT NULL,
        probabilities TEXT NOT NULL,                -- JSON {option: probabilité}
        asked_at      TEXT NOT NULL,
        PRIMARY KEY (message_id, question)
    );
    CREATE INDEX answers_question ON answers (question, fingerprint);

    -- Labels que le programme considère comme posés par lui sur chaque message.
    CREATE TABLE applied_labels (
        message_id TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        label_id   TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        PRIMARY KEY (message_id, label_id)
    );
    CREATE INDEX applied_labels_label ON applied_labels (label_id);

    -- Corrections manuelles devenues verrous : jamais modifiés ensuite par le programme.
    CREATE TABLE locks (
        message_id TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        label_id   TEXT NOT NULL,
        present    INTEGER NOT NULL CHECK (present IN (0, 1)),
        locked_at  TEXT NOT NULL,
        PRIMARY KEY (message_id, label_id)
    );

    -- Historique des corrections, matière pour améliorer les descriptions.
    CREATE TABLE corrections (
        id                   INTEGER PRIMARY KEY,
        message_id           TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        label_id             TEXT NOT NULL,
        action               TEXT NOT NULL CHECK (action IN ('added', 'removed')),
        probability          REAL,                  -- probabilité donnée par Jev pour ce label
        question_fingerprint TEXT,
        corrected_at         TEXT NOT NULL
    );
    CREATE INDEX corrections_label ON corrections (label_id);

    -- Lien entre un label (ou un parent) et son label Gmail, pour suivre les renommages.
    -- key : id du label, ou 'parent:<famille>'.
    CREATE TABLE gmail_labels (
        key        TEXT PRIMARY KEY,
        gmail_id   TEXT NOT NULL,
        name       TEXT NOT NULL,                   -- nom complet actuellement dans Gmail
        created_at TEXT NOT NULL
    );

    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY,
        mode        TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        stats       TEXT,                           -- JSON : compteurs du bilan
        error       TEXT
    );

    -- Valeurs diverses, par exemple le dernier historyId Gmail.
    CREATE TABLE state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
}

Status = Literal["pending", "done", "error"]
CorrectionAction = Literal["added", "removed"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MessageRecord:
    id: str
    thread_id: str
    internal_date: int | None
    first_seen_at: str
    status: Status
    processed_at: str | None
    rules_fingerprint: str | None
    review_reasons: tuple[str, ...]
    last_error: str | None


@dataclass(frozen=True)
class Answer:
    question: str
    fingerprint: str
    probabilities: dict[str, float]
    asked_at: str


@dataclass(frozen=True)
class Correction:
    message_id: str
    label_id: str
    action: CorrectionAction
    probability: float | None
    question_fingerprint: str | None
    corrected_at: str


@dataclass(frozen=True)
class GmailLabel:
    key: str
    gmail_id: str
    name: str


class Database:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    # ------------------------------------------------------------ ouverture

    @classmethod
    def open(cls, path: str | Path) -> Database:
        """Ouvre (ou crée) la base et applique les migrations manquantes."""
        # Les PRAGMA sont sans effet dans une transaction : on les applique en mode autocommit,
        # puis on passe en transactions explicites.
        connection = sqlite3.connect(str(path), autocommit=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.autocommit = False
        db = cls(connection)
        db._migrate()
        return db

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"base en version {version}, plus récente que le programme ({SCHEMA_VERSION})"
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self.transaction():
                for statement in _split_sql(_MIGRATIONS[target]):
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {target}")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Valide toutes les écritures du bloc, ou aucune en cas d'exception."""
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # ------------------------------------------------------------ messages

    def add_message(self, message_id: str, thread_id: str, internal_date: int | None) -> bool:
        """Enregistre un message vu pour la première fois. Renvoie False s'il était connu."""
        cursor = self._conn.execute(
            "INSERT INTO messages (id, thread_id, internal_date, first_seen_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (message_id, thread_id, internal_date, _now()),
        )
        return cursor.rowcount == 1

    def get_message(self, message_id: str) -> MessageRecord | None:
        row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def known_message_ids(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT id FROM messages")}

    def message_ids(self, status: Status | None = None) -> list[str]:
        """Identifiants des messages, du plus récent au plus ancien."""
        query = "SELECT id FROM messages"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY internal_date DESC, id"
        return [row[0] for row in self._conn.execute(query, params)]

    def forget_message(self, message_id: str) -> None:
        """Oublie un message (supprimé de Gmail) et tout ce qui s'y rattache."""
        self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def mark_error(self, message_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE messages SET status = 'error', last_error = ? WHERE id = ?",
            (error, message_id),
        )

    # ------------------------------------------------------------ réponses de Jev

    def save_answer(
        self,
        message_id: str,
        question: str,
        fingerprint: str,
        probabilities: Mapping[str, float],
    ) -> None:
        """Enregistre une réponse de Jev. Une nouvelle réponse au choix de type invalide le
        second passage, qui en dépend."""
        if question == TYPE_QUESTION:
            self._conn.execute(
                "DELETE FROM answers WHERE message_id = ? AND question = ?",
                (message_id, SECONDARY_TYPE_QUESTION),
            )
        self._conn.execute(
            "INSERT INTO answers (message_id, question, fingerprint, probabilities, asked_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (message_id, question) DO UPDATE SET"
            " fingerprint = excluded.fingerprint, probabilities = excluded.probabilities,"
            " asked_at = excluded.asked_at",
            (message_id, question, fingerprint, json.dumps(dict(probabilities)), _now()),
        )

    def answers(self, message_id: str) -> dict[str, Answer]:
        rows = self._conn.execute(
            "SELECT question, fingerprint, probabilities, asked_at FROM answers"
            " WHERE message_id = ?",
            (message_id,),
        )
        return {
            row["question"]: Answer(
                question=row["question"],
                fingerprint=row["fingerprint"],
                probabilities=json.loads(row["probabilities"]),
                asked_at=row["asked_at"],
            )
            for row in rows
        }

    def messages_needing_question(self, question: str, fingerprint: str) -> list[str]:
        """Messages sans réponse à cette question, ou avec une réponse d'une ancienne version."""
        rows = self._conn.execute(
            "SELECT m.id FROM messages m"
            " LEFT JOIN answers a ON a.message_id = m.id AND a.question = ?"
            " WHERE a.fingerprint IS NULL OR a.fingerprint != ?"
            " ORDER BY m.internal_date DESC, m.id",
            (question, fingerprint),
        )
        return [row[0] for row in rows]

    # ------------------------------------------------------------ décisions

    def save_decision(
        self,
        message_id: str,
        labels: Iterable[str],
        *,
        rules_fingerprint: str,
        review_reasons: Iterable[str] = (),
    ) -> None:
        """Enregistre les labels posés par le programme (remplace les précédents)."""
        now = _now()
        labels = set(labels)
        current = self.applied_labels(message_id)
        for label_id in current - labels:
            self._conn.execute(
                "DELETE FROM applied_labels WHERE message_id = ? AND label_id = ?",
                (message_id, label_id),
            )
        self._conn.executemany(
            "INSERT INTO applied_labels (message_id, label_id, applied_at) VALUES (?, ?, ?)",
            [(message_id, label_id, now) for label_id in sorted(labels - current)],
        )
        cursor = self._conn.execute(
            "UPDATE messages SET status = 'done', processed_at = ?, rules_fingerprint = ?,"
            " review_reasons = ?, last_error = NULL WHERE id = ?",
            (now, rules_fingerprint, json.dumps(list(review_reasons)), message_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"message inconnu : {message_id}")

    def applied_labels(self, message_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT label_id FROM applied_labels WHERE message_id = ?", (message_id,)
        )
        return {row[0] for row in rows}

    def messages_with_label(self, label_id: str) -> list[str]:
        """Messages sur lesquels le programme a posé ce label."""
        rows = self._conn.execute(
            "SELECT message_id FROM applied_labels WHERE label_id = ? ORDER BY message_id",
            (label_id,),
        )
        return [row[0] for row in rows]

    def messages_with_stale_rules(self, rules_fingerprint: str) -> list[str]:
        """Messages déjà traités avec d'autres règles que celles d'aujourd'hui."""
        rows = self._conn.execute(
            "SELECT id FROM messages WHERE status = 'done' AND rules_fingerprint != ?"
            " ORDER BY internal_date DESC, id",
            (rules_fingerprint,),
        )
        return [row[0] for row in rows]

    def label_counts(self) -> dict[str, int]:
        """Nombre de messages par label posé par le programme."""
        rows = self._conn.execute(
            "SELECT label_id, COUNT(*) FROM applied_labels GROUP BY label_id ORDER BY label_id"
        )
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------ verrous et corrections

    def set_lock(self, message_id: str, label_id: str, present: bool) -> None:
        self._conn.execute(
            "INSERT INTO locks (message_id, label_id, present, locked_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (message_id, label_id) DO UPDATE SET"
            " present = excluded.present, locked_at = excluded.locked_at",
            (message_id, label_id, int(present), _now()),
        )

    def locks(self, message_id: str) -> dict[str, bool]:
        rows = self._conn.execute(
            "SELECT label_id, present FROM locks WHERE message_id = ?", (message_id,)
        )
        return {row[0]: bool(row[1]) for row in rows}

    def record_correction(
        self,
        message_id: str,
        label_id: str,
        action: CorrectionAction,
        *,
        probability: float | None,
        question_fingerprint: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO corrections (message_id, label_id, action, probability,"
            " question_fingerprint, corrected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, label_id, action, probability, question_fingerprint, _now()),
        )

    def corrections_by_label(self) -> dict[str, list[Correction]]:
        """Corrections groupées par label, les plus récentes d'abord."""
        rows = self._conn.execute(
            "SELECT message_id, label_id, action, probability, question_fingerprint,"
            " corrected_at FROM corrections ORDER BY label_id, corrected_at DESC, id DESC"
        )
        grouped: dict[str, list[Correction]] = {}
        for row in rows:
            grouped.setdefault(row["label_id"], []).append(Correction(**dict(row)))
        return grouped

    # ------------------------------------------------------------ labels Gmail

    def set_gmail_label(self, key: str, gmail_id: str, name: str) -> None:
        self._conn.execute(
            "INSERT INTO gmail_labels (key, gmail_id, name, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET gmail_id = excluded.gmail_id, name = excluded.name",
            (key, gmail_id, name, _now()),
        )

    def gmail_labels(self) -> dict[str, GmailLabel]:
        rows = self._conn.execute("SELECT key, gmail_id, name FROM gmail_labels ORDER BY key")
        return {row["key"]: GmailLabel(**dict(row)) for row in rows}

    def delete_gmail_label(self, key: str) -> None:
        self._conn.execute("DELETE FROM gmail_labels WHERE key = ?", (key,))

    # ------------------------------------------------------------ passages et état

    def start_run(self, mode: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO runs (mode, started_at) VALUES (?, ?)", (mode, _now())
        )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, stats: Mapping[str, object], error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, stats = ?, error = ? WHERE id = ?",
            (_now(), json.dumps(dict(stats), ensure_ascii=False), error, run_id),
        )

    def last_run(self) -> dict[str, object] | None:
        row = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        run = dict(row)
        run["stats"] = json.loads(run["stats"]) if run["stats"] else None
        return run

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _message(row: sqlite3.Row) -> MessageRecord:
    data = dict(row)
    data["review_reasons"] = tuple(json.loads(data["review_reasons"]))
    return MessageRecord(**data)


def _split_sql(script: str) -> list[str]:
    """Découpe un script de migration en instructions (sans `;` dans les chaînes)."""
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
