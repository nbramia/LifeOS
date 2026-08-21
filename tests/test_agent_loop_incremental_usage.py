"""#629: a cancellation landing mid-round (after the client has surfaced a
"usage_update" event but before that round's "done" event) must not report
zero usage for the whole round, and a round that completes normally must
still fold its usage into AgentResult exactly once -- the "usage_update"
snapshot from AnthropicLLMClient.astream must never be double-counted
against the round-end total from _track_usage.

These are unit tests of run_agent_loop's own wiring (a fake client stands in
for AnthropicLLMClient); tests/test_chat_turn_cancel_usage.py covers the
route-level cancel/usage-recording behavior built on top of it.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.services.llm_client import LLMUsage

pytestmark = pytest.mark.unit


class _FakeAnthropicLikeClient:
    """One round: text, then a "usage_update" snapshot (as
    AnthropicLLMClient.astream yields from message_start/message_delta),
    then blocks on ``hold`` before the round's "done" event -- standing in
    for a cancellation landing mid-round, after tokens were already billed
    but before the round closed out."""

    def __init__(self, hold, final_usage=None):
        self._hold = hold
        self._final_usage = final_usage or LLMUsage(input_tokens=100, output_tokens=20, total_tokens=120)

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        yield {"type": "text", "content": "Hello "}
        yield {
            "type": "usage_update",
            "usage": LLMUsage(input_tokens=100, output_tokens=15, total_tokens=115),
        }
        await self._hold.wait()
        yield {"type": "done", "usage": self._final_usage, "finish_reason": "end_turn"}


@pytest.mark.asyncio
async def test_mid_round_cancellation_credits_provisional_tokens_without_folding_into_total():
    """A cancellation that lands after "usage_update" but before "done"
    must leave the tokens visible on the live AgentResult via
    provisional_input_tokens/provisional_output_tokens -- exactly what a
    cancel/deadline handler reads (#615, #629) -- without having advanced
    total_input_tokens/total_output_tokens, since the round never closed
    out."""
    from api.services import agent_loop

    hold = asyncio.Event()

    with patch.object(agent_loop, "_select_client", return_value=_FakeAnthropicLikeClient(hold)):
        gen = agent_loop.run_agent_loop("hi")
        first = await gen.__anext__()
        live = first["result"]
        assert live.provisional_input_tokens == 0

        second = await gen.__anext__()
        assert second == {"type": "text", "content": "Hello "}

        # Resuming the generator processes "usage_update" (mutating `live`)
        # before it reaches the genuine suspension point (`hold.wait()`),
        # all without another `yield` in between -- so by the time this
        # task is actually running, `live.provisional_*` already reflects
        # the "usage_update" event.
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert live.provisional_input_tokens == 100
        assert live.provisional_output_tokens == 15
        # The round hasn't closed out -- _track_usage never ran.
        assert live.total_input_tokens == 0
        assert live.total_output_tokens == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation landed mid-round: the provisional credit survives
        # (this is what a cancel handler reads instead of a confident
        # zero), and the confirmed totals are still untouched.
        assert live.provisional_input_tokens == 100
        assert live.provisional_output_tokens == 15
        assert live.total_input_tokens == 0
        assert live.total_output_tokens == 0

        await gen.aclose()


@pytest.mark.asyncio
async def test_usage_update_does_not_double_count_when_round_completes_normally():
    """Regression guard for the double-count risk #629 explicitly warns
    about: once the round's "done" event arrives, total_input_tokens/
    total_output_tokens must reflect exactly that event's usage -- not the
    "usage_update" snapshot plus the "done" usage added on top -- and the
    provisional fields must be cleared so a later reader doesn't add them
    again."""
    from api.services import agent_loop

    hold = asyncio.Event()
    hold.set()  # never actually blocks -- straight through to "done"

    with patch.object(agent_loop, "_select_client", return_value=_FakeAnthropicLikeClient(hold)):
        events = [e async for e in agent_loop.run_agent_loop("hi")]

    result = next(e["result"] for e in events if e["type"] == "result")
    # The "done" event's usage (100/20) -- NOT 100+100 / 15+20 -- proves the
    # "usage_update" snapshot (100/15) was never added on top.
    assert result.total_input_tokens == 100
    assert result.total_output_tokens == 20
    assert result.provisional_input_tokens == 0
    assert result.provisional_output_tokens == 0


@pytest.mark.asyncio
async def test_synthesis_round_tolerates_usage_update_without_double_counting():
    """The second astream() call site in run_agent_loop -- the synthesis
    round that runs once tool rounds are exhausted -- must handle
    "usage_update" the same way as the main tool-round loop: tolerate it
    without erroring, and fold "done" usage into the total exactly once.
    Drives the REAL AnthropicLLMClient.astream (only the SDK's
    messages.stream is mocked), mirroring
    test_agent_loop_caching.py::test_synthesis_round_drives_real_client_with_timeout.
    """
    from unittest.mock import AsyncMock
    from api.services import agent_loop
    from api.services.llm_client import AnthropicLLMClient

    def _usage(input_tokens=10, output_tokens=5):
        return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)

    class _MockAnthropicStream:
        def __init__(self, final, deltas=None):
            self._final = final
            self._deltas = deltas or []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            for d in self._deltas:
                yield d

        async def get_final_message(self):
            return self._final

    def fake_stream(**kwargs):
        if kwargs.get("tools"):
            # tool round: a tool_use block so the loop spends its one round
            # and falls through to the synthesis round.
            final = SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", id="c1", name="search_vault", input={})],
                usage=_usage(), stop_reason="tool_use")
            return _MockAnthropicStream(final)
        # synthesis round: message_start + message_delta ahead of the text
        # answer -- the "usage_update" events the tool-round loop already
        # handles, now exercised on this second call site.
        final = SimpleNamespace(content=[], usage=_usage(input_tokens=30, output_tokens=12), stop_reason="end_turn")
        message_start = SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(
                input_tokens=30, output_tokens=1,
                cache_creation_input_tokens=0, cache_read_input_tokens=0)))
        text_delta = SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="Synthesized answer."))
        return _MockAnthropicStream(final, deltas=[message_start, text_delta])

    client = AnthropicLLMClient(api_key="sk-ant-test")
    client._async_client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))

    with patch.object(agent_loop, "_select_client", return_value=client), \
         patch.object(agent_loop, "execute_tool_parallel", AsyncMock(return_value="vault result")):
        events = [e async for e in agent_loop.run_agent_loop("find X", max_tool_rounds=1)]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.full_text == "Synthesized answer."
    # Tool round (10/5) + synthesis round's "done" usage (30/12) -- not the
    # synthesis round's "usage_update" snapshot (30/1) added on top too.
    assert result.total_input_tokens == 10 + 30
    assert result.total_output_tokens == 5 + 12
    assert result.provisional_input_tokens == 0
    assert result.provisional_output_tokens == 0
