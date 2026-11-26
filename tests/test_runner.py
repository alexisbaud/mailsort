"""Vrai passage : labels posés dans Gmail, corrections manuelles, règle 6, reprise après coupure."""

from pathlib import Path

import pytest
import yaml
from fake_gmail import FakeGmailService, gmail_message, http_error
from fake_jev import FakeJev

from mailsort.config import TYPE_QUESTION, load_config
from mailsort.db import Database
from mailsort.gmail import Gmail, GmailAuthError, GmailQuotaError, QuotaLimiter
from mailsort.jev import NONE_CHOICE, JevClient
from mailsort.labelsync import LabelSyncError
from mailsort.sorter import MAX_CONSECUTIVE_ERRORS, Runner

LABELS_YAML = Path(__file__).resolve().parent.parent / "labels.yaml"
CONFIG = load_config(LABELS_YAML)

PAY = "Types/💳 Payments"
MARKETING = "Types/📣 Marketing"
BANKING = "Domains/🏦 Banking & Investments"
FAMILY = "Domains/👨‍👩‍👧 Family & Friends"
REPLY = "Actions/✉️ To Reply"
DELETABLE = "Actions/🗑️ Deletable"
REVIEW = "Actions/🤔 To Review"

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
}


def thread(*labels: str) -> dict:
    return {"messages": [{"labelIds": [lb], "internalDate": str(i)} for i, lb in enumerate(labels)]}


def mailbox(**kwargs) -> FakeGmailService:
    messages = {
        "m1": gmail_message("m1", "t1", Subject="Facture"),
        "m2": gmail_message("m2", "t2", Subject="Promo"),
        "m3": gmail_message("m3", "t3", Subject="Dîner"),
        "m4": gmail_message("m4", "t4", Subject="Rien"),
    }
    messages.update(kwargs.pop("messages", {}))
    threads = {f"t{i}": thread("INBOX") for i in range(1, 10)}
    threads.update(kwargs.pop("threads", {}))
    return FakeGmailService(messages=messages, threads=threads, **kwargs)


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "mailsort.db")
    yield database
    database.close()


def jev_client(fake=None):
    fake = fake or FakeJev(by_subject=SCRIPTS)
    return JevClient(
        "k", transport=fake.transport(), max_requests_per_second=0, sleep=lambda s: None
    )


def sort(service, db, *, fake=None, config=CONFIG, **kwargs):
    fake = fake or FakeJev(by_subject=SCRIPTS)
    client = jev_client(fake)
    runner = Runner(Gmail(service), client, db, config, **kwargs)
    messages = runner.prepare(None, None)
    report = runner.run(messages)
    return runner, report, fake


def config_with(tmp_path, change) -> object:
    raw = yaml.safe_load(LABELS_YAML.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(path)


# ================================================================== premier passage


def test_first_run_creates_labels_and_applies_them(db):
    service = mailbox()
    _, report, _ = sort(service, db)

    names = set(service.user_labels.values())
    assert {"Types", "Domains", "Actions"} <= names
    assert {PAY, MARKETING, BANKING, REPLY, DELETABLE, REVIEW} <= names
    assert len(names) == 3 + len(CONFIG.labels)

    payments = service.label_settings[service.label_id(PAY)]
    assert payments["labelListVisibility"] == "labelShow"
    assert payments["messageListVisibility"] == "show"
    assert payments["color"] == {"backgroundColor": "#16a765", "textColor": "#ffffff"}
    review = service.label_settings[service.label_id(REVIEW)]
    assert review["labelListVisibility"] == "labelShowIfUnread"
    assert review["messageListVisibility"] == "hide"
    assert "#17a765" in " ".join(report.sync.color_notes)

    assert service.names_on("m1") == {PAY, BANKING}
    assert service.names_on("m2") == {MARKETING, DELETABLE}
    assert service.names_on("m3") == {FAMILY, REPLY}
    assert service.names_on("m4") == {REVIEW}
    assert report.total == report.new == report.changed == 4
    assert report.added["payments"] == 1
    assert report.review_only == 1
    assert db.applied_labels("m1") == {"payments", "banking"}
    assert db.get_message("m1").status == "done"
    assert not db.get_message("m1").sync_pending


def test_second_run_changes_nothing_and_does_not_call_jev(db):
    service = mailbox()
    sort(service, db)
    service.calls.clear()
    _, report, fake = sort(service, db)
    assert fake.requests == []
    assert service.count("messages.modify") == 0
    assert service.count("labels.create") == 0
    assert service.count("messages.get") == 0
    assert report.changed == 0
    assert report.corrections == []
    assert report.new == 0


def test_dry_run_answers_are_reused(db):
    from mailsort.sorter import DryRun

    service = mailbox()
    fake = FakeJev(by_subject=SCRIPTS)
    client = JevClient("k", transport=fake.transport(), max_requests_per_second=0)
    dry = DryRun(Gmail(service), client, db, CONFIG)
    dry.run(dry.list_messages(None, None))
    _, report, second = sort(service, db)
    assert second.requests == []
    assert report.new == 4
    assert service.names_on("m1") == {PAY, BANKING}


def test_excluded_messages_are_skipped(db):
    service = mailbox(messages={"m5": gmail_message("m5", "t5", labels=("SENT",), Subject="Moi")})
    _, report, _ = sort(service, db)
    assert report.skipped == 1
    assert service.names_on("m5") == set()


# ============================================================ corrections manuelles


def test_label_removed_by_hand_is_locked_absent(db, tmp_path):
    service = mailbox()
    sort(service, db)
    service.set_labels("m1", PAY)  # Alexis retire 🏦 Banking
    _, report, _ = sort(service, db)
    assert report.corrections == [("m1", "banking", "removed")]
    assert db.locks("m1") == {"banking": False}
    assert service.names_on("m1") == {PAY}
    (correction,) = db.corrections_by_label()["banking"]
    assert correction.probability == pytest.approx(0.9)
    assert correction.question_fingerprint

    # Jamais reposé, même lors d'un retri complet.
    sort(service, db, force=True)
    assert service.names_on("m1") == {PAY}


def test_label_added_by_hand_is_locked_present(db):
    service = mailbox()
    sort(service, db)
    service.set_labels("m1", PAY, BANKING, FAMILY)
    _, report, _ = sort(service, db)
    assert report.corrections == [("m1", "family_friends", "added")]
    assert db.locks("m1") == {"family_friends": True}
    sort(service, db, force=True)
    assert FAMILY in service.names_on("m1")


def test_manual_type_clears_to_review(db):
    service = mailbox()
    sort(service, db)
    assert service.names_on("m4") == {REVIEW}
    service.set_labels("m4", REVIEW, PAY)  # Alexis tranche : c'est un paiement
    _, report, _ = sort(service, db)
    assert service.names_on("m4") == {PAY}
    assert report.removed["to_review"] == 1


def test_to_review_removed_by_hand_never_comes_back(db):
    service = mailbox()
    sort(service, db)
    service.set_labels("m4")
    sort(service, db)
    sort(service, db, force=True)
    assert service.names_on("m4") == set()
    assert db.locks("m4") == {"to_review": False}


def test_label_present_before_first_run_is_a_manual_choice(db):
    service = mailbox()
    sort(service, db)
    new = gmail_message("m5", "t5", Subject="Promo")
    new["labelIds"] = ["INBOX", service.label_id(PAY)]  # posé à la main avant le passage
    service.messages_by_id["m5"] = new
    _, report, _ = sort(service, db)
    # 💳 Payments posé à la main protège de 🗑️ Deletable.
    assert service.names_on("m5") == {PAY, MARKETING}
    assert db.locks("m5") == {"payments": True}


def test_user_labels_are_never_touched(db):
    service = mailbox(labels={"Label_perso": "Perso"})
    service.messages_by_id["m1"]["labelIds"].append("Label_perso")
    sort(service, db)
    sort(service, db)
    assert "Perso" in service.names_on("m1")


# ======================================================================= règle 6


def test_reply_removes_to_reply(db):
    service = mailbox()
    sort(service, db)
    assert REPLY in service.names_on("m3")
    service.threads_by_id["t3"] = thread("INBOX", "SENT")  # Alexis a répondu
    _, report, fake = sort(service, db)
    assert service.names_on("m3") == {FAMILY}
    assert report.removed["to_reply"] == 1
    assert report.corrections == []
    assert fake.requests == []


def test_thread_is_read_only_when_to_reply_is_at_stake_and_user_wrote_in_it(db):
    service = mailbox()
    sort(service, db)
    service.calls.clear()
    sort(service, db)
    # ✉️ To Reply est en jeu sur « Dîner », mais Alexis n'a rien écrit dans ce fil : une seule
    # liste de ses messages envoyés suffit, aucun fil n'est lu.
    assert [k["id"] for n, k in service.calls if n == "threads.get"] == []
    assert sum(1 for n, k in service.calls if n == "messages.list" and k.get("q") == "in:sent") == 1

    service.threads_by_id["t3"] = thread("INBOX", "SENT")
    service.threads_by_id["t1"] = thread("INBOX", "SENT")  # écrit dans le fil, mais pas en jeu
    service.calls.clear()
    sort(service, db)
    assert [k["id"] for n, k in service.calls if n == "threads.get"] == ["t3"]


# ===================================================================== labels


def test_renamed_label_keeps_its_mails(db, tmp_path):
    service = mailbox()
    sort(service, db)
    gmail_id = service.label_id(PAY)

    def rename(raw):
        next(lb for lb in raw["labels"] if lb["id"] == "payments")["name"] = "💳 Paiements"

    config = config_with(tmp_path, rename)
    service.calls.clear()
    _, report, fake = sort(service, db, config=config)
    assert service.user_labels[gmail_id] == "Types/💳 Paiements"
    assert report.sync.renamed == [(PAY, "Types/💳 Paiements")]
    assert service.count("messages.modify") == 0
    assert "Types/💳 Paiements" in service.names_on("m1")
    # Le nom fait partie de la question : seuls les types sont reposés.
    assert {tuple(r["questions"]) for r in fake.requests} == {(TYPE_QUESTION,)}


def test_label_deleted_in_gmail_is_recreated(db):
    service = mailbox()
    sort(service, db)
    service.labels().delete(userId="me", id=service.label_id(PAY)).execute()
    _, report, _ = sort(service, db)
    assert report.sync.recreated == ["payments"]
    assert PAY in service.names_on("m1")
    assert report.corrections == []


def test_existing_label_with_same_name_is_adopted(db):
    service = mailbox(labels={"Label_old": PAY})
    _, report, _ = sort(service, db)
    assert report.sync.adopted == ["payments"]
    assert service.label_id(PAY) == "Label_old"
    assert PAY in service.names_on("m1")


def test_rename_conflict_stops_before_touching_mails(db, tmp_path):
    service = mailbox()
    sort(service, db)
    service.labels().create(userId="me", body={"name": "Types/💳 Paiements"}).execute()

    def rename(raw):
        next(lb for lb in raw["labels"] if lb["id"] == "payments")["name"] = "💳 Paiements"

    service.calls.clear()
    with pytest.raises(LabelSyncError, match="déjà ce nom"):
        sort(service, db, config=config_with(tmp_path, rename))
    assert service.count("messages.modify") == 0


def test_disabled_label_is_kept_and_not_evaluated(db, tmp_path):
    service = mailbox()
    sort(service, db)

    def disable(raw):
        next(lb for lb in raw["labels"] if lb["id"] == "banking")["enabled"] = False

    _, report, _ = sort(service, db, config=config_with(tmp_path, disable))
    assert BANKING in service.names_on("m1")
    assert report.removed["banking"] == 0
    assert report.corrections == []


def test_label_removed_from_file_is_reported(db, tmp_path):
    service = mailbox()
    sort(service, db)

    def drop(raw):
        raw["labels"] = [lb for lb in raw["labels"] if lb["id"] != "health"]

    _, report, _ = sort(service, db, config=config_with(tmp_path, drop))
    assert report.sync.orphans == ["health"]
    assert "Domains/🩺 Health" in service.user_labels.values()


def test_color_refused_by_gmail_creates_label_without_color(db, monkeypatch):
    import fake_gmail

    original = fake_gmail._Labels.create

    def create(self, **kwargs):
        if "color" in kwargs["body"]:
            return fake_gmail.Request(http_error(400, "Label color is not on the allowed palette"))
        return original(self, **kwargs)

    monkeypatch.setattr(fake_gmail._Labels, "create", create)
    service = mailbox()
    sort(service, db)
    assert PAY in service.user_labels.values()
    assert "color" not in service.label_settings[service.label_id(PAY)]


# ============================================================== robustesse


def test_interrupted_write_is_finished_without_false_correction(db):
    service = mailbox()
    service.modify_errors["m1"] = http_error(500, "backend error")
    _, report, _ = sort(service, db)
    assert [m for m, _ in report.errors] == ["m1"]
    assert db.get_message("m1").sync_pending
    assert service.names_on("m1") == set()

    del service.modify_errors["m1"]
    _, report, _ = sort(service, db)
    assert report.corrections == []
    assert service.names_on("m1") == {PAY, BANKING}
    assert not db.get_message("m1").sync_pending


def test_rate_limits_are_retried(db):
    service = mailbox(
        flaky={
            "messages.get": [
                http_error(429, "Too many requests", **{"retry-after": "3"}),
                http_error(
                    403,
                    "Quota exceeded for quota metric 'Queries' and limit 'Queries "
                    "per minute per user'",
                    reason="rateLimitExceeded",
                ),
            ],
            "messages.modify": [http_error(503, "Backend Error"), ConnectionResetError()],
        }
    )
    sleeps: list[float] = []
    client = jev_client()
    limiter = QuotaLimiter(sleep=lambda seconds: None)
    box = Gmail(service, sleep=sleeps.append, jitter=lambda: 0.0, limiter=limiter)
    runner = Runner(box, client, db, CONFIG)
    report = runner.run(runner.prepare(None, None))
    assert report.errors == []
    assert service.names_on("m1") == {PAY, BANKING}
    assert box.retries == 4
    assert sleeps[0] == 3.0  # Retry-After suivi


def test_daily_quota_stops_the_run_cleanly(db):
    service = mailbox(
        flaky={
            "messages.get": [
                http_error(403, "Quota exceeded: Queries per day", reason="dailyLimitExceeded")
            ]
        }
    )
    _, report, _ = sort(service, db)
    assert report.stopped and "journalier" in report.stopped
    assert report.total == 0


def test_too_many_errors_in_a_row_stop_the_run(db):
    messages = {f"x{i}": gmail_message(f"x{i}", f"t{i}", Subject="Facture") for i in range(8)}
    service = FakeGmailService(
        messages=messages,
        threads={f"t{i}": thread("INBOX") for i in range(8)},
        errors={f"x{i}": http_error(500, "boom") for i in range(8)},
    )
    _, report, _ = sort(service, db)
    assert len(report.errors) == MAX_CONSECUTIVE_ERRORS
    assert report.stopped
    assert db.get_message("x0").status == "error"


def test_jev_outage_stops_the_run_and_resumes_next_time(db):
    service = mailbox()
    _, report, _ = sort(service, db, fake=FakeJev(failures=[503] * 100))
    assert len(report.errors) == 4
    assert "indisponible après 6 tentatives" in report.errors[0][1]
    assert report.total == 0
    _, report, _ = sort(service, db)
    assert report.total == 4
    assert service.names_on("m1") == {PAY, BANKING}


def test_auth_error_stops_the_run(db):
    service = mailbox(flaky={"labels.list": [http_error(401, "Invalid Credentials")]})
    with pytest.raises(GmailAuthError):
        sort(service, db)


def test_deleted_message_is_skipped(db):
    service = mailbox()
    service.messages_by_id["m9"] = gmail_message("m9", "t9", Subject="Facture")
    client = jev_client()
    runner = Runner(Gmail(service), client, db, CONFIG)
    messages = runner.prepare(None, None)
    del service.messages_by_id["m9"]  # supprimé entre la liste et la lecture
    report = runner.run(messages)
    assert report.skipped == 1
    assert report.errors == []


def test_quota_error_type_is_exported():
    assert issubclass(GmailQuotaError, Exception)
