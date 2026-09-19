"""Background refresher for the pull-request merge status shown on Review
cards.

`GET /board` and the board stream never call `gh` directly — a card's PR
badge and drawer status line are joined from `session_store.get_pr_status()`,
a small cache table this loop keeps current. That keeps a board read fast
and independent of the git host's availability or latency: a card whose PR
merged an hour ago reflects that on the next board build, but the build
itself never waits on a network call.

Disabled in tests the same way as every other /agents background job —
`LIFEOS_TEST_INSTANCE=1` skips starting it at all (see api/main.py's
lifespan).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Tick cadence — how often the loop looks for stale PR urls to refresh.
_TICK_SECONDS = 30.0
# How long a cached status is trusted before it's worth re-checking.
_CACHE_TTL_S = 300
# Bounds `gh` subprocess calls per tick — a card with many PRs (or many
# cards) still only costs a handful of processes per tick, not a burst.
_MAX_REFRESH_PER_TICK = 3
# Per-call timeout — a hung or unreachable `gh` costs at most this long,
# once, and never blocks a board read either way.
_GH_TIMEOUT_S = 15

_task: "asyncio.Task[None] | None" = None


def _gh_pr_view(url: str, *, timeout_s: float = _GH_TIMEOUT_S) -> dict[str, Any] | None:
    """One bounded `gh pr view` call, normalized to
    ``{number, title, state, merged_at}``, or None on any failure. Never
    raises — a hung/unreachable/erroring `gh` reads as "couldn't refresh
    this tick", not an exception that would kill the loop.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", url, "--json", "number,title,state,mergedAt"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("gh pr view %s failed: %s", url, exc)
        return None
    if proc.returncode != 0:
        logger.debug(
            "gh pr view %s exited %s: %s",
            url, proc.returncode, (proc.stderr or "").strip()[:200],
        )
        return None
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "merged_at": data.get("mergedAt"),
    }


def refresh_stale(
    session_store,
    *,
    ttl_s: int = _CACHE_TTL_S,
    max_refresh: int = _MAX_REFRESH_PER_TICK,
    timeout_s: float = _GH_TIMEOUT_S,
    viewer: "Callable[[str], dict[str, Any] | None] | None" = None,
) -> int:
    """Refresh up to `max_refresh` PR urls whose cache entry is missing or
    older than `ttl_s`, recording either the fresh status or (on failure) a
    `stale` mark that keeps whatever was last known. `viewer` defaults to a
    real bounded `gh pr view` call; a test double lets the freshness
    behavior — including a PR that starts open and is later reported
    merged, and a lookup that fails — be exercised without a real `gh`
    binary or network. Returns how many urls were attempted.
    """
    view = viewer or (lambda url: _gh_pr_view(url, timeout_s=timeout_s))
    stale_urls = session_store.list_stale_pr_urls(ttl_s=ttl_s)[:max_refresh]
    for url in stale_urls:
        session_store.upsert_pr_status(url, view(url))
    return len(stale_urls)


async def _refresh_loop() -> None:
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore()
    while True:
        try:
            refreshed = await asyncio.to_thread(refresh_stale, store)
            if refreshed:
                logger.info("pr_status prefetch: refreshed %d PR(s)", refreshed)
        except asyncio.CancelledError:
            logger.info("pr_status prefetch loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die quietly
            logger.exception("pr_status prefetch loop tick failed")
        await asyncio.sleep(_TICK_SECONDS)


def start() -> None:
    """Launch the background loop. Idempotent — calling twice is a no-op."""
    global _task
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_refresh_loop(), name="pr_status_prefetch")
    logger.info(
        "pr_status prefetch loop started (tick=%ds, ttl=%ds)",
        int(_TICK_SECONDS), _CACHE_TTL_S,
    )


def stop() -> None:
    """Cancel the loop on shutdown."""
    global _task
    if _task is None:
        return
    _task.cancel()
    _task = None
