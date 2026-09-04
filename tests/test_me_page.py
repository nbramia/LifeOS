"""
Tests for "Me" page API endpoints.

These endpoints power the personal dashboard for the CRM owner.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from api.routes.crm import MY_PERSON_ID

# Marked per-class below rather than at module level: every class here is
# mock-based (unit) except test_my_person_id_is_valid_uuid, which needs a
# real configured settings.my_person_id (#682) and is marked individually.


@pytest.mark.unit
class TestMeStatsEndpoint:
    """Tests for GET /api/crm/me/stats endpoint.

    #871 replaced the "load every PersonEntity and sum in Python" approach
    with a single SQL SUM (PersonEntityStore.get_totals()), so these mock the
    new method's return value instead of get_all().
    """

    @pytest.fixture
    def mock_person_store(self):
        """Create a mock person store with test data."""
        store = MagicMock()
        store.get_totals.return_value = {
            "total_people": 3,
            "total_emails": 175,  # 100 + 50 + 25
            "total_meetings": 35,  # 20 + 10 + 5
            "total_messages": 800,  # 500 + 200 + 100
        }
        return store

    def test_returns_aggregate_stats(self, mock_person_store):
        """Stats endpoint should return totals across all people."""
        from api.routes.crm import get_me_stats

        with patch('api.routes.crm.get_person_entity_store', return_value=mock_person_store):
            result = get_me_stats()

        assert result.total_people == 3
        assert result.total_emails == 175
        assert result.total_meetings == 35
        assert result.total_messages == 800
        mock_person_store.get_totals.assert_called_once()

    def test_handles_empty_database(self):
        """Stats endpoint should handle empty database gracefully."""
        from api.routes.crm import get_me_stats

        mock_store = MagicMock()
        mock_store.get_totals.return_value = {
            "total_people": 0,
            "total_emails": 0,
            "total_meetings": 0,
            "total_messages": 0,
        }

        with patch('api.routes.crm.get_person_entity_store', return_value=mock_store):
            result = get_me_stats()

        assert result.total_people == 0
        assert result.total_emails == 0
        assert result.total_meetings == 0
        assert result.total_messages == 0


@pytest.mark.unit
class TestMeInteractionsEndpoint:
    """Tests for GET /api/crm/me/interactions endpoint (aggregated data).

    #871 moved the heatmap/breakdown/by_circle/messaging aggregation from a
    Python loop over every hydrated Interaction in the window to a single SQL
    GROUP BY query (get_daily_person_source_counts, grouped by day + person +
    source and filtered in Python instead of via a SQL exclude list — see
    that method's docstring for why). #897's review follow-up moved the
    health-score and neglected-contacts widgets to SQL too:
    get_bucketed_counts (a CASE-per-bucket SUM, replacing a fetch +
    _bucket_counts_by_period) and get_person_julianday_timestamps (a
    covering person_id+timestamp query, replacing get_person_timestamps).
    These tests mock those new methods directly; full correctness of the
    aggregation math itself (identical output to the pre-#871
    implementation) is covered by the oracle tests in
    tests/test_me_family_aggregates_oracle.py against a real synthetic DB.
    """

    @pytest.fixture
    def mock_stores(self):
        """Create mock stores with test data."""
        now = datetime.now(timezone.utc)

        # Mock person store
        person_store = MagicMock()
        people = [
            MagicMock(
                id="person-1",
                canonical_name="Alice",
                relationship_strength=90.0,
                last_seen=now - timedelta(days=1),
                first_seen=now - timedelta(days=365),
                dunbar_circle=2,
                category="personal",
                is_peripheral_contact=False,
            ),
            MagicMock(
                id="person-2",
                canonical_name="Bob",
                relationship_strength=50.0,
                last_seen=now - timedelta(days=2),
                first_seen=now - timedelta(days=180),
                dunbar_circle=3,
                category="personal",
                is_peripheral_contact=False,
            ),
            MagicMock(
                id=MY_PERSON_ID,
                canonical_name="Test User",
                relationship_strength=100.0,
                last_seen=now,
                first_seen=now - timedelta(days=730),
                dunbar_circle=0,
                category="personal",
                is_peripheral_contact=False,
            ),
        ]
        person_store.get_all.return_value = people
        person_store.get_hidden_ids.return_value = set()
        person_store.get_merged_secondary_ids.return_value = set()

        # Mock interaction store: two imessage interactions with person-1/-2,
        # already reflected as SQL-grouped output rather than raw rows.
        interaction_store = MagicMock()
        interaction_store.get_daily_person_source_counts.return_value = [
            (now.strftime('%Y-%m-%d'), "person-1", "imessage", 1),
            ((now - timedelta(days=2)).strftime('%Y-%m-%d'), "person-2", "imessage", 1),
        ]
        interaction_store.get_person_counts.return_value = {"person-1": 1, "person-2": 1}
        interaction_store.get_bucketed_counts.side_effect = lambda time_points, **kwargs: [0] * len(time_points)
        interaction_store.get_person_julianday_timestamps.return_value = []
        interaction_store.get_julianday.return_value = 0.0
        interaction_store.get_all_in_range.return_value = []
        interaction_store.get_first_interaction_dates.return_value = {}

        return person_store, interaction_store

    def test_returns_aggregated_data(self, mock_stores):
        """Interactions endpoint should return aggregated data for dashboard."""
        from api.routes.crm import get_me_interactions

        person_store, interaction_store = mock_stores

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = get_me_interactions(days_back=30)

        # Should have total count (sum of get_daily_person_source_counts rows)
        assert result.total_count == 2

        # Should have aggregated data structures
        assert isinstance(result.daily, list)
        assert isinstance(result.by_source, dict)
        assert isinstance(result.by_month, dict)
        assert isinstance(result.by_circle, dict)
        assert isinstance(result.top_contacts, list)
        assert isinstance(result.warming, list)
        assert isinstance(result.cooling, list)

        # Check source breakdown
        assert result.by_source.get('imessage') == 2

        # by_circle should reflect the window rows mapped through circle_map
        # (person-1 -> circle 2, person-2 -> circle 3)
        assert result.by_circle.get('2') == 1
        assert result.by_circle.get('3') == 1

    def test_excludes_self_interactions(self, mock_stores):
        """Self's interactions must never contribute to totals, and the
        small trend/top-contacts SQL queries still carry exclude_person_ids
        directly (their windows are cheap to filter that way; only the big
        window scan moved exclusion into Python — see
        get_daily_person_source_counts's docstring)."""
        from api.routes.crm import get_me_interactions

        person_store, interaction_store = mock_stores
        now = datetime.now(timezone.utc)
        # Inject a self-attributed row into the window scan: it must be
        # dropped by the handler's Python-side exclusion filter, not counted.
        interaction_store.get_daily_person_source_counts.return_value = [
            (now.strftime('%Y-%m-%d'), "person-1", "imessage", 1),
            (now.strftime('%Y-%m-%d'), MY_PERSON_ID, "imessage", 5),
        ]

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = get_me_interactions(days_back=365)

        assert result.total_count == 1  # only person-1's row, not self's 5

        interaction_store.get_person_counts.assert_called()
        for call in interaction_store.get_person_counts.call_args_list:
            assert MY_PERSON_ID in call.kwargs.get('exclude_person_ids', [])

    def test_get_all_not_called_more_than_once(self, mock_stores):
        """None of the Me handlers may call the load-all-people store method
        more than once per request (#871 acceptance criterion)."""
        from api.routes.crm import get_me_interactions

        person_store, interaction_store = mock_stores

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                get_me_interactions(days_back=365)

        assert person_store.get_all.call_count <= 1

    def test_filters_by_date_range(self):
        """Date filtering is passed through to the SQL aggregate queries."""
        from api.routes.crm import get_me_interactions

        now = datetime.now(timezone.utc)

        person_store = MagicMock()
        person_store.get_all.return_value = [
            MagicMock(
                id="person-1",
                canonical_name="Alice",
                relationship_strength=50.0,
                last_seen=now - timedelta(days=5),
                first_seen=now - timedelta(days=100),
                dunbar_circle=2,
                category="personal",
                is_peripheral_contact=False,
            )
        ]
        person_store.get_hidden_ids.return_value = set()
        person_store.get_merged_secondary_ids.return_value = set()

        interaction_store = MagicMock()
        interaction_store.get_daily_person_source_counts.return_value = [
            ((now - timedelta(days=5)).strftime('%Y-%m-%d'), "person-1", "imessage", 1),
        ]
        interaction_store.get_person_counts.return_value = {"person-1": 1}
        interaction_store.get_bucketed_counts.side_effect = lambda time_points, **kwargs: [0] * len(time_points)
        interaction_store.get_person_julianday_timestamps.return_value = []
        interaction_store.get_julianday.return_value = 0.0
        interaction_store.get_all_in_range.return_value = []
        interaction_store.get_first_interaction_dates.return_value = {}

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = get_me_interactions(days_back=30)

        assert result.total_count == 1
        assert len(result.daily) == 1  # Should have one day with data

        # Verify the requested days_back translated into a start_date roughly
        # `days_back` days before now, passed to the SQL aggregate query.
        call_args = interaction_store.get_daily_person_source_counts.call_args
        start_date = call_args.args[0] if call_args.args else call_args.kwargs['start_date']
        assert (now - start_date).days in (29, 30, 31)


class TestMyPersonIdConstant:
    """Tests for MY_PERSON_ID constant."""

    @pytest.mark.integration
    def test_my_person_id_is_valid_uuid(self):
        """MY_PERSON_ID should be a valid UUID string. Needs a real configured
        settings.my_person_id — empty (not a UUID) in a clean checkout."""
        import uuid
        # Should not raise
        uuid.UUID(MY_PERSON_ID)

    @pytest.mark.unit
    def test_my_person_id_from_settings(self):
        """MY_PERSON_ID should come from settings (not hardcoded)."""
        from config.settings import settings
        assert MY_PERSON_ID == settings.my_person_id
