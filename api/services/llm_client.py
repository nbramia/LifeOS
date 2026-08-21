"""Provider-neutral LLM clients and model-profile resolution for LifeOS.

Anthropic and OpenAI-compatible providers share the same LifeOS response and
streaming contract. Legacy ``LIFEOS_LLM_BACKEND`` settings remain supported;
named provider/model profiles can be added through ``LIFEOS_LLM_PROVIDERS`` and
``LIFEOS_LLM_MODELS`` without changing the personal data layer.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMProviderConfig:
    """Provider connection details, independent of a selected model."""

    name: str
    type: str
    base_url: str = ""
    api_key: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMModelConfig:
    """Named model profile used by an operation or a chat turn."""

    name: str
    provider: str
    model: str


def _json_setting(value: str, setting_name: str) -> dict:
    """Parse an optional JSON registry setting without breaking startup."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("must be a JSON object")
        return parsed
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid %s: %s", setting_name, exc)
        return {}


def get_llm_registry() -> tuple[dict[str, LLMProviderConfig], dict[str, LLMModelConfig]]:
    """Resolve the configured provider and named-model registries.

    The legacy backend settings remain the fallback, so existing installations
    do not need migration. A provider's ``api_key_env`` is resolved only here;
    credentials never need to be stored in the registry itself.
    """
    providers: dict[str, LLMProviderConfig] = {
        "anthropic": LLMProviderConfig(
            name="anthropic", type="anthropic",
            api_key=getattr(settings, "anthropic_api_key", ""),
        ),
        "local": LLMProviderConfig(
            name="local", type="openai_compatible",
            base_url=getattr(settings, "local_llm_url", "http://localhost:8080"),
        ),
    }
    raw_providers = _json_setting(
        getattr(settings, "llm_providers_json", ""), "LIFEOS_LLM_PROVIDERS"
    )
    for name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            logger.warning("Ignoring provider %r: expected an object", name)
            continue
        api_key = str(raw.get("api_key", "") or "")
        api_key_env = str(raw.get("api_key_env", "") or "")
        if api_key_env:
            api_key = os.environ.get(api_key_env, "")
        headers = raw.get("headers", {})
        providers[str(name)] = LLMProviderConfig(
            name=str(name),
            type=str(raw.get("type", "openai_compatible")),
            base_url=str(raw.get("base_url", "") or ""),
            api_key=api_key,
            headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        )

    models: dict[str, LLMModelConfig] = {}
    raw_models = _json_setting(
        getattr(settings, "llm_models_json", ""), "LIFEOS_LLM_MODELS"
    )
    for name, raw in raw_models.items():
        if not isinstance(raw, dict) or not raw.get("provider") or not raw.get("model"):
            logger.warning("Ignoring model profile %r: provider and model are required", name)
            continue
        models[str(name)] = LLMModelConfig(
            name=str(name), provider=str(raw["provider"]), model=str(raw["model"])
        )

    if "default" not in models:
        backend = getattr(settings, "llm_backend", "anthropic").lower()
        if backend == "anthropic":
            models["default"] = LLMModelConfig(
                name="default", provider="anthropic",
                model=getattr(settings, "anthropic_model", "claude-haiku-4-5"),
            )
        else:
            models["default"] = LLMModelConfig(
                name="default", provider="local",
                model=getattr(settings, "local_llm_model", "local"),
            )
    if "specialist" not in models:
        default = models["default"]
        # Preserve the historical Sonnet specialist when running the legacy
        # Anthropic backend. With a custom registry, specialist work follows
        # the configured default until explicitly assigned another profile.
        specialist_model = (
            getattr(settings, "anthropic_specialist_model", default.model)
            if default.provider == "anthropic" else default.model
        )
        models["specialist"] = LLMModelConfig(
            name="specialist", provider=default.provider, model=specialist_model
        )
    if "fast" not in models:
        models["fast"] = models["default"]
    return providers, models


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
    reasoning: str = ""

    @property
    def reasoning_starved(self) -> bool:
        """True when the model spent its whole token budget on reasoning and
        never got to an answer.

        Reasoning-capable models (e.g. Gemma 4 26B-A4B via llama-server) can
        return chain-of-thought separately from the answer — via a dedicated
        ``reasoning_content`` field or inline ``<think>...</think>`` tags —
        both stripped into ``reasoning`` by ``_parse_response``. If the token
        budget runs out before the model reaches its answer, ``text`` ends up
        empty with ``finish_reason == "length"``, which is otherwise
        indistinguishable from a legitimate empty response. Computed rather
        than stored so it can't drift out of sync with the fields it reads.
        """
        return not self.text and bool(self.reasoning) and self.finish_reason == "length"


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


def _reasoning_control_payload(
    enable_thinking: bool | None, reasoning_effort: str | None
) -> dict[str, Any]:
    """Optional per-request reasoning-control fields for the llama-server payload.

    ``enable_thinking`` is sent as ``chat_template_kwargs: {"enable_thinking": ...}``
    (llama-server's jinja template switch); ``reasoning_effort`` is forwarded
    as-is — llama-server parses it directly (server-common.cpp) and treats
    ``"none"`` as its own way to disable reasoning, separate from
    ``enable_thinking``. Both are ``None`` by default, in which case this
    returns ``{}`` — no new keys appear in the request body unless a caller
    explicitly asks, so an unset call stays byte-identical to the payload
    before this existed.
    """
    extra: dict[str, Any] = {}
    if enable_thinking is not None:
        extra["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    if reasoning_effort is not None:
        extra["reasoning_effort"] = reasoning_effort
    return extra


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _extract_inline_thinking(content: str) -> tuple[str, str]:
    """Strip a leading inline ``<think>...</think>`` reasoning prefix out of
    ``content``.

    Reasoning is always a PREFIX of the response, never mid-text — a real
    reasoning block opens at the very start of the content (modulo leading
    whitespace). A ``<think>``/``</think>`` pair appearing after ordinary
    prose (e.g. a user asking about the tag itself) is just text and is left
    completely alone.

    Returns ``(content, reasoning)``. Two cases open a reasoning prefix:

    1. ``content`` starts with ``<think>`` (after stripping leading
       whitespace): everything up to the matching ``</think>`` is reasoning,
       or — if the tag is never closed (the response ran out of tokens
       mid-thought) — everything to the end of the string.
    2. ``content`` has no leading ``<think>`` but contains a ``</think>``
       with no ``<think>`` anywhere before it: many llama.cpp jinja
       templates pre-fill the opening ``<think>`` into the *prompt* rather
       than the model's output, so the model emits only the closing tag.
       Everything before that first ``</think>`` is reasoning; everything
       after is the answer.

    If neither case applies, ``content`` is returned completely untouched
    (no ``.strip()``, nothing) so ordinary non-reasoning responses are
    byte-for-byte unchanged.
    """
    if not content:
        return content, ""

    stripped = content.lstrip()
    if stripped.startswith(_THINK_OPEN):
        after_open = stripped[len(_THINK_OPEN):]
        close_idx = after_open.find(_THINK_CLOSE)
        if close_idx == -1:
            return "", after_open.strip()
        return after_open[close_idx + len(_THINK_CLOSE):].strip(), after_open[:close_idx].strip()

    close_idx = content.find(_THINK_CLOSE)
    if close_idx != -1:
        open_idx = content.find(_THINK_OPEN)
        if open_idx == -1 or open_idx > close_idx:
            return content[close_idx + len(_THINK_CLOSE):].strip(), content[:close_idx].strip()

    return content, ""


def _split_reasoning(content: str, reasoning_field: str) -> tuple[str, str]:
    """Combine a dedicated ``reasoning_content`` field with any inline
    ``<think>...</think>`` tags found in ``content`` into one reasoning
    string, separate from the answer text.
    """
    content, inline_reasoning = _extract_inline_thinking(content or "")
    parts = [p for p in (reasoning_field or "", inline_reasoning) if p]
    return content, "\n\n".join(parts)


def _longest_partial_tag_suffix(buffer: str, tag: str) -> int:
    """Length of the longest suffix of ``buffer`` that could be the start of
    ``tag`` — i.e. a tag possibly split across two stream chunks."""
    max_len = min(len(buffer), len(tag) - 1)
    for length in range(max_len, 0, -1):
        if tag.startswith(buffer[-length:]):
            return length
    return 0


# Phase values for _consume_think_stream's state machine:
#   "undetermined" — not yet known whether the stream opens with a leading
#     <think>. Bounded to at most len("<think>") - 1 = 6 buffered characters:
#     as soon as the leading non-whitespace text stops being a viable prefix
#     of "<think>", it's ruled out and we go straight to passthrough.
#   "in_think"     — inside a leading <think>...</think> block.
#   "passthrough"  — resolved: no reasoning prefix (or the prefix has already
#     closed). Everything from here on, tags included, is emitted verbatim —
#     a tag appearing later in the stream is just text, never reasoning.
#
# Unlike _extract_inline_thinking (the non-streaming path), this does NOT
# also detect a closing-only "</think>" with no preceding opener. Measured
# against the live llama-server deployment (Gemma 4 26B-A4B, --jinja,
# default --reasoning-format): a real streamed request produced 281
# reasoning_content deltas and 0 occurrences of "<think>"/"</think>" in any
# content delta. llama.cpp's reasoning-format parser pulls chain-of-thought
# into the dedicated reasoning_content channel before it ever reaches
# content, so on this deployment inline reasoning tags simply don't occur in
# the stream. Handling the closing-only case here would mean buffering
# either indefinitely or behind an arbitrary cap on every single streamed
# turn — a real, visible latency cost — to guard against a case this server
# doesn't produce. The non-streaming path keeps handling it (no latency
# cost there, and it's still reachable: a different server, a
# --reasoning-format none config, or a non-streaming client).


def _consume_think_stream(buffer: str, phase: str) -> tuple[str, str, str]:
    """Incrementally resolve a growing streamed text buffer against the
    leading-reasoning-prefix rule used by ``_extract_inline_thinking``,
    restricted to the leading-``<think>`` case (see module note above for
    why the closing-only case is intentionally not handled here).

    Returns ``(emit_text, phase, remainder)``: ``emit_text`` is safe to
    yield to the caller now; ``remainder`` must be carried over to the next
    chunk (e.g. a ``<think>``/``</think>`` tag split across two chunks isn't
    fully visible yet, so it's held back rather than emitted or discarded).
    """
    if phase == "passthrough":
        return buffer, phase, ""

    if phase == "in_think":
        idx = buffer.find(_THINK_CLOSE)
        if idx == -1:
            keep = _longest_partial_tag_suffix(buffer, _THINK_CLOSE)
            return "", phase, (buffer[-keep:] if keep else "")
        return buffer[idx + len(_THINK_CLOSE):], "passthrough", ""

    # phase == "undetermined"
    stripped = buffer.lstrip()
    if stripped.startswith(_THINK_OPEN):
        after_open = stripped[len(_THINK_OPEN):]
        idx = after_open.find(_THINK_CLOSE)
        if idx == -1:
            return "", "in_think", after_open
        return after_open[idx + len(_THINK_CLOSE):], "passthrough", ""
    if not stripped or _THINK_OPEN.startswith(stripped):
        # All whitespace so far, or still a viable prefix of "<think>" —
        # keep waiting to see it confirmed or ruled out.
        return "", "undetermined", buffer

    # Ruled out — this stream does not open with "<think>". Resolve
    # immediately and stream in real time from here on.
    return buffer, "passthrough", ""


class LocalLLMClient:
    """Client for the local OpenAI-compatible LLM server (llama-server)."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        *,
        model: str = "local",
        api_key: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.base_url = (base_url or getattr(settings, "local_llm_url", None) or "http://localhost:8080").rstrip("/")
        self.timeout = timeout or getattr(settings, "local_llm_timeout", 90)
        self.model = model
        self.api_key = api_key
        self.headers = dict(headers or {})
        if self.api_key:
            self.headers.setdefault("Authorization", f"Bearer {self.api_key}")
        self._async_client: httpx.AsyncClient | None = None
        self._sync_client: httpx.Client | None = None

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers=self.headers,
            )
        return self._async_client

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers=self.headers,
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
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Synchronous chat completion.

        ``enable_thinking``/``reasoning_effort`` control reasoning for this
        request only — see ``_reasoning_control_payload``. Both default to
        ``None`` (unset), which adds no new keys to the request body.
        """
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)
        payload.update(_reasoning_control_payload(enable_thinking, reasoning_effort))

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
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Async chat completion.

        ``enable_thinking``/``reasoning_effort`` control reasoning for this
        request only — see ``_reasoning_control_payload``. Both default to
        ``None`` (unset), which adds no new keys to the request body.
        """
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)
        payload.update(_reasoning_control_payload(enable_thinking, reasoning_effort))

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
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Async streaming chat completion.

        Yields events:
            {"type": "text", "content": "..."}     — text delta
            {"type": "tool_calls", "calls": [...]}  — tool calls (complete)
            {"type": "done", "usage": LLMUsage, "finish_reason": "..."}

        No ``{"type": "usage_update", ...}`` event (#629): unlike
        AnthropicLLMClient.astream, this backend's OpenAI-compatible
        streaming protocol carries no mid-stream usage signal at all, so
        there's nothing to surface incrementally — usage is only ever known
        at the terminal "done" chunk above. A caller that tracks a running
        total from "usage_update" events (to credit a mid-round
        cancellation for tokens already generated) gets no such credit on
        this backend; that's a real, documented gap, not a bug to fix here.

        ``enable_thinking``/``reasoning_effort`` control reasoning for this
        request only — see ``_reasoning_control_payload``. Both default to
        ``None`` (unset), which adds no new keys to the request body.

        Reasoning models may inline chain-of-thought as a leading
        <think>...</think> prefix in the content delta. That's buffered and
        stripped below via _consume_think_stream (never yielded as text); a
        <think>/</think> appearing later, mid-answer, is left alone since
        reasoning is only ever a leading prefix. _consume_think_stream does
        NOT also handle a closing-only "</think>" with no preceding opener
        (see its module-level comment) — on this deployment reasoning
        arrives via the separate ``reasoning_content`` delta field instead.
        That field is only checked for presence (to warn on starvation, see
        below), never reassembled or surfaced — it still never reaches a
        "text" event.

        If the stream ends with ``finish_reason == "length"``, reasoning
        deltas were seen, and no text was ever emitted, the whole token
        budget was spent on chain-of-thought with no answer to show for it —
        a warning is logged (not yielded; this never changes what the
        caller receives).
        """
        all_messages = self._build_messages_list(messages, system)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = _anthropic_tools_to_openai(tools)
        payload.update(_reasoning_control_payload(enable_thinking, reasoning_effort))

        request_timeout = httpx.Timeout(timeout or self.timeout, connect=10.0) if timeout else None
        async with self.async_client.stream(
            "POST", "/v1/chat/completions", json=payload,
            **({"timeout": request_timeout} if request_timeout else {}),
        ) as resp:
            resp.raise_for_status()
            tool_calls_acc: dict[int, dict] = {}  # index -> accumulated tool call
            think_buffer = ""  # holds text not yet resolved against the reasoning-prefix rule
            think_phase = "undetermined"
            # Starvation tracking (#567): reasoning_content deltas are never
            # surfaced as a "text" event (see docstring), which made the
            # failure mode silent — a reasoning model can burn its entire
            # max_tokens budget on chain-of-thought and stream nothing back.
            # These two flags don't change what's yielded; they only decide
            # whether to log a warning at "done".
            reasoning_seen = False
            text_emitted = False
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

                # Text content — strip a leading <think>...</think> reasoning
                # prefix before it reaches a "text" event (delta.reasoning_content,
                # the other place reasoning shows up, is intentionally never
                # read here, so it can't leak either).
                if delta.get("content"):
                    think_buffer += delta["content"]
                    emit_text, think_phase, think_buffer = _consume_think_stream(think_buffer, think_phase)
                    if emit_text:
                        text_emitted = True
                        yield {"type": "text", "content": emit_text}

                # reasoning_content deltas are never turned into text (see
                # docstring) — tracked only so a starved response (all
                # reasoning, no answer) can be logged below instead of
                # silently returning nothing.
                if delta.get("reasoning_content"):
                    reasoning_seen = True

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
                    # Flush whatever's left in the buffer — unless it's an
                    # unterminated <think> (truncated mid-thought), which is
                    # reasoning, not an answer, so it's discarded rather than
                    # leaked as text. A buffer still "undetermined" at this
                    # point never resolved into a reasoning prefix at all (no
                    # <think>, no </think> ever showed up), so per the
                    # leading-prefix rule it's just ordinary text and is
                    # flushed rather than dropped.
                    if think_buffer and think_phase != "in_think":
                        text_emitted = True
                        yield {"type": "text", "content": think_buffer}
                    think_buffer = ""

                    if not text_emitted and reasoning_seen and finish_reason == "length":
                        logger.warning(
                            "Streamed LLM response reasoning starved: spent the "
                            "entire %d-token max_tokens budget on chain-of-thought "
                            "and streamed no answer text (finish_reason=length). "
                            "Disable thinking for this call (enable_thinking=False) "
                            "or raise max_tokens.",
                            payload.get("max_tokens", 0),
                        )

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

        text, reasoning = _split_reasoning(
            message.get("content", "") or "",
            message.get("reasoning_content", "") or "",
        )

        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            model=data.get("model", ""),
            finish_reason=choice.get("finish_reason", ""),
            tool_calls=tool_calls,
            reasoning=reasoning,
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

        Yields the same event format as LocalLLMClient.astream(), plus one
        this backend alone can produce (#629):

            {"type": "usage_update", "usage": LLMUsage}  — cumulative
            usage-so-far for the in-flight response, not a per-event delta.

        Anthropic's wire protocol carries a running usage count via the
        ``message_start`` and ``message_delta`` SSE events — the former as
        soon as the request is accepted (before any output token exists),
        the latter as generation proceeds. Surfacing them lets a caller
        that's cancelled mid-round credit the tokens that were already
        billed instead of reporting nothing for the whole round.
        LocalLLMClient.astream never yields this event — see its docstring
        for why that backend can't.

        ``timeout`` (seconds) sets a per-request timeout — the agent loop's
        synthesis round passes one, and LocalLLMClient.astream accepts the
        same kwarg.
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
            # (#629) message_start's usage always carries all four fields
            # (Anthropic's `Usage` type), but message_delta's usage has
            # input_tokens/cache_* typed Optional and frequently absent —
            # only output_tokens is guaranteed there. Carry the last-known
            # value forward per field so an event that omits one doesn't
            # look like it dropped to zero in the yielded snapshot.
            last_input_tokens = 0
            last_cache_creation = 0
            last_cache_read = 0
            async for event in stream:
                if not hasattr(event, "type"):
                    continue
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield {"type": "text", "content": event.delta.text}
                elif event.type == "message_start":
                    u = event.message.usage
                    last_input_tokens = u.input_tokens
                    last_cache_creation = u.cache_creation_input_tokens or 0
                    last_cache_read = u.cache_read_input_tokens or 0
                    yield {
                        "type": "usage_update",
                        "usage": LLMUsage(
                            input_tokens=last_input_tokens,
                            output_tokens=u.output_tokens,
                            total_tokens=last_input_tokens + u.output_tokens,
                            cache_creation_input_tokens=last_cache_creation,
                            cache_read_input_tokens=last_cache_read,
                        ),
                    }
                elif event.type == "message_delta":
                    u = event.usage
                    if u.input_tokens is not None:
                        last_input_tokens = u.input_tokens
                    if u.cache_creation_input_tokens is not None:
                        last_cache_creation = u.cache_creation_input_tokens
                    if u.cache_read_input_tokens is not None:
                        last_cache_read = u.cache_read_input_tokens
                    yield {
                        "type": "usage_update",
                        "usage": LLMUsage(
                            input_tokens=last_input_tokens,
                            output_tokens=u.output_tokens,
                            total_tokens=last_input_tokens + u.output_tokens,
                            cache_creation_input_tokens=last_cache_creation,
                            cache_read_input_tokens=last_cache_read,
                        ),
                    }

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


class OpenAICompatibleLLMClient(LocalLLMClient):
    """Client for any provider exposing the OpenAI chat-completions shape."""


def get_llm(
    profile: str = "default",
    *,
    model: str | None = None,
    provider: str | None = None,
) -> LocalLLMClient | AnthropicLLMClient:
    """Create a client from a named provider/model profile.

    With no registry configured this resolves to the historical Anthropic or
    local backend, preserving existing deployments.
    """
    providers, models = get_llm_registry()
    selected = models.get(profile) or models["default"]
    provider_name = provider or selected.provider
    model_name = model or selected.model
    if provider and not models.get(profile):
        if provider_name == "local":
            model_name = getattr(settings, "local_llm_model", model_name)
    provider_config = providers.get(provider_name)
    if provider_config is None:
        raise ValueError(f"Unknown LLM provider {provider_name!r}")

    if provider_config.type == "anthropic":
        return AnthropicLLMClient(api_key=provider_config.api_key, model=model_name)
    if provider_config.type in ("openai", "openai_compatible", "local"):
        return OpenAICompatibleLLMClient(
            base_url=provider_config.base_url,
            model=model_name,
            api_key=provider_config.api_key,
            headers=provider_config.headers,
        )
    raise ValueError(f"Unsupported LLM provider type {provider_config.type!r}")


# --- Singleton ---

_llm_client: LocalLLMClient | AnthropicLLMClient | None = None


def get_local_llm() -> LocalLLMClient | AnthropicLLMClient:
    """Get or create the LLM client singleton.

    Returns AnthropicLLMClient (default) or LocalLLMClient based on LIFEOS_LLM_BACKEND.
    Set LIFEOS_LLM_BACKEND=local in .env to use a local llama-server instead.
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = get_llm()
        logger.info("Using configured LLM client %s", type(_llm_client).__name__)
    return _llm_client


_anthropic_client: AnthropicLLMClient | None = None


def get_anthropic_llm() -> AnthropicLLMClient:
    """Get or create a dedicated Anthropic client for specialist calls.

    Used by relationship insights, fact extraction, tone analysis, and web search
    where frontier model quality provides clear value. These always use the Claude
    API regardless of the LIFEOS_LLM_BACKEND setting.

    Sonnet-tier for quality — resolved from LIFEOS_ANTHROPIC_SPECIALIST_MODEL
    (default claude-sonnet-5), independent of the orchestrator model
    (LIFEOS_ANTHROPIC_MODEL). Was previously hardcoded to the dated snapshot
    claude-sonnet-4-20250514, which retired and 404'd every caller (#470).
    """
    global _anthropic_client
    if _anthropic_client is None:
        model = settings.anthropic_specialist_model
        _anthropic_client = AnthropicLLMClient(model=model)
        logger.info("Created Anthropic client for specialist calls (%s)", model)
    return _anthropic_client


def reset_local_llm() -> None:
    """Reset all singletons (for testing)."""
    global _llm_client, _anthropic_client, _routing_client, _routing_client_url
    _llm_client = None
    _anthropic_client = None
    _routing_client = None
    _routing_client_url = None


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
_routing_client_url: str | None = None  # URL the cached client above was built against


def _get_local_routing_client() -> LocalLLMClient:
    """Return a LocalLLMClient pinned to llama-server, regardless of LIFEOS_LLM_BACKEND.

    Distinct from ``get_local_llm`` because that one switches to Anthropic
    when the backend is set to ``anthropic``; routing/validation should stay
    local even then.

    Cached per resolved URL rather than unconditionally: settings.routing_llm_url
    is configurable (#566), so if it changes after the first call — an operator
    edits LIFEOS_LOCAL_ROUTING_LLM_URL, or a test monkeypatches settings mid-process
    — the client is rebuilt against the new target instead of silently keeping
    traffic pinned to wherever it first resolved.
    """
    global _routing_client, _routing_client_url
    url = settings.routing_llm_url
    if _routing_client is None or _routing_client_url != url:
        _routing_client = LocalLLMClient(base_url=url)
        _routing_client_url = url
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
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Generate raw text from the local LLM.

    Replaces ``OllamaClient.generate(...)`` for routing / validation callers.
    A per-call ``timeout`` uses a transient client (still pinned to
    ``settings.routing_llm_url``, same as the cached singleton — a caller
    passing ``timeout`` must not silently fall back to the main chat model's
    URL) so concurrent default-timeout calls aren't affected.
    ``enable_thinking``/``reasoning_effort`` are forwarded to
    ``LocalLLMClient.acreate`` unchanged — see ``_reasoning_control_payload``;
    both default to ``None`` (unset).
    """
    client = (
        LocalLLMClient(base_url=settings.routing_llm_url, timeout=timeout)
        if timeout is not None
        else _get_local_routing_client()
    )
    response = await client.acreate(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
    )
    if response.reasoning_starved:
        logger.warning(
            "LLM reasoning starved the response: spent the entire %d-token budget "
            "on chain-of-thought and returned no answer (finish_reason=length). "
            "Consider raising max_tokens. reasoning=%r",
            max_tokens, response.reasoning[:200],
        )
    return response.text or ""


async def generate_json(
    prompt: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    timeout: float | None = None,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Generate a JSON dict from the local LLM.

    Replaces ``OllamaClient.generate_json(...)`` for routing / validation
    callers. Uses a low temperature by default for structured output.
    ``enable_thinking``/``reasoning_effort`` are forwarded to
    ``generate_text`` unchanged; both default to ``None`` (unset).
    """
    text = await generate_text(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
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
