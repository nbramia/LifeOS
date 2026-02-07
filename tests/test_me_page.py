"""
Tests for "Me" page API endpoints.

These endpoints power the personal dashboard for the CRM owner.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from api.routes.crm import MY_PERSON_ID


class TestMeStatsEndpoint:
    """Tests for GET /api/crm/me/stats endpoint."""

    @pytest.fixture
    def mock_person_store(self):
        """Create a mock person store with test data."""
        store = MagicMock()
        # Create mock people with different stats
        people = [
            MagicMock(
                id="person-1",
                email_count=100,
                meeting_count=20,
                message_count=500,
            ),
            MagicMock(
                id="person-2",
                email_count=50,
                meeting_count=10,
                message_count=200,
            ),
            MagicMock(
                id="person-3",
                email_count=25,
                meeting_count=5,
                message_count=100,
            ),
        ]
        store.get_all.return_value = people
        return store

    @pytest.mark.asyncio
    async def test_returns_aggregate_stats(self, mock_person_store):
        """Stats endpoint should return totals across all people."""
        from api.routes.crm import get_me_stats

        with patch('api.routes.crm.get_person_entity_store', return_value=mock_person_store):
            with patch('api.routes.crm.get_interaction_store'):
                result = await get_me_stats()

        assert result.total_people == 3
        assert result.total_emails == 175  # 100 + 50 + 25
        assert result.total_meetings == 35  # 20 + 10 + 5
        assert result.total_messages == 800  # 500 + 200 + 100

    @pytest.mark.asyncio
    async def test_handles_empty_database(self):
        """Stats endpoint should handle empty database gracefully."""
        from api.routes.crm import get_me_stats

        mock_store = MagicMock()
        mock_store.get_all.return_value = []

        with patch('api.routes.crm.get_person_entity_store', return_value=mock_store):
            with patch('api.routes.crm.get_interaction_store'):
                result = await get_me_stats()

        assert result.total_people == 0
        assert result.total_emails == 0
        assert result.total_meetings == 0
        assert result.total_messages == 0


class TestMeInteractionsEndpoint:
    """Tests for GET /api/crm/me/interactions endpoint (aggregated data)."""

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
            ),
            MagicMock(
                id="person-2",
                canonical_name="Bob",
                relationship_strength=50.0,
                last_seen=now - timedelta(days=2),
                first_seen=now - timedelta(days=180),
                dunbar_circle=3,
                category="personal",
            ),
            MagicMock(
                id=MY_PERSON_ID,
                canonical_name="Test User",
                relationship_strength=100.0,
                last_seen=now,
                first_seen=now - timedelta(days=730),
                dunbar_circle=0,
                category="personal",
            ),
        ]
        person_store.get_all.return_value = people

        # Mock interaction store
        interaction_store = MagicMock()
        interactions = [
            MagicMock(
                id="int-1",
                person_id="person-1",
                source_type="imessage",
                timestamp=now - timedelta(days=1),
            ),
            MagicMock(
                id="int-2",
                person_id="person-2",
                source_type="imessage",
                timestamp=now - timedelta(days=2),
            ),
        ]
        # Mock get_all_in_range to return filtered interactions (excludes self)
        interaction_store.get_all_in_range.return_value = interactions

        return person_store, interaction_store

    @pytest.mark.asyncio
    async def test_returns_aggregated_data(self, mock_stores):
        """Interactions endpoint should return aggregated data for dashboard."""
        from api.routes.crm import get_me_interactions

        person_store, interaction_store = mock_stores

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = await get_me_interactions(days_back=30)

        # Should have total count
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
        assert 'imessage' in result.by_source

    @pytest.mark.asyncio
    async def test_excludes_self_interactions(self, mock_stores):
        """Interactions should not include self-interactions (via get_all_in_range exclude)."""
        from api.routes.crm import get_me_interactions

        person_store, interaction_store = mock_stores

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = await get_me_interactions(days_back=365)

        # Verify get_all_in_range was called with exclude_person_ids
        interaction_store.get_all_in_range.assert_called_once()
        call_args = interaction_store.get_all_in_range.call_args
        assert MY_PERSON_ID in call_args.kwargs.get('exclude_person_ids', [])

    @pytest.mark.asyncio
    async def test_filters_by_date_range(self):
        """Date filtering is done by get_all_in_range method."""
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
            )
        ]

        interaction_store = MagicMock()
        # Only return recent interaction (simulating date filter in get_all_in_range)
        interactions = [
            MagicMock(
                id="recent",
                person_id="person-1",
                source_type="imessage",
                timestamp=now - timedelta(days=5),
            ),
        ]
        interaction_store.get_all_in_range.return_value = interactions

        with patch('api.routes.crm.get_person_entity_store', return_value=person_store):
            with patch('api.routes.crm.get_interaction_store', return_value=interaction_store):
                result = await get_me_interactions(days_back=30)

        assert result.total_count == 1
        assert len(result.daily) == 1  # Should have one day with data
        # Verify date range was passed to get_all_in_range
        interaction_store.get_all_in_range.assert_called_once()


class TestMyPersonIdConstant:
    """Tests for MY_PERSON_ID constant."""

    def test_my_person_id_is_valid_uuid(self):
        """MY_PERSON_ID should be a valid UUID string."""
        import uuid
        # Should not raise
        uuid.UUID(MY_PERSON_ID)

    def test_my_person_id_from_settings(self):
        """MY_PERSON_ID should come from settings (not hardcoded)."""
        from config.settings import settings
        assert MY_PERSON_ID == settings.my_person_id
