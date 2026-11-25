from pathlib import Path

import httpx
import pytest
from fake_jev import FakeJev

from mailsort.config import SECONDARY_TYPE_QUESTION, TYPE_QUESTION, load_config
from mailsort.engine import NONE_OPTION
from mailsort.jev import (
    NONE_CHOICE,
    JevAuthError,
    JevClient,
    JevError,
    ask,
    classify,
    cost_usd,
    plain_name,
    stored_fingerprints,
    type_question,
    with_user,
    yes_no_question,
)

CONFIG = load_config(Path(__file__).resolve().parent.parent / "labels.yaml")
STATE = {"from": "x@example.org", "subject": "Facture", "body": "Merci pour votre paiement."}


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def client_for(fake, **kwargs):
    kwargs.setdefault("sleep", Sleeps())
    kwargs.setdefault("max_requests_per_second", 0)
    return JevClient("test-key", transport=fake.transport(), **kwargs)


# ==================================================================== questions


def test_type_question_lists_all_types_and_none():
    question = type_question(CONFIG)
    assert question["type"] == "choice"
    assert list(question["criteria"]) == [t.id for t in CONFIG.types] + [NONE_CHOICE]
    assert question["criteria"]["payments"] == CONFIG.by_id["payments"].description
    assert question["criteria"][NONE_CHOICE] == CONFIG.settings.types.none_description


def test_secondary_question_excludes_winner():
    question = type_question(CONFIG, exclude="payments")
    assert "payments" not in question["criteria"]
    assert NONE_CHOICE in question["criteria"]
    assert question["instructions"] == (
        "This email has already been classified as Payments. Is it also one of the following?"
    )


def test_yes_no_question_uses_description():
    question = yes_no_question(CONFIG.by_id["housing"])
    assert question["type"] == "noul"
    assert CONFIG.by_id["housing"].description in question["instructions"]


@pytest.mark.parametrize(
    ("label_id", "expected"),
    [
        ("payments", "Payments"),
        ("tickets_bookings", "Tickets & Bookings"),
        ("to_reply", "To Reply"),
    ],
)
def test_plain_name(label_id, expected):
    assert plain_name(CONFIG.by_id[label_id]) == expected


def test_stored_fingerprints_depend_on_model():
    a = stored_fingerprints(CONFIG, "jev-1.14.0")
    b = stored_fingerprints(CONFIG, "jev-1.15.0")
    assert set(a) == set(CONFIG.question_fingerprints())
    assert all(a[q] != b[q] for q in a)
    assert stored_fingerprints(CONFIG, "jev-1.14.0") == a


def test_user_line_comes_first_and_changes_fingerprints():
    state = with_user(CONFIG, {"from": "Maelly BAUD <m@example.com>", "body": ""})
    assert list(state) == ["user", "from", "body"]
    assert state["user"] == "Alexis Baud (the owner of this mailbox)"

    anonymous = CONFIG.model_copy(
        update={"settings": CONFIG.settings.model_copy(update={"user": None})}
    )
    assert "user" not in with_user(anonymous, {"from": "x"})
    named = stored_fingerprints(CONFIG, "jev-1.14.0")
    unnamed = stored_fingerprints(anonymous, "jev-1.14.0")
    assert all(named[q] != unnamed[q] for q in named)


# ======================================================================= client


def test_request_shape_and_auth():
    fake = FakeJev(nouls={"housing": 0.9})
    with client_for(fake, model="jev-1.14.0") as client:
        response = client.system_one(STATE, {"housing": yes_no_question(CONFIG.by_id["housing"])})
    assert fake.headers[0]["authorization"] == "Bearer test-key"
    assert fake.requests[0]["model"] == "jev-1.14.0"
    assert fake.requests[0]["state"] == STATE
    assert response.answers["housing"]["noul"] == 0.9
    assert response.input_tokens == 1000
    assert response.model == "jev-1.14.0"


def test_missing_key():
    with pytest.raises(JevAuthError, match=r"\.env"):
        JevClient("")


def test_from_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("JEV_MODEL", "jev-1.13.0")
    with JevClient.from_env() as client:
        assert client.model == "jev-1.13.0"


@pytest.mark.parametrize("status", [429, 529, 500, 503])
def test_retries_then_succeeds(status):
    fake = FakeJev(failures=[status, status])
    sleeps = Sleeps()
    with client_for(fake, sleep=sleeps) as client:
        client.system_one(STATE, {"housing": yes_no_question(CONFIG.by_id["housing"])})
    assert len(fake.requests) == 3
    assert sleeps == [0.5, 1.0]  # délai croissant


def test_retry_after_header_is_followed():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    sleeps = Sleeps()
    client = JevClient(
        "k", transport=httpx.MockTransport(handler), sleep=sleeps, max_requests_per_second=0
    )
    client.system_one(STATE, {})
    assert sleeps == [7.0]


def test_gives_up_after_max_retries():
    fake = FakeJev(failures=[529] * 10)
    with client_for(fake, max_retries=2) as client, pytest.raises(JevError, match="3 tentatives"):
        client.system_one(STATE, {})
    assert len(fake.requests) == 3


def test_network_errors_are_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    client = JevClient(
        "k", transport=httpx.MockTransport(handler), sleep=Sleeps(), max_requests_per_second=0
    )
    client.system_one(STATE, {})
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retried(status):
    fake = FakeJev(failures=[status])
    with client_for(fake) as client, pytest.raises(JevAuthError, match="TYPESAFE_API_KEY"):
        client.system_one(STATE, {})
    assert len(fake.requests) == 1


def test_validation_errors_are_not_retried():
    fake = FakeJev(failures=[422])
    with client_for(fake) as client, pytest.raises(JevError, match="422") as exc_info:
        client.system_one(STATE, {})
    assert exc_info.value.status == 422
    assert len(fake.requests) == 1


def test_unreadable_response():
    client = JevClient(
        "k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="pas du json")),
        max_requests_per_second=0,
    )
    with pytest.raises(JevError, match="illisible"):
        client.system_one(STATE, {})


def test_rate_limit_spaces_requests():
    now = [100.0]
    sleeps = Sleeps()
    fake = FakeJev()
    client = JevClient(
        "k",
        transport=fake.transport(),
        max_requests_per_second=4,
        sleep=sleeps,
        clock=lambda: now[0],
    )
    client.system_one(STATE, {})
    client.system_one(STATE, {})
    assert sleeps == [0.25]


# ================================================================== ask, classify


def test_ask_maps_none_option_and_yes_no():
    fake = FakeJev(
        choices={TYPE_QUESTION: {"payments": 0.7, NONE_CHOICE: 0.3}}, nouls={"banking": 0.85}
    )
    with client_for(fake) as client:
        result = ask(client, CONFIG, STATE, [TYPE_QUESTION, "banking", "housing"])
    assert result.probabilities[TYPE_QUESTION]["payments"] == 0.7
    assert result.probabilities[TYPE_QUESTION][NONE_OPTION] == 0.3
    assert NONE_CHOICE not in result.probabilities[TYPE_QUESTION]
    assert result.probabilities["banking"] == {"yes": 0.85}
    assert result.probabilities["housing"] == {"yes": 0.0}
    assert set(fake.requests[0]["questions"]) == {TYPE_QUESTION, "banking", "housing"}


def test_ask_only_requested_questions():
    fake = FakeJev(nouls={"health": 0.9})
    with client_for(fake) as client:
        result = ask(client, CONFIG, STATE, ["health"])
    assert list(fake.requests[0]["questions"]) == ["health"]
    assert result.probabilities == {"health": {"yes": 0.9}}


def test_ask_nothing_makes_no_call():
    fake = FakeJev()
    with client_for(fake) as client:
        assert ask(client, CONFIG, STATE, []).probabilities == {}
    assert fake.requests == []


def test_ask_rejects_missing_answer():
    def handler(request):
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    client = JevClient("k", transport=httpx.MockTransport(handler), max_requests_per_second=0)
    with pytest.raises(JevError, match="Pas de réponse"):
        ask(client, CONFIG, STATE, ["health"])


def test_ask_rejects_unexpected_options():
    def handler(request):
        answer = {"type": "choice", "choice": "x", "probabilities": {"x": 1.0}}
        return httpx.Response(200, json={"model": "m", "answers": {TYPE_QUESTION: answer}})

    client = JevClient("k", transport=httpx.MockTransport(handler), max_requests_per_second=0)
    with pytest.raises(JevError, match="Options inattendues"):
        ask(client, CONFIG, STATE, [TYPE_QUESTION])


def test_classify_single_call():
    fake = FakeJev(
        choices={TYPE_QUESTION: {"payments": 0.92, NONE_CHOICE: 0.08}},
        nouls={"banking": 0.9},
        tokens=2400,
    )
    with client_for(fake) as client:
        result = classify(client, CONFIG, STATE)
    assert len(fake.requests) == 1
    assert len(fake.requests[0]["questions"]) == 1 + len(CONFIG.yes_no_labels)
    assert result.secondary is None
    assert result.decision.labels == {"payments", "banking"}
    assert result.calls == 1
    assert result.input_tokens == 2400
    assert result.cost_usd == pytest.approx(cost_usd(2400))


def test_classify_with_secondary_pass():
    fake = FakeJev(
        choices={
            TYPE_QUESTION: {"payments": 0.62, "official_documents": 0.33, NONE_CHOICE: 0.05},
            SECONDARY_TYPE_QUESTION: {"official_documents": 0.8, NONE_CHOICE: 0.2},
        },
        tokens=1000,
    )
    with client_for(fake) as client:
        result = classify(client, CONFIG, STATE)
    assert len(fake.requests) == 2
    second = fake.requests[1]["questions"]
    assert list(second) == [SECONDARY_TYPE_QUESTION]
    assert "payments" not in second[SECONDARY_TYPE_QUESTION]["criteria"]
    assert result.secondary.candidate == "official_documents"
    assert result.decision.labels == {"payments", "official_documents"}
    assert result.calls == 2
    assert result.input_tokens == 2000


def test_cost():
    assert cost_usd(1_000_000) == pytest.approx(0.042)
