"""Discover, normalize, and price Codex CLI sessions for the /agents viz.

Codex stores each terminal session as an append-only JSONL at
`~/.codex/sessions/<year>/<month>/<day>/rollout-<datetime>-<uuid>.jsonl`.
The schema is OpenAI-flavored (`event_msg` subtypes for lifecycle,
`response_item` for content, `session_meta` for one-time setup, plus a
`turn_context` snapshot per turn) and quite different from the LifeOS
agent worker's `{ts, kind, payload}` shape.

This module is a read-only adapter that translates the Codex schema
into the LifeOS shape so the /agents route can union LifeOS + Claude
Code + Codex sources without forking its rendering logic.

Public surface mirrors `api.services.claude_code.session_ingest`:
- `discover_sessions(...)` — list session metadata for snapshot use
- `read_normalized_events(session_id)` — full normalized event list
- `to_session_dict(meta)` — snapshot row matching the agent-worker shape
- `build_snapshot(...)` — cached `(sessions, edges)` for the route
- `validate_session_id(...)` — path-traversal protection

Path-traversal protection is enforced on every `session_id` lookup.
The adapter never writes to Codex's data.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# Synthetic session_id prefix so the agents route can dispatch by source
# without ambiguity. Codex session UUIDs do not collide with LifeOS or
# Claude Code ids, but the prefix makes routing intent explicit.
CX_PREFIX = "cx:"

# Truncation cap for text previews (privacy + UI bandwidth).
_PAYLOAD_PREVIEW_MAX = 240

# A rollout file touched within the last 10 minutes reads as `running`
# even without a live-process signal — interactive sessions append in
# bursts and a tight threshold flips during natural pauses.
_RUNNING_MTIME_THRESHOLD = 600  # 10 min
# Anything modified within 24h is `inactive` (resumable). Older is
# treated as truly done.
_INACTIVE_MTIME_THRESHOLD = 86_400  # 24h

# Session id allowlist — Codex uses UUIDs (hex + dashes) but accept the
# Claude-Code-style character set so a future format change doesn't
# silently break discovery.
_VALID_SESSION_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_session_id(session_id: str) -> str:
    """Reject anything that could traverse outside the sessions dir.

    Strips the `cx:` prefix if present and returns the bare id. Raises
    `ValueError` for input containing path separators, `..`, or characters
    outside the allowed set.
    """
    if not session_id:
        raise ValueError("session_id is required")
    bare = session_id[len(CX_PREFIX):] if session_id.startswith(CX_PREFIX) else session_id
    if "/" in bare or "\\" in bare or ".." in bare:
        raise ValueError(f"invalid session_id: {session_id!r}")
    if not _VALID_SESSION_ID.match(bare):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return bare


# ---------------------------------------------------------------------------
# OpenAI pricing (placeholder — operator may need to refresh)
# ---------------------------------------------------------------------------


# Dollars per token. Verified 2026-05-30 against developers.openai.com/api/docs/pricing.
# Cached input is exactly 10% of standard input for the gpt-5 family
# (see `_OPENAI_CACHED_INPUT_MULTIPLIER`). Update when OpenAI publishes new
# rates or when codex CLI starts routing to newer models.
#
# NB: Codex CLI sessions on a ChatGPT plan (`plan_type: team/pro/enterprise`
# in the rollout's `token_count` event) are billed against that plan's flat
# subscription, not per-token API rates. The dollar column here is the
# equivalent API cost — useful as a relative-cost signal between sessions,
# not a literal invoice number.
#
# As of GPT-5.5 (released 2026-04-23), there is no separate `gpt-5.5-codex`
# variant — gpt-5.5 itself serves codex tasks. gpt-5.3-codex remains as a
# cheaper specialized model that codex CLI can be pinned to via `-c model=`.
_OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5":          {"input": 5.00e-6, "output": 30.00e-6},
    "gpt-5.5-pro":      {"input": 30.00e-6, "output": 180.00e-6},
    "gpt-5.4":          {"input": 2.50e-6, "output": 15.00e-6},
    "gpt-5.3-codex":    {"input": 1.75e-6, "output": 14.00e-6},
    # gpt-5-codex is the pre-5.3 alias still emitted by older rollouts; map
    # to the closest published rate (5.3-codex) until OpenAI clarifies.
    "gpt-5-codex":      {"input": 1.75e-6, "output": 14.00e-6},
    "gpt-4o":           {"input": 5.00e-6, "output": 15.00e-6},
    "gpt-4o-mini":      {"input": 0.15e-6, "output":  0.60e-6},
}
_OPENAI_CACHED_INPUT_MULTIPLIER: float = 0.10


def _cost_from_usage(usage: dict[str, Any], model: str) -> float:
    """Price a Codex `total_token_usage` dict against `_OPENAI_PRICING`.

    Unknown models fall through to 0.0 — Codex sessions still show token
    counts in the UI; only the dollar rollup is suppressed.
    """
    rates = _OPENAI_PRICING.get((model or "").lower())
    if rates is None:
        return 0.0
    in_total = int(usage.get("input_tokens", 0) or 0)
    cached = int(usage.get("cached_input_tokens", 0) or 0)
    out_total = int(usage.get("output_tokens", 0) or 0)
    reasoning = int(usage.get("reasoning_output_tokens", 0) or 0)
    fresh_in = max(0, in_total - cached)
    fresh_out = max(0, out_total - reasoning)
    input_rate = rates["input"]
    return (
        fresh_in * input_rate
        + cached * input_rate * _OPENAI_CACHED_INPUT_MULTIPLIER
        + (fresh_out + reasoning) * rates["output"]
    )


# ---------------------------------------------------------------------------
# Session metadata + discovery
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Per-session metadata produced by discovery + parsing."""

    session_id: str  # already cx:-prefixed
    raw_session_id: str
    jsonl_path: str
    mtime: float
    decoded_cwd: str = ""
    started_at: int = 0
    last_activity_at: int = 0
    status: str = "inactive"
    status_inferred: bool = True
    model: str = ""
    cli_version: str = ""
    originator: str = ""
    git_branch: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_tokens: int = 0
    total_dollars: float = 0.0
    tool_call_count: int = 0
    error_count: int = 0
    last_event_kind: str = ""
    label: str = ""
    last_user_text: str = ""
    first_user_text: str = ""


def _sessions_dir(override: str | None = None) -> Path:
    raw = override or os.environ.get(
        "LIFEOS_CODEX_SESSIONS_DIR", "~/.codex/sessions"
    )
    return Path(os.path.expanduser(raw))


def _resolve_sessions_dir(sessions_dir: str | Path | None) -> Path:
    if sessions_dir is None:
        return _sessions_dir()
    return Path(os.path.expanduser(str(sessions_dir)))


# Filename pattern: rollout-<iso-datetime>-<uuid>.jsonl
_ROLLOUT_FILENAME = re.compile(
    r"^rollout-(?P<ts>[0-9T\-]+)-(?P<sid>[A-Za-z0-9\-]+)\.jsonl$"
)


def discover_sessions(
    sessions_dir: str | Path | None = None,
    lookback_days: int = 7,
    limit: int = 200,
    now: float | None = None,
) -> list[SessionMeta]:
    """Walk the sessions dir and return one SessionMeta per recent rollout.

    Returned newest-first by mtime, capped to `limit`. Files older than
    `lookback_days` are excluded. The on-disk layout is
    `<root>/<year>/<month>/<day>/rollout-*.jsonl`, but we glob recursively
    rather than parse the path — codex may change the layout.
    """
    root = _resolve_sessions_dir(sessions_dir)
    if not root.exists() or not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - max(0, lookback_days) * 86_400

    metas: list[SessionMeta] = []
    for jsonl in root.rglob("rollout-*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        match = _ROLLOUT_FILENAME.match(jsonl.name)
        if not match:
            continue
        raw_id = match.group("sid")
        try:
            validate_session_id(raw_id)
        except ValueError:
            continue
        metas.append(SessionMeta(
            session_id=CX_PREFIX + raw_id,
            raw_session_id=raw_id,
            jsonl_path=str(jsonl),
            mtime=st.st_mtime,
        ))
    metas.sort(key=lambda m: m.mtime, reverse=True)
    return metas[:limit]


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def _ts_from(raw: dict[str, Any]) -> float:
    ts_str = raw.get("timestamp")
    if not ts_str:
        return time.time()
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


def _truncate(s: str, cap: int = _PAYLOAD_PREVIEW_MAX) -> str:
    if len(s) <= cap:
        return s
    return s[: cap - 1] + "…"


def _extract_text(content: Any) -> str:
    """Concatenate text from a response_item.message content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("input_text", "output_text", "text"):
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def normalize_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one Codex rollout line into LifeOS shape, or None to skip.

    Codex has three top-level shapes:
    - `session_meta` — one-time setup; mostly noise, kept as `system_session_meta`
      to give the transcript view a starting beacon.
    - `event_msg` — lifecycle events. Subtypes we surface: `user_message`,
      `agent_message`, `task_started`, `task_complete`, `token_count`.
    - `response_item` — content. We surface `message` (user/assistant/developer)
      and tool-call shapes.
    """
    rtype = raw.get("type")
    payload = raw.get("payload") or {}

    if rtype == "session_meta":
        return {
            "ts": _ts_from(raw),
            "kind": "system_session_meta",
            "payload": {
                "cwd": payload.get("cwd"),
                "originator": payload.get("originator"),
                "cli_version": payload.get("cli_version"),
                "model_provider": payload.get("model_provider"),
                "git": payload.get("git") or {},
            },
        }

    if rtype == "event_msg":
        sub = payload.get("type")
        if sub == "user_message":
            return {
                "ts": _ts_from(raw),
                "kind": "user_message",
                "payload": {"text": _truncate(str(payload.get("message", "")))},
            }
        if sub == "agent_message":
            return {
                "ts": _ts_from(raw),
                "kind": "assistant_message",
                "payload": {
                    "text": _truncate(str(payload.get("message", ""))),
                    "phase": payload.get("phase"),
                },
            }
        if sub == "task_started":
            return {
                "ts": _ts_from(raw),
                "kind": "system_task_started",
                "payload": {
                    "turn_id": payload.get("turn_id"),
                    "model_context_window": payload.get("model_context_window"),
                },
            }
        if sub == "task_complete":
            return {
                "ts": _ts_from(raw),
                "kind": "system_task_complete",
                "payload": {
                    "turn_id": payload.get("turn_id"),
                    "duration_ms": payload.get("duration_ms"),
                },
            }
        if sub == "token_count":
            info = payload.get("info") or {}
            return {
                "ts": _ts_from(raw),
                "kind": "system_token_count",
                "payload": {
                    "total": info.get("total_token_usage") or {},
                    "last": info.get("last_token_usage") or {},
                    "context_window": info.get("model_context_window"),
                },
            }
        if sub == "error":
            return {
                "ts": _ts_from(raw),
                "kind": "error",
                "payload": {"message": _truncate(str(payload.get("message", "")))},
            }
        # Unrecognized event_msg subtype — drop silently.
        return None

    if rtype == "response_item":
        sub = payload.get("type")
        if sub == "message":
            role = payload.get("role") or "assistant"
            text = _extract_text(payload.get("content"))
            if role == "developer":
                # Developer messages are the base instructions / permissions /
                # apps / skills boilerplate Codex injects every turn. Useful
                # in the rollout for replay but noise in the viz.
                return None
            if role == "user":
                # Codex emits user prompts twice: once cleanly via
                # event_msg/user_message (the literal prompt), once here with
                # AGENTS.md + environment_context prepended. Tag the bundled
                # version distinctly so the label/dedup logic prefers the
                # clean event_msg copy.
                return {
                    "ts": _ts_from(raw),
                    "kind": "context_message",
                    "payload": {"text": _truncate(text)},
                }
            return {
                "ts": _ts_from(raw),
                "kind": "assistant_message",
                "payload": {"text": _truncate(text)},
            }
        if sub == "reasoning":
            summary = payload.get("summary") or []
            text_parts: list[str] = []
            if isinstance(summary, list):
                for item in summary:
                    if isinstance(item, dict):
                        text_parts.append(str(item.get("text", "")))
            return {
                "ts": _ts_from(raw),
                "kind": "thinking",
                "payload": {
                    "text": _truncate("".join(text_parts)),
                    "chars": sum(len(p) for p in text_parts),
                },
            }
        if sub in ("function_call", "local_shell_call", "custom_tool_call"):
            return {
                "ts": _ts_from(raw),
                "kind": "tool_call",
                "payload": {
                    "name": payload.get("name") or payload.get("type"),
                    "call_id": payload.get("call_id") or payload.get("id"),
                    "arguments_preview": _truncate(str(payload.get("arguments", ""))[:_PAYLOAD_PREVIEW_MAX]),
                },
            }
        if sub in ("function_call_output", "local_shell_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            preview = ""
            if isinstance(output, str):
                preview = output
            elif isinstance(output, dict):
                preview = str(output.get("content") or output.get("text") or output)
            return {
                "ts": _ts_from(raw),
                "kind": "tool_result",
                "payload": {
                    "call_id": payload.get("call_id") or payload.get("id"),
                    "is_error": bool(payload.get("is_error")),
                    "content_preview": _truncate(preview),
                },
            }
        return None

    # turn_context, unknown types — drop silently.
    return None


# ---------------------------------------------------------------------------
# Full session parse
# ---------------------------------------------------------------------------


def _iter_lines(path: str) -> Iterator[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def parse_session(
    meta: SessionMeta,
    now: float | None = None,
) -> tuple[SessionMeta, list[dict[str, Any]]]:
    """Open the rollout, populate `meta`, return normalized events."""
    events: list[dict[str, Any]] = []
    started_at: int = 0
    last_activity_at: int = 0
    last_kind = ""
    last_user_text = ""
    first_user_text = ""
    last_token_usage: dict[str, Any] = {}
    tool_call_count = 0
    error_count = 0
    last_event_was_error = False
    pending_tool = False
    open_tool_call_ids: set[str] = set()

    for raw in _iter_lines(meta.jsonl_path):
        rtype = raw.get("type")
        payload = raw.get("payload") or {}

        # Pull metadata from session_meta one-shot.
        if rtype == "session_meta":
            meta.decoded_cwd = str(payload.get("cwd") or meta.decoded_cwd)
            meta.cli_version = str(payload.get("cli_version") or meta.cli_version)
            meta.originator = str(payload.get("originator") or meta.originator)
            git = payload.get("git") or {}
            meta.git_branch = str(git.get("branch") or meta.git_branch)

        # turn_context carries the most reliable model id (session_meta
        # doesn't always include it).
        elif rtype == "turn_context":
            ev_model = str(payload.get("model") or "")
            if ev_model and not meta.model:
                meta.model = ev_model

        # token_count is cumulative — last one wins for the session totals.
        if rtype == "event_msg" and (payload.get("type") == "token_count"):
            info = payload.get("info") or {}
            tot = info.get("total_token_usage") or {}
            if tot:
                last_token_usage = tot

        ev = normalize_event(raw)
        if ev is None:
            continue
        events.append(ev)
        ts = ev["ts"]
        if started_at == 0:
            started_at = int(ts)
        last_activity_at = int(ts)
        last_kind = ev["kind"]
        kind = ev["kind"]
        ev_payload = ev["payload"] or {}

        if kind == "user_message":
            text = (ev_payload.get("text") or "").strip()
            if text:
                last_user_text = text
                if not first_user_text:
                    first_user_text = text
            last_event_was_error = False
            pending_tool = bool(open_tool_call_ids)
        elif kind == "assistant_message":
            last_event_was_error = False
        elif kind == "tool_call":
            tool_call_count += 1
            call_id = ev_payload.get("call_id")
            if call_id:
                open_tool_call_ids.add(str(call_id))
            pending_tool = True
        elif kind == "tool_result":
            call_id = ev_payload.get("call_id")
            if call_id:
                open_tool_call_ids.discard(str(call_id))
            if ev_payload.get("is_error"):
                error_count += 1
                last_event_was_error = True
            else:
                last_event_was_error = False
            pending_tool = bool(open_tool_call_ids)
        elif kind == "error":
            error_count += 1
            last_event_was_error = True

    meta.started_at = started_at
    meta.last_activity_at = last_activity_at
    meta.first_user_text = first_user_text
    meta.last_user_text = last_user_text
    meta.tool_call_count = tool_call_count
    meta.error_count = error_count
    meta.last_event_kind = last_kind
    if last_token_usage:
        meta.total_input_tokens = int(last_token_usage.get("input_tokens", 0) or 0)
        meta.total_cached_input_tokens = int(last_token_usage.get("cached_input_tokens", 0) or 0)
        meta.total_output_tokens = int(last_token_usage.get("output_tokens", 0) or 0)
        meta.total_reasoning_tokens = int(last_token_usage.get("reasoning_output_tokens", 0) or 0)
        meta.total_tokens = int(last_token_usage.get("total_tokens", 0) or 0)
        meta.total_dollars = _cost_from_usage(last_token_usage, meta.model)

    meta.status, meta.status_inferred = _infer_status(
        mtime=meta.mtime,
        pending_tool=pending_tool,
        last_event_was_error=last_event_was_error,
        now=now,
        has_live_process=False,
    )

    if first_user_text:
        meta.label = _truncate(first_user_text.replace("\n", " "), 60)
    elif last_user_text:
        meta.label = _truncate(last_user_text.replace("\n", " "), 60)
    elif meta.decoded_cwd:
        meta.label = os.path.basename(meta.decoded_cwd.rstrip("/")) or meta.decoded_cwd
    else:
        meta.label = meta.raw_session_id

    return meta, events


def _infer_status(
    mtime: float,
    pending_tool: bool,
    last_event_was_error: bool,
    now: float | None = None,
    has_live_process: bool = False,
) -> tuple[str, bool]:
    """Same status rules as the Claude Code adapter, by design."""
    if has_live_process:
        return ("running", False)
    age = (now if now is not None else time.time()) - mtime
    if age < _RUNNING_MTIME_THRESHOLD:
        return ("running", True)
    if age < _INACTIVE_MTIME_THRESHOLD:
        return ("inactive", True)
    if last_event_was_error:
        return ("failed", True)
    if pending_tool:
        return ("inactive", True)
    return ("completed", True)


# ---------------------------------------------------------------------------
# Live-process detection via psutil
# ---------------------------------------------------------------------------


_PROCESS_CACHE_TTL = 5.0


@dataclass
class _ProcessCache:
    expires_at: float = 0.0
    cwd_counts: dict[str, int] = field(default_factory=dict)


_process_cache = _ProcessCache()
_process_cache_lock = threading.Lock()


# Wrapper processes that have `codex` in their argv but are not the actual
# Codex binary — exclude to avoid inflating the live-session count.
_WRAPPER_BINARY_BASENAMES = frozenset({"vt", "vibetunnel", "node", "bash", "sh", "zsh"})


def live_codex_cwd_counts(now: float | None = None) -> dict[str, int]:
    """Return `{cwd: count}` for live `codex` processes per project dir.

    Mirrors `live_claude_cwd_counts` — same strict matcher: `name() ==
    'codex'` OR `exe basename == 'codex'`, wrapper basenames excluded.
    """
    now_t = now if now is not None else time.time()
    with _process_cache_lock:
        if _process_cache.expires_at > now_t:
            return dict(_process_cache.cwd_counts)

    counts: dict[str, int] = {}
    try:
        import psutil
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "")
                exe_base = exe.rsplit("/", 1)[-1].lower() if exe else ""
                if name != "codex" and exe_base != "codex":
                    continue
                if name in _WRAPPER_BINARY_BASENAMES or exe_base in _WRAPPER_BINARY_BASENAMES:
                    continue
                cwd = proc.cwd()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:  # noqa: BLE001
                continue
            if cwd:
                counts[cwd] = counts.get(cwd, 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("live_codex_cwd_counts: psutil scan failed: %s", exc)

    with _process_cache_lock:
        _process_cache.expires_at = now_t + _PROCESS_CACHE_TTL
        _process_cache.cwd_counts = dict(counts)
    return dict(counts)


def invalidate_process_cache() -> None:
    with _process_cache_lock:
        _process_cache.expires_at = 0.0


# ---------------------------------------------------------------------------
# Public API used by the agents route
# ---------------------------------------------------------------------------


def model_label(model: str) -> str:
    """Short routing badge label for Codex sessions."""
    m = (model or "").lower()
    if "codex" in m:
        return "Codex"
    if "gpt-5" in m or "gpt5" in m:
        return "GPT-5"
    if "gpt-4" in m or "gpt4" in m:
        return "GPT-4"
    return "Codex"


def to_session_dict(meta: SessionMeta) -> dict[str, Any]:
    """Render `meta` into a snapshot row matching the agent-worker shape."""
    # cache_creation_input_tokens / cache_read_input_tokens are Anthropic-
    # specific. OpenAI exposes only cached_input_tokens; bucket it as
    # cache_read so the existing UI columns line up.
    return {
        "session_id": meta.session_id,
        "task_id": meta.raw_session_id,
        "status": meta.status,
        "routing": "codex",
        "parent_session_id": None,
        "root_session_id": meta.session_id,
        "spawn_depth": 0,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": meta.started_at,
        "last_activity_at": meta.last_activity_at,
        "total_input_tokens": meta.total_input_tokens,
        "total_output_tokens": meta.total_output_tokens,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": meta.total_cached_input_tokens,
        "total_dollars": round(meta.total_dollars, 6),
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": meta.label,
        "model_label": model_label(meta.model),
        "last_event_kind": meta.last_event_kind,
        "tool_call_count": meta.tool_call_count,
        "error_count": meta.error_count,
        "source": "codex",
        "status_inferred": meta.status_inferred,
        "project_key": "",
        "decoded_cwd": meta.decoded_cwd,
        "cli_version": meta.cli_version,
        "git_branch": meta.git_branch,
    }


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


_CACHE_TTL = 30.0


@dataclass
class _CacheEntry:
    expires_at: float = 0.0
    sessions: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


_snapshot_cache: dict[tuple[str, int], _CacheEntry] = {}
_snapshot_cache_lock = threading.Lock()


def _cache_key(sessions_dir: str | Path | None, lookback_days: int) -> tuple[str, int]:
    return (str(sessions_dir) if sessions_dir is not None else "", int(lookback_days))


def build_snapshot(
    sessions_dir: str | Path | None = None,
    lookback_days: int = 7,
    limit: int = 200,
    cache_ttl: float = _CACHE_TTL,
    now: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(sessions, edges)` for the /agents snapshot. Cached."""
    now_t = now if now is not None else time.time()
    key = _cache_key(sessions_dir, lookback_days)
    if cache_ttl > 0:
        with _snapshot_cache_lock:
            entry = _snapshot_cache.get(key)
            if entry and entry.expires_at > now_t:
                return list(entry.sessions), list(entry.edges)

    sessions: list[dict[str, Any]] = []
    cwd_counts = live_codex_cwd_counts(now=now_t)

    parsed_by_cwd: dict[str, list[SessionMeta]] = {}
    for meta in discover_sessions(sessions_dir, lookback_days=lookback_days, limit=limit, now=now_t):
        try:
            parsed_meta, _events = parse_session(meta, now=now_t)
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex parse failed for %s: %s", meta.jsonl_path, exc)
            continue
        parsed_by_cwd.setdefault(parsed_meta.decoded_cwd or "", []).append(parsed_meta)

    # Promote the top-N most-recent sessions per live-codex cwd to
    # authoritative `running`. Same cap-by-process-count rule as the cc
    # adapter so a single live codex doesn't inflate every historical
    # rollout in that project.
    for cwd, parsed_list in parsed_by_cwd.items():
        n_live = cwd_counts.get(cwd, 0)
        if not n_live or not cwd:
            continue
        parsed_list.sort(key=lambda m: m.mtime, reverse=True)
        for parsed_meta in parsed_list[:n_live]:
            parsed_meta.status = "running"
            parsed_meta.status_inferred = False

    for parsed_list in parsed_by_cwd.values():
        for parsed_meta in parsed_list:
            sessions.append(to_session_dict(parsed_meta))

    edges: list[dict[str, Any]] = []  # Codex has no spawn graph today.

    if cache_ttl > 0:
        with _snapshot_cache_lock:
            _snapshot_cache[key] = _CacheEntry(
                expires_at=now_t + cache_ttl,
                sessions=list(sessions),
                edges=list(edges),
            )
    return list(sessions), list(edges)


def invalidate_cache() -> None:
    with _snapshot_cache_lock:
        _snapshot_cache.clear()


def read_normalized_events(
    session_id: str,
    sessions_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return the normalized event list for one Codex session."""
    bare = validate_session_id(session_id)
    root = _resolve_sessions_dir(sessions_dir)
    if not root.exists() or not root.is_dir():
        return []
    # Rollout filenames embed the session uuid as the last segment; glob
    # by suffix to avoid walking deep year/month/day directories blindly.
    for candidate in root.rglob(f"rollout-*-{bare}.jsonl"):
        events = [
            ev for ev in (normalize_event(raw) for raw in _iter_lines(str(candidate)))
            if ev is not None
        ]
        return events
    return []
