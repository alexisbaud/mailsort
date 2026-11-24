from email.message import EmailMessage
from pathlib import Path

import pytest

from mailsort.clean import load_eml
from mailsort.prepare import summarize_message, user_position

FIXTURES = Path(__file__).parent / "fixtures"
ME = ["alexis@example.com"]


def summary(name, **kwargs):
    kwargs.setdefault("user_addresses", ME)
    return summarize_message(load_eml(FIXTURES / name), **kwargs)


def message(**headers):
    m = EmailMessage()
    for key, value in headers.items():
        m[key.replace("_", "-")] = value
    m.set_content("Bonjour")
    return m


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"To": "Alexis <ALEXIS@example.com>"}, "direct"),
        ({"To": "team@example.com", "Cc": "alexis@example.com"}, "cc"),
        ({"To": "liste@example.com"}, "indirect"),
        ({"To": "a@example.org, Alexis <alexis@example.com>"}, "direct"),
    ],
)
def test_user_position(headers, expected):
    assert user_position(message(**headers), ME) == expected


def test_user_position_unknown_without_addresses():
    assert user_position(message(To="alexis@example.com"), []) == "unknown"


def test_reply_summary():
    s = summary("reply_with_history.eml", user_replied=True)
    assert s.sender == "Marie Dupont <marie@example.org>"
    assert s.user_position == "direct"
    assert s.subject == "Re: Dîner samedi"
    assert s.date == "2026-11-24T09:30:00+00:00"
    assert not s.list_headers
    assert not s.unsubscribe_link
    assert s.attachments == ()
    assert s.body.text.startswith("Parfait, je réserve")


def test_newsletter_summary():
    s = summary("long_newsletter.eml")
    assert s.list_headers
    assert s.unsubscribe_link
    assert s.body.truncated


def test_unsubscribe_link_found_in_body_without_header():
    m = message(To="alexis@example.com")
    m.set_content("Pour vous désinscrire, cliquez ici.")
    assert summarize_message(m, user_addresses=ME).unsubscribe_link


def test_precedence_bulk_is_a_list_header():
    assert summarize_message(message(Precedence="bulk")).list_headers


def test_forward_summary_lists_attachments():
    assert summary("forward.eml").attachments == ("attestation_loyer.pdf",)


def test_state_content():
    state = summary("forward.eml", user_replied=False).to_state()
    assert list(state) == [
        "from",
        "user_is",
        "subject",
        "date",
        "mailing_list_headers",
        "unsubscribe_link",
        "attachments",
        "user_already_replied_in_thread",
        "body",
    ]
    assert state["user_is"] == "direct recipient (To)"
    assert state["attachments"] == ["attestation_loyer.pdf"]
    assert state["user_already_replied_in_thread"] is False
    assert "Veuillez trouver ci-joint" in state["body"]


def test_state_omits_unknowns():
    state = summarize_message(message()).to_state()
    assert "user_is" not in state
    assert "user_already_replied_in_thread" not in state
    assert "date" not in state


def test_invalid_date_is_ignored():
    assert summarize_message(message(Date="pas une date")).date is None
