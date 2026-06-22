"""
Tests that the chat orchestrator's caching wiring is live end-to-end (#383 Phase 1).

These guard the two halves of the fix that live in run_agent_loop:
1. Tool definitions are forwarded with their ``cache_control`` marker intact
   (we stopped stripping it on the Anthropic path).
2. Cache-token usage reported by the client flows into AgentResult so caching
   is observable. (These are unit tests of the wiring; that Anthropic actually
   caches across turns is exercised by the client-level tests in
   test_llm_client.py against the real SDK shapes.)
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class _FakeClient:
    """Captures the kwargs run_agent_loop hands to the client and replays a
    single text+done round carrying the given cache-token usage."""

    def __init__(self, captured, cache_read=0, cache_creation=0):
        self._captured = captured
        self._cache_read = cache_read
        self._cache_creation = cache_creation

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None):
        # Signature mirrors the real AnthropicLLMClient.astream exactly (no
        # **kwargs) so drift between run_agent_loop's calls and the client
        # surfaces as a test failure instead of being silently swallowed.
        from api.services.llm_client import LLMUsage
        self._captured["tools"] = tools
        self._captured["system"] = system
        yield {"type": "text", "content": "The answer is 42."}
        yield {
            "type": "done",
            "usage": LLMUsage(
                input_tokens=120,
                output_tokens=8,
                cache_creation_input_tokens=self._cache_creation,
                cache_read_input_tokens=self._cache_read,
            ),
            "finish_reason": "end_turn",
        }


async def _run(fake):
    from api.services import agent_loop
    with patch.object(agent_loop, "_select_client", return_value=fake):
        return [e async for e in agent_loop.run_agent_loop("what is six times seven")]


@pytest.mark.asyncio
async def test_tool_cache_control_is_forwarded():
    """The last tool definition keeps its cache_control marker (cache breakpoint)."""
    captured = {}
    await _run(_FakeClient(captured))
    assert captured["tools"][-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_system_prompt_forwarded_as_cached_block_list():
    """The system prompt reaches the client as a block list whose first (static)
    block carries cache_control — not a flattened string."""
    captured = {}
    await _run(_FakeClient(captured))
    system = captured["system"]
    assert isinstance(system, list)
    assert system[0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_tokens_surface_in_agent_result():
    """Cache-token usage reported by the client is accumulated onto AgentResult —
    the plumbing that makes caching observable. (A warm turn reporting
    cache_read > 0 is how callers see the cache working.)"""
    captured = {}
    events = await _run(_FakeClient(captured, cache_read=2600, cache_creation=512))
    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.total_cache_read_tokens == 2600
    assert result.total_cache_creation_tokens == 512
    assert result.full_text == "The answer is 42."
