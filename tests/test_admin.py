"""
Tests for Admin API endpoints.
"""
import pytest

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from api.main import app


class TestAdminEndpoints:
    """Test admin API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_status_endpoint_exists(self, client):
        """Status endpoint should exist."""
        response = client.get("/api/admin/status")
        assert response.status_code == 200

    def test_status_returns_structure(self, client):
        """Status should return required fields."""
        response = client.get("/api/admin/status")
        data = response.json()

        assert "status" in data
        assert "document_count" in data
        assert "vault_path" in data

    def test_reindex_endpoint_exists(self, client):
        """Reindex endpoint should exist."""
        with patch('api.routes.admin._do_reindex'):
            response = client.post("/api/admin/reindex")
            assert response.status_code == 200

    def test_reindex_returns_started(self, client):
        """Reindex should return started status."""
        with patch('api.routes.admin._do_reindex'):
            response = client.post("/api/admin/reindex")
            data = response.json()

            assert "status" in data
            assert data["status"] in ["started", "already_running"]

    def test_reindex_sync_endpoint_exists(self, client):
        """Sync reindex endpoint should exist."""
        with patch('api.routes.admin.IndexerService') as mock_indexer:
            mock_indexer.return_value.index_all.return_value = 100
            response = client.post("/api/admin/reindex/sync")
            assert response.status_code == 200

    def test_reindex_sync_returns_count(self, client):
        """Sync reindex should return file count."""
        with patch('api.routes.admin.IndexerService') as mock_indexer:
            mock_indexer.return_value.index_all.return_value = 4726
            response = client.post("/api/admin/reindex/sync")
            data = response.json()

            assert data["status"] == "success"
            assert data["files_indexed"] == 4726
