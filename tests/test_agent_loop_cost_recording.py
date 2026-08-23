"""#661: every native chat turn used to be recorded as model="local" at
$0.00 regardless of which backend actually served it -- `run_agent_loop`
hardcoded `AgentResult(model="local")` at construction and then
unconditionally zeroed `total_cost_usd` in `_track_usage`, with a comment
("Local model has no cost") that was true only for the local backend but
ran for every backend.

These are unit tests of run_agent_loop's own wiring, exercised the same way
as tests/test_agent_loop_caching.py and tests/test_agent_loop_incremental_
usage.py: a fake client stands in for _select_client's real return value.
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class _FakeAnthropicClient:
    """Stands in for a real AnthropicLLMClient: exposes `.model` the way the
    real class's property does, and replays a single text+done round."""

    def __init__(self, model: str, input_tokens=1000, output_tokens=200):
        self.model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        yield {"type": "text", "content": "42."}
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
            "finish_reason": "end_turn",
        }


class _FakeLocalClient:
    """Stands in for a real LocalLLMClient: `.model` is always "local"."""

    model = "local"

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        yield {"type": "text", "content": "42."}
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=1000, output_tokens=200),
            "finish_reason": "end_turn",
        }


async def _run(fake, model_override=""):
    from api.services import agent_loop
    with patch.object(agent_loop, "_select_client", return_value=fake):
        events = [e async for e in agent_loop.run_agent_loop("what is six times seven", model=model_override)]
    return next(e["result"] for e in events if e["type"] == "result")


@pytest.mark.asyncio
async def test_anthropic_turn_records_real_model_id_and_nonzero_cost():
    """A stubbed Anthropic turn on the default model must record that
    model's real id, not "local", and a cost derived from its actual rate
    -- not the unconditional 0.0 the bug asserted for every backend."""
    result = await _run(_FakeAnthropicClient("claude-haiku-4-5"))
    assert result.model == "claude-haiku-4-5"
    assert result.total_cost_usd > 0.0
    assert result.unpriced is False


@pytest.mark.asyncio
async def test_local_turn_records_local_and_zero_cost():
    """A genuinely local turn still records "local" / $0.00 -- but as a
    derived result of pricing a free model, not a hardcoded assignment."""
    result = await _run(_FakeLocalClient())
    assert result.model == "local"
    assert result.total_cost_usd == 0.0
    assert result.unpriced is False


@pytest.mark.asyncio
async def test_escalated_turn_records_the_escalated_model():
    """When the caller escalates to a stronger model (chat.py passes the
    escalated id as `model=`), the recorded model must be the escalated
    one, not the orchestrator's configured default."""
    result = await _run(_FakeAnthropicClient("claude-opus-4-8"), model_override="claude-opus-4-8")
    assert result.model == "claude-opus-4-8"
    assert result.total_cost_usd > 0.0
    assert result.unpriced is False


@pytest.mark.asyncio
async def test_explicit_picker_choice_records_that_model():
    """The chat model picker's explicit choice (also passed as `model=`,
    same mechanism as escalation) must be recorded verbatim too."""
    result = await _run(_FakeAnthropicClient("claude-sonnet-5"), model_override="claude-sonnet-5")
    assert result.model == "claude-sonnet-5"
    assert result.total_cost_usd > 0.0
    assert result.unpriced is False


@pytest.mark.asyncio
async def test_unknown_model_records_unpriced_not_free():
    """A model with no known rate (e.g. a misconfigured
    settings.anthropic_model) must be marked unpriced rather than recorded
    as a confident, silent $0.00 -- and must NOT silently fall through to
    pricing.cost_for's conservative Opus-rate estimate, which exists for its
    budget-enforcement callers, not for a usage reader that needs to tell
    "free" apart from "unknown"."""
    result = await _run(_FakeAnthropicClient("some-future-model-id"))
    assert result.model == "some-future-model-id"
    assert result.total_cost_usd == 0.0
    assert result.unpriced is True


@pytest.mark.asyncio
async def test_cost_matches_pricing_cost_for():
    """The recorded cost is exactly what pricing.cost_for computes for the
    same tokens -- not a made-up figure -- confirming #661's fix routes
    through the sole live pricing table (#656) rather than reintroducing a
    second one."""
    from api.services.agent_worker.pricing import cost_for
    result = await _run(_FakeAnthropicClient("claude-sonnet-5", input_tokens=500, output_tokens=100))
    assert result.total_cost_usd == cost_for("claude-sonnet-5", 500, 100, 0, 0)
