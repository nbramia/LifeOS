"""
Tests for Google Calendar Integration.
P3.2 Acceptance Criteria:
- Can fetch upcoming events from Google Calendar
- Can fetch past events within date range
- Events indexed to ChromaDB with correct metadata
- Daily sync updates index (cron or scheduler)
- Search by attendee name returns correct events
- Search by keyword in title/description works
- "What's on my calendar tomorrow" returns correct events
- Event times displayed in local timezone
- All-day events handled correctly
"""
import pytest

# All tests in this file use mocks (unit tests)
pytestmark = pytest.mark.unit
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

from api.services.calendar import (
    CalendarService,
    CalendarEvent,
    format_event_time,
    parse_attendees,
)
from api.services.google_auth import GoogleAccount


class TestCalendarEvent:
    """Test CalendarEvent dataclass."""

    def test_creates_event_with_required_fields(self):
        """Should create event with all required fields."""
        event = CalendarEvent(
            event_id="abc123",
            title="Team Standup",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
            source_account="personal"
        )
        assert event.event_id == "abc123"
        assert event.title == "Team Standup"

    def test_event_to_dict(self):
        """Should convert event to dict for indexing."""
        event = CalendarEvent(
            event_id="abc123",
            title="Team Standup",
            start_time=datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 7, 11, 0, tzinfo=timezone.utc),
            attendees=["alice@example.com", "bob@example.com"],
            description="Daily standup meeting",
            location="Conference Room A",
            source_account="personal"
        )
        data = event.to_dict()
        assert data["event_id"] == "abc123"
        assert data["source"] == "google_calendar"
        assert "alice@example.com" in data["attendees"]

    def test_event_is_all_day(self):
        """Should detect all-day events."""
        all_day = CalendarEvent(
            event_id="1",
            title="Vacation",
            start_time=datetime(2026, 1, 7, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc),
            is_all_day=True,
            source_account="personal"
        )
        assert all_day.is_all_day is True

        timed = CalendarEvent(
            event_id="2",
            title="Meeting",
            start_time=datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 7, 11, 0, tzinfo=timezone.utc),
            source_account="personal"
        )
        assert timed.is_all_day is False


class TestCalendarService:
    """Test CalendarService."""

    @pytest.fixture
    def mock_auth_service(self):
        """Create mock auth service."""
        mock = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock.get_credentials.return_value = mock_creds
        return mock

    @pytest.fixture
    def calendar_service(self, mock_auth_service):
        """Create calendar service with mock auth."""
        with patch('api.services.calendar.get_google_auth', return_value=mock_auth_service):
            with patch('api.services.calendar.build') as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                service = CalendarService(account_type=GoogleAccount.PERSONAL)
                service._service = mock_service
                return service

    def test_fetches_upcoming_events(self, calendar_service):
        """Should fetch upcoming events."""
        mock_events = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Team Standup",
                    "start": {"dateTime": "2026-01-07T10:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T11:00:00-08:00"},
                    "attendees": [{"email": "alice@example.com"}],
                }
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        events = calendar_service.get_upcoming_events(days=7)

        assert len(events) >= 1
        assert events[0].title == "Team Standup"

    def test_fetches_past_events_in_range(self, calendar_service):
        """Should fetch past events within date range."""
        mock_events = {
            "items": [
                {
                    "id": "past1",
                    "summary": "Past Meeting",
                    "start": {"dateTime": "2026-01-01T10:00:00-08:00"},
                    "end": {"dateTime": "2026-01-01T11:00:00-08:00"},
                }
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end_date = datetime(2026, 1, 5, tzinfo=timezone.utc)
        events = calendar_service.get_events_in_range(start_date, end_date)

        assert len(events) >= 1

    def test_handles_all_day_events(self, calendar_service):
        """Should handle all-day events correctly."""
        mock_events = {
            "items": [
                {
                    "id": "allday1",
                    "summary": "Company Holiday",
                    "start": {"date": "2026-01-07"},
                    "end": {"date": "2026-01-08"},
                }
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        events = calendar_service.get_upcoming_events(days=7)

        assert len(events) >= 1
        assert events[0].is_all_day is True

    def test_extracts_attendees(self, calendar_service):
        """Should extract attendee names and emails."""
        mock_events = {
            "items": [
                {
                    "id": "event1",
                    "summary": "1:1 with Alice",
                    "start": {"dateTime": "2026-01-07T14:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T14:30:00-08:00"},
                    "attendees": [
                        {"email": "alice@example.com", "displayName": "Alice Smith"},
                        {"email": "bob@example.com"},
                    ],
                }
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        events = calendar_service.get_upcoming_events(days=7)

        assert "Alice Smith" in events[0].attendees or "alice@example.com" in events[0].attendees

    def test_searches_by_attendee(self, calendar_service):
        """Should filter events by attendee."""
        mock_events = {
            "items": [
                {
                    "id": "1",
                    "summary": "Meeting with Alice",
                    "start": {"dateTime": "2026-01-07T10:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T11:00:00-08:00"},
                    "attendees": [{"email": "alice@example.com"}],
                },
                {
                    "id": "2",
                    "summary": "Meeting with Bob",
                    "start": {"dateTime": "2026-01-07T14:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T15:00:00-08:00"},
                    "attendees": [{"email": "bob@example.com"}],
                },
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        events = calendar_service.search_events(attendee="alice")

        # Should only return events with Alice
        assert all("alice" in str(e.attendees).lower() or "alice" in e.title.lower() for e in events)

    def test_searches_by_keyword(self, calendar_service):
        """Should search events by keyword in title/description."""
        mock_events = {
            "items": [
                {
                    "id": "1",
                    "summary": "Budget Review",
                    "description": "Review Q1 budget",
                    "start": {"dateTime": "2026-01-07T10:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T11:00:00-08:00"},
                },
                {
                    "id": "2",
                    "summary": "Team Standup",
                    "start": {"dateTime": "2026-01-07T14:00:00-08:00"},
                    "end": {"dateTime": "2026-01-07T15:00:00-08:00"},
                },
            ]
        }
        calendar_service._service.events().list().execute.return_value = mock_events

        events = calendar_service.search_events(query="budget")

        assert len(events) >= 1
        assert any("budget" in e.title.lower() or (e.description and "budget" in e.description.lower()) for e in events)


class TestHelperFunctions:
    """Test helper functions."""

    def test_format_event_time_timed(self):
        """Should format timed events with time."""
        dt = datetime(2026, 1, 7, 10, 30, tzinfo=timezone.utc)
        formatted = format_event_time(dt, is_all_day=False)
        # Time is converted to local timezone, so just verify time is present
        assert "AM" in formatted or "PM" in formatted
        assert "2026" in formatted

    def test_format_event_time_all_day(self):
        """Should format all-day events without time."""
        dt = datetime(2026, 1, 7, 0, 0, tzinfo=timezone.utc)
        formatted = format_event_time(dt, is_all_day=True)
        # All-day should show just the date
        assert "2026" in formatted or "Jan" in formatted or "7" in formatted

    def test_parse_attendees_with_names(self):
        """Should parse attendees with display names."""
        raw = [
            {"email": "alice@example.com", "displayName": "Alice Smith"},
            {"email": "bob@example.com"},
        ]
        attendees = parse_attendees(raw)
        assert "Alice Smith" in attendees
        assert "bob@example.com" in attendees

    def test_parse_attendees_empty(self):
        """Should handle empty attendees list."""
        attendees = parse_attendees([])
        assert attendees == []

        attendees = parse_attendees(None)
        assert attendees == []
