"""Tool catalog and dispatcher for the local executor.

The local executor exposes a small fixed set of operating-system tools
(Read/Write/Edit/Bash/WebFetch/WebSearch) plus the full LifeOS MCP tool
surface proxied through `LifeOSMCPServer._call_api`. The agent sees Anthropic-
style tool definitions; the dispatcher routes calls to Python handlers.

WebSearch is stubbed — LifeOS doesn't ship a built-in search backend in
Issue C. The tool definition is exposed so the agent knows the affordance
exists; the handler returns a structured "not configured" reply so the
agent can pivot rather than hang.

No sandboxing per the user's design decision: the worker runs with the
operator's full filesystem and shell access. This is intentional and is
called out in `AGENTS.md` and the setup guide.

(#925) When a task names a working directory, Read/Write/Edit resolve
`file_path` against it and reject anything that escapes via `..`
traversal or a symlink (`_resolve_within_base`), and Bash runs with it as
`cwd`. That's a path-resolution guard, not a sandbox: a Bash command can
still `cd` elsewhere or pass an absolute path outside the working
directory and LifeOS won't stop it — only Read/Write/Edit's own path
argument is checked. See `local_executor._resolve_task_working_dir` for
where the directory itself is validated before any of this runs.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


logger = logging.getLogger(__name__)


# How much of a file we'll surface to the model at once. Beyond this we
# truncate with a footer pointing at the file path so the agent can re-read.
_MAX_READ_BYTES = 200_000
_MAX_BASH_OUTPUT_BYTES = 32_000
_MAX_WEBFETCH_BYTES = 200_000


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format)
# ---------------------------------------------------------------------------

STANDARD_TOOLS: list[dict[str, Any]] = [
    {
        "name": "Read",
        "description": "Read the contents of a file from the operator's filesystem. Pass an absolute path. Returns the text content (truncated for large files).",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "Absolute path to a file"}},
            "required": ["file_path"],
        },
    },
    {
        "name": "Write",
        "description": "Write (or overwrite) a file. Pass an absolute path and the new content. Existing parents must already exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "Edit",
        "description": "Replace an exact substring in a file. The old_string must appear exactly once in the file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "Bash",
        "description": "Run a shell command (sh/bash) and return its stdout + stderr. No sandbox. Long output is truncated.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 60},
            },
            "required": ["command"],
        },
    },
    {
        "name": "WebFetch",
        "description": "Fetch a URL and return the body as text. Large responses are truncated.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "WebSearch",
        "description": "Run a web search. NOTE: this LifeOS install does not yet have a search backend configured. The tool returns 'not configured' until a backend is wired up.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "sleep",
        "description": "Yield control until a number of seconds has elapsed. The session is paused (not idle-billed) and the worker resumes it when the timer expires. Use this when waiting on an external state change rather than busy-looping.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "minimum": 1},
                "reason": {"type": "string", "description": "Why you're sleeping (one short sentence)"},
            },
            "required": ["seconds"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch results
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Output of a single tool call."""
    output: str         # what the agent sees
    is_error: bool = False
    yield_seconds: int | None = None  # set by `sleep` to instruct the executor


# ---------------------------------------------------------------------------
# Standard tool handlers
# ---------------------------------------------------------------------------

def _resolve_within_base(file_path: str, base_dir: str | None) -> tuple[Path | None, str | None]:
    """Resolve `file_path` against `base_dir`, when one is named, and
    reject anything that escapes it via `..` traversal or a symlink.

    A relative `file_path` is joined onto `base_dir`; an absolute one is
    accepted only when its resolved form still falls inside `base_dir`.
    Resolution is realpath-based (`Path.resolve()` follows symlinks and
    collapses `..`), so a path that only *looks* contained is caught here.

    `base_dir=None` (no working directory named on the task) is a no-op —
    `file_path` resolves exactly as it always has, byte-identical to
    before #925.
    """
    p = Path(file_path)
    if base_dir is None:
        return p, None
    base = Path(base_dir).resolve()
    candidate = p if p.is_absolute() else (base / p)
    resolved = candidate.resolve()
    if resolved != base and base not in resolved.parents:
        return None, f"path escapes working directory {base_dir}: {file_path}"
    return resolved, None


def _tool_read(args: dict, base_dir: str | None = None) -> ToolResult:
    file_path = args.get("file_path", "")
    if not file_path:
        return ToolResult("Read requires file_path", is_error=True)
    p, escape_error = _resolve_within_base(file_path, base_dir)
    if escape_error:
        return ToolResult(escape_error, is_error=True)
    if not p.exists():
        return ToolResult(f"file not found: {file_path}", is_error=True)
    if not p.is_file():
        return ToolResult(f"not a file: {file_path}", is_error=True)
    try:
        data = p.read_bytes()
    except Exception as e:
        return ToolResult(f"read failed: {e}", is_error=True)
    if len(data) > _MAX_READ_BYTES:
        text = data[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
        text += f"\n\n[truncated: file is {len(data)} bytes; showed first {_MAX_READ_BYTES}]"
        return ToolResult(text)
    return ToolResult(data.decode("utf-8", errors="replace"))


def _tool_write(args: dict, base_dir: str | None = None) -> ToolResult:
    file_path = args.get("file_path", "")
    content = args.get("content", "")
    if not file_path:
        return ToolResult("Write requires file_path", is_error=True)
    p, escape_error = _resolve_within_base(file_path, base_dir)
    if escape_error:
        return ToolResult(escape_error, is_error=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return ToolResult(f"write failed: {e}", is_error=True)
    return ToolResult(f"wrote {len(content)} bytes to {file_path}")


def _tool_edit(args: dict, base_dir: str | None = None) -> ToolResult:
    file_path = args.get("file_path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not file_path or not old:
        return ToolResult("Edit requires file_path and a non-empty old_string", is_error=True)
    p, escape_error = _resolve_within_base(file_path, base_dir)
    if escape_error:
        return ToolResult(escape_error, is_error=True)
    if not p.exists():
        return ToolResult(f"file not found: {file_path}", is_error=True)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(f"read failed: {e}", is_error=True)
    count = text.count(old)
    if count == 0:
        return ToolResult("old_string not found in file", is_error=True)
    if count > 1:
        return ToolResult(
            f"old_string occurs {count} times — must be unique; widen the context",
            is_error=True,
        )
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
    except Exception as e:
        return ToolResult(f"write failed: {e}", is_error=True)
    return ToolResult(f"replaced 1 occurrence in {file_path}")


def _tool_bash(args: dict, base_dir: str | None = None) -> ToolResult:
    command = args.get("command", "")
    timeout = int(args.get("timeout_seconds", 60))
    if not command:
        return ToolResult("Bash requires command", is_error=True)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            # None (no working directory named) is subprocess.run's own
            # default — byte-identical to before #925 in that case. A
            # named `cwd` only bounds the command's *starting* directory;
            # the command itself can still `cd` elsewhere or touch an
            # absolute path outside it (see module docstring).
            cwd=base_dir,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"command timed out after {timeout}s", is_error=True)
    except Exception as e:
        return ToolResult(f"command failed to start: {e}", is_error=True)
    out = completed.stdout or ""
    err = completed.stderr or ""
    combined = out
    if err:
        combined += "\n[stderr]\n" + err
    if len(combined) > _MAX_BASH_OUTPUT_BYTES:
        combined = combined[:_MAX_BASH_OUTPUT_BYTES] + "\n\n[truncated]"
    if completed.returncode != 0:
        return ToolResult(f"exit {completed.returncode}\n{combined}", is_error=True)
    return ToolResult(combined or "(no output)")


def _tool_webfetch(args: dict, base_dir: str | None = None) -> ToolResult:
    del base_dir  # unused — WebFetch doesn't touch the local filesystem
    url = args.get("url", "")
    if not url:
        return ToolResult("WebFetch requires url", is_error=True)
    try:
        resp = httpx.get(url, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        return ToolResult(f"fetch failed: {e}", is_error=True)
    text = resp.text or ""
    if len(text) > _MAX_WEBFETCH_BYTES:
        text = text[:_MAX_WEBFETCH_BYTES] + "\n\n[truncated]"
    return ToolResult(text)


def _tool_websearch(args: dict, base_dir: str | None = None) -> ToolResult:
    # Search backend is intentionally not configured in Issue C. We surface
    # the tool so the agent can plan around it, but calling it returns a
    # structured "not configured" message rather than failing silently.
    del base_dir  # unused — WebSearch doesn't touch the local filesystem
    return ToolResult(
        "WebSearch is not configured on this LifeOS install. "
        "Use WebFetch with a specific URL, or pivot to a different approach.",
        is_error=True,
    )


STANDARD_HANDLERS: dict[str, Callable[..., ToolResult]] = {
    "Read": _tool_read,
    "Write": _tool_write,
    "Edit": _tool_edit,
    "Bash": _tool_bash,
    "WebFetch": _tool_webfetch,
    "WebSearch": _tool_websearch,
}


# ---------------------------------------------------------------------------
# Tool registry — combines standard tools with LifeOS MCP tools
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Standard tools + inter-agent tools + LifeOS MCP tools, unified.

    The LifeOS MCP tools come from instantiating `LifeOSMCPServer` (a
    self-contained class with `.tools` and `._call_api()`). Doing so is cheap
    and avoids duplicating the curated endpoint catalog.

    Inter-agent tools (`lifeos_agent_*`) need a caller's session_id + store
    handles. When `inter_agent_context` is provided, those tools are surfaced
    alongside the standard ones; otherwise they're omitted so the registry
    stays usable in stand-alone contexts (preflight, test fixtures).
    """

    def __init__(self, lifeos_mcp_server=None, inter_agent_context=None):
        if lifeos_mcp_server is None:
            # Lazy import — keeps the test surface light when MCP isn't needed.
            from mcp_server import LifeOSMCPServer
            lifeos_mcp_server = LifeOSMCPServer()
        self._mcp = lifeos_mcp_server
        self._mcp_tool_names: set[str] = {t["name"] for t in self._mcp.tools}
        self._inter_ctx = inter_agent_context

    def definitions(self) -> list[dict[str, Any]]:
        """Anthropic-format tool definitions for the agent's system message."""
        if self._inter_ctx is not None:
            from api.services.agent_worker.inter_agent import INTER_AGENT_TOOL_SCHEMAS
            return STANDARD_TOOLS + list(INTER_AGENT_TOOL_SCHEMAS) + list(self._mcp.tools)
        return STANDARD_TOOLS + list(self._mcp.tools)

    def dispatch(self, name: str, arguments: dict, base_dir: str | None = None) -> ToolResult:
        """Run one tool call. Returns a ToolResult.

        `base_dir` (#925) is the task's named working directory, if any —
        forwarded only to the standard Read/Write/Edit/Bash/WebFetch/
        WebSearch handlers (see their signatures); MCP and inter-agent
        tools don't touch the local filesystem and ignore it.
        """
        # `sleep` is special: tell the executor to yield rather than producing
        # immediate output.
        if name == "sleep":
            seconds = max(1, int(arguments.get("seconds", 60)))
            reason = arguments.get("reason", "")
            return ToolResult(
                output=f"sleeping {seconds}s — {reason}" if reason else f"sleeping {seconds}s",
                yield_seconds=seconds,
            )

        # Inter-agent tools (when wired): dispatch via inter_agent module.
        if self._inter_ctx is not None:
            from api.services.agent_worker import inter_agent
            if inter_agent.is_inter_agent_tool(name):
                # The local executor knows the caller's session_id via
                # context — ignore whatever the agent passed for that
                # field and use the authoritative value. The schema
                # declares caller_session_id required (so cloud agents
                # over MCP HTTP can supply it); for local we override.
                args = dict(arguments or {})
                args["caller_session_id"] = self._inter_ctx.caller_session_id
                payload = inter_agent.dispatch(self._inter_ctx, name, args)
                # `yield_until` and `lifeos_agent_user_ask` both end the executor
                # turn — the first waits for child completion, the second for
                # a Telegram reply.
                yield_signal = (
                    name in ("lifeos_agent_yield_until", "lifeos_agent_user_ask")
                    and payload.get("ok")
                )
                return ToolResult(
                    json.dumps(payload),
                    is_error=not payload.get("ok", False),
                    yield_seconds=-1 if yield_signal else None,  # sentinel: "yield, no wake timer"
                )

        handler = STANDARD_HANDLERS.get(name)
        if handler is not None:
            try:
                return handler(arguments, base_dir=base_dir)
            except Exception as e:  # pragma: no cover — defensive
                logger.exception("tool %s raised: %s", name, e)
                return ToolResult(f"tool {name} crashed: {e}", is_error=True)

        if name in self._mcp_tool_names:
            try:
                data = self._mcp._call_api(name, arguments)
                formatted = self._mcp._format_response(name, data)
                # MCP _call_api signals errors by returning a dict with an
                # "error" key — surface that as an error result.
                is_error = isinstance(data, dict) and "error" in data
                return ToolResult(formatted, is_error=is_error)
            except Exception as e:
                logger.exception("LifeOS MCP tool %s raised: %s", name, e)
                return ToolResult(f"tool {name} crashed: {e}", is_error=True)

        return ToolResult(f"unknown tool: {name}", is_error=True)
