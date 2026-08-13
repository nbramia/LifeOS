"""Discover, normalize, and price Claude Code CLI sessions for the /agents viz.

Claude Code stores each terminal session as an append-only JSONL at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The schema is rich
(per-message `usage` blocks with cache-token accounting, tool-use blocks,
extended-thinking blocks, etc.) but very different from the LifeOS agent
worker's `{ts, kind, payload}` shape.

This module is a **read-only** adapter that translates the Claude Code
schema into the LifeOS shape so the /agents route can union both sources
without forking its rendering logic.

Public surface:

- `discover_sessions(...)` — list session metadata for snapshot use
- `read_normalized(session_id)` — return `(meta, events)` for one session
- `read_events_tail(session_id, since_line=...)` — for live SSE tail
- `to_session_dict(meta)` — produce a snapshot row matching the
  agent-worker shape

Path-traversal protection is enforced on every `session_id` lookup. The
adapter never writes to Claude Code's data.
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
# without ambiguity. Claude Code session UUIDs do not naturally collide
# with LifeOS session_ids, but the prefix makes routing intent explicit.
CC_PREFIX = "cc:"

# Event types dropped as noise. The keep-list is implicit in `normalize_event`
# (we explicitly handle `user`, `assistant`, `system` and ignore everything
# not matched).
_RAW_TYPES_NOISE = frozenset({
    "mode", "permission-mode", "ai-title", "last-prompt", "worktree-state",
    "file-history-snapshot", "queue-operation", "attachment", "pr-link",
})

# Anthropic standard `usage` keys we sum across assistant messages for cost.
_USAGE_INPUT = "input_tokens"
_USAGE_OUTPUT = "output_tokens"
_USAGE_CACHE_CREATION = "cache_creation_input_tokens"
_USAGE_CACHE_READ = "cache_read_input_tokens"

# Subagent-spawning tool names (Claude Code calls them "Agent" today; the
# issue body mentions "Task" as the historical name — accept both).
_SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

# Status-inference thresholds (seconds).
# A jsonl touched in the last 10 minutes reads as `running` even if the
# user has stepped away — the CLI typically appends in bursts and a single
# tight 60s threshold flipped sessions to inactive mid-pause.
_RUNNING_MTIME_THRESHOLD = 600  # 10 min
# Anything modified within 24h is `inactive` (resumable) — beyond that
# we treat as truly done.
_INACTIVE_MTIME_THRESHOLD = 86_400  # 24h

# Truncation cap for tool-result payload previews (privacy + UI bandwidth).
_PAYLOAD_PREVIEW_MAX = 240


# ---------------------------------------------------------------------------
# Validation / decoding
# ---------------------------------------------------------------------------


_VALID_SESSION_ID = re.compile(r"^[A-Za-z0-9_\-:]+$")


def validate_session_id(session_id: str) -> str:
    """Reject anything that could traverse outside the projects dir.

    Strips the `cc:` prefix if present and returns the bare id. Raises
    `ValueError` for any input containing path separators, `..`, or
    characters outside `[A-Za-z0-9_-:]`.
    """
    if not session_id:
        raise ValueError("session_id is required")
    bare = session_id[len(CC_PREFIX):] if session_id.startswith(CC_PREFIX) else session_id
    if "/" in bare or "\\" in bare or ".." in bare:
        raise ValueError(f"invalid session_id: {session_id!r}")
    if not _VALID_SESSION_ID.match(bare):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return bare


def decode_project_key(key: str) -> str:
    """Decode an encoded cwd path back to the original.

    Claude Code stores projects as `~/.claude/projects/-home-user-Code-X/`.
    The encoding replaces `/` with `-`, so a leading `-` represents `/`.
    """
    if not key:
        return ""
    # Treat a leading `-` as `/`; subsequent `-` separators also become `/`.
    if key.startswith("-"):
        return "/" + key[1:].replace("-", "/")
    return key.replace("-", "/")


def basename_for(decoded_cwd: str) -> str:
    """Last path segment, e.g. `LifeOS` for `/home/n/Code/LifeOS`."""
    return os.path.basename(decoded_cwd.rstrip("/")) or decoded_cwd


# ---------------------------------------------------------------------------
# Session metadata + discovery
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Per-session metadata produced by discovery + parsing.

    Mirrors the fields the agent-worker `Session` dataclass exposes, plus a
    few Claude-Code-specific extras (`project_key`, `decoded_cwd`,
    `status_inferred`).
    """

    session_id: str  # already cc:-prefixed
    raw_session_id: str
    project_key: str
    decoded_cwd: str
    jsonl_path: str
    mtime: float
    started_at: int = 0
    last_activity_at: int = 0
    status: str = "inactive"
    status_inferred: bool = True
    model: str = ""
    parent_session_id: str | None = None
    root_session_id: str | None = None
    spawn_depth: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_dollars: float = 0.0
    tool_call_count: int = 0
    error_count: int = 0
    last_event_kind: str = ""
    label: str = ""
    last_user_text: str = ""
    subagents: list[dict[str, Any]] = field(default_factory=list)


def _projects_dir(override: str | None = None) -> Path:
    raw = override or os.environ.get(
        "LIFEOS_CLAUDE_CODE_PROJECTS_DIR", "~/.claude/projects"
    )
    return Path(os.path.expanduser(raw))


def _resolve_projects_dir(projects_dir: str | Path | None) -> Path:
    """Resolve a projects_dir value into a `Path` with `~` expanded.

    Callers pass either an absolute `Path` (tests), a string that may
    contain `~` (settings — `~/.claude/projects` is the default), or
    `None` (fall back to env / default). Without this, the route handed
    the literal `~/.claude/projects` string straight to `Path()`, which
    does not expand `~` and silently returned zero sessions in
    production.
    """
    if projects_dir is None:
        return _projects_dir()
    return Path(os.path.expanduser(str(projects_dir)))


def discover_sessions(
    projects_dir: str | Path | None = None,
    lookback_days: int = 7,
    limit: int = 200,
    now: float | None = None,
) -> list[SessionMeta]:
    """Walk the projects dir and return one SessionMeta per recent jsonl file.

    Sessions are returned newest-first by mtime, capped to `limit`. Files
    older than `lookback_days` are excluded so the snapshot stays lean
    (older transcripts can still be opened on demand via the events
    endpoint with their full session_id).

    Discovery is the expensive part of ingestion — callers should cache the
    result for ~30s rather than re-scanning every SSE tick.
    """
    root = _resolve_projects_dir(projects_dir)
    if not root.exists() or not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - max(0, lookback_days) * 86_400

    metas: list[SessionMeta] = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        project_key = proj.name
        decoded_cwd = decode_project_key(project_key)
        for jsonl in proj.glob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            raw_id = jsonl.stem
            try:
                validate_session_id(raw_id)
            except ValueError:
                # Skip anything weird — a malformed filename in a dir we don't own
                # is not worth crashing the snapshot over.
                continue
            metas.append(SessionMeta(
                session_id=CC_PREFIX + raw_id,
                raw_session_id=raw_id,
                project_key=project_key,
                decoded_cwd=decoded_cwd,
                jsonl_path=str(jsonl),
                mtime=st.st_mtime,
            ))
    metas.sort(key=lambda m: m.mtime, reverse=True)
    return metas[:limit]


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def _normalize_assistant_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate one Claude Code assistant event into a LifeOS event.

    Returns a `{ts, kind, payload}` dict. Tool-use and tool-result content
    blocks are surfaced as separate logical events when present (one
    `tool_call` per block). This matches what LifeOS agents emit.

    For MVP we coalesce a single assistant message into a single
    `assistant_message` event regardless of how many content blocks it
    contains — but populate `payload.tool_uses` so callers (and the
    subagent correlator) can find them.
    """
    msg = raw.get("message") or {}
    content = msg.get("content") or []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "thinking":
                thinking_parts.append(str(block.get("thinking", "")))
            elif btype == "tool_use":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input_keys": sorted(list((block.get("input") or {}).keys())),
                })
    elif isinstance(content, str):
        text_parts.append(content)

    payload = {
        "model": msg.get("model"),
        "text": _truncate("".join(text_parts)),
        "tool_uses": tool_uses,
        "thinking_chars": sum(len(t) for t in thinking_parts),
        "usage": msg.get("usage") or {},
    }
    return {
        "ts": _ts_from(raw),
        "kind": "assistant_message",
        "payload": payload,
    }


def _normalize_user_event(raw: dict[str, Any]) -> dict[str, Any]:
    msg = raw.get("message") or {}
    content = msg.get("content")
    text: str | None = None
    tool_results: list[dict[str, Any]] = []
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "tool_result":
                inner = block.get("content")
                preview: str
                if isinstance(inner, str):
                    preview = inner
                elif isinstance(inner, list):
                    bits: list[str] = []
                    for item in inner:
                        if isinstance(item, dict):
                            bits.append(str(item.get("text", "")))
                    preview = "".join(bits)
                else:
                    preview = ""
                tool_results.append({
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")),
                    "content_preview": _truncate(preview),
                })
        text = "".join(text_parts)
    payload: dict[str, Any] = {"text": _truncate(text or "")}
    if tool_results:
        payload["tool_results"] = tool_results
        # If every tool_result was an error, surface the kind upward for the
        # frontend's error-styling.
        kind = "tool_result"
    else:
        kind = "user_message"
    return {"ts": _ts_from(raw), "kind": kind, "payload": payload}


def _normalize_system_event(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": _ts_from(raw),
        "kind": "system_" + str(raw.get("subtype") or "event"),
        "payload": {
            "cwd": raw.get("cwd"),
            "duration_ms": raw.get("durationMs"),
            "message_count": raw.get("messageCount"),
        },
    }


def normalize_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one raw Claude Code event into LifeOS shape, or skip."""
    rtype = raw.get("type")
    if rtype in _RAW_TYPES_NOISE:
        return None
    if rtype == "assistant":
        return _normalize_assistant_event(raw)
    if rtype == "user":
        return _normalize_user_event(raw)
    if rtype == "system":
        return _normalize_system_event(raw)
    return None


def _ts_from(raw: dict[str, Any]) -> float:
    """Parse the line's ISO timestamp into a unix epoch float.

    Falls back to current time so a malformed timestamp doesn't break the
    transcript view. Logs at debug level — common enough on early lines.
    """
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


# ---------------------------------------------------------------------------
# Pricing + cost rollup
# ---------------------------------------------------------------------------


def _cost_from_usage(usage: dict[str, Any], model: str) -> float:
    """Apply pricing.py to a single message's `usage` block.

    Sums all four Anthropic token buckets — uncached input, output,
    cache_creation (1.25× input), cache_read (0.10× input) — matching
    the agent worker's cost accounting after #145 / #157 landed
    cache-aware pricing in `pricing.cost_for`. This keeps API spend
    numbers apples-to-apples across LifeOS agent and Claude Code
    sources.
    """
    from api.services.agent_worker.pricing import cost_for

    in_tok = int(usage.get(_USAGE_INPUT, 0) or 0)
    out_tok = int(usage.get(_USAGE_OUTPUT, 0) or 0)
    cache_creation = int(usage.get(_USAGE_CACHE_CREATION, 0) or 0)
    cache_read = int(usage.get(_USAGE_CACHE_READ, 0) or 0)
    use_model = model or "claude-sonnet-5"  # safer default than the Opus fallback
    return cost_for(
        use_model,
        in_tok,
        out_tok,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


# ---------------------------------------------------------------------------
# Full session parse
# ---------------------------------------------------------------------------


def _iter_lines(path: str) -> Iterator[dict[str, Any]]:
    """Read JSONL lines, skipping unparseable ones (mirrors transcript_store)."""
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
    live_cwds: frozenset[str] | None = None,
) -> tuple[SessionMeta, list[dict[str, Any]]]:
    """Open the jsonl, populate `meta` fields, return normalized events.

    `live_cwds` is the precomputed set of cwds for live `claude` processes
    on this machine. Passed in by `build_snapshot` so a single scan is
    shared across every session in one snapshot tick. Falls back to a
    fresh scan if omitted (convenient for direct callers and tests).
    """
    events: list[dict[str, Any]] = []
    started_at: int = 0
    last_activity_at: int = 0
    total_in = 0
    total_out = 0
    total_cache_creation = 0
    total_cache_read = 0
    total_dollars = 0.0
    tool_call_count = 0
    error_count = 0
    last_kind = ""
    last_assistant_had_pending_tool = False
    last_event_was_error = False
    last_user_text = ""
    model = ""
    subagents: list[dict[str, Any]] = []
    # Session titles written by the CLI as their own record types (dropped from
    # the normalized event stream as noise, but captured here for labeling):
    #   custom-title → the user's explicit `/rename` value (`customTitle`)
    #   ai-title     → the CLI's auto-generated summary title (`aiTitle`)
    # Latest record of each wins (they're appended over the session's life).
    custom_title = ""
    ai_title = ""
    # Track open tool_use ids so we can decide if the last assistant turn
    # is "waiting on a tool result" (yielded) vs. final.
    open_tool_uses: set[str] = set()

    for raw in _iter_lines(meta.jsonl_path):
        rtype = raw.get("type")
        if rtype == "custom-title":
            custom_title = (raw.get("customTitle") or "").strip() or custom_title
            continue
        if rtype == "ai-title":
            ai_title = (raw.get("aiTitle") or "").strip() or ai_title
            continue
        ev = normalize_event(raw)
        if ev is None:
            continue
        events.append(ev)
        ts = ev["ts"]
        if started_at == 0:
            started_at = int(ts)
        last_activity_at = int(ts)
        last_kind = ev["kind"]

        if ev["kind"] == "assistant_message":
            payload = ev["payload"] or {}
            usage = payload.get("usage") or {}
            ev_model = payload.get("model") or ""
            if ev_model and not model:
                model = ev_model
            total_in += int(usage.get(_USAGE_INPUT, 0) or 0)
            total_out += int(usage.get(_USAGE_OUTPUT, 0) or 0)
            total_cache_creation += int(usage.get(_USAGE_CACHE_CREATION, 0) or 0)
            total_cache_read += int(usage.get(_USAGE_CACHE_READ, 0) or 0)
            total_dollars += _cost_from_usage(usage, ev_model or model)
            for tu in payload.get("tool_uses") or []:
                tool_call_count += 1
                if tu.get("id"):
                    open_tool_uses.add(tu["id"])
                if tu.get("name") in _SUBAGENT_TOOL_NAMES:
                    subagents.append({
                        "tool_use_id": tu.get("id"),
                        "name": tu.get("name"),
                        "started_at": int(ts),
                        "status": "running",
                    })
            last_assistant_had_pending_tool = bool(payload.get("tool_uses"))
            last_event_was_error = False
        elif ev["kind"] == "tool_result":
            # A user-side tool_result turn closes one or more open tool_use ids.
            # `last_event_was_error` becomes True if ANY result in this turn was
            # an error — that's a more conservative signal for status inference
            # than the last-result-wins behavior we had before.
            results = (ev["payload"] or {}).get("tool_results", [])
            turn_had_error = False
            for tr in results:
                tu_id = tr.get("tool_use_id")
                if tu_id and tu_id in open_tool_uses:
                    open_tool_uses.discard(tu_id)
                if tr.get("is_error"):
                    error_count += 1
                    turn_had_error = True
                # Close any matching subagent record.
                for sa in subagents:
                    if sa.get("tool_use_id") == tu_id:
                        sa["status"] = "failed" if tr.get("is_error") else "completed"
                        sa["last_activity_at"] = int(ts)
            last_event_was_error = turn_had_error
            last_assistant_had_pending_tool = bool(open_tool_uses)
        elif ev["kind"] == "user_message":
            text = (ev["payload"] or {}).get("text") or ""
            if text:
                last_user_text = text
            last_assistant_had_pending_tool = bool(open_tool_uses)
            last_event_was_error = False

    meta.started_at = started_at
    meta.last_activity_at = last_activity_at
    meta.model = model
    meta.total_input_tokens = total_in
    meta.total_output_tokens = total_out
    meta.total_cache_creation_tokens = total_cache_creation
    meta.total_cache_read_tokens = total_cache_read
    meta.total_dollars = total_dollars
    meta.tool_call_count = tool_call_count
    meta.error_count = error_count
    meta.last_event_kind = last_kind
    meta.last_user_text = last_user_text
    meta.subagents = subagents
    # Process-detection promotion to `running` is now applied at the
    # snapshot level (build_snapshot) — it requires cross-session comparison
    # within a cwd to pick the live N of M sessions. For a stand-alone
    # parse_session call we fall back to the mtime heuristic only.
    meta.status, meta.status_inferred = _infer_status(
        mtime=meta.mtime,
        last_assistant_had_pending_tool=last_assistant_had_pending_tool,
        last_event_was_error=last_event_was_error,
        now=now,
        has_live_process=False,
    )
    # Choose a label, most human-intentful first:
    #   1. the user's explicit `/rename` (custom-title) — always wins
    #   2. the CLI's auto-generated summary title (ai-title)
    #   3. the most recent user prompt (truncated)
    #   4. the working-directory basename, so the node is at least locatable
    #   5. the raw session id as a last resort
    if custom_title:
        meta.label = _truncate(custom_title.replace("\n", " "), 60)
    elif ai_title:
        meta.label = _truncate(ai_title.replace("\n", " "), 60)
    elif last_user_text:
        meta.label = _truncate(last_user_text.replace("\n", " "), 60)
    elif meta.decoded_cwd:
        meta.label = basename_for(meta.decoded_cwd)
    else:
        meta.label = meta.raw_session_id
    return meta, events


def _infer_status(
    mtime: float,
    last_assistant_had_pending_tool: bool,
    last_event_was_error: bool,
    now: float | None = None,
    has_live_process: bool = False,
) -> tuple[str, bool]:
    """Infer Claude Code session status.

    Returns `(status, inferred)`. `inferred=False` means the status came
    from a process-detection signal (authoritative); `inferred=True`
    means it was guessed from mtime alone.

    Rules:
      - Live `claude` process matches the project cwd → `running`
        (authoritative — `inferred=False`).
      - Modified in the last 10 minutes → `running` (mtime-only).
      - Modified within 24h → `inactive` (resumable; user closed the
        terminal or stepped away). Distinct from the agent worker's
        `yielded` which specifically means 'paused waiting on children'.
      - Older, ended on an error → `failed`.
      - Older, pending tool in flight → `inactive` (could be a long-
        running tool; lacking process evidence we can't say abandoned).
      - Otherwise → `completed`.
    """
    if has_live_process:
        return ("running", False)
    age = (now if now is not None else time.time()) - mtime
    if age < _RUNNING_MTIME_THRESHOLD:
        return ("running", True)
    if age < _INACTIVE_MTIME_THRESHOLD:
        return ("inactive", True)
    if last_event_was_error:
        return ("failed", True)
    if last_assistant_had_pending_tool:
        return ("inactive", True)
    return ("completed", True)


# ---------------------------------------------------------------------------
# Live-process detection via psutil
# ---------------------------------------------------------------------------




# Cache the live-process scan briefly so a single snapshot tick across many
# sessions doesn't enumerate /proc once per session.
_PROCESS_CACHE_TTL = 5.0


@dataclass
class _ProcessCache:
    expires_at: float = 0.0
    # Map of cwd → count of live `claude` processes with that cwd.
    cwd_counts: dict[str, int] = field(default_factory=dict)


_process_cache = _ProcessCache()
_process_cache_lock = threading.Lock()


# Wrapper processes that have `claude` in their argv but are not the actual
# Claude Code CLI binary. Matching argv loosely (the previous approach)
# pulled these in and inflated the running-session count.
_WRAPPER_BINARY_BASENAMES = frozenset({"vt", "vibetunnel", "node", "bash", "sh", "zsh"})


def live_claude_cwd_counts(now: float | None = None) -> dict[str, int]:
    """Return a `{cwd: count}` map of live `claude` processes per project dir.

    Used by `build_snapshot` to scope `running` status per-cwd: for each cwd
    that has N live processes, the N most-recently-modified jsonl files in
    that project are marked authoritative-running. Older jsonl files in the
    same cwd fall back to the mtime heuristic — this prevents the previous
    bug where one live session inflated every historical session in the
    same project to `running`.

    Strict matcher: name() == 'claude' OR exe basename == 'claude'. Wrapper
    processes (`vt claude`, `vibetunnel fwd claude`, the chrome-native-host
    versioned binary) are excluded explicitly.

    Returns an empty dict on any failure (psutil missing, unreadable proc,
    etc.) — the caller then uses mtime alone.
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
                if name != "claude" and exe_base != "claude":
                    continue
                if name in _WRAPPER_BINARY_BASENAMES or exe_base in _WRAPPER_BINARY_BASENAMES:
                    continue
                # Versioned shipping binary — `/home/.../claude/versions/2.1.152` —
                # has a numeric basename that won't match 'claude'. Detect it
                # by exe-path containment so we still count it.
                cwd = proc.cwd()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:  # noqa: BLE001 — never crash the scan on one bad proc
                continue
            if cwd:
                counts[cwd] = counts.get(cwd, 0) + 1
    except Exception as exc:  # noqa: BLE001 — psutil import/iter failures degrade gracefully
        logger.debug("live_claude_cwd_counts: psutil scan failed: %s", exc)

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
    """Short routing badge label for Claude Code sessions."""
    m = (model or "").lower()
    if "haiku" in m:
        return "Haiku"
    if "sonnet" in m:
        return "Sonnet"
    if "opus" in m:
        return "Opus"
    return "Claude Code"


def to_session_dict(meta: SessionMeta) -> dict[str, Any]:
    """Render `meta` into a snapshot row matching the agent-worker shape."""
    return {
        "session_id": meta.session_id,
        "task_id": meta.raw_session_id,
        "status": meta.status,
        "routing": "claude_code",
        "parent_session_id": meta.parent_session_id,
        "root_session_id": meta.root_session_id or meta.session_id,
        "spawn_depth": meta.spawn_depth,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": meta.started_at,
        "last_activity_at": meta.last_activity_at,
        "total_input_tokens": meta.total_input_tokens,
        "total_output_tokens": meta.total_output_tokens,
        "total_cache_creation_tokens": meta.total_cache_creation_tokens,
        "total_cache_read_tokens": meta.total_cache_read_tokens,
        "total_dollars": round(meta.total_dollars, 6),
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": meta.label,
        "model_label": model_label(meta.model),
        "last_event_kind": meta.last_event_kind,
        "tool_call_count": meta.tool_call_count,
        "error_count": meta.error_count,
        "source": "claude_code",
        "status_inferred": meta.status_inferred,
        "project_key": meta.project_key,
        "decoded_cwd": meta.decoded_cwd,
    }


def subagent_session_dict(parent: SessionMeta, subagent: dict[str, Any]) -> dict[str, Any]:
    """Synthetic snapshot row for a Task/Agent tool-use spawned by `parent`.

    These don't have their own jsonl — the subagent's response stream is
    embedded in the parent's transcript. The node exists in the graph for
    relationship clarity; clicking it loads the parent's transcript filtered
    by the tool_use_id (future work — for MVP the side panel can show the
    parent's transcript).
    """
    tu_id = subagent.get("tool_use_id") or ""
    synthetic_id = f"{parent.session_id}:agent:{tu_id}"
    return {
        "session_id": synthetic_id,
        "task_id": tu_id,
        "status": subagent.get("status", "running"),
        "routing": "claude_code",
        "parent_session_id": parent.session_id,
        "root_session_id": parent.session_id,
        "spawn_depth": (parent.spawn_depth or 0) + 1,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": subagent.get("started_at", parent.started_at),
        "last_activity_at": subagent.get("last_activity_at", parent.last_activity_at),
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_dollars": 0.0,
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": subagent.get("name") or "subagent",
        "model_label": model_label(parent.model),
        "last_event_kind": "subagent",
        "tool_call_count": 0,
        "error_count": 1 if subagent.get("status") == "failed" else 0,
        "source": "claude_code",
        "status_inferred": True,
        "project_key": parent.project_key,
        "decoded_cwd": parent.decoded_cwd,
        "is_subagent": True,
    }


# ---------------------------------------------------------------------------
# Snapshot builder — used by api/routes/agents.py
# ---------------------------------------------------------------------------


# Discovery + parse is the expensive op (touches the filesystem and reads
# every active jsonl). Cache the snapshot dicts for a short window so the
# 2s SSE tick doesn't hammer disk. The cache is keyed by (projects_dir,
# lookback_days) so callers with different scopes (e.g. tests) don't
# cross-contaminate, and guarded by a lock so concurrent FastAPI threads
# can't see a partially-written entry.
_CACHE_TTL = 30.0


@dataclass
class _CacheEntry:
    expires_at: float = 0.0
    sessions: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


_snapshot_cache: dict[tuple[str, int], _CacheEntry] = {}
_snapshot_cache_lock = threading.Lock()


def _cache_key(projects_dir: str | Path | None, lookback_days: int) -> tuple[str, int]:
    return (str(projects_dir) if projects_dir is not None else "", int(lookback_days))


def build_snapshot(
    projects_dir: str | Path | None = None,
    lookback_days: int = 7,
    limit: int = 200,
    cache_ttl: float = _CACHE_TTL,
    now: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(sessions, edges)` for the /agents snapshot. Cached.

    Edges include parent→subagent spawn edges. Subagent nodes are synthetic;
    they don't have their own jsonl. The cache is bypassed entirely when
    `cache_ttl <= 0` (no read, no write) so tests get a fresh snapshot.
    """
    now_t = now if now is not None else time.time()
    key = _cache_key(projects_dir, lookback_days)
    if cache_ttl > 0:
        with _snapshot_cache_lock:
            entry = _snapshot_cache.get(key)
            if entry and entry.expires_at > now_t:
                return list(entry.sessions), list(entry.edges)

    sessions: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    # One process-scan per snapshot tick — shared across every session.
    cwd_counts = live_claude_cwd_counts(now=now_t)
    # First pass: parse every discovered session with mtime-only status.
    # Process-detection promotion is layered on per-cwd below so we don't
    # over-attribute `running` to historical sessions sharing a project dir.
    parsed_by_cwd: dict[str, list[SessionMeta]] = {}
    for meta in discover_sessions(projects_dir, lookback_days=lookback_days, limit=limit, now=now_t):
        try:
            parsed_meta, _events = parse_session(meta, now=now_t)
        except Exception as exc:  # noqa: BLE001 — never break the snapshot on one bad file
            logger.warning("claude_code parse failed for %s: %s", meta.jsonl_path, exc)
            continue
        parsed_by_cwd.setdefault(parsed_meta.decoded_cwd or "", []).append(parsed_meta)

    # Second pass: for each cwd with live `claude` processes, promote the
    # top-N (by mtime, most-recent first) sessions to authoritative `running`.
    # This caps the running-set size by the actual process count so a single
    # live session doesn't drag every historical jsonl in the project along.
    for cwd, parsed_list in parsed_by_cwd.items():
        n_live = cwd_counts.get(cwd, 0)
        if not n_live or not cwd:
            continue
        parsed_list.sort(key=lambda m: m.mtime, reverse=True)
        for parsed_meta in parsed_list[:n_live]:
            parsed_meta.status = "running"
            parsed_meta.status_inferred = False

    # Third pass: assemble snapshot dicts + spawn edges (subagents emit from
    # their parent's parse output, so we walk parsed_by_cwd in order).
    for parsed_list in parsed_by_cwd.values():
        for parsed_meta in parsed_list:
            sessions.append(to_session_dict(parsed_meta))
            for sa in parsed_meta.subagents:
                child = subagent_session_dict(parsed_meta, sa)
                sessions.append(child)
                edges.append({
                    "from": parsed_meta.session_id,
                    "to": child["session_id"],
                    "type": "spawn",
                })

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
    projects_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return the normalized event list for one Claude Code session.

    `session_id` may include the `cc:` prefix. Subagent synthetic ids
    (`cc:<parent>:agent:<tool_use_id>`) return only the slice of the
    parent's transcript that corresponds to that subagent invocation —
    the assistant message containing the spawning `tool_use`, every
    event in between, and the user message containing the matching
    `tool_result` (inclusive).
    """
    bare = validate_session_id(session_id)
    subagent_tool_use_id: str | None = None
    if ":agent:" in bare:
        bare, subagent_tool_use_id = bare.split(":agent:", 1)
        bare = validate_session_id(bare)
    # Scan projects dir for the matching jsonl. Linear scan; for very large
    # projects directories consider an explicit map cache later.
    root = _resolve_projects_dir(projects_dir)
    if not root.exists() or not root.is_dir():
        return []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        candidate = proj / f"{bare}.jsonl"
        if candidate.exists():
            events = [
                ev for ev in (
                    normalize_event(raw) for raw in _iter_lines(str(candidate))
                ) if ev is not None
            ]
            if subagent_tool_use_id:
                return _filter_subagent_window(events, subagent_tool_use_id)
            return events
    return []


def _filter_subagent_window(
    events: list[dict[str, Any]],
    tool_use_id: str,
) -> list[dict[str, Any]]:
    """Slice a parent transcript to the window of one subagent invocation.

    The window starts at the assistant message whose `payload.tool_uses`
    contains `tool_use_id`, includes every event after it, and ends at
    the user message whose `tool_results` carries that same id. If the
    closing result hasn't been seen yet (subagent still running), returns
    everything from the spawn forward.
    """
    start: int | None = None
    end: int | None = None
    for idx, ev in enumerate(events):
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if start is None and kind == "assistant_message":
            for tu in payload.get("tool_uses") or []:
                if tu.get("id") == tool_use_id:
                    start = idx
                    break
            continue
        if start is not None and kind == "tool_result":
            for tr in payload.get("tool_results") or []:
                if tr.get("tool_use_id") == tool_use_id:
                    end = idx
                    break
            if end is not None:
                break
    if start is None:
        return []
    if end is None:
        return events[start:]
    return events[start : end + 1]
