import csv
from pathlib import Path

import pytest
from fake_gmail import FakeGmailService, gmail_message
from fake_jev import FakeJev
from googleapiclient.errors import HttpError
from httplib2 import Response

from mailsort.config import SECONDARY_TYPE_QUESTION, TYPE_QUESTION, load_config
from mailsort.db import Database
from mailsort.gmail import Gmail, GmailAuthError
from mailsort.jev import NONE_CHOICE, JevAuthError, JevClient
from mailsort.sorter import DryRun

CONFIG = load_config(Path(__file__).resolve().parent.parent / "labels.yaml")

SCRIPTS = {
    "Facture": {
        "choices": {TYPE_QUESTION: {"payments": 0.95, NONE_CHOICE: 0.05}},
        "nouls": {"banking": 0.9},
    },
    "Promo": {
        "choices": {TYPE_QUESTION: {"marketing": 0.97, NONE_CHOICE: 0.03}},
        "nouls": {"deletable": 0.9},
    },
    "Dîner": {
        "choices": {TYPE_QUESTION: {NONE_CHOICE: 0.9, "tickets_bookings": 0.1}},
        "nouls": {"family_friends": 0.92, "to_reply": 0.95},
    },
    "Rien": {"choices": {TYPE_QUESTION: {NONE_CHOICE: 1.0}}, "nouls": {}},
    "Contrat": {
        "choices": {
            TYPE_QUESTION: {"payments": 0.62, "official_documents": 0.33, NONE_CHOICE: 0.05},
            SECONDARY_TYPE_QUESTION: {"official_documents": 0.8, NONE_CHOICE: 0.2},
        },
        "nouls": {"housing": 0.9},
    },
}


def mailbox(**extra):
    messages = {
        "m1": gmail_message("m1", "t1", Subject="Facture"),
        "m2": gmail_message("m2", "t2", Subject="Promo"),
        "m3": gmail_message("m3", "t3", Subject="Dîner"),
        "m4": gmail_message("m4", "t4", Subject="Rien"),
        "m5": gmail_message("m5", "t5", Subject="Contrat"),
    }
    messages.update(extra.pop("messages", {}))
    threads = {
        f"t{i}": {"messages": [{"labelIds": ["INBOX"], "internalDate": "1"}]} for i in range(1, 7)
    }
    threads.update(extra.pop("threads", {}))
    return FakeGmailService(messages=messages, threads=threads, **extra)


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "mailsort.db")
    yield database
    database.close()


def dry_run(service, fake, db, **kwargs):
    client = JevClient("k", transport=fake.transport(), max_requests_per_second=0)
    runner = DryRun(Gmail(service), client, db, CONFIG, **kwargs)
    report = runner.run(runner.list_messages(None, None))
    return runner, report


def test_counts_per_label(db):
    fake = FakeJev(by_subject=SCRIPTS)
    _, report = dry_run(mailbox(), fake, db)
    assert report.total == 5
    assert report.label_counts == {
        "payments": 2,
        "banking": 1,
        "marketing": 1,
        "deletable": 1,
        "family_friends": 1,
        "to_reply": 1,
        "official_documents": 1,
        "housing": 1,
        "to_review": 1,
    }
    assert report.review_only == 1  # « Rien » : aucun repère
    assert report.no_label == 0
    assert report.jev_calls == 6  # 5 mails + un second passage pour « Contrat »
    assert report.input_tokens == 6000
    assert report.cached == 0
    assert report.errors == []


def test_gmail_is_never_modified(db):
    service = mailbox()
    dry_run(service, FakeJev(by_subject=SCRIPTS), db)
    called = {name for name, _ in service.calls}
    assert called <= {"getProfile", "messages.list", "messages.get", "threads.get"}


def test_answers_are_reused_on_second_run(db):
    service = mailbox()
    first = FakeJev(by_subject=SCRIPTS)
    dry_run(service, first, db)
    second = FakeJev(by_subject=SCRIPTS)
    _, report = dry_run(service, second, db)
    assert second.requests == []
    assert report.cached == 5
    assert report.total == 5
    assert report.label_counts["official_documents"] == 1  # second passage repris aussi


def test_decisions_are_not_saved(db):
    dry_run(mailbox(), FakeJev(by_subject=SCRIPTS), db)
    assert db.label_counts() == {}
    assert db.message_ids("done") == []


def test_changed_description_only_reasks_that_question(db, tmp_path):
    import yaml

    dry_run(mailbox(), FakeJev(by_subject=SCRIPTS), db)
    raw = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "labels.yaml").read_text(encoding="utf-8")
    )
    next(lb for lb in raw["labels"] if lb["id"] == "health")["description"] = "Health matters."
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    config = load_config(path)

    fake = FakeJev(by_subject=SCRIPTS)
    client = JevClient("k", transport=fake.transport(), max_requests_per_second=0)
    runner = DryRun(Gmail(mailbox()), client, db, config)
    runner.run(runner.list_messages(None, None))
    assert len(fake.requests) == 5
    assert all(list(r["questions"]) == ["health"] for r in fake.requests)


def test_excluded_messages_are_skipped(db):
    service = mailbox(messages={"m6": gmail_message("m6", "t6", labels=("SENT",), Subject="Moi")})
    fake = FakeJev(by_subject=SCRIPTS)
    _, report = dry_run(service, fake, db)
    assert report.skipped == 1
    assert report.total == 5
    assert all(r["state"]["subject"] != "Moi" for r in fake.requests)


def test_rule_6_uses_thread(db):
    threads = {
        "t3": {
            "messages": [
                {"labelIds": ["INBOX"], "internalDate": "1"},
                {"labelIds": ["SENT"], "internalDate": "2"},
            ]
        }
    }
    fake = FakeJev(by_subject=SCRIPTS)
    _, report = dry_run(mailbox(threads=threads), fake, db)
    assert report.label_counts["to_reply"] == 0
    dinner = next(r for r in fake.requests if r["state"]["subject"] == "Dîner")
    assert dinner["state"]["user_already_replied_in_thread"] is True


def test_errors_are_counted_and_run_continues(db):
    service = mailbox(
        messages={"m6": gmail_message("m6", "t6", Subject="Cassé")},
        errors={"m6": HttpError(Response({"status": 500}), b"boom")},
    )
    _, report = dry_run(service, FakeJev(by_subject=SCRIPTS), db)
    assert report.total == 5
    assert [m for m, _ in report.errors] == ["m6"]


@pytest.mark.parametrize(
    ("service_kwargs", "fake", "error"),
    [
        ({"errors": {"m1": HttpError(Response({"status": 401}), b"")}}, FakeJev(), GmailAuthError),
        ({}, FakeJev(failures=[401]), JevAuthError),
    ],
    ids=["gmail", "jev"],
)
def test_auth_errors_stop_the_run(db, service_kwargs, fake, error):
    with pytest.raises(error):
        dry_run(mailbox(**service_kwargs), fake, db)


def test_uncached_count(db):
    service = mailbox()
    fake = FakeJev(by_subject=SCRIPTS)
    client = JevClient("k", transport=fake.transport(), max_requests_per_second=0)
    runner = DryRun(Gmail(service), client, db, CONFIG)
    messages = runner.list_messages(None, None)
    assert runner.uncached_count(messages) == 5
    runner.run(messages[:2])
    assert runner.uncached_count(messages) == 3


def test_csv(db, tmp_path):
    runner, _ = dry_run(mailbox(), FakeJev(by_subject=SCRIPTS), db)
    path = tmp_path / "out.csv"
    runner.write_csv(path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 5
    contract = next(r for r in rows if r["subject"] == "Contrat")
    assert contract["labels"] == "💳 Payments 📄 Official Documents 🏠 Housing"
    assert contract["type:payments"] == "0.620"
    assert "official_documents=0.80" in contract["secondary"]
    assert contract["housing"] == "0.900"
    nothing = next(r for r in rows if r["subject"] == "Rien")
    assert "aucun repère" in nothing["review_reasons"]
