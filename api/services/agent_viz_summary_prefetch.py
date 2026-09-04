"""Background worker that pre-computes /agents session summaries.

The lazy click-on-demand model (api/services/agent_viz_summary.py) means a
fresh first view spends ~25-30s in Gemma. This worker walks the snapshot
between user actions, summarizes any session that doesn't have a cached
short_label yet, and yields when the agent worker is actively using Gemma —
so a node label is usually ready by the time the operator looks.

Disabled by default in tests; enabled via LIFEOS_AGENT_VIZ_PREFETCH_ENABLED.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from api.services import agent_viz_summary

logger = logging.getLogger(__name__)

# Tick cadence — how often to look for a session to summarize. One summary
# per tick keeps the queue depth shallow and avoids hogging Gemma.
_TICK_SECONDS = 20.0

# When the agent worker is hot, skip our turn entirely so the worker's
# real-time Gemma calls aren't queued behind a cosmetic prefetch.
_BUSY_SKIP_TICKS = 1

# A session that just failed to summarize gets this many ticks of cooldown
# before we try it again — bounds retry loops on permanently-broken inputs.
_FAILURE_BACKOFF_TICKS = 30

_task: asyncio.Task[None] | None = None


def _agent_worker_busy() -> bool:
    """Return True if a LifeOS agent worker session is actively running.

    We avoid competing with the worker for Gemma cycles — a real agent
    Gemma call already takes seconds; piling a 30s summary on top is the
    fastest way to make the worker feel sluggish. Best-effort: any failure
    in the lookup is treated as "not busy" so a broken store doesn't
    permanently stall the prefetcher.
    """
    try:
        from api.services.agent_worker.session_store import SessionStore, STATUS_RUNNING
        store = SessionStore()
        for s in store.list_sessions(limit=50):
            if s.status == STATUS_RUNNING and (s.routing or "local") == "local":
                return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent worker busy check failed: %s", exc)
    return False


def _candidate_sessions() -> list[dict[str, Any]]:
    """Sessions worth prefetching: no cached short_label, ordered by
    "summary will be most valuable" — terminal sessions first (their
    summary is permanently valid), then live ones (in case the operator
    is about to look)."""
    try:
        from api.routes.agents import _build_snapshot
        snap = _build_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prefetch snapshot build failed: %s", exc)
        return []
    from api.services.agent_worker.session_store import TERMINAL_STATUSES

    pending: list[dict[str, Any]] = []
    for s in snap.get("sessions", []):
        if s.get("short_label"):
            continue
        # is_subagent CC nodes synthesize from a parent transcript — skip.
        if s.get("is_subagent"):
            continue
        pending.append(s)
    pending.sort(key=lambda s: (
        0 if s.get("status") in TERMINAL_STATUSES else 1,  # terminal first
        -(s.get("last_activity_at") or 0),                  # recent within bucket
    ))
    return pending


async def _summarize_one(session: dict[str, Any]) -> bool:
    """Summarize a single session. Returns True on success, False on failure."""
    sid = session["session_id"]
    last_activity = float(session.get("last_activity_at") or 0.0)
    label = session.get("label") or sid

    # Pull events the same way the /summary route does — mirror its
    # three-way dispatch (cc: / cx: / everything else) so Codex sessions
    # get summarized too (#863 — Codex ids used to fall through to the
    # LifeOS TranscriptStore, which returns [] for them).
    try:
        if sid.startswith("cc:"):
            from api.routes.agents import _claude_code_enabled
            if not _claude_code_enabled():
                return False
            from config.settings import settings
            from api.services.claude_code import session_ingest as cc
            events = cc.read_normalized_events(sid, settings.claude_code_projects_dir)
        elif sid.startswith("cx:"):
            from api.routes.agents import _codex_enabled
            if not _codex_enabled():
                return False
            from config.settings import settings
            from api.services.codex import session_ingest as cx
            events = cx.read_normalized_events(sid, settings.codex_sessions_dir)
        else:
            from api.services.agent_worker.transcript_store import TranscriptStore
            events = TranscriptStore().read(sid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("prefetch event load failed for %s: %s", sid, exc)
        return False

    try:
        await agent_viz_summary.summarize_session(
            sid,
            label=label,
            last_activity_at=last_activity,
            events=events,
            status=str(session.get("status") or ""),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("prefetch summarize failed for %s: %s", sid, exc)
        return False


async def _prefetch_loop() -> None:
    """Main loop. One summary per tick; backs off when worker is busy or
    when a candidate just failed."""
    # Per-session cooldown: session_id → tick index when we may retry.
    cooldown: dict[str, int] = {}
    tick = 0
    consecutive_busy = 0

    while True:
        tick += 1
        try:
            if _agent_worker_busy():
                consecutive_busy += 1
                # Log a heartbeat occasionally so the operator can see why
                # the queue isn't draining when the worker is running hot.
                if consecutive_busy % 10 == 1:
                    logger.info(
                        "agent_viz prefetch yielding to agent worker "
                        "(%d ticks)", consecutive_busy,
                    )
                await asyncio.sleep(_TICK_SECONDS * _BUSY_SKIP_TICKS)
                continue
            consecutive_busy = 0

            for session in _candidate_sessions():
                sid = session["session_id"]
                cd = cooldown.get(sid, 0)
                if cd > tick:
                    continue
                started = time.time()
                ok = await _summarize_one(session)
                elapsed = time.time() - started
                if ok:
                    logger.info(
                        "agent_viz prefetch: summarized %s in %.1fs",
                        sid, elapsed,
                    )
                # Park every attempted session, success or failure. A real
                # summary drops out of the candidate list on its own (it now
                # has a cached short_label), so the cooldown only matters for a
                # session that "succeeds" without producing a cacheable label —
                # e.g. a live transcript with only tool calls and no
                # user/assistant text. Without a cooldown such a session sorts
                # first every tick, gets re-picked in ~0s, and starves every
                # other candidate behind it.
                cooldown[sid] = tick + _FAILURE_BACKOFF_TICKS
                # Only do one per tick — keeps Gemma queue shallow.
                break
        except asyncio.CancelledError:
            logger.info("agent_viz prefetch loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die quietly
            logger.exception("agent_viz prefetch loop tick failed")
        await asyncio.sleep(_TICK_SECONDS)


def start() -> None:
    """Launch the background loop. Idempotent — calling twice is a no-op."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        from config.settings import settings
        enabled = bool(getattr(settings, "agent_viz_prefetch_enabled", True))
    except Exception:  # noqa: BLE001
        enabled = True
    if not enabled:
        logger.info("agent_viz prefetch disabled via settings")
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_prefetch_loop(), name="agent_viz_prefetch")
    logger.info("agent_viz prefetch loop started (tick=%ds)", int(_TICK_SECONDS))


def stop() -> None:
    """Cancel the loop on shutdown."""
    global _task
    if _task is None:
        return
    _task.cancel()
    _task = None
