"""
Tests for sync health monitoring system.

Ensures all data sources remain in sync (at least daily) and errors are visible.
"""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.sync_health import (
    SYNC_SOURCES,
    SYNC_STALE_HOURS,
    SyncStatus,
    SyncHealth,
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
    reap_orphan_sync_runs,
    emit_sync_stats,
    detect_silent_source_entity_drift,
)

pytestmark = pytest.mark.unit


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
                "imessage",
                "Database locked",
                error_type="OperationalError",
                stack_trace="Traceback...",
                context="Running sync_imessage_interactions.py",
            )

            errors = get_recent_errors("imessage")
            assert len(errors) == 1
            assert errors[0]["source"] == "imessage"
            assert errors[0]["error_message"] == "Database locked"
            assert errors[0]["error_type"] == "OperationalError"


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
        from api.services.sync_health import _is_source_disabled
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Fresh sync for gmail
            run_id = record_sync_start("gmail")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            # No sync for others - non-disabled sources should be stale
            stale = get_stale_syncs()

            # Count expected: never-run sources minus disabled (which are not flagged)
            expected_stale = sum(
                1 for s in SYNC_SOURCES.keys()
                if s != "gmail" and not _is_source_disabled(s)
            )
            assert len(stale) >= expected_stale
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
        """Test summary when all enabled sources are healthy."""
        from api.services.sync_health import _is_source_disabled
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # Sync all sources (disabled ones still get a row but the summary excludes them)
            for source in SYNC_SOURCES.keys():
                run_id = record_sync_start(source)
                record_sync_complete(run_id, SyncStatus.SUCCESS)

            summary = get_sync_summary()
            expected_healthy = sum(1 for s in SYNC_SOURCES.keys() if not _is_source_disabled(s))

            assert summary["all_healthy"] is True
            assert summary["stale"] == 0
            assert summary["failed"] == 0
            assert summary["healthy"] == expected_healthy

    def test_get_sync_summary_with_issues(self, temp_db):
        """Test summary when some sources have issues."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            # One fresh success — use vault_reindex (never conditionally
            # disabled, unlike gmail_personal since #687: a dev checkout
            # with no config/credentials-personal.json now correctly
            # reports that source as disabled, so it can no longer stand
            # in for "a source that's always healthy" here).
            run_id = record_sync_start("vault_reindex")
            record_sync_complete(run_id, SyncStatus.SUCCESS)

            # One failure — use imessage (never platform-disabled in the disabled list)
            run_id = record_sync_start("imessage")
            record_sync_complete(run_id, SyncStatus.FAILED)

            # Rest are never run (stale)

            summary = get_sync_summary()

            assert summary["all_healthy"] is False
            assert summary["healthy"] == 1
            assert summary["failed"] == 1
            assert "imessage" in summary["failed_sources"]
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

    def test_check_sync_health_healthy_breakdown_when_disabled(self, temp_db):
        """When some tracked sources are disabled, the healthy message must
        break the total down (active vs disabled) so it doesn't read as
        contradicting a nightly 'Total sources: N' line that only counts the
        sources actually run (issue #494 follow-up)."""
        summary = {
            "all_healthy": True,
            "total_sources": 28,
            "enabled_sources": 27,
            "disabled": 1,
        }
        with patch('api.services.sync_health.get_sync_summary', return_value=summary):
            is_healthy, message = check_sync_health()

        assert is_healthy is True
        assert "28" in message
        assert "27" in message
        assert "1" in message
        assert "healthy" in message.lower()

    def test_check_sync_health_healthy_no_disabled_sources(self, temp_db):
        """When nothing is disabled, keep the original simple message."""
        summary = {
            "all_healthy": True,
            "total_sources": 28,
            "enabled_sources": 28,
            "disabled": 0,
        }
        with patch('api.services.sync_health.get_sync_summary', return_value=summary):
            is_healthy, message = check_sync_health()

        assert is_healthy is True
        assert message == "All 28 sources are healthy"

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
        valid_frequencies = ["daily", "weekly", "hourly", "monthly"]

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


class TestPersonalGoogleDisabledCheck:
    """Direct tests of _is_source_disabled()'s personal-Google handling
    (issue #687). Without this, an unconfigured install would show
    gmail_personal/calendar_personal as permanently "never run" in the
    health summary instead of quietly excluded, since run_all_syncs
    pre-skips the source and never writes a sync_runs row for it.

    sync_health.py locates the repo root via __file__ (three parents up
    from api/services/sync_health.py), not cwd, so these point __file__ at
    a fake tree rather than chdir-ing.
    """

    def _fake_module_file(self, fake_root):
        return str(fake_root / "api" / "services" / "sync_health.py")

    def test_disabled_when_credentials_file_absent(self, tmp_path, monkeypatch):
        import api.services.sync_health as mod

        (tmp_path / "config").mkdir()
        monkeypatch.setattr(mod, "__file__", self._fake_module_file(tmp_path))

        assert mod._is_source_disabled("gmail_personal") is True
        assert mod._is_source_disabled("calendar_personal") is True

    def test_not_disabled_when_credentials_file_present(self, tmp_path, monkeypatch):
        """Behavior-neutrality: a configured install (credentials file
        present, today's status quo) must see no change."""
        import api.services.sync_health as mod

        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "credentials-personal.json").write_text("{}")
        monkeypatch.setattr(mod, "__file__", self._fake_module_file(tmp_path))

        assert mod._is_source_disabled("gmail_personal") is False
        assert mod._is_source_disabled("calendar_personal") is False

    def test_unrelated_source_unaffected(self, tmp_path, monkeypatch):
        """The new check must only ever apply to the two personal-Google
        sources, never leak into an unrelated source's disabled-ness."""
        import api.services.sync_health as mod

        (tmp_path / "config").mkdir()  # credentials-personal.json absent
        monkeypatch.setattr(mod, "__file__", self._fake_module_file(tmp_path))

        # imessage has no Google-account dependency at all, so it must stay
        # unaffected by the personal-credentials check either way.
        assert mod._is_source_disabled("imessage") is False


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


class TestOrphanReaper:
    """Tests for reap_orphan_sync_runs — cleans up rows from killed processes."""

    def test_reaps_old_running_rows(self, temp_db):
        """Rows in status=running older than max_age_hours are marked failed."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            conn = get_sync_health_db()
            # 10h ago — should be reaped at default 8h cutoff. vault_reindex
            # (not gmail_personal) since #687: a dev checkout with no
            # config/credentials-personal.json now correctly reports that
            # source as disabled, and get_sync_health() deliberately doesn't
            # surface last_status for a disabled source.
            old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
            conn.execute(
                "INSERT INTO sync_runs (source, status, started_at) VALUES (?, ?, ?)",
                ("vault_reindex", "running", old),
            )
            conn.commit()
            conn.close()

            reaped = reap_orphan_sync_runs()
            assert reaped == 1

            health = get_sync_health("vault_reindex")
            assert health.last_status == SyncStatus.FAILED

    def test_leaves_fresh_running_rows_alone(self, temp_db):
        """A sync that started 1h ago is still legitimately running."""
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            conn = get_sync_health_db()
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            conn.execute(
                "INSERT INTO sync_runs (source, status, started_at) VALUES (?, ?, ?)",
                ("gmail_personal", "running", recent),
            )
            conn.commit()
            conn.close()

            reaped = reap_orphan_sync_runs()
            assert reaped == 0

    def test_returns_zero_when_no_orphans(self, temp_db):
        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            assert reap_orphan_sync_runs() == 0


class TestSourceEntityDriftDetector:
    """Tests for the silent-regression detector from issue #199 §2."""

    def _seed(self, tmp_path, interactions: list, source_entities: list):
        """Seed minimal interactions.db / crm.db with given rows."""
        import sqlite3
        i_path = tmp_path / "interactions.db"
        c_path = tmp_path / "crm.db"
        i_conn = sqlite3.connect(str(i_path))
        i_conn.execute(
            "CREATE TABLE interactions (id TEXT, source_type TEXT, created_at TEXT)"
        )
        i_conn.executemany(
            "INSERT INTO interactions (id, source_type, created_at) VALUES (?, ?, ?)",
            interactions,
        )
        i_conn.commit()
        i_conn.close()

        c_conn = sqlite3.connect(str(c_path))
        c_conn.execute(
            "CREATE TABLE source_entities (id TEXT, source_type TEXT, created_at TEXT)"
        )
        c_conn.executemany(
            "INSERT INTO source_entities (id, source_type, created_at) VALUES (?, ?, ?)",
            source_entities,
        )
        c_conn.commit()
        c_conn.close()
        return str(i_path), str(c_path)

    def test_flags_drift_when_interactions_flow_but_entities_stale(self, tmp_path):
        now = datetime.now(timezone.utc)
        recent_iso = now.isoformat()
        stale_iso = (now - timedelta(days=60)).isoformat()

        i_path, c_path = self._seed(
            tmp_path,
            interactions=[("i1", "imessage", recent_iso)],
            source_entities=[("se1", "imessage", stale_iso)],
        )

        warnings = detect_silent_source_entity_drift(
            interactions_db=i_path, crm_db=c_path
        )
        assert len(warnings) == 1
        assert warnings[0]["source"] == "imessage"
        assert warnings[0]["gap_days"] > 30

    def test_quiet_source_does_not_trip(self, tmp_path):
        """A source with no recent interactions shouldn't be flagged at all."""
        now = datetime.now(timezone.utc)
        old_interaction = (now - timedelta(days=90)).isoformat()
        stale_iso = (now - timedelta(days=60)).isoformat()

        i_path, c_path = self._seed(
            tmp_path,
            interactions=[("i1", "slack", old_interaction)],
            source_entities=[("se1", "slack", stale_iso)],
        )
        # Default lookback = 7 days; slack interaction is 90 days old, so it
        # isn't considered "active" and shouldn't warn.
        warnings = detect_silent_source_entity_drift(
            interactions_db=i_path, crm_db=c_path
        )
        assert warnings == []

    def test_healthy_source_does_not_trip(self, tmp_path):
        now = datetime.now(timezone.utc)
        recent_iso = now.isoformat()

        i_path, c_path = self._seed(
            tmp_path,
            interactions=[("i1", "gmail", recent_iso)],
            source_entities=[("se1", "gmail", recent_iso)],
        )
        assert detect_silent_source_entity_drift(
            interactions_db=i_path, crm_db=c_path
        ) == []

    def test_missing_dbs_returns_empty(self, tmp_path):
        """Fresh install or test environment without dbs: no false alerts."""
        assert detect_silent_source_entity_drift(
            interactions_db=str(tmp_path / "nope.db"),
            crm_db=str(tmp_path / "also-nope.db"),
        ) == []


class TestEmitSyncStats:
    """Tests for emit_sync_stats — the canonical stats-reporting helper."""

    def test_prints_machine_readable_line(self, capsys):
        emit_sync_stats({"interactions_created": 5, "source_entities_created": 2})
        captured = capsys.readouterr()
        # Single line, parseable by the orchestrator's regex.
        import re
        import json
        match = re.search(r"SYNC_STATS:(\{.*\})", captured.out)
        assert match, f"Expected SYNC_STATS line in: {captured.out!r}"
        payload = json.loads(match.group(1))
        assert payload["interactions_created"] == 5
        assert payload["source_entities_created"] == 2


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

        # Most sources should sync daily; contacts weekly, financial monthly
        allowed_frequencies = ["daily", "hourly", "weekly", "monthly"]
        less_frequent_allowed = {
            "contacts": "weekly",        # Contacts don't change often
            "monarch_money": "monthly",  # Financial summaries are monthly
        }

        for source, config in SYNC_SOURCES.items():
            assert config["frequency"] in allowed_frequencies, \
                f"Source {source} has invalid frequency: {config['frequency']}"

            if source in less_frequent_allowed:
                assert config["frequency"] == less_frequent_allowed[source], \
                    f"Source {source} should be {less_frequent_allowed[source]}, not {config['frequency']}"
            else:
                assert config["frequency"] in ["daily", "hourly"], \
                    f"Source {source} must sync at least daily, not {config['frequency']}"


class TestTypicalDuration:
    """Tests for get_typical_duration_seconds (duration-collapse detection)."""

    def _insert_run(self, source, status, duration, started_at):
        conn = get_sync_health_db()
        conn.execute(
            """
            INSERT INTO sync_runs (source, status, started_at, completed_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, status, started_at.isoformat(), started_at.isoformat(), duration),
        )
        conn.commit()
        conn.close()

    def test_median_of_recent_successful_runs(self, temp_db):
        """Returns the median duration of recent successful runs."""
        from api.services.sync_health import get_typical_duration_seconds

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            for i, duration in enumerate([400.0, 450.0, 500.0]):
                self._insert_run("slack", "success", duration, base - timedelta(days=i))

            assert get_typical_duration_seconds("slack") == 450.0

    def test_ignores_failed_runs(self, temp_db):
        """Failed runs don't contribute to the typical duration."""
        from api.services.sync_health import get_typical_duration_seconds

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            self._insert_run("slack", "success", 400.0, base - timedelta(days=2))
            self._insert_run("slack", "failed", 3.0, base - timedelta(days=1))

            assert get_typical_duration_seconds("slack") == 400.0

    def test_ignores_collapsed_durations(self, temp_db):
        """Sub-threshold 'successes' (the silent no-ops we're hunting) don't
        poison the typical duration."""
        from api.services.sync_health import get_typical_duration_seconds

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            # 5 recent broken runs (silent no-ops) after 2 real ones
            for i in range(5):
                self._insert_run("slack", "success", 0.28, base - timedelta(days=i))
            self._insert_run("slack", "success", 420.0, base - timedelta(days=6))
            self._insert_run("slack", "success", 480.0, base - timedelta(days=7))

            assert get_typical_duration_seconds("slack") == 450.0

    def test_returns_none_without_history(self, temp_db):
        """Returns None when the source has no eligible successful runs."""
        from api.services.sync_health import get_typical_duration_seconds

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            assert get_typical_duration_seconds("slack") is None

            base = datetime.now(timezone.utc)
            self._insert_run("slack", "failed", 300.0, base)
            assert get_typical_duration_seconds("slack") is None

    def test_only_considers_last_n_runs(self, temp_db):
        """Old history beyond the window doesn't affect the median."""
        from api.services.sync_health import get_typical_duration_seconds

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            # 5 recent fast-but-legitimate runs (> collapse floor)
            for i in range(5):
                self._insert_run("gmail", "success", 100.0, base - timedelta(days=i))
            # Ancient slow runs outside the window
            for i in range(5, 10):
                self._insert_run("gmail", "success", 5000.0, base - timedelta(days=i))

            assert get_typical_duration_seconds("gmail", n=5) == 100.0


class TestRepeatedYieldStreak:
    """Tests for get_repeated_yield_streak (issue #646).

    A dead export agent that leaves a stale upstream file in place makes a
    re-import report the same non-zero count night after night — invisible
    to yield-collapse (which only fires on zero) and to never-yielded
    (which only fires when nothing was EVER produced). This is the detector
    for "reports the same thing every time," the signature defect #2 in the
    linked issue: ten consecutive nights of an identical "1294 created."
    """

    def _insert_run(self, source, status, created, started_at):
        conn = get_sync_health_db()
        conn.execute(
            """
            INSERT INTO sync_runs (source, status, started_at, completed_at, records_created)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, status, started_at.isoformat(), started_at.isoformat(), created),
        )
        conn.commit()
        conn.close()

    def test_streak_of_identical_value(self, temp_db):
        from api.services.sync_health import get_repeated_yield_streak

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            for i in range(5):
                self._insert_run("apple_import", "success", 1294, base - timedelta(days=i))

            assert get_repeated_yield_streak("apple_import", 1294) == 5

    def test_streak_stops_at_first_different_value(self, temp_db):
        """A genuinely varying night (real new data) breaks the streak."""
        from api.services.sync_health import get_repeated_yield_streak

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            # 3 most-recent identical runs, then a different value further back
            for i in range(3):
                self._insert_run("apple_import", "success", 1294, base - timedelta(days=i))
            self._insert_run("apple_import", "success", 800, base - timedelta(days=3))
            self._insert_run("apple_import", "success", 1294, base - timedelta(days=4))

            assert get_repeated_yield_streak("apple_import", 1294) == 3

    def test_no_matching_value_returns_zero(self, temp_db):
        from api.services.sync_health import get_repeated_yield_streak

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            self._insert_run("apple_import", "success", 500, base)

            assert get_repeated_yield_streak("apple_import", 1294) == 0

    def test_no_history_returns_zero(self, temp_db):
        from api.services.sync_health import get_repeated_yield_streak

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            assert get_repeated_yield_streak("apple_import", 1294) == 0

    def test_failed_runs_excluded(self, temp_db):
        """Only successful runs count toward the streak."""
        from api.services.sync_health import get_repeated_yield_streak

        with patch('api.services.sync_health.SYNC_HEALTH_DB_PATH', temp_db):
            base = datetime.now(timezone.utc)
            self._insert_run("apple_import", "success", 1294, base - timedelta(days=0))
            self._insert_run("apple_import", "failed", 1294, base - timedelta(days=1))
            self._insert_run("apple_import", "success", 1294, base - timedelta(days=2))

            # The failed row breaks the streak (it's excluded from the query
            # entirely, so it doesn't even count as a "different value" —
            # the two surrounding successes are adjacent in the result set).
            assert get_repeated_yield_streak("apple_import", 1294) == 2
