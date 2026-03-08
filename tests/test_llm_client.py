"""
Tests for the unified LLM client.

Covers tool format translation, message conversion, response parsing,
and singleton/backend switching behavior.
"""
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
            mock_settings.anthropic_model = "claude-haiku-4-5-latest"
            try:
                client = get_local_llm()
                assert isinstance(client, AnthropicLLMClient)
                assert client._model == "claude-haiku-4-5-latest"
            except ImportError:
                pytest.skip("anthropic package not installed")
        reset_local_llm()


# ---- LLMResponse / LLMUsage ----


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
