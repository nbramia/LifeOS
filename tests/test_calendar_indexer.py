"""
Tests for the Calendar Indexer (P3.2).

Tests calendar event indexing and scheduling functionality.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestCalendarIndexer:
    """Tests for the CalendarIndexer service."""

    @pytest.fixture
    def mock_vector_store(self):
        """Mock the vector store."""
        with patch("api.services.calendar_indexer.get_vector_store") as mock:
            store = mock.return_value
            store.add_document = MagicMock()
            yield store

    @pytest.fixture
    def mock_calendar_service(self):
        """Mock the calendar service."""
        with patch("api.services.calendar_indexer.CalendarService") as mock:
            service = mock.return_value
            service.get_events_in_range.return_value = []
            yield service

    @pytest.fixture
    def indexer(self, mock_vector_store):
        """Create calendar indexer with mocked dependencies."""
        from api.services.calendar_indexer import CalendarIndexer
        return CalendarIndexer()

    def test_indexer_initialization(self, indexer):
        """Indexer should initialize with default values."""
        assert indexer._last_sync is None
        assert indexer._scheduler_thread is None

    def test_index_empty_events_returns_zero(self, indexer):
        """Should return 0 when no events provided."""
        result = indexer.index_events([])
        assert result == 0

    def test_index_single_event(self, indexer, mock_vector_store):
        """Should index a single calendar event."""
        from api.services.calendar import CalendarEvent

        event = CalendarEvent(
            event_id="evt-123",
            title="Team Meeting",
            start_time=datetime(2025, 1, 15, 10, 0),
            end_time=datetime(2025, 1, 15, 11, 0),
            source_account="personal",
            attendees=["Alice", "Bob"],
        )

        result = indexer.index_events([event])

        assert result == 1
        mock_vector_store.add_document.assert_called_once()

    def test_index_event_includes_metadata(self, indexer, mock_vector_store):
        """Indexed event should include proper metadata."""
        from api.services.calendar import CalendarEvent

        event = CalendarEvent(
            event_id="evt-456",
            title="Budget Review",
            start_time=datetime(2025, 1, 20, 14, 0),
            end_time=datetime(2025, 1, 20, 15, 0),
            source_account="work",
            attendees=["CFO", "Finance Team"],
            location="Conference Room A",
        )

        indexer.index_events([event])

        call_args = mock_vector_store.add_document.call_args
        metadata = call_args.kwargs["metadata"]

        assert metadata["note_type"] == "calendar_event"
        assert metadata["file_name"] == "Calendar: Budget Review"
        assert "work" in metadata["tags"]

    def test_sync_fetches_from_both_calendars(self, indexer, mock_calendar_service, mock_vector_store):
        """Sync should attempt to fetch from both personal and work calendars."""
        with patch("api.services.calendar_indexer.CalendarService") as mock_cs:
            mock_cs.return_value.get_events_in_range.return_value = []

            result = indexer.sync(days_past=7, days_future=7)

            assert result["status"] in ["success", "partial"]
            assert "events_indexed" in result
            assert "elapsed_seconds" in result

    def test_sync_handles_calendar_error(self, indexer, mock_vector_store):
        """Sync should handle calendar service errors gracefully."""
        with patch("api.services.calendar_indexer.CalendarService") as mock_cs:
            mock_cs.return_value.get_events_in_range.side_effect = Exception("Auth failed")

            result = indexer.sync()

            # Should return partial status with errors
            assert len(result["errors"]) > 0

    def test_get_status_when_not_running(self, indexer):
        """Status should show scheduler not running initially."""
        status = indexer.get_status()

        assert status["running"] is False
        assert status["last_sync"] is None


class TestCalendarIndexerScheduler:
    """Tests for the scheduler functionality."""

    @pytest.fixture
    def indexer(self):
        """Create calendar indexer."""
        with patch("api.services.calendar_indexer.get_vector_store"):
            from api.services.calendar_indexer import CalendarIndexer
            return CalendarIndexer()

    def test_start_scheduler_creates_thread(self, indexer):
        """Starting scheduler should create a daemon thread."""
        with patch.object(indexer, "sync"):
            indexer.start_scheduler(interval_hours=1.0)

            assert indexer._scheduler_thread is not None
            assert indexer._scheduler_thread.daemon is True

            # Clean up
            indexer.stop_scheduler()

    def test_stop_scheduler_stops_thread(self, indexer):
        """Stopping scheduler should stop the thread."""
        with patch.object(indexer, "sync"):
            indexer.start_scheduler(interval_hours=1.0)
            indexer.stop_scheduler()

            # Thread should be None after stop
            assert indexer._scheduler_thread is None

    def test_scheduler_status_while_running(self, indexer):
        """Status should show running when scheduler is active."""
        with patch.object(indexer, "sync"):
            indexer.start_scheduler(interval_hours=1.0)

            status = indexer.get_status()
            assert status["running"] is True

            indexer.stop_scheduler()


class TestCalendarAdminEndpoints:
    """Tests for the admin calendar endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_indexer(self):
        """Mock the calendar indexer."""
        with patch("api.services.calendar_indexer.get_calendar_indexer") as mock:
            indexer = mock.return_value
            indexer.get_status.return_value = {
                "running": True,
                "last_sync": "2025-01-15T10:00:00"
            }
            indexer.sync.return_value = {
                "status": "success",
                "events_indexed": 50,
                "errors": [],
                "elapsed_seconds": 2.5,
                "last_sync": "2025-01-15T10:00:00"
            }
            yield indexer

    def test_get_calendar_status(self, client, mock_indexer):
        """Should return calendar indexer status."""
        response = client.get("/api/admin/calendar/status")

        assert response.status_code == 200
        data = response.json()
        assert data["scheduler_running"] is True

    def test_trigger_calendar_sync(self, client, mock_indexer):
        """Should trigger immediate calendar sync."""
        response = client.post("/api/admin/calendar/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["events_indexed"] == 50

    def test_start_calendar_scheduler(self, client, mock_indexer):
        """Should start the calendar scheduler with time-based schedule by default."""
        response = client.post("/api/admin/calendar/start")

        assert response.status_code == 200
        # Default uses time-based scheduler at 8 AM, noon, 3 PM Eastern
        mock_indexer.start_time_scheduler.assert_called_once()

    def test_stop_calendar_scheduler(self, client, mock_indexer):
        """Should stop the calendar scheduler."""
        response = client.post("/api/admin/calendar/stop")

        assert response.status_code == 200
        mock_indexer.stop_scheduler.assert_called_once()
