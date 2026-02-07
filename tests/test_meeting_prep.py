"""
Tests for Meeting Prep service and API endpoint.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from api.services.meeting_prep import (
    get_meeting_prep,
    _extract_time,
    _truncate_description,
    _normalize_title_for_search,
    _extract_person_names,
    _find_people_notes,
    _find_past_meetings,
    RelatedNote,
)
from api.services.calendar import CalendarEvent


class TestHelperFunctions:
    """Test helper functions in meeting_prep module."""

    def test_extract_time_formats_correctly(self):
        """Should format time as 12-hour format."""
        dt = datetime(2026, 1, 28, 14, 30, tzinfo=timezone.utc)
        # Will be converted to Pacific time
        result = _extract_time(dt)
        assert ":" in result
        assert "AM" in result or "PM" in result

    def test_truncate_description_short(self):
        """Should not truncate short descriptions."""
        desc = "Short description"
        assert _truncate_description(desc) == desc

    def test_truncate_description_long(self):
        """Should truncate long descriptions with ellipsis."""
        desc = "A" * 300
        result = _truncate_description(desc, max_length=100)
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")

    def test_truncate_description_none(self):
        """Should handle None description."""
        assert _truncate_description(None) is None

    def test_normalize_title_removes_date(self):
        """Should remove date patterns from title."""
        assert "2026-01-28" not in _normalize_title_for_search("1:1 with Alex - 2026-01-28")
        assert "2026/01/28" not in _normalize_title_for_search("Meeting 2026/01/28")

    def test_normalize_title_removes_temporal_words(self):
        """Should remove weekly/biweekly/etc."""
        result = _normalize_title_for_search("Weekly Team Standup")
        assert "weekly" not in result.lower()

    def test_normalize_title_converts_hyphenated_names(self):
        """Should convert John-Bob to John Bob."""
        result = _normalize_title_for_search("John-Bob")
        assert result == "John Bob"

    def test_extract_person_names_from_attendees(self):
        """Should extract names from attendee list."""
        attendees = ["john.smith@example.com", "Jane Doe"]
        names = _extract_person_names(attendees, "Meeting")
        assert "John Smith" in names
        assert "Jane Doe" in names

    def test_extract_person_names_from_title(self):
        """Should extract names from 'with X' pattern."""
        attendees = []
        names = _extract_person_names(attendees, "1:1 with Alex")
        assert "Alex" in names

    def test_extract_person_names_deduplicates(self):
        """Should deduplicate names."""
        attendees = ["alex@example.com", "Alex"]
        names = _extract_person_names(attendees, "Meeting with Alex")
        # Should have Alex only once
        alex_count = sum(1 for n in names if "alex" in n.lower())
        assert alex_count == 1


class TestFindPeopleNotes:
    """Test _find_people_notes function."""

    def test_finds_people_notes(self):
        """Should find People notes for given names."""
        mock_search = MagicMock()
        mock_search.search.return_value = [
            {
                "file_path": "/vault/Work/ML/People/Alex Rechtman.md",
                "file_name": "Alex Rechtman.md",
                "content": "Alex is the director...",
            }
        ]

        notes = _find_people_notes(["Alex"], mock_search)

        assert len(notes) == 1
        assert notes[0].title == "Alex Rechtman"
        assert notes[0].relevance == "attendee"
        mock_search.search.assert_called()

    def test_skips_non_people_notes(self):
        """Should skip results not in People folder."""
        mock_search = MagicMock()
        mock_search.search.return_value = [
            {
                "file_path": "/vault/Work/ML/Meetings/meeting.md",
                "file_name": "meeting.md",
                "content": "Alex attended...",
            }
        ]

        notes = _find_people_notes(["Alex"], mock_search)
        assert len(notes) == 0


class TestFindPastMeetings:
    """Test _find_past_meetings function."""

    def test_finds_past_meetings(self):
        """Should find past meeting notes."""
        mock_search = MagicMock()
        mock_search.search.return_value = [
            {
                "file_path": "/vault/Work/Meetings/1-1s/Alex-John 20260121.md",
                "file_name": "Alex-John 20260121.md",
                "content": "Discussed Q1 goals...",
                "metadata": {"date": "2026-01-21"},
            }
        ]

        event_date = datetime(2026, 1, 28, 10, 0, tzinfo=timezone.utc)
        notes = _find_past_meetings("1:1 with Alex", event_date, mock_search)

        assert len(notes) == 1
        assert notes[0].relevance == "past_meeting"
        assert notes[0].date == "2026-01-21"

    def test_skips_future_meetings(self):
        """Should skip meetings on or after the event date."""
        mock_search = MagicMock()
        mock_search.search.return_value = [
            {
                "file_path": "/vault/Work/ML/Meetings/meeting.md",
                "file_name": "meeting 20260130.md",
                "content": "Future meeting...",
                "metadata": {"date": "2026-01-30"},
            }
        ]

        event_date = datetime(2026, 1, 28, 10, 0, tzinfo=timezone.utc)
        notes = _find_past_meetings("Team Meeting", event_date, mock_search)
        assert len(notes) == 0


class TestGetMeetingPrep:
    """Test main get_meeting_prep function."""

    @pytest.fixture
    def mock_calendar_events(self):
        """Create mock calendar events."""
        return [
            CalendarEvent(
                event_id="1",
                title="1:1 with Alex",
                start_time=datetime(2026, 1, 28, 10, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 28, 10, 30, tzinfo=timezone.utc),
                attendees=["alex@example.com"],
                description="Weekly sync",
                source_account="work",
                html_link="https://calendar.google.com/event?eid=123",
            ),
            CalendarEvent(
                event_id="2",
                title="Team Standup",
                start_time=datetime(2026, 1, 28, 14, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 28, 14, 15, tzinfo=timezone.utc),
                attendees=["alice@example.com", "bob@example.com"],
                source_account="work",
            ),
        ]

    @pytest.fixture
    def mock_search_results(self):
        """Create mock search results."""
        return [
            {
                "file_path": "/vault/Work/ML/People/Alex Rechtman.md",
                "file_name": "Alex Rechtman.md",
                "content": "Director of Engineering",
                "metadata": {},
            }
        ]

    def test_returns_meeting_prep_response(self, mock_calendar_events, mock_search_results):
        """Should return MeetingPrepResponse with meetings."""
        from api.services.google_auth import GoogleAccount

        def mock_get_service(account):
            mock = MagicMock()
            # Only return events for work calendar to avoid duplicates
            if account == GoogleAccount.WORK:
                mock.get_events_in_range.return_value = mock_calendar_events
            else:
                mock.get_events_in_range.return_value = []
            return mock

        mock_search = MagicMock()
        mock_search.search.return_value = mock_search_results

        with patch('api.services.meeting_prep.get_calendar_service', side_effect=mock_get_service):
            with patch('api.services.meeting_prep.get_hybrid_search', return_value=mock_search):
                result = get_meeting_prep("2026-01-28")

        assert result.date == "2026-01-28"
        assert result.count == 2
        assert len(result.meetings) == 2

    def test_meeting_has_required_fields(self, mock_calendar_events, mock_search_results):
        """Should include all required fields in meeting prep."""
        from api.services.google_auth import GoogleAccount

        def mock_get_service(account):
            mock = MagicMock()
            if account == GoogleAccount.WORK:
                mock.get_events_in_range.return_value = mock_calendar_events
            else:
                mock.get_events_in_range.return_value = []
            return mock

        mock_search = MagicMock()
        mock_search.search.return_value = mock_search_results

        with patch('api.services.meeting_prep.get_calendar_service', side_effect=mock_get_service):
            with patch('api.services.meeting_prep.get_hybrid_search', return_value=mock_search):
                result = get_meeting_prep("2026-01-28")

        meeting = result.meetings[0]
        assert meeting.event_id == "1"
        assert meeting.title == "1:1 with Alex"
        assert ":" in meeting.start_time  # Time format
        assert meeting.html_link is not None
        assert isinstance(meeting.related_notes, list)

    def test_filters_all_day_events_by_default(self, mock_search_results):
        """Should exclude all-day events by default."""
        from api.services.google_auth import GoogleAccount

        all_day_event = CalendarEvent(
            event_id="3",
            title="All Day Event",
            start_time=datetime(2026, 1, 28, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 29, 0, 0, tzinfo=timezone.utc),
            is_all_day=True,
            source_account="personal",
        )

        def mock_get_service(account):
            mock = MagicMock()
            if account == GoogleAccount.PERSONAL:
                mock.get_events_in_range.return_value = [all_day_event]
            else:
                mock.get_events_in_range.return_value = []
            return mock

        mock_search = MagicMock()
        mock_search.search.return_value = []

        with patch('api.services.meeting_prep.get_calendar_service', side_effect=mock_get_service):
            with patch('api.services.meeting_prep.get_hybrid_search', return_value=mock_search):
                result = get_meeting_prep("2026-01-28", include_all_day=False)

        assert result.count == 0

    def test_includes_all_day_events_when_requested(self, mock_search_results):
        """Should include all-day events when include_all_day=True."""
        from api.services.google_auth import GoogleAccount

        all_day_event = CalendarEvent(
            event_id="3",
            title="All Day Event",
            start_time=datetime(2026, 1, 28, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 29, 0, 0, tzinfo=timezone.utc),
            is_all_day=True,
            source_account="personal",
        )

        def mock_get_service(account):
            mock = MagicMock()
            if account == GoogleAccount.PERSONAL:
                mock.get_events_in_range.return_value = [all_day_event]
            else:
                mock.get_events_in_range.return_value = []
            return mock

        mock_search = MagicMock()
        mock_search.search.return_value = []

        with patch('api.services.meeting_prep.get_calendar_service', side_effect=mock_get_service):
            with patch('api.services.meeting_prep.get_hybrid_search', return_value=mock_search):
                result = get_meeting_prep("2026-01-28", include_all_day=True)

        assert result.count == 1

    def test_invalid_date_raises_error(self):
        """Should raise ValueError for invalid date format."""
        with pytest.raises(ValueError, match="Invalid date format"):
            get_meeting_prep("invalid-date")

    def test_fetches_from_both_calendars(self, mock_search_results):
        """Should fetch from both work and personal calendars."""
        def mock_get_service(account):
            mock = MagicMock()
            mock.get_events_in_range.return_value = []
            return mock

        mock_search = MagicMock()
        mock_search.search.return_value = []

        with patch('api.services.meeting_prep.get_calendar_service', side_effect=mock_get_service) as mock_get_cal:
            with patch('api.services.meeting_prep.get_hybrid_search', return_value=mock_search):
                get_meeting_prep("2026-01-28")

        # Should be called twice (work and personal)
        assert mock_get_cal.call_count == 2


# API endpoint tests (slow - requires app initialization)
pytestmark_api = pytest.mark.slow


class TestMeetingPrepEndpoint:
    """Test /api/calendar/meeting-prep endpoint."""

    @pytest.fixture
    def mock_meeting_prep(self):
        """Create mock meeting prep response."""
        from api.services.meeting_prep import MeetingPrepResponse, MeetingPrep
        return MeetingPrepResponse(
            date="2026-01-28",
            meetings=[
                MeetingPrep(
                    event_id="1",
                    title="1:1 with Alex",
                    start_time="10:00 AM",
                    end_time="10:30 AM",
                    html_link="https://calendar.google.com/event?eid=123",
                    attendees=["alex@example.com"],
                    description="Weekly sync",
                    location=None,
                    is_all_day=False,
                    source_account="work",
                    related_notes=[
                        RelatedNote(
                            title="Alex Rechtman",
                            path="Work/ML/People/Alex Rechtman.md",
                            relevance="attendee",
                        )
                    ],
                    agenda_summary="Weekly sync",
                )
            ],
            count=1,
        )

    @pytest.mark.slow
    def test_endpoint_exists(self, mock_meeting_prep):
        """Should have meeting-prep endpoint."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        with patch('api.routes.calendar.get_meeting_prep', return_value=mock_meeting_prep):
            response = client.get("/api/calendar/meeting-prep?date=2026-01-28")
            assert response.status_code == 200

    @pytest.mark.slow
    def test_returns_expected_structure(self, mock_meeting_prep):
        """Should return expected JSON structure."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        with patch('api.routes.calendar.get_meeting_prep', return_value=mock_meeting_prep):
            response = client.get("/api/calendar/meeting-prep?date=2026-01-28")
            data = response.json()

            assert "date" in data
            assert "meetings" in data
            assert "count" in data
            assert data["count"] == 1

            meeting = data["meetings"][0]
            assert meeting["title"] == "1:1 with Alex"
            assert "related_notes" in meeting
            assert len(meeting["related_notes"]) == 1

    @pytest.mark.slow
    def test_defaults_to_today(self, mock_meeting_prep):
        """Should default to today's date if not provided."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        with patch('api.routes.calendar.get_meeting_prep', return_value=mock_meeting_prep) as mock_fn:
            response = client.get("/api/calendar/meeting-prep")
            assert response.status_code == 200
            # The date should be today's date (we can't easily test the exact value)
            mock_fn.assert_called_once()

    @pytest.mark.slow
    def test_accepts_include_all_day(self, mock_meeting_prep):
        """Should accept include_all_day parameter."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        with patch('api.routes.calendar.get_meeting_prep', return_value=mock_meeting_prep) as mock_fn:
            response = client.get("/api/calendar/meeting-prep?include_all_day=true")
            assert response.status_code == 200

    @pytest.mark.slow
    def test_invalid_date_returns_400(self):
        """Should return 400 for invalid date format."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        with patch('api.routes.calendar.get_meeting_prep', side_effect=ValueError("Invalid date")):
            response = client.get("/api/calendar/meeting-prep?date=invalid")
            assert response.status_code == 400
