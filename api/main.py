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
# Load environment variables from .env file first, before any imports
from dotenv import load_dotenv
load_dotenv()

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from api.routes import search, ask, calendar, gmail, drive, people, chat, briefings, admin, conversations, memories, imessage, crm, slack, photos, reminders, tasks, monarch
from config.settings import settings

logger = logging.getLogger(__name__)

# Background services (initialized on startup)
_granola_processor = None
_omi_processor = None
_calendar_indexer = None
_people_v2_sync_thread = None
_people_v2_stop_event = threading.Event()
_telegram_listener = None
_reminder_scheduler = None


def _health_check_loop(stop_event: threading.Event, schedule_times: list[tuple[int, int]] = None, timezone: str = "America/New_York"):
    """
    Background thread for health checks and failure notifications.

    Runs at scheduled times to check sync health and processor status.
    Default schedule: 2:30 AM (pre-sync) and 7:00 AM (post-sync).

    NOTE: All sync operations run via launchd at 3:00 AM (scripts/run_all_syncs.py).
    This loop only monitors health and sends alerts.

    Args:
        stop_event: Event to signal thread shutdown
        schedule_times: List of (hour, minute) tuples for when to run health checks
        timezone: Timezone for scheduling
    """
    if schedule_times is None:
        schedule_times = [(2, 30), (7, 0)]  # 2:30 AM pre-sync, 7:00 AM post-sync

    tz = ZoneInfo(timezone)

    while not stop_event.is_set():
        now = datetime.now(tz)

        # Calculate next run time from all scheduled times
        candidates = []
        for hour, minute in schedule_times:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= candidate:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        next_run = min(candidates)

        # Sleep until next run (check every 60 seconds for stop signal)
        while datetime.now(tz) < next_run and not stop_event.is_set():
            stop_event.wait(timeout=60)

        if stop_event.is_set():
            break

        check_time = datetime.now(tz).strftime("%H:%M")
        logger.info(f"Health check ({check_time}): Starting...")

        # Track failures for notification
        failures = []

        # Collect processor failures from the last 24 hours
        # (Granola processor, Omi processor, file watcher, etc.)
        try:
            from api.services.notifications import get_recent_failures, clear_failures
            processor_failures = get_recent_failures(hours=24)
            for ts, source, error in processor_failures:
                failures.append((f"{source} ({ts.strftime('%H:%M')})", error))
            if processor_failures:
                clear_failures()  # Clear after collecting
                logger.info(f"Collected {len(processor_failures)} processor failures from last 24h")
        except Exception as e:
            logger.error(f"Failed to collect processor failures: {e}")

        # Check sync health from the unified daily sync
        try:
            from api.services.sync_health import get_stale_syncs, get_failed_syncs
            stale = get_stale_syncs()
            failed = get_failed_syncs(hours=24)

            for sync in stale:
                failures.append((f"{sync.source} (stale)", f"Last sync: {sync.hours_since_sync:.1f}h ago"))
            for sync in failed:
                failures.append((f"{sync['source']} (failed)", sync.get('error_message', 'Unknown error')[:100]))
        except Exception as e:
            logger.error(f"Failed to check sync health: {e}")

        # Collect service degradation events (fallback usage)
        try:
            from api.services.service_health import get_service_health
            registry = get_service_health()
            events = registry.get_degradation_events(hours=24)

            # Report if there were frequent degradations (>5 in 24h)
            if len(events) >= 5:
                # Group by service for summary
                by_service = {}
                for event in events:
                    by_service[event.service] = by_service.get(event.service, 0) + 1

                for service, count in by_service.items():
                    failures.append((f"{service} (degraded)", f"{count} fallback events in 24h"))

            # Also report critical issues
            for service, error in registry.get_critical_issues():
                failures.append((f"{service} (CRITICAL)", error[:100]))

            # Clear events after including in report
            if events:
                registry.clear_degradation_events()
                logger.info(f"Collected {len(events)} degradation events from last 24h")
        except Exception as e:
            logger.error(f"Failed to collect service health: {e}")

        # Send notification if any failures occurred
        if failures:
            logger.warning(f"Health check: {len(failures)} issue(s), sending alert...")
            try:
                from api.services.notifications import send_alert
                failure_lines = [f"- {name}: {error}" for name, error in failures]
                send_alert(
                    subject=f"LifeOS: {len(failures)} sync issue(s)",
                    body=f"The following issues were detected in the last 24 hours:\n\n" + "\n".join(failure_lines),
                )
            except Exception as e:
                logger.error(f"Failed to send failure notification: {e}")
        else:
            logger.info("Health check: All systems healthy")

        logger.info("Health check: Complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    global _granola_processor, _omi_processor, _calendar_indexer, _people_v2_sync_thread, _telegram_listener, _reminder_scheduler

    # Startup: Initialize and start Granola processor
    try:
        from api.services.granola_processor import GranolaProcessor
        _granola_processor = GranolaProcessor(settings.vault_path)
        _granola_processor.start_watching()
        logger.info("Granola processor started successfully")
    except Exception as e:
        logger.error(f"Failed to start Granola processor: {e}")

    # Startup: Initialize and start Omi processor
    try:
        from api.services.omi_processor import OmiProcessor
        _omi_processor = OmiProcessor(settings.vault_path)
        _omi_processor.start()
        logger.info("Omi processor started successfully")
    except Exception as e:
        logger.error(f"Failed to start Omi processor: {e}")

    # Startup: Initialize and start Calendar indexer at specific times (Eastern)
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        _calendar_indexer = get_calendar_indexer()
        # Sync at 8 AM, noon, and 3 PM Eastern
        _calendar_indexer.start_time_scheduler(
            schedule_times=[(8, 0), (12, 0), (15, 0)],
            timezone="America/New_York"
        )
        logger.info("Calendar indexer scheduler started (8:00, 12:00, 15:00 Eastern)")
    except Exception as e:
        logger.error(f"Failed to start Calendar indexer: {e}")

    # Startup: Initialize health check scheduler (2:30 AM and 7:00 AM Eastern)
    # Unified sync runs via launchd at 3:00 AM
    try:
        _people_v2_stop_event.clear()
        _people_v2_sync_thread = threading.Thread(
            target=_health_check_loop,
            args=(_people_v2_stop_event,),
            kwargs={"schedule_times": [(2, 30), (7, 0)], "timezone": "America/New_York"},
            daemon=True,
            name="HealthCheckThread"
        )
        _people_v2_sync_thread.start()
        logger.info("Health check scheduler started (2:30 AM + 7:00 AM Eastern)")
    except Exception as e:
        logger.error(f"Failed to start health check scheduler: {e}")

    # Startup: Start Telegram bot listener
    try:
        from api.services.telegram import get_telegram_listener
        _telegram_listener = get_telegram_listener()
        _telegram_listener.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram bot listener: {e}")

    # Startup: Start reminder scheduler
    try:
        from api.services.reminder_store import get_reminder_scheduler
        _reminder_scheduler = get_reminder_scheduler()
        _reminder_scheduler.start()
    except Exception as e:
        logger.error(f"Failed to start reminder scheduler: {e}")

    yield  # Application runs here

    # Shutdown: Stop services
    if _granola_processor:
        _granola_processor.stop()
        logger.info("Granola processor stopped")

    if _omi_processor:
        _omi_processor.stop()
        logger.info("Omi processor stopped")

    if _calendar_indexer:
        _calendar_indexer.stop_scheduler()
        logger.info("Calendar indexer stopped")

    if _people_v2_sync_thread and _people_v2_sync_thread.is_alive():
        _people_v2_stop_event.set()
        _people_v2_sync_thread.join(timeout=5)
        logger.info("Nightly sync scheduler stopped")

    if _telegram_listener:
        _telegram_listener.stop()
        logger.info("Telegram bot listener stopped")

    # Cancel any active Claude Code session
    try:
        from api.services.claude_orchestrator import get_orchestrator
        orch = get_orchestrator()
        if orch.is_busy():
            orch.cancel()
            logger.info("Cancelled active Claude Code session during shutdown")
    except Exception:
        pass

    if _reminder_scheduler:
        _reminder_scheduler.stop()
        logger.info("Reminder scheduler stopped")


app = FastAPI(
    title="LifeOS",
    description="Personal assistant system for semantic search and synthesis across Obsidian vault",
    version="0.2.0",
    lifespan=lifespan
)

# CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
app.include_router(tasks.router)
app.include_router(monarch.router)

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
        "api_key_configured": bool(settings.anthropic_api_key and settings.anthropic_api_key.strip()),
    }

    all_healthy = all(checks.values())

    return {
        "status": "healthy" if all_healthy else "degraded",
        "service": "lifeos",
        "checks": checks,
    }


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

    # 1. Config check (no HTTP needed)
    if settings.anthropic_api_key and settings.anthropic_api_key.strip():
        results["checks"]["anthropic_api_key"] = {"status": "ok", "detail": "configured"}
    else:
        results["checks"]["anthropic_api_key"] = {"status": "error", "error": "not configured"}
        results["errors"].append("anthropic_api_key: not configured")

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

    # 5. Gmail Search (GET /api/gmail/search)
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


@app.get("/crm")
async def crm_page():
    """Serve the CRM UI."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/crm/{path:path}")
async def crm_page_with_path(path: str):
    """Serve the CRM UI for any sub-path (client-side routing)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/me")
async def me_page():
    """Serve the CRM UI for the 'Me' dashboard (owner's profile)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/me/{path:path}")
async def me_page_with_path(path: str):
    """Serve the CRM UI for 'Me' sub-paths (client-side routing)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/family")
async def family_page():
    """Serve the CRM UI for the Family dashboard."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/family/{path:path}")
async def family_page_with_path(path: str):
    """Serve the CRM UI for Family sub-paths (client-side routing)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/relationship")
async def relationship_page():
    """Serve the CRM UI for the Relationship dashboard."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/relationship/{path:path}")
async def relationship_page_with_path(path: str):
    """Serve the CRM UI for Relationship sub-paths (client-side routing)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/birthdays")
async def birthdays_page():
    """Serve the CRM UI for the Birthdays page."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}


@app.get("/birthdays/{path:path}")
async def birthdays_page_with_path(path: str):
    """Serve the CRM UI for Birthdays sub-paths (client-side routing)."""
    crm_path = Path(__file__).parent.parent / "web" / "crm.html"
    if crm_path.exists():
        return FileResponse(str(crm_path))
    return {"message": "CRM page not found"}
