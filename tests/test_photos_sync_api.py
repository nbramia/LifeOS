"""
Tests for the Apple Photos sync endpoint (POST /api/photos/sync).
"""
import pytest
from unittest.mock import patch, PropertyMock

pytestmark = pytest.mark.unit


class TestPhotosSyncEndpoint:
    """Tests for POST /api/photos/sync."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def photos_enabled(self):
        """Photos routes gate on `settings.photos_enabled`, a property
        derived from whether the Photos library file exists on disk — force
        it on so the sync handler itself gets exercised."""
        from config.settings import Settings
        with patch.object(
            Settings, "photos_enabled", new_callable=PropertyMock, return_value=True
        ):
            yield

    def test_sync_success(self, client, photos_enabled):
        """A successful sync should not carry a top-level `error` key."""
        with patch(
            "api.services.apple_photos_sync.sync_apple_photos",
            return_value={"person_matches": 3, "interactions_created": 5},
        ):
            response = client.post("/api/photos/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "error" not in data

    def test_sync_failure_carries_top_level_error(self, client, photos_enabled):
        """#609: a sync failure stays a 200 (the request WAS served — see
        #614 for whether that should change) but must carry a top-level
        `error` key, since `mcp_server.py: dispatch()` and the agent
        worker's `ToolRegistry` both key off exactly that to flag a result
        as failed. Previously the failure detail only lived nested inside
        `stats["error"]`, invisible to that generic check."""
        with patch(
            "api.services.apple_photos_sync.sync_apple_photos",
            side_effect=RuntimeError("Photos library locked"),
        ):
            response = client.post("/api/photos/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data
        assert "Photos library locked" in data["error"]
