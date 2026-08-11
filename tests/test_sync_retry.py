"""
Tests for transient-failure retry in the nightly sync orchestrator (issue #541).

Regression context: the nightly sync fires once at 03:30 with no retry. On a
WiFi-only host, a momentary DNS blip (measured baseline: ~2-in-3 nightly
failures for gmail_personal/gmail_work/slack over six weeks, every failure a
name-resolution error) cost a full day of freshness even though the host was
online and healthy almost the whole time.

These tests pin: a transiently-failing source is retried and, on eventual
success, recorded as a success that needed a retry; a non-transient failure
is never retried; an always-failing source terminates within the retry
bound rather than looping forever; a first-time success records no retry;
a dependency-skipped source never enters the retry path at all; and the
sync_health schema migration preserves rows written before this feature
existed.
"""
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.run_all_syncs import (
    MAX_SYNC_RETRIES,
    RETRY_BACKOFF_SECONDS,
    SYNC_SCRIPTS,
    SyncStatus,
    _is_transient_failure,
    run_sync,
)

pytestmark = pytest.mark.unit


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


# Any real source works — run_sync's retry logic doesn't depend on which one.
_SOURCE = next(iter(SYNC_SCRIPTS))


def _run_sync_with_patches(subprocess_side_effect):
    """Run run_sync(_SOURCE) with sync_health/markdown/detection recording
    mocked out, and subprocess.run driven by `subprocess_side_effect`.

    Returns (success, stats, mocks) where mocks is a dict of the patched
    callables, so tests can assert on call counts/kwargs without touching a
    real sync_health.db.
    """
    with (
        patch("scripts.run_all_syncs.subprocess.run", side_effect=subprocess_side_effect) as run_mock,
        patch("scripts.run_all_syncs.record_sync_start", return_value=999),
        patch("scripts.run_all_syncs.record_sync_complete") as complete_mock,
        patch("scripts.run_all_syncs.record_sync_error") as error_mock,
        patch("scripts.run_all_syncs.log_error_to_markdown") as markdown_mock,
        patch("scripts.run_all_syncs.time.sleep") as sleep_mock,
        patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
        patch("scripts.run_all_syncs._detect_yield_collapse", return_value=None),
        patch("scripts.run_all_syncs._detect_never_yielded", return_value=None),
    ):
        success, stats = run_sync(_SOURCE, dry_run=False)

    mocks = {
        "run": run_mock,
        "complete": complete_mock,
        "error": error_mock,
        "markdown": markdown_mock,
        "sleep": sleep_mock,
    }
    return success, stats, mocks


class TestTransientFailureClassifier:
    """_is_transient_failure must key on connectivity/rate-limit signatures,
    not on any single library's error wording — issue #540 (landing
    separately) is about to change Gmail's current "expired/revoked" text,
    so the classifier must not depend on that phrase."""

    @pytest.mark.parametrize(
        "error_text",
        [
            "[Errno -3] Temporary failure in name resolution",
            "socket.gaierror: [Errno -3] Temporary failure in name resolution",
            "requests.exceptions.ConnectionError: HTTPSConnectionPool(...): "
            "Max retries exceeded with url: /gmail/v1/... "
            "(Caused by NameResolutionError(\"Failed to resolve 'gmail.googleapis.com'\"))",
            "urllib3.exceptions.NewConnectionError: Failed to establish a new connection",
            "ConnectionRefusedError: [Errno 111] Connection refused",
            "http.client.RemoteDisconnected: Remote end closed connection without response",
            "slack_sdk.errors.SlackApiError: ratelimited",
            "HTTPError: 429 Too Many Requests",
            "googleapiclient.errors.HttpError: <HttpError 503 Service Unavailable>",
        ],
    )
    def test_connectivity_and_rate_limit_signatures_are_transient(self, error_text):
        assert _is_transient_failure(error_text) is True

    @pytest.mark.parametrize(
        "error_text",
        [
            "Token has expired or been revoked",  # the #540 misattribution — not a connectivity signature
            "PermissionError: [Errno 13] Permission denied: '/data/crm.db'",
            "KeyError: 'GMAIL_CLIENT_ID' not found in config",
            "google.auth.exceptions.RefreshError: invalid_grant: Token has been expired or revoked",
            "ModuleNotFoundError: No module named 'nonexistent'",
            "ValueError: malformed date '2026-13-45'",
        ],
    )
    def test_config_and_permission_failures_are_not_transient(self, error_text):
        assert _is_transient_failure(error_text) is False

    def test_empty_or_missing_text_is_not_transient(self):
        """Conservative default: no signal means no retry."""
        assert _is_transient_failure(None) is False
        assert _is_transient_failure("") is False


class TestRetryLoop:
    """run_sync's within-run retry behavior."""

    def test_transient_failure_retried_then_succeeds(self):
        """A source that fails once with a DNS error then succeeds is
        retried, and the final health record shows a success that needed
        a retry (attempt_count=2)."""
        dns_failure = _completed(1, stderr="[Errno -3] Temporary failure in name resolution")
        ok = _completed(0)

        success, stats, mocks = _run_sync_with_patches([dns_failure, ok])

        assert success is True
        assert mocks["run"].call_count == 2
        mocks["sleep"].assert_called_once_with(RETRY_BACKOFF_SECONDS[0])
        # A retry that ultimately succeeds is not a markdown-worthy error.
        mocks["markdown"].assert_not_called()

        mocks["complete"].assert_called_once()
        args, kwargs = mocks["complete"].call_args
        assert args[1] == SyncStatus.SUCCESS
        assert kwargs["attempt_count"] == 2

    def test_transient_failure_retried_twice_then_succeeds(self):
        """Two transient failures followed by success uses both backoff
        slots and lands on attempt 3."""
        dns_failure = _completed(1, stderr="Temporary failure in name resolution")
        ok = _completed(0)

        success, stats, mocks = _run_sync_with_patches([dns_failure, dns_failure, ok])

        assert success is True
        assert mocks["run"].call_count == 3
        assert mocks["sleep"].call_args_list == [
            ((RETRY_BACKOFF_SECONDS[0],),),
            ((RETRY_BACKOFF_SECONDS[1],),),
        ]
        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["attempt_count"] == 3

    def test_non_transient_failure_not_retried(self):
        """A permission/config failure is recorded as a terminal failure on
        the very first attempt — no retry, no backoff sleep."""
        auth_failure = _completed(1, stderr="google.auth.exceptions.RefreshError: invalid_grant")

        success, stats, mocks = _run_sync_with_patches([auth_failure])

        assert success is False
        assert mocks["run"].call_count == 1
        mocks["sleep"].assert_not_called()
        mocks["markdown"].assert_called_once()

        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["attempt_count"] == 1
        args = mocks["complete"].call_args.args
        assert args[1] == SyncStatus.FAILED

    def test_always_failing_source_terminates_within_bound(self):
        """A dead uplink (every attempt fails transiently) must not retry
        forever: it stops after MAX_SYNC_RETRIES retries (MAX_SYNC_RETRIES + 1
        attempts total), bounding this source's contribution to total
        runtime."""
        dns_failure = _completed(1, stderr="[Errno -3] Temporary failure in name resolution")
        max_attempts = MAX_SYNC_RETRIES + 1

        success, stats, mocks = _run_sync_with_patches([dns_failure] * max_attempts)

        assert success is False
        assert mocks["run"].call_count == max_attempts
        assert mocks["sleep"].call_count == max_attempts - 1
        # Exactly one terminal failure recorded, not one per attempt.
        mocks["complete"].assert_called_once()
        mocks["markdown"].assert_called_once()

        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["attempt_count"] == max_attempts

    def test_first_time_success_records_no_retry(self):
        """A clean first-try success is unaffected: attempt_count=1, no
        backoff, no retry-related recording."""
        ok = _completed(0)

        success, stats, mocks = _run_sync_with_patches([ok])

        assert success is True
        assert mocks["run"].call_count == 1
        mocks["sleep"].assert_not_called()
        mocks["error"].assert_not_called()

        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["attempt_count"] == 1

    def test_retry_attempts_are_logged_to_sync_errors_for_visibility(self):
        """Each failed attempt (including retried ones) is recorded via
        record_sync_error, so an operator can see "it took 2 tries" even
        though only one sync_runs row exists for the whole campaign."""
        dns_failure = _completed(1, stderr="Temporary failure in name resolution")
        ok = _completed(0)

        success, stats, mocks = _run_sync_with_patches([dns_failure, ok])

        assert success is True
        # One record_sync_error for the failed first attempt.
        assert mocks["error"].call_count == 1


class TestDependencySkipNotRetried:
    """A source skipped for dependency reasons must never enter run_sync's
    retry path — the skip happens in run_all_syncs before run_sync is ever
    called, so it can't be mistaken for a retryable failure."""

    def test_dependency_skipped_source_never_calls_run_sync(self):
        from scripts.run_all_syncs import run_all_syncs

        sources = {
            "a": {"description": "A", "phase": 1, "frequency": "daily"},
            "b": {"description": "B", "phase": 2, "frequency": "daily", "depends_on": ["a"]},
        }

        def side_effect(source, dry_run=False):
            if source == "a":
                return False, {"error": "simulated failure"}
            return True, {}

        run_sync_mock = MagicMock(side_effect=side_effect)

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", sources),
            patch("scripts.run_all_syncs.SYNC_ORDER", ["a", "b"]),
            patch("scripts.run_all_syncs.run_sync", run_sync_mock),
            patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
            patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
            patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        ):
            result = run_all_syncs(dry_run=True)

        assert "b" in result["dep_skipped_sources"]
        assert result["results"]["b"]["reason"] == "dependency_failed"
        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "b" not in called_sources


class TestSyncRunsSchemaMigration:
    """A pre-#541 sync_health.db (no attempt_count column) must migrate in
    place: existing rows stay intact, and the new column becomes usable for
    both old and new rows."""

    def test_migration_preserves_existing_rows_and_new_writes_use_new_column(self, tmp_path):
        from api.services.sync_health import get_sync_health_db, record_sync_complete, record_sync_start

        db_path = tmp_path / "sync_health.db"

        # Build the pre-#541 schema by hand and seed it with a real
        # historical row, mirroring the six-week baseline the issue cites,
        # before the current module ever touches the file.
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                records_processed INTEGER DEFAULT 0,
                records_created INTEGER DEFAULT 0,
                records_updated INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                error_message TEXT,
                duration_seconds REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO sync_runs (source, status, started_at, completed_at, error_message) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "gmail_personal",
                "failed",
                "2026-07-15T03:31:00+00:00",
                "2026-07-15T03:31:05+00:00",
                "[Errno -3] Temporary failure in name resolution",
            ),
        )
        conn.commit()
        conn.close()

        with patch("api.services.sync_health.SYNC_HEALTH_DB_PATH", db_path):
            # Opening a connection runs _init_schema's migrations.
            migrated_conn = get_sync_health_db()
            old_row = migrated_conn.execute(
                "SELECT * FROM sync_runs WHERE source = 'gmail_personal'"
            ).fetchone()
            migrated_conn.close()

            # The pre-existing row survived the migration untouched...
            assert old_row["status"] == "failed"
            assert old_row["error_message"] == "[Errno -3] Temporary failure in name resolution"
            # ...and reads back as "no retry" — the correct interpretation
            # for history recorded before retries existed.
            assert old_row["attempt_count"] == 1

            # New rows can use the column for real.
            run_id = record_sync_start("gmail_personal")
            record_sync_complete(run_id, SyncStatus.SUCCESS, attempt_count=2)

            check_conn = get_sync_health_db()
            new_row = check_conn.execute(
                "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            check_conn.close()

        assert new_row["attempt_count"] == 2
