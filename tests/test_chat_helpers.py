"""
Tests for chat helper functions.

Covers follow-up query expansion, including imperative follow-up detection.
"""
import pytest
from dataclasses import dataclass

pytestmark = pytest.mark.unit


@dataclass
class FakeMessage:
    role: str
    content: str


class TestExpandFollowupQuery:
    """Tests for expand_followup_query."""

    def test_no_history_returns_original(self):
        from api.services.chat_helpers import expand_followup_query
        assert expand_followup_query("find my KTN", []) == "find my KTN"

    def test_pronoun_followup_expanded(self):
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "Tell me about the meeting with Dave")]
        result = expand_followup_query("what about it", history)
        assert result != "what about it"
        assert "Dave" in result or "meeting" in result

    def test_imperative_email_followup(self):
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "find my KTN")]
        result = expand_followup_query("look in my email", history)
        assert result != "look in my email"
        assert "KTN" in result or "find my KTN" in result

    def test_imperative_calendar_followup(self):
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "when is the dentist?")]
        result = expand_followup_query("check my calendar", history)
        assert result != "check my calendar"

    def test_imperative_search_drive(self):
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "find the Q4 report")]
        result = expand_followup_query("search drive for it", history)
        assert result != "search drive for it"

    def test_imperative_try_again(self):
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "what did Sarah say?")]
        result = expand_followup_query("try searching again", history)
        assert result != "try searching again"

    def test_standalone_query_not_expanded(self):
        """Non-follow-up queries should not be expanded."""
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "unrelated previous question")]
        # No pronouns, no imperative verbs — should pass through
        assert expand_followup_query("what color is Mars", history) == "what color is Mars"

    def test_long_query_not_expanded(self):
        """Queries with 10+ words skip follow-up detection."""
        from api.services.chat_helpers import expand_followup_query
        history = [FakeMessage("user", "previous question")]
        long_q = "look in my email for the document that Sarah sent about the project"
        assert expand_followup_query(long_q, history) == long_q


class TestImperativeFollowupRegex:
    """Direct tests for the imperative follow-up regex pattern."""

    def test_matches(self):
        from api.services.chat_helpers import _IMPERATIVE_FOLLOWUP_RE
        should_match = [
            "look in my email",
            "check my calendar",
            "search drive for it",
            "find it in my notes",
            "try searching email again",
            "scan my messages",
            "find that in slack",
        ]
        for q in should_match:
            assert _IMPERATIVE_FOLLOWUP_RE.search(q), f"Should match: {q!r}"

    def test_non_matches(self):
        from api.services.chat_helpers import _IMPERATIVE_FOLLOWUP_RE
        should_not_match = [
            "what is the weather",
            "hello",
            "tell me about Dave",
            "who is the president",
            "how do I cook pasta",
        ]
        for q in should_not_match:
            assert not _IMPERATIVE_FOLLOWUP_RE.search(q), f"Should not match: {q!r}"
