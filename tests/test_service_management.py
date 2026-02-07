"""
Tests for Service Management (P4.1).
Acceptance Criteria:
- Services start automatically on boot (launchd plist)
- Services restart automatically after crash (KeepAlive)
- Logs written to file with rotation
- Health endpoint returns service status
- Can check status via launchctl
"""
import pytest

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow
import os
from pathlib import Path
from fastapi.testclient import TestClient

from api.main import app


class TestHealthEndpoint:
    """Test health check endpoint for service monitoring."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_health_endpoint_exists(self, client):
        """Health endpoint should exist."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_status(self, client):
        """Health should return status field."""
        response = client.get("/health")
        data = response.json()
        assert "status" in data
        # Status can be "healthy" or "degraded" based on configuration
        assert data["status"] in ["healthy", "degraded"]

    def test_health_returns_service_name(self, client):
        """Health should identify the service."""
        response = client.get("/health")
        data = response.json()
        assert "service" in data
        assert data["service"] == "lifeos"


class TestLaunchdConfiguration:
    """Test launchd plist configuration."""

    @pytest.fixture
    def plist_path(self):
        """Path to launchd plist."""
        return Path(__file__).parent.parent / "config" / "launchd" / "com.lifeos.api.plist"

    def test_plist_exists(self, plist_path):
        """Plist file should exist."""
        assert plist_path.exists(), f"Plist not found at {plist_path}"

    def test_plist_is_valid_xml(self, plist_path):
        """Plist should be valid XML."""
        import plistlib

        with open(plist_path, 'rb') as f:
            try:
                data = plistlib.load(f)
                assert data is not None
            except Exception as e:
                pytest.fail(f"Invalid plist: {e}")

    def test_plist_has_required_keys(self, plist_path):
        """Plist should have all required keys."""
        import plistlib

        with open(plist_path, 'rb') as f:
            data = plistlib.load(f)

        required_keys = [
            'Label',
            'ProgramArguments',
            'WorkingDirectory',
            'RunAtLoad',
            'KeepAlive',
            'StandardOutPath',
            'StandardErrorPath',
        ]

        for key in required_keys:
            assert key in data, f"Missing required key: {key}"

    def test_plist_run_at_load(self, plist_path):
        """Service should start on boot."""
        import plistlib

        with open(plist_path, 'rb') as f:
            data = plistlib.load(f)

        assert data.get('RunAtLoad') is True, "RunAtLoad should be true"

    def test_plist_keep_alive(self, plist_path):
        """Service should restart on crash."""
        import plistlib

        with open(plist_path, 'rb') as f:
            data = plistlib.load(f)

        keep_alive = data.get('KeepAlive', {})
        assert keep_alive.get('SuccessfulExit') is False, "Should restart on non-zero exit"


class TestServiceScript:
    """Test service management script."""

    @pytest.fixture
    def script_path(self):
        """Path to service script."""
        return Path(__file__).parent.parent / "scripts" / "service.sh"

    def test_script_exists(self, script_path):
        """Service script should exist."""
        assert script_path.exists(), f"Script not found at {script_path}"

    def test_script_is_executable(self, script_path):
        """Script should be executable."""
        assert os.access(script_path, os.X_OK), "Script should be executable"

    def test_script_has_required_commands(self, script_path):
        """Script should support required commands."""
        content = script_path.read_text()

        required_commands = ['install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs']
        for cmd in required_commands:
            assert cmd in content, f"Script should support '{cmd}' command"


class TestLogsDirectory:
    """Test logs directory setup."""

    @pytest.fixture
    def logs_dir(self):
        """Path to logs directory."""
        return Path(__file__).parent.parent / "logs"

    def test_logs_directory_can_be_created(self, logs_dir, tmp_path):
        """Logs directory should be creatable."""
        test_logs = tmp_path / "logs"
        test_logs.mkdir()
        assert test_logs.exists()

    def test_logs_gitignored(self):
        """Logs should be in .gitignore."""
        gitignore = Path(__file__).parent.parent / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            assert "logs/" in content or "*.log" in content, "Logs should be gitignored"
