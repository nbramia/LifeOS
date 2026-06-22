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
            mock_settings.anthropic_model = "claude-haiku-4-5"
            try:
                client = get_local_llm()
                assert isinstance(client, AnthropicLLMClient)
                assert client._model == "claude-haiku-4-5"
            except ImportError:
                pytest.skip("anthropic package not installed")
        reset_local_llm()

    def test_specialist_client_pinned_to_sonnet(self):
        """get_anthropic_llm() specialist client stays on Sonnet regardless of LIFEOS_ANTHROPIC_MODEL."""
        from api.services.llm_client import get_anthropic_llm, reset_local_llm, AnthropicLLMClient
        reset_local_llm()
        with patch("api.services.llm_client.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test-key"
            mock_settings.anthropic_model = "claude-haiku-4-5"
            try:
                client = get_anthropic_llm()
                assert isinstance(client, AnthropicLLMClient)
                assert client._model == "claude-sonnet-4-20250514"
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
    """Reset the routing-client singleton around each test so mutations don't leak."""
    from api.services import llm_client as mod
    prev = mod._routing_client
    mod._routing_client = None
    yield mod
    mod._routing_client = prev


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

    def test_llm_usage_cache_fields_default_zero(self):
        """Cache-token fields default to 0 (only populated on the Anthropic backend)."""
        from api.services.llm_client import LLMUsage
        usage = LLMUsage()
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0


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
