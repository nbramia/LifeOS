"""
Tests for the Memories API (P6.3).

Tests CRUD operations and Claude synthesis for the /api/memories endpoints.
"""
import pytest
from unittest.mock import patch, AsyncMock

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestMemoriesAPI:
    """Tests for the memories API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_memory_store(self):
        """Mock the memory store."""
        with patch("api.routes.memories.get_memory_store") as mock:
            from datetime import datetime
            from api.services.memory_store import Memory

            store = mock.return_value
            store.create_memory.return_value = Memory(
                id="test-123",
                content="Erika may also be spelled as Erica",
                category="people",
                keywords=["Erika", "Erica", "spelling"],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            store.list_memories.return_value = []
            store.get_memory.return_value = None
            store.delete_memory.return_value = True
            yield store

    @pytest.fixture
    def mock_synthesizer(self):
        """Mock the synthesizer for memory formatting."""
        with patch("api.routes.memories.get_synthesizer") as mock:
            synth = mock.return_value
            synth.get_response = AsyncMock(return_value="Erika may also be spelled as Erica")
            yield synth

    def test_create_memory_without_synthesis(self, client, mock_memory_store, mock_synthesizer):
        """Creating memory with synthesize=False should use raw content."""
        response = client.post(
            "/api/memories",
            json={"content": "Remember Erika spelling", "synthesize": False}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-123"
        assert data["category"] == "people"
        mock_memory_store.create_memory.assert_called_once()

    def test_create_memory_with_synthesis(self, client, mock_memory_store, mock_synthesizer):
        """Creating memory with synthesize=True should format via Claude."""
        response = client.post(
            "/api/memories",
            json={"content": "remember that Erika can be Erica", "synthesize": True}
        )

        assert response.status_code == 200
        mock_synthesizer.get_response.assert_called_once()

    def test_create_memory_empty_content_rejected(self, client):
        """Empty content should be rejected with 400 validation error."""
        response = client.post(
            "/api/memories",
            json={"content": ""}
        )

        # FastAPI returns 422 by default, but LifeOS validation error handler returns 400
        assert response.status_code == 400

    def test_list_memories(self, client, mock_memory_store):
        """Should return list of memories."""
        from datetime import datetime
        from api.services.memory_store import Memory

        mock_memory_store.list_memories.return_value = [
            Memory(
                id="mem-1",
                content="Test memory",
                category="context",
                keywords=["test"],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        ]

        response = client.get("/api/memories")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["memories"]) == 1

    def test_list_memories_by_category(self, client, mock_memory_store):
        """Should filter memories by category."""
        response = client.get("/api/memories?category=people")

        assert response.status_code == 200
        mock_memory_store.list_memories.assert_called_once()
        call_args = mock_memory_store.list_memories.call_args
        assert call_args.kwargs.get("category") == "people"

    def test_get_memory_not_found(self, client, mock_memory_store):
        """Should return 404 for non-existent memory."""
        mock_memory_store.get_memory.return_value = None

        response = client.get("/api/memories/nonexistent-id")

        assert response.status_code == 404

    def test_delete_memory(self, client, mock_memory_store):
        """Should delete memory and return success."""
        response = client.delete("/api/memories/test-123")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        mock_memory_store.delete_memory.assert_called_once_with("test-123")

    def test_delete_memory_not_found(self, client, mock_memory_store):
        """Should return 404 when deleting non-existent memory."""
        mock_memory_store.delete_memory.return_value = False

        response = client.delete("/api/memories/nonexistent")

        assert response.status_code == 404

    def test_search_memories(self, client, mock_memory_store):
        """Should search memories by query."""
        from datetime import datetime
        from api.services.memory_store import Memory

        mock_memory_store.search_memories.return_value = [
            Memory(
                id="mem-1",
                content="Erika spelling",
                category="people",
                keywords=["Erika"],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        ]

        response = client.get("/api/memories/search/Erika")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1


class TestMemorySynthesis:
    """Tests for the Claude memory synthesis prompt."""

    def test_synthesis_prompt_contains_required_elements(self):
        """The synthesis prompt should include key instructions."""
        from api.routes.memories import MEMORY_SYNTHESIS_PROMPT

        assert "rewrite" in MEMORY_SYNTHESIS_PROMPT.lower()
        assert "preserve" in MEMORY_SYNTHESIS_PROMPT.lower()
        assert "alternative spelling" in MEMORY_SYNTHESIS_PROMPT.lower()


class TestMemoryResponse:
    """Tests for the MemoryResponse model."""

    def test_from_memory_creates_response(self):
        """Should create response from Memory object."""
        from datetime import datetime
        from api.services.memory_store import Memory
        from api.routes.memories import MemoryResponse

        memory = Memory(
            id="test-id",
            content="Test content",
            category="context",
            keywords=["test"],
            created_at=datetime(2025, 1, 1, 12, 0),
            updated_at=datetime(2025, 1, 1, 12, 0),
        )

        response = MemoryResponse.from_memory(memory)

        assert response.id == "test-id"
        assert response.content == "Test content"
        assert response.category == "context"
        assert "test" in response.keywords
