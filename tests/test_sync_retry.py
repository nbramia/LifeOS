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
        patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
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

    @pytest.mark.parametrize(
        "error_text",
        [
            # A bare "429" with no status-code context — could be a line
            # number, byte count, or message id in an unrelated traceback,
            # not a rate-limit response (issue #541 adversarial review
            # finding #2).
            "IndexError: list index out of range at line 429",
            "AssertionError: expected 429 rows, got 12",
            "ValueError: message id 429 already processed",
        ],
    )
    def test_bare_429_without_status_context_is_not_transient(self, error_text):
        assert _is_transient_failure(error_text) is False

    @pytest.mark.parametrize(
        "error_text",
        [
            "googleapiclient.errors.HttpError: <HttpError 429 when requesting ...>",
            "HTTP status 429 returned",
            "response code 429: too many requests",
            "429 Too Many Requests",
            "429 rate limit exceeded",
        ],
    )
    def test_429_with_status_context_is_transient(self, error_text):
        assert _is_transient_failure(error_text) is True

    def test_incidental_rate_limiter_class_name_is_not_transient(self):
        """'RateLimiter' is a common HTTP-client helper class name. A bug in
        that class (e.g. an AttributeError) is not a rate-limit response and
        must not be misread as one just because the class name contains
        'rate' + 'limit' as a substring (issue #541 adversarial review,
        found while auditing pattern shapes similar to the bare-429 issue)."""
        assert _is_transient_failure(
            "AttributeError: 'RateLimiter' object has no attribute 'wait'"
        ) is False

    def test_real_rate_limit_wording_still_transient(self):
        """The tightened rate-limit pattern must still catch the real thing."""
        assert _is_transient_failure("Rate limit exceeded, please retry later") is True
        assert _is_transient_failure("slack_sdk.errors.SlackApiError: ratelimited") is True

    def test_entity_resolution_wording_is_not_transient(self):
        """This codebase's own domain vocabulary uses "resolve" for
        merging/linking person entities. A bug there ("Failed to resolve
        duplicate entity...") is not a DNS failure and must not match just
        because both contain the words "Failed to resolve" (issue #541
        adversarial review, found while auditing pattern shapes similar to
        the bare-429 issue)."""
        assert _is_transient_failure(
            "RuntimeError: Failed to resolve duplicate entity for source_id=abc123"
        ) is False

    def test_real_dns_failed_to_resolve_still_transient(self):
        """The tightened pattern must still catch urllib3's actual wording,
        which always quotes the hostname immediately after this phrase."""
        assert _is_transient_failure(
            "NameResolutionError: Failed to resolve 'gmail.googleapis.com' "
            "([Errno -3] Temporary failure in name resolution)"
        ) is True


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

    def test_retried_run_records_only_final_attempt_duration_not_backoff(self):
        """A retried run's recorded duration_seconds must reflect only the
        attempt that produced the final outcome — not the failed attempt's
        time plus the backoff sleep between attempts.

        Regression: duration_seconds used to be derived from the row's
        started_at (set once, at the first attempt), so a retried run
        recorded failed-attempt-time + backoff on top of the successful
        attempt's real execution time. That value feeds
        get_typical_duration_seconds, which _detect_duration_collapse
        compares against to catch silent no-op syncs — inflating it on
        every retry would make that detector progressively less sensitive
        (issue #541 adversarial review finding #1).
        """
        dns_failure = _completed(1, stderr="Temporary failure in name resolution")
        ok = _completed(0)

        # Two time.monotonic() calls per attempt (start, then elapsed-from-
        # start) — fully controlled so backoff/failed-attempt time can't
        # leak into the measurement by accident.
        # Attempt 1: 100.0 -> 100.1 (0.1s, fast failure).
        # Attempt 2: 250.0 -> 250.05 ("150s later" including backoff, but a
        # fast 0.05s successful attempt).
        monotonic_values = [100.0, 100.1, 250.0, 250.05]

        with (
            patch("scripts.run_all_syncs.subprocess.run", side_effect=[dns_failure, ok]),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete") as complete_mock,
            patch("scripts.run_all_syncs.record_sync_error"),
            patch("scripts.run_all_syncs.log_error_to_markdown"),
            patch("scripts.run_all_syncs.time.sleep"),
            patch("scripts.run_all_syncs.time.monotonic", side_effect=monotonic_values),
            patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_yield_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_never_yielded", return_value=None),
            patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
        ):
            success, stats = run_sync(_SOURCE, dry_run=False)

        assert success is True
        kwargs = complete_mock.call_args.kwargs
        # Only the successful attempt's ~0.05s — not the failed attempt plus
        # the ~150s gap, and not the two summed together.
        assert kwargs["duration_seconds"] == pytest.approx(0.05, abs=1e-6)

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


class TestOrchestrationExceptionSafety:
    """A health-DB write failure (e.g. sync_health.db locked — this host runs
    several agents against it concurrently) must not escape run_sync.

    Regression: the retry refactor briefly dropped the outer
    ``except Exception`` that pre-#541 `run_sync` had around its single
    subprocess call. `_execute_sync_once` catches everything the subprocess
    attempt itself can raise, but the orchestration around it (detection
    calls, record_sync_error/record_sync_complete) ran unguarded — if any of
    those raised, the exception would escape run_sync entirely, and since
    run_all_syncs has no exception guard of its own around each run_sync
    call, one source's DB hiccup would have aborted the whole nightly
    pipeline instead of just failing that source (adversarial review
    finding #1).
    """

    def test_health_db_write_exception_produces_terminal_failure_not_propagation(self):
        ok = _completed(0)

        with (
            patch("scripts.run_all_syncs.subprocess.run", return_value=ok),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch(
                "scripts.run_all_syncs.record_sync_complete",
                side_effect=sqlite3.OperationalError("database is locked"),
            ) as complete_mock,
            patch("scripts.run_all_syncs.record_sync_error"),
            patch("scripts.run_all_syncs.log_error_to_markdown"),
            patch("scripts.run_all_syncs.time.sleep"),
            patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_yield_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_never_yielded", return_value=None),
            patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
        ):
            # Must not raise — a locked DB is not the subprocess's fault.
            success, stats = run_sync(_SOURCE, dry_run=False)

        assert success is False
        assert "database is locked" in stats["error"]
        # First call is the normal completion recording (raises); the
        # second is the except-block's best-effort fallback (also raises,
        # and is swallowed there).
        assert complete_mock.call_count == 2

    def test_record_sync_error_exception_also_produces_terminal_failure(self):
        """Same guarantee when the failing health-DB write is
        record_sync_error rather than record_sync_complete (e.g. a
        transient attempt's mid-loop visibility logging)."""
        dns_failure = _completed(1, stderr="Temporary failure in name resolution")

        with (
            patch("scripts.run_all_syncs.subprocess.run", return_value=dns_failure),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete") as complete_mock,
            patch(
                "scripts.run_all_syncs.record_sync_error",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("scripts.run_all_syncs.log_error_to_markdown"),
            patch("scripts.run_all_syncs.time.sleep"),
        ):
            success, stats = run_sync(_SOURCE, dry_run=False)

        assert success is False
        assert "database is locked" in stats["error"]
        # record_sync_error blew up before the loop's own record_sync_complete
        # call was ever reached; the outer except handler's best-effort
        # fallback is what actually records the terminal failure here.
        complete_mock.assert_called_once()
        assert complete_mock.call_args.kwargs["error_message"] == "database is locked"


class TestCampaignStatsNotLostOnRetry:
    """A retried run must not under-report or zero out real work an earlier
    attempt already did.

    Regression: if attempt 1 does real work and then fails partway with a
    transient error, the idempotent retry (attempt 2) legitimately reports
    near-zero new counters for rows attempt 1 already wrote (the sources are
    idempotent — see the comment above MAX_SYNC_RETRIES). Recording only the
    final attempt's numbers would under-report, or even zero out, a run that
    actually did work — and `_detect_yield_collapse`/the consecutive-zero-
    run streak read exactly these fields, so a successful-after-retry run
    could trip a false zero-yield alert (adversarial review finding #2).
    """

    def test_success_after_retry_preserves_earlier_attempts_yield(self):
        # Attempt 1: did real work (50 new interactions), then hit a
        # transient DNS error partway through and exited non-zero.
        partial_then_fail = _completed(
            1,
            stdout='SYNC_STATS:{"interactions_created": 50, "processed": 50}\n',
            stderr="Temporary failure in name resolution",
        )
        # Attempt 2: idempotent re-run — the 50 rows already exist, so this
        # attempt legitimately reports zero new records.
        idempotent_retry = _completed(
            0, stdout='SYNC_STATS:{"interactions_created": 0, "processed": 0}\n'
        )

        success, stats, mocks = _run_sync_with_patches([partial_then_fail, idempotent_retry])

        assert success is True
        # The campaign's real yield survives, not attempt 2's near-zero number.
        assert stats["interactions_created"] == 50
        assert stats["processed"] == 50

        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["interactions_created"] == 50
        assert kwargs["records_processed"] == 50

    def test_terminal_failure_after_retry_also_preserves_earlier_yield(self):
        """Same guarantee on the give-up path: if attempt 1 did real work
        and a later attempt is the one that exhausts retries or hits a
        non-transient error, the recorded failure still reflects the real
        work done, not zero."""
        partial_then_fail = _completed(
            1,
            stdout='SYNC_STATS:{"interactions_created": 30}\n',
            stderr="Temporary failure in name resolution",
        )
        non_transient_failure = _completed(
            1, stderr="google.auth.exceptions.RefreshError: invalid_grant"
        )

        success, stats, mocks = _run_sync_with_patches([partial_then_fail, non_transient_failure])

        assert success is False
        kwargs = mocks["complete"].call_args.kwargs
        assert kwargs["interactions_created"] == 30

    def test_yield_collapse_detector_sees_campaign_max_not_last_attempt(self):
        """End-to-end: _detect_yield_collapse must be evaluated against the
        merged campaign stats, so a real-yield attempt followed by an
        idempotent zero-yield retry does not look like a silent no-op."""
        partial_then_fail = _completed(
            1,
            stdout='SYNC_STATS:{"created": 100}\n',
            stderr="Temporary failure in name resolution",
        )
        idempotent_retry = _completed(0, stdout='SYNC_STATS:{"created": 0}\n')

        with (
            patch("scripts.run_all_syncs.subprocess.run", side_effect=[partial_then_fail, idempotent_retry]),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete"),
            patch("scripts.run_all_syncs.record_sync_error"),
            patch("scripts.run_all_syncs.log_error_to_markdown"),
            patch("scripts.run_all_syncs.time.sleep"),
            patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_yield_collapse") as yield_collapse_mock,
            patch("scripts.run_all_syncs._detect_never_yielded", return_value=None),
            patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
        ):
            yield_collapse_mock.return_value = None
            success, stats = run_sync(_SOURCE, dry_run=False)

        assert success is True
        # _detect_yield_collapse must have been called with the merged
        # (created=100) stats, not attempt 2's raw (created=0) stats.
        yield_collapse_mock.assert_called_once()
        _, called_stats = yield_collapse_mock.call_args.args
        assert called_stats["created"] == 100


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
