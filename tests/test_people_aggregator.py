"""
Tests for People Aggregator - multi-source people tracking.

Sources:
- LinkedIn connections CSV
- Gmail contacts (last 2 years)
- Calendar attendees
- Granola meeting notes
- Obsidian note mentions
"""
import pytest

# All tests in this file use mocks (unit tests)
pytestmark = pytest.mark.unit
import tempfile
import csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from api.services.people_aggregator import (
    PeopleAggregator,
    PersonRecord,
    load_linkedin_connections,
    extract_gmail_contacts,
    extract_calendar_attendees,
)


class TestLinkedInLoader:
    """Test LinkedIn CSV loading."""

    def test_loads_csv_file(self):
        """Should load LinkedIn connections from CSV."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['First Name', 'Last Name', 'URL', 'Email Address', 'Company', 'Position', 'Connected On'])
            writer.writerow(['John', 'Doe', 'https://linkedin.com/in/johndoe', 'john@example.com', 'Acme', 'Engineer', '01 Jan 2025'])
            writer.writerow(['Jane', 'Smith', 'https://linkedin.com/in/janesmith', '', 'Corp', 'Manager', '15 Feb 2025'])
            f.flush()

            connections = load_linkedin_connections(f.name)

        assert len(connections) >= 2
        assert any(c['first_name'] == 'John' for c in connections)
        assert any(c['last_name'] == 'Smith' for c in connections)

    def test_handles_missing_email(self):
        """Should handle connections without email."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['First Name', 'Last Name', 'URL', 'Email Address', 'Company', 'Position', 'Connected On'])
            writer.writerow(['Jane', 'Smith', 'https://linkedin.com/in/janesmith', '', 'Corp', 'Manager', '15 Feb 2025'])
            f.flush()

            connections = load_linkedin_connections(f.name)

        assert len(connections) == 1
        assert connections[0]['email'] == '' or connections[0]['email'] is None

    def test_returns_empty_for_missing_file(self):
        """Should return empty list if file doesn't exist."""
        connections = load_linkedin_connections('/nonexistent/file.csv')
        assert connections == []


class TestGmailContactExtraction:
    """Test extracting contacts from Gmail."""

    @pytest.fixture
    def mock_gmail_service(self):
        """Create mock Gmail service."""
        mock = MagicMock()
        return mock

    def test_extracts_sender_from_emails(self, mock_gmail_service):
        """Should extract sender info from emails."""
        mock_gmail_service.search.return_value = [
            MagicMock(
                sender='kevin@example.com',
                sender_name='Kevin',
                date=datetime(2025, 6, 1, tzinfo=timezone.utc)
            ),
            MagicMock(
                sender='alice@example.com',
                sender_name='Alice Smith',
                date=datetime(2025, 7, 1, tzinfo=timezone.utc)
            ),
        ]

        contacts = extract_gmail_contacts(mock_gmail_service, days_back=365)

        assert len(contacts) >= 2
        assert any(c['email'] == 'kevin@example.com' for c in contacts)

    def test_deduplicates_by_email(self, mock_gmail_service):
        """Should deduplicate contacts by email."""
        mock_gmail_service.search.return_value = [
            MagicMock(sender='kevin@example.com', sender_name='Kevin', date=datetime(2025, 6, 1, tzinfo=timezone.utc)),
            MagicMock(sender='kevin@example.com', sender_name='Kevin', date=datetime(2025, 7, 1, tzinfo=timezone.utc)),
        ]

        contacts = extract_gmail_contacts(mock_gmail_service, days_back=365)

        emails = [c['email'] for c in contacts]
        assert emails.count('kevin@example.com') == 1


class TestCalendarAttendeeExtraction:
    """Test extracting attendees from Calendar."""

    @pytest.fixture
    def mock_calendar_service(self):
        """Create mock Calendar service."""
        mock = MagicMock()
        return mock

    def test_extracts_attendees(self, mock_calendar_service):
        """Should extract attendees from calendar events."""
        mock_calendar_service.get_events_in_range.return_value = [
            MagicMock(
                attendees=['alice@example.com', 'Bob Smith'],
                start_time=datetime(2025, 6, 1, tzinfo=timezone.utc)
            ),
        ]

        attendees = extract_calendar_attendees(mock_calendar_service, days_back=365)

        assert len(attendees) >= 1
        assert any('alice' in a.get('name', '').lower() or 'alice' in a.get('email', '').lower() for a in attendees)

    def test_counts_meeting_frequency(self, mock_calendar_service):
        """Should count meeting frequency per person."""
        mock_calendar_service.get_events_in_range.return_value = [
            MagicMock(attendees=['alice@example.com'], start_time=datetime(2025, 6, 1, tzinfo=timezone.utc)),
            MagicMock(attendees=['alice@example.com'], start_time=datetime(2025, 7, 1, tzinfo=timezone.utc)),
            MagicMock(attendees=['bob@example.com'], start_time=datetime(2025, 6, 15, tzinfo=timezone.utc)),
        ]

        attendees = extract_calendar_attendees(mock_calendar_service, days_back=365)

        alice = next((a for a in attendees if 'alice' in a.get('email', '').lower()), None)
        if alice:
            assert alice.get('meeting_count', 0) >= 2


class TestPersonRecord:
    """Test PersonRecord dataclass."""

    def test_creates_record_with_sources(self):
        """Should track which sources mentioned this person."""
        record = PersonRecord(
            canonical_name='Kevin',
            email='kevin@example.com',
            sources=['gmail', 'calendar', 'linkedin']
        )
        assert 'gmail' in record.sources
        assert 'linkedin' in record.sources

    def test_merge_records(self):
        """Should merge records from different sources."""
        r1 = PersonRecord(
            canonical_name='Kevin',
            email='kevin@example.com',
            sources=['gmail'],
            first_seen=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_seen=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        r2 = PersonRecord(
            canonical_name='Kevin',
            email='kevin@example.com',
            sources=['calendar'],
            first_seen=datetime(2025, 3, 1, tzinfo=timezone.utc),
            last_seen=datetime(2025, 8, 1, tzinfo=timezone.utc),
            meeting_count=5,
        )

        merged = r1.merge(r2)

        assert 'gmail' in merged.sources
        assert 'calendar' in merged.sources
        assert merged.last_seen == datetime(2025, 8, 1, tzinfo=timezone.utc)
        assert merged.meeting_count >= 5


class TestPeopleAggregator:
    """Test PeopleAggregator service."""

    @pytest.fixture
    def aggregator(self, tmp_path):
        """Create aggregator with mock services and temp storage."""
        return PeopleAggregator(
            linkedin_csv_path=None,
            gmail_service=None,
            calendar_service=None,
            storage_path=str(tmp_path / "people_test.json"),
        )

    def test_aggregates_from_multiple_sources(self, aggregator):
        """Should combine people from all sources."""
        # Add people from different sources
        aggregator.add_person_from_source(
            name='Kevin',
            email='kevin@example.com',
            source='gmail',
        )
        aggregator.add_person_from_source(
            name='Kevin',
            email='kevin@example.com',
            source='calendar',
        )
        aggregator.add_person_from_source(
            name='Alice',
            email='alice@example.com',
            source='linkedin',
        )

        people = aggregator.get_all_people()

        assert len(people) == 2  # Kevin merged, Alice separate
        kevin = next((p for p in people if p.canonical_name == 'Kevin'), None)
        assert kevin is not None
        assert 'gmail' in kevin.sources
        assert 'calendar' in kevin.sources

    def test_search_by_name(self, aggregator):
        """Should search people by name."""
        aggregator.add_person_from_source(name='Kevin Smith', email='kevin@example.com', source='gmail')
        aggregator.add_person_from_source(name='Alice Jones', email='alice@example.com', source='gmail')

        results = aggregator.search('kevin')

        assert len(results) == 1
        assert results[0].canonical_name == 'Kevin Smith'

    def test_get_person_summary(self, aggregator):
        """Should generate summary for a person."""
        aggregator.add_person_from_source(
            name='Kevin',
            email='kevin@example.com',
            source='gmail',
            company='Acme Corp',
        )
        aggregator.add_person_from_source(
            name='Kevin',
            email='kevin@example.com',
            source='calendar',
            meeting_count=5,
        )

        summary = aggregator.get_person_summary('Kevin')

        assert summary is not None
        assert 'Kevin' in summary['name']
        assert summary['email'] == 'kevin@example.com'
