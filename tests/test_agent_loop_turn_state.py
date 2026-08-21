"""#615: `run_agent_loop` yields a `turn_state` event -- carrying a live,
mutable reference to its `AgentResult` -- as the very first event, before
any round runs. `api/routes/chat.py` stashes this reference so a cancelled
turn's cancel/deadline handler can read accrued usage without waiting for
the terminal `result` event, which a cancelled turn never reaches.

This is a unit test of the loop's own event contract; tests/test_chat_turn_
cancel_usage.py covers the route-level cancel/usage-recording behavior that
depends on it.
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class _FakeClient:
    """One text+done round, mirroring tests/test_agent_loop_caching.py's
    fake client."""

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        yield {"type": "text", "content": "The answer is 42."}
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=120, output_tokens=8),
            "finish_reason": "end_turn",
        }


@pytest.mark.asyncio
async def test_turn_state_is_the_first_event_and_starts_at_zero_usage():
    from api.services import agent_loop

    with patch.object(agent_loop, "_select_client", return_value=_FakeClient()):
        gen = agent_loop.run_agent_loop("what is six times seven")
        first = await gen.__anext__()

        assert first["type"] == "turn_state"
        live = first["result"]
        # Yielded before any round has run -- nothing accrued yet.
        assert live.total_input_tokens == 0
        assert live.total_output_tokens == 0

        # Drain the rest of the loop.
        rest = [e async for e in gen]

    result_event = next(e for e in rest if e["type"] == "result")
    # The terminal `result` event's payload IS the same object handed out by
    # `turn_state`, mutated in place across the round -- not a copy, and not
    # a different instance built later. This identity is what makes it safe
    # for a caller to stash the `turn_state` reference and read it at any
    # later point instead of waiting for `result`.
    assert result_event["result"] is live
    assert live.total_input_tokens == 120
    assert live.total_output_tokens == 8
    assert live.full_text == "The answer is 42."
