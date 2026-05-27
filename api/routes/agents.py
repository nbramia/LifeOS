"""Read-only agent activity visualization endpoints.

Powers the `/agents` UI: a live graph of in-flight and recently-completed
agent worker sessions, plus per-session transcript tailing. See
`docs/specs/technical/agent-worker.md` and issue #133.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.services.agent_worker.session_store import (
    TERMINAL_STATUSES,
    Session,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


# Module-level lazy singletons. Tests monkeypatch these directly so they can
# point the endpoints at temp-dir-backed stores without hitting the real data
# directory.
_session_store: SessionStore | None = None
_transcript_store: TranscriptStore | None = None

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
        "total_dollars": round(s.total_dollars, 6),
        "total_active_seconds": round(s.total_active_seconds, 3),
        "expected_output": s.expected_output,
        "label": _label_for_session(s, events),
        "last_event_kind": summary["last_event_kind"],
        "tool_call_count": summary["tool_call_count"],
        "error_count": summary["error_count"],
    }


def _build_snapshot() -> dict[str, Any]:
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    sessions = session_store.list_sessions(limit=_SNAPSHOT_LIMIT)
    session_dicts = [_session_to_dict(s, transcript_store) for s in sessions]
    edges = [
        {"from": s.parent_session_id, "to": s.session_id, "type": "spawn"}
        for s in sessions
        if s.parent_session_id
    ]
    return {
        "sessions": session_dicts,
        "edges": edges,
        "generated_at": int(time.time()),
    }


@router.get("/snapshot")
async def get_snapshot() -> dict[str, Any]:
    """Full current snapshot of agent sessions + spawn edges."""
    return _build_snapshot()


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Recent transcript events for one session (paginated tail)."""
    transcript_store = _get_transcript_store()
    try:
        events = transcript_store.read(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tail = events[-limit:] if len(events) > limit else events
    return {"session_id": session_id, "events": tail, "total": len(events)}


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


@router.get("/sessions/{session_id}/stream")
async def stream_session_transcript(
    session_id: str,
    backfill: int = Query(50, ge=0, le=500),
) -> StreamingResponse:
    """Per-session SSE: backfill last N events, then live-tail the JSONL file.

    Closes cleanly when the session reaches a terminal status.
    """
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
