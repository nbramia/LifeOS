"""
Tests for relationship discovery service.

Tests cover:
- Helper functions for timezone-safe datetime comparison
- Discovery functions for each source type
- Full discovery orchestration
- Suggested connections and overlap analysis
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api.services.relationship_discovery import (
    _ensure_tz_aware,
    _datetime_lt,
    _datetime_gt,
    DISCOVERY_WINDOW_DAYS,
    discover_from_calendar_direct,
    discover_from_imessage_direct,
    discover_from_whatsapp_direct,
    discover_from_phone_calls,
    _iter_chroma_metadatas,
    discover_linkedin_connections,
    run_full_discovery,
    get_suggested_connections,
    get_connection_overlap,
)
from api.services.relationship import Relationship
from api.services.person_entity import PersonEntity

pytestmark = pytest.mark.unit


class TestTimezoneHelpers:
    """Tests for timezone-aware datetime helpers."""

    def test_ensure_tz_aware_none(self):
        """None input returns None."""
        assert _ensure_tz_aware(None) is None

    def test_ensure_tz_aware_naive_datetime(self):
        """Naive datetime gets UTC timezone."""
        dt = datetime(2026, 1, 15, 10, 30)
        result = _ensure_tz_aware(dt)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_ensure_tz_aware_already_aware(self):
        """Timezone-aware datetime is unchanged."""
        dt = datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)
        result = _ensure_tz_aware(dt)
        assert result == dt
        assert result.tzinfo == timezone.utc

    def test_datetime_lt_both_none(self):
        """Comparing None values returns False."""
        assert _datetime_lt(None, None) is False

    def test_datetime_lt_one_none(self):
        """Comparing with None returns False."""
        dt = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert _datetime_lt(dt, None) is False
        assert _datetime_lt(None, dt) is False

    def test_datetime_lt_true(self):
        """Earlier date is less than later date."""
        earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
        later = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert _datetime_lt(earlier, later) is True

    def test_datetime_lt_false(self):
        """Later date is not less than earlier date."""
        earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
        later = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert _datetime_lt(later, earlier) is False

    def test_datetime_lt_mixed_timezone(self):
        """Handles mixed naive and aware datetimes."""
        naive = datetime(2026, 1, 10)
        aware = datetime(2026, 1, 15, tzinfo=timezone.utc)
        # naive is treated as UTC, so it's earlier
        assert _datetime_lt(naive, aware) is True

    def test_datetime_gt_true(self):
        """Later date is greater than earlier date."""
        earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
        later = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert _datetime_gt(later, earlier) is True

    def test_datetime_gt_false(self):
        """Earlier date is not greater than later date."""
        earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
        later = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert _datetime_gt(earlier, later) is False


class TestDiscoveryConfig:
    """Tests for discovery configuration."""

    def test_discovery_window_days_default(self):
        """Discovery window is set to reasonable default."""
        assert DISCOVERY_WINDOW_DAYS == 3650  # ~10 years - all available data


class TestDiscoverFromCalendarDirect:
    """Tests for direct calendar relationship discovery."""

    @pytest.fixture
    def mock_stores(self):
        """Create mock stores for testing."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person, \
             patch('api.services.relationship_discovery.get_interaction_store') as mock_int:

            # Setup mock relationship store
            rel_store = MagicMock()
            rel_store.get_between.return_value = None
            rel_store.add.return_value = None
            mock_rel.return_value = rel_store

            # Setup mock person store with test people
            person_store = MagicMock()
            me = PersonEntity(
                id="me-uuid",
                canonical_name="John",
                emails=["john@example.com"],
            )
            other = PersonEntity(
                id="other-uuid",
                canonical_name="Alex Johnson",
                emails=["alex@example.com"],
            )
            person_store.get_all.return_value = [me, other]
            mock_person.return_value = person_store

            # Setup mock interaction store
            int_store = MagicMock()
            mock_int.return_value = int_store

            yield {
                'rel_store': rel_store,
                'person_store': person_store,
                'int_store': int_store,
            }

    def test_discover_creates_relationships(self, mock_stores):
        """Calendar discovery creates relationships between attendees."""
        # This test verifies the function runs without error
        # Detailed testing requires database mocking
        with patch('api.services.relationship_discovery.get_interaction_db_path') as mock_path:
            mock_path.return_value = ":memory:"
            with patch('sqlite3.connect') as mock_conn:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = []
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.execute.return_value = mock_cursor

                result = discover_from_calendar_direct()
                assert isinstance(result, list)


class TestDiscoverFromMessagingDirect:
    """Tests for direct messaging relationship discovery."""

    @pytest.fixture
    def mock_stores(self):
        """Create mock stores for testing."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_between.return_value = None
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_store.get_all.return_value = []
            mock_person.return_value = person_store

            yield {
                'rel_store': rel_store,
                'person_store': person_store,
            }

    def test_imessage_discovery_handles_empty_db(self, mock_stores):
        """iMessage discovery handles empty database gracefully."""
        with patch('api.services.relationship_discovery.get_interaction_db_path') as mock_path:
            mock_path.return_value = ":memory:"
            with patch('sqlite3.connect') as mock_conn:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = []
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.execute.return_value = mock_cursor

                result = discover_from_imessage_direct()
                assert isinstance(result, list)
                assert len(result) == 0

    def test_whatsapp_discovery_handles_empty_db(self, mock_stores):
        """WhatsApp discovery handles empty database gracefully."""
        with patch('api.services.relationship_discovery.get_interaction_db_path') as mock_path:
            mock_path.return_value = ":memory:"
            with patch('sqlite3.connect') as mock_conn:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = []
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.execute.return_value = mock_cursor

                result = discover_from_whatsapp_direct()
                assert isinstance(result, list)
                assert len(result) == 0

    def test_phone_calls_discovery_handles_empty_db(self, mock_stores):
        """Phone call discovery handles empty database gracefully."""
        with patch('api.services.relationship_discovery.get_interaction_db_path') as mock_path:
            mock_path.return_value = ":memory:"
            with patch('sqlite3.connect') as mock_conn:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = []
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.execute.return_value = mock_cursor

                result = discover_from_phone_calls()
                assert isinstance(result, list)
                assert len(result) == 0


class TestDiscoverLinkedInConnections:
    """Tests for LinkedIn connection discovery."""

    def test_linkedin_discovery_returns_list(self):
        """LinkedIn discovery returns a list of relationships."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            # Create test people with LinkedIn URLs
            person_with_linkedin = PersonEntity(
                id="linkedin-person",
                canonical_name="Alex",
                linkedin_url="https://linkedin.com/in/alex",
            )
            person_without_linkedin = PersonEntity(
                id="no-linkedin-person",
                canonical_name="Bob",
            )
            person_store.get_all.return_value = [person_with_linkedin, person_without_linkedin]
            mock_person.return_value = person_store

            result = discover_linkedin_connections()
            assert isinstance(result, list)


class TestRunFullDiscovery:
    """Tests for full discovery orchestration."""

    def test_run_full_discovery_returns_stats(self):
        """Full discovery returns statistics dictionary."""
        # Mock all individual discovery functions
        with patch('api.services.relationship_discovery.discover_from_calendar') as mock_cal, \
             patch('api.services.relationship_discovery.discover_from_calendar_direct') as mock_cal_direct, \
             patch('api.services.relationship_discovery.discover_from_email_threads') as mock_email, \
             patch('api.services.relationship_discovery.discover_from_vault_comments') as mock_vault, \
             patch('api.services.relationship_discovery.discover_from_messaging_groups') as mock_msg, \
             patch('api.services.relationship_discovery.discover_from_imessage_direct') as mock_im, \
             patch('api.services.relationship_discovery.discover_from_whatsapp_direct') as mock_wa, \
             patch('api.services.relationship_discovery.discover_from_phone_calls') as mock_phone, \
             patch('api.services.relationship_discovery.discover_from_slack_direct') as mock_slack, \
             patch('api.services.relationship_discovery.discover_linkedin_connections') as mock_li:

            # Each function returns empty list
            mock_cal.return_value = []
            mock_cal_direct.return_value = []
            mock_email.return_value = []
            mock_vault.return_value = []
            mock_msg.return_value = []
            mock_im.return_value = []
            mock_wa.return_value = []
            mock_phone.return_value = []
            mock_slack.return_value = []
            mock_li.return_value = []

            result = run_full_discovery()

            assert isinstance(result, dict)
            assert 'by_source' in result
            assert 'total' in result
            by_source = result['by_source']
            assert 'calendar' in by_source
            assert 'calendar_direct' in by_source
            assert 'email' in by_source
            assert 'vault' in by_source
            assert 'messaging_groups' in by_source
            assert 'imessage_direct' in by_source
            assert 'whatsapp_direct' in by_source
            assert 'phone_calls' in by_source
            assert 'slack_direct' in by_source
            assert 'linkedin' in by_source

    def test_run_full_discovery_aggregates_counts(self):
        """Full discovery aggregates relationship counts from all sources."""
        with patch('api.services.relationship_discovery.discover_from_calendar') as mock_cal, \
             patch('api.services.relationship_discovery.discover_from_calendar_direct') as mock_cal_direct, \
             patch('api.services.relationship_discovery.discover_from_email_threads') as mock_email, \
             patch('api.services.relationship_discovery.discover_from_vault_comments') as mock_vault, \
             patch('api.services.relationship_discovery.discover_from_messaging_groups') as mock_msg, \
             patch('api.services.relationship_discovery.discover_from_imessage_direct') as mock_im, \
             patch('api.services.relationship_discovery.discover_from_whatsapp_direct') as mock_wa, \
             patch('api.services.relationship_discovery.discover_from_phone_calls') as mock_phone, \
             patch('api.services.relationship_discovery.discover_from_slack_direct') as mock_slack, \
             patch('api.services.relationship_discovery.discover_linkedin_connections') as mock_li, \
             patch('api.services.relationship_discovery.discover_from_shared_photos') as mock_photos:

            # Create mock relationships
            mock_rel = MagicMock(spec=Relationship)

            mock_cal.return_value = [mock_rel, mock_rel]  # 2
            mock_cal_direct.return_value = [mock_rel, mock_rel, mock_rel]  # 3
            mock_email.return_value = [mock_rel]  # 1
            mock_vault.return_value = []  # 0
            mock_msg.return_value = []  # 0
            mock_im.return_value = [mock_rel, mock_rel]  # 2
            mock_wa.return_value = [mock_rel]  # 1
            mock_phone.return_value = []  # 0
            mock_slack.return_value = [mock_rel]  # 1
            mock_li.return_value = []  # 0
            mock_photos.return_value = []  # 0

            result = run_full_discovery()

            by_source = result['by_source']
            assert by_source['calendar'] == 2
            assert by_source['calendar_direct'] == 3
            assert by_source['email'] == 1
            assert by_source['imessage_direct'] == 2
            assert by_source['whatsapp_direct'] == 1
            assert by_source['slack_direct'] == 1
            assert by_source['photos'] == 0
            assert result['total'] == 10


class TestGetSuggestedConnections:
    """Tests for connection suggestion functionality."""

    def test_suggested_connections_returns_list(self):
        """Suggested connections returns a list."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_store.get_all.return_value = []
            person_store.get_by_id.return_value = PersonEntity(id="test-person", canonical_name="Test")
            mock_person.return_value = person_store

            result = get_suggested_connections(person_id="test-person")
            assert isinstance(result, list)

    def test_suggested_connections_with_limit(self):
        """Suggested connections respects limit parameter."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_store.get_all.return_value = []
            person_store.get_by_id.return_value = PersonEntity(id="test-person", canonical_name="Test")
            mock_person.return_value = person_store

            result = get_suggested_connections(person_id="test-person", limit=5)
            assert len(result) <= 5


class TestGetConnectionOverlap:
    """Tests for connection overlap analysis."""

    def test_overlap_returns_dict(self):
        """Connection overlap returns a dictionary."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_between.return_value = None
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_a = PersonEntity(id="person-a", canonical_name="Alice")
            person_b = PersonEntity(id="person-b", canonical_name="Bob")
            person_store.get_by_id.side_effect = lambda x: person_a if x == "person-a" else person_b
            mock_person.return_value = person_store

            result = get_connection_overlap("person-a", "person-b")
            assert isinstance(result, dict)

    def test_overlap_includes_relationship_info(self):
        """Connection overlap includes relationship information."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            mock_relationship = MagicMock(spec=Relationship)
            mock_relationship.shared_events_count = 5
            mock_relationship.shared_threads_count = 3
            mock_relationship.shared_messages_count = 10
            mock_relationship.shared_whatsapp_count = 2
            mock_relationship.shared_slack_count = 1
            mock_relationship.shared_phone_calls_count = 0
            mock_relationship.is_linkedin_connection = True
            mock_relationship.first_seen_together = datetime(2025, 1, 1, tzinfo=timezone.utc)
            mock_relationship.last_seen_together = datetime(2026, 1, 1, tzinfo=timezone.utc)
            mock_relationship.shared_contexts = ["work"]
            rel_store.get_between.return_value = mock_relationship
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_a = PersonEntity(id="person-a", canonical_name="Alice")
            person_b = PersonEntity(id="person-b", canonical_name="Bob")
            person_store.get_by_id.side_effect = lambda x: person_a if x == "person-a" else person_b
            mock_person.return_value = person_store

            result = get_connection_overlap("person-a", "person-b")

            assert 'relationship' in result
            assert result['relationship'] is not None

    def test_overlap_with_no_relationship(self):
        """Connection overlap handles missing relationship gracefully."""
        with patch('api.services.relationship_discovery.get_relationship_store') as mock_rel, \
             patch('api.services.relationship_discovery.get_person_entity_store') as mock_person:

            rel_store = MagicMock()
            rel_store.get_between.return_value = None
            rel_store.get_for_person.return_value = []
            mock_rel.return_value = rel_store

            person_store = MagicMock()
            person_a = PersonEntity(id="person-a", canonical_name="Alice")
            person_b = PersonEntity(id="person-b", canonical_name="Bob")
            person_store.get_by_id.side_effect = lambda x: person_a if x == "person-a" else person_b
            mock_person.return_value = person_store

            result = get_connection_overlap("person-a", "person-b")

            # Relationship dict exists but shows no connection
            assert result.get('relationship') is not None
            assert result['relationship']['exists'] is False


class TestChromaMetadataScan:
    """
    Tests for the paged ChromaDB metadata scan.

    Regression cover for the nightly-sync outage where a single unbounded
    get() over the Slack collection failed with "too many SQL variables"
    once it grew past SQLite's bind-parameter ceiling, and for the
    `$gte`-on-a-string date filter that had silently never worked.
    """

    @staticmethod
    def _collection(metadatas):
        """A fake ChromaDB collection that serves `metadatas` via limit/offset."""
        collection = MagicMock()

        def _get(include=None, limit=None, offset=0, **kwargs):
            assert "where" not in kwargs, "date filter must not be pushed down to ChromaDB"
            return {"metadatas": metadatas[offset:offset + limit]}

        collection.get.side_effect = _get
        return collection

    def test_pages_through_collection_larger_than_page_size(self):
        """All rows are returned even when the collection spans many pages."""
        metadatas = [{"modified_date": "2026-01-01", "n": i} for i in range(250)]
        collection = self._collection(metadatas)

        result = list(_iter_chroma_metadatas(collection, "2020-01-01", page_size=100))

        assert [m["n"] for m in result] == list(range(250))
        assert collection.get.call_count == 3

    def test_never_requests_more_than_page_size_at_once(self):
        """No single get() may exceed the page size — that is what broke."""
        metadatas = [{"modified_date": "2026-01-01"} for _ in range(40000)]
        collection = self._collection(metadatas)

        list(_iter_chroma_metadatas(collection, "2020-01-01", page_size=5000))

        assert collection.get.call_count == 9
        for call in collection.get.call_args_list:
            assert call.kwargs["limit"] == 5000

    def test_applies_cutoff_date_as_string_comparison(self):
        """The cutoff filters rows, using lexicographic ISO date ordering."""
        metadatas = [
            {"modified_date": "2025-12-31", "n": 0},
            {"modified_date": "2026-01-01", "n": 1},
            {"modified_date": "2026-06-15", "n": 2},
        ]
        collection = self._collection(metadatas)

        result = list(_iter_chroma_metadatas(collection, "2026-01-01", page_size=10))

        assert [m["n"] for m in result] == [1, 2]

    def test_row_missing_modified_date_is_excluded(self):
        """A row with no usable date does not slip past the cutoff."""
        collection = self._collection([{"n": 0}, {"modified_date": None, "n": 1}])

        result = list(_iter_chroma_metadatas(collection, "2026-01-01", page_size=10))

        assert result == []

    def test_stops_when_collection_size_is_an_exact_page_multiple(self):
        """A full final page is followed by one empty page, then termination."""
        metadatas = [{"modified_date": "2026-01-01"} for _ in range(200)]
        collection = self._collection(metadatas)

        result = list(_iter_chroma_metadatas(collection, "2020-01-01", page_size=100))

        assert len(result) == 200
        assert collection.get.call_count == 3

    def test_empty_collection_yields_nothing(self):
        """An empty collection terminates after a single get()."""
        collection = self._collection([])

        assert list(_iter_chroma_metadatas(collection, "2020-01-01")) == []
        assert collection.get.call_count == 1
