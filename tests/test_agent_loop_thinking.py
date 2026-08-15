"""
Tests for thinking control on the orchestrator path (#567, follow-up to #566/#570).

PR #570 added LIFEOS_ROUTER_ENABLE_THINKING for query_router, but run_agent_loop
had no equivalent — its two client.astream(...) call sites always let a local
reasoning model think with no way to turn it off. settings.local_agent_enable_thinking
closes that gap.

The critical constraint: client at those call sites may be either LocalLLMClient
or AnthropicLLMClient (see agent_loop._select_client). AnthropicLLMClient.astream
has no enable_thinking parameter — passing it unconditionally breaks the
(default) cloud backend. These tests cover: the local body stays byte-identical
at the default, a disabled setting reaches the local body as enable_thinking=False,
and the Anthropic path is provably never handed the kwarg.
"""
import json

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class _FakeSSEResponse:
    """Mimics the httpx streaming response LocalLLMClient.astream reads from."""

    def __init__(self, chunks: list[dict]):
        self._lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _CapturingAsyncClient:
    """Stand-in for httpx.AsyncClient that records each streamed request body."""

    def __init__(self, resp):
        self.is_closed = False
        self._resp = resp
        self.bodies: list[dict] = []

    def stream(self, method, url, **kwargs):
        self.bodies.append(kwargs["json"])
        return _FakeStreamCtx(self._resp)


def _done_chunk(finish_reason="stop"):
    return {
        "choices": [{"delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _local_client_with_chunks(chunks):
    from api.services.llm_client import LocalLLMClient
    client = LocalLLMClient(base_url="http://fake:8080")
    async_client = _CapturingAsyncClient(_FakeSSEResponse(chunks))
    client._async_client = async_client
    return client, async_client


@pytest.mark.asyncio
async def test_local_default_true_omits_enable_thinking_from_wire_body():
    """settings.local_agent_enable_thinking defaults True -> the orchestrator's
    outgoing request body to a LOCAL client is byte-identical to before this
    setting existed (mirrors #566 PR 2's router-level payload assertion) —
    asserted at the actual wire body, not a mocked call-kwargs shape."""
    from api.services import agent_loop

    client, async_client = _local_client_with_chunks([
        {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
        _done_chunk(),
    ])
    with patch.object(agent_loop, "_select_client", return_value=client):
        events = [e async for e in agent_loop.run_agent_loop("what is 6x7", max_tool_rounds=1)]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.full_text == "42"
    assert async_client.bodies, "expected at least one streamed request"
    for body in async_client.bodies:
        assert "chat_template_kwargs" not in body


@pytest.mark.asyncio
async def test_local_disabled_setting_sends_enable_thinking_false():
    """When settings.local_agent_enable_thinking is False, the tool-round
    request reaching a LOCAL client explicitly turns thinking off."""
    from api.services import agent_loop

    client, async_client = _local_client_with_chunks([
        {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
        _done_chunk(),
    ])
    with (
        patch.object(agent_loop, "_select_client", return_value=client),
        patch.object(agent_loop.settings, "local_agent_enable_thinking", False),
    ):
        _ = [e async for e in agent_loop.run_agent_loop("what is 6x7", max_tool_rounds=1)]

    assert async_client.bodies
    assert async_client.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


class _MockAnthropicStream:
    """Minimal async context manager mimicking anthropic's streaming response
    (same shape as test_agent_loop_caching.py's fixture), so the REAL
    AnthropicLLMClient.astream can be exercised end-to-end."""

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
async def test_anthropic_astream_never_receives_enable_thinking():
    """Regression guard: AnthropicLLMClient.astream's real signature has no
    enable_thinking parameter (no **kwargs to swallow it either). If
    agent_loop's isinstance(client, LocalLLMClient) gate were ever dropped
    and the kwarg passed unconditionally, this call raises TypeError before
    ever reaching the (mocked) SDK internals — proving the cloud path,
    which is the default backend, is unaffected by this feature."""
    from types import SimpleNamespace
    from api.services import agent_loop
    from api.services.llm_client import AnthropicLLMClient

    def _usage():
        return SimpleNamespace(input_tokens=10, output_tokens=5,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)

    def fake_stream(**kwargs):
        final = SimpleNamespace(content=[], usage=_usage(), stop_reason="end_turn")
        delta = SimpleNamespace(type="content_block_delta",
                                delta=SimpleNamespace(text="42"))
        return _MockAnthropicStream(final, deltas=[delta])

    client = AnthropicLLMClient(api_key="sk-ant-test")
    client._async_client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))

    with patch.object(agent_loop, "_select_client", return_value=client):
        events = [e async for e in agent_loop.run_agent_loop("what is 6x7", max_tool_rounds=1)]

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.full_text == "42"
