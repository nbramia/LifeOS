"""Read-only agent activity visualization endpoints.

Powers the `/agents` UI: a live graph of in-flight and recently-completed
agent worker sessions, plus per-session transcript tailing. See
`docs/specs/technical/agent-worker.md` and issue #133.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.services.agent_worker.session_store import (
    CLI_ENGINE_PREFIXES,
    CLI_SESSION_EVENTS,
    TERMINAL_STATUSES,
    CliSession,
    Session,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services import agent_viz_summary
from api.services import agent_viz_label_override

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


# Module-level lazy singletons. Tests monkeypatch these directly so they can
# point the endpoints at temp-dir-backed stores without hitting the real data
# directory.
_session_store: SessionStore | None = None
_transcript_store: TranscriptStore | None = None

# (#851) Injectable remote-kill runner, forwarded to `teardown_session` for a
# session whose `host` names a machine other than this API host. None (the
# default) uses `remote_spawn.kill_remote_process_group`'s real `subprocess.run`
# over ssh; tests monkeypatch this to a fake that records the argv.
_remote_kill_runner = None

# Labels are stable once derived from a non-fallback source, so cache to avoid
# re-deriving on every snapshot tick. Capped to bound memory if the process
# accumulates a lot of session IDs over a long uptime.
_label_cache: dict[str, str] = {}
_LABEL_CACHE_MAX = 500

# Transcript tail size used for snapshot summary fields. Capping keeps SSE
# tick cost bounded for sessions with long transcripts.
_SUMMARY_TAIL = 100

# How many sessions to list per snapshot. Sessions are returned newest-first.
_SNAPSHOT_LIMIT = 200


# Event kinds that count as errors. Includes operator/peer-initiated kills
# because the frontend renders them as failures and the count chip should
# match. The endswith check picks up future `*_failed`/`*_error` kinds.
_ERROR_KINDS = frozenset({
    "failed",
    "managed_failed",
    "child_failed_internal",
    "killed",
    "cascade_killed",
})


def _get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


def _get_transcript_store() -> TranscriptStore:
    global _transcript_store
    if _transcript_store is None:
        _transcript_store = TranscriptStore()
    return _transcript_store


def api_host_name() -> str:
    """Short hostname of the machine running this API process (#849).

    Used to label every snapshot row with `host` — worker sessions and
    local-transcript CLI sessions always ran here, so they get this value
    directly; remote `cli_sessions` rows carry whatever host their own
    hook posted. Strips any domain suffix the same way the hook script
    does, so a row's `host` reads identically whether it came from this
    function or from a remote hook post. A single function (rather than
    inlining `socket.gethostname()` at each call site) so tests can
    monkeypatch one target.
    """
    return socket.gethostname().split(".")[0]


def _is_error_kind(kind: str) -> bool:
    if kind in _ERROR_KINDS:
        return True
    return kind.endswith("_failed") or kind.endswith("_error")


def _label_for_session(s: Session, events: list[dict[str, Any]]) -> str:
    cached = _label_cache.get(s.session_id)
    if cached is not None:
        return cached

    label: str | None = None

    # Future-proof: if a transcript event ever carries a description directly
    # (e.g. via a future enrichment in worker._claim), use it.
    for ev in events[:5]:
        payload = ev.get("payload") or {}
        if ev.get("kind") in ("claim", "seed", "spawn"):
            desc = (
                payload.get("description")
                or payload.get("task_description")
                or payload.get("prompt")
            )
            if desc:
                label = str(desc)
                break

    # Real worker `claim`/`seed` payloads today only carry `task_id`, so for
    # root sessions look the task description up via the TaskManager. This
    # call is cheap (in-memory dict in the singleton) and the result is
    # cached below.
    if label is None and s.parent_session_id is None and s.task_id:
        try:
            from api.services.task_manager import get_task_manager

            task = get_task_manager().get(s.task_id)
            if task is not None and task.description:
                label = task.description
        except Exception as exc:  # noqa: BLE001 — defensive: never break the snapshot on label lookup
            logger.debug("task_manager lookup failed for %s: %s", s.task_id, exc)

    # Only cache when we found a real label — never the fallback, so a snapshot
    # tick that races the `claim` append doesn't poison the cache.
    found = label is not None
    if label is None:
        label = s.task_id or s.session_id
    label = label.strip().replace("\n", " ")
    if len(label) > 60:
        label = label[:57] + "…"

    if found:
        if len(_label_cache) >= _LABEL_CACHE_MAX:
            # Drop one arbitrary entry to keep the cache bounded.
            _label_cache.pop(next(iter(_label_cache)))
        _label_cache[s.session_id] = label
    return label


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    tool_call_count = 0
    error_count = 0
    last_event_kind = ""
    for ev in events:
        kind = str(ev.get("kind", ""))
        if kind == "tool_call":
            tool_call_count += 1
        if _is_error_kind(kind):
            error_count += 1
        last_event_kind = kind
    return {
        "tool_call_count": tool_call_count,
        "error_count": error_count,
        "last_event_kind": last_event_kind,
    }


def _session_to_dict(s: Session, transcript: TranscriptStore) -> dict[str, Any]:
    try:
        events = transcript.read(s.session_id)
    except Exception as exc:  # noqa: BLE001 — defensive: read should never break the snapshot
        logger.warning("transcript read failed for %s: %s", s.session_id, exc)
        events = []
    tail = events[-_SUMMARY_TAIL:] if len(events) > _SUMMARY_TAIL else events
    summary = _summarize_events(tail)
    return {
        "session_id": s.session_id,
        "task_id": s.task_id,
        "status": s.status,
        # (#851) `Session.host` is the board-assignment field a worker was
        # dispatched to run on; unset (legacy rows, or a session created
        # before #851) falls back to the machine hosting this API process.
        "host": s.host or api_host_name(),
        "routing": s.routing,
        "parent_session_id": s.parent_session_id,
        "root_session_id": s.root_session_id,
        "spawn_depth": s.spawn_depth,
        "yield_waiting_for": s.yield_waiting_for or [],
        "managed_agent_session_id": s.managed_agent_session_id,
        "started_at": s.started_at,
        "last_activity_at": s.last_activity_at,
        "total_input_tokens": s.total_input_tokens,
        "total_output_tokens": s.total_output_tokens,
        "total_cache_creation_tokens": getattr(s, "total_cache_creation_tokens", 0),
        "total_cache_read_tokens": getattr(s, "total_cache_read_tokens", 0),
        "total_dollars": round(s.total_dollars, 6),
        "total_active_seconds": round(s.total_active_seconds, 3),
        "expected_output": s.expected_output,
        "label": _label_for_session(s, events),
        "model_label": _model_label_for_routing(s.routing, getattr(s, "model", None)),
        # (#863) board-assignment + identity fields, passed through so the
        # panel and filters can surface them without re-deriving.
        "model": getattr(s, "model", None),
        "effort": getattr(s, "effort", None),
        "conversation_id": getattr(s, "conversation_id", None),
        "bot": getattr(s, "bot", None),
        "origin": getattr(s, "origin", None),
        "last_event_kind": summary["last_event_kind"],
        "tool_call_count": summary["tool_call_count"],
        "error_count": summary["error_count"],
        # Populated only if a previous /summary call has cached a result for
        # this session at its current last_activity_at. Snapshot ticks never
        # block on the LLM — short labels for unfetched sessions appear when
        # the operator opens the panel.
        "short_label": _short_label_for_snapshot(
            s.session_id, s.last_activity_at or 0.0, s.status or "",
        ),
        # Operator-pinned manual label. When set, the frontend uses it as the
        # node name in preference to short_label and label.
        "custom_label": agent_viz_label_override.get_override(s.session_id),
    }


def _short_label_for_snapshot(session_id: str, last_activity_at: float, status: str = "") -> str | None:
    cached = agent_viz_summary.get_cached_summary(session_id, last_activity_at, status)
    return cached.short_label if cached else None


def _claude_code_enabled() -> bool:
    try:
        from config.settings import settings
        return bool(getattr(settings, "claude_code_viz_enabled", True))
    except Exception:  # noqa: BLE001 — degrade gracefully if settings fail to load
        return False


def _claude_code_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover + parse Claude Code sessions for the snapshot. Cached."""
    if not _claude_code_enabled():
        return [], []
    try:
        from config.settings import settings
        from api.services.claude_code import session_ingest as cc

        return cc.build_snapshot(
            projects_dir=settings.claude_code_projects_dir,
            lookback_days=int(getattr(settings, "claude_code_lookback_days", 7)),
        )
    except Exception as exc:  # noqa: BLE001 — never break the LifeOS snapshot if cc ingest fails
        logger.warning("claude_code snapshot failed: %s", exc)
        return [], []


def _codex_enabled() -> bool:
    try:
        from config.settings import settings
        return bool(getattr(settings, "codex_viz_enabled", True))
    except Exception:  # noqa: BLE001
        return False


def _codex_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover + parse Codex sessions for the snapshot. Cached."""
    if not _codex_enabled():
        return [], []
    try:
        from config.settings import settings
        from api.services.codex import session_ingest as cx

        return cx.build_snapshot(
            sessions_dir=settings.codex_sessions_dir,
            lookback_days=int(getattr(settings, "codex_lookback_days", 7)),
        )
    except Exception as exc:  # noqa: BLE001 — never break the LifeOS snapshot if cx ingest fails
        logger.warning("codex snapshot failed: %s", exc)
        return [], []


def _apply_cli_session_to_dict(sd: dict[str, Any], cli: CliSession) -> None:
    """Merge an event-driven `cli_sessions` row onto a transcript-derived
    snapshot dict for the same session (#849). Event status wins over the
    transcript scan's file-age guess; token/cost fields stay
    transcript-derived — the hook posts no usage data.
    """
    sd["status"] = cli.status
    sd["status_inferred"] = False
    sd["host"] = cli.host
    sd["branch"] = cli.branch
    sd["prompt_preview"] = cli.prompt_preview
    if cli.task_id:
        sd["task_id"] = cli.task_id


def _cli_session_to_dict(cli: CliSession) -> dict[str, Any]:
    """Synthetic snapshot row for a `cli_sessions` row with no matching
    local transcript — a session running on a different machine (#849).
    No token or dollar detail (the hook posts none); `status_inferred` is
    always False because the status here is event-driven by definition.
    """
    if cli.engine == "claude_code":
        from api.services.claude_code import session_ingest as cc
        model_lbl = cc.model_label(cli.model or "")
    elif cli.engine == "codex":
        from api.services.codex import session_ingest as cx
        model_lbl = cx.model_label(cli.model or "")
    else:
        # (#863) An unrecognized engine used to hardcode a Claude tier
        # guess ("Claude") — surface the engine's own name instead.
        model_lbl = cli.engine.replace("_", " ").title() if cli.engine else "Claude"
    return {
        "session_id": cli.session_id,
        "task_id": cli.task_id,
        "status": cli.status,
        "routing": cli.engine,
        "parent_session_id": None,
        "root_session_id": cli.session_id,
        "spawn_depth": 0,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": cli.started_at,
        "last_activity_at": cli.last_event_at,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_dollars": 0.0,
        "total_active_seconds": 0.0,
        "expected_output": None,
        # (#863) Prefer the hook-posted prompt preview — the session id is
        # not a human label.
        "label": cli.prompt_preview or cli.session_id,
        "model_label": model_lbl,
        "last_event_kind": "",
        "tool_call_count": 0,
        "error_count": 0,
        "source": cli.engine,
        "status_inferred": False,
        "project_key": "",
        "decoded_cwd": cli.cwd or "",
        "host": cli.host,
        "branch": cli.branch,
        "prompt_preview": cli.prompt_preview,
    }


def _build_snapshot() -> dict[str, Any]:
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    sessions = session_store.list_sessions(limit=_SNAPSHOT_LIMIT)
    session_dicts = [_session_to_dict(s, transcript_store) for s in sessions]
    # Tag LifeOS sessions with a source discriminator so the frontend can
    # distinguish them from Claude Code sessions in the union below.
    for sd in session_dicts:
        sd.setdefault("source", "lifeos_agent")
        sd.setdefault("model_label", _model_label_for_routing(sd.get("routing")))
    edges = [
        {"from": s.parent_session_id, "to": s.session_id, "type": "spawn"}
        for s in sessions
        if s.parent_session_id
    ]

    # Cross-machine CLI sessions (#849): registered via the hook script's
    # POST /api/agents/cli-sessions/events, keyed the same way
    # transcript-derived rows are (cc:<uuid> / cx:<uuid>). Ids that also
    # have a local transcript merge below (event status wins, tokens stay
    # transcript-derived); whatever's left after both scans ran had no
    # local transcript match — those become synthetic remote rows further
    # down. Popped from this dict as each cc/cx row consumes its match, so
    # what remains at the end is exactly the unmatched set.
    try:
        cli_by_id: dict[str, CliSession] = {
            c.session_id: c for c in session_store.list_cli_sessions()
        }
    except Exception as exc:  # noqa: BLE001 — never break the snapshot on a store error
        logger.warning("cli_sessions read failed: %s", exc)
        cli_by_id = {}

    cc_sessions, cc_edges = _claude_code_snapshot()
    # CC session dicts come from claude_code/session_ingest.py and don't carry
    # the short_label field — attach it the same way (cached-only, no LLM in
    # the snapshot path).
    for sd in cc_sessions:
        sid = sd.get("session_id") or ""
        sd.setdefault(
            "short_label",
            _short_label_for_snapshot(
                sid,
                sd.get("last_activity_at") or 0.0,
                sd.get("status") or "",
            ),
        )
        sd["custom_label"] = agent_viz_label_override.get_override(sid)
        cli = cli_by_id.pop(sid, None)
        if cli is not None:
            _apply_cli_session_to_dict(sd, cli)
        else:
            sd["host"] = api_host_name()
    session_dicts.extend(cc_sessions)
    edges.extend(cc_edges)

    cx_sessions, cx_edges = _codex_snapshot()
    for sd in cx_sessions:
        sid = sd.get("session_id") or ""
        sd.setdefault(
            "short_label",
            _short_label_for_snapshot(
                sid,
                sd.get("last_activity_at") or 0.0,
                sd.get("status") or "",
            ),
        )
        sd["custom_label"] = agent_viz_label_override.get_override(sid)
        cli = cli_by_id.pop(sid, None)
        if cli is not None:
            _apply_cli_session_to_dict(sd, cli)
        else:
            sd["host"] = api_host_name()
    session_dicts.extend(cx_sessions)
    edges.extend(cx_edges)

    # Whatever's left in cli_by_id had no local transcript — a remote host,
    # or (rarely) a local hook post that raced ahead of the transcript
    # scan's cache. Bound to the same recency window the transcript scan
    # already applies per engine so a stale registration doesn't linger.
    now = time.time()
    try:
        from config.settings import settings
        cc_cutoff = now - int(getattr(settings, "claude_code_lookback_days", 7)) * 86400
        cx_cutoff = now - int(getattr(settings, "codex_lookback_days", 7)) * 86400
    except Exception:  # noqa: BLE001 — degrade to "no cutoff" if settings fail to load
        cc_cutoff = cx_cutoff = 0.0
    for cli in cli_by_id.values():
        cutoff = cc_cutoff if cli.engine == "claude_code" else cx_cutoff
        if cli.last_event_at < cutoff:
            continue
        sd = _cli_session_to_dict(cli)
        sd.setdefault(
            "short_label",
            _short_label_for_snapshot(
                cli.session_id,
                sd.get("last_activity_at") or 0.0,
                sd.get("status") or "",
            ),
        )
        sd["custom_label"] = agent_viz_label_override.get_override(cli.session_id)
        session_dicts.append(sd)

    return {
        "sessions": session_dicts,
        "edges": edges,
        "generated_at": int(time.time()),
    }


def _model_label_for_routing(routing: str | None, model: str | None = None) -> str:
    if (routing or "local") == "local":
        return "Local"
    if routing == "ask":
        # (#863) A worker session parked waiting on the operator has no
        # model running at all — it must never render a Claude tier guess.
        return "Waiting on you"
    if routing == "remote":
        # (#809) `#cloud` — the configured remote OpenAI-compatible provider,
        # not an Anthropic model, so it must not fall into the Claude-model-
        # name guessing below.
        try:
            from config.settings import settings
            return settings.remote_llm_label or "Remote"
        except Exception:  # noqa: BLE001
            return "Remote"
    from api.services.agent_worker.hermes_session import HERMES_ROUTING
    if routing == HERMES_ROUTING:
        # (#850) Hermes sessions used to fall through to the Claude-model-name
        # guess below and get mislabeled "Claude" — Hermes runs its own
        # DeepSeek-backed engine, not an Anthropic model.
        # (#863) When the session's `model` column is populated, surface it
        # alongside "Hermes" — nothing else records what Hermes last used.
        if model:
            return f"Hermes · {model}"
        return "Hermes"
    if routing in ("claude_code", "code"):
        return "Claude Code"
    if routing == "codex":
        return "Codex"
    try:
        from config.settings import settings
        m = (settings.agent_managed_model or "").lower()
    except Exception:  # noqa: BLE001
        m = ""
    if "haiku" in m:
        return "Haiku"
    if "sonnet" in m:
        return "Sonnet"
    if "opus" in m:
        return "Opus"
    return "Claude"


@router.get("/snapshot")
async def get_snapshot() -> dict[str, Any]:
    """Full current snapshot of agent sessions + spawn edges."""
    return _build_snapshot()


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Recent transcript events for one session (paginated tail).

    Dispatches by `cc:` prefix — Claude Code session ids are served by the
    `claude_code` ingest service, everything else by the LifeOS transcript
    store.
    """
    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from config.settings import settings
            from api.services.claude_code import session_ingest as cc

            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tail = events[-limit:] if len(events) > limit else events
        return {"session_id": session_id, "events": tail, "total": len(events)}

    if session_id.startswith("cx:"):
        if not _codex_enabled():
            raise HTTPException(status_code=404, detail="codex viz disabled")
        try:
            from config.settings import settings
            from api.services.codex import session_ingest as cx

            events = cx.read_normalized_events(session_id, settings.codex_sessions_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tail = events[-limit:] if len(events) > limit else events
        return {"session_id": session_id, "events": tail, "total": len(events)}

    transcript_store = _get_transcript_store()
    try:
        events = transcript_store.read(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tail = events[-limit:] if len(events) > limit else events
    return {"session_id": session_id, "events": tail, "total": len(events)}


@router.get("/sessions/{session_id}/summary")
async def get_session_summary(session_id: str) -> dict[str, Any]:
    """Concise "what did this session work on" summary for the /agents panel.

    Pulls the first user message, final assistant message, and any PR
    references out of the transcript, then asks the local Gemma LLM to
    produce a short node label (2-6 words) and a 1-2 sentence recap.

    Cached by (session_id, last_activity_at). A session that hasn't moved
    since the last call gets a cache hit and zero LLM cost.
    """
    # Resolve the session's label + last_activity_at + status + events.
    last_activity = 0.0
    label = session_id
    status = ""

    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from config.settings import settings
            from api.services.claude_code import session_ingest as cc

            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Find this CC session in the cached snapshot to pull label + activity.
        cc_sessions, _ = _claude_code_snapshot()
        match = next((s for s in cc_sessions if s.get("session_id") == session_id), None)
        if match:
            label = str(match.get("label") or session_id)
            last_activity = float(match.get("last_activity_at") or 0.0)
            status = str(match.get("status") or "")
    elif session_id.startswith("cx:"):
        if not _codex_enabled():
            raise HTTPException(status_code=404, detail="codex viz disabled")
        try:
            from config.settings import settings
            from api.services.codex import session_ingest as cx

            events = cx.read_normalized_events(session_id, settings.codex_sessions_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cx_sessions, _ = _codex_snapshot()
        match = next((s for s in cx_sessions if s.get("session_id") == session_id), None)
        if match:
            label = str(match.get("label") or session_id)
            last_activity = float(match.get("last_activity_at") or 0.0)
            status = str(match.get("status") or "")
    else:
        session_store = _get_session_store()
        s = session_store.get_by_session_id(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        transcript_store = _get_transcript_store()
        try:
            events = transcript_store.read(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        label = _label_for_session(s, events)
        last_activity = float(s.last_activity_at or 0.0)
        status = s.status or ""

    result = await agent_viz_summary.summarize_session(
        session_id,
        label=label,
        last_activity_at=last_activity,
        events=events,
        status=status,
    )
    return {"session_id": session_id, **result.as_dict()}


class LabelOverrideRequest(BaseModel):
    """Body for PUT /api/agents/sessions/{id}/label."""
    label: str = Field(default="", max_length=200)


@router.put("/sessions/{session_id}/label")
async def set_session_label(session_id: str, body: LabelOverrideRequest) -> dict[str, Any]:
    """Set or clear an operator-pinned manual label for a session node.

    A non-empty label overrides the auto-derived node name (AI short_label /
    task description) everywhere it's shown. An empty label clears the
    override and reverts the node to auto-naming. Durable across restarts.
    """
    custom = agent_viz_label_override.set_override(session_id, body.label)
    return {"session_id": session_id, "custom_label": custom or None}


@router.get("/search")
async def search_sessions(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Substring search over cached session summaries (short_label + summary).

    Powers the /agents search box's summary tiers. The client handles the
    label tier locally; this endpoint covers the LLM-generated summary text
    that isn't fully loaded client-side. Reads only the disk cache — it never
    triggers summary generation, so it's cheap and safe to call on keystroke.
    """
    matches = agent_viz_summary.search_cached_summaries(q, limit=limit)
    return {"query": q, "matches": matches}


@router.get("/stream")
async def stream_snapshots() -> StreamingResponse:
    """SSE stream emitting a full snapshot every ~2 seconds."""

    async def generate():
        yield ": ok\n\n"
        while True:
            try:
                snap = _build_snapshot()
                yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
            except Exception as exc:  # noqa: BLE001 — keep the stream alive on errors
                logger.warning("snapshot stream tick failed: %s", exc)
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Kanban board (#850) — vault-task-backed view of /agents. Lane derivation
# lives in api/services/agent_board.py; this section only reads the task,
# scheduler, and session stores, calls into that pure module for the
# decision, and performs the write. See docs/specs/technical/agent-viz.md.
# ---------------------------------------------------------------------------

# Board SSE tick interval. The task watcher's own debounce is 2.0s (see
# api/services/task_watcher.py); ticking the board every 0.5s on top of that
# (plus the shared cache's 0.25s TTL, see _BOARD_CACHE_TTL below) keeps
# "reflects an external vault edit" comfortably under the 3s budget without a
# heavier cross-thread push mechanism — both TaskManager and SchedulerStore
# serve from in-memory indexes, so rebuilding the board is cheap. A tick only
# emits when the board actually changed (compared by a cheap signature), so
# an idle board doesn't spam the client. See docs/specs/technical/agent-viz.md
# for the full worst-case arithmetic.
_BOARD_STREAM_INTERVAL = 0.5


def _task_card(task, sessions_by_task: dict[str, list[dict[str, Any]]],
                open_question_by_task: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from api.services import agent_board

    candidates = sessions_by_task.get(task.id) or []
    session = max(candidates, key=lambda s: s.get("last_activity_at") or 0) if candidates else None
    pq = open_question_by_task.get(task.id)
    return {
        "kind": "task",
        "id": task.id,
        "title": task.description,
        "notes": task.notes,
        "status": task.status,
        "tags": list(task.tags),
        "assignee": agent_board.derive_assignee(task.tags),
        "fields": dict(task.fields),
        "context": task.context,
        "updated_at": task.updated_at,
        "session": session,
        "pending_question": (
            {
                "id": pq["id"],
                "session_id": pq["session_id"],
                "question": pq["question"],
                "asked_at": pq["sent_at"],
                "bot": pq.get("bot"),
            }
            if pq else None
        ),
    }


def _schedule_card(entry) -> dict[str, Any]:
    last_run = None
    if entry.last_triggered_at:
        last_run = {
            "at": entry.last_triggered_at,
            "outcome": entry.last_status or "",
            "snippet": entry.last_result or "",
        }
    return {
        "kind": "schedule",
        "id": entry.id,
        "name": entry.name,
        "message_content": entry.message_content,
        "enabled": entry.enabled,
        "next_fire_at": entry.next_trigger_at,
        "recurring": entry.schedule_type == "cron",
        "last_run": last_run,
    }


def _build_board() -> dict[str, Any]:
    from api.services import agent_board
    from api.services.task_manager import get_task_manager
    from api.services.scheduler_store import get_scheduler_store

    task_manager = get_task_manager()
    scheduler_store = get_scheduler_store()
    session_store = _get_session_store()

    tasks = task_manager.list_tasks()

    sessions_by_task: dict[str, list[dict[str, Any]]] = {}
    for sd in _build_snapshot()["sessions"]:
        tid = sd.get("task_id")
        if tid:
            sessions_by_task.setdefault(tid, []).append(sd)

    open_question_by_task: dict[str, dict[str, Any]] = {}
    for q in session_store.list_open_questions():
        open_question_by_task[q["task_id"]] = q

    lanes: dict[str, list[dict[str, Any]]] = {lane: [] for lane in agent_board.LANES}
    for task in tasks:
        lane = agent_board.derive_lane(task.status, task.tags)
        lanes[lane].append(_task_card(task, sessions_by_task, open_question_by_task))

    for entry in scheduler_store.list_all():
        bucket = "scheduled" if agent_board.is_schedule_active(entry.enabled, entry.next_trigger_at) else "done"
        lanes[bucket].append(_schedule_card(entry))

    return {"lanes": lanes, "generated_at": int(time.time())}


# Module-level (built_at, board) cache used ONLY by the stream's own tick —
# NOT by GET /board (round-2 finding 6). `_build_board` reads every task,
# every schedule entry, and up to 200 session transcript files — cheap once,
# but every open board tab's stream connection rebuilding independently on
# every tick would multiply that cost by the number of open tabs. Sharing
# one build per short window keeps concurrent stream connections reading
# identical data without adding this TTL's staleness to a direct GET.
# TTL is intentionally much shorter than the tick interval — it only exists
# to de-duplicate simultaneous stream connections within the same instant,
# not to skip rebuilds between ticks.
_BOARD_CACHE_TTL = 0.25
_board_cache: tuple[float, dict[str, Any]] | None = None


async def _get_board_cached() -> dict[str, Any]:
    global _board_cache
    now = time.monotonic()
    if _board_cache is not None and now - _board_cache[0] < _BOARD_CACHE_TTL:
        return _board_cache[1]
    board = await run_in_threadpool(_build_board)
    _board_cache = (now, board)
    return board


def _invalidate_board_cache() -> None:
    """Drop the cached board so the next stream tick rebuilds it immediately
    instead of possibly serving a pre-write board for up to `_BOARD_CACHE_TTL`
    more seconds. Called after every board write (lane-move, accept, answer).
    """
    global _board_cache
    _board_cache = None


@router.get("/board")
async def get_board() -> dict[str, Any]:
    """Full current board view model — see `_build_board`.

    Always built fresh, never served from `_board_cache` — an explicit GET
    is a direct client request (e.g. the drawer's own `await putTask();
    await fetchBoard()` after a save) and must reflect the write that just
    happened, not a cache built before it (round-2 finding 6).
    """
    return await run_in_threadpool(_build_board)


@router.get("/board/stream")
async def stream_board() -> StreamingResponse:
    """SSE stream emitting the board whenever it changes.

    Ticks every `_BOARD_STREAM_INTERVAL` seconds but only sends when the
    lanes actually differ from the last-sent snapshot, so a page left open
    doesn't re-render on every tick.
    """

    async def generate():
        yield ": ok\n\n"
        last_signature: str | None = None
        while True:
            try:
                board = await _get_board_cached()
                signature = json.dumps(board["lanes"], sort_keys=True, default=str)
                if signature != last_signature:
                    last_signature = signature
                    yield f"event: board\ndata: {json.dumps(board, default=str)}\n\n"
            except Exception as exc:  # noqa: BLE001 — keep the stream alive on errors
                logger.warning("board stream tick failed: %s", exc)
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(_BOARD_STREAM_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


class LaneMoveRequest(BaseModel):
    """Body for PUT /api/agents/board/cards/{id}/lane."""
    lane: str
    assignee: str | None = None


@router.put("/board/cards/{card_id}/lane")
async def move_board_card(card_id: str, body: LaneMoveRequest) -> dict[str, Any]:
    """Move a task card to `lane`, writing the corresponding status/tag at
    once. See `api.services.agent_board.plan_lane_move` for the rules.
    """
    from api.services import agent_board
    from api.services.task_manager import get_task_manager, TaskConflictError

    task_manager = get_task_manager()
    task = task_manager.get(card_id)
    if task is None:
        raise HTTPException(status_code=404, detail="card not found")

    plan = agent_board.plan_lane_move(task.status, task.tags, body.lane, body.assignee)
    if plan.error is not None:
        status_code, detail = plan.error
        raise HTTPException(status_code=status_code, detail=detail)

    write_kwargs: dict[str, Any] = {}
    if plan.status is not None:
        write_kwargs["status"] = plan.status
    if plan.tags is not None:
        write_kwargs["tags"] = plan.tags

    if write_kwargs:
        try:
            task = task_manager.update(card_id, **write_kwargs)
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="card not found")
        _invalidate_board_cache()

    lane = agent_board.derive_lane(task.status, task.tags)
    return {"id": task.id, "lane": lane, "status": task.status, "tags": list(task.tags)}


@router.post("/board/cards/{card_id}/accept")
async def accept_board_card(card_id: str) -> dict[str, Any]:
    """Move a Review card to Done by adding the `accepted` tag. Idempotent —
    calling this on an already-accepted, already-done card is a no-op.
    """
    from api.services import agent_board
    from api.services.task_manager import get_task_manager, TaskConflictError

    task_manager = get_task_manager()
    task = task_manager.get(card_id)
    if task is None:
        raise HTTPException(status_code=404, detail="card not found")

    tags_norm = {t.lstrip("#").lower() for t in task.tags}
    already_accepted = agent_board.ACCEPTED_TAG in tags_norm
    if agent_board.derive_lane(task.status, task.tags) != "review" and not already_accepted:
        raise HTTPException(status_code=409, detail="card is not in the Review lane")

    needs_tag = not already_accepted
    needs_status = task.status != "done"
    if needs_tag or needs_status:
        new_tags = list(task.tags) + ([agent_board.ACCEPTED_TAG] if needs_tag else [])
        try:
            task = task_manager.update(card_id, status="done", tags=new_tags)
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="card not found")
        _invalidate_board_cache()

    lane = agent_board.derive_lane(task.status, task.tags)
    return {"id": task.id, "lane": lane, "status": task.status, "tags": list(task.tags)}


@router.get("/pending-questions")
async def list_pending_questions() -> dict[str, Any]:
    """Unanswered agent questions across all sessions — the board's "waiting
    on an answer" list. Same rows `worker.py::_process_clarification_answers`
    will eventually drain, just read before they're answered.
    """
    session_store = _get_session_store()
    rows = session_store.list_open_questions()
    return {
        "questions": [
            {
                "id": r["id"],
                "task_id": r["task_id"],
                "session_id": r["session_id"],
                "question": r["question"],
                "asked_at": r["sent_at"],
                "bot": r.get("bot"),
            }
            for r in rows
        ],
    }


class PendingQuestionAnswerRequest(BaseModel):
    """Body for POST /api/agents/pending-questions/{id}/answer."""
    # 4096 mirrors Telegram's message cap — the answer becomes the agent's
    # next turn via the same path a Telegram reply takes (see the handler).
    answer: str = Field(..., min_length=1, max_length=4096)


@router.post("/pending-questions/{question_id}/answer")
async def answer_pending_question(question_id: int, body: PendingQuestionAnswerRequest) -> dict[str, Any]:
    """Answer a pending question from the board drawer.

    Writes the same `answer`/`answered_at` columns a Telegram reply would via
    `SessionStore.deposit_answer` — `worker.py::_process_clarification_answers`
    picks the row up and resumes the session on its next tick, unchanged.
    """
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    session_store = _get_session_store()
    ok = session_store.deposit_answer_by_id(question_id, answer)
    if not ok:
        raise HTTPException(status_code=404, detail="question not found, already answered, or timed out")
    _invalidate_board_cache()
    return {"ok": True, "id": question_id}


# ---------------------------------------------------------------------------
# Agent threads for web /chat (#236, Phase 3 of #233)
# ---------------------------------------------------------------------------


class SpawnRequest(BaseModel):
    """Body for POST /api/agents/spawn — operator agent spawn from web chat."""
    prompt: str
    # "local" | "claude" force the model; "auto"/None routes via preflight.
    routing: str | None = None


class ReplyRequest(BaseModel):
    """Body for POST /api/agents/threads/{id}/reply."""
    text: str


def _thread_dict(s: Session) -> dict[str, Any]:
    """Lightweight thread projection for the panel — no transcript read.

    The list endpoint is polled, so it avoids `_session_to_dict`'s per-session
    JSONL read (whose event-summary fields the panel doesn't use). `label`
    resolves via the cached TaskManager lookup, not the transcript.
    """
    return {
        "session_id": s.session_id,
        "task_id": s.task_id,
        "status": s.status,
        "routing": s.routing,
        "parent_session_id": s.parent_session_id,
        "root_session_id": s.root_session_id,
        "started_at": s.started_at,
        "last_activity_at": s.last_activity_at,
        "total_dollars": round(s.total_dollars, 6),
        "expected_output": s.expected_output,
        "label": _label_for_session(s, []),
        "model_label": _model_label_for_routing(s.routing),
        "origin": getattr(s, "origin", None),
        "resumable": s.status in TERMINAL_STATUSES,
    }


def _reconstruct_conversation(messages: list[dict], events: list[dict]) -> list[dict]:
    """Build a readable conversation — `[{role, text, tools:[{name, input}]}]` —
    for the web /chat thread view.

    Prefers the local `messages` table (the authoritative structured
    conversation for local / operator sessions); falls back to the managed
    transcript events for cloud sessions whose turns live remotely. Tool calls
    are attached to the assistant turn that issued them so the UI can show them
    as collapsible detail under the reply.
    """
    turns: list[dict] = []

    if messages:
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "system":
                continue
            if role == "user":
                # String user content is a real prompt / follow-up. List content
                # is tool results fed back to the model — internal plumbing, skip.
                if isinstance(content, str) and content.strip():
                    turns.append({"role": "user", "text": content.strip(), "tools": []})
                continue
            if role == "assistant":
                text_parts: list[str] = []
                tools: list[dict] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and block.get("text"):
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tools.append({
                                "name": block.get("name", "tool"),
                                "input": block.get("input", {}),
                            })
                text = "\n".join(p.strip() for p in text_parts if p and p.strip()).strip()
                if text or tools:
                    turns.append({"role": "assistant", "text": text, "tools": tools})
        return turns

    # /claude sessions: turns are reconstructed from the dedicated
    # `claude_code_*` transcript events (the CLI doesn't write to the
    # `messages` table). Detect by event-kind shape rather than by routing
    # so unit-test fixtures work without a session row.
    if any(ev.get("kind", "").startswith("claude_code_") for ev in events):
        return _reconstruct_claude_code_conversation(events)

    # Managed (cloud) sessions: turns live in the transcript, not the DB.
    pending_tools: list[dict] = []
    for ev in events:
        kind = ev.get("kind", "")
        payload = ev.get("payload") or {}
        if kind in ("managed_event_agent.mcp_tool_use", "managed_event_agent.tool_use", "tool_call"):
            pending_tools.append({
                "name": payload.get("name") or payload.get("tool") or "tool",
                "input": payload.get("input") or payload.get("arguments") or {},
            })
        elif kind == "managed_event_agent.message":
            text_parts = [
                c.get("text") for c in (payload.get("content") or [])
                if isinstance(c, dict) and c.get("text")
            ]
            text = "\n".join(t.strip() for t in text_parts if t).strip()
            if text or pending_tools:
                turns.append({"role": "assistant", "text": text, "tools": pending_tools})
                pending_tools = []
    if pending_tools:
        turns.append({"role": "assistant", "text": "", "tools": pending_tools})
    return turns


def _reconstruct_claude_code_conversation(events: list[dict]) -> list[dict]:
    """Build /chat-friendly turns from a routing='claude_code' transcript.

    Events of interest:
      - ``claude_code_user_prompt`` — operator prompt or threaded reply,
        starts a new user turn
      - ``claude_code_notify`` / ``claude_code_clarify`` — assistant body,
        accumulated into the current assistant turn
      - ``claude_code_tool_use`` — attached to the current assistant turn
    Everything else (init, completion markers, failure events) is metadata
    that the events panel already surfaces, so it's skipped here.
    """
    turns: list[dict] = []
    cur_assistant: dict | None = None

    def _flush():
        nonlocal cur_assistant
        if cur_assistant and (cur_assistant["text"] or cur_assistant["tools"]):
            turns.append(cur_assistant)
        cur_assistant = None

    for ev in events:
        kind = ev.get("kind", "")
        payload = ev.get("payload") or {}
        if kind == "claude_code_user_prompt":
            _flush()
            text = (payload.get("text") or "").strip()
            if text:
                turns.append({"role": "user", "text": text, "tools": []})
            continue
        if kind in ("claude_code_notify", "claude_code_clarify"):
            body = (payload.get("body") or payload.get("text") or "").strip()
            if not body:
                # The executor stores `body_chars` for these events to keep the
                # transcript small; the full body lives in messages streamed to
                # Telegram. /chat falls back to the cardinal label.
                body = "(notification)" if kind == "claude_code_notify" else "(clarification)"
            if cur_assistant is None:
                cur_assistant = {"role": "assistant", "text": body, "tools": []}
            else:
                cur_assistant["text"] = (cur_assistant["text"] + "\n\n" + body).strip()
            continue
        if kind == "claude_code_tool_use":
            if cur_assistant is None:
                cur_assistant = {"role": "assistant", "text": "", "tools": []}
            cur_assistant["tools"].append({
                "name": payload.get("name") or "tool",
                "input": payload.get("input") or {},
            })
            continue

    _flush()
    return turns


@router.get("/threads")
async def list_threads(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """List recent/resumable agent threads (root sessions only) for /chat.

    Spawned children are internal to a parent's flow, so only top-level
    sessions (no `parent_session_id`) are surfaced as conversable threads.
    """
    session_store = _get_session_store()
    sessions = session_store.list_sessions(limit=_SNAPSHOT_LIMIT)
    threads = [_thread_dict(s) for s in sessions if not s.parent_session_id]
    return {"threads": threads[:limit], "total": len(threads)}


@router.get("/threads/{session_id}")
async def get_thread(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """A single thread: session metadata + a transcript tail."""
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    session = session_store.get_by_session_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="thread not found")
    try:
        events = transcript_store.read(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tail = events[-limit:] if len(events) > limit else events
    # Reconstructed conversation for the /chat thread view: user/assistant
    # turns with the agent's tool calls attached (collapsible in the UI).
    try:
        messages = session_store.get_messages(session_id)
    except Exception:  # noqa: BLE001 — never break the read on a messages-table hiccup
        messages = []
    conversation = _reconstruct_conversation(messages, events)
    return {
        "thread": _thread_dict(session),
        "conversation": conversation,
        "events": tail,
        "total": len(events),
    }


@router.post("/threads/{session_id}/reply")
async def reply_to_thread(session_id: str, body: ReplyRequest) -> dict[str, Any]:
    """Reply to a completed/failed thread to resume it as a follow-up turn.

    Deposits into the same follow-up path a Telegram reply consumes — the
    worker's next tick reopens the session via `_resume_as_followup`.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="reply text is required")
    session_store = _get_session_store()
    session = session_store.get_by_session_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if session.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"thread is {session.status}; can only reply to a finished thread",
        )
    session_store.enqueue_web_followup(session_id, session.task_id, text)
    return {"ok": True, "session_id": session_id, "status": "queued"}


@router.post("/spawn")
async def spawn_agent(body: SpawnRequest) -> dict[str, Any]:
    """Spawn an operator agent on demand (#235's create_operator_session)."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    routing = body.routing
    explicit = routing if routing in ("local", "claude") else None  # "auto"/None → preflight
    from api.services.agent_worker.operator_spawn import create_operator_session

    session_store = _get_session_store()
    result = await asyncio.to_thread(
        create_operator_session, session_store, prompt, explicit_routing=explicit,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "spawn failed"))
    if result.get("needs_routing"):
        # Preflight was ambiguous about the model. The web surface has no inline
        # routing-clarification flow (unlike Telegram), so don't leave a parked
        # session that never runs — tear it down and ask for an explicit model.
        session_store.delete_session(result["session_id"])
        raise HTTPException(
            status_code=409,
            detail="Ambiguous which model to use — retry with routing 'local' or 'claude'.",
        )
    return result


class KillRequest(BaseModel):
    reason: str = ""


def _collect_subtree(session_store: SessionStore, target: Session) -> list[Session]:
    """Return `target` followed by every non-self descendant in its subtree.

    Walks via `parent_session_id` so non-root targets only take down their
    own children — not unrelated peers under the same root.
    """
    root_id = target.root_session_id or target.session_id
    all_in_root: list[Session] = list(session_store.list_descendants(root_id))
    if target.session_id != root_id:
        # `list_descendants` excludes the root itself; ensure we have the
        # root in the pool too in case the target's parent chain runs back
        # up through it.
        root_session = session_store.get_by_session_id(root_id)
        if root_session is not None:
            all_in_root.append(root_session)

    children_of: dict[str, list[Session]] = {}
    for s in all_in_root:
        if s.session_id == target.session_id:
            continue
        children_of.setdefault(s.parent_session_id or "", []).append(s)

    subtree: list[Session] = [target]
    queue: list[str] = [target.session_id]
    seen: set[str] = {target.session_id}
    while queue:
        sid = queue.pop(0)
        for child in children_of.get(sid, []):
            if child.session_id in seen:
                continue
            seen.add(child.session_id)
            subtree.append(child)
            queue.append(child.session_id)
    return subtree


def _maybe_managed_driver():
    """Lazy-instantiate a ManagedAgentsDriver when credentials are available.

    Returning `None` is fine — `teardown_session` then degrades to a
    local-only kill, and the worker's next managed poll reconciles the
    remote side. We don't reuse the worker's driver because the worker
    runs in a separate process.
    """
    try:
        from config.settings import settings
        api_key = settings.anthropic_api_key
        if not api_key:
            return None
        from api.services.agent_worker.managed_driver import ManagedAgentsDriver
        return ManagedAgentsDriver(api_key=api_key)
    except Exception as exc:  # noqa: BLE001 — never block the kill on driver setup
        logger.warning("operator kill: managed driver unavailable: %s", exc)
        return None


@router.post("/sessions/{session_id}/kill")
async def operator_kill_session(session_id: str, body: KillRequest | None = None) -> dict[str, Any]:
    """Operator-initiated kill: stop the target session and all descendants
    in its subtree. Target gets an `operator_killed` transcript event;
    descendants get `cascade_killed`.

    Local-network only — must NOT be exposed via Tailscale Funnel or the
    public MCP HTTP transport.
    """
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    reason = (body.reason if body else "").strip() if body else ""

    target = session_store.get_by_session_id(session_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    if target.status in TERMINAL_STATUSES:
        return {"killed": [], "failures": [], "reason": f"already {target.status}"}

    subtree = _collect_subtree(session_store, target)
    driver = _maybe_managed_driver()
    try:
        from api.services.agent_worker.inter_agent import teardown_session as _teardown

        def _run_teardown() -> tuple[list[str], list[dict[str, str]]]:
            # #379: the per-CLI-child subprocess reap inside teardown_session does
            # a blocking SIGTERM→grace(up to ~2s)→SIGKILL. Run the whole subtree
            # teardown off the event loop (mirror spawn_agent's asyncio.to_thread)
            # so a multi-node kill can't stall all HTTP for N×grace seconds.
            killed: list[str] = []
            failures: list[dict[str, str]] = []
            for idx, s in enumerate(subtree):
                if s.status in TERMINAL_STATUSES:
                    continue
                if idx == 0:
                    kind = "operator_killed"
                    payload = {
                        "reason": reason,
                        "managed_remote": s.managed_agent_session_id,
                    }
                else:
                    kind = "cascade_killed"
                    payload = {
                        "root": target.session_id,
                        "reason": reason,
                        "managed_remote": s.managed_agent_session_id,
                    }
                result = _teardown(
                    session_store, transcript_store, s,
                    transcript_kind=kind,
                    transcript_payload=payload,
                    managed_driver=driver,
                    remote_kill_runner=_remote_kill_runner,
                )
                killed.append(s.session_id)
                if result.get("managed_failure"):
                    failures.append({
                        "session_id": s.session_id,
                        "reason": result["managed_failure"],
                    })
            return killed, failures

        killed, failures = await asyncio.to_thread(_run_teardown)
        return {"killed": killed, "failures": failures}
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass


class CCResumeRequest(BaseModel):
    """Body for POST /api/agents/sessions/{id}/resume — accepts overrides
    for the spawn env when the systemd-inherited env isn't enough."""
    extra_env: dict[str, str] = {}


class CCPaneBindRequest(BaseModel):
    """Body for POST /api/agents/cc-pane-bind — written by the SessionStart
    hook script on every `claude` start. Lets the /agents Focus / Go To
    button target sessions that were never opened via /agents Resume.
    """
    session_id: str = Field(..., min_length=1, max_length=128)
    pane_id: int = Field(..., ge=0)
    cwd: str = Field(default="", max_length=4096)


class CliSessionEventRequest(BaseModel):
    """Body for POST /api/agents/cli-sessions/events — posted by
    `scripts/lifeos-agent-hook.sh` from any machine, on Claude Code /
    Codex SessionStart, UserPromptSubmit, Stop, and SessionEnd (#849).

    Unlike /cc-pane-bind and /cx-pane-bind, this endpoint is reachable over
    the tailnet (bearer-token gated, not localhost-only) — it's what lets a
    session on a laptop or another box register itself with /agents.
    """
    engine: str = Field(..., min_length=1, max_length=32)
    event: str = Field(..., min_length=1, max_length=32)
    session_id: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=255)
    cwd: str = Field(default="", max_length=4096)
    transcript_path: str = Field(default="", max_length=4096)
    branch: str = Field(default="", max_length=255)
    model: str = Field(default="", max_length=128)
    prompt_preview: str = Field(default="", max_length=4096)
    task_id: str = Field(default="", max_length=128)
    pane_id: int | None = Field(default=None, ge=0)
    wezterm_pid: int | None = Field(default=None, ge=0)


def _check_agent_hook_auth(request: Request) -> None:
    """Bearer-token gate for POST /api/agents/cli-sessions/events (#849).

    Mirrors `api/routes/hermes_proxy.py`'s `_check_hermes_inbound_auth`:
    disabled (503) until an operator sets `LIFEOS_AGENT_HOOK_TOKEN`, since
    an empty token here would mean "accept unauthenticated session data
    from the tailnet" rather than "send no auth" (which is what an empty
    *outbound* token means elsewhere in this codebase).
    """
    from config.settings import settings

    expected = settings.agent_hook_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CLI session registration disabled: set LIFEOS_AGENT_HOOK_TOKEN to enable.",
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@router.post("/cli-sessions/events")
async def cli_session_event(request: Request, body: CliSessionEventRequest) -> dict[str, Any]:
    """Register one Claude Code / Codex CLI lifecycle event (#849).

    Posted by `scripts/lifeos-agent-hook.sh` on SessionStart,
    UserPromptSubmit, Stop, and SessionEnd, from any machine. Applies the
    event to the `cli_sessions` table (see
    `SessionStore.record_cli_session_event` for the status machine) so
    `/agents` shows sessions from every machine, not just the one hosting
    the API.

    When the event carries `pane_id` AND its `host` matches this API's own
    host AND the request itself arrived from loopback, the pane mapping is
    also written into `cc_wezterm_store` — the same store `/cc-pane-bind`
    and `/cx-pane-bind` write to — so Focus keeps working for local
    sessions registered this way. The loopback check matters because
    `host` is client-supplied and this endpoint is reachable off-host: a
    remote, bearer-authenticated caller naming this API's hostname could
    otherwise redirect Go To for a real local session by supplying an
    arbitrary `pane_id`. Requests that aren't from loopback (or don't name
    this host) still record the event and pane id on the `cli_sessions`
    row — they just don't touch the shared pane store.
    """
    _check_agent_hook_auth(request)

    if body.engine not in CLI_ENGINE_PREFIXES:
        raise HTTPException(status_code=422, detail=f"unknown engine: {body.engine!r}")
    if body.event not in CLI_SESSION_EVENTS:
        raise HTTPException(status_code=422, detail=f"unknown event: {body.event!r}")

    store = _get_session_store()
    cli = store.record_cli_session_event(
        engine=body.engine,
        event=body.event,
        session_id=body.session_id,
        host=body.host,
        cwd=body.cwd or None,
        transcript_path=body.transcript_path or None,
        branch=body.branch or None,
        model=body.model or None,
        prompt=body.prompt_preview or None,
        task_id=body.task_id or None,
        pane_id=body.pane_id,
        wezterm_pid=body.wezterm_pid,
    )

    client_host = request.client.host if request.client else ""
    if (
        body.pane_id is not None
        and body.host == api_host_name()
        and client_host in ("127.0.0.1", "::1")
    ):
        try:
            from api.services.cc_wezterm_store import get_default_store
            get_default_store().upsert(
                cli.session_id, int(body.pane_id), body.cwd or "",
                wezterm_pid=body.wezterm_pid or 0,
            )
        except Exception as exc:  # noqa: BLE001 — pane mapping is a nice-to-have, never fail the event
            logger.warning("cc_wezterm_store upsert failed for %s: %s", cli.session_id, exc)

    # (#851) A `session_start` naming a task links this CLI session to its
    # board card and moves the card to In progress — the interactive
    # terminal `POST /board/cards/{id}/open` (api/routes/agent_assignment.py)
    # spawns sets `LIFEOS_TASK_ID`, the hook script already forwards it as
    # `task_id` on every event (scripts/lifeos-agent-hook.sh), so this is
    # the one piece #849 didn't need: turning a task_id-bearing session_start
    # into a lane move. Best-effort — a task lookup/update failure must
    # never break the registration event itself.
    if body.event == "session_start" and body.task_id:
        try:
            from api.services.task_manager import get_task_manager
            manager = get_task_manager()
            task = manager.get(body.task_id)
            if task is not None and task.status == "todo":
                manager.update(body.task_id, status="in_progress")
        except Exception as exc:  # noqa: BLE001 — never fail the registration event
            logger.warning("task status update for %s failed: %s", body.task_id, exc)

    return {
        "registered": True,
        "session_id": cli.session_id,
        "status": cli.status,
    }


def _copy_to_clipboard(text: str, env: dict[str, str]) -> bool:
    """Push `text` to the system clipboard via `wl-copy` (Wayland) or
    `xclip` (X11). Returns True on success, False on any failure. Never
    raises — the resume flow proceeds either way; the frontend gets the
    rendered command in the response as a backup.
    """
    import shutil
    import subprocess

    # Wayland first (the user is on a Wayland session — WAYLAND_DISPLAY is
    # set in the env overlay). xclip works on X11 / Xwayland.
    candidates: list[list[str]] = []
    if shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if shutil.which("xclip"):
        candidates.append(["xclip", "-selection", "clipboard"])
    for argv in candidates:
        try:
            proc = subprocess.run(
                argv,
                input=text.encode("utf-8"),
                env=env,
                timeout=2.0,
                check=False,
                capture_output=True,
            )
            if proc.returncode == 0:
                return True
            logger.debug("clipboard helper %s exited rc=%d: %s",
                         argv[0], proc.returncode, proc.stderr[:200])
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("clipboard helper %s failed: %s", argv[0], exc)
            continue
    return False


def _xdg_runtime_dir(env: dict[str, str] | None = None) -> str:
    """Resolve XDG_RUNTIME_DIR with the standard `/run/user/<uid>` fallback.

    Single source of truth for the convention used by /resume (via
    `_resume_env`), /cc-pane-bind, and /focus when probing for live
    wezterm-gui sockets. systemd's lifeos-api unit env usually lacks
    XDG_RUNTIME_DIR, so the fallback is the load-bearing branch in
    production.
    """
    import os
    src = env if env is not None else os.environ
    value = src.get("XDG_RUNTIME_DIR")
    if value:
        return value
    return f"/run/user/{os.getuid()}"


def _enumerate_live_wezterm_sockets(
    xdg_runtime_dir: str | None,
) -> list[tuple[float, str, int]]:
    """List `(mtime, socket_path, pid)` for every live wezterm-gui process.

    Scans `$XDG_RUNTIME_DIR/wezterm/gui-sock-*`. Each filename ends in the
    wezterm-gui process's pid; we keep only sockets whose pid responds to
    a `kill -0` liveness check (filters out stale sockets left behind by
    crashed wezterm-gui processes).

    Returns an empty list if the runtime dir is missing or unreadable.
    Sort by mtime descending to get the most-recently-active GUI first.
    """
    import os

    if not xdg_runtime_dir:
        return []
    sock_dir = os.path.join(xdg_runtime_dir, "wezterm")
    if not os.path.isdir(sock_dir):
        return []
    try:
        names = os.listdir(sock_dir)
    except OSError:
        return []
    out: list[tuple[float, str, int]] = []
    for name in names:
        if not name.startswith("gui-sock-"):
            continue
        try:
            pid = int(name[len("gui-sock-"):])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)  # signal 0 = liveness check, no actual signal sent
        except (OSError, ProcessLookupError, PermissionError):
            continue
        path = os.path.join(sock_dir, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        out.append((mtime, path, pid))
    return out


def _find_live_wezterm_socket(xdg_runtime_dir: str | None) -> str | None:
    """Return the path to a wezterm-gui socket whose owning PID is alive.

    If multiple GUI instances are running, pick the most-recently-modified
    socket (the user's "current" wezterm typically being the one they just
    interacted with). Returns None if no live socket exists, leaving
    wezterm cli to fall back to its default discovery.
    """
    candidates = _enumerate_live_wezterm_sockets(xdg_runtime_dir)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _live_wezterm_pids(xdg_runtime_dir: str | None) -> set[int]:
    """Return the set of PIDs of all currently-live wezterm-gui processes.

    Used by /focus to validate a cached `session_id → pane_id` mapping: pane
    ids reset on wezterm restart, so if the pid that wrote the mapping is
    no longer in the live set, the cache entry is stale.
    """
    return {pid for _mtime, _path, pid in _enumerate_live_wezterm_sockets(xdg_runtime_dir)}


def _current_wezterm_pid(xdg_runtime_dir: str | None) -> int:
    """Return the PID of the user's "current" wezterm-gui process — the one
    whose socket was modified most recently. Used at upsert time so the
    cached mapping records which wezterm boot it belongs to.

    Returns 0 if no live wezterm is reachable; the upsert path treats 0 as
    "unknown" and the focus path treats it as stale (forces a re-probe).
    """
    candidates = _enumerate_live_wezterm_sockets(xdg_runtime_dir)
    if not candidates:
        return 0
    candidates.sort(reverse=True)
    return candidates[0][2]


def _notify_dock(env: dict[str, str], summary: str, body: str) -> None:
    """Best-effort `notify-send --urgency=critical` so a hidden wezterm
    window pulses the dock icon. Wayland disallows cross-client window
    raise; this is the strongest attention hint we can issue from
    outside the focused client.

    Crucially, we send the notification AS wezterm (via app-name +
    desktop-entry hint pointing at `org.wezfurlong.wezterm`) so GNOME
    Shell associates the urgency with wezterm's dock icon and pulses
    *that* icon — not a generic LifeOS one. The .desktop file lives at
    `/usr/share/applications/org.wezfurlong.wezterm.desktop` (set by the
    wezterm package).

    Failure (no notify-send, libnotify not running, etc.) is silent on
    purpose — the dock flash is an extra; the underlying spawn/focus
    already succeeded by the time this is called.
    """
    import shutil
    import subprocess

    notify_bin = shutil.which("notify-send")
    if not notify_bin:
        return
    try:
        subprocess.run(  # noqa: S603 — fixed argv
            [
                notify_bin,
                "--urgency=critical",
                "--app-name=org.wezfurlong.wezterm",
                "--hint=string:desktop-entry:org.wezfurlong.wezterm",
                "--icon=org.wezfurlong.wezterm",
                summary,
                body,
            ],
            env=env,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _inject_wezterm_pane(env: dict[str, str]) -> None:
    """Set $WEZTERM_PANE in `env` to an existing pane id, so `wezterm cli
    spawn` knows which window to add the new tab to. No-op if WEZTERM_PANE
    is already set, if wezterm isn't running, or if the probe fails — in
    those cases the spawn will surface its own error.

    Pane selection: prefer panes in the `default` workspace (the user's
    primary window), fall back to any pane. We deliberately don't store
    the chosen window — wezterm's mux state is the source of truth, and
    pane ids reset on wezterm-gui restart.
    """
    import json
    import shutil
    import subprocess

    if env.get("WEZTERM_PANE"):
        return
    wezterm_bin = shutil.which("wezterm")
    if not wezterm_bin:
        return
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [wezterm_bin, "cli", "list", "--format", "json"],
            env=env,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    if proc.returncode != 0:
        return
    try:
        panes = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return
    if not isinstance(panes, list) or not panes:
        return

    preferred = next(
        (p for p in panes
         if isinstance(p, dict) and p.get("workspace") == "default" and "pane_id" in p),
        None,
    )
    chosen = preferred or next(
        (p for p in panes if isinstance(p, dict) and "pane_id" in p),
        None,
    )
    if chosen is None:
        return
    env["WEZTERM_PANE"] = str(chosen["pane_id"])


def _resume_env() -> dict[str, str]:
    """Build the environment dict for the resume subprocess.

    Inherits from the FastAPI process env (typical systemd-imported env),
    then layers in key=value lines from LIFEOS_CC_RESUME_ENV_FILE if set.
    The file lets operators pin DISPLAY / XAUTHORITY / WAYLAND_DISPLAY /
    DBUS_SESSION_BUS_ADDRESS explicitly — systemd usually omits them.

    Also derives XDG_RUNTIME_DIR (`/run/user/<uid>`) when missing — it's
    required for wezterm cli to locate the GUI's socket at
    `$XDG_RUNTIME_DIR/wezterm/gui-sock-<pid>`. Without it, `wezterm cli`
    silently auto-starts a headless `wezterm-mux-server` and spawns
    tabs into THAT (invisible to the user). The systemd unit's env
    usually lacks XDG_RUNTIME_DIR even when the env file is set, so
    we always backfill the convention.
    """
    import os
    env: dict[str, str] = dict(os.environ)
    try:
        from config.settings import settings
        path = (settings.cc_resume_env_file or "").strip()
    except Exception:  # noqa: BLE001
        path = ""
    if path:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        except OSError as exc:
            logger.warning("cc_resume_env_file unreadable (%s): %s", path, exc)

    # Backfill XDG_RUNTIME_DIR using the standard `/run/user/<uid>`
    # convention if the env didn't already define it. wezterm cli needs
    # this to find the running GUI's socket.
    if not env.get("XDG_RUNTIME_DIR"):
        uid = os.getuid()
        candidate = f"/run/user/{uid}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate

    # Point wezterm cli directly at the LIVE wezterm-gui socket. Without
    # this, wezterm cli scans `$XDG_RUNTIME_DIR/wezterm/gui-sock-*` and
    # picks a stale socket reference (observed even when only one live
    # socket file exists on disk — wezterm appears to cache historical
    # gui pids from log files). Setting WEZTERM_UNIX_SOCKET bypasses the
    # discovery and forces the connection to the user's actual window.
    if not env.get("WEZTERM_UNIX_SOCKET"):
        socket_path = _find_live_wezterm_socket(env.get("XDG_RUNTIME_DIR"))
        if socket_path:
            env["WEZTERM_UNIX_SOCKET"] = socket_path

    # Prepend $HOME/.local/bin to PATH so the spawned terminal can find
    # user-installed binaries (notably claude itself, which the npm CLI
    # installs there). systemd's lifeos-api unit inherits the bare system
    # PATH (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`)
    # so without this the inner `claude --resume` errors with
    # "No viable candidates found in PATH". The wezterm CLI propagates
    # our env through to the spawned tab, so setting PATH here is
    # sufficient — no template wrapping needed.
    home = env.get("HOME", "")
    if home:
        local_bin = f"{home}/.local/bin"
        current_path = env.get("PATH", "")
        if os.path.isdir(local_bin) and local_bin not in current_path.split(":"):
            env["PATH"] = f"{local_bin}:{current_path}" if current_path else local_bin
    return env


def _check_session_host_or_409(session_id: str) -> str | None:
    """Resolve /focus and /resume's target host for `session_id` (#849, #851).

    Returns `None` when the session ran on THIS API host (or has no
    `cli_sessions` row at all — never registered via the hook, or
    registered before this feature existed — which falls through
    unchanged to the existing local-only resolution: cache / FD-probe).
    Returns the ssh target string when the session's host is a DIFFERENT,
    but registered (`settings.agent_hosts`), machine — the caller then
    runs the launcher over ssh instead of spawning it locally. Raises
    `HTTPException(409)` when the session's host is neither this API host
    nor a registered one.
    """
    try:
        store = _get_session_store()
        cli = store.get_cli_session(session_id)
    except Exception as exc:  # noqa: BLE001 — never block a local resume/focus on a store error
        logger.warning("cli_sessions lookup failed for %s: %s", session_id, exc)
        return None
    if cli is None or cli.host == api_host_name():
        return None
    from config.settings import settings
    target = settings.agent_hosts.get(cli.host)
    if not target:
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is running on host {cli.host!r}, which is "
                    f"not this API host and not in LIFEOS_AGENT_HOSTS",
        )
    return target


async def _resume_codex_session(
    session_id: str,
    body: "CCResumeRequest | None",
    remote_ssh_target: str | None = None,
) -> dict[str, Any]:
    """Codex sibling of resume_claude_code_session.

    Reuses the same env / wezterm pane injection / clipboard / dock
    machinery; only the lookup, settings, and inner command differ.
    `remote_ssh_target` (#851): the ssh target to run the launcher on when
    the session's host isn't this API host, resolved by the caller via
    `_check_session_host_or_409`.
    """
    import shlex
    import subprocess
    import urllib.parse

    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"settings unavailable: {exc}") from exc

    if not getattr(settings, "codex_resume_enabled", False):
        raise HTTPException(status_code=400, detail="codex resume disabled — set LIFEOS_CODEX_RESUME_ENABLED=true")
    template = (settings.codex_resume_cmd or "").strip()
    if not template:
        raise HTTPException(status_code=400, detail="LIFEOS_CODEX_RESUME_CMD is empty")

    from api.services.codex import session_ingest as cx

    try:
        bare = cx.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if remote_ssh_target:
        # (#851) See the identical branch in resume_claude_code_session —
        # a remote session's rollout file isn't under this API's local
        # `codex_sessions_dir`; its cwd comes from the `cli_sessions` row.
        cli = _get_session_store().get_cli_session(session_id)
        if cli is None or not cli.cwd:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found or has no cwd")
        from types import SimpleNamespace
        target = SimpleNamespace(decoded_cwd=cli.cwd)
    else:
        # Widen lookback for resume so older sessions are still resolvable.
        metas = cx.discover_sessions(
            sessions_dir=settings.codex_sessions_dir,
            lookback_days=max(int(settings.codex_lookback_days), 365),
        )
        target = next((m for m in metas if m.raw_session_id == bare), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        # Codex stores cwd inside session_meta, populated by parse_session. The
        # snapshot prepopulates it but on a fresh resume call we may need to
        # re-parse to recover it (cheap — one jsonl read).
        if not target.decoded_cwd:
            target, _ = cx.parse_session(target)
        if not target.decoded_cwd:
            raise HTTPException(status_code=404, detail=f"session {session_id} has no cwd")

    inner_template = (settings.codex_resume_inner_cmd or "").strip()
    inner_rendered = (
        inner_template
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
    )

    rendered = (
        template
        .replace("{session_id_url}", urllib.parse.quote(bare, safe=""))
        .replace("{cwd_url}", urllib.parse.quote(target.decoded_cwd, safe=""))
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
        .replace("{inner_command}", inner_rendered)
    )
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"codex_resume_cmd parse failed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="codex_resume_cmd resolved to an empty argv")

    env = _resume_env()
    if body and body.extra_env:
        env.update(body.extra_env)

    if "wezterm" in argv[0] and not remote_ssh_target:
        _inject_wezterm_pane(env)

    popen_argv = argv
    popen_cwd = target.decoded_cwd
    if remote_ssh_target:
        from api.services.agent_worker.remote_spawn import build_remote_launcher_argv
        popen_argv = build_remote_launcher_argv(argv, target=remote_ssh_target)
        popen_cwd = None

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv only, no shell=True
            popen_argv,
            cwd=popen_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"resume binary not found: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"resume spawn failed: {exc}") from exc

    clipboard_text = ""
    if inner_rendered:
        local_form = (
            f"cd {shlex.quote(target.decoded_cwd)} && {inner_rendered}"
            if target.decoded_cwd else inner_rendered
        )
        clipboard_text = (
            f"ssh {shlex.quote(remote_ssh_target)} -- {shlex.quote(local_form)}"
            if remote_ssh_target else local_form
        )
    clipboard_copied = False
    if clipboard_text:
        clipboard_copied = _copy_to_clipboard(clipboard_text, env)

    pane_id: int | None = None
    stdout_bytes = b""
    stderr_bytes = b""
    # (round 1, finding #7) An ssh round trip routinely exceeds the local
    # 1.5s budget — use the connect-timeout-derived value on the remote
    # branch so a remote launcher doesn't spuriously degrade to
    # `pane_id: None` before the real ssh response even arrives.
    communicate_timeout = 1.5
    if remote_ssh_target:
        communicate_timeout = settings.agent_ssh_connect_timeout + 1.5
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=communicate_timeout)
    except subprocess.TimeoutExpired:
        return {
            "spawned": True,
            "pid": proc.pid,
            "pane_id": None,
            "command": argv,
            "cwd": target.decoded_cwd,
            "inner_command": clipboard_text or inner_rendered,
            "clipboard_copied": clipboard_copied,
        }

    if proc.returncode != 0:
        detail = (stderr_bytes.decode("utf-8", errors="replace").strip()
                  or f"resume process exited rc={proc.returncode}")
        raise HTTPException(status_code=500, detail=detail)

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    if stdout_text:
        first_token = stdout_text.split()[0]
        try:
            pane_id = int(first_token)
        except ValueError:
            pane_id = None

    # (round 1, finding #10) `wezterm_pid` comes from THIS host's own
    # `_current_wezterm_pid` — on a remote resume the pane and its wezterm
    # process live on `remote_ssh_target`, not here, so upserting would
    # record a host-mismatched pid/pane into the LOCAL store.
    if pane_id is not None and not remote_ssh_target:
        try:
            from api.services.cc_wezterm_store import get_default_store
            wezterm_pid = _current_wezterm_pid(env.get("XDG_RUNTIME_DIR"))
            # Reuse the same store — keys are cx:-prefixed, no collision with cc:.
            get_default_store().upsert(
                session_id, pane_id, target.decoded_cwd, wezterm_pid=wezterm_pid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cc_wezterm_store upsert failed for %s: %s", session_id, exc)

    pane_label = f"pane {pane_id}" if pane_id is not None else "new tab"
    _notify_dock(
        env,
        "Codex session resumed",
        f"Wezterm opened {pane_label} in {shlex.quote(target.decoded_cwd)}",
    )

    return {
        "spawned": True,
        "pid": proc.pid,
        "pane_id": pane_id,
        "command": argv,
        "cwd": target.decoded_cwd,
        "inner_command": clipboard_text or inner_rendered,
        "clipboard_copied": clipboard_copied,
    }


@router.post("/sessions/{session_id}/resume")
async def resume_claude_code_session(
    session_id: str,
    body: CCResumeRequest | None = None,
) -> dict[str, Any]:
    """Spawn a local terminal that re-opens a Claude Code or Codex session.

    Valid for `cc:`- and `cx:`-prefixed sessions. Each source is opt-in
    via its own flag (`LIFEOS_CC_RESUME_ENABLED` / `LIFEOS_CODEX_RESUME_ENABLED`).
    Local-network only — do not expose via Tailscale Funnel or the
    public MCP HTTP transport.
    """
    if not session_id.startswith("cx:") and not session_id.startswith("cc:"):
        raise HTTPException(status_code=400, detail="resume is only available for Claude Code or Codex sessions")

    # (#849/#851) A session registered on a different, but REGISTERED
    # (settings.agent_hosts), host resumes over ssh; an unregistered host
    # still 409s rather than silently no-op'ing.
    remote_ssh_target = _check_session_host_or_409(session_id)

    if session_id.startswith("cx:"):
        return await _resume_codex_session(session_id, body, remote_ssh_target=remote_ssh_target)
    return await _resume_claude_code_launcher(session_id, body, remote_ssh_target=remote_ssh_target)


async def _resume_claude_code_launcher(
    session_id: str,
    body: "CCResumeRequest | None",
    remote_ssh_target: str | None = None,
) -> dict[str, Any]:
    """The `cc:`-prefixed half of `resume_claude_code_session`, factored out
    (#851) so `/focus`'s remote fallback (no cross-host pane registry — see
    that function) can reuse it exactly like it already reuses
    `_resume_codex_session` for `cx:` sessions."""
    import shlex
    import subprocess

    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001 — settings should always load
        raise HTTPException(status_code=500, detail=f"settings unavailable: {exc}") from exc

    if not getattr(settings, "cc_resume_enabled", False):
        raise HTTPException(status_code=400, detail="cc resume disabled — set LIFEOS_CC_RESUME_ENABLED=true")
    template = (settings.cc_resume_cmd or "").strip()
    if not template:
        raise HTTPException(status_code=400, detail="LIFEOS_CC_RESUME_CMD is empty")

    from api.services.claude_code import session_ingest as cc

    try:
        bare = cc.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Subagent synthetic ids point at the parent's bare uuid; the operator
    # really wants to resume the parent terminal session, not a sub-slice.
    if ":agent:" in bare:
        bare = bare.split(":agent:", 1)[0]

    if remote_ssh_target:
        # (#851) A remote session's transcript lives on the REMOTE host, not
        # under this API's local `claude_code_projects_dir` — the local
        # `discover_sessions` scan below would always 404 it. Its cwd comes
        # from the `cli_sessions` row instead (populated by the remote
        # host's own hook script over HTTP, host-agnostic by design — #849).
        cli = _get_session_store().get_cli_session(session_id)
        if cli is None or not cli.cwd:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found or has no cwd")
        from types import SimpleNamespace
        target = SimpleNamespace(decoded_cwd=cli.cwd)
    else:
        # Find the matching jsonl to recover the working directory.
        metas = cc.discover_sessions(
            projects_dir=settings.claude_code_projects_dir,
            lookback_days=max(int(settings.claude_code_lookback_days), 365),  # widen lookback for resume
        )
        target = next((m for m in metas if m.raw_session_id == bare), None)
        if target is None or not target.decoded_cwd:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found or has no cwd")

    # Render the inner command first so the outer template can substitute
    # `{inner_command}` (wezterm path) AND so we can copy it to the clipboard
    # as a backup if the spawn doesn't actually run it (legacy launcher path).
    inner_template = (settings.cc_resume_inner_cmd or "").strip()
    inner_rendered = (
        inner_template
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
    )

    # Template substitutions on the launcher:
    #   {session_id}     — bare uuid (no cc: prefix)
    #   {cwd}            — decoded project working directory
    #   {session_id_url} — URL-encoded session_id for use inside `warp://`,
    #                      `vscode://` etc. query strings
    #   {cwd_url}        — URL-encoded cwd for the same
    #   {inner_command}  — the rendered inner_command, inserted unquoted so
    #                      shlex.split sees its individual argv tokens (used
    #                      by the default `wezterm cli spawn ... -- {inner_command}`)
    import urllib.parse
    rendered = (
        template
        .replace("{session_id_url}", urllib.parse.quote(bare, safe=""))
        .replace("{cwd_url}", urllib.parse.quote(target.decoded_cwd, safe=""))
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
        .replace("{inner_command}", inner_rendered)
    )
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"resume_cmd parse failed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="resume_cmd resolved to an empty argv")

    env = _resume_env()
    if body and body.extra_env:
        env.update(body.extra_env)

    # `wezterm cli spawn` (the default launcher) needs to know which window
    # to spawn the new tab into. When called from a systemd context there's
    # no $WEZTERM_PANE and wezterm can't probe focus, so it errors out
    # ("--pane-id was not specified and $WEZTERM_PANE is not set"). Probe
    # `wezterm cli list` for an existing pane and pin its id into the env
    # — wezterm then spawns into that pane's window. Only meaningful for a
    # LOCAL spawn — a remote target's own wezterm window state isn't
    # something this process's env can probe.
    if "wezterm" in argv[0] and not remote_ssh_target:
        _inject_wezterm_pane(env)

    popen_argv = argv
    popen_cwd = target.decoded_cwd
    if remote_ssh_target:
        # (#851) The rendered argv already carries `--cwd <remote path>` (or
        # equivalent) baked in by the template above — that path is on the
        # REMOTE filesystem, so the LOCAL ssh client must not `cwd=` into
        # it (it likely doesn't exist locally at all).
        from api.services.agent_worker.remote_spawn import build_remote_launcher_argv
        popen_argv = build_remote_launcher_argv(argv, target=remote_ssh_target)
        popen_cwd = None

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv only, no shell=True (explicit shlex.split above)
            popen_argv,
            cwd=popen_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"resume binary not found: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"resume spawn failed: {exc}") from exc

    # Push a cwd-portable form of the inner command to the system
    # clipboard. `claude --resume <id>` only finds the session when run
    # in the matching project directory (claude scopes sessions by cwd),
    # so we wrap with `cd <cwd> && ...` — that way pasting the clipboard
    # into any terminal works. The wezterm path doesn't need this (it
    # already runs with `--cwd target.decoded_cwd`), but the clipboard
    # is a backup for operator-overridden non-wezterm launchers AND
    # for the case where the wezterm tab opened off-screen and the
    # operator just wants to run the command somewhere visible. For a
    # remote target (#851), wrap it as a runnable ssh command instead of a
    # bare `cd && ...` — the local clipboard's contents must be paste-able
    # into a LOCAL terminal to be useful.
    clipboard_text = ""
    if inner_rendered:
        local_form = (
            f"cd {shlex.quote(target.decoded_cwd)} && {inner_rendered}"
            if target.decoded_cwd else inner_rendered
        )
        clipboard_text = (
            f"ssh {shlex.quote(remote_ssh_target)} -- {shlex.quote(local_form)}"
            if remote_ssh_target else local_form
        )
    clipboard_copied = False
    if clipboard_text:
        clipboard_copied = _copy_to_clipboard(clipboard_text, env)

    # Two launcher flavors coexist:
    #   (a) `wezterm cli spawn` exits cleanly with a pane id on stdout — we
    #       want to wait for it, parse the id, store it, and return.
    #   (b) The launcher BECOMES the terminal (rare; not the default) —
    #       communicate() times out, no pane id available, return spawned=True.
    # A 1.5s timeout is plenty for (a) and short enough that the API stays
    # snappy for (b) — LOCALLY. (round 1, finding #7) An ssh round trip
    # routinely exceeds 1.5s, so the remote branch uses the connect-
    # timeout-derived value instead, leaving the local value untouched.
    pane_id: int | None = None
    stdout_bytes = b""
    stderr_bytes = b""
    communicate_timeout = 1.5
    if remote_ssh_target:
        communicate_timeout = settings.agent_ssh_connect_timeout + 1.5
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=communicate_timeout)
    except subprocess.TimeoutExpired:
        # Long-running launcher — no pane id this turn, but the spawn is
        # in progress; surface what we have.
        response_body = {
            "spawned": True,
            "pid": proc.pid,
            "pane_id": None,
            "command": argv,
            "cwd": target.decoded_cwd,
            "inner_command": clipboard_text or inner_rendered,
            "clipboard_copied": clipboard_copied,
        }
        return response_body

    if proc.returncode != 0:
        detail = (stderr_bytes.decode("utf-8", errors="replace").strip()
                  or f"resume process exited rc={proc.returncode}")
        raise HTTPException(status_code=500, detail=detail)

    # `wezterm cli spawn` prints a single integer (the pane id) on stdout.
    # If the operator overrode cc_resume_cmd to a launcher that doesn't
    # print one, we just skip the focus mapping for this session.
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    if stdout_text:
        first_token = stdout_text.split()[0]
        try:
            pane_id = int(first_token)
        except ValueError:
            pane_id = None

    # (round 1, finding #10) See the codex sibling above — a remote resume's
    # pane and wezterm process live on `remote_ssh_target`, not here.
    if pane_id is not None and not remote_ssh_target:
        try:
            from api.services.cc_wezterm_store import get_default_store
            wezterm_pid = _current_wezterm_pid(env.get("XDG_RUNTIME_DIR"))
            get_default_store().upsert(
                session_id, pane_id, target.decoded_cwd, wezterm_pid=wezterm_pid,
            )
        except Exception as exc:  # noqa: BLE001 — store failure is non-fatal
            logger.warning("cc_wezterm_store upsert failed for %s: %s", session_id, exc)

    # Pulse the wezterm dock icon so the operator knows a new tab is
    # waiting — Wayland disallows cross-client window raise, so without
    # this hint the spawn would be invisible if wezterm is hidden.
    pane_label = f"pane {pane_id}" if pane_id is not None else "new tab"
    _notify_dock(
        env,
        "Agent session resumed",
        f"Wezterm opened {pane_label} in {shlex.quote(target.decoded_cwd)}",
    )

    return {
        "spawned": True,
        "pid": proc.pid,
        "pane_id": pane_id,
        "command": argv,
        "cwd": target.decoded_cwd,
        "inner_command": clipboard_text or inner_rendered,
        "clipboard_copied": clipboard_copied,
    }


def _lookup_cc_session_meta(session_id: str):
    """Resolve a `cc:`-prefixed session id back to its discovered SessionMeta.

    Used by /focus to locate the transcript jsonl for the FD probe fallback.
    Returns (meta, bare_id) or raises HTTPException with an appropriate status.

    Searches project dirs directly for `<bare>.jsonl` rather than calling
    `discover_sessions`, which would parse every transcript under
    `~/.claude/projects/` on every Go To cache miss. The /focus caller only
    reads `meta.jsonl_path` and `meta.decoded_cwd`, so we synthesize a
    minimal SessionMeta with just those two fields populated.
    """
    import os
    from pathlib import Path

    from api.services.claude_code.session_ingest import (
        CC_PREFIX,
        SessionMeta,
        decode_project_key,
        validate_session_id,
    )
    from config.settings import settings

    try:
        bare = validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ":agent:" in bare:
        bare = bare.split(":agent:", 1)[0]

    root = Path(os.path.expanduser(str(settings.claude_code_projects_dir)))
    if root.exists() and root.is_dir():
        for proj in root.iterdir():
            if not proj.is_dir():
                continue
            candidate = proj / f"{bare}.jsonl"
            if candidate.exists():
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    mtime = 0.0
                project_key = proj.name
                meta = SessionMeta(
                    session_id=CC_PREFIX + bare,
                    raw_session_id=bare,
                    project_key=project_key,
                    decoded_cwd=decode_project_key(project_key),
                    jsonl_path=str(candidate),
                    mtime=mtime,
                )
                return meta, bare

    raise HTTPException(status_code=404, detail=f"session {session_id} not found")


def _lookup_cx_session_meta(session_id: str):
    """Resolve a `cx:`-prefixed session id to a minimal SessionMeta.

    Returns a meta with `jsonl_path` + `decoded_cwd` populated — enough
    for the focus FD probe. Raises HTTPException with an appropriate
    status on failure.
    """
    from api.services.codex import session_ingest as cx
    from config.settings import settings

    try:
        bare = cx.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metas = cx.discover_sessions(
        sessions_dir=settings.codex_sessions_dir,
        lookback_days=max(int(settings.codex_lookback_days), 365),
    )
    target = next((m for m in metas if m.raw_session_id == bare), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    if not target.decoded_cwd:
        target, _ = cx.parse_session(target)
    return target


def _activate_pane(pane_id: int, env: dict[str, str]) -> tuple[bool, str]:
    """Run `wezterm cli activate-pane --pane-id <id>`. Returns (success, detail).

    Raises HTTPException only for unrecoverable errors (wezterm missing,
    timeout). A non-zero rc returns (False, stderr) so the caller can
    decide whether to re-probe.
    """
    import shutil
    import subprocess

    wezterm_bin = shutil.which("wezterm") or "wezterm"
    argv = [wezterm_bin, "cli", "activate-pane", "--pane-id", str(pane_id)]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            argv,
            env=env,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"wezterm not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"wezterm activate-pane timed out: {exc}") from exc

    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr.decode("utf-8", errors="replace").strip()
                   or f"wezterm activate-pane exited rc={proc.returncode}")


@router.post("/cx-pane-bind")
async def cx_pane_bind(request: Request, body: CCPaneBindRequest) -> dict[str, Any]:
    """Localhost-only endpoint called by the Codex SessionStart hook.

    Mirror of /cc-pane-bind for Codex: the script at
    `scripts/codex-session-pane.sh` posts here every time `codex` starts in
    a wezterm pane, so /agents Focus / Go To can hit the pane authoritatively
    without the FD-probe fallback. Storage key is `cx:`-prefixed; no
    collision with cc: rows.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="cx-pane-bind is localhost-only")

    from api.services.codex import session_ingest as cx

    try:
        bare = cx.validate_session_id(body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    storage_id = f"cx:{bare}"
    cwd = body.cwd or ""

    from api.services.cc_wezterm_store import get_default_store
    wezterm_pid = _current_wezterm_pid(_xdg_runtime_dir())
    mapping = get_default_store().upsert(
        storage_id, int(body.pane_id), cwd, wezterm_pid=wezterm_pid,
    )
    return {
        "bound": True,
        "session_id": storage_id,
        "pane_id": mapping.pane_id,
        "cwd": mapping.cwd,
    }


@router.post("/cc-pane-bind")
async def cc_pane_bind(request: Request, body: CCPaneBindRequest) -> dict[str, Any]:
    """Localhost-only endpoint called by the Claude Code SessionStart hook.

    Writes `session_id → pane_id` into `data/cc_wezterm.db` at the moment a
    `claude` invocation starts inside a wezterm pane, so the /agents
    Focus / Go To button can later target the pane authoritatively without
    the FD-probe fallback. The session_id from the hook payload is the
    bare uuid; we prefix with `cc:` to match the store's keying convention.

    Bound to 127.0.0.1 only — never expose this endpoint publicly. Anyone
    who can reach it could rewrite the pane mapping for any session.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="cc-pane-bind is localhost-only")

    from api.services.claude_code import session_ingest as cc

    try:
        bare = cc.validate_session_id(body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Storage convention: keys are always `cc:`-prefixed (matches /resume's
    # upsert). `validate_session_id` strips the prefix from the request, so
    # we re-prefix unconditionally.
    storage_id = f"cc:{bare}"
    cwd = body.cwd or ""

    from api.services.cc_wezterm_store import get_default_store
    # Capture the live wezterm-gui pid so /focus can invalidate the mapping
    # across wezterm restarts. The hook runs inside a wezterm pane so a
    # live gui is expected; if none is discoverable (e.g. XDG_RUNTIME_DIR
    # missing from the systemd env), wezterm_pid=0 makes the mapping
    # invalidate on first focus, which falls through to the probe — still
    # correct, just doesn't save the round trip.
    wezterm_pid = _current_wezterm_pid(_xdg_runtime_dir())
    mapping = get_default_store().upsert(
        storage_id, int(body.pane_id), cwd, wezterm_pid=wezterm_pid,
    )
    return {
        "bound": True,
        "session_id": storage_id,
        "pane_id": mapping.pane_id,
        "cwd": mapping.cwd,
    }


@router.post("/sessions/{session_id}/focus")
async def focus_claude_code_session(session_id: str) -> dict[str, Any]:
    """Activate the WezTerm pane running this Claude Code session.

    Resolution order:
      1. Cached mapping in `data/cc_wezterm.db` (written by /resume or by
         the SessionStart hook → /cc-pane-bind).
      2. FD-probe fallback (`cc_pane_locate`): walk the session's
         transcript file's holders to a wezterm pane via TTY matching.
         On hit, cache the mapping so subsequent calls are O(1).
      3. If activate-pane fails on a cached mapping (typical: user closed
         the tab), the mapping is cleared and the probe runs once more —
         the session may have been resumed in a fresh pane.

    Returns 404 only when no mapping exists AND probe finds nothing
    (session not running, non-wezterm terminal). Returns 410 when a pane
    existed but is now gone and no replacement can be found.

    Caveat: on GNOME Wayland the compositor disallows cross-client window
    raise, so this reliably switches the tab within wezterm but cannot
    bring a hidden wezterm window to the foreground. A `notify-send`
    follows the activate-pane call to flash the dock icon as a hint.
    """
    import shlex

    is_cx = session_id.startswith("cx:")
    if not session_id.startswith("cc:") and not is_cx:
        raise HTTPException(status_code=400, detail="focus is only available for Claude Code or Codex sessions")

    # (#849/#851) A session registered on a different, but REGISTERED host
    # resumes over ssh; an unregistered host still 409s.
    remote_ssh_target = _check_session_host_or_409(session_id)

    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"settings unavailable: {exc}") from exc

    # Each source has its own resume-enabled gate; focus reuses the same
    # wezterm machinery so we require the matching gate to be on.
    if is_cx:
        if not getattr(settings, "codex_resume_enabled", False):
            raise HTTPException(status_code=400, detail="codex resume disabled — set LIFEOS_CODEX_RESUME_ENABLED=true")
    else:
        if not getattr(settings, "cc_resume_enabled", False):
            raise HTTPException(status_code=400, detail="cc resume disabled — set LIFEOS_CC_RESUME_ENABLED=true")

    if remote_ssh_target:
        # (#851) There is no cross-host pane registry: the local
        # `cc_wezterm_store` mapping is only ever written for a session
        # that ran ON this API host (`cli_session_event`'s upsert guard),
        # and the FD-probe fallback below reads a local transcript file a
        # remote session's never has. Rather than invent a remote pane
        # registry, "focus" a remote session by running the same launcher
        # `/resume` does (over ssh) — same operator outcome (a terminal
        # showing the session), degraded from "reuse the existing pane"
        # to "open a new one", which the local path only reaches anyway
        # once its own cache/probe both miss.
        if is_cx:
            result = await _resume_codex_session(session_id, None, remote_ssh_target=remote_ssh_target)
        else:
            result = await _resume_claude_code_launcher(session_id, None, remote_ssh_target=remote_ssh_target)
        return {
            "focused": True,
            "pane_id": result.get("pane_id"),
            "cwd": result.get("cwd"),
        }

    from api.services.cc_wezterm_store import get_default_store
    from api.services import cc_pane_locate

    store = get_default_store()
    env = _resume_env()

    # Resolve session metadata lazily — only needed if cache misses or the
    # cached pane is stale and we need to re-probe.
    meta = None
    def _meta():
        nonlocal meta
        if meta is None:
            if is_cx:
                meta = _lookup_cx_session_meta(session_id)
            else:
                meta, _ = _lookup_cc_session_meta(session_id)
        return meta

    def _probe_and_store() -> int | None:
        try:
            m = _meta()
        except HTTPException:
            return None
        if not m.jsonl_path:
            return None
        pane_id = cc_pane_locate.locate_pane_for_transcript(m.jsonl_path, env=env)
        if pane_id is None:
            return None
        wezterm_pid = _current_wezterm_pid(env.get("XDG_RUNTIME_DIR"))
        store.upsert(session_id, pane_id, m.decoded_cwd or "", wezterm_pid=wezterm_pid)
        return pane_id

    mapping = store.get(session_id)
    probed_this_request = False

    # Invalidate the cache across wezterm restarts. Pane ids reset when
    # wezterm-gui restarts, so a cached pane_id from a dead wezterm could
    # silently activate an unrelated session's pane in the new wezterm.
    # `wezterm_pid=0` covers pre-#257 rows that have no boot id recorded.
    if mapping is not None:
        live_pids = _live_wezterm_pids(env.get("XDG_RUNTIME_DIR"))
        if mapping.wezterm_pid == 0 or mapping.wezterm_pid not in live_pids:
            store.delete(session_id)
            mapping = None

    if mapping is None:
        # No cache → probe before giving up.
        probed = _probe_and_store()
        probed_this_request = True
        if probed is None:
            raise HTTPException(
                status_code=404,
                detail="couldn't locate pane — session not running, "
                       "wezterm unreachable, or SessionStart hook not installed",
            )
        mapping = store.get(session_id)
        assert mapping is not None  # _probe_and_store just wrote it

    ok, detail = _activate_pane(mapping.pane_id, env)
    if not ok:
        # Stale mapping — pane was closed, or wezterm restarted and pane
        # ids reset. Clear the mapping; if the cache came from a prior
        # request, re-probe once before giving up.
        store.delete(session_id)
        if probed_this_request:
            raise HTTPException(status_code=410, detail=detail)
        probed = _probe_and_store()
        if probed is None:
            raise HTTPException(status_code=410, detail=detail)
        mapping = store.get(session_id)
        assert mapping is not None
        ok, detail = _activate_pane(mapping.pane_id, env)
        if not ok:
            store.delete(session_id)
            raise HTTPException(status_code=410, detail=detail)

    _notify_dock(
        env,
        "Agent session focused",
        f"Switched wezterm to pane {mapping.pane_id} ({shlex.quote(mapping.cwd)})",
    )

    return {
        "focused": True,
        "pane_id": mapping.pane_id,
        "cwd": mapping.cwd,
    }


async def _stream_claude_code_session(session_id: str, backfill: int):
    """Per-session SSE generator for Claude Code (cc:-prefixed) sessions."""
    from config.settings import settings
    from api.services.claude_code import session_ingest as cc

    yield ": ok\n\n"
    try:
        events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
    except Exception as exc:  # noqa: BLE001
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        return
    tail = events[-backfill:] if backfill and len(events) > backfill else events
    for ev in tail:
        yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"

    line_count = len(events)
    # Track new-events time and heartbeat time separately so the
    # idle-close check actually fires (heartbeats don't postpone close).
    now0 = time.time()
    last_new_event_at = now0
    last_heartbeat_at = now0
    while True:
        await asyncio.sleep(1.0)
        try:
            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except Exception:
            events = []
        if len(events) > line_count:
            for ev in events[line_count:]:
                yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"
            line_count = len(events)
            last_new_event_at = time.time()
            last_heartbeat_at = last_new_event_at
        elif time.time() - last_heartbeat_at >= 15.0:
            yield ": heartbeat\n\n"
            last_heartbeat_at = time.time()
        # Claude Code has no DB status — close after 5 minutes of no new
        # transcript events so we don't hold connections forever. Heartbeats
        # do NOT postpone this — only real new events do.
        if time.time() - last_new_event_at > 300.0:
            yield (
                "event: closed\n"
                f"data: {json.dumps({'session_id': session_id, 'status': 'idle'})}\n\n"
            )
            return


async def _stream_codex_session(session_id: str, backfill: int):
    """Per-session SSE generator for Codex (cx:-prefixed) sessions.

    Mirrors `_stream_claude_code_session` — Codex has no DB status either, so
    it uses the same idle-close-after-5-minutes heuristic. (#850: previously
    `/sessions/{id}/stream` only dispatched `cc:` here, so opening a Codex
    session's panel fell through to the LifeOS transcript store and 400'd.)
    """
    from config.settings import settings
    from api.services.codex import session_ingest as cx

    yield ": ok\n\n"
    try:
        events = cx.read_normalized_events(session_id, settings.codex_sessions_dir)
    except Exception as exc:  # noqa: BLE001
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        return
    tail = events[-backfill:] if backfill and len(events) > backfill else events
    for ev in tail:
        yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"

    line_count = len(events)
    now0 = time.time()
    last_new_event_at = now0
    last_heartbeat_at = now0
    while True:
        await asyncio.sleep(1.0)
        try:
            events = cx.read_normalized_events(session_id, settings.codex_sessions_dir)
        except Exception:
            events = []
        if len(events) > line_count:
            for ev in events[line_count:]:
                yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"
            line_count = len(events)
            last_new_event_at = time.time()
            last_heartbeat_at = last_new_event_at
        elif time.time() - last_heartbeat_at >= 15.0:
            yield ": heartbeat\n\n"
            last_heartbeat_at = time.time()
        if time.time() - last_new_event_at > 300.0:
            yield (
                "event: closed\n"
                f"data: {json.dumps({'session_id': session_id, 'status': 'idle'})}\n\n"
            )
            return


@router.get("/sessions/{session_id}/stream")
async def stream_session_transcript(
    session_id: str,
    backfill: int = Query(50, ge=0, le=500),
) -> StreamingResponse:
    """Per-session SSE: backfill last N events, then live-tail the JSONL file.

    Closes cleanly when the session reaches a terminal status.
    Dispatches by `cc:` prefix to the Claude Code ingest path, `cx:` to the
    Codex ingest path (#850).
    """
    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from api.services.claude_code.session_ingest import validate_session_id
            validate_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            _stream_claude_code_session(session_id, backfill),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    if session_id.startswith("cx:"):
        if not _codex_enabled():
            raise HTTPException(status_code=404, detail="codex viz disabled")
        try:
            from api.services.codex.session_ingest import validate_session_id as cx_validate
            cx_validate(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            _stream_codex_session(session_id, backfill),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    transcript_store = _get_transcript_store()
    session_store = _get_session_store()

    # Validate session_id up-front via the store's path protection — fail fast
    # before opening the streaming response.
    try:
        transcript_store._path(session_id)  # raises ValueError on traversal
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def generate():
        yield ": ok\n\n"
        try:
            events = transcript_store.read(session_id)
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            return
        tail = events[-backfill:] if backfill and len(events) > backfill else events
        for ev in tail:
            yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"

        line_count = len(events)
        last_heartbeat = time.time()
        terminal_check_at = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                events = transcript_store.read(session_id)
            except Exception:
                events = []
            if len(events) > line_count:
                for ev in events[line_count:]:
                    yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"
                line_count = len(events)
                last_heartbeat = time.time()
            elif time.time() - last_heartbeat >= 15.0:
                yield ": heartbeat\n\n"
                last_heartbeat = time.time()

            # Check terminal status at most every ~5s to avoid hammering the DB.
            now = time.time()
            if now - terminal_check_at >= 5.0:
                terminal_check_at = now
                try:
                    s = session_store.get_by_session_id(session_id)
                    if s and s.status in TERMINAL_STATUSES:
                        yield (
                            "event: closed\n"
                            f"data: {json.dumps({'session_id': session_id, 'status': s.status})}\n\n"
                        )
                        return
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
