"""
Tests for Calendar API endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from api.main import app
from api.services.calendar import CalendarEvent

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow


client = TestClient(app)


@pytest.fixture
def mock_calendar_service():
    """Create mock calendar service."""
    mock = MagicMock()
    mock.get_upcoming_events.return_value = [
        CalendarEvent(
            event_id="1",
            title="Team Standup",
            start_time=datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 7, 11, 0, tzinfo=timezone.utc),
            attendees=["alice@example.com"],
            source_account="personal",
        )
    ]
    mock.search_events.return_value = [
        CalendarEvent(
            event_id="2",
            title="Budget Review",
            start_time=datetime(2026, 1, 8, 14, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 8, 15, 0, tzinfo=timezone.utc),
            description="Q1 budget review",
            source_account="personal",
        )
    ]
    return mock


class TestCalendarUpcomingEndpoint:
    """Test /api/calendar/upcoming endpoint."""

    def test_upcoming_endpoint_exists(self, mock_calendar_service):
        """Should have upcoming events endpoint."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/upcoming")
            assert response.status_code == 200

    def test_returns_events_list(self, mock_calendar_service):
        """Should return list of events."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/upcoming")
            data = response.json()
            assert "events" in data
            assert "count" in data
            assert isinstance(data["events"], list)

    def test_event_has_required_fields(self, mock_calendar_service):
        """Should include all required event fields."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/upcoming")
            data = response.json()
            if data["events"]:
                event = data["events"][0]
                assert "event_id" in event
                assert "title" in event
                assert "start_time" in event
                assert "end_time" in event
                assert "start_formatted" in event

    def test_accepts_days_parameter(self, mock_calendar_service):
        """Should accept days parameter."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/upcoming?days=14")
            assert response.status_code == 200
            mock_calendar_service.get_upcoming_events.assert_called_once()

    def test_accepts_account_parameter(self, mock_calendar_service):
        """Should accept account parameter."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/upcoming?account=work")
            assert response.status_code == 200


class TestCalendarSearchEndpoint:
    """Test /api/calendar/search endpoint."""

    def test_search_endpoint_exists(self, mock_calendar_service):
        """Should have search endpoint."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/search?q=budget")
            assert response.status_code == 200

    def test_search_requires_query_or_attendee(self, mock_calendar_service):
        """Should require at least query or attendee."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/search")
            assert response.status_code == 400

    def test_search_by_query(self, mock_calendar_service):
        """Should search by query."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/search?q=budget")
            data = response.json()
            assert "events" in data
            assert data["query"] == "budget"

    def test_search_by_attendee(self, mock_calendar_service):
        """Should search by attendee."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/search?attendee=alice")
            data = response.json()
            assert "events" in data
            assert data["attendee"] == "alice"

    def test_search_returns_count(self, mock_calendar_service):
        """Should return result count."""
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.get("/api/calendar/search?q=review")
            data = response.json()
            assert "count" in data
            assert isinstance(data["count"], int)


class TestCalendarWriteFailures:
    """#609: a failed create/update/delete must be a non-2xx, never a 200
    that merely omits the fields a successful write would have."""

    def test_create_event_failure_returns_500(self, mock_calendar_service):
        mock_calendar_service.create_event.side_effect = Exception("Google API error")
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.post("/api/calendar/events", json={
                "title": "Standup",
                "start_time": "2026-01-07T10:00:00Z",
                "end_time": "2026-01-07T10:30:00Z",
            })
            assert response.status_code == 500

    def test_update_event_failure_returns_500(self, mock_calendar_service):
        mock_calendar_service.update_event.side_effect = Exception("Google API error")
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.put("/api/calendar/events/evt-1", json={"title": "New title"})
            assert response.status_code == 500

    def test_delete_event_failure_returns_500(self, mock_calendar_service):
        mock_calendar_service.delete_event.side_effect = Exception("Google API error")
        with patch('api.routes.calendar.get_calendar_service', return_value=mock_calendar_service):
            response = client.delete("/api/calendar/events/evt-1")
            assert response.status_code == 500
