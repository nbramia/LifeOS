"""
Tests for Admin API endpoints.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow


class TestAdminEndpoints:
    """Test admin API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_status_endpoint_exists(self, client):
        """Status endpoint should exist."""
        # /api/admin/status's VectorStore() call is already wrapped in its
        # own try/except that falls back to document_count=0 (admin.py:46-53)
        # -- these tests only care about the response shape/status, not a
        # real count, so mock VectorStore rather than reaching the live
        # store (#828).
        with patch('api.routes.admin.VectorStore'):
            response = client.get("/api/admin/status")
        assert response.status_code == 200

    def test_status_returns_structure(self, client):
        """Status should return required fields."""
        with patch('api.routes.admin.VectorStore'):
            response = client.get("/api/admin/status")
        data = response.json()

        assert "status" in data
        assert "document_count" in data
        assert "vault_path" in data

    def test_status_ignores_stale_running_reindex_job(self, client):
        """#768: a `reindex_vault` job stuck "running" because the process
        that was executing it restarted mid-job (e.g. an unrelated
        auto-deploy) must not be reported as `status: "reindexing"` — its
        started_at predates this process's own start."""
        from api.services.job_queue import Job

        stale_job = Job(
            id="stale-1", type="reindex_vault", status="running", params={},
            result=None, created_at="2020-01-01T00:00:00+00:00",
            started_at="2020-01-01T00:00:00+00:00", completed_at=None,
            attempts=1, max_attempts=3, priority=10, error=None,
        )
        with patch('api.routes.admin.VectorStore'), \
                patch('api.routes.admin.get_job_queue') as mock_jq:
            mock_jq.return_value.list_jobs.return_value = [stale_job]
            mock_jq.return_value.process_start_time = "2026-08-27T00:00:00+00:00"
            response = client.get("/api/admin/status")
            data = response.json()
            assert data["status"] != "reindexing"

    def test_status_reports_reindexing_for_a_genuinely_running_job(self, client):
        """Regression guard: a job actually claimed by this process (started
        after process_start_time) must still report "reindexing"."""
        from api.services.job_queue import Job

        live_job = Job(
            id="live-1", type="reindex_vault", status="running", params={},
            result=None, created_at="2026-08-27T00:00:01+00:00",
            started_at="2026-08-27T00:00:01+00:00", completed_at=None,
            attempts=1, max_attempts=3, priority=10, error=None,
        )
        with patch('api.routes.admin.VectorStore'), \
                patch('api.routes.admin.get_job_queue') as mock_jq:
            mock_jq.return_value.list_jobs.return_value = [live_job]
            mock_jq.return_value.process_start_time = "2026-08-27T00:00:00+00:00"
            response = client.get("/api/admin/status")
            data = response.json()
            assert data["status"] == "reindexing"

    def test_reindex_endpoint_exists(self, client):
        """Reindex endpoint should exist and enqueue a job."""
        with patch('api.routes.admin.get_job_queue') as mock_jq:
            mock_jq.return_value.list_jobs.return_value = []
            mock_jq.return_value.enqueue.return_value = "job-123"
            response = client.post("/api/admin/reindex")
            assert response.status_code == 200

    def test_reindex_returns_started(self, client):
        """Reindex should return started status with job_id."""
        with patch('api.routes.admin.get_job_queue') as mock_jq:
            mock_jq.return_value.list_jobs.return_value = []
            mock_jq.return_value.enqueue.return_value = "job-456"
            response = client.post("/api/admin/reindex")
            data = response.json()

            assert data["status"] == "started"
            assert data["job_id"] == "job-456"

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
