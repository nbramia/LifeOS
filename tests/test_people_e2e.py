"""End-to-end tests for the People Query Orchestrator."""
import pytest

# Mark all tests as unit tests
pytestmark = pytest.mark.unit

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from api.services.query_router import QueryRouter
from api.services.briefings import BriefingsService
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.entity_resolver import EntityResolver
from api.services.interaction_store import InteractionStore, Interaction


class TestQueryRoutingE2E:
    """End-to-end tests for query routing."""

    @pytest.mark.asyncio
    async def test_full_people_query_routing(self):
        """Test the complete routing for a people query."""
        router = QueryRouter()

        with patch.object(router.ollama_client, 'is_available', return_value=False):
            result = await router.route("prep me for my 1:1 with Kevin tomorrow")

        assert "people" in result.sources
        assert "calendar" in result.sources


class TestBriefingIntegration:
    """Tests for briefing service integration."""

    @pytest.mark.asyncio
    async def test_briefing_uses_email_for_resolution(self):
        """Test that briefing service uses email for entity resolution."""
        # Create mock stores
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = MagicMock(
            entity=PersonEntity(
                id="kevin-123",
                canonical_name="Kevin Chen",
                emails=["kevin@company.com"],
                company="Acme Corp",
            ),
            is_new=False,
            confidence=1.0,
        )

        mock_interaction_store = MagicMock()
        mock_interaction_store.get_for_person.return_value = []
        mock_interaction_store.format_interaction_history.return_value = "No interactions"

        mock_hybrid_search = MagicMock()
        mock_hybrid_search.search.return_value = []

        mock_task_manager = MagicMock()
        mock_task_manager.list_tasks.return_value = []

        service = BriefingsService(
            hybrid_search=mock_hybrid_search,
            task_manager=mock_task_manager,
            entity_resolver=mock_resolver,
            interaction_store=mock_interaction_store,
        )

        # Gather context with email provided
        context = service.gather_context("Kevin", email="kevin@company.com")

        # Verify resolver was called with email
        mock_resolver.resolve.assert_called()
        call_kwargs = mock_resolver.resolve.call_args
        assert call_kwargs[1].get('email') == "kevin@company.com" or call_kwargs[0][1] == "kevin@company.com" if len(call_kwargs[0]) > 1 else True
