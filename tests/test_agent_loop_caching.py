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
                      tools=None, temperature=None, timeout=None):
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


class _MockAnthropicStream:
    """Minimal async context manager mimicking anthropic's streaming response,
    so the REAL AnthropicLLMClient.astream can be exercised against it."""

    def __init__(self, final_message, deltas=None):
        self._final = final_message
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


@pytest.mark.asyncio
async def test_synthesis_round_drives_real_client_with_timeout():
    """End-to-end regression for #385. run_agent_loop exhausts its tool rounds
    and the synthesis round drives the REAL AnthropicLLMClient.astream with
    timeout=180 (only the SDK is mocked). If astream loses its timeout param the
    synthesis call raises TypeError, the loop yields "(Error during synthesis…)"
    instead of the answer, and this test fails — so it genuinely guards the bug,
    not just the call shape.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.services import agent_loop
    from api.services.llm_client import AnthropicLLMClient

    def _usage():
        return SimpleNamespace(input_tokens=10, output_tokens=5,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)

    seen_timeouts = []

    def fake_stream(**kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        if kwargs.get("tools"):
            # tool round: final message carries a tool_use block so the loop
            # spends its one round and falls through to synthesis.
            final = SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", id="c1",
                                         name="search_vault", input={})],
                usage=_usage(), stop_reason="tool_use")
            return _MockAnthropicStream(final)
        # synthesis round: a text answer, no tool_use.
        final = SimpleNamespace(content=[], usage=_usage(), stop_reason="end_turn")
        delta = SimpleNamespace(type="content_block_delta",
                                delta=SimpleNamespace(text="Synthesized answer."))
        return _MockAnthropicStream(final, deltas=[delta])

    client = AnthropicLLMClient(api_key="sk-ant-test")
    client._async_client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))

    with patch.object(agent_loop, "_select_client", return_value=client), \
         patch.object(agent_loop, "execute_tool_parallel",
                      AsyncMock(return_value="vault result")):
        events = [e async for e in agent_loop.run_agent_loop("find X", max_tool_rounds=1)]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.full_text == "Synthesized answer."
    assert 180 in seen_timeouts  # the synthesis round forwarded the timeout
