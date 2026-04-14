"""
Admin API endpoints for LifeOS.

Provides:
- Reindexing endpoint
- System status
- Configuration info
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import logging

from api.services.indexer import IndexerService
from api.services.vectorstore import VectorStore
from api.services.job_queue import get_job_queue
from config.settings import settings

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)


class IndexStatus(BaseModel):
    """Status of the index."""
    status: str
    document_count: int
    vault_path: str
    message: Optional[str] = None


class ReindexResponse(BaseModel):
    """Response from reindex operation."""
    status: str
    message: str
    job_id: Optional[str] = None
    files_indexed: Optional[int] = None


@router.get("/status", response_model=IndexStatus)
async def get_status() -> IndexStatus:
    """
    Get current system status.

    Returns document count and configuration info.
    """
    try:
        vs = VectorStore()
        # Get approximate count from ChromaDB
        count = len(vs.search("", top_k=10000))  # Workaround for count
    except Exception as e:
        logger.error(f"Error getting document count: {e}")
        count = 0

    # Check if a reindex job is currently running
    queue = get_job_queue()
    running_jobs = queue.list_jobs(status="running", job_type="reindex_vault", limit=1)
    reindex_in_progress = len(running_jobs) > 0

    # Get last completed reindex result
    completed_jobs = queue.list_jobs(status="completed", job_type="reindex_vault", limit=1)
    last_count = 0
    if completed_jobs and completed_jobs[0].result:
        last_count = completed_jobs[0].result.get("files_indexed", 0)

    return IndexStatus(
        status="reindexing" if reindex_in_progress else "ready",
        document_count=count,
        vault_path=str(settings.vault_path),
        message=f"Last reindex: {last_count} files" if last_count > 0 else None,
    )


@router.post("/reindex", response_model=ReindexResponse)
async def reindex() -> ReindexResponse:
    """
    Trigger a full reindex of the vault.

    Enqueues a background job. Use /api/jobs/{job_id} to check progress.
    """
    queue = get_job_queue()

    # Check if a reindex is already running or pending
    for status in ("running", "pending"):
        existing = queue.list_jobs(status=status, job_type="reindex_vault", limit=1)
        if existing:
            return ReindexResponse(
                status="already_queued",
                message=f"Reindex is already {status}. Check /api/jobs/{existing[0].id} for updates.",
                job_id=existing[0].id,
            )

    job_id = queue.enqueue("reindex_vault", priority=5)

    return ReindexResponse(
        status="started",
        message=f"Reindex enqueued. Check /api/jobs/{job_id} for progress.",
        job_id=job_id,
    )


@router.post("/reindex/sync", response_model=ReindexResponse)
async def reindex_sync() -> ReindexResponse:
    """
    Trigger a synchronous reindex of the vault.

    Blocks until complete. Use for initial setup.
    """
    try:
        indexer = IndexerService(vault_path=settings.vault_path)
        count = indexer.index_all()

        return ReindexResponse(
            status="success",
            message="Reindex complete.",
            files_indexed=count,
        )
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
        return ReindexResponse(
            status="error",
            message=f"Reindex failed: {str(e)}",
        )


# ============ Maintenance Mode Endpoints ============


@router.post("/maintenance")
async def enter_maintenance_mode(duration_seconds: int = 14400):
    """
    Enter maintenance mode — suppress CRITICAL alerts for the given duration.

    Call this before operations that may cause transient service unavailability
    (nightly sync, manual ChromaDB restart, etc.). Auto-expires after duration.

    Args:
        duration_seconds: How long to suppress alerts (default: 4 hours)
    """
    from api.services.service_health import get_service_health
    registry = get_service_health()
    registry.enter_maintenance(duration_seconds)
    return {"status": "ok", "duration_seconds": duration_seconds}


@router.delete("/maintenance")
async def exit_maintenance_mode():
    """Exit maintenance mode early — re-enable CRITICAL alerts."""
    from api.services.service_health import get_service_health
    registry = get_service_health()
    registry.exit_maintenance()
    return {"status": "ok"}


# ============ Calendar Indexer Endpoints ============


class CalendarSyncStatus(BaseModel):
    """Status of the calendar indexer."""
    status: str
    scheduler_running: bool
    last_sync: Optional[str] = None
    message: Optional[str] = None


class CalendarSyncResponse(BaseModel):
    """Response from calendar sync operation."""
    status: str
    events_indexed: int
    errors: list[str] = []
    elapsed_seconds: float
    last_sync: str


@router.get("/calendar/status", response_model=CalendarSyncStatus)
async def get_calendar_sync_status() -> CalendarSyncStatus:
    """
    Get status of the calendar indexer scheduler.

    Returns whether the scheduler is running and when the last sync occurred.
    """
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        indexer = get_calendar_indexer()
        status = indexer.get_status()

        return CalendarSyncStatus(
            status="ok",
            scheduler_running=status["running"],
            last_sync=status["last_sync"],
            message="Calendar sync scheduler is running" if status["running"] else "Scheduler not running"
        )
    except Exception as e:
        logger.error(f"Failed to get calendar status: {e}")
        return CalendarSyncStatus(
            status="error",
            scheduler_running=False,
            message=str(e)
        )


@router.post("/calendar/sync", response_model=CalendarSyncResponse)
async def trigger_calendar_sync(days_past: int = 30, days_future: int = 30) -> CalendarSyncResponse:
    """
    Trigger an immediate calendar sync.

    Fetches events from the specified date range and indexes them into ChromaDB.

    Args:
        days_past: Number of days in the past to fetch (default: 30)
        days_future: Number of days in the future to fetch (default: 30)
    """
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        indexer = get_calendar_indexer()
        result = indexer.sync(days_past=days_past, days_future=days_future)

        return CalendarSyncResponse(
            status=result["status"],
            events_indexed=result["events_indexed"],
            errors=result.get("errors", []),
            elapsed_seconds=result["elapsed_seconds"],
            last_sync=result["last_sync"]
        )
    except Exception as e:
        logger.error(f"Calendar sync failed: {e}")
        return CalendarSyncResponse(
            status="error",
            events_indexed=0,
            errors=[str(e)],
            elapsed_seconds=0,
            last_sync=""
        )


@router.post("/calendar/start")
async def start_calendar_scheduler(
    interval_hours: Optional[float] = None,
    use_time_schedule: bool = True,
    timezone: str = ""
):
    """
    Start the calendar sync scheduler.

    Args:
        interval_hours: Hours between syncs (if not using time schedule)
        use_time_schedule: Use time-of-day schedule (default: True, syncs at 8 AM, noon, 3 PM)
        timezone: Timezone for time schedule (defaults to settings.timezone)
    """
    timezone = timezone or settings.timezone
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        indexer = get_calendar_indexer()

        if use_time_schedule:
            indexer.start_time_scheduler(
                schedule_times=[(8, 0), (12, 0), (15, 0)],
                timezone=timezone
            )
            return {"status": "started", "message": f"Calendar scheduler started (8:00, 12:00, 15:00 {timezone})"}
        else:
            hours = interval_hours or 24.0
            indexer.start_scheduler(interval_hours=hours)
            return {"status": "started", "message": f"Calendar scheduler started ({hours}h interval)"}
    except Exception as e:
        logger.error(f"Failed to start calendar scheduler: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/calendar/stop")
async def stop_calendar_scheduler():
    """Stop the calendar sync scheduler."""
    try:
        from api.services.calendar_indexer import get_calendar_indexer
        indexer = get_calendar_indexer()
        indexer.stop_scheduler()
        return {"status": "stopped", "message": "Calendar scheduler stopped"}
    except Exception as e:
        logger.error(f"Failed to stop calendar scheduler: {e}")
        return {"status": "error", "message": str(e)}


# ============ Usage Tracking Endpoints ============


class UsageStats(BaseModel):
    """Usage statistics for a time period."""
    total_cost: float
    total_input_tokens: int
    total_output_tokens: int
    request_count: int


class UsageSummary(BaseModel):
    """Complete usage summary."""
    last_24h: UsageStats
    last_7d: UsageStats
    last_30d: UsageStats
    all_time: UsageStats
    daily_breakdown: list[dict]


@router.get("/usage", response_model=UsageSummary)
async def get_usage_summary() -> UsageSummary:
    """
    Get usage summary with stats for 24h, 7d, 30d, and all-time.

    Also includes daily cost breakdown for charting.
    """
    try:
        from api.services.usage_store import get_usage_store
        usage_store = get_usage_store()
        summary = usage_store.get_summary()

        return UsageSummary(
            last_24h=UsageStats(**summary["last_24h"]),
            last_7d=UsageStats(**summary["last_7d"]),
            last_30d=UsageStats(**summary["last_30d"]),
            all_time=UsageStats(**summary["all_time"]),
            daily_breakdown=summary["daily_breakdown"]
        )
    except Exception as e:
        logger.error(f"Failed to get usage summary: {e}")
        # Return empty stats on error
        empty_stats = UsageStats(
            total_cost=0.0,
            total_input_tokens=0,
            total_output_tokens=0,
            request_count=0
        )
        return UsageSummary(
            last_24h=empty_stats,
            last_7d=empty_stats,
            last_30d=empty_stats,
            all_time=empty_stats,
            daily_breakdown=[]
        )