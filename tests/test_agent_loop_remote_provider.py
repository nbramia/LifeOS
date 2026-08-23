"""Tests for the paid remote OpenAI-compatible provider on the native chat
path (#654) -- the "Remote" model picker option.

Unlike every other client `run_agent_loop` can select, a `force_remote` turn
has a real, non-zero cost: `_track_usage` computes it from
`settings.remote_llm_{input,output}_price_per_mtok` and accumulates it across
rounds, rather than the free-local `total_cost_usd = 0.0` every other path
takes. An unconfigured rate marks the turn `unpriced` (#613's usage_store
column) instead of guessing another model's price -- the exact hazard
`cost_tracker.calculate_cost`'s old Sonnet fall-through created. The model id
is set on `AgentResult` up front (from settings, not discovered mid-round),
so even a turn cancelled before any round completes reports the right model.

These are unit tests of `run_agent_loop`'s own wiring -- a fake client stands
in for the real `LocalLLMClient`; `tests/test_llm_client.py::TestRemoteProviderConfig`
covers that the real client actually sends the configured model/auth header
on the wire, and `tests/test_agent_escalation.py` covers `_select_client`
building that real client from settings.
"""
from unittest.mock import AsyncMock, patch

import pytest

from api.services.llm_client import LLMUsage

pytestmark = pytest.mark.unit


class _OneRoundClient:
    """A single tool-round-free turn: text, then "done" with usage."""

    def __init__(self, usage):
        self._usage = usage

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, temperature=None, enable_thinking=None,
                       reasoning_effort=None):
        yield {"type": "text", "content": "42"}
        yield {"type": "done", "usage": self._usage, "finish_reason": "end_turn"}


class _TwoRoundClient:
    """Round 1: a tool call. Round 2 (after the tool result is fed back):
    text + done. Exercises cost accumulation across rounds."""

    def __init__(self, usage_round_1, usage_round_2):
        self._usages = [usage_round_1, usage_round_2]
        self._round = 0

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, temperature=None, enable_thinking=None,
                       reasoning_effort=None):
        usage = self._usages[self._round]
        if self._round == 0:
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "search_vault", "arguments": "{}"},
                }],
            }
            finish_reason = "tool_calls"  # keeps the loop going into round 2
        else:
            yield {"type": "text", "content": "final answer"}
            finish_reason = "end_turn"
        yield {"type": "done", "usage": usage, "finish_reason": finish_reason}
        self._round += 1


def _configure_remote(monkeypatch, *, input_price=0.11, output_price=0.44,
                       model="accounts/fireworks/models/x"):
    from api.services import agent_loop
    monkeypatch.setattr(agent_loop.settings, "remote_llm_model", model, raising=False)
    monkeypatch.setattr(agent_loop.settings, "remote_llm_input_price_per_mtok", input_price, raising=False)
    monkeypatch.setattr(agent_loop.settings, "remote_llm_output_price_per_mtok", output_price, raising=False)


@pytest.mark.asyncio
async def test_remote_turn_prices_usage_from_configured_rates(monkeypatch):
    from api.services import agent_loop

    _configure_remote(monkeypatch, input_price=0.11, output_price=0.44)
    usage = LLMUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    client = _OneRoundClient(usage)

    with patch.object(agent_loop, "_select_client", return_value=client):
        events = [e async for e in agent_loop.run_agent_loop(
            "what is 6x7", max_tool_rounds=1, force_remote=True,
        )]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.model == "accounts/fireworks/models/x"
    assert result.unpriced is False
    # 1000 * 0.11/1e6 + 500 * 0.44/1e6 = 0.00011 + 0.00022 = 0.00033
    assert result.total_cost_usd == pytest.approx(0.00033)


@pytest.mark.asyncio
async def test_remote_turn_accumulates_cost_across_rounds(monkeypatch):
    """A tool round followed by a synthesis round must sum both rounds'
    cost, not overwrite it -- unlike the free-local path, which just resets
    total_cost_usd to 0.0 every round (harmless there since it's always 0)."""
    from api.services import agent_loop

    _configure_remote(monkeypatch, input_price=1.0, output_price=2.0)
    client = _TwoRoundClient(
        LLMUsage(input_tokens=100, output_tokens=10, total_tokens=110),
        LLMUsage(input_tokens=50, output_tokens=5, total_tokens=55),
    )

    with (
        patch.object(agent_loop, "_select_client", return_value=client),
        patch.object(agent_loop, "execute_tool_parallel", AsyncMock(return_value="vault result")),
    ):
        events = [e async for e in agent_loop.run_agent_loop(
            "find X", max_tool_rounds=2, force_remote=True,
        )]

    result = next(e["result"] for e in events if e["type"] == "result")
    # round 1: 100*1/1e6 + 10*2/1e6 = 0.0001 + 0.00002 = 0.00012
    # round 2: 50*1/1e6 + 5*2/1e6 = 0.00005 + 0.00001 = 0.00006
    assert result.total_cost_usd == pytest.approx(0.00018)
    assert result.total_input_tokens == 150
    assert result.total_output_tokens == 15


@pytest.mark.asyncio
async def test_remote_turn_with_no_configured_rate_is_unpriced(monkeypatch):
    """A provider configured to run (URL/model/key) but with no configured
    rate must not be silently priced at 0.0 (which reads as "genuinely
    free") -- it's marked unpriced instead, same #613 convention Hermes
    turns already use."""
    from api.services import agent_loop

    _configure_remote(monkeypatch, input_price=None, output_price=None)
    usage = LLMUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    client = _OneRoundClient(usage)

    with patch.object(agent_loop, "_select_client", return_value=client):
        events = [e async for e in agent_loop.run_agent_loop(
            "what is 6x7", max_tool_rounds=1, force_remote=True,
        )]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.unpriced is True
    assert result.total_cost_usd == 0.0


@pytest.mark.asyncio
async def test_remote_model_id_set_even_if_cancelled_before_any_round(monkeypatch):
    """The model id comes from settings at construction, not discovered
    mid-round -- so it's already correct on the live `turn_state` result a
    cancel handler would read, before any round ever completes."""
    from api.services import agent_loop

    _configure_remote(monkeypatch, model="accounts/fireworks/models/never-called")
    client = _OneRoundClient(LLMUsage())

    with patch.object(agent_loop, "_select_client", return_value=client):
        gen = agent_loop.run_agent_loop("hi", max_tool_rounds=1, force_remote=True)
        first = await gen.__anext__()
        assert first["type"] == "turn_state"
        assert first["result"].model == "accounts/fireworks/models/never-called"
        await gen.aclose()


@pytest.mark.asyncio
async def test_remote_turn_never_receives_local_thinking_control(monkeypatch):
    """The remote provider is also a LocalLLMClient instance (same
    OpenAI-compatible plumbing as Gemma) but isn't llama-server -- the
    local-only enable_thinking knob must never reach it, even when the
    operator has local thinking enabled globally."""
    from api.services import agent_loop

    _configure_remote(monkeypatch)
    captured = {}

    class _CapturingClient:
        async def astream(self, messages, *, system=None, max_tokens=4096,
                           tools=None, temperature=None, **kwargs):
            captured["kwargs"] = kwargs
            yield {"type": "text", "content": "42"}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}

    with (
        patch.object(agent_loop, "_select_client", return_value=_CapturingClient()),
        patch.object(agent_loop.settings, "local_agent_enable_thinking", True),
    ):
        _ = [e async for e in agent_loop.run_agent_loop(
            "hi", max_tool_rounds=1, force_remote=True,
        )]

    assert "enable_thinking" not in captured["kwargs"]
