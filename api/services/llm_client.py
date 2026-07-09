"""
Unified LLM client for LifeOS.

Wraps both Claude (Anthropic) and local (OpenAI-compatible llama-server) backends
behind a common interface. Claude is the default; local model is available as a
fallback or for offline use.

The local model server runs at LIFEOS_LOCAL_LLM_URL (default http://localhost:8080)
and speaks the OpenAI chat completions API.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Token usage from an LLM response.

    The cache fields are populated only on the Anthropic backend (prompt
    caching); they stay 0 for the local/OpenAI backend, which has no caching.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class LLMResponse:
    """Non-streaming LLM response."""
    text: str
    usage: LLMUsage
    model: str = ""
    finish_reason: str = ""
    tool_calls: list[dict] | None = None


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool definitions to OpenAI function-calling format.

    Anthropic format:
        {"name": "x", "description": "...", "input_schema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": "x", "description": "...", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        schema = dict(tool.get("input_schema", {}))
        schema.pop("cache_control", None)
        func = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        }
        # Strip cache_control from the tool itself (Anthropic-specific)
        result.append({"type": "function", "function": func})
    return result


def openai_tool_calls_to_anthropic(tool_calls: list[dict]) -> list[dict]:
    """Convert OpenAI tool_calls response format to Anthropic-style blocks.

    OpenAI format (from response):
        {"id": "call_...", "type": "function", "function": {"name": "x", "arguments": "{...}"}}

    Returns blocks compatible with agent_loop's processing:
        {"id": "call_...", "name": "x", "input": {...}, "type": "tool_use"}
    """
    blocks = []
    for tc in tool_calls:
        func = tc.get("function", {})
        try:
            args = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        blocks.append(_ToolUseBlock(
            id=tc.get("id", ""),
            name=func.get("name", ""),
            input=args,
        ))
    return blocks


@dataclass
class _ToolUseBlock:
    """Mimics anthropic's ToolUseBlock for compatibility with agent_loop."""
    id: str
    name: str
    input: dict
    type: str = "tool_use"


class LocalLLMClient:
    """Client for the local OpenAI-compatible LLM server (llama-server)."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or getattr(settings, "local_llm_url", None) or "http://localhost:8080").rstrip("/")
        self.timeout = timeout or getattr(settings, "local_llm_timeout", 90)
        self._async_client: httpx.AsyncClient | None = None
        self._sync_client: httpx.Client | None = None

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        return self._async_client

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        return self._sync_client

    def _convert_message(self, msg: dict) -> dict | None:
        """Convert a single message from Anthropic format to OpenAI format."""
        role = msg.get("role", "user")
        content = msg.get("content")

        if content is None:
            return None

        # Simple string content
        if isinstance(content, str):
            return {"role": role, "content": content}

        # List of content blocks (Anthropic format)
        if isinstance(content, list):
            # Check if it's tool_result blocks (user message with tool results)
            if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                return self._convert_tool_results(content)

            # Check if it contains tool_use blocks (assistant message)
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            )
            if has_tool_use:
                return self._convert_assistant_with_tools(content)

            # Regular content blocks — extract text
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") in ("image", "document"):
                        parts.append("[attachment]")
            return {"role": role, "content": "\n".join(parts) if parts else ""}

        return {"role": role, "content": str(content)}

    def _convert_assistant_with_tools(self, content: list) -> dict:
        """Convert assistant message with tool_use blocks to OpenAI format."""
        text_parts = []
        tool_calls = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = "\n".join(text_parts) if text_parts else None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return msg

    def _convert_tool_results(self, content: list) -> list[dict]:
        """Convert Anthropic tool_result blocks to OpenAI tool messages.

        Returns a list because each tool result is a separate message in OpenAI format.
        """
        # This returns a list — caller should handle flattening
        messages = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": block.get("content", ""),
                })
        return messages  # type: ignore

    def _build_messages_list(self, messages: list[dict], system: str | list | None = None) -> list[dict]:
        """Build flat messages list, handling tool result expansion."""
        all_messages = []
        if system:
            if isinstance(system, list):
                sys_text = "\n\n".join(
                    block["text"] for block in system if block.get("type") == "text"
                )
            else:
                sys_text = system
            if sys_text:
                all_messages.append({"role": "system", "content": sys_text})

        for msg in messages:
            content = msg.get("content")
            # Check if this is a tool results message (list of tool_result blocks)
            if (
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and content[0].get("type") == "tool_result"
            ):
                converted = self._convert_tool_results(content)
                all_messages.extend(converted)
            else:
                converted = self._convert_message(msg)
                if converted:
                    all_messages.append(converted)

        return all_messages

    def create(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Synchronous chat completion."""
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": "local",
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)

        resp = self.sync_client.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    async def acreate(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Async chat completion."""
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": "local",
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)

        resp = await self.async_client.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    async def astream(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Async streaming chat completion.

        Yields events:
            {"type": "text", "content": "..."}     — text delta
            {"type": "tool_calls", "calls": [...]}  — tool calls (complete)
            {"type": "done", "usage": LLMUsage, "finish_reason": "..."}
        """
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": "local",
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)

        request_timeout = httpx.Timeout(timeout or self.timeout, connect=10.0) if timeout else None
        async with self.async_client.stream(
            "POST", "/v1/chat/completions", json=payload,
            **({"timeout": request_timeout} if request_timeout else {}),
        ) as resp:
            resp.raise_for_status()
            tool_calls_acc: dict[int, dict] = {}  # index -> accumulated tool call
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                finish_reason = choices[0].get("finish_reason")

                # Text content
                if delta.get("content"):
                    yield {"type": "text", "content": delta["content"]}

                # Tool calls (streamed incrementally)
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc_delta.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        acc = tool_calls_acc[idx]
                        if tc_delta.get("id"):
                            acc["id"] = tc_delta["id"]
                        func_delta = tc_delta.get("function", {})
                        if func_delta.get("name"):
                            acc["function"]["name"] = func_delta["name"]
                        if func_delta.get("arguments"):
                            acc["function"]["arguments"] += func_delta["arguments"]

                if finish_reason:
                    usage_data = chunk.get("usage", {})
                    usage = LLMUsage(
                        input_tokens=usage_data.get("prompt_tokens", 0),
                        output_tokens=usage_data.get("completion_tokens", 0),
                        total_tokens=usage_data.get("total_tokens", 0),
                    )
                    if tool_calls_acc:
                        calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                        yield {"type": "tool_calls", "calls": calls}
                    yield {"type": "done", "usage": usage, "finish_reason": finish_reason}

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse OpenAI chat completions response into LLMResponse."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(text="", usage=LLMUsage(), model=data.get("model", ""))

        choice = choices[0]
        message = choice.get("message", {})
        usage_data = data.get("usage", {})

        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = message["tool_calls"]

        return LLMResponse(
            text=message.get("content", "") or "",
            usage=LLMUsage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            model=data.get("model", ""),
            finish_reason=choice.get("finish_reason", ""),
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        """Check if the local LLM server is reachable."""
        try:
            resp = self.sync_client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def ais_available(self) -> bool:
        """Async check if the local LLM server is reachable."""
        try:
            resp = await self.async_client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False


class AnthropicLLMClient:
    """Client that wraps the Anthropic SDK behind the same interface as LocalLLMClient.

    Allows switching between local and Anthropic backends via LIFEOS_LLM_BACKEND.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required for Anthropic backend: pip install anthropic")
        self._api_key = api_key or getattr(settings, "anthropic_api_key", "")
        self._model = model or getattr(settings, "anthropic_model", "claude-haiku-4-5")
        self._sync_client = anthropic.Anthropic(api_key=self._api_key)
        self._async_client = anthropic.AsyncAnthropic(api_key=self._api_key)

    def _prepare_system(self, system: str | list | None) -> str | list | None:
        """Return the ``system`` value for the Anthropic SDK unchanged.

        A list of content blocks is forwarded as-is so the ``cache_control``
        markers set in build_system_prompt survive to the API and prompt
        caching stays active across turns. A plain string is returned as-is.
        (LocalLLMClient flattens a block list to a string instead — the
        OpenAI-compatible backend has no prompt caching.)
        """
        return system

    def create(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Synchronous chat completion via Anthropic API."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        system_param = self._prepare_system(system)
        if system_param:
            kwargs["system"] = system_param
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = self._sync_client.messages.create(**kwargs)
        return self._parse_anthropic_response(resp)

    async def acreate(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Async chat completion via Anthropic API."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        system_param = self._prepare_system(system)
        if system_param:
            kwargs["system"] = system_param
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = await self._async_client.messages.create(**kwargs)
        return self._parse_anthropic_response(resp)

    async def astream(
        self,
        messages: list[dict],
        *,
        system: str | list | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Async streaming via Anthropic API.

        Yields the same event format as LocalLLMClient.astream(). ``timeout``
        (seconds) sets a per-request timeout — the agent loop's synthesis round
        passes one, and LocalLLMClient.astream accepts the same kwarg.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        system_param = self._prepare_system(system)
        if system_param:
            kwargs["system"] = system_param
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if timeout is not None:
            # The SDK accepts float | httpx.Timeout; a bare float sets a uniform
            # per-request timeout. (LocalLLMClient wraps it with connect=10s.)
            kwargs["timeout"] = timeout

        async with self._async_client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            yield {"type": "text", "content": event.delta.text}

            # Get the final message for tool calls and usage
            msg = await stream.get_final_message()
            tool_calls = []
            for block in msg.content:
                if block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    })
            if tool_calls:
                yield {"type": "tool_calls", "calls": tool_calls}
            yield {
                "type": "done",
                "usage": LLMUsage(
                    input_tokens=msg.usage.input_tokens,
                    output_tokens=msg.usage.output_tokens,
                    total_tokens=msg.usage.input_tokens + msg.usage.output_tokens,
                    cache_creation_input_tokens=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_input_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
                ),
                "finish_reason": msg.stop_reason or "end_turn",
            }

    def _parse_anthropic_response(self, resp) -> LLMResponse:
        """Parse Anthropic response into LLMResponse."""
        text = ""
        tool_calls = None
        for block in resp.content:
            if block.type == "text":
                # Accumulate: a response may carry multiple text blocks (e.g. a
                # native web-search answer split at citation boundaries). Keeping
                # only the last would truncate the answer to its final fragment.
                text += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
                cache_creation_input_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            ),
            model=resp.model,
            finish_reason=resp.stop_reason or "",
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        """Check if Anthropic API is reachable."""
        return bool(self._api_key)

    async def ais_available(self) -> bool:
        """Async check if Anthropic API is reachable."""
        return bool(self._api_key)


# --- Singleton ---

_llm_client: LocalLLMClient | AnthropicLLMClient | None = None


def get_local_llm() -> LocalLLMClient | AnthropicLLMClient:
    """Get or create the LLM client singleton.

    Returns AnthropicLLMClient (default) or LocalLLMClient based on LIFEOS_LLM_BACKEND.
    Set LIFEOS_LLM_BACKEND=local in .env to use a local llama-server instead.
    """
    global _llm_client
    if _llm_client is None:
        backend = getattr(settings, "llm_backend", "anthropic").lower()
        if backend == "anthropic":
            logger.info("Using Anthropic LLM backend")
            _llm_client = AnthropicLLMClient()
        else:
            logger.info("Using local LLM backend at %s", settings.local_llm_url)
            _llm_client = LocalLLMClient()
    return _llm_client


_anthropic_client: AnthropicLLMClient | None = None


def get_anthropic_llm() -> AnthropicLLMClient:
    """Get or create a dedicated Anthropic client for specialist calls.

    Used by relationship insights, fact extraction, tone analysis, and web search
    where frontier model quality provides clear value. These always use the Claude
    API regardless of the LIFEOS_LLM_BACKEND setting.

    Pinned to Sonnet for quality — the orchestrator model (LIFEOS_ANTHROPIC_MODEL)
    is independent of this.
    """
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AnthropicLLMClient(model="claude-sonnet-4-20250514")
        logger.info("Created Anthropic client for specialist calls (claude-sonnet-4-20250514)")
    return _anthropic_client


def reset_local_llm() -> None:
    """Reset all singletons (for testing)."""
    global _llm_client, _anthropic_client, _routing_client
    _llm_client = None
    _anthropic_client = None
    _routing_client = None


# ============================================================================
# Routing / validation helpers — always go to the local llama-server.
#
# Query routing, fact filtering, and entity-cleanup auto-hide decisions used
# to call Ollama directly (separate runtime at :11434). They were never sent
# to the cloud and they're cheap enough to keep local even when the main
# orchestrator is on Anthropic. These helpers wrap LocalLLMClient with the
# small text / JSON helpers those callers actually need so the rest of the
# codebase doesn't need to think about Ollama vs llama-server.
# ============================================================================

_routing_client: LocalLLMClient | None = None


def _get_local_routing_client() -> LocalLLMClient:
    """Return a LocalLLMClient pinned to llama-server, regardless of LIFEOS_LLM_BACKEND.

    Distinct from ``get_local_llm`` because that one switches to Anthropic
    when the backend is set to ``anthropic``; routing/validation should stay
    local even then.
    """
    global _routing_client
    if _routing_client is None:
        _routing_client = LocalLLMClient()
    return _routing_client


def extract_json(text: str) -> dict:
    """Extract a JSON object from an LLM response.

    Handles raw JSON, ```json fenced blocks, plain ``` fences, and JSON
    embedded in surrounding prose. Returns the first balanced object.

    Raises:
        ValueError: if no parseable JSON object is found.
    """
    text = text or ""
    # Raw JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # ```json fenced block
    fenced = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    fenced_any = re.search(r'```\s*([\s\S]*?)\s*```', text)
    if fenced_any:
        try:
            return json.loads(fenced_any.group(1))
        except json.JSONDecodeError:
            pass

    # First balanced object in the text
    start = text.find('{')
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"Failed to extract JSON from LLM response: {text[:200]}…")


async def generate_text(
    prompt: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    timeout: float | None = None,
) -> str:
    """Generate raw text from the local LLM.

    Replaces ``OllamaClient.generate(...)`` for routing / validation callers.
    A per-call ``timeout`` uses a transient client so concurrent default-
    timeout calls aren't affected.
    """
    client = LocalLLMClient(timeout=timeout) if timeout is not None else _get_local_routing_client()
    response = await client.acreate(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.text or ""


async def generate_json(
    prompt: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    timeout: float | None = None,
) -> dict:
    """Generate a JSON dict from the local LLM.

    Replaces ``OllamaClient.generate_json(...)`` for routing / validation
    callers. Uses a low temperature by default for structured output.
    """
    text = await generate_text(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    return extract_json(text)


def is_local_routing_llm_available() -> bool:
    """Sync availability check for routing/validation callers."""
    try:
        return _get_local_routing_client().is_available()
    except Exception:
        return False


async def ais_local_routing_llm_available() -> bool:
    """Async availability check for routing/validation callers."""
    try:
        return await _get_local_routing_client().ais_available()
    except Exception:
        return False
