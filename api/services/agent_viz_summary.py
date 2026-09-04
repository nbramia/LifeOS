"""Per-session "what did this agent work on?" summaries for the /agents UI.

Extracts a small context window from a session's transcript — the first user
message, the final assistant message, and any PR titles/URLs mentioned — and
asks the local Gemma LLM to produce two outputs:

    short_label: 2-6 word node label
    summary:     1-2 sentence / bullet recap

Results are cached by (session_id, last_activity_at) so a session that hasn't
moved doesn't re-summarize on every panel open or snapshot tick.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.services.llm_client import extract_json, generate_text

logger = logging.getLogger(__name__)

# Cap how much text we ship to Gemma. Generous enough that a chunky final
# message survives, tight enough that a 5000-event transcript doesn't drown
# the prompt.
_FIRST_MSG_CAP = 1500
_FINAL_MSG_CAP = 2500
_PR_CAP = 8

_GH_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
_GH_PR_CREATE_RE = re.compile(r"gh\s+pr\s+create\b", re.IGNORECASE)


@dataclass(frozen=True)
class SummaryResult:
    short_label: str
    summary: str

    def as_dict(self) -> dict[str, str]:
        return {"short_label": self.short_label, "summary": self.summary}


# In-process cache: session_id → (last_activity_at, created_at, SummaryResult).
# Disk cache (below) is the durable layer; this just keeps hot reads
# fingertip-fast. `created_at` is wall-clock seconds at write time and feeds
# the live-session grace window below.
_cache: dict[str, tuple[float, float, SummaryResult]] = {}
_CACHE_MAX = 500

# Non-terminal sessions move `last_activity_at` on every tool call. Without
# a grace window the cache would be stale within seconds of being written
# and the prefetch loop would burn Gemma cycles re-summarizing the same hot
# session every tick. A cached summary for a live session counts as "fresh
# enough" for this many seconds — only after that do we re-summarize on
# next access. Terminal sessions ignore this grace and use cache forever.
_LIVE_REFRESH_GRACE_SECONDS = 60 * 60  # 60 min

# Statuses that mean the session is done and `last_activity_at` is frozen.
# Imported lazily inside _is_terminal so this module stays importable
# without the agent_worker package on tests/standalone scripts.
_TERMINAL_STATUSES_CACHE: frozenset[str] | None = None


# CLI session statuses ("ended"/"inactive") don't exist in the worker's
# TERMINAL_STATUSES set at all — they're event-driven statuses from
# `cli_sessions` / the transcript scan's file-age guess, not worker session
# statuses. Without this a CLI session's fallback label was never cached and
# the prefetcher retried it on every tick forever (#863).
#
# `inactive` is deliberately included here even though it's NOT truly frozen
# (see `_is_frozen` below) — this set answers "should a fallback/no-content
# summary be cached for this session so the prefetcher stops retrying it?"
# (acceptance criterion 7), which is a different question from "is it safe
# to keep serving that cache once activity moves?" (`_is_fresh_enough`).
_CLI_TERMINAL_STATUSES = frozenset({"ended", "inactive"})


def _is_terminal(status: str) -> bool:
    global _TERMINAL_STATUSES_CACHE
    if _TERMINAL_STATUSES_CACHE is None:
        try:
            from api.services.agent_worker.session_store import TERMINAL_STATUSES
            _TERMINAL_STATUSES_CACHE = frozenset(TERMINAL_STATUSES) | _CLI_TERMINAL_STATUSES
        except Exception:  # noqa: BLE001
            _TERMINAL_STATUSES_CACHE = frozenset({
                "completed", "failed", "budget_exceeded", "killed", "cascade_killed",
            }) | _CLI_TERMINAL_STATUSES
    return (status or "") in _TERMINAL_STATUSES_CACHE


# Strict subset of `_is_terminal` whose `last_activity_at` is genuinely
# frozen — the session cannot produce more transcript content, so a cache
# for it is valid forever no matter what a later read reports. `inactive`
# is deliberately excluded (#863 review): it's the transcript scan's
# file-age guess for a Claude Code session idle >30 min, not a real
# terminal event, and `web/agents/panel.js`'s `TERMINAL` set explicitly
# agrees — "'idle' is a live cli session waiting for input — it must NOT be
# treated as terminal. Only 'ended' … is." A session reported `inactive`
# can resume (new activity), and its cache — possibly only a failure
# fallback cached to satisfy acceptance criterion 7 above — must not be
# trusted "regardless of new activity" the way a truly frozen session's
# can.
def _is_frozen(status: str) -> bool:
    return _is_terminal(status) and status != "inactive"


def _is_fresh_enough(cached_activity: float, cached_created_at: float,
                     new_activity: float, status: str) -> bool:
    """True iff the cached summary can be served even though the session
    may have advanced. Frozen (`_is_frozen`): always, regardless of new
    activity — the status guarantees nothing more will happen. Everything
    else (a genuinely live session, or `inactive` whose activity really did
    move — i.e. it resumed) only gets served within the live grace window,
    same as any other unsettled session."""
    # Exact match on activity → never stale regardless of status.
    if cached_activity >= new_activity:
        return True
    # Activity moved forward despite a frozen status — that shouldn't
    # happen, but if it does, trust the cache rather than treat a stray
    # timestamp nudge as meaningful new content: frozen statuses are valid
    # "regardless of new activity" by definition.
    if _is_frozen(status):
        return True
    # Everything not frozen falls to the grace window below, EXCEPT the
    # extended-but-not-frozen bucket (currently just `inactive`): that
    # status moving is the resumption signal itself, so don't extend it the
    # same leniency a genuinely live session gets — re-summarize now.
    if _is_terminal(status):
        return False
    # Live session: cached entry stays valid for the grace window.
    return (time.time() - cached_created_at) < _LIVE_REFRESH_GRACE_SECONDS

# --- Disk cache (SQLite) ---------------------------------------------------
# Survives server restarts so we never re-summarize a terminal session twice.
# Co-located under data/ next to other observability stores. WAL so concurrent
# request handlers don't serialize on writes.

_DB_PATH: str | None = None
_DB_LOCK = threading.Lock()


def _resolve_db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from config.settings import settings
            data_dir = Path(settings.chroma_path).parent
        except Exception:  # noqa: BLE001
            data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = str(data_dir / "agent_viz_summaries.db")
    return _DB_PATH


def _init_db() -> None:
    path = _resolve_db_path()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_viz_summary (
                session_id TEXT PRIMARY KEY,
                last_activity_at REAL NOT NULL,
                short_label TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def _disk_get(session_id: str, last_activity_at: float, status: str = "") -> SummaryResult | None:
    """Read the disk cache. Honors the live-session grace window so a hot
    session doesn't get re-summarized on every tick of the prefetch loop.
    """
    try:
        with sqlite3.connect(_resolve_db_path()) as conn:
            row = conn.execute(
                "SELECT last_activity_at, short_label, summary, created_at "
                "FROM agent_viz_summary WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("disk cache read failed for %s: %s", session_id, exc)
        return None
    if not row:
        return None
    cached_activity, short_label, summary, created_at = row
    if not _is_fresh_enough(cached_activity, float(created_at), last_activity_at, status):
        return None
    return SummaryResult(short_label=short_label, summary=summary)


def _disk_put(session_id: str, last_activity_at: float, result: SummaryResult) -> None:
    try:
        with _DB_LOCK, sqlite3.connect(_resolve_db_path()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_viz_summary "
                "(session_id, last_activity_at, short_label, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, last_activity_at, result.short_label, result.summary, int(time.time())),
            )
            conn.commit()
    except sqlite3.Error as exc:
        # Persistence is best-effort — never block a successful summary on
        # a disk write failure (full disk, permissions, locked DB, …).
        logger.warning("disk cache write failed for %s: %s", session_id, exc)


def prune_disk_cache(max_age_days: int = 90, max_rows: int = 5000) -> int:
    """Drop rows older than `max_age_days`; if still over `max_rows`, drop
    oldest. Returns count deleted. Safe to call from a periodic task or
    leave unscheduled — the table is bounded by session count anyway.
    """
    cutoff = int(time.time()) - max_age_days * 86400
    deleted = 0
    try:
        with _DB_LOCK, sqlite3.connect(_resolve_db_path()) as conn:
            cur = conn.execute(
                "DELETE FROM agent_viz_summary WHERE created_at < ?", (cutoff,)
            )
            deleted += cur.rowcount
            (count,) = conn.execute("SELECT COUNT(*) FROM agent_viz_summary").fetchone()
            if count > max_rows:
                cur = conn.execute(
                    "DELETE FROM agent_viz_summary WHERE session_id IN ("
                    "  SELECT session_id FROM agent_viz_summary "
                    "  ORDER BY created_at ASC LIMIT ?"
                    ")",
                    (count - max_rows,),
                )
                deleted += cur.rowcount
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("disk cache prune failed: %s", exc)
    return deleted


def _trim(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _extract_text_from_payload(payload: Any) -> str:
    """Pull a plain-text body from a normalized transcript payload."""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    # CC assistant_message has tool_uses but no text — return ""
    final_text = payload.get("final_text")
    if isinstance(final_text, str):
        return final_text
    return ""


def _scan_prs(events: list[dict[str, Any]]) -> list[str]:
    """Find PR references in tool calls and assistant text.

    Returns a list of short descriptors (URL or `gh pr create` title fragment).
    Deduped, capped at _PR_CAP.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.strip()
        if not s or s in seen:
            return
        seen.add(s)
        found.append(s)

    for ev in events:
        if len(found) >= _PR_CAP:
            break
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = ev.get("kind") or ""

        # LifeOS-style tool_call payload
        if kind == "tool_call":
            tool = str(payload.get("tool") or "")
            args = payload.get("arguments") or {}
            if isinstance(args, dict):
                cmd = str(args.get("command") or "")
                if tool == "Bash" and _GH_PR_CREATE_RE.search(cmd):
                    title_match = re.search(r"--title\s+['\"]([^'\"]+)", cmd)
                    body_match = re.search(r"--body\s+['\"]([^'\"]+)", cmd)
                    bits = []
                    if title_match:
                        bits.append(title_match.group(1))
                    if body_match:
                        bits.append(body_match.group(1)[:200])
                    if bits:
                        add(" — ".join(bits))
                    else:
                        add("gh pr create (no title parsed)")

        # Free-form text in any payload — assistant_message / completed / etc.
        for key in ("text", "final_text"):
            val = payload.get(key)
            if isinstance(val, str):
                for m in _GH_PR_URL_RE.findall(val):
                    add(m)

        # CC tool_result content_preview can contain a PR URL too
        results = payload.get("tool_results")
        if isinstance(results, list):
            for tr in results:
                if isinstance(tr, dict):
                    preview = tr.get("content_preview")
                    if isinstance(preview, str):
                        for m in _GH_PR_URL_RE.findall(preview):
                            add(m)
    return found


def _extract_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull the three context fragments Gemma will summarize."""
    first_user = ""
    final_assistant = ""

    for ev in events:
        if first_user:
            break
        kind = ev.get("kind") or ""
        if kind in ("user_message", "seed"):
            txt = _extract_text_from_payload(ev.get("payload"))
            if txt:
                first_user = txt

    # Walk backwards for the final assistant/completed message.
    for ev in reversed(events):
        if final_assistant:
            break
        kind = ev.get("kind") or ""
        if kind in ("assistant_message", "completed"):
            txt = _extract_text_from_payload(ev.get("payload"))
            if txt:
                final_assistant = txt

    return {
        "first_user": _trim(first_user, _FIRST_MSG_CAP),
        "final_assistant": _trim(final_assistant, _FINAL_MSG_CAP),
        "prs": _scan_prs(events),
    }


def _build_prompt(label: str, ctx: dict[str, Any]) -> str:
    parts: list[str] = [
        "You are summarizing an AI coding agent's work session for a status dashboard.",
        "Return STRICT JSON with two fields:",
        '  "short_label": 2-6 words, Title Case, no trailing period — a node label',
        '  "summary": 1-2 sentences OR up to 4 short bullets (markdown "- "), max ~80 words',
        "",
        "Focus on WHAT the agent worked on / accomplished, not the framing.",
        "Be concrete: name the feature/bug/file area. Do not hedge.",
        "",
        f"Session label (from task description): {label}",
        "",
    ]
    if ctx["first_user"]:
        parts.append("--- Original request ---")
        parts.append(ctx["first_user"])
        parts.append("")
    if ctx["prs"]:
        parts.append("--- Pull requests produced ---")
        for pr in ctx["prs"]:
            parts.append(f"- {pr}")
        parts.append("")
    if ctx["final_assistant"]:
        parts.append("--- Final agent message ---")
        parts.append(ctx["final_assistant"])
        parts.append("")
    parts.append('Respond with JSON only, no prose: {"short_label": "...", "summary": "..."}')
    return "\n".join(parts)


def _fallback_label(label: str) -> str:
    """Heuristic short label when LLM is unavailable or returns garbage."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", label or "")
    return " ".join(words[:5]).strip() or "Untitled"


def _cache_if_terminal(session_id: str, last_activity_at: float, status: str,
                        result: SummaryResult) -> None:
    """Cache `result` for a session whose status is terminal, so it drops out
    of the prefetch candidate list for good. A live session's activity keeps
    moving and might yet produce real content, so callers must not cache for
    it — the next access retries instead (#863)."""
    if not _is_terminal(status):
        return
    now = time.time()
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[session_id] = (last_activity_at, now, result)
    _disk_put(session_id, last_activity_at, result)


async def summarize_session(
    session_id: str,
    *,
    label: str,
    last_activity_at: float,
    events: list[dict[str, Any]],
    status: str = "",
) -> SummaryResult:
    """Return cached summary or compute a fresh one.

    Caller passes events (already fetched) so this module doesn't take a
    dependency on the transcript store / claude_code ingest dispatch logic
    that already lives in api/routes/agents.py.

    `status` controls live-session staleness handling — terminal sessions
    cache forever; non-terminal ones get a grace window
    (_LIVE_REFRESH_GRACE_SECONDS) before being re-summarized even when
    `last_activity_at` advances.
    """
    cached = _cache.get(session_id)
    if cached:
        cached_activity, cached_created_at, cached_result = cached
        if _is_fresh_enough(cached_activity, cached_created_at, last_activity_at, status):
            return cached_result

    # Disk cache check — populated by a previous call (possibly across a
    # restart). Promotes into the in-process cache on hit.
    disk_hit = _disk_get(session_id, last_activity_at, status)
    if disk_hit is not None:
        _cache[session_id] = (last_activity_at, time.time(), disk_hit)
        return disk_hit

    ctx = _extract_context(events)
    # Nothing to summarize: return a deterministic fallback so the UI still
    # gets *something*. A terminal session will never gain content, so cache
    # the fallback to keep it out of the prefetch candidate list forever; a
    # live session might produce content later, so leave it uncached and let
    # the next access retry.
    if not ctx["first_user"] and not ctx["final_assistant"] and not ctx["prs"]:
        result = SummaryResult(
            short_label=_fallback_label(label),
            summary="(No transcript content yet.)",
        )
        _cache_if_terminal(session_id, last_activity_at, status, result)
        return result

    prompt = _build_prompt(label, ctx)
    try:
        # Gemma 4 burns most of its budget on reasoning_content; only
        # `content` is returned in `text` by LocalLLMClient. Anything under
        # ~3000 tokens regularly hits the cap mid-reasoning, leaving content
        # empty. Observed real CC sessions used ~2400 completion tokens
        # (≈9.5k chars reasoning + 200 chars JSON), so 4096 is the floor.
        #
        # 240s timeout: a fresh fast-path summary lands in ~25-30s, but when
        # Gemma is already serving chat / the agent worker, the queue can push
        # past 120s. The frontend has its own 5-minute abort, so this is the
        # outer bound for "Gemma will finish eventually" before we give up.
        text = await generate_text(
            prompt,
            max_tokens=4096,
            temperature=0.2,
            timeout=240.0,
        )
        data = extract_json(text)
        short_label = str(data.get("short_label") or "").strip()
        summary = str(data.get("summary") or "").strip()
        if not short_label:
            short_label = _fallback_label(label)
        if not summary:
            summary = "(empty summary)"
        # Clamp short_label hard so a runaway response doesn't blow up the node.
        words = short_label.split()
        if len(words) > 7:
            short_label = " ".join(words[:7])
    except Exception as exc:  # noqa: BLE001 — never break the panel on summary failure
        logger.warning("session summary failed for %s: %s", session_id, exc)
        fallback = SummaryResult(
            short_label=_fallback_label(label),
            summary=f"(Summary unavailable: {type(exc).__name__})",
        )
        # (#863) A terminal session whose summary call keeps raising was
        # never cached, so the prefetcher retried it on every tick forever.
        # A live session might succeed later, so it stays uncached.
        _cache_if_terminal(session_id, last_activity_at, status, fallback)
        return fallback

    result = SummaryResult(short_label=short_label, summary=summary)
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    now = time.time()
    _cache[session_id] = (last_activity_at, now, result)
    _disk_put(session_id, last_activity_at, result)
    return result


def get_cached_summary(session_id: str, last_activity_at: float, status: str = "") -> SummaryResult | None:
    """Synchronous peek for the snapshot path — never triggers an LLM call.

    Checks the in-process cache first, then the disk cache. A hit on disk is
    promoted into the in-process cache so subsequent snapshot ticks are
    free. Honors the live-session grace window via `status`.
    """
    cached = _cache.get(session_id)
    if cached:
        cached_activity, cached_created_at, cached_result = cached
        if _is_fresh_enough(cached_activity, cached_created_at, last_activity_at, status):
            return cached_result
    disk = _disk_get(session_id, last_activity_at, status)
    if disk is not None:
        _cache[session_id] = (last_activity_at, time.time(), disk)
        return disk
    return None


_LIKE_SPECIAL = re.compile(r"([\\%_])")


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so a query like `100%` matches that literal text
    instead of acting as a `match-anything` pattern."""
    return _LIKE_SPECIAL.sub(r"\\\1", s)


def _make_snippet(text: str, query_lower: str, width: int = 72) -> str:
    """A short, whitespace-collapsed window around the first case-insensitive
    match of `query_lower` in `text`, with ellipses when truncated."""
    collapsed = " ".join(text.split())
    idx = collapsed.lower().find(query_lower)
    if idx < 0:
        return collapsed[:width] + ("…" if len(collapsed) > width else "")
    half = max(0, (width - len(query_lower)) // 2)
    start = max(0, idx - half)
    end = min(len(collapsed), start + width)
    snippet = collapsed[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(collapsed):
        snippet = snippet + "…"
    return snippet


def search_cached_summaries(query: str, limit: int = 200) -> list[dict[str, str]]:
    """Substring-search cached `short_label` + `summary` rows. No LLM, no network.

    Case-insensitive. Returns at most `limit` matches as
    ``{session_id, field, snippet}`` where ``field`` is ``"short_label"`` when
    the short label matched, else ``"summary"``. Only rows already in the disk
    cache are searched — sessions without a generated summary are not covered
    (the client's label tier handles those). Reads only; never re-summarizes.
    """
    q = query.strip()
    if not q:
        return []
    q_lower = q.lower()
    pattern = f"%{_escape_like(q_lower)}%"
    try:
        with sqlite3.connect(_resolve_db_path()) as conn:
            rows = conn.execute(
                "SELECT session_id, short_label, summary FROM agent_viz_summary "
                "WHERE short_label LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' "
                "ORDER BY last_activity_at DESC LIMIT ?",
                (pattern, pattern, int(limit)),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("summary search failed for %r: %s", query, exc)
        return []
    results: list[dict[str, str]] = []
    for session_id, short_label, summary in rows:
        if short_label and q_lower in short_label.lower():
            results.append({
                "session_id": session_id,
                "field": "short_label",
                "snippet": _make_snippet(short_label, q_lower),
            })
        elif summary and q_lower in summary.lower():
            results.append({
                "session_id": session_id,
                "field": "summary",
                "snippet": _make_snippet(summary, q_lower),
            })
    return results


def reset_cache() -> None:
    """Clear in-process cache only. Use for tests; disk cache survives."""
    _cache.clear()


# Initialize the DB lazily on first import — cheap (just ensures the schema
# exists) and avoids forcing every test that imports this module to set up
# a temp DB path. _resolve_db_path() picks up settings at call time.
_init_db()
