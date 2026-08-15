"""
Tests for the unified LLM client.

Covers tool format translation, message conversion, response parsing,
and singleton/backend switching behavior.
"""
import json

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


# ---- Tool Format Translation ----


class TestAnthropicToolsToOpenAI:
    """Tests for Anthropic → OpenAI tool schema conversion."""

    def test_basic_conversion(self):
        """Convert a simple Anthropic tool definition to OpenAI format."""
        from api.services.llm_client import _anthropic_tools_to_openai

        tools = [{
            "name": "search_vault",
            "description": "Search the vault",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }]
        result = _anthropic_tools_to_openai(tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search_vault"
        assert result[0]["function"]["description"] == "Search the vault"
        assert result[0]["function"]["parameters"]["type"] == "object"
        assert "query" in result[0]["function"]["parameters"]["properties"]

    def test_strips_cache_control(self):
        """Anthropic cache_control fields are stripped from the schema."""
        from api.services.llm_client import _anthropic_tools_to_openai

        tools = [{
            "name": "test",
            "description": "test",
            "input_schema": {
                "type": "object",
                "properties": {},
                "cache_control": {"type": "ephemeral"},
            },
        }]
        result = _anthropic_tools_to_openai(tools)
        assert "cache_control" not in result[0]["function"]["parameters"]

    def test_empty_tools(self):
        """Empty list returns empty list."""
        from api.services.llm_client import _anthropic_tools_to_openai

        assert _anthropic_tools_to_openai([]) == []

    def test_missing_fields(self):
        """Handles tools with missing optional fields gracefully."""
        from api.services.llm_client import _anthropic_tools_to_openai

        tools = [{"name": "minimal"}]
        result = _anthropic_tools_to_openai(tools)
        assert result[0]["function"]["name"] == "minimal"
        assert result[0]["function"]["description"] == ""
        assert result[0]["function"]["parameters"] == {}


class TestOpenAIToolCallsToAnthropic:
    """Tests for OpenAI → Anthropic tool call response conversion."""

    def test_basic_conversion(self):
        """Convert OpenAI tool calls to Anthropic-style blocks."""
        from api.services.llm_client import openai_tool_calls_to_anthropic

        tool_calls = [{
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "search_vault",
                "arguments": '{"query": "meeting notes"}',
            },
        }]
        result = openai_tool_calls_to_anthropic(tool_calls)

        assert len(result) == 1
        block = result[0]
        assert block.id == "call_123"
        assert block.name == "search_vault"
        assert block.input == {"query": "meeting notes"}
        assert block.type == "tool_use"

    def test_malformed_json_arguments(self):
        """Invalid JSON arguments fall back to empty dict."""
        from api.services.llm_client import openai_tool_calls_to_anthropic

        tool_calls = [{
            "id": "call_456",
            "type": "function",
            "function": {"name": "test", "arguments": "not-json"},
        }]
        result = openai_tool_calls_to_anthropic(tool_calls)
        assert result[0].input == {}

    def test_multiple_tool_calls(self):
        """Multiple tool calls are all converted."""
        from api.services.llm_client import openai_tool_calls_to_anthropic

        tool_calls = [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]
        result = openai_tool_calls_to_anthropic(tool_calls)
        assert len(result) == 2
        assert result[0].name == "a"
        assert result[1].name == "b"


# ---- Message Conversion ----


class TestConvertMessage:
    """Tests for LocalLLMClient message conversion."""

    def _client(self):
        """Create a client without connecting."""
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient(base_url="http://fake:8080")

    def test_string_content(self):
        """Simple string content passes through."""
        client = self._client()
        result = client._convert_message({"role": "user", "content": "hello"})
        assert result == {"role": "user", "content": "hello"}

    def test_text_blocks(self):
        """Anthropic-style text content blocks are joined."""
        client = self._client()
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        }
        result = client._convert_message(msg)
        assert result["content"] == "first\nsecond"

    def test_tool_use_blocks(self):
        """Assistant messages with tool_use blocks convert to OpenAI format."""
        client = self._client()
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me search."},
                {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "test"}},
            ],
        }
        result = client._convert_assistant_with_tools(msg["content"])
        assert result["role"] == "assistant"
        assert result["content"] == "Let me search."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "search"

    def test_tool_result_blocks(self):
        """Tool result blocks convert to OpenAI tool messages."""
        client = self._client()
        content = [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "result text"},
        ]
        result = client._convert_tool_results(content)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tu_1"
        assert result[0]["content"] == "result text"

    def test_none_content(self):
        """None content returns None."""
        client = self._client()
        assert client._convert_message({"role": "user", "content": None}) is None


class TestBuildMessagesList:
    """Tests for _build_messages_list which handles tool result expansion."""

    def _client(self):
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient(base_url="http://fake:8080")

    def test_system_string(self):
        """String system prompt is prepended as system message."""
        client = self._client()
        result = client._build_messages_list(
            [{"role": "user", "content": "hi"}],
            system="You are helpful.",
        )
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "hi"}

    def test_system_blocks(self):
        """Anthropic-style system blocks are joined into a single system message."""
        client = self._client()
        result = client._build_messages_list(
            [{"role": "user", "content": "hi"}],
            system=[
                {"type": "text", "text": "Block 1"},
                {"type": "text", "text": "Block 2"},
            ],
        )
        assert result[0]["role"] == "system"
        assert "Block 1" in result[0]["content"]
        assert "Block 2" in result[0]["content"]

    def test_tool_results_expanded(self):
        """Tool result messages are expanded into individual tool messages."""
        client = self._client()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking..."},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "res1"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "res2"},
                ],
            },
        ]
        result = client._build_messages_list(messages)
        # Tool results should be expanded to two separate messages
        tool_msgs = [m for m in result if m["role"] == "tool"]
        assert len(tool_msgs) == 2


# ---- Response Parsing ----


class TestParseResponse:
    """Tests for _parse_response."""

    def _client(self):
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient(base_url="http://fake:8080")

    def test_text_response(self):
        """Parse a standard text response."""
        client = self._client()
        data = {
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == "Hello!"
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
        assert resp.finish_reason == "stop"

    def test_tool_calls_response(self):
        """Parse a response with tool calls."""
        client = self._client()
        data = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q": "test"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == ""
        assert resp.tool_calls is not None
        assert len(resp.tool_calls) == 1
        assert resp.finish_reason == "tool_calls"

    def test_empty_choices(self):
        """Empty choices returns empty response."""
        client = self._client()
        resp = client._parse_response({"choices": [], "model": "test"})
        assert resp.text == ""
        assert resp.tool_calls is None

    def test_plain_response_unaffected_by_reasoning_handling(self):
        """A response with no reasoning_content and no <think> tags is
        byte-for-byte unchanged — no stripping, no whitespace changes."""
        client = self._client()
        data = {
            "choices": [{"message": {"content": "  Hello!  \n"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == "  Hello!  \n"
        assert resp.reasoning == ""
        assert resp.reasoning_starved is False

    def test_reasoning_content_field_kept_separate(self):
        """A dedicated reasoning_content field is exposed on .reasoning, never
        merged into .text."""
        client = self._client()
        data = {
            "choices": [{
                "message": {
                    "content": "The answer is 42.",
                    "reasoning_content": "Let me think... 6 * 7 = 42.",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == "The answer is 42."
        assert resp.reasoning == "Let me think... 6 * 7 = 42."

    def test_inline_think_tag_stripped(self):
        """Inline <think>...</think> in content is stripped out and routed to
        .reasoning instead."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "<think>6 * 7 = 42</think>The answer is 42."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == "The answer is 42."
        assert resp.reasoning == "6 * 7 = 42"

    def test_unterminated_think_tag_truncation(self):
        """An unterminated <think> (response truncated mid-thought) has
        everything from the tag onward treated as reasoning, not answer text."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "<think>still working through the mat"},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2048, "total_tokens": 2058},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == ""
        assert resp.reasoning == "still working through the mat"

    def test_reasoning_starved_true_when_budget_exhausted_on_reasoning(self):
        """reasoning_starved distinguishes 'ran out of budget mid-thought'
        from a legitimate empty answer."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "", "reasoning_content": "Thinking very hard..."},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2048, "total_tokens": 2058},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == ""
        assert resp.reasoning == "Thinking very hard..."
        assert resp.reasoning_starved is True

    def test_reasoning_starved_false_for_legitimate_empty_answer(self):
        """A genuinely empty response (no reasoning at all) is NOT flagged as
        reasoning-starved."""
        client = self._client()
        data = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == ""
        assert resp.reasoning_starved is False

    def test_reasoning_starved_false_when_finish_reason_is_stop(self):
        """Reasoning present but finish_reason=stop (not length) — the model
        chose to answer with nothing, this isn't budget starvation."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "", "reasoning_content": "hmm"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.reasoning_starved is False

    def test_closing_only_think_tag_leaks_no_reasoning(self):
        """Finding 1 (CRITICAL): many llama.cpp jinja templates pre-fill the
        opening <think> into the prompt, so the model emits only a closing
        </think>. Everything before it is reasoning; everything after is the
        answer — none of the reasoning should leak into .text."""
        client = self._client()
        data = {
            "choices": [{
                "message": {
                    "content": 'internal reasoning\n</think>{"sources":["calendar"],"reasoning":"ok"}',
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == '{"sources":["calendar"],"reasoning":"ok"}'
        assert resp.reasoning == "internal reasoning"
        assert "internal reasoning" not in resp.text

    def test_literal_think_tag_in_legitimate_content_untouched(self):
        """Finding 2 (MAJOR): a literal <think>...</think> pair appearing
        after ordinary prose (not at the start of the response) is just text
        a user is asking about — it must not be corrupted or split into
        .reasoning."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "Use `<think>draft</think>` as the example tag."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == "Use `<think>draft</think>` as the example tag."
        assert resp.reasoning == ""

    def test_reasoning_starved_true_for_closing_only_truncation(self):
        """Finding 3 (MAJOR): a closing-only <think> block that consumed the
        whole token budget must be flagged as reasoning_starved, same as the
        full-pair case."""
        client = self._client()
        data = {
            "choices": [{
                "message": {"content": "long hidden reasoning\n</think>"},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2048, "total_tokens": 2058},
            "model": "local",
        }
        resp = client._parse_response(data)
        assert resp.text == ""
        assert resp.reasoning == "long hidden reasoning"
        assert resp.reasoning_starved is True


# ---- Reasoning control (#566 PR 2: per-request thinking/reasoning_effort) ----


class TestReasoningControlPayloadHelper:
    """Tests for the ``_reasoning_control_payload`` helper shared by
    create/acreate/astream."""

    def test_both_unset_returns_empty(self):
        """The critical backward-compat case: no keys unless a caller asks."""
        from api.services.llm_client import _reasoning_control_payload
        assert _reasoning_control_payload(None, None) == {}

    def test_enable_thinking_true(self):
        from api.services.llm_client import _reasoning_control_payload
        assert _reasoning_control_payload(True, None) == {
            "chat_template_kwargs": {"enable_thinking": True}
        }

    def test_enable_thinking_false(self):
        from api.services.llm_client import _reasoning_control_payload
        assert _reasoning_control_payload(False, None) == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_reasoning_effort_passthrough(self):
        from api.services.llm_client import _reasoning_control_payload
        assert _reasoning_control_payload(None, "low") == {"reasoning_effort": "low"}

    def test_both_set(self):
        from api.services.llm_client import _reasoning_control_payload
        assert _reasoning_control_payload(True, "high") == {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_effort": "high",
        }


class TestCreateReasoningControl:
    """create()/acreate() forward reasoning control into the request body,
    and the request body is byte-identical to before this feature existed
    when neither knob is passed."""

    def _client(self):
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient(base_url="http://fake:8080")

    def _fake_sync_client(self):
        from unittest.mock import MagicMock
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"choices": [], "model": "local"}
        fake_sync_client = MagicMock(is_closed=False)
        fake_sync_client.post.return_value = fake_resp
        return fake_sync_client

    def test_create_unset_body_byte_identical(self):
        """No enable_thinking/reasoning_effort ⇒ no new keys in the request body."""
        client = self._client()
        fake_sync_client = self._fake_sync_client()
        client._sync_client = fake_sync_client

        client.create([{"role": "user", "content": "hi"}])

        args, kwargs = fake_sync_client.post.call_args
        assert args == ("/v1/chat/completions",)
        assert kwargs["json"] == {
            "model": "local",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4096,
            "stream": False,
        }

    def test_create_enable_thinking_false(self):
        client = self._client()
        fake_sync_client = self._fake_sync_client()
        client._sync_client = fake_sync_client

        client.create([{"role": "user", "content": "hi"}], enable_thinking=False)

        payload = fake_sync_client.post.call_args.kwargs["json"]
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    def test_create_reasoning_effort_passthrough(self):
        client = self._client()
        fake_sync_client = self._fake_sync_client()
        client._sync_client = fake_sync_client

        client.create([{"role": "user", "content": "hi"}], reasoning_effort="low")

        payload = fake_sync_client.post.call_args.kwargs["json"]
        assert payload["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_acreate_unset_body_byte_identical(self):
        from unittest.mock import AsyncMock, MagicMock
        client = self._client()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"choices": [], "model": "local"}
        fake_async_client = MagicMock(is_closed=False)
        fake_async_client.post = AsyncMock(return_value=fake_resp)
        client._async_client = fake_async_client

        await client.acreate([{"role": "user", "content": "hi"}])

        payload = fake_async_client.post.call_args.kwargs["json"]
        assert payload == {
            "model": "local",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4096,
            "stream": False,
        }

    @pytest.mark.asyncio
    async def test_acreate_enable_thinking_true(self):
        from unittest.mock import AsyncMock, MagicMock
        client = self._client()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"choices": [], "model": "local"}
        fake_async_client = MagicMock(is_closed=False)
        fake_async_client.post = AsyncMock(return_value=fake_resp)
        client._async_client = fake_async_client

        await client.acreate([{"role": "user", "content": "hi"}], enable_thinking=True, reasoning_effort="high")

        payload = fake_async_client.post.call_args.kwargs["json"]
        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
        assert payload["reasoning_effort"] == "high"


class TestAStreamReasoningControl:
    """astream() forwards reasoning control the same way create()/acreate() do."""

    def _client_with_capture(self, chunks):
        from api.services.llm_client import LocalLLMClient
        client = LocalLLMClient(base_url="http://fake:8080")
        captured = {}

        class _CapturingAsyncClient(_FakeAsyncClient):
            def stream(self, method, url, **kwargs):
                captured["kwargs"] = kwargs
                return super().stream(method, url, **kwargs)

        client._async_client = _CapturingAsyncClient(_FakeSSEResponse(chunks))
        return client, captured

    @pytest.mark.asyncio
    async def test_astream_unset_body_byte_identical(self):
        client, captured = self._client_with_capture([_done_chunk()])
        _ = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        assert captured["kwargs"]["json"] == {
            "model": "local",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4096,
            "stream": True,
        }

    @pytest.mark.asyncio
    async def test_astream_enable_thinking_false(self):
        client, captured = self._client_with_capture([_done_chunk()])
        _ = [
            e async for e in client.astream(
                [{"role": "user", "content": "hi"}], enable_thinking=False, reasoning_effort="low"
            )
        ]
        assert captured["kwargs"]["json"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["kwargs"]["json"]["reasoning_effort"] == "low"


# ---- Singleton ----


class TestSingleton:
    """Tests for get_local_llm and reset_local_llm."""

    def test_returns_same_instance(self):
        """Singleton returns the same client on repeated calls."""
        from api.services.llm_client import get_local_llm, reset_local_llm, LocalLLMClient
        reset_local_llm()
        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.llm_backend = "local"
            mock_settings.local_llm_url = "http://fake:8080"
            mock_settings.local_llm_timeout = 90
            c1 = get_local_llm()
            c2 = get_local_llm()
            assert c1 is c2
            assert isinstance(c1, LocalLLMClient)
        reset_local_llm()

    def test_reset_clears_singleton(self):
        """reset_local_llm clears the cached instance."""
        from api.services.llm_client import get_local_llm, reset_local_llm
        reset_local_llm()
        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.llm_backend = "local"
            mock_settings.local_llm_url = "http://fake:8080"
            mock_settings.local_llm_timeout = 90
            c1 = get_local_llm()
            reset_local_llm()
            c2 = get_local_llm()
            assert c1 is not c2
        reset_local_llm()

    def test_anthropic_backend(self):
        """Setting llm_backend=anthropic returns AnthropicLLMClient."""
        from api.services.llm_client import get_local_llm, reset_local_llm, AnthropicLLMClient
        reset_local_llm()
        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.llm_backend = "anthropic"
            mock_settings.anthropic_api_key = "sk-ant-test-key"
            mock_settings.anthropic_model = "claude-haiku-4-5"
            try:
                client = get_local_llm()
                assert isinstance(client, AnthropicLLMClient)
                assert client._model == "claude-haiku-4-5"
            except ImportError:
                pytest.skip("anthropic package not installed")
        reset_local_llm()

    def test_specialist_client_resolves_model_from_settings(self):
        """get_anthropic_llm() resolves LIFEOS_ANTHROPIC_SPECIALIST_MODEL,
        independent of the orchestrator model (#470 — was a hardcoded dated
        snapshot that retired and 404'd every specialist caller)."""
        from api.services.llm_client import get_anthropic_llm, reset_local_llm, AnthropicLLMClient
        reset_local_llm()
        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test-key"
            mock_settings.anthropic_model = "claude-haiku-4-5"
            mock_settings.anthropic_specialist_model = "claude-sonnet-5"
            try:
                client = get_anthropic_llm()
                assert isinstance(client, AnthropicLLMClient)
                assert client._model == "claude-sonnet-5"
                assert client._model != mock_settings.anthropic_model  # independent
            except ImportError:
                pytest.skip("anthropic package not installed")
        reset_local_llm()


# ---- LLMResponse / LLMUsage ----


class TestExtractJson:
    """Tests for the shared ``extract_json`` helper used by routing/validation callers."""

    def test_raw_json(self):
        from api.services.llm_client import extract_json
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_block(self):
        from api.services.llm_client import extract_json
        text = 'Here is the result:\n```json\n{"x": [1, 2]}\n```\nDone.'
        assert extract_json(text) == {"x": [1, 2]}

    def test_unfenced_code_block(self):
        from api.services.llm_client import extract_json
        text = '```\n{"y": "v"}\n```'
        assert extract_json(text) == {"y": "v"}

    def test_embedded_in_prose(self):
        from api.services.llm_client import extract_json
        text = 'Decision below.\n{"keep": true, "score": 0.9}\nThanks.'
        assert extract_json(text) == {"keep": True, "score": 0.9}

    def test_nested_braces(self):
        from api.services.llm_client import extract_json
        text = '{"outer": {"inner": 42}}'
        assert extract_json(text) == {"outer": {"inner": 42}}

    def test_raises_on_no_json(self):
        from api.services.llm_client import extract_json
        with pytest.raises(ValueError):
            extract_json("no json here")


@pytest.fixture
def _routing_singleton_reset():
    """Reset the routing-client singleton (and its cache key) around each
    test so mutations don't leak."""
    from api.services import llm_client as mod
    prev_client, prev_url = mod._routing_client, mod._routing_client_url
    mod._routing_client = None
    mod._routing_client_url = None
    yield mod
    mod._routing_client = prev_client
    mod._routing_client_url = prev_url


class TestRoutingHelpers:
    """Tests for ``generate_text``, ``generate_json``, and availability checks."""

    @pytest.mark.asyncio
    async def test_generate_text_calls_local_client(self, _routing_singleton_reset):
        """generate_text should round-trip through LocalLLMClient.acreate()."""
        from unittest.mock import AsyncMock, MagicMock
        mod = _routing_singleton_reset

        fake_client = MagicMock()
        fake_resp = MagicMock(text="result text")
        fake_client.acreate = AsyncMock(return_value=fake_resp)
        with patch.object(mod, "_get_local_routing_client", return_value=fake_client):
            text = await mod.generate_text("hi", max_tokens=10, temperature=0.5)
        assert text == "result text"
        # Ensure it actually went through acreate with our params.
        kwargs = fake_client.acreate.await_args.kwargs
        assert kwargs["max_tokens"] == 10
        assert kwargs["temperature"] == 0.5

    @pytest.mark.asyncio
    async def test_generate_text_with_timeout_uses_routing_url(self, _routing_singleton_reset):
        """Regression: generate_text's ``timeout is not None`` branch builds a
        transient LocalLLMClient instead of using the cached routing
        singleton — that transient client must still be pinned to
        settings.routing_llm_url, not silently fall back to the default
        local LLM URL. Two of three routing/validation callers pass a
        timeout (agent_viz_summary, person_facts), so this branch is the
        common case, not an edge case."""
        from unittest.mock import AsyncMock, MagicMock
        mod = _routing_singleton_reset

        fake_instance = MagicMock()
        fake_resp = MagicMock(text="ok", reasoning_starved=False)
        fake_instance.acreate = AsyncMock(return_value=fake_resp)
        with (
            patch.object(mod, "settings") as mock_settings,
            patch.object(mod, "LocalLLMClient", return_value=fake_instance) as mock_cls,
        ):
            mock_settings.routing_llm_url = "http://routing-box:9090"
            await mod.generate_text("hi", timeout=30)

        kwargs = mock_cls.call_args.kwargs
        assert kwargs["base_url"] == "http://routing-box:9090"
        assert kwargs["timeout"] == 30

    @pytest.mark.asyncio
    async def test_generate_text_warns_on_reasoning_starvation(self, _routing_singleton_reset, caplog):
        """generate_text logs a distinct warning when the model burned its
        whole budget on reasoning and returned no answer — otherwise this
        failure mode is silent (query_router falls through to a generic
        failure path with no clue why)."""
        import logging
        from unittest.mock import AsyncMock, MagicMock
        from api.services.llm_client import LLMResponse, LLMUsage
        mod = _routing_singleton_reset

        starved_resp = LLMResponse(
            text="",
            usage=LLMUsage(),
            finish_reason="length",
            reasoning="thinking forever...",
        )
        fake_client = MagicMock()
        fake_client.acreate = AsyncMock(return_value=starved_resp)
        with patch.object(mod, "_get_local_routing_client", return_value=fake_client):
            with caplog.at_level(logging.WARNING, logger="api.services.llm_client"):
                text = await mod.generate_text("hi", max_tokens=10)
        assert text == ""
        assert any("reasoning starved" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_generate_text_no_warning_for_normal_response(self, _routing_singleton_reset, caplog):
        """A normal, non-starved response logs no reasoning warning."""
        import logging
        from unittest.mock import AsyncMock, MagicMock
        from api.services.llm_client import LLMResponse, LLMUsage
        mod = _routing_singleton_reset

        normal_resp = LLMResponse(text="42", usage=LLMUsage(), finish_reason="stop")
        fake_client = MagicMock()
        fake_client.acreate = AsyncMock(return_value=normal_resp)
        with patch.object(mod, "_get_local_routing_client", return_value=fake_client):
            with caplog.at_level(logging.WARNING, logger="api.services.llm_client"):
                text = await mod.generate_text("hi", max_tokens=10)
        assert text == "42"
        assert not any("reasoning starved" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_generate_text_forwards_reasoning_control(self, _routing_singleton_reset):
        """generate_text forwards enable_thinking/reasoning_effort to acreate() unchanged."""
        from unittest.mock import AsyncMock, MagicMock
        mod = _routing_singleton_reset

        fake_client = MagicMock()
        fake_resp = MagicMock(text="ok", reasoning_starved=False)
        fake_client.acreate = AsyncMock(return_value=fake_resp)
        with patch.object(mod, "_get_local_routing_client", return_value=fake_client):
            await mod.generate_text("hi", enable_thinking=False, reasoning_effort="low")
        kwargs = fake_client.acreate.await_args.kwargs
        assert kwargs["enable_thinking"] is False
        assert kwargs["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_generate_text_reasoning_control_unset_by_default(self, _routing_singleton_reset):
        """Callers that don't ask for reasoning control get None through to acreate()."""
        from unittest.mock import AsyncMock, MagicMock
        mod = _routing_singleton_reset

        fake_client = MagicMock()
        fake_resp = MagicMock(text="ok", reasoning_starved=False)
        fake_client.acreate = AsyncMock(return_value=fake_resp)
        with patch.object(mod, "_get_local_routing_client", return_value=fake_client):
            await mod.generate_text("hi")
        kwargs = fake_client.acreate.await_args.kwargs
        assert kwargs["enable_thinking"] is None
        assert kwargs["reasoning_effort"] is None

    @pytest.mark.asyncio
    async def test_generate_json_extracts(self, _routing_singleton_reset):
        """generate_json should parse the JSON object from the LLM response."""
        from unittest.mock import AsyncMock
        mod = _routing_singleton_reset

        with patch.object(mod, "generate_text", AsyncMock(return_value='{"answer": 7}')):
            result = await mod.generate_json("count please")
        assert result == {"answer": 7}

    @pytest.mark.asyncio
    async def test_routing_helpers_use_local_singleton(self, _routing_singleton_reset):
        """``_get_local_routing_client`` caches a LocalLLMClient even when backend=anthropic."""
        mod = _routing_singleton_reset

        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.llm_backend = "anthropic"
            mock_settings.local_llm_url = "http://localhost:8080"
            mock_settings.local_llm_timeout = 90
            client = mod._get_local_routing_client()
            assert isinstance(client, mod.LocalLLMClient)

    def test_get_local_routing_client_rebuilds_when_url_changes(self, _routing_singleton_reset):
        """Regression: the cached routing client must rebuild when
        settings.routing_llm_url changes mid-process (an operator edits
        LIFEOS_LOCAL_ROUTING_LLM_URL, or a test monkeypatches settings),
        not silently keep pointing at wherever it first resolved."""
        mod = _routing_singleton_reset

        with patch.object(mod, "settings") as mock_settings:
            mock_settings.routing_llm_url = "http://localhost:8080"
            first = mod._get_local_routing_client()
            assert first.base_url == "http://localhost:8080"

            mock_settings.routing_llm_url = "http://routing-box:9090"
            second = mod._get_local_routing_client()
            assert second is not first
            assert second.base_url == "http://routing-box:9090"

            # Same (new) URL on the next call returns the cached instance.
            third = mod._get_local_routing_client()
            assert third is second

    def test_is_available_swallows_errors(self, _routing_singleton_reset):
        """is_local_routing_llm_available returns False rather than propagating."""
        from unittest.mock import MagicMock
        mod = _routing_singleton_reset

        bad_client = MagicMock()
        bad_client.is_available.side_effect = RuntimeError("boom")
        with patch.object(mod, "_get_local_routing_client", return_value=bad_client):
            assert mod.is_local_routing_llm_available() is False


class TestDataClasses:
    """Tests for LLMResponse and LLMUsage dataclasses."""

    def test_llm_usage_defaults(self):
        """LLMUsage has sensible defaults."""
        from api.services.llm_client import LLMUsage
        usage = LLMUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0

    def test_llm_response_defaults(self):
        """LLMResponse has sensible defaults."""
        from api.services.llm_client import LLMResponse, LLMUsage
        resp = LLMResponse(text="hello", usage=LLMUsage())
        assert resp.text == "hello"
        assert resp.model == ""
        assert resp.finish_reason == ""
        assert resp.tool_calls is None
        assert resp.reasoning == ""
        assert resp.reasoning_starved is False

    def test_llm_usage_cache_fields_default_zero(self):
        """Cache-token fields default to 0 (only populated on the Anthropic backend)."""
        from api.services.llm_client import LLMUsage
        usage = LLMUsage()
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0


# ---- LocalLLMClient.astream reasoning handling ----


class _FakeSSEResponse:
    """Mimics the httpx streaming response astream() reads from."""

    def __init__(self, chunks: list[dict]):
        # Each chunk is serialized as its own "data: ..." SSE line, followed
        # by a final "data: [DONE]" sentinel, matching llama-server's format.
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


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient — only implements what astream() calls."""

    def __init__(self, resp):
        self.is_closed = False
        self._resp = resp

    def stream(self, method, url, **kwargs):
        return _FakeStreamCtx(self._resp)


def _done_chunk(finish_reason="stop", completion_tokens=1):
    return {
        "choices": [{"delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": completion_tokens, "total_tokens": completion_tokens + 1},
    }


class TestAStreamReasoning:
    """Tests for LocalLLMClient.astream()'s reasoning handling."""

    def _client_with_chunks(self, chunks: list[dict]):
        from api.services.llm_client import LocalLLMClient
        client = LocalLLMClient(base_url="http://fake:8080")
        client._async_client = _FakeAsyncClient(_FakeSSEResponse(chunks))
        return client

    @pytest.mark.asyncio
    async def test_plain_stream_unaffected(self):
        """A normal, non-reasoning stream is unaffected — every content delta
        is yielded as text, in order."""
        chunks = [
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": ", world!"}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "Hello, world!"
        done = next(e for e in events if e["type"] == "done")
        assert done["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_reasoning_content_delta_never_surfaced_as_text(self):
        """A delta carrying only reasoning_content (no content) never becomes
        a text event."""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "hmm, let's see..."}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text_events = [e for e in events if e["type"] == "text"]
        assert len(text_events) == 1
        assert text_events[0]["content"] == "42"

    @pytest.mark.asyncio
    async def test_inline_think_tag_split_across_chunks_is_stripped(self):
        """<think>...</think> split across multiple content deltas is still
        fully stripped — only text outside the tag reaches "text" events."""
        chunks = [
            {"choices": [{"delta": {"content": "<thi"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "nk>reasoning here"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "</thi"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "nk>The answer is 42."}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "The answer is 42."
        assert "reasoning here" not in text
        assert "<think>" not in text and "</think>" not in text

    @pytest.mark.asyncio
    async def test_unterminated_think_tag_at_truncation_not_leaked(self):
        """An unterminated <think> at the end of the stream (truncated by
        max_tokens) is discarded rather than flushed as text."""
        chunks = [
            {"choices": [{"delta": {"content": "<think>still reasoning"}, "finish_reason": None}]},
            _done_chunk(finish_reason="length", completion_tokens=2048),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text_events = [e for e in events if e["type"] == "text"]
        assert text_events == []
        done = next(e for e in events if e["type"] == "done")
        assert done["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_literal_angle_bracket_in_text_not_swallowed(self):
        """Ordinary text containing '<' (not part of a <think> tag) still
        reaches the caller in full once the stream ends."""
        chunks = [
            {"choices": [{"delta": {"content": "5 < 10 is true"}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "5 < 10 is true"

    @pytest.mark.asyncio
    async def test_closing_only_think_tag_is_not_stripped_in_streaming_path(self):
        """Streaming intentionally does NOT handle a closing-only </think>
        (no preceding opener), unlike the non-streaming path.

        Measured against the live llama-server deployment: a real streamed
        request produced 281 reasoning_content deltas and 0 occurrences of
        <think>/</think> in any content delta — llama.cpp's reasoning-format
        parser pulls chain-of-thought into the dedicated reasoning_content
        channel before it reaches content, so this shape does not occur in
        the stream on this deployment. Buffering to catch it anyway (behind
        a cap, or indefinitely) would cost real, visible latency on every
        streamed turn to guard a case that never happens. So this content
        passes through unstripped, letter for letter, rather than being
        held back — documenting the deliberate scope narrowing, not a
        regression. (_parse_response — the non-streaming path — still
        strips this shape; see TestParseResponse.
        test_closing_only_think_tag_leaks_no_reasoning.)"""
        chunks = [
            {"choices": [{"delta": {"content": "internal"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " reasoning</thi"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "nk>answer"}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "internal reasoning</think>answer"

    @pytest.mark.asyncio
    async def test_literal_think_tag_in_stream_not_corrupted(self):
        """Finding 2 (MAJOR), streaming version: a literal <think>...</think>
        pair after ordinary prose (not a leading prefix) must reach the
        caller untouched, not be split into reasoning."""
        chunks = [
            {"choices": [{"delta": {"content": "Use `<think>draft</think>`"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " as the example tag."}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = [e async for e in client.astream([{"role": "user", "content": "hi"}])]
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "Use `<think>draft</think>` as the example tag."

    @pytest.mark.asyncio
    async def test_normal_answer_first_delta_emitted_immediately(self):
        """A normal (non-reasoning) answer's first content delta is emitted
        as its own "text" event right away — not buffered pending a later
        chunk or the end of the stream. This is the latency regression that
        matters in practice: a delta whose leading characters aren't even a
        possible prefix of "<think>" is ruled out and streamed in real time,
        with no cap or indefinite hold."""
        chunks = [
            {"choices": [{"delta": {"content": "The answer is 42."}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " Anything else?"}, "finish_reason": None}]},
            _done_chunk(),
        ]
        client = self._client_with_chunks(chunks)
        events = []
        async for e in client.astream([{"role": "user", "content": "hi"}]):
            events.append(e)
            if e["type"] == "text":
                break
        assert events[0] == {"type": "text", "content": "The answer is 42."}


# ---- Anthropic prompt caching (#383 Phase 1) ----


def _fake_anthropic_usage(*, input_tokens=100, output_tokens=10,
                          cache_creation=0, cache_read=0):
    from types import SimpleNamespace
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


class _FakeAnthropicStream:
    """Minimal async context manager mimicking anthropic's streaming response."""

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


# A block list shaped exactly like build_system_prompt() output: a cached
# static block followed by an uncached dynamic block.
_SYSTEM_BLOCKS = [
    {"type": "text", "text": "STATIC PROMPT", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "Current date/time: ..."},
]
_CACHED_TOOLS = [
    {"name": "search", "description": "d", "input_schema": {"type": "object", "properties": {}},
     "cache_control": {"type": "ephemeral"}},
]


def _anthropic_client():
    from api.services.llm_client import AnthropicLLMClient
    try:
        return AnthropicLLMClient(api_key="sk-ant-test")
    except ImportError:
        pytest.skip("anthropic package not installed")


class TestAnthropicCaching:
    """The Anthropic backend must forward the system block list (and tool
    cache_control markers) to the SDK unflattened so prompt caching works,
    and must surface cache-token usage so caching is measurable."""

    def test_create_forwards_system_blocks_unflattened(self):
        """create() passes the block list through verbatim — NOT a joined string —
        so the cache_control marker reaches the API."""
        from types import SimpleNamespace
        client = _anthropic_client()
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="hi")],
                usage=_fake_anthropic_usage(cache_read=2600),
                model="claude-haiku-4-5",
                stop_reason="end_turn",
            )

        client._sync_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
        resp = client.create([{"role": "user", "content": "hi"}], system=_SYSTEM_BLOCKS)

        # System forwarded as a list, with cache_control intact (the bug was it
        # got flattened to a "\n\n".join(...) string, dropping cache_control).
        assert captured["system"] == _SYSTEM_BLOCKS
        assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
        # Cache-read tokens surfaced from the response usage.
        assert resp.usage.cache_read_input_tokens == 2600

    def test_create_still_accepts_plain_string_system(self):
        """A plain-string system prompt is forwarded as-is (backward compat)."""
        from types import SimpleNamespace
        client = _anthropic_client()
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                usage=_fake_anthropic_usage(),
                model="claude-haiku-4-5",
                stop_reason="end_turn",
            )

        client._sync_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
        client.create([{"role": "user", "content": "hi"}], system="You are helpful.")
        assert captured["system"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_astream_forwards_cache_control_and_captures_cache_tokens(self):
        """The streaming path (used by the chat orchestrator) forwards the system
        blocks and tool cache_control, and reports cache-read tokens on 'done'."""
        from types import SimpleNamespace
        client = _anthropic_client()
        captured = {}

        final = SimpleNamespace(
            content=[],
            usage=_fake_anthropic_usage(input_tokens=120, output_tokens=8, cache_read=2600),
            stop_reason="end_turn",
        )
        delta = SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(text="hello"),
        )

        def fake_stream(**kwargs):
            captured.update(kwargs)
            return _FakeAnthropicStream(final, deltas=[delta])

        client._async_client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))

        events = [
            e async for e in client.astream(
                [{"role": "user", "content": "hi"}],
                system=_SYSTEM_BLOCKS,
                tools=_CACHED_TOOLS,
            )
        ]

        # System + tools forwarded with cache_control intact.
        assert captured["system"] == _SYSTEM_BLOCKS
        assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}

        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert text == "hello"
        done = next(e for e in events if e["type"] == "done")
        assert done["usage"].cache_read_input_tokens == 2600

    @pytest.mark.asyncio
    async def test_astream_forwards_timeout_to_sdk(self):
        """astream accepts a per-request timeout and forwards it to the SDK.

        Regression for #385: the agent loop's synthesis round calls
        astream(..., timeout=180), which used to raise TypeError on the
        Anthropic backend because the method had no timeout parameter.
        """
        from types import SimpleNamespace
        client = _anthropic_client()
        captured = {}
        final = SimpleNamespace(
            content=[], usage=_fake_anthropic_usage(), stop_reason="end_turn")

        def fake_stream(**kwargs):
            captured.update(kwargs)
            return _FakeAnthropicStream(final)

        client._async_client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))
        _ = [e async for e in client.astream([{"role": "user", "content": "hi"}], timeout=180)]
        assert captured["timeout"] == 180
