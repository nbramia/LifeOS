"""
Tests for the phantom-write self-correction in run_agent_loop.

Observed failure (fitness bot on Haiku): after a few "Logged …" confirmations
accumulate in the conversation history, the model starts replying "Logged …"
WITHOUT calling manage_workouts — the set is confirmed to the user but never
written (tool_rounds=0). The loop must catch a write-claim reply made with zero
tool calls and nudge the model to actually call the tool.
"""
import pytest
from unittest.mock import patch

from api.services.agent_loop import (
    PHANTOM_WRITE_NUDGE,
    _claims_write_without_tools,
)

pytestmark = pytest.mark.unit


class TestWriteClaimPattern:
    def test_matches_logged_confirmation(self):
        assert _claims_write_without_tools("Logged 6/20: Pec Fly Machine 3×12 @100 lb.")

    def test_matches_updated_confirmation(self):
        assert _claims_write_without_tools("Updated — 2026-06-20: Bench Press 8 @145 lb")

    def test_matches_case_insensitive_with_leading_space(self):
        assert _claims_write_without_tools("  logged body weight: 178.4 lb.")

    def test_ignores_ordinary_answers(self):
        assert not _claims_write_without_tools("Your last squat session was 6/13.")
        assert not _claims_write_without_tools("You logged 3 sessions last week.")
        assert not _claims_write_without_tools("No sessions logged.")


class _PhantomThenToolClient:
    """First call replies 'Logged …' with NO tool call; after the nudge, calls
    manage_workouts and confirms for real. Captures each call's messages."""

    def __init__(self):
        self.calls = []

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        self.calls.append(list(messages))
        n = len(self.calls)
        if n == 1:
            yield {"type": "text", "content": "Logged 6/20: Pec Fly Machine 3×12 @100 lb."}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}
        elif n == 2:
            yield {"type": "tool_calls", "calls": [{
                "id": "call_1",
                "function": {
                    "name": "manage_workouts",
                    "arguments": '{"action": "log", "sets": [{"exercise": "pec fly machine", "reps": 12, "weight": 100, "count": 3}]}',
                },
            }]}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
        else:
            yield {"type": "text", "content": "Logged 6/20: Pec Fly Machine 3×12 @100 lb."}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}


class _AlwaysPhantomClient:
    """Claims a write with no tool call every time — the nudge must fire only
    once (no infinite self-correction loop)."""

    def __init__(self):
        self.calls = 0

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        self.calls += 1
        yield {"type": "text", "content": "Logged 6/20: Pec Fly Machine 3×12 @100 lb."}
        yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}


async def _run(fake, question="Pec fly machine 3x12 100lb"):
    from api.services import agent_loop
    with patch.object(agent_loop, "_select_client", return_value=fake):
        return [e async for e in agent_loop.run_agent_loop(question)]


@pytest.mark.asyncio
async def test_phantom_write_claim_is_nudged_to_real_tool_call():
    from unittest.mock import AsyncMock
    fake = _PhantomThenToolClient()
    with patch(
        "api.services.agent_loop.execute_tool_parallel",
        AsyncMock(return_value="Logged — 2026-06-20: Pec Fly Machine 3×12 @100 lb (session id: abc123def456)"),
    ):
        events = await _run(fake)
    # The phantom reply was retracted and the nudge sent on the second call.
    assert any(e["type"] == "self_correction" for e in events)
    assert any(
        m.get("content") == PHANTOM_WRITE_NUDGE
        for m in fake.calls[1]
        if isinstance(m, dict) and m.get("role") == "user"
    )
    # The real tool call happened.
    result = next(e for e in events if e["type"] == "result")
    assert any(tc["tool"] == "manage_workouts" for tc in result["result"].tool_calls_log)


@pytest.mark.asyncio
async def test_phantom_nudge_fires_at_most_once():
    fake = _AlwaysPhantomClient()
    events = await _run(fake)
    assert fake.calls == 2  # original + one nudged retry, then give up
    # The (still phantom) retry text is surfaced rather than looping forever.
    assert any(e["type"] == "text" for e in events)


@pytest.mark.asyncio
async def test_normal_answer_without_tools_is_untouched():
    """A plain informational answer with no tools must not trigger the nudge."""
    class _Plain:
        def __init__(self):
            self.calls = 0

        async def astream(self, messages, *, system=None, max_tokens=4096,
                          tools=None, temperature=None, timeout=None):
            from api.services.llm_client import LLMUsage
            self.calls += 1
            yield {"type": "text", "content": "Your last squat session was 6/13."}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}

    fake = _Plain()
    events = await _run(fake, question="when did I last squat?")
    assert fake.calls == 1
    assert not any(e["type"] == "self_correction" for e in events)
