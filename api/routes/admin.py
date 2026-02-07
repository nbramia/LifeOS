"""
Admin API endpoints for LifeOS.

Provides:
- Reindexing endpoint
- System status
- Configuration info
"""
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
import logging

from api.services.indexer import IndexerService
from api.services.vectorstore import VectorStore
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
    files_indexed: Optional[int] = None


# Track reindex state
_reindex_in_progress = False
_last_reindex_count = 0


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

    global _reindex_in_progress

    return IndexStatus(
        status="reindexing" if _reindex_in_progress else "ready",
        document_count=count,
        vault_path=str(settings.vault_path),
        message=f"Last reindex: {_last_reindex_count} files" if _last_reindex_count > 0 else None,
    )


def _do_reindex():
    """Background task to reindex vault."""
    global _reindex_in_progress, _last_reindex_count

    _reindex_in_progress = True
    try:
        indexer = IndexerService(vault_path=settings.vault_path)
        count = indexer.index_all()
        _last_reindex_count = count
        logger.info(f"Reindex complete: {count} files")
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
    finally:
        _reindex_in_progress = False


@router.post("/reindex", response_model=ReindexResponse)
async def reindex(background_tasks: BackgroundTasks) -> ReindexResponse:
    """
    Trigger a full reindex of the vault.

    Runs in the background. Use /api/admin/status to check progress.
    """
    global _reindex_in_progress

    if _reindex_in_progress:
        return ReindexResponse(
            status="already_running",
            message="Reindex is already in progress. Check /api/admin/status for updates.",
        )

    background_tasks.add_task(_do_reindex)

    return ReindexResponse(
        status="started",
        message="Reindex started in background. Check /api/admin/status for progress.",
    )


@router.post("/reindex/sync", response_model=ReindexResponse)
async def reindex_sync() -> ReindexResponse:
    """
    Trigger a synchronous reindex of the vault.

    Blocks until complete. Use for initial setup.
    """
    global _reindex_in_progress, _last_reindex_count

    if _reindex_in_progress:
        return ReindexResponse(
            status="already_running",
            message="Reindex is already in progress.",
        )

    _reindex_in_progress = True
    try:
        indexer = IndexerService(vault_path=settings.vault_path)
        count = indexer.index_all()
        _last_reindex_count = count

        return ReindexResponse(
            status="success",
            message=f"Reindex complete.",
            files_indexed=count,
        )
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
        return ReindexResponse(
            status="error",
            message=f"Reindex failed: {str(e)}",
        )
    finally:
        _reindex_in_progress = False


# ============ Granola Processor Endpoints ============


class GranolaStatus(BaseModel):
    """Status of the Granola processor."""
    status: str
    watching: bool
    granola_path: str
    pending_files: int
    interval_seconds: int
    message: Optional[str] = None


class GranolaProcessResponse(BaseModel):
    """Response from Granola processing operation."""
    status: str
    message: str
    processed: int
    failed: int
    skipped: int
    moves: list[dict] = []


@router.get("/granola/status", response_model=GranolaStatus)
async def get_granola_status() -> GranolaStatus:
    """
    Get status of the Granola inbox processor.

    Returns whether it's running and how many files are pending.
    """
    from pathlib import Path

    granola_path = Path(settings.vault_path) / "Granola"
    pending_count = len(list(granola_path.glob("*.md"))) if granola_path.exists() else 0

    # Try to get the processor instance from main
    try:
        from api.main import _granola_processor
        running = _granola_processor.is_running if _granola_processor else False
        interval = _granola_processor.interval_seconds if _granola_processor else 300
    except Exception:
        running = False
        interval = 300

    return GranolaStatus(
        status="running" if running else "stopped",
        watching=running,
        granola_path=str(granola_path),
        pending_files=pending_count,
        interval_seconds=interval,
        message=f"{pending_count} files pending in Granola inbox" if pending_count > 0 else "Inbox is empty"
    )


@router.post("/granola/process", response_model=GranolaProcessResponse)
async def process_granola_backlog() -> GranolaProcessResponse:
    """
    Process all pending files in the Granola inbox immediately.

    Classifies and moves files to appropriate destinations based on content.
    """
    try:
        from api.services.granola_processor import GranolaProcessor

        processor = GranolaProcessor(settings.vault_path)
        results = processor.process_backlog()

        return GranolaProcessResponse(
            status="success",
            message=f"Processed {results['processed']} files",
            processed=results["processed"],
            failed=results["failed"],
            skipped=results["skipped"],
            moves=results["moves"]
        )
    except Exception as e:
        logger.error(f"Granola processing failed: {e}")
        return GranolaProcessResponse(
            status="error",
            message=f"Processing failed: {str(e)}",
            processed=0,
            failed=0,
            skipped=0
        )


@router.post("/granola/start")
async def start_granola_processor():
    """Start the Granola processor (runs every 5 minutes)."""
    try:
        from api.main import _granola_processor
        if _granola_processor:
            _granola_processor.start()
            return {"status": "started", "message": "Granola processor started (runs every 5 minutes)"}
        else:
            # Create new processor if not initialized
            from api.services.granola_processor import GranolaProcessor
            import api.main as main_module
            main_module._granola_processor = GranolaProcessor(settings.vault_path)
            main_module._granola_processor.start()
            return {"status": "started", "message": "Granola processor created and started"}
    except Exception as e:
        logger.error(f"Failed to start Granola processor: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/granola/stop")
async def stop_granola_processor():
    """Stop the Granola processor."""
    try:
        from api.main import _granola_processor
        if _granola_processor:
            _granola_processor.stop()
            return {"status": "stopped", "message": "Granola processor stopped"}
        return {"status": "not_running", "message": "Granola processor was not running"}
    except Exception as e:
        logger.error(f"Failed to stop Granola processor: {e}")
        return {"status": "error", "message": str(e)}


class ReclassifyRequest(BaseModel):
    """Request to reclassify files in a folder."""
    folder: str = "Work/ML/People/Hiring"


@router.post("/granola/reclassify", response_model=GranolaProcessResponse)
async def reclassify_granola_files(request: ReclassifyRequest) -> GranolaProcessResponse:
    """
    Reclassify Granola files that may have been incorrectly categorized.

    Scans the specified folder and moves any Granola files to their correct
    destination based on the updated classification rules.

    Default folder: Work/ML/People/Hiring (where files were incorrectly placed)
    """
    try:
        from api.services.granola_processor import GranolaProcessor
        from pathlib import Path

        processor = GranolaProcessor(settings.vault_path)
        folder_path = Path(settings.vault_path) / request.folder

        results = processor.reclassify_folder(str(folder_path))

        return GranolaProcessResponse(
            status="success",
            message=f"Reclassified {results['reclassified']} files from {request.folder}",
            processed=results["reclassified"],
            failed=results["failed"],
            skipped=results["skipped"],
            moves=results["moves"]
        )
    except Exception as e:
        logger.error(f"Reclassification failed: {e}")
        return GranolaProcessResponse(
            status="error",
            message=f"Reclassification failed: {str(e)}",
            processed=0,
            failed=0,
            skipped=0
        )


class DeduplicateResponse(BaseModel):
    """Response from deduplication operation."""
    status: str
    message: str
    duplicates_found: int
    files_deleted: int
    files_kept: int
    details: list[dict] = []


@router.post("/granola/deduplicate", response_model=DeduplicateResponse)
async def deduplicate_granola_files() -> DeduplicateResponse:
    """
    Find and remove all duplicate Granola files in the vault.

    For each set of duplicates (files with the same granola_id),
    keeps the file in the best location (based on classification)
    and deletes the rest.
    """
    try:
        from api.services.granola_processor import GranolaProcessor

        processor = GranolaProcessor(settings.vault_path)
        results = processor.deduplicate_all()

        return DeduplicateResponse(
            status="success",
            message=f"Found {results['duplicates_found']} duplicate sets, deleted {results['files_deleted']} files",
            duplicates_found=results["duplicates_found"],
            files_deleted=results["files_deleted"],
            files_kept=results["files_kept"],
            details=results["details"]
        )
    except Exception as e:
        logger.error(f"Deduplication failed: {e}")
        return DeduplicateResponse(
            status="error",
            message=f"Deduplication failed: {str(e)}",
            duplicates_found=0,
            files_deleted=0,
            files_kept=0
        )


# ============ Omi Processor Endpoints ============


class OmiStatus(BaseModel):
    """Status of the Omi processor."""
    status: str
    running: bool
    omi_events_path: str
    pending_files: int
    interval_seconds: int
    message: Optional[str] = None


class OmiProcessResponse(BaseModel):
    """Response from Omi processing operation."""
    status: str
    message: str
    processed: int
    failed: int
    skipped: int
    moves: list[dict] = []


@router.get("/omi/status", response_model=OmiStatus)
async def get_omi_status() -> OmiStatus:
    """
    Get status of the Omi events processor.

    Returns whether it's running and how many files are pending.
    """
    from pathlib import Path

    omi_events_path = Path(settings.vault_path) / "Omi" / "Events"
    pending_count = len(list(omi_events_path.glob("*.md"))) if omi_events_path.exists() else 0

    # Try to get the processor instance from main
    try:
        from api.main import _omi_processor
        running = _omi_processor.is_running if _omi_processor else False
        interval = _omi_processor.interval_seconds if _omi_processor else 300
    except Exception:
        running = False
        interval = 300

    return OmiStatus(
        status="running" if running else "stopped",
        running=running,
        omi_events_path=str(omi_events_path),
        pending_files=pending_count,
        interval_seconds=interval,
        message=f"{pending_count} files pending in Omi/Events" if pending_count > 0 else "No files pending"
    )


@router.post("/omi/process", response_model=OmiProcessResponse)
async def process_omi_backlog() -> OmiProcessResponse:
    """
    Process all pending files in the Omi/Events folder immediately.

    Classifies and moves files to appropriate destinations:
    - /Personal/Omi - general personal events
    - /Personal/Self-Improvement/Therapy and coaching/Omi - therapy sessions
    - /Work/ML/Meetings/Omi - work meetings
    """
    try:
        from api.services.omi_processor import OmiProcessor

        processor = OmiProcessor(settings.vault_path)
        results = processor.process_backlog()

        return OmiProcessResponse(
            status="success",
            message=f"Processed {results['processed']} files",
            processed=results["processed"],
            failed=results["failed"],
            skipped=results["skipped"],
            moves=results["moves"]
        )
    except Exception as e:
        logger.error(f"Omi processing failed: {e}")
        return OmiProcessResponse(
            status="error",
            message=f"Processing failed: {str(e)}",
            processed=0,
            failed=0,
            skipped=0
        )


@router.post("/omi/start")
async def start_omi_processor():
    """Start the Omi processor (runs every 5 minutes)."""
    try:
        from api.main import _omi_processor
        if _omi_processor:
            _omi_processor.start()
            return {"status": "started", "message": "Omi processor started (runs every 5 minutes)"}
        else:
            # Create new processor if not initialized
            from api.services.omi_processor import OmiProcessor
            import api.main as main_module
            main_module._omi_processor = OmiProcessor(settings.vault_path)
            main_module._omi_processor.start()
            return {"status": "started", "message": "Omi processor created and started"}
    except Exception as e:
        logger.error(f"Failed to start Omi processor: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/omi/stop")
async def stop_omi_processor():
    """Stop the Omi processor."""
    try:
        from api.main import _omi_processor
        if _omi_processor:
            _omi_processor.stop()
            return {"status": "stopped", "message": "Omi processor stopped"}
        return {"status": "not_running", "message": "Omi processor was not running"}
    except Exception as e:
        logger.error(f"Failed to stop Omi processor: {e}")
        return {"status": "error", "message": str(e)}


class OmiReclassifyRequest(BaseModel):
    """Request to reclassify Omi files in a folder."""
    folder: str = "Personal/Omi"


@router.post("/omi/reclassify", response_model=OmiProcessResponse)
async def reclassify_omi_files(request: OmiReclassifyRequest) -> OmiProcessResponse:
    """
    Reclassify Omi files that may have been incorrectly categorized.

    Scans the specified folder and moves any Omi files to their correct
    destination based on classification rules.
    """
    try:
        from api.services.omi_processor import OmiProcessor
        from pathlib import Path

        processor = OmiProcessor(settings.vault_path)
        folder_path = Path(settings.vault_path) / request.folder

        results = processor.reclassify_folder(str(folder_path))

        return OmiProcessResponse(
            status="success",
            message=f"Reclassified {results['reclassified']} files from {request.folder}",
            processed=results["reclassified"],
            failed=results["failed"],
            skipped=results["skipped"],
            moves=results["moves"]
        )
    except Exception as e:
        logger.error(f"Omi reclassification failed: {e}")
        return OmiProcessResponse(
            status="error",
            message=f"Reclassification failed: {str(e)}",
            processed=0,
            failed=0,
            skipped=0
        )


class OmiDeduplicateResponse(BaseModel):
    """Response from Omi deduplication operation."""
    status: str
    message: str
    duplicates_found: int
    files_deleted: int
    files_kept: int
    details: list[dict] = []


@router.post("/omi/deduplicate", response_model=OmiDeduplicateResponse)
async def deduplicate_omi_files() -> OmiDeduplicateResponse:
    """
    Find and remove all duplicate Omi files in the vault.

    For each set of duplicates (files with the same omi_id),
    keeps the file in the best location (based on classification)
    and deletes the rest.
    """
    try:
        from api.services.omi_processor import OmiProcessor

        processor = OmiProcessor(settings.vault_path)
        results = processor.deduplicate_all()

        return OmiDeduplicateResponse(
            status="success",
            message=f"Found {results['duplicates_found']} duplicate sets, deleted {results['files_deleted']} files",
            duplicates_found=results["duplicates_found"],
            files_deleted=results["files_deleted"],
            files_kept=results["files_kept"],
            details=results["details"]
        )
    except Exception as e:
        logger.error(f"Omi deduplication failed: {e}")
        return OmiDeduplicateResponse(
            status="error",
            message=f"Deduplication failed: {str(e)}",
            duplicates_found=0,
            files_deleted=0,
            files_kept=0
        )


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
    timezone: str = "America/New_York"
):
    """
    Start the calendar sync scheduler.

    Args:
        interval_hours: Hours between syncs (if not using time schedule)
        use_time_schedule: Use time-of-day schedule (default: True, syncs at 8 AM, noon, 3 PM)
        timezone: Timezone for time schedule (default: America/New_York)
    """
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