"""Sync warning detectors: what each one counts, and what it must stay quiet on.

Covers the repeated-yield detector (new records only), the never-yielded
exemption for sources that declare ``idle_is_normal``, and the stale
source-file check for the LinkedIn export.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services.sync_health import SYNC_SOURCES, get_sync_health_db

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEAD_HISTORY = {"runs": 200, "best_yield": 0, "avg_duration_seconds": 1.0}


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "sync_health.db"
    with patch("api.services.sync_health.SYNC_HEALTH_DB_PATH", db_path):
        get_sync_health_db().close()
        yield db_path


def _insert_run(source, days_ago, **columns):
    started = datetime.now(timezone.utc) - timedelta(days=days_ago)
    names = ["source", "status", "started_at", "completed_at", *columns]
    values = [source, "success", started.isoformat(), started.isoformat(), *columns.values()]
    conn = get_sync_health_db()
    conn.execute(
        f"INSERT INTO sync_runs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        values,
    )
    conn.commit()
    conn.close()


# =============================================================================
# Repeated yield counts new records only
# =============================================================================


class TestRepeatedYieldCountsOnlyCreated:
    def test_created_streak_fires_at_three_runs(self):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=2):
            info = _detect_repeated_yield("apple_import", {"created": 1294})

        assert info == {"repeated_value": 1294, "consecutive_runs": 3}

    def test_created_streak_of_two_does_not_fire(self):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=1):
            assert _detect_repeated_yield("apple_import", {"created": 1294}) is None

    def test_streak_lookup_uses_the_created_count(self):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=5) as streak:
            _detect_repeated_yield("apple_import", {"created": 1294, "updated": 9999, "processed": 8888})

        streak.assert_called_once_with("apple_import", 1294)

    @pytest.mark.parametrize("stats", [
        {"updated": 2094, "people_updated": 2094},
        {"processed": 48, "updated": 1},
        {"processed": 6},
        {"updated": 19},
    ])
    def test_updated_or_processed_only_never_fires(self, stats):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=50) as streak:
            assert _detect_repeated_yield("linkedin", stats) is None

        streak.assert_not_called()

    @pytest.mark.parametrize("field", [
        "created", "people_created", "interactions_created", "source_entities_created",
    ])
    def test_each_created_field_counts(self, field):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=2):
            info = _detect_repeated_yield("src", {field: 7})

        assert info == {"repeated_value": 7, "consecutive_runs": 3}

    def test_run_created_takes_largest_created_field_not_updates(self):
        from scripts.run_all_syncs import _run_created

        assert _run_created({"created": 3, "people_created": 5, "updated": 99, "processed": 99}) == 5
        assert _run_created({"updated": 99, "processed": 99}) == 0


class TestRepeatedYieldAgainstStoredHistory:
    """The live check and the stored-history query share one definition."""

    def test_identical_created_counts_fire_on_the_third_run(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        _insert_run("apple_import", 1, records_created=1294)
        _insert_run("apple_import", 2, records_created=1294)

        info = _detect_repeated_yield("apple_import", {"created": 1294})
        assert info == {"repeated_value": 1294, "consecutive_runs": 3}

    def test_two_runs_in_a_row_do_not_fire(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        _insert_run("apple_import", 1, records_created=1294)

        assert _detect_repeated_yield("apple_import", {"created": 1294}) is None

    def test_steady_updates_never_fire(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        for day in range(1, 11):
            _insert_run("apple_import", day, records_updated=1998, people_updated=1998)

        assert _detect_repeated_yield("apple_import", {"updated": 1998, "people_updated": 1998}) is None

    def test_steady_processed_never_fires(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        for day in range(1, 11):
            _insert_run("google_docs", day, records_processed=6)

        assert _detect_repeated_yield("google_docs", {"processed": 6}) is None

    def test_updates_matching_the_created_value_do_not_extend_a_streak(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        for day in (1, 2, 3, 4):
            _insert_run("src", day, records_updated=1294)

        assert _detect_repeated_yield("src", {"created": 1294}) is None

    def test_zero_created_run_breaks_the_streak(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        _insert_run("src", 1, records_created=0, records_updated=1294)
        _insert_run("src", 2, records_created=1294)
        _insert_run("src", 3, records_created=1294)

        assert _detect_repeated_yield("src", {"created": 1294}) is None

    def test_zero_created_this_run_never_fires(self, temp_db):
        from scripts.run_all_syncs import _detect_repeated_yield

        for day in (1, 2, 3):
            _insert_run("src", day, records_created=0)

        assert _detect_repeated_yield("src", {"created": 0}) is None


# =============================================================================
# Never-yielded: declared-idle sources and the yield definition
# =============================================================================


class TestNeverYieldedIdleDeclaration:
    SOURCES = {
        "declared_idle": {"description": "d", "phase": 1, "frequency": "daily", "idle_is_normal": True},
        "undeclared": {"description": "u", "phase": 1, "frequency": "daily"},
    }

    def test_declared_idle_source_does_not_warn(self):
        from scripts.run_all_syncs import _detect_never_yielded

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", self.SOURCES),
            patch("scripts.run_all_syncs.get_yield_history", return_value=DEAD_HISTORY),
        ):
            assert _detect_never_yielded("declared_idle", {"created": 0}) is None

    def test_undeclared_zero_yield_source_still_warns(self):
        from scripts.run_all_syncs import _detect_never_yielded

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", self.SOURCES),
            patch("scripts.run_all_syncs.get_yield_history", return_value=DEAD_HISTORY),
        ):
            info = _detect_never_yielded("undeclared", {"created": 0})

        assert info is not None and info["runs"] == 200

    def test_source_missing_from_metadata_still_warns(self):
        from scripts.run_all_syncs import _detect_never_yielded

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", self.SOURCES),
            patch("scripts.run_all_syncs.get_yield_history", return_value=DEAD_HISTORY),
        ):
            assert _detect_never_yielded("not_listed", {"created": 0}) is not None

    def test_repoint_stale_ids_declares_idle(self):
        assert SYNC_SOURCES["repoint_stale_ids"]["idle_is_normal"] is True

    @pytest.mark.parametrize("source", ["link_slack", "google_sheets", "vault_tag", "linkedin", "contacts"])
    def test_sources_that_report_real_work_are_not_declared_idle(self, source):
        assert not SYNC_SOURCES[source].get("idle_is_normal")

    def test_updated_only_run_is_not_never_yielded(self):
        """A source that only updates did real work: total yield stays the
        never-yielded measure."""
        from scripts.run_all_syncs import _detect_never_yielded

        with patch("scripts.run_all_syncs.get_yield_history", return_value=DEAD_HISTORY):
            assert _detect_never_yielded("contacts", {"updated": 4}) is None


# =============================================================================
# Stale LinkedIn export
# =============================================================================

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _export_file(tmp_path, age_days):
    path = tmp_path / "LinkedInConnections.csv"
    path.write_text("First Name,Last Name\nAda,Example\n")
    mtime = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


class TestStaleLinkedinExport:
    def test_over_ninety_days_fires_and_names_the_action(self, tmp_path):
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        info = _detect_stale_linkedin_export(_export_file(tmp_path, 91), now=NOW)

        assert info["age_days"] == 91
        assert info["source"] == "linkedin"
        assert "download a fresh LinkedIn export" in info["action"]
        assert "LinkedInConnections.csv" in info["action"]

    def test_under_ninety_days_is_silent(self, tmp_path):
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        assert _detect_stale_linkedin_export(_export_file(tmp_path, 89), now=NOW) is None

    def test_exactly_ninety_days_is_silent(self, tmp_path):
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        assert _detect_stale_linkedin_export(_export_file(tmp_path, 90), now=NOW) is None

    def test_just_over_ninety_days_fires(self, tmp_path):
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        path = _export_file(tmp_path, 90.1)
        assert _detect_stale_linkedin_export(path, now=NOW) is not None

    def test_absent_file_is_silent(self, tmp_path):
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        assert _detect_stale_linkedin_export(tmp_path / "missing.csv", now=NOW) is None

    def test_absent_file_is_silent_without_logging_a_failure(self, tmp_path, caplog):
        """Absence is an expected state, handled before any stat call rather
        than by the never-raises fallback."""
        from scripts.run_all_syncs import _detect_stale_linkedin_export

        with caplog.at_level("WARNING"):
            _detect_stale_linkedin_export(tmp_path / "missing.csv", now=NOW)

        assert caplog.records == []

    def test_default_path_matches_the_linkedin_sync_script(self):
        from scripts.run_all_syncs import LINKEDIN_EXPORT_PATH
        from scripts.sync_linkedin import DEFAULT_CSV_PATH

        assert (REPO_ROOT / DEFAULT_CSV_PATH).resolve() == LINKEDIN_EXPORT_PATH.resolve()


class TestStaleSourceFileWeeklyLimit:
    def _check(self, tmp_path, prior_errors):
        from scripts.run_all_syncs import _check_stale_source_files

        path = _export_file(tmp_path, 200)
        with (
            patch("scripts.run_all_syncs.get_recent_errors", return_value=prior_errors),
            patch("scripts.run_all_syncs.record_sync_error") as record,
        ):
            return _check_stale_source_files(path=path, now=NOW), record

    @staticmethod
    def _error(days_ago, error_type="stale_source_file"):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return {"error_type": error_type, "timestamp": ts}

    def test_first_warning_is_recorded(self, tmp_path):
        stale, record = self._check(tmp_path, [])

        assert [s["source"] for s in stale] == ["linkedin"]
        record.assert_called_once()
        assert record.call_args.args[0] == "linkedin"
        assert record.call_args.kwargs["error_type"] == "stale_source_file"
        assert "download a fresh LinkedIn export" in record.call_args.args[1]

    def test_warning_within_a_week_is_suppressed(self, tmp_path):
        stale, record = self._check(tmp_path, [self._error(6)])

        assert stale == []
        record.assert_not_called()

    def test_warning_older_than_a_week_rewarns(self, tmp_path):
        stale, record = self._check(tmp_path, [self._error(8)])

        assert len(stale) == 1
        record.assert_called_once()

    def test_other_error_types_do_not_suppress(self, tmp_path):
        stale, _ = self._check(tmp_path, [self._error(1, "never_yielded")])

        assert len(stale) == 1

    def test_fresh_file_records_nothing(self, tmp_path):
        from scripts.run_all_syncs import _check_stale_source_files

        path = _export_file(tmp_path, 10)
        with (
            patch("scripts.run_all_syncs.get_recent_errors", return_value=[]),
            patch("scripts.run_all_syncs.record_sync_error") as record,
        ):
            assert _check_stale_source_files(path=path, now=NOW) == []

        record.assert_not_called()

    def test_record_false_reports_without_writing(self, tmp_path):
        from scripts.run_all_syncs import _check_stale_source_files

        path = _export_file(tmp_path, 200)
        with (
            patch("scripts.run_all_syncs.get_recent_errors", return_value=[]),
            patch("scripts.run_all_syncs.record_sync_error") as record,
        ):
            stale = _check_stale_source_files(path=path, now=NOW, record=False)

        assert len(stale) == 1
        record.assert_not_called()


# =============================================================================
# End-of-run summaries
# =============================================================================

STALE = {
    "source": "linkedin",
    "file": "LinkedInConnections.csv",
    "age_days": 200,
    "action": "download a fresh LinkedIn export",
}


def _result(**overrides):
    base = {
        "sources_run": 3, "succeeded": 3, "failed": 0, "failed_sources": [],
        "results": {}, "duration_seconds": 5, "dep_skipped_sources": [],
        "duration_collapsed_sources": [], "repeated_yield_sources": [],
        "stale_source_files": [], "investments_stale": None,
        "apple_agent_sha_drift": None, "people_created": 0, "interactions_created": 0,
    }
    base.update(overrides)
    return base


class TestStaleSourceFileInSummaries:
    def _markdown(self, result):
        from scripts.run_all_syncs import log_sync_summary_to_markdown

        with patch("scripts.run_all_syncs._write_to_markdown_log") as write:
            log_sync_summary_to_markdown(result, trigger="scheduled")
        return write.call_args.args[0]

    def _telegram(self, result):
        from scripts.run_all_syncs import send_sync_summary_telegram

        send = MagicMock(return_value=True)
        with patch("api.services.telegram.send_message", send):
            send_sync_summary_telegram(result, trigger="scheduled")
        return send.call_args.args[0]

    def test_markdown_lists_stale_file(self):
        entry = self._markdown(_result(stale_source_files=[STALE]))

        assert "Stale source file (1)" in entry
        assert "LinkedInConnections.csv: 200 days old" in entry
        assert "download a fresh LinkedIn export" in entry

    def test_markdown_omits_section_when_clean(self):
        assert "Stale source file" not in self._markdown(_result())

    def test_telegram_lists_stale_file_and_marks_run_as_needing_attention(self):
        text = self._telegram(_result(stale_source_files=[STALE]))

        assert "Stale source file (1)" in text
        assert "LinkedInConnections.csv: 200 days old" in text
        assert text.startswith("⚠️")

    def test_telegram_clean_run_is_green_without_the_section(self):
        text = self._telegram(_result())

        assert "Stale source file" not in text
        assert text.startswith("✅")


class TestRunAllSyncsWiring:
    def _run(self, sources, stale):
        from scripts.run_all_syncs import run_all_syncs

        with (
            patch("scripts.run_all_syncs.run_sync", MagicMock(return_value=(True, {}))),
            patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
            patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
            patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
            patch("scripts.run_all_syncs._detect_stale_linkedin_export", return_value=stale) as detect,
            patch("scripts.run_all_syncs.get_recent_errors", return_value=[]),
            patch("scripts.run_all_syncs.record_sync_error") as record,
        ):
            result = run_all_syncs(sources=sources, dry_run=True)
        return result, detect, record

    def test_result_carries_stale_file_when_linkedin_ran(self):
        result, _, record = self._run(["linkedin"], STALE)

        assert result["stale_source_files"] == [STALE]
        record.assert_not_called()  # dry run reports without writing

    def test_clean_export_leaves_result_empty(self):
        result, _, _ = self._run(["linkedin"], None)

        assert result["stale_source_files"] == []

    def test_partial_run_without_linkedin_does_not_check(self):
        result, detect, _ = self._run(["contacts"], STALE)

        assert result["stale_source_files"] == []
        detect.assert_not_called()
