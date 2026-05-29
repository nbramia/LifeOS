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


# =============================================================================
# Operator agent spawn from chat (Issue #235, AC6)
# =============================================================================


class TestAgentSlashChat:
    """`/agent ...` operator spawn from the web chat orchestrator."""

    class _FakeStore:
        def __init__(self):
            self.msgs = []
        def add_message(self, cid, role, content):
            self.msgs.append((role, content))

    @staticmethod
    def _reassemble(events):
        """Join the 'content' fields from the SSE stream (the assistant message
        is streamed char-by-char)."""
        import json
        out = []
        for ev in events:
            try:
                data = json.loads(ev[len("data: "):].strip())
            except ValueError:
                continue
            if data.get("type") == "content":
                out.append(data["content"])
        return "".join(out)

    @pytest.mark.asyncio
    async def test_agent_slash_spawns_and_confirms(self):
        from unittest.mock import patch
        from api.routes.chat import _handle_agent_slash

        store = self._FakeStore()
        spawn_result = {"ok": True, "routing": "claude", "needs_routing": False,
                        "session_id": "s", "task_id": "op_1"}
        with patch("api.services.agent_worker.operator_spawn.create_operator_session",
                   return_value=spawn_result) as mock_spawn, \
             patch("api.services.agent_worker.session_store.SessionStore"):
            events = [ev async for ev in _handle_agent_slash("/agent claude refactor x", "c1", store)]

        assert mock_spawn.call_args.kwargs.get("explicit_routing") == "claude"
        assert mock_spawn.call_args.args[1] == "refactor x"
        assert "Spawned" in self._reassemble(events)
        assert any(role == "assistant" for role, _ in store.msgs)
        assert events[-1].strip().endswith('{"type": "done"}')

    @pytest.mark.asyncio
    async def test_agent_slash_empty_shows_usage(self):
        from api.routes.chat import _handle_agent_slash
        store = self._FakeStore()
        events = [ev async for ev in _handle_agent_slash("/agent", "c1", store)]
        assert "Usage:" in self._reassemble(events)

    @pytest.mark.asyncio
    async def test_agent_slash_ask_prompts_for_explicit_model(self):
        from unittest.mock import patch
        from api.routes.chat import _handle_agent_slash

        store = self._FakeStore()
        spawn_result = {"ok": True, "routing": "ask", "needs_routing": True,
                        "session_id": "s", "task_id": "op_1"}
        with patch("api.services.agent_worker.operator_spawn.create_operator_session",
                   return_value=spawn_result), \
             patch("api.services.agent_worker.session_store.SessionStore"):
            events = [ev async for ev in _handle_agent_slash("/agent do the thing", "c1", store)]

        body = self._reassemble(events)
        assert "/agent local" in body and "/agent claude" in body
