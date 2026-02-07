"""
Calendar Event Indexer for LifeOS (P3.2).

Indexes Google Calendar events into ChromaDB for semantic search.
Runs daily sync to keep events up to date.

Features:
- Indexes events from the past 30 days and next 30 days
- Updates incrementally on schedule
- Stores event metadata for filtering
- Supports both personal and work calendars
- Supports time-of-day scheduling (e.g., 8 AM, noon, 3 PM Eastern)
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from api.services.calendar import CalendarService, CalendarEvent
from api.services.google_auth import GoogleAccount
from api.services.vectorstore import VectorStore, get_vector_store

logger = logging.getLogger(__name__)

# How far back and forward to index
DAYS_PAST = 30
DAYS_FUTURE = 30

# Collection name for calendar events
CALENDAR_COLLECTION = "lifeos_calendar"



class CalendarIndexer:
    """
    Indexes calendar events into ChromaDB for semantic search.

    Events are stored with metadata including:
    - event_id: Unique identifier
    - title: Event title
    - start_time: ISO format start time
    - end_time: ISO format end time
    - attendees: Comma-separated list
    - source_account: personal or work
    - note_type: "calendar_event" for filtering
    """

    def __init__(self):
        """Initialize the calendar indexer."""
        self._vector_store: Optional[VectorStore] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_sync: Optional[datetime] = None

    @property
    def vector_store(self) -> VectorStore:
        """Lazy load vector store."""
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    def index_events(self, events: list[CalendarEvent]) -> int:
        """
        Index calendar events into ChromaDB.

        Args:
            events: List of CalendarEvent objects

        Returns:
            Number of events indexed
        """
        if not events:
            return 0

        indexed = 0
        for event in events:
            try:
                # Convert to searchable text
                content = event.to_text()

                # Build metadata
                metadata = {
                    "file_path": f"calendar/{event.event_id}",
                    "file_name": f"Calendar: {event.title}",
                    "modified_date": event.start_time.strftime("%Y-%m-%d"),
                    "note_type": "calendar_event",
                    "people": event.attendees,
                    "tags": ["calendar", event.source_account],
                }

                # Add additional metadata for calendar-specific filtering
                # These will be stored in the chunk metadata
                extra_meta = {
                    "event_id": event.event_id,
                    "title": event.title,
                    "start_time": event.start_time.isoformat(),
                    "end_time": event.end_time.isoformat(),
                    "source_account": event.source_account,
                }
                if event.location:
                    extra_meta["location"] = event.location

                # Create a single chunk for this event
                chunks = [{
                    "content": content,
                    "chunk_index": 0,
                    **extra_meta,  # Include extra metadata in chunk
                }]

                # Add to vector store using existing interface
                self.vector_store.add_document(
                    chunks=chunks,
                    metadata=metadata
                )
                indexed += 1

            except Exception as e:
                logger.error(f"Failed to index event {event.event_id}: {e}")

        return indexed

    def sync(self, days_past: int = DAYS_PAST, days_future: int = DAYS_FUTURE) -> dict:
        """
        Sync calendar events to ChromaDB.

        Fetches events from the specified date range and indexes them.

        Args:
            days_past: Number of days in the past to fetch
            days_future: Number of days in the future to fetch

        Returns:
            Dict with sync statistics
        """
        logger.info(f"Starting calendar sync: {days_past} days past, {days_future} days future")

        start_time = datetime.now()
        total_indexed = 0
        errors = []

        # Calculate date range
        now = datetime.now()
        start_date = now - timedelta(days=days_past)
        end_date = now + timedelta(days=days_future)

        # Try personal calendar
        try:
            personal_calendar = CalendarService(account_type=GoogleAccount.PERSONAL)
            personal_events = personal_calendar.get_events_in_range(start_date, end_date)
            indexed = self.index_events(personal_events)
            total_indexed += indexed
            logger.info(f"Indexed {indexed} personal calendar events")
        except Exception as e:
            error_msg = f"Personal calendar sync failed: {e}"
            logger.warning(error_msg)
            errors.append(error_msg)

        # Try work calendar
        try:
            work_calendar = CalendarService(account_type=GoogleAccount.WORK)
            work_events = work_calendar.get_events_in_range(start_date, end_date)
            indexed = self.index_events(work_events)
            total_indexed += indexed
            logger.info(f"Indexed {indexed} work calendar events")
        except Exception as e:
            error_msg = f"Work calendar sync failed: {e}"
            logger.warning(error_msg)
            errors.append(error_msg)

        self._last_sync = datetime.now()
        elapsed = (datetime.now() - start_time).total_seconds()

        result = {
            "status": "success" if not errors else "partial",
            "events_indexed": total_indexed,
            "errors": errors,
            "elapsed_seconds": elapsed,
            "last_sync": self._last_sync.isoformat(),
        }

        logger.info(f"Calendar sync complete: {total_indexed} events in {elapsed:.1f}s")
        return result

    def _scheduler_loop(self, interval_hours: float):
        """
        Background scheduler loop.

        Args:
            interval_hours: Hours between syncs
        """
        interval_seconds = interval_hours * 3600

        # Initial sync on startup
        try:
            self.sync()
        except Exception as e:
            logger.error(f"Initial calendar sync failed: {e}")

        while not self._stop_event.is_set():
            # Wait for interval or stop signal
            self._stop_event.wait(interval_seconds)

            if not self._stop_event.is_set():
                try:
                    self.sync()
                except Exception as e:
                    logger.error(f"Scheduled calendar sync failed: {e}")

    def start_scheduler(self, interval_hours: float = 24.0):
        """
        Start the background sync scheduler.

        Args:
            interval_hours: Hours between syncs (default: 24)
        """
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            logger.warning("Scheduler already running")
            return

        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            args=(interval_hours,),
            daemon=True,
            name="CalendarIndexerScheduler"
        )
        self._scheduler_thread.start()
        logger.info(f"Calendar sync scheduler started (interval: {interval_hours}h)")

    def stop_scheduler(self):
        """Stop the background sync scheduler."""
        if self._scheduler_thread:
            self._stop_event.set()
            self._scheduler_thread.join(timeout=5)
            self._scheduler_thread = None
            logger.info("Calendar sync scheduler stopped")

    def _get_next_scheduled_time(self, schedule_times: list[tuple[int, int]], tz: ZoneInfo) -> datetime:
        """
        Calculate the next scheduled sync time.

        Args:
            schedule_times: List of (hour, minute) tuples in the target timezone
            tz: Timezone for schedule times

        Returns:
            Next scheduled time as a datetime in UTC
        """
        now = datetime.now(tz)
        today = now.date()

        # Find the next scheduled time
        for hour, minute in sorted(schedule_times):
            scheduled = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)
            if scheduled > now:
                return scheduled

        # All times today have passed, schedule for tomorrow's first time
        tomorrow = today + timedelta(days=1)
        first_hour, first_minute = min(schedule_times)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, first_hour, first_minute, tzinfo=tz)

    def _time_scheduler_loop(self, schedule_times: list[tuple[int, int]], timezone: str, skip_initial_sync: bool = False):
        """
        Background scheduler loop that runs at specific times of day.

        Args:
            schedule_times: List of (hour, minute) tuples
            timezone: IANA timezone string (e.g., "America/New_York")
            skip_initial_sync: If True, skip the initial sync on startup
        """
        tz = ZoneInfo(timezone)

        # Initial sync on startup (unless skipped)
        if not skip_initial_sync:
            try:
                self.sync()
            except Exception as e:
                logger.error(f"Initial calendar sync failed: {e}")

        while not self._stop_event.is_set():
            # Calculate seconds until next scheduled time
            next_time = self._get_next_scheduled_time(schedule_times, tz)
            now = datetime.now(tz)
            wait_seconds = (next_time - now).total_seconds()

            logger.info(f"Next calendar sync scheduled for {next_time.strftime('%Y-%m-%d %H:%M %Z')} ({wait_seconds/3600:.1f}h from now)")

            # Wait for scheduled time or stop signal
            if self._stop_event.wait(wait_seconds):
                break  # Stop event was set

            if not self._stop_event.is_set():
                try:
                    self.sync()
                except Exception as e:
                    logger.error(f"Scheduled calendar sync failed: {e}")
                    # Record failure for nightly batch report
                    try:
                        from api.services.notifications import record_failure
                        record_failure("Calendar sync", str(e))
                    except Exception as notify_err:
                        logger.error(f"Failed to record calendar failure: {notify_err}")

    def start_time_scheduler(
        self,
        schedule_times: list[tuple[int, int]] = [(8, 0), (12, 0), (15, 0)],
        timezone: str = "America/New_York",
        skip_initial_sync: bool = True
    ):
        """
        Start the background sync scheduler at specific times of day.

        Args:
            schedule_times: List of (hour, minute) tuples in 24-hour format
                           Default: 8:00 AM, 12:00 PM, 3:00 PM
            timezone: IANA timezone string (default: America/New_York for Eastern)
            skip_initial_sync: Skip initial sync on startup (default: True to avoid blocking)
        """
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            logger.warning("Scheduler already running")
            return

        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._time_scheduler_loop,
            args=(schedule_times, timezone, skip_initial_sync),
            daemon=True,
            name="CalendarIndexerScheduler"
        )
        self._scheduler_thread.start()

        times_str = ", ".join(f"{h:02d}:{m:02d}" for h, m in schedule_times)
        logger.info(f"Calendar sync scheduler started (times: {times_str} {timezone})")

    def get_status(self) -> dict:
        """Get scheduler status."""
        return {
            "running": self._scheduler_thread is not None and self._scheduler_thread.is_alive(),
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
        }


# Singleton instance
_calendar_indexer: Optional[CalendarIndexer] = None


def get_calendar_indexer() -> CalendarIndexer:
    """Get or create CalendarIndexer singleton."""
    global _calendar_indexer
    if _calendar_indexer is None:
        _calendar_indexer = CalendarIndexer()
    return _calendar_indexer
