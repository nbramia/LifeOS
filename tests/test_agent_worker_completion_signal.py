"""Unit tests for completion_signal.has_positive_completion_signal (#760).

Deterministic gate that decides whether a claude_code/codex session's
nominal STATUS_COMPLETED outcome is an EARNED completion or an interrupted
mid-work session. See the module docstring for the exact rules.
"""
from __future__ import annotations

import pytest

from api.services.agent_worker.completion_signal import has_positive_completion_signal


pytestmark = pytest.mark.unit


# The field fixture, verbatim: session sess_099c0b8ca254486f ended mid-turn
# with this as its final assistant text, notifications_sent=0, no PR opened.
FIELD_FRAGMENT = "Now update the cancel test to drop the no-longer-needed release:"


def test_field_fixture_is_not_earned():
    """The exact field-observed fragment must NOT earn completion — this is
    the regression case #760 exists to fix."""
    assert has_positive_completion_signal(FIELD_FRAGMENT, notifications_sent=0) is False


def test_notification_sent_earns_completion_regardless_of_text():
    """At least one [NOTIFY] sent is sufficient on its own — even over a
    fragment-shaped final text (the executor already stripped/streamed the
    real content elsewhere)."""
    assert has_positive_completion_signal(FIELD_FRAGMENT, notifications_sent=1) is True
    assert has_positive_completion_signal("", notifications_sent=2) is True


def test_pr_url_in_final_text_earns_completion():
    text = "Opened https://github.com/nbramia/LifeOS/pull/761 with the fix."
    assert has_positive_completion_signal(text, notifications_sent=0) is True


def test_issue_url_in_final_text_earns_completion():
    text = "Filed https://github.com/nbramia/LifeOS/issues/762 for follow-up."
    assert has_positive_completion_signal(text, notifications_sent=0) is True


def test_pr_mention_with_hash_number_earns_completion():
    """A bare #123 only counts alongside merge/PR-ish phrasing (conservative
    per #760 — a URL is the strong signal)."""
    assert has_positive_completion_signal(
        "Opened PR #123 for the cancel-test fix.", notifications_sent=0,
    ) is True
    assert has_positive_completion_signal(
        "Closes #760 after the fix landed.", notifications_sent=0,
    ) is True


def test_bare_hash_number_without_pr_phrasing_does_not_earn_completion():
    """A passing mention of an issue number, with no merge/PR-ish phrasing
    nearby and no other signal, must not be mistaken for "I opened it". This
    fragment also fails the summary check on its own (trailing colon), so
    it's a clean negative on both signals."""
    text = "Still need to look at issue #123:"
    assert has_positive_completion_signal(text, notifications_sent=0) is False


def test_summary_like_final_text_earns_completion():
    text = "Implemented the earned-completion check, added tests, and verified them locally."
    assert has_positive_completion_signal(text, notifications_sent=0) is True


def test_short_final_text_does_not_earn_completion():
    """Below the length floor, even a clean-looking sentence is too thin to
    trust as a real summary."""
    assert has_positive_completion_signal("Done.", notifications_sent=0) is False


def test_empty_final_text_does_not_earn_completion():
    assert has_positive_completion_signal("", notifications_sent=0) is False
    assert has_positive_completion_signal(None, notifications_sent=0) is False
    assert has_positive_completion_signal("   ", notifications_sent=0) is False


@pytest.mark.parametrize("trailing", [
    "Now update the cancel test to drop the release,",
    "Now update the cancel test to drop the release;",
    "Now update the cancel test to drop the release-",
    "Now update the cancel test and then do the",
    "Now update the cancel test and then do that with the",
])
def test_dangling_trailing_punctuation_or_word_does_not_earn_completion(trailing):
    assert has_positive_completion_signal(trailing, notifications_sent=0) is False
