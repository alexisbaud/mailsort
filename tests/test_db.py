import sqlite3

import pytest

from mailsort.config import SECONDARY_TYPE_QUESTION, TYPE_QUESTION
from mailsort.db import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "mailsort.db")
    yield database
    database.close()


def add(db, message_id="m1", thread_id="t1", internal_date=1_000):
    with db.transaction():
        return db.add_message(message_id, thread_id, internal_date)


# ------------------------------------------------------------------ ouverture


def test_schema_created(db):
    assert db.schema_version == SCHEMA_VERSION


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "mailsort.db"
    with Database.open(path) as first:
        add(first)
    with Database.open(path) as second:
        assert second.schema_version == SCHEMA_VERSION
        assert second.known_message_ids() == {"m1"}


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "mailsort.db"
    Database.open(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="plus récente"):
        Database.open(path)


def test_wal_mode(tmp_path):
    with Database.open(tmp_path / "x.db") as database:
        mode = database._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        db.save_answer("unknown", "housing", "fp", {"yes": 0.5})


def test_no_mail_text_is_stored(db):
    """Garantie de confidentialité : aucune colonne ne peut contenir le texte d'un mail."""
    forbidden = {"body", "text", "content", "subject", "snippet", "sender", "from", "html"}
    tables = [
        row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    for table in tables:
        columns = {row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")}
        assert not columns & forbidden, (table, columns & forbidden)


# ---------------------------------------------------------------- transactions


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(ValueError), db.transaction():
        db.add_message("m1", "t1", 1)
        raise ValueError("boom")
    assert db.known_message_ids() == set()


def test_uncommitted_writes_are_lost_on_close(tmp_path):
    path = tmp_path / "mailsort.db"
    database = Database.open(path)
    database.add_message("m1", "t1", 1)
    database.close()
    with Database.open(path) as reopened:
        assert reopened.known_message_ids() == set()


# ------------------------------------------------------------------- messages


def test_add_message_is_idempotent(db):
    assert add(db) is True
    first = db.get_message("m1")
    assert add(db, thread_id="other") is False
    assert db.get_message("m1") == first
    assert first.status == "pending"
    assert first.thread_id == "t1"


def test_get_unknown_message(db):
    assert db.get_message("nope") is None


def test_message_ids_sorted_newest_first_and_filtered(db):
    add(db, "old", internal_date=1)
    add(db, "new", internal_date=3)
    add(db, "mid", internal_date=2)
    assert db.message_ids() == ["new", "mid", "old"]
    with db.transaction():
        db.mark_error("mid", "Jev indisponible")
    assert db.message_ids("error") == ["mid"]
    assert db.message_ids("pending") == ["new", "old"]
    assert db.get_message("mid").last_error == "Jev indisponible"


def test_forget_message_cascades(db):
    add(db)
    with db.transaction():
        db.save_answer("m1", "housing", "fp", {"yes": 0.9})
        db.save_decision("m1", {"housing"}, rules_fingerprint="r")
        db.set_lock("m1", "work", True)
        db.record_correction("m1", "work", "added", probability=0.2, question_fingerprint="fp")
    with db.transaction():
        db.forget_message("m1")
    assert db.answers("m1") == {}
    assert db.applied_labels("m1") == set()
    assert db.locks("m1") == {}
    assert db.corrections_by_label() == {}


# -------------------------------------------------------------------- réponses


def test_save_and_replace_answer(db):
    add(db)
    with db.transaction():
        db.save_answer("m1", "housing", "fp1", {"yes": 0.4})
    with db.transaction():
        db.save_answer("m1", "housing", "fp2", {"yes": 0.9})
    answer = db.answers("m1")["housing"]
    assert answer.fingerprint == "fp2"
    assert answer.probabilities == {"yes": 0.9}


def test_new_type_answer_invalidates_secondary(db):
    add(db)
    with db.transaction():
        db.save_answer("m1", TYPE_QUESTION, "t1", {"payments": 0.62, "@none": 0.05})
        db.save_answer("m1", SECONDARY_TYPE_QUESTION, "t1", {"official_documents": 0.8})
    assert set(db.answers("m1")) == {TYPE_QUESTION, SECONDARY_TYPE_QUESTION}
    with db.transaction():
        db.save_answer("m1", TYPE_QUESTION, "t2", {"payments": 0.9, "@none": 0.1})
    assert set(db.answers("m1")) == {TYPE_QUESTION}


def test_messages_needing_question(db):
    for message_id, date in [("missing", 3), ("stale", 2), ("fresh", 1)]:
        add(db, message_id, internal_date=date)
    with db.transaction():
        db.save_answer("stale", "housing", "old", {"yes": 0.1})
        db.save_answer("fresh", "housing", "new", {"yes": 0.1})
        db.save_answer("missing", "work", "new", {"yes": 0.1})
    assert db.messages_needing_question("housing", "new") == ["missing", "stale"]


# ------------------------------------------------------------------- décisions


def test_save_decision(db):
    add(db)
    with db.transaction():
        db.save_decision(
            "m1", {"payments", "to_review"}, rules_fingerprint="r1", review_reasons=["doute"]
        )
    record = db.get_message("m1")
    assert record.status == "done"
    assert record.rules_fingerprint == "r1"
    assert record.review_reasons == ("doute",)
    assert record.processed_at is not None
    assert db.applied_labels("m1") == {"payments", "to_review"}


def test_save_decision_replaces_labels_and_clears_error(db):
    add(db)
    with db.transaction():
        db.mark_error("m1", "boom")
        db.save_decision("m1", {"payments", "housing"}, rules_fingerprint="r1")
    with db.transaction():
        db.save_decision("m1", {"payments", "work"}, rules_fingerprint="r2")
    assert db.applied_labels("m1") == {"payments", "work"}
    record = db.get_message("m1")
    assert record.last_error is None
    assert record.review_reasons == ()


def test_save_decision_unknown_message(db):
    with pytest.raises(KeyError), db.transaction():
        db.save_decision("nope", set(), rules_fingerprint="r")


def test_messages_with_label_and_counts(db):
    for message_id in ("a", "b", "c"):
        add(db, message_id)
    with db.transaction():
        db.save_decision("a", {"to_reply", "work"}, rules_fingerprint="r")
        db.save_decision("b", {"to_reply"}, rules_fingerprint="r")
        db.save_decision("c", {"marketing"}, rules_fingerprint="r")
    assert db.messages_with_label("to_reply") == ["a", "b"]
    assert db.label_counts() == {"marketing": 1, "to_reply": 2, "work": 1}


def test_messages_with_stale_rules(db):
    for message_id in ("old", "current", "pending"):
        add(db, message_id)
    with db.transaction():
        db.save_decision("old", set(), rules_fingerprint="r1")
        db.save_decision("current", set(), rules_fingerprint="r2")
    assert db.messages_with_stale_rules("r2") == ["old"]


# --------------------------------------------------------- verrous, corrections


def test_locks(db):
    add(db)
    with db.transaction():
        db.set_lock("m1", "housing", False)
        db.set_lock("m1", "work", True)
    with db.transaction():
        db.set_lock("m1", "housing", True)
    assert db.locks("m1") == {"housing": True, "work": True}


def test_corrections_grouped_by_label(db):
    add(db, "a")
    add(db, "b")
    with db.transaction():
        db.record_correction("a", "work", "added", probability=0.3, question_fingerprint="w1")
        db.record_correction("b", "work", "removed", probability=0.85, question_fingerprint="w1")
        db.record_correction(
            "a", "payments", "removed", probability=None, question_fingerprint=None
        )
    grouped = db.corrections_by_label()
    assert list(grouped) == ["payments", "work"]
    assert [c.message_id for c in grouped["work"]] == ["b", "a"]  # plus récente d'abord
    assert grouped["work"][0].action == "removed"
    assert grouped["work"][0].probability == 0.85
    assert grouped["payments"][0].probability is None


# --------------------------------------------------------------- labels Gmail


def test_gmail_labels(db):
    with db.transaction():
        db.set_gmail_label("payments", "Label_1", "Types/💳 Payments")
        db.set_gmail_label("parent:type", "Label_0", "Types")
    with db.transaction():
        db.set_gmail_label("payments", "Label_1", "Types/💳 Paid")
    labels = db.gmail_labels()
    assert labels["payments"].name == "Types/💳 Paid"
    assert labels["parent:type"].gmail_id == "Label_0"
    with db.transaction():
        db.delete_gmail_label("payments")
    assert set(db.gmail_labels()) == {"parent:type"}


# ------------------------------------------------------------ passages, état


def test_runs(db):
    assert db.last_run() is None
    with db.transaction():
        run_id = db.start_run("dry-run")
    with db.transaction():
        db.finish_run(run_id, {"processed": 12, "labels": {"payments": 3}})
    run = db.last_run()
    assert run["mode"] == "dry-run"
    assert run["stats"] == {"processed": 12, "labels": {"payments": 3}}
    assert run["finished_at"] is not None
    assert run["error"] is None


def test_state(db):
    assert db.get_state("history_id") is None
    assert db.get_state("history_id", "0") == "0"
    with db.transaction():
        db.set_state("history_id", "123")
        db.set_state("history_id", "456")
    assert db.get_state("history_id") == "456"
