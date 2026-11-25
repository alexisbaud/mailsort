"""Gmail face à ses limites (quota, erreurs passagères), couleurs, verrou et journal."""

import logging

import pytest
from fake_gmail import FakeGmailService, gmail_message, http_error

from mailsort.colors import GMAIL_PALETTE, label_color, nearest, text_color
from mailsort.gmail import (
    MAX_ATTEMPTS,
    Gmail,
    GmailAuthError,
    GmailError,
    GmailNotFoundError,
    GmailQuotaError,
    QuotaLimiter,
)
from mailsort.runtime import AlreadyRunning, run_lock, setup_logging


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# =================================================================== quota


def test_limiter_spreads_calls_over_the_minute():
    clock = Clock()
    limiter = QuotaLimiter(600, clock=clock, sleep=clock.sleep)  # 10 unités par seconde
    for _ in range(100):
        limiter.acquire(5)
    # 500 unités : la réserve de départ (20) puis 10 unités par seconde.
    assert clock.now == pytest.approx(48, abs=0.01)
    assert limiter.waited == pytest.approx(48, abs=0.01)


def test_limiter_never_exceeds_the_budget_over_any_minute():
    clock = Clock()
    limiter = QuotaLimiter(6000, clock=clock, sleep=clock.sleep)
    spent: list[tuple[float, int]] = []
    for _ in range(5000):
        limiter.acquire(15)
        spent.append((clock.now, 15))
    for start, _ in spent[::250]:
        window = sum(cost for t, cost in spent if start <= t < start + 60)
        assert window <= 6000 + limiter.capacity


def test_limiter_reads_env(monkeypatch):
    monkeypatch.setenv("GMAIL_QUOTA_UNITS_PER_MINUTE", "1200")
    assert QuotaLimiter.from_env().rate == pytest.approx(20)
    monkeypatch.setenv("GMAIL_QUOTA_UNITS_PER_MINUTE", "n'importe quoi")
    assert QuotaLimiter.from_env().rate == pytest.approx(100)


def test_each_call_pays_its_cost():
    calls: list[float] = []

    class Spy(QuotaLimiter):
        def acquire(self, cost: float) -> None:
            calls.append(cost)

    service = FakeGmailService(messages={"m1": gmail_message()}, threads={"t1": {"messages": []}})
    box = Gmail(service, limiter=Spy())
    box.get_message("m1")
    box.thread_info("t1")
    box.list_labels()
    assert calls == [5, 10, 1]


# ============================================================ nouvelles tentatives


def box_with(flaky, **kwargs):
    service = FakeGmailService(messages={"m1": gmail_message()}, flaky={"messages.get": flaky})
    sleeps: list[float] = []
    limiter = QuotaLimiter(sleep=lambda seconds: None)
    box = Gmail(service, sleep=sleeps.append, jitter=lambda: 0.5, limiter=limiter, **kwargs)
    return box, sleeps


@pytest.mark.parametrize(
    "error",
    [
        http_error(429, "Too Many Requests"),
        http_error(403, "User-rate limit exceeded", reason="userRateLimitExceeded"),
        http_error(
            403,
            "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute "
            "per user' of service 'gmail.googleapis.com'",
        ),
        http_error(500, "Backend Error"),
        http_error(503, "Service unavailable"),
        TimeoutError("timed out"),
        ConnectionResetError(),
    ],
    ids=["429", "403-user-rate", "403-per-minute", "500", "503", "timeout", "reset"],
)
def test_transient_errors_are_retried(error):
    box, sleeps = box_with([error, error])
    assert box.get_message("m1").id == "m1"
    assert box.retries == 2
    assert sleeps == [1.5, 2.5]  # 2^0 + 0,5 puis 2^1 + 0,5


def test_retries_are_logged(caplog):
    logging.getLogger("mailsort").propagate = True
    box, _ = box_with([http_error(503, "Backend Error")])
    with caplog.at_level(logging.WARNING, logger="mailsort"):
        box.get_message("m1")
    assert "Gmail messages.get : 503 : Backend Error ; nouvelle tentative dans 2 s" in caplog.text


def test_backoff_grows_and_is_capped():
    box, sleeps = box_with([http_error(500, "x")] * (MAX_ATTEMPTS - 1))
    box.get_message("m1")
    assert sleeps == [min(2**i + 0.5, 64) for i in range(MAX_ATTEMPTS - 1)]
    assert sum(sleeps) > 120  # de quoi laisser passer une limite par minute


def test_retry_after_is_followed():
    box, sleeps = box_with([http_error(429, "slow down", **{"retry-after": "12"})])
    box.get_message("m1")
    assert sleeps == [12.0]


def test_gives_up_after_max_attempts():
    box, sleeps = box_with([http_error(500, "x")] * 20)
    with pytest.raises(GmailError, match=f"après {MAX_ATTEMPTS} tentatives"):
        box.get_message("m1")
    assert len(sleeps) == MAX_ATTEMPTS - 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(403, "Queries per day exceeded", reason="dailyLimitExceeded"), GmailQuotaError),
        (
            http_error(403, "Insufficient Permission", reason="insufficientPermissions"),
            GmailAuthError,
        ),
        (http_error(401, "Invalid Credentials"), GmailAuthError),
        (http_error(404, "Not Found"), GmailNotFoundError),
        (http_error(400, "Invalid label"), GmailError),
        (http_error(403, "Mail service not enabled"), GmailError),
    ],
    ids=["daily", "scope", "401", "404", "400", "403-other"],
)
def test_permanent_errors_are_not_retried(error, expected):
    box, sleeps = box_with([error])
    with pytest.raises(expected):
        box.get_message("m1")
    assert sleeps == []


def test_rate_limit_empties_the_quota_reserve():
    clock = Clock()
    limiter = QuotaLimiter(600, clock=clock, sleep=clock.sleep)
    service = FakeGmailService(
        messages={"m1": gmail_message()}, flaky={"messages.get": [http_error(429, "slow")]}
    )
    box = Gmail(service, limiter=limiter, sleep=clock.sleep, jitter=lambda: 0.0)
    box.get_message("m1")
    # Après le refus, la réserve est vidée : seule la seconde d'attente l'a un peu remplie.
    assert limiter._tokens == pytest.approx(5)
    assert limiter.capacity == pytest.approx(20)


# ==================================================================== couleurs


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("#16a765", "#16a765"),
        ("#17A765", "#16a765"),
        ("#fcd2e0", "#fbd3e0"),
        ("#b1b1b1", "#c2c2c2"),  # un gris reste gris
        ("#007286", "#2da2bb"),  # un bleu-vert reste bleu-vert
    ],
)
def test_nearest_palette_color(requested, expected):
    assert nearest(requested) == expected


def test_every_label_color_maps_to_the_palette():
    from pathlib import Path

    from mailsort.config import load_config

    config = load_config(Path(__file__).resolve().parent.parent / "labels.yaml")
    for label in config.labels:
        color = label_color(label.color)
        assert color.background in GMAIL_PALETTE
        assert color.text in GMAIL_PALETTE


def test_label_colors_stay_distinct():
    from pathlib import Path

    from mailsort.config import load_config

    config = load_config(Path(__file__).resolve().parent.parent / "labels.yaml")
    colors = [label_color(label.color).background for label in config.labels]
    assert len(set(colors)) == len(colors)


def test_text_color_contrasts():
    assert text_color("#000000") == "#ffffff"
    assert text_color("#1c4587") == "#ffffff"
    assert text_color("#fbe983") == "#434343"
    assert text_color("#ffffff") == "#434343"


# ======================================================================= verrou


def test_second_run_is_refused_while_first_holds_the_lock(tmp_path):
    db = tmp_path / "mailsort.db"
    with run_lock(db), pytest.raises(AlreadyRunning, match="déjà en cours"), run_lock(db):
        pass
    with run_lock(db):  # libéré à la fin du premier
        pass


def test_log_file_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILSORT_LOG_DIR", str(tmp_path / "logs"))
    logger = setup_logging()
    try:
        logger.info("bilan de test")
        for handler in logger.handlers:
            handler.flush()
        text = (tmp_path / "logs" / "mailsort.log").read_text(encoding="utf-8")
        assert "INFO bilan de test" in text
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logging.getLogger("mailsort").propagate = True
