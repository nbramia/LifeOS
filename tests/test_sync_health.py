"""
Tests for sync health monitoring system.

Ensures all data sources remain in sync (at least daily) and errors are visible.
"""
import pytest
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.sync_health import (
    SYNC_SOURCES,
    SYNC_STALE_HOURS,
    SyncStatus,
    SyncHealth,
    SyncResult,
    get_sync_health_db,
    record_sync_start,
    record_sync_complete,
    record_sync_error,
    get_sync_health,
    get_all_sync_health,
    get_stale_syncs,
    get_failed_syncs,
    get_recent_errors,
    get_sync_summary,
    check_sync_health,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary sync health database."""
    db_path = tmp_path / "sync_health.db"
    with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', db_path):
        conn = get_sync_health_db()
        conn.close()
        yield db_path


class TestSyncHealthRecording:
    """Tests for recording sync operations."""

    def test_record_sync_start(self, temp_db):
        """Test recording the start of a sync."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            run_id = record_sync_start("gmail")

            assert run_id > 0

            conn = get_sync_health_db()
            row = conn.execute(
                "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.close()

            assert row["source"] == "gmail"
            assert row["status"] == "running"
            assert row["started_at"] is not None

    def test_record_sync_complete_success(self, temp_db):
        """Test recording successful sync completion."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            run_id = record_sync_start("calendar")

            record_sync_complete(
                run_id,
                SyncStatus.SUCCESS,
                records_processed=100,
                records_created=50,
                records_updated=25,
                errors=0,
            )

            conn = get_sync_health_db()
            row = conn.execute(
                "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.close()

            assert row["status"] == "success"
            assert row["records_processed"] == 100
            assert row["records_created"] == 50
            assert row["records_updated"] == 25
            assert row["errors"] == 0
            assert row["completed_at"] is not None
            assert row["duration_seconds"] is not None

    def test_record_sync_complete_failure(self, temp_db):
        """Test recording failed sync completion."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            run_id = record_sync_start("phone")

            record_sync_complete(
                run_id,
                SyncStatus.FAILED,
                errors=1,
                error_message="Connection refused",
            )

            conn = get_sync_health_db()
            row = conn.execute(
                "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.close()

            assert row["status"] == "failed"
            assert row["errors"] == 1
            assert row["error_message"] == "Connection refused"

    def test_record_sync_error(self, temp_db):
        """Test recording sync errors."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            record_sync_error(
                "whatsapp",
                "wacli not found",
                error_type="FileNotFoundError",
                stack_trace="Traceback...",
                context="Running sync_whatsapp.py",
            )

            errors = get_recent_errors("whatsapp")
            assert len(errors) == 1
            assert errors[0]["source"] == "whatsapp"
            assert errors[0]["error_message"] == "wacli not found"
            assert errors[0]["error_type"] == "FileNotFoundError"


class TestSyncHealthQueries:
    """Tests for querying sync health."""

    def test_get_sync_health_fresh(self, temp_db):
        """Test getting health for a freshly synced source."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Record a recent successful sync
            run_id = record_sync_start("gmail")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            health = get_sync_health("gmail")

            assert health.source == "gmail"
            assert health.is_stale is False
            assert health.last_status == SyncStatus.SUCCESS
            assert health.hours_since_sync is not None
            assert health.hours_since_sync < 1

    def test_get_sync_health_stale(self, temp_db):
        """Test getting health for a stale source."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Insert an old sync record directly
            conn = get_sync_health_db()
            old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
            conn.execute(
                """
                INSERT INTO sync_runs (source, status, started_at, completed_at)
                VALUES (?, ?, ?, ?)
                """,
                ("calendar", "success", old_time, old_time)
            )
            conn.commit()
            conn.close()

            health = get_sync_health("calendar")

            assert health.is_stale is True
            assert health.hours_since_sync > SYNC_STALE_HOURS

    def test_get_sync_health_never_run(self, temp_db):
        """Test getting health for a source that has never been synced."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            health = get_sync_health("gmail")

            assert health.is_stale is True
            assert health.last_sync is None
            assert health.last_status is None

    def test_get_all_sync_health(self, temp_db):
        """Test getting health for all sources."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            all_health = get_all_sync_health()

            assert len(all_health) == len(SYNC_SOURCES)
            assert all(isinstance(h, SyncHealth) for h in all_health)

    def test_get_stale_syncs(self, temp_db):
        """Test getting stale syncs."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Fresh sync for gmail
            run_id = record_sync_start("gmail")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            # No sync for others - they should be stale
            stale = get_stale_syncs()

            assert len(stale) >= len(SYNC_SOURCES) - 1
            assert "gmail" not in [s.source for s in stale]

    def test_get_failed_syncs(self, temp_db):
        """Test getting failed syncs."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Record a failed sync
            run_id = record_sync_start("phone")
            record_sync_complete(run_id, SyncStatus.FAILED, errors=1)

            failed = get_failed_syncs(hours=24)

            assert len(failed) == 1
            assert failed[0]["source"] == "phone"


class TestSyncHealthSummary:
    """Tests for sync health summary."""

    def test_get_sync_summary_all_healthy(self, temp_db):
        """Test summary when all sources are healthy."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Sync all sources
            for source in SYNC_SOURCES.keys():
                run_id = record_sync_start(source)
                record_sync_complete(run_id, SyncStatus.SUCCESS)

            summary = get_sync_summary()

            assert summary["all_healthy"] is True
            assert summary["stale"] == 0
            assert summary["failed"] == 0
            assert summary["healthy"] == len(SYNC_SOURCES)

    def test_get_sync_summary_with_issues(self, temp_db):
        """Test summary when some sources have issues."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # One fresh success
            run_id = record_sync_start("gmail")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            # One failure
            run_id = record_sync_start("phone")
            record_sync_complete(run_id, SyncStatus.FAILED)

            # Rest are never run (stale)

            summary = get_sync_summary()

            assert summary["all_healthy"] is False
            assert summary["healthy"] == 1
            assert summary["failed"] == 1
            assert "phone" in summary["failed_sources"]
            assert len(summary["never_run_sources"]) > 0

    def test_check_sync_health_healthy(self, temp_db):
        """Test health check when all is well."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            for source in SYNC_SOURCES.keys():
                run_id = record_sync_start(source)
                record_sync_complete(run_id, SyncStatus.SUCCESS)

            is_healthy, message = check_sync_health()

            assert is_healthy is True
            assert "healthy" in message.lower()

    def test_check_sync_health_unhealthy(self, temp_db):
        """Test health check when there are issues."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Only sync one source
            run_id = record_sync_start("gmail")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            is_healthy, message = check_sync_health()

            assert is_healthy is False
            assert "never run" in message.lower() or "stale" in message.lower()


class TestSyncSourceConfiguration:
    """Tests for sync source configuration."""

    def test_all_sources_have_required_fields(self):
        """Test that all sync sources have required configuration."""
        required_fields = ["description", "script", "frequency"]

        for source, config in SYNC_SOURCES.items():
            for field in required_fields:
                assert field in config, f"Source {source} missing {field}"

    def test_all_sources_have_valid_frequency(self):
        """Test that all sync sources have valid frequency."""
        valid_frequencies = ["daily", "weekly", "hourly"]

        for source, config in SYNC_SOURCES.items():
            assert config["frequency"] in valid_frequencies, \
                f"Source {source} has invalid frequency: {config['frequency']}"

    def test_all_scripts_exist(self):
        """Test that all configured sync scripts exist."""
        project_root = Path(__file__).parent.parent

        for source, config in SYNC_SOURCES.items():
            script_path = project_root / config["script"]
            assert script_path.exists(), \
                f"Script not found for {source}: {config['script']}"


class TestSyncHealthIntegration:
    """Integration tests for sync health system."""

    def test_full_sync_lifecycle(self, temp_db):
        """Test complete sync lifecycle: start → progress → complete."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Start sync
            run_id = record_sync_start("gmail")

            # Check it's running
            conn = get_sync_health_db()
            row = conn.execute(
                "SELECT status FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert row["status"] == "running"
            conn.close()

            # Complete sync
            record_sync_complete(
                run_id,
                SyncStatus.SUCCESS,
                records_processed=1000,
                records_created=100,
                records_updated=50,
            )

            # Check health
            health = get_sync_health("gmail")
            assert health.is_stale is False
            assert health.last_status == SyncStatus.SUCCESS

            # Check summary
            summary = get_sync_summary()
            assert "gmail" not in summary["stale_sources"]
            assert "gmail" not in summary["failed_sources"]

    def test_multiple_syncs_for_same_source(self, temp_db):
        """Test that we track the most recent sync."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # First sync - fails
            run_id1 = record_sync_start("calendar")
            record_sync_complete(run_id1, SyncStatus.FAILED, error_message="First error")

            # Second sync - succeeds
            run_id2 = record_sync_start("calendar")
            record_sync_complete(run_id2, SyncStatus.SUCCESS)

            # Health should show success
            health = get_sync_health("calendar")
            assert health.last_status == SyncStatus.SUCCESS
            assert health.last_error is None


class TestSyncHealthDailyCheck:
    """Tests that verify daily sync requirements."""

    def test_stale_detection_threshold(self, temp_db):
        """Test that staleness is detected at exactly 24 hours."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            conn = get_sync_health_db()

            # 23 hours ago - should be fresh
            fresh_time = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
            conn.execute(
                "INSERT INTO sync_runs (source, status, started_at, completed_at) VALUES (?, ?, ?, ?)",
                ("gmail", "success", fresh_time, fresh_time)
            )

            # 25 hours ago - should be stale
            stale_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            conn.execute(
                "INSERT INTO sync_runs (source, status, started_at, completed_at) VALUES (?, ?, ?, ?)",
                ("calendar", "success", stale_time, stale_time)
            )
            conn.commit()
            conn.close()

            gmail_health = get_sync_health("gmail")
            calendar_health = get_sync_health("calendar")

            assert gmail_health.is_stale is False
            assert calendar_health.is_stale is True

    def test_all_sources_have_valid_sync_frequency(self):
        """Verify that all sources have appropriate sync frequency."""
        # This test documents the requirement that all sources
        # should be synced at appropriate intervals
        assert SYNC_STALE_HOURS == 24, "Stale threshold must be 24 hours"

        # Most sources should sync daily, some can be weekly
        allowed_frequencies = ["daily", "hourly", "weekly"]
        weekly_allowed = ["contacts"]  # Contacts don't change often

        for source, config in SYNC_SOURCES.items():
            assert config["frequency"] in allowed_frequencies, \
                f"Source {source} has invalid frequency: {config['frequency']}"

            if source not in weekly_allowed:
                assert config["frequency"] in ["daily", "hourly"], \
                    f"Source {source} must sync at least daily, not {config['frequency']}"
