"""
LifeOS - Personal RAG System for Obsidian Vault
FastAPI Application Entry Point

WARNING: Do not run this file directly with uvicorn!
=========================================================
Always use the server management script:

    ./scripts/server.sh start    # Start server
    ./scripts/server.sh restart  # Restart after code changes
    ./scripts/server.sh stop     # Stop server

Running uvicorn directly can create ghost processes that bind to different
interfaces, causing localhost and Tailscale/network access to hit different
server instances with different code versions.

See CLAUDE.md for full instructions for AI coding agents.
"""
# Load environment variables from .env file first, before any imports.
#
# The path is explicit (repo root, derived from this file's own location) —
# NOT a bare `load_dotenv()`. python-dotenv's default search (`usecwd=False`)
# walks upward from *this file's own directory* — not the process cwd —
# until it finds a `.env`, climbing all the way to the filesystem root if
# necessary. That's fine when this file lives in the real checkout (the
# first candidate found one level up IS the real checkout's own `.env`),
# but it's exactly the wrong behavior anywhere else this module gets
# imported from with no `.env` of its own (a git worktree, notably): the
# search keeps climbing past that directory and can load an unrelated,
# real `.env` from a parent — including another checkout's machine-specific
# config (see #598). Anchoring to `Path(__file__).parent.parent / ".env"`
# loads the exact same file as today for the real checkout (that first
# candidate IS the repo root there), so server behavior is unchanged, while
# a nested import (worktree, tests) only loads a `.env` that actually lives
# in that same checkout — never a parent's.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import hashlib
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import formatdate

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from starlette.middleware.gzip import GZipMiddleware

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from api.routes import search, ask, calendar, gmail, drive, people, chat, briefings, admin, conversations, memories, imessage, crm, slack, photos, reminders, scheduler, tasks, monarch, investments, jobs, perf, agents, vault, fitness, voice, agent_proxy, hermes_proxy, journal, journal_trends, journal_ingest
from api.services.log_redaction import configure_telegram_log_redaction, install_query_string_redaction_filter
from api.services.route_timing import RouteTimingMiddleware
from config.settings import settings

# Configure root logging here, explicitly, rather than leaving it to whatever
# module happens to call `logging.basicConfig()` first. `scripts/merge_people`
# (imported lazily on startup below) does exactly that with this same
# level/format, so this is a no-op for existing log output — but doing it up
# front, and pairing it with `configure_telegram_log_redaction()`, is what
# keeps httpx's request logger (which logs full URLs, and the Telegram Bot
# API embeds the bot token in the URL) from ever logging at INFO here (#519).
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
configure_telegram_log_redaction()
# #904: uvicorn's own access logger writes every request's raw query string
# to logs/server.log regardless of what a route handler logs -- this is the
# only process that runs an HTTP server, so it's the only place this needs
# installing (see install_query_string_redaction_filter()'s docstring for
# why this must run at import time, not inside a route or startup event).
install_query_string_redaction_filter()

logger = logging.getLogger(__name__)

# Background services (initialized on startup)
_calendar_indexer = None
_telegram_listeners = []
_reminder_scheduler = None
_scheduler_watcher = None
_job_queue = None
_task_watcher = None

# Health monitoring (previously _health_check_loop) is now an out-of-band
# watcher in nbramia/local-processing that polls /health/raw-state. Moving it
# out-of-process means a LifeOS outage produces an alert instead of silencing
# the alerts themselves.


def check_server_host_guard() -> None:
    """Refuse to start unless this machine is the designated LifeOS host (#506).

    The LifeOS API is architecturally supposed to run on exactly one machine
    — every other machine is a client or export agent. A second live server
    writes to its own SQLite/Chroma copy that silently diverges from the
    real one, and clients pointed at the wrong host get stale answers with
    no indication anything is wrong.

    ``LIFEOS_SERVER_HOSTNAME`` unset (the default) disables this guard
    entirely — a fresh open-source clone must never be blocked from running
    its own server. Only set it once you've deliberately designated a host.
    """
    expected = (settings.server_hostname or "").strip()
    if not expected:
        logger.info(
            "LIFEOS_SERVER_HOSTNAME not set — host guard disabled "
            "(this or any machine may run the LifeOS API server)."
        )
        return
    actual = socket.gethostname()
    if actual == expected:
        return
    raise RuntimeError(
        f"Refusing to start: LIFEOS_SERVER_HOSTNAME designates {expected!r} as "
        f"the only machine allowed to run the LifeOS API server, but this "
        f"machine's hostname is {actual!r}. Running a second server here would "
        f"write to its own SQLite/Chroma copy that silently diverges from the "
        f"real one. Point this machine's clients at the designated host via "
        f"LIFEOS_API_URL instead of running the server locally."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    global _calendar_indexer, _telegram_listeners, _reminder_scheduler, _scheduler_watcher, _job_queue, _task_watcher

    # Startup: refuse to run a second server on a non-designated machine (#506).
    # Deliberately not wrapped in try/except — unlike the best-effort blocks
    # below, this must actually stop startup on a mismatch.
    check_server_host_guard()

    # Startup: Recover any incomplete merge operations
    try:
        from scripts.merge_people import recover_incomplete_merge
        if recover_incomplete_merge():
            logger.warning("Recovered incomplete merge operation on startup")
    except Exception as e:
        logger.error(f"Failed to check for incomplete merges: {e}")

    # Startup: Initialize and start Calendar indexer at specific times (Eastern)
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        _calendar_indexer = get_calendar_indexer()
        # Sync at 8 AM, noon, and 3 PM Eastern
        _calendar_indexer.start_time_scheduler(
            schedule_times=[(8, 0), (12, 0), (15, 0)],
            timezone=settings.timezone
        )
        logger.info("Calendar indexer scheduler started (8:00, 12:00, 15:00 Eastern)")
    except Exception as e:
        logger.error(f"Failed to start Calendar indexer: {e}")

    # Health monitoring (previously an in-process 2:30/7:00 scheduler) now runs
    # out-of-band in nbramia/local-processing via the lifeos_health watcher.

    # Startup: Start Telegram bot listeners (primary + any specialized bots)
    try:
        from api.services.telegram import get_telegram_listeners
        _telegram_listeners = get_telegram_listeners()
        for _listener in _telegram_listeners:
            _listener.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram bot listeners: {e}")

    # Startup: Start the scheduler (cron/one-off triggers → actions) and watch
    # LifeOS/Scheduler/ for external edits (e.g. via Obsidian). Markdown is the
    # source of truth, so rebuild the index from the vault before firing.
    try:
        from api.services.scheduler_store import get_scheduler, get_scheduler_store
        from api.services.scheduler_watcher import SchedulerWatcher
        store = get_scheduler_store()
        store.rebuild_index()
        _reminder_scheduler = get_scheduler()
        _reminder_scheduler.start()
        _scheduler_watcher = SchedulerWatcher(scheduler_dir=store.scheduler_dir)
        _scheduler_watcher.start()
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

    # Startup: Start job queue worker
    try:
        from api.services.job_queue import get_job_queue
        _job_queue = get_job_queue()
        _job_queue.start_worker()
        logger.info("Job queue worker started")
    except Exception as e:
        logger.error(f"Failed to start job queue worker: {e}")

    # Startup: Watch LifeOS/Tasks/ for external edits (e.g. via Obsidian) so
    # the task index and auto-generated Dashboard stay in sync.
    try:
        from api.services.task_manager import get_task_manager
        from api.services.task_watcher import TaskWatcher
        tm = get_task_manager()
        # Pull tasks from disk once at startup in case files changed while we were down
        tm.rebuild_index()
        _task_watcher = TaskWatcher(tasks_dir=tm.tasks_dir)
        _task_watcher.start()
    except Exception as e:
        logger.error(f"Failed to start task file watcher: {e}")

    # Startup: Background prefetch for /agents session summaries — so the
    # graph already shows real short labels by the time the operator looks.
    try:
        from api.services import agent_viz_summary_prefetch
        agent_viz_summary_prefetch.start()
    except Exception as e:
        logger.error(f"Failed to start agent_viz prefetch loop: {e}")

    # Hint for new users who haven't set their person ID yet
    if not settings.my_person_id and settings.user_name and settings.user_name != "User":
        logger.info(
            "LIFEOS_MY_PERSON_ID not set. After your first sync, find your ID with:\n"
            f'  curl "http://localhost:8000/api/crm/people?q={settings.user_name}" | jq \'.people[0].id\'\n'
            "Then add to .env: LIFEOS_MY_PERSON_ID=<your-id>"
        )

    yield  # Application runs here

    # Shutdown: drain in-flight chat turns (#611). A turn's task now runs
    # independently of its SSE reader, so a shutdown here (e.g. a mid-turn
    # auto-redeploy, #437) would otherwise just kill it via task
    # cancellation at process exit with no chance to persist anything —
    # this cancels every turn explicitly and awaits its own partial-persist
    # handling first, so a redeploy stores an honest partial instead of
    # silently losing the turn. Runs before the other shutdown blocks below
    # since those don't depend on it and a turn task may itself touch
    # services (the conversation/usage stores) that should still be up.
    try:
        from api.services.chat_turns import get_turn_registry
        await get_turn_registry().shutdown()
    except Exception as e:
        logger.error(f"Failed to drain in-flight chat turns: {e}")

    # Shutdown: Stop services
    if _calendar_indexer:
        _calendar_indexer.stop_scheduler()
        logger.info("Calendar indexer stopped")

    for _listener in _telegram_listeners:
        _listener.stop()
    if _telegram_listeners:
        logger.info("Telegram bot listeners stopped")

    if _reminder_scheduler:
        _reminder_scheduler.stop()
        logger.info("Scheduler stopped")

    if _scheduler_watcher:
        _scheduler_watcher.stop()
        logger.info("Scheduler file watcher stopped")

    if _task_watcher:
        _task_watcher.stop()
        logger.info("Task file watcher stopped")

    if _job_queue:
        _job_queue.stop_worker()
        logger.info("Job queue worker stopped")

    try:
        from api.services import agent_viz_summary_prefetch
        agent_viz_summary_prefetch.stop()
    except Exception:
        pass


app = FastAPI(
    title="LifeOS",
    description="Personal assistant system for semantic search and synthesis across Obsidian vault",
    version="0.2.0",
    lifespan=lifespan
)

# CORS: an explicit allowlist of the addresses this app answers on.
#
# The web UI does not depend on any of this — it is served by this app and
# addresses it with root-relative paths, so its requests are same-origin and
# never consult these rules. The list covers a page loaded at one of these
# addresses that calls the API at another.
#
# A wildcard here was both ineffective and dangerous. Browsers reject `*` on
# credentialed requests, so it never granted what it appeared to; and because
# this app has no authentication of its own, `*` widened the security boundary
# from "the tailnet" to "any page a tailnet device happens to have open".
_cors_origins = [
    f"http://localhost:{settings.port}",
    f"http://127.0.0.1:{settings.port}",
]
if settings.tailnet_https_url:
    _cors_origins.append(settings.tailnet_https_url.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Gzip: scoped to the CRM/people JSON endpoints plus the CRM page itself
# (#874), rather than applied app-wide. A 300-person list is ~216 KB, a
# person's full timeline ~620 KB, and the CRM page (`web/crm.html`) itself
# is ~750 KB uncompressed — all of which matter on Tailscale from a phone.
#
# This is a deliberate scoping choice, not a workaround for a streaming
# hazard: Starlette 0.52.1's GZipMiddleware already refuses to compress
# `text/event-stream` responses on its own (`DEFAULT_EXCLUDED_CONTENT_TYPES`
# in `starlette.middleware.gzip`), so applying it app-wide would not in fact
# risk buffering the chat/agents/conversations SSE endpoints. Scoping by an
# *allow*-list of paths instead keeps the gzip CPU cost (and the `Vary:
# Accept-Encoding` header) confined to the handful of routes this issue
# targets, and keeps that guarantee independent of Starlette's own default
# exclusion list, which is this dependency's choice to change.
_GZIP_API_PREFIXES = ("/api/crm", "/api/people")
# The page routes that serve `web/crm.html` (api/main.py's crm_page() and
# friends, further down this file) and their client-side-routed sub-paths
# (e.g. /crm/{person_id}/timeline). Matched as a full path segment, not a
# bare prefix, so a hypothetical future "/crmfoo" route wouldn't match.
_GZIP_PAGE_ROUTES = ("/crm", "/me", "/family", "/relationship", "/birthdays")


def _in_gzip_scope(path: str) -> bool:
    if path.startswith(_GZIP_API_PREFIXES):
        return True
    return any(path == route or path.startswith(route + "/") for route in _GZIP_PAGE_ROUTES)


class _ScopedGZipMiddleware:
    """Apply `GZipMiddleware` only to requests `_in_gzip_scope()` accepts."""

    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self._gzip_app = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _in_gzip_scope(scope["path"]):
            await self._gzip_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# compresslevel=6 (Starlette's default is 9): the gzip step itself runs
# synchronously in this middleware, on the event loop, for every matching
# response. Measured on the real 300-person list response (215,625 raw
# bytes): level 9 took ~4.5ms for 33,171 compressed bytes; level 6 took
# ~2.5ms for 34,311 bytes — nearly half the blocking time on the loop for
# ~3% more bytes on the wire, a better trade here.
app.add_middleware(_ScopedGZipMiddleware, minimum_size=1024, compresslevel=6)

# Route timing (#877): added last, which Starlette's middleware stack makes
# the OUTERMOST of this app's own middleware (each `add_middleware` call
# wraps *around* everything added before it) -- so its timing and byte
# count cover the full response, including gzip compression performed by
# `_ScopedGZipMiddleware` above and any header work CORS does. See
# api/services/route_timing.py for what is recorded.
app.add_middleware(RouteTimingMiddleware)

# Include routers
app.include_router(search.router)
app.include_router(ask.router)
app.include_router(calendar.router)
app.include_router(gmail.router)
app.include_router(drive.router)
app.include_router(people.router)
app.include_router(chat.router)
app.include_router(briefings.router)
app.include_router(admin.router)
app.include_router(conversations.router)
app.include_router(memories.router)
app.include_router(imessage.router)
app.include_router(crm.router)
app.include_router(slack.router)
app.include_router(photos.router)
app.include_router(reminders.router)
app.include_router(scheduler.router)
app.include_router(tasks.router)
app.include_router(monarch.router)
app.include_router(investments.router)
app.include_router(jobs.router)
app.include_router(perf.router)
app.include_router(agents.router)
app.include_router(vault.router)
app.include_router(fitness.router)
app.include_router(voice.router)
app.include_router(agent_proxy.router)
app.include_router(hermes_proxy.router)
app.include_router(journal.router)
app.include_router(journal_trends.router)
app.include_router(journal_ingest.router)

# Serve static files
web_dir = Path(__file__).parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert validation errors to 400 with clear messages."""
    errors = exc.errors()

    # Sanitize errors for JSON serialization (convert bytes to string)
    sanitized_errors = []
    for error in errors:
        sanitized = dict(error)
        if "input" in sanitized and isinstance(sanitized["input"], bytes):
            sanitized["input"] = sanitized["input"].decode("utf-8", errors="replace")
        sanitized_errors.append(sanitized)

    # Check if this is an empty query error
    for error in errors:
        if "query" in str(error.get("loc", [])):
            return JSONResponse(
                status_code=400,
                content={"error": "Query cannot be empty", "detail": sanitized_errors}
            )
    return JSONResponse(
        status_code=400,
        content={"error": "Validation error", "detail": sanitized_errors}
    )


@app.get("/health")
async def health_check():
    """Health check endpoint that verifies critical dependencies."""
    from config.settings import settings

    checks = {
        # Literally "is ANTHROPIC_API_KEY set" (#697's acceptance criteria),
        # not "is an LLM available" — an install running fully on
        # LIFEOS_LLM_BACKEND=local with no Anthropic key at all is a
        # supported configuration that will still report degraded here,
        # since this field only ever meant the Anthropic key specifically.
        "api_key_configured": bool(settings.anthropic_api_key and settings.anthropic_api_key.strip()),
        "reminder_scheduler": _reminder_scheduler.is_alive() if _reminder_scheduler else False,
        # Distinct from reminder_scheduler (the delivery thread, gated on
        # Telegram being configured): this is the file watcher that picks up
        # vault edits (e.g. via Obsidian) and re-indexes them, starts
        # unconditionally, and previously had no liveness signal of its own
        # (#766).
        "scheduler_watcher": _scheduler_watcher.is_alive() if _scheduler_watcher else False,
    }

    all_healthy = all(checks.values())

    return {
        "status": "healthy" if all_healthy else "degraded",
        "service": "lifeos",
        "checks": checks,
    }


@app.get("/health/raw-state")
async def health_raw_state(clear: bool = False):
    """
    Raw health state for out-of-band monitors (the lifeos_health watcher in
    nbramia/local-processing).

    Returns the same data the removed `_health_check_loop` used to aggregate:
    processor failures, stale/failed syncs, service degradation events, and
    critical service issues — all from the last 24h.

    If `clear=true`, atomically clears the transient in-memory counters
    (processor failures + degradation events) after reading. Intended for the
    monitor to call only after it has successfully delivered an alert, so the
    same incidents aren't reported twice. Safe to call without `clear` for
    read-only polls.
    """
    from api.services.notifications import get_recent_failures, clear_failures
    from api.services.sync_health import get_stale_syncs, get_failed_syncs
    from api.services.service_health import get_service_health

    processor_failures = get_recent_failures(hours=24)
    stale = get_stale_syncs()
    failed = get_failed_syncs(hours=24)
    registry = get_service_health()
    degradation_events = registry.get_degradation_events(hours=24)
    critical_issues = registry.get_critical_issues()

    cleared = {"processor_failures": 0, "degradation_events": 0}
    if clear:
        cleared["processor_failures"] = clear_failures()
        cleared["degradation_events"] = registry.clear_degradation_events()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "processor_failures": [
            {"timestamp": ts.isoformat(), "source": src, "error": err}
            for ts, src, err in processor_failures
        ],
        "stale_syncs": [
            {
                "source": s.source,
                "hours_since_sync": s.hours_since_sync,
                "last_sync": s.last_sync.isoformat() if s.last_sync else None,
                "expected_frequency": s.expected_frequency,
            }
            for s in stale
        ],
        "failed_syncs": failed,
        "degradation_events": [
            {
                "timestamp": e.timestamp.isoformat(),
                "service": e.service,
                "operation": e.operation,
                "fallback": e.fallback_used,
                "original_error": e.original_error,
            }
            for e in degradation_events
        ],
        "critical_issues": [
            {"service": svc, "error": err} for svc, err in critical_issues
        ],
        "cleared": cleared,
    }


def _check_vault_root_sanity(vault_search_check: "dict | None", vault_path) -> None:
    """Additive sanity check for the `vault_search` row in `GET /health/full`
    (#762). Sample a handful of indexed file paths and confirm they still
    fall under the currently configured vault root, catching a moved/deleted
    vault whose index keeps serving stale content from the old location — a
    drift the base request/response probe (does search return results at
    all) can't see, since it only confirms search returns *something*.

    Mutates `vault_search_check` in place, downgrading an "ok" status to
    "degraded" and adding a `vault_root_check` field explaining the mismatch
    — the existing `detail` from the request/response probe (e.g. "1
    results") is left untouched, since that check is preserved unchanged
    and this is an additional signal, not a replacement. A no-op if the base
    check isn't present or didn't itself report "ok" — this never turns a
    failing check into a passing one, or vice versa, only adds a further
    downgrade on top of an already-passing result.

    If the sample comes back empty (no vault documents were sampled — e.g.
    a collection that is currently all non-vault content), that is absence
    of signal, not evidence of a moved vault, so the check stays "ok" with a
    neutral note instead of downgrading (#762 follow-up).

    Degrade rule (#762 second follow-up): the failure this check exists to
    catch is a vault that *moved* — in that case NO sampled vault path is
    under the configured root. A sample where some paths match and some
    don't isn't that failure; it's more likely stray debris (e.g. a test
    run that indexed documents straight into this collection, or a leftover
    path from an old sync) sitting alongside otherwise-healthy content. So
    this only downgrades to "degraded" when the sample contains at least one
    vault path and *none* of them are under the root. A partial mismatch
    stays "ok" but still surfaces a `vault_root_check` note (counts only)
    so it's visible without being alarming.
    """
    if not vault_search_check or vault_search_check.get("status") != "ok":
        return
    try:
        from api.services.vectorstore import get_vector_store, sample_paths_match_vault_root
        sample = get_vector_store().sample_file_paths(limit=50)
        if not sample:
            vault_search_check["vault_root_check"] = "no vault documents sampled"
            return
        all_match, mismatched = sample_paths_match_vault_root(sample, vault_path)
        matched = len(sample) - len(mismatched)
        # Deliberately omit the mismatched paths and the configured root
        # itself in both branches below — this is an unauthenticated
        # endpoint, a real indexed file path can reveal personal
        # folder/file names, and the vault root is typically an absolute
        # path under the user's home directory (#697 review). The counts
        # alone are enough for an operator to act on.
        if matched == 0:
            vault_search_check["status"] = "degraded"
            vault_search_check["vault_root_check"] = (
                f"{len(mismatched)}/{len(sample)} sampled indexed path(s) fall outside "
                "the configured vault root"
            )
        elif not all_match:
            vault_search_check["vault_root_check"] = (
                f"{matched}/{len(sample)} sampled vault paths under root"
            )
    except Exception as e:
        # Never let this additive sanity check take down the primary
        # vault_search result — a vector-store hiccup here is already
        # visible via the chromadb_server check above.
        logger.warning(f"vault-root sanity check failed: {e}")


@app.get("/health/full")
async def full_health_check():
    """
    Comprehensive health check that tests all LifeOS services.

    Tests each service by calling the actual API endpoints the same way
    MCP tools would call them. Use this to verify all MCP tools will work.

    Returns detailed status for each service with timing.
    """
    import time
    import httpx
    from config.settings import settings

    BASE_URL = f"http://localhost:{settings.port}"

    results = {
        "status": "healthy",
        "service": "lifeos",
        "checks": {},
        "errors": [],
    }

    async def test_endpoint(name: str, method: str, path: str, params: dict = None, json_body: dict = None):
        """Test an endpoint by actually calling it."""
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                url = f"{BASE_URL}{path}"
                if method == "GET":
                    resp = await client.get(url, params=params)
                else:
                    resp = await client.post(url, json=json_body)

                elapsed = int((time.time() - start) * 1000)

                if resp.status_code == 200:
                    data = resp.json()
                    # Extract a summary from the response
                    if "results" in data:
                        detail = f"{len(data['results'])} results"
                    elif "files" in data:
                        detail = f"{len(data['files'])} files"
                    elif "events" in data:
                        detail = f"{len(data['events'])} events"
                    elif "emails" in data or "messages" in data:
                        detail = f"{len(data.get('emails', data.get('messages', [])))} emails"
                    elif "conversations" in data:
                        detail = f"{len(data['conversations'])} conversations"
                    elif "memories" in data:
                        detail = f"{len(data['memories'])} memories"
                    elif "people" in data:
                        detail = f"{len(data['people'])} people"
                    elif "answer" in data:
                        detail = f"synthesized ({len(data['answer'])} chars)"
                    else:
                        detail = "ok"

                    results["checks"][name] = {
                        "status": "ok",
                        "latency_ms": elapsed,
                        "detail": detail
                    }
                    return True
                else:
                    results["checks"][name] = {
                        "status": "error",
                        "latency_ms": elapsed,
                        "error": f"HTTP {resp.status_code}: {resp.text[:100]}"
                    }
                    results["errors"].append(f"{name}: HTTP {resp.status_code}")
                    return False

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            results["checks"][name] = {
                "status": "error",
                "latency_ms": elapsed,
                "error": str(e)
            }
            results["errors"].append(f"{name}: {str(e)}")
            return False

    # 1. Local LLM — `local_llm_url` has a non-empty default regardless of
    # whether the local backend is actually in use, so a bare "is the URL
    # string set" check always said "ok" even on an install that talks only
    # to Anthropic and has nothing listening on that port (#697). Report
    # not-in-use when the backend isn't "local" (truthful and not a
    # failure — excluded from the `failed` count below same as "ok"); only
    # when the backend is "local" do we actually probe reachability.
    backend = (settings.llm_backend or "anthropic").strip().lower()
    if backend != "local":
        results["checks"]["local_llm"] = {
            "status": "not_in_use",
            "detail": f"LIFEOS_LLM_BACKEND={backend!r}, local LLM not in use",
        }
    else:
        start = time.time()
        try:
            from api.services.llm_client import LocalLLMClient
            reachable = await LocalLLMClient().ais_available()
        except Exception as e:
            # ais_available() already catches its own network errors and
            # returns False; this is belt-and-suspenders against anything
            # else (e.g. client construction) so a local-LLM problem always
            # surfaces as this row's error, never a 500 from the whole
            # /health/full endpoint.
            reachable = False
            logger.warning(f"local_llm reachability probe raised unexpectedly: {e}")
        elapsed = int((time.time() - start) * 1000)
        if reachable:
            results["checks"]["local_llm"] = {
                "status": "ok",
                "latency_ms": elapsed,
                "detail": f"reachable ({settings.local_llm_url})",
            }
        else:
            results["checks"]["local_llm"] = {
                "status": "error",
                "latency_ms": elapsed,
                "error": f"unreachable ({settings.local_llm_url})",
            }
            results["errors"].append(f"local_llm: unreachable ({settings.local_llm_url})")

    # 2. ChromaDB Server (direct health check)
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.chroma_url}/api/v2/heartbeat")
            elapsed = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                results["checks"]["chromadb_server"] = {
                    "status": "ok",
                    "latency_ms": elapsed,
                    "detail": "connected",
                    "url": settings.chroma_url
                }
            else:
                results["checks"]["chromadb_server"] = {
                    "status": "error",
                    "latency_ms": elapsed,
                    "error": f"HTTP {resp.status_code}",
                    "url": settings.chroma_url
                }
                results["errors"].append(f"chromadb_server: HTTP {resp.status_code}")
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        results["checks"]["chromadb_server"] = {
            "status": "error",
            "latency_ms": elapsed,
            "error": str(e),
            "url": settings.chroma_url
        }
        results["errors"].append(f"chromadb_server: {str(e)}")

    # 3. Vault Search (POST /api/search) - tests ChromaDB + BM25
    await test_endpoint(
        "vault_search",
        "POST", "/api/search",
        json_body={"query": "test", "top_k": 1}
    )

    # 3b. Vault-root sanity check (#762) — the request/response check above
    # only confirms search returns *something*, not that what it returns
    # still lives where the vault is currently configured. A moved/deleted
    # vault can leave the index serving stale content from the old location
    # while that check keeps passing.
    _check_vault_root_sanity(results["checks"].get("vault_search"), settings.vault_path)

    # 3. Calendar Upcoming (GET /api/calendar/upcoming)
    await test_endpoint(
        "calendar_upcoming",
        "GET", "/api/calendar/upcoming",
        params={"days": 1}
    )

    # 4. Calendar Search (GET /api/calendar/search)
    await test_endpoint(
        "calendar_search",
        "GET", "/api/calendar/search",
        params={"q": "meeting"}
    )

    # 5. Gmail Search (GET /api/gmail/search) — GmailService.search() catches
    # credential errors internally and returns an empty list (by design, so
    # chat/agent callers degrade gracefully rather than raising), so hitting
    # the endpoint directly always reports "ok, 0 emails" even with zero
    # Google credentials configured — unlike calendar/drive below, whose
    # routes let a missing-credentials FileNotFoundError surface as a 401.
    # Preflight a plain file-existence check for the account this probe uses
    # so all three Google rows report the same not-configured shape (#697).
    from api.services.google_auth import get_google_auth, GoogleAccount
    if not get_google_auth(GoogleAccount.PERSONAL).credentials_path.exists():
        # Same shape as test_endpoint()'s error rows below (status/latency_ms/
        # error) so a monitor parsing "error" rows doesn't need a special
        # case for this one — latency_ms is 0 since this is a local
        # filesystem check, not a network call.
        results["checks"]["gmail_search"] = {
            "status": "error",
            "latency_ms": 0,
            "error": "Google credentials not configured (personal account)",
        }
        results["errors"].append("gmail_search: Google credentials not configured (personal account)")
    else:
        await test_endpoint(
            "gmail_search",
            "GET", "/api/gmail/search",
            params={"q": "in:inbox", "max_results": 1}
        )

    # 6. Drive Search - Personal (GET /api/drive/search)
    await test_endpoint(
        "drive_search_personal",
        "GET", "/api/drive/search",
        params={"q": "test", "account": "personal", "max_results": 1}
    )

    # 7. Drive Search - Work (GET /api/drive/search)
    await test_endpoint(
        "drive_search_work",
        "GET", "/api/drive/search",
        params={"q": "test", "account": "work", "max_results": 1}
    )

    # 8. People Search (GET /api/people/search)
    await test_endpoint(
        "people_search",
        "GET", "/api/people/search",
        params={"q": "a"}
    )

    # 9. Conversations List (GET /api/conversations)
    await test_endpoint(
        "conversations_list",
        "GET", "/api/conversations",
        params={"limit": 1}
    )

    # 10. Memories List (GET /api/memories)
    await test_endpoint(
        "memories_list",
        "GET", "/api/memories",
        params={"limit": 1}
    )

    # 11. iMessage Statistics (GET /api/imessage/statistics)
    await test_endpoint(
        "imessage_stats",
        "GET", "/api/imessage/statistics",
    )

    # 12. Model readout (#658) — which model is actually serving each chat
    # surface right now. Informational, not a pass/fail check: kept out of
    # `results["checks"]` (and its "ok"/"error" degraded/unhealthy counting
    # below) because an "unknown" Hermes readout doesn't mean LifeOS itself
    # is unhealthy. See api/services/model_readout.py.
    from api.services.model_readout import get_model_readout
    results["models"] = await get_model_readout()

    # Set overall status
    failed = [k for k, v in results["checks"].items() if v["status"] == "error"]
    if failed:
        results["status"] = "degraded" if len(failed) < 5 else "unhealthy"
        results["summary"] = f"{len(failed)} service(s) failing: {', '.join(failed)}"
    else:
        results["summary"] = f"All {len(results['checks'])} services healthy"

    return results


@app.get("/health/services")
async def service_health_check():
    """
    Get real-time status of all external services.

    Returns:
    - overall_status: healthy/degraded/critical
    - services: per-service status with last check time
    - degradation_events: recent fallback usage (last 24h)
    - critical_issues: services requiring immediate attention

    Use this to monitor service availability and degradation patterns.
    """
    from api.services.service_health import get_service_health
    return get_service_health().get_summary()


def _run_consistency_check() -> dict:
    """Run Phase 7 consistency verification in read-only mode."""
    import sys as _sys
    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    from sync_consistency_verify import verify_consistency
    return verify_consistency(dry_run=True)


# Cache for data-integrity check results (1-hour TTL)
_data_integrity_cache: dict = {"result": None, "timestamp": 0.0}
_DATA_INTEGRITY_TTL = 3600  # seconds


@app.get("/health/data-integrity")
def data_integrity_check():
    """
    Check cross-store data consistency.

    Reuses Phase 7 verification logic (sync_consistency_verify.py) in
    read-only mode. Results are cached for 1 hour since the checks hit
    multiple databases.

    Returns per-check counts and an overall status:
    - "healthy" if all counts are zero
    - "degraded" if any non-zero counts exist

    Declared as a sync def so FastAPI runs it in a threadpool,
    avoiding event loop blocking during SQLite I/O.
    """
    import time

    now = time.time()
    if (
        _data_integrity_cache["result"] is not None
        and now - _data_integrity_cache["timestamp"] < _DATA_INTEGRITY_TTL
    ):
        return _data_integrity_cache["result"]

    start = time.time()

    try:
        result = _run_consistency_check()
    except Exception as e:
        logger.error(f"Data integrity check failed: {e}")
        return {"status": "error", "error": str(e), "cached": False}

    status = "healthy" if result["total_issues"] == 0 else "degraded"
    response = {
        "status": status,
        "checks": {
            "person_stats_mismatches": result["person_stats_mismatches"]["count"],
            "orphaned_interactions": result["orphaned_interactions"]["count"],
            "hidden_interactions": result["hidden_interactions"]["count"],
            "stale_merged_ids": result["stale_merged_ids"]["count"],
            "stale_merged_relationships": result["stale_merged_relationships"]["count"],
            "relationship_hygiene": result["relationship_hygiene"]["count"],
            "orphaned_crm_records": result["orphaned_crm_records"]["count"],
        },
        "total_issues": result["total_issues"],
        "elapsed_ms": round((time.time() - start) * 1000),
        "cached": False,
    }

    _data_integrity_cache["result"] = {**response, "cached": True}
    _data_integrity_cache["timestamp"] = now

    return response


@app.get("/manifest.webmanifest")
async def web_manifest():
    """Serve the web app manifest (#727).

    Served from its own route rather than through /static so the
    Content-Type is guaranteed to be application/manifest+json — some
    browsers ignore a manifest served with the wrong content type, and
    relying on the OS's mimetypes registry to know the .webmanifest
    extension isn't portable across a fresh install.
    """
    manifest_path = Path(__file__).parent.parent / "web" / "manifest.webmanifest"
    if manifest_path.exists():
        return FileResponse(str(manifest_path), media_type="application/manifest+json")
    return {"message": "Manifest not found"}


@app.get("/")
async def root():
    """Serve the homepage."""
    home_path = Path(__file__).parent.parent / "web" / "home.html"
    if home_path.exists():
        return FileResponse(str(home_path))
    return {"message": "LifeOS API", "version": "0.3.0"}


@app.get("/chat")
async def chat_page():
    """Serve the chat UI."""
    index_path = Path(__file__).parent.parent / "web" / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Chat page not found"}


_CRM_CACHE_CONTROL = "max-age=60, must-revalidate"


def _if_none_match_hits(if_none_match: str, etag: str) -> bool:
    """Does this request's `If-None-Match` cover `etag` (RFC 9110 §13.1.2)?

    `*` matches any current representation. Otherwise this is a
    comma-separated list of entity-tags, each optionally weak (`W/"..."`
    rather than `"..."`) — a weak validator from an intermediary still means
    "you already have an equivalent representation" for a static file that
    only ever changes by full replacement, so it's stripped before
    comparing rather than treated as a non-match.
    """
    candidates = [tag.strip() for tag in if_none_match.split(",")]
    if "*" in candidates:
        return True
    normalized = [tag[2:] if tag.startswith("W/") else tag for tag in candidates]
    return etag in normalized


def _crm_file_response(request: Request) -> Response:
    """Serve `web/crm.html` with a short revalidation cache lifetime.

    `FileResponse` on its own computes an ETag from the file's mtime/size but
    never checks it against the request's `If-None-Match`, so every
    navigation between Me/Family/Birthdays/a person page re-sent the full
    ~750 KB page. This replicates Starlette's own ETag algorithm so the
    header matches what `FileResponse` would have sent, and answers with a
    bodyless 304 when the client's cached copy is still current.
    """
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if not crm_path.exists():
        return JSONResponse({"message": "CRM page not found"}, status_code=404)

    stat_result = os.stat(crm_path)
    etag_base = f"{stat_result.st_mtime}-{stat_result.st_size}"
    etag = f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    headers = {
        "Cache-Control": _CRM_CACHE_CONTROL,
        "ETag": etag,
        "Last-Modified": formatdate(stat_result.st_mtime, usegmt=True),
    }

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and _if_none_match_hits(if_none_match, etag):
        return Response(status_code=304, headers=headers)

    return FileResponse(str(crm_path), stat_result=stat_result, headers=headers)


@app.get("/crm")
async def crm_page(request: Request):
    """Serve the CRM UI."""
    return _crm_file_response(request)


@app.get("/agents")
async def agents_page():
    """Serve the agent activity visualization UI."""
    agents_path = Path(__file__).parent.parent / "web" / "agents.html"
    if agents_path.exists():
        return FileResponse(str(agents_path))
    return {"message": "Agents page not found"}


@app.get("/journal")
async def journal_page():
    """Serve the journal emotion-wheel visualization UI (#212)."""
    journal_path = Path(__file__).parent.parent / "web" / "journal.html"
    if journal_path.exists():
        return FileResponse(str(journal_path))
    return {"message": "Journal page not found"}


@app.get("/journal/trends")
async def journal_trends_page():
    """Serve the journal trend views UI: the strip, the unexplored wheel,
    felt-vs-recorded connection, and the scalar stack."""
    trends_path = Path(__file__).parent.parent / "web" / "journal-trends.html"
    if trends_path.exists():
        return FileResponse(str(trends_path))
    return {"message": "Journal trends page not found"}


@app.get("/crm/{path:path}")
async def crm_page_with_path(request: Request, path: str):
    """Serve the CRM UI for any sub-path (client-side routing)."""
    return _crm_file_response(request)


@app.get("/me")
async def me_page(request: Request):
    """Serve the CRM UI for the 'Me' dashboard (owner's profile)."""
    return _crm_file_response(request)


@app.get("/me/{path:path}")
async def me_page_with_path(request: Request, path: str):
    """Serve the CRM UI for 'Me' sub-paths (client-side routing)."""
    return _crm_file_response(request)


@app.get("/family")
async def family_page(request: Request):
    """Serve the CRM UI for the Family dashboard."""
    return _crm_file_response(request)


@app.get("/family/{path:path}")
async def family_page_with_path(request: Request, path: str):
    """Serve the CRM UI for Family sub-paths (client-side routing)."""
    return _crm_file_response(request)


@app.get("/relationship")
async def relationship_page(request: Request):
    """Serve the CRM UI for the Relationship dashboard."""
    return _crm_file_response(request)


@app.get("/relationship/{path:path}")
async def relationship_page_with_path(request: Request, path: str):
    """Serve the CRM UI for Relationship sub-paths (client-side routing)."""
    return _crm_file_response(request)


@app.get("/birthdays")
async def birthdays_page(request: Request):
    """Serve the CRM UI for the Birthdays page."""
    return _crm_file_response(request)


@app.get("/birthdays/{path:path}")
async def birthdays_page_with_path(request: Request, path: str):
    """Serve the CRM UI for Birthdays sub-paths (client-side routing)."""
    return _crm_file_response(request)
