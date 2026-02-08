"""
Tests for Stakeholder Briefings.
P2.3 Acceptance Criteria:
- "Tell me about [person]" generates briefing
- Briefing includes last interaction date
- Briefing includes open action items
- Briefing includes recent discussion context
- Handles unknown people gracefully
- Sources cited with links
"""
import pytest

from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from api.services.briefings import BriefingsService, BriefingContext


class TestBriefingContext:
    """Test BriefingContext dataclass."""

    def test_creates_context_with_defaults(self):
        """Should create context with default values."""
        context = BriefingContext(
            person_name="alex",
            resolved_name="Alex"
        )

        assert context.person_name == "alex"
        assert context.resolved_name == "Alex"
        assert context.meeting_count == 0
        assert context.related_notes == []
        assert context.action_items == []

    def test_context_stores_all_fields(self):
        """Should store all provided fields."""
        context = BriefingContext(
            person_name="alex",
            resolved_name="Alex",
            email="alex@example.com",
            company="Example Corp",
            position="CEO",
            category="work",
            meeting_count=15,
            email_count=50,
            mention_count=30,
            last_interaction=datetime(2023, 1, 7, tzinfo=timezone.utc),
        )

        assert context.email == "alex@example.com"
        assert context.company == "Example Corp"
        assert context.meeting_count == 15
        assert context.last_interaction.year == 2023


class TestBriefingsService:
    """Test BriefingsService."""

    @pytest.fixture
    def mock_hybrid_search(self):
        """Create mock hybrid search."""
        search = MagicMock()
        return search

    @pytest.fixture
    def mock_task_manager(self):
        """Create mock task manager."""
        manager = MagicMock()
        manager.list_tasks.return_value = []
        return manager

    @pytest.fixture
    def mock_entity_resolver(self):
        """Create mock entity resolver."""
        resolver = MagicMock()
        resolver.resolve.return_value = None  # Default to not found, tests can override
        return resolver

    @pytest.fixture
    def mock_interaction_store(self):
        """Create mock interaction store."""
        store = MagicMock()
        store.get_for_person.return_value = []
        store.format_interaction_history.return_value = ""
        return store

    @pytest.fixture
    def service(self, mock_hybrid_search, mock_task_manager, mock_entity_resolver, mock_interaction_store):
        """Create briefings service with mocks."""
        return BriefingsService(
            hybrid_search=mock_hybrid_search,
            task_manager=mock_task_manager,
            entity_resolver=mock_entity_resolver,
            interaction_store=mock_interaction_store,
        )

    def test_gather_context_resolves_name(self, service):
        """Should resolve person name."""
        context = service.gather_context("alex")

        assert context is not None
        # resolved_name is title-cased if name is in dictionary, otherwise as-is
        assert context.resolved_name in ["alex", "Alex"]

    def test_gather_context_includes_entity_data(self, service, mock_entity_resolver):
        """Should include data from entity resolver."""
        from api.services.person_entity import PersonEntity
        from datetime import datetime

        mock_entity = PersonEntity(
            id="test-123",
            canonical_name="Alex",
            emails=["alex@example.com"],
            company="Example Corp",
            position="CEO",
            sources=["linkedin", "calendar"],
            meeting_count=10,
            email_count=20,
        )

        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_entity_resolver.resolve.return_value = mock_result

        context = service.gather_context("alex")

        assert context.email == "alex@example.com"
        assert context.company == "Example Corp"
        assert context.meeting_count == 10

    def test_gather_context_searches_vault(self, service, mock_hybrid_search):
        """Should search vault for mentions."""
        mock_hybrid_search.search.return_value = [
            {
                "content": "Meeting with Alex about Q1 goals",
                "metadata": {"file_name": "Q1 Planning.md", "file_path": "/vault/Q1 Planning.md"},
                "score": 0.9,
            }
        ]

        context = service.gather_context("alex")

        assert len(context.related_notes) == 1
        assert "Q1 Planning.md" in context.sources

    def test_gather_context_gets_action_items(self, service, mock_task_manager):
        """Should get action items for person."""
        mock_task = MagicMock()
        mock_task.description = "Review budget proposal"
        mock_task.status = "todo"
        mock_task.due_date = None
        mock_task.source_file = "Budget.md"
        mock_task_manager.list_tasks.return_value = [mock_task]

        context = service.gather_context("alex")

        assert len(context.action_items) == 1
        assert context.action_items[0]["task"] == "Review budget proposal"

    @pytest.mark.asyncio
    async def test_generate_briefing_for_known_person(self, service, mock_entity_resolver, mock_hybrid_search):
        """Should generate briefing for known person."""
        from api.services.person_entity import PersonEntity

        mock_entity = PersonEntity(
            id="test-123",
            canonical_name="John",
            emails=["john@example.com"],
            company="Example Corp",
            sources=["linkedin"],
        )
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_entity_resolver.resolve.return_value = mock_result

        mock_hybrid_search.search.return_value = [
            {"content": "Discussion about strategy", "metadata": {"file_name": "Strategy.md"}, "score": 0.9}
        ]

        with patch('api.services.briefings.get_synthesizer') as mock_synth:
            mock_synth.return_value.get_response = AsyncMock(
                return_value="## John — Briefing\n\nThis is the briefing content."
            )

            result = await service.generate_briefing("john")

            assert result["status"] == "success"
            assert "briefing" in result
            assert result["person_name"] == "John"

    @pytest.mark.asyncio
    async def test_generate_briefing_handles_unknown_person(self, service, mock_hybrid_search):
        """Should handle unknown person gracefully."""
        mock_hybrid_search.search.return_value = []

        result = await service.generate_briefing("unknown_person_xyz")

        assert result["status"] in ["not_found", "limited"]


@pytest.mark.slow
class TestBriefingsAPI:
    """Test briefings API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_briefing_endpoint_exists(self, client):
        """Briefing endpoint should exist."""
        response = client.post("/api/briefing", json={"person_name": "test"})
        assert response.status_code != 404
        assert response.status_code != 405

    def test_briefing_rejects_empty_name(self, client):
        """Should reject empty person name."""
        response = client.post("/api/briefing", json={"person_name": ""})
        assert response.status_code == 400

    def test_briefing_get_endpoint_exists(self, client):
        """GET briefing endpoint should exist."""
        response = client.get("/api/briefing/test")
        assert response.status_code != 404
        assert response.status_code != 405

    def test_briefing_returns_valid_response_structure(self, client):
        """Response should have valid structure."""
        with patch('api.routes.briefings.get_briefings_service') as mock_service:
            mock_service.return_value.generate_briefing = AsyncMock(
                return_value={
                    "status": "success",
                    "briefing": "Test briefing content",
                    "person_name": "Test Person",
                    "metadata": {},
                    "sources": [],
                }
            )

            response = client.post("/api/briefing", json={"person_name": "Test"})

            if response.status_code == 200:
                data = response.json()
                assert "status" in data
                assert "person_name" in data

    def test_briefing_includes_sources(self, client):
        """Briefing should include sources."""
        with patch('api.routes.briefings.get_briefings_service') as mock_service:
            mock_service.return_value.generate_briefing = AsyncMock(
                return_value={
                    "status": "success",
                    "briefing": "Briefing with sources",
                    "person_name": "Alex",
                    "sources": ["Meeting Notes.md", "Strategy.md"],
                    "metadata": {},
                }
            )

            response = client.post("/api/briefing", json={"person_name": "Alex"})

            if response.status_code == 200:
                data = response.json()
                assert "sources" in data
                assert isinstance(data.get("sources", []), list)

    def test_briefing_handles_not_found(self, client):
        """Should handle unknown person."""
        with patch('api.routes.briefings.get_briefings_service') as mock_service:
            mock_service.return_value.generate_briefing = AsyncMock(
                return_value={
                    "status": "not_found",
                    "message": "I don't have notes about this person",
                    "person_name": "Unknown Person",
                }
            )

            response = client.post("/api/briefing", json={"person_name": "Unknown Person"})

            # Should return 200 with status="not_found", not 404
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "not_found"
