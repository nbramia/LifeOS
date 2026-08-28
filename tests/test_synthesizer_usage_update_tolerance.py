"""#629 audit: Synthesizer.stream_response is one of astream()'s consumers
(the other two call sites are in api/services/agent_loop.py, covered by
tests/test_agent_loop_incremental_usage.py). Adding the "usage_update" event
type to the astream() contract must not break a consumer that doesn't know
about it -- stream_response's if/elif chain has no `else`, so an
unrecognized event type is silently skipped by construction, but that's
worth pinning down with a real test rather than leaving it implicit.
"""
import pytest

from api.services.llm_client import LLMUsage
from api.services.synthesizer import Synthesizer

pytestmark = pytest.mark.unit


class _FakeClientWithUsageUpdate:
    """Mirrors AnthropicLLMClient.astream: text, then a "usage_update"
    snapshot the caller doesn't ask for, then the terminal "done"."""

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        yield {"type": "text", "content": "The answer is 42."}
        yield {
            "type": "usage_update",
            "usage": LLMUsage(input_tokens=50, output_tokens=8, total_tokens=58),
        }
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=50, output_tokens=10, total_tokens=60),
            "finish_reason": "end_turn",
        }


@pytest.mark.asyncio
async def test_stream_response_tolerates_usage_update_event():
    synth = Synthesizer()
    synth._client = _FakeClientWithUsageUpdate()

    chunks = [c async for c in synth.stream_response("what is six times seven")]

    text = "".join(c for c in chunks if isinstance(c, str))
    assert text == "The answer is 42."

    usage_events = [c for c in chunks if isinstance(c, dict)]
    # Only the "done" event's usage reaches the caller -- the "usage_update"
    # snapshot in between was silently ignored, not surfaced as a second
    # (and different-shaped) usage dict.
    assert len(usage_events) == 1
    assert usage_events[0] == {
        "type": "usage",
        "input_tokens": 50,
        "output_tokens": 10,
        "cost_usd": 0.0,
        "model": "local",
    }


class _FakeClientDone:
    """A single text chunk, then "done" — no usage_update, the simple case."""

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        yield {"type": "text", "content": "42."}
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=5, output_tokens=2, total_tokens=7),
            "finish_reason": "end_turn",
        }


@pytest.mark.asyncio
async def test_usage_event_model_label_matches_summarizer_model_default(monkeypatch):
    """#775: the usage event's "model" label was a hardcoded "local" literal
    -- now it reads settings.summarizer_model, same as the summarizer's
    outbound payload, for consistency. Default is unchanged."""
    from config.settings import settings
    monkeypatch.setattr(settings, "summarizer_model", "local", raising=False)

    synth = Synthesizer()
    synth._client = _FakeClientDone()
    chunks = [c async for c in synth.stream_response("what is six times seven")]

    usage_event = next(c for c in chunks if isinstance(c, dict))
    assert usage_event["model"] == "local"


@pytest.mark.asyncio
async def test_usage_event_model_label_reflects_configured_override(monkeypatch):
    """#775: an operator running Ollama (LIFEOS_SUMMARIZER_MODEL set) sees
    that model name in the usage label instead of the misleading "local"."""
    from config.settings import settings
    monkeypatch.setattr(settings, "summarizer_model", "qwen2.5:3b-instruct", raising=False)

    synth = Synthesizer()
    synth._client = _FakeClientDone()
    chunks = [c async for c in synth.stream_response("what is six times seven")]

    usage_event = next(c for c in chunks if isinstance(c, dict))
    assert usage_event["model"] == "qwen2.5:3b-instruct"
