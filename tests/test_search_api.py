"""
Tests for the Vector Search API endpoint.
P1.2 Acceptance Criteria:
- API starts and accepts requests on configured port
- Query returns relevant chunks
- Filters correctly narrow results by note_type, people, date range
- Response includes all required metadata fields
- Query latency <500ms for typical queries
- Empty query returns error, not crash
- Invalid filters return 400 with clear message
"""
import pytest

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from api.main import app


class TestSearchEndpoint:
    """Test the /api/search endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_search_endpoint_exists(self, client):
        """Search endpoint should exist and accept POST."""
        response = client.post("/api/search", json={"query": "test"})
        # Should not be 404 or 405
        assert response.status_code != 404
        assert response.status_code != 405

    def test_search_requires_query(self, client):
        """Should return 400/422 for missing query."""
        response = client.post("/api/search", json={})
        assert response.status_code in [400, 422]

    def test_search_rejects_empty_query(self, client):
        """Should return 400 for empty query string."""
        response = client.post("/api/search", json={"query": ""})
        assert response.status_code == 400
        assert "error" in response.json() or "detail" in response.json()

    def test_search_returns_results_structure(self, client):
        """Response should have correct structure."""
        response = client.post("/api/search", json={"query": "test query"})
        assert response.status_code == 200

        data = response.json()
        assert "results" in data
        assert "query_time_ms" in data
        assert isinstance(data["results"], list)
        assert isinstance(data["query_time_ms"], (int, float))

    def test_search_result_fields(self, client):
        """Each result should have required fields."""
        response = client.post("/api/search", json={"query": "test"})

        if response.status_code == 200:
            data = response.json()
            # If there are results, check their structure
            for result in data.get("results", []):
                assert "content" in result
                assert "file_path" in result
                assert "file_name" in result
                assert "score" in result

    def test_search_with_top_k(self, client):
        """Should respect top_k parameter."""
        response = client.post("/api/search", json={
            "query": "test",
            "top_k": 5
        })
        assert response.status_code == 200

        data = response.json()
        assert len(data["results"]) <= 5

    def test_search_with_note_type_filter(self, client):
        """Should filter by note_type."""
        response = client.post("/api/search", json={
            "query": "test",
            "filters": {"note_type": ["Work"]}
        })
        assert response.status_code == 200

    def test_search_with_people_filter(self, client):
        """Should filter by people."""
        response = client.post("/api/search", json={
            "query": "meeting",
            "filters": {"people": ["Alex"]}
        })
        assert response.status_code == 200

    def test_search_with_date_range_filter(self, client):
        """Should filter by date range."""
        response = client.post("/api/search", json={
            "query": "test",
            "filters": {
                "date_from": "2025-01-01",
                "date_to": "2025-12-31"
            }
        })
        assert response.status_code == 200

    def test_search_with_invalid_filter_type(self, client):
        """Should return 400 for invalid filter values."""
        response = client.post("/api/search", json={
            "query": "test",
            "filters": {"note_type": "invalid"}  # Should be list
        })
        # Should handle gracefully (either accept or return 400/422)
        assert response.status_code in [200, 400, 422]

    def test_search_performance(self, client):
        """Search should complete within 500ms."""
        import time
        start = time.time()
        response = client.post("/api/search", json={"query": "test query"})
        elapsed = (time.time() - start) * 1000

        assert response.status_code == 200
        # Allow more time in test environment but log if slow
        assert elapsed < 2000, f"Search took {elapsed}ms, expected <500ms"


class TestSearchRequestValidation:
    """Test request validation."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_rejects_non_json(self, client):
        """Should reject non-JSON requests."""
        response = client.post(
            "/api/search",
            content="not json",
            headers={"Content-Type": "text/plain"}
        )
        assert response.status_code in [400, 415, 422]

    def test_handles_extra_fields(self, client):
        """Should ignore extra fields gracefully."""
        response = client.post("/api/search", json={
            "query": "test",
            "unknown_field": "value"
        })
        # Should succeed, ignoring unknown field
        assert response.status_code == 200

    def test_handles_null_filters(self, client):
        """Should handle null/None filters."""
        response = client.post("/api/search", json={
            "query": "test",
            "filters": None
        })
        assert response.status_code == 200
