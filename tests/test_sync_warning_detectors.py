"""Sync warning detectors: what each one counts, and what it must stay quiet on.

Covers the repeated-yield detector (new records only) and the never-yielded
and yield-collapse exemption for sources that declare ``idle_is_normal``.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api.services.sync_health import SYNC_SOURCES, get_sync_health_db

pytestmark = pytest.mark.unit

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

    def test_linkedin_identical_updated_count_across_many_runs_raises_nothing(self, temp_db):
        """LinkedIn re-checks the same export every night and updates the same
        connections; an unchanged file is expected, not a stale re-import."""
        from scripts.run_all_syncs import _detect_repeated_yield

        for day in range(1, 31):
            _insert_run("linkedin", day, records_updated=2094, people_updated=2094)

        stats = {"updated": 2094, "people_updated": 2094}
        assert _detect_repeated_yield("linkedin", stats) is None

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
# Yield collapse honours idle_is_normal
# =============================================================================


class TestYieldCollapseIdleDeclaration:
    SOURCES = {
        "declared_idle": {"description": "d", "phase": 1, "frequency": "daily", "idle_is_normal": True},
        "undeclared": {"description": "u", "phase": 1, "frequency": "daily"},
    }

    def _detect(self, source, stats=None):
        from scripts.run_all_syncs import _detect_yield_collapse

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", self.SOURCES),
            patch("scripts.run_all_syncs.get_typical_yield", return_value=20.0),
            patch("scripts.run_all_syncs.get_consecutive_zero_yield_runs", return_value=5),
        ):
            return _detect_yield_collapse(source, stats or {"created": 0})

    def test_undeclared_source_with_productive_history_then_zeros_collapses(self):
        info = self._detect("undeclared")

        assert info == {"typical_yield": 20.0, "consecutive_zero_runs": 6}

    def test_declared_idle_source_with_same_history_does_not_collapse(self):
        assert self._detect("declared_idle") is None

    def test_declared_idle_source_is_checked_before_history_is_read(self):
        from scripts.run_all_syncs import _detect_yield_collapse

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", self.SOURCES),
            patch("scripts.run_all_syncs.get_typical_yield") as typical,
        ):
            assert _detect_yield_collapse("declared_idle", {"created": 0}) is None

        typical.assert_not_called()

    def test_source_missing_from_metadata_still_collapses(self):
        assert self._detect("not_listed") is not None

    def test_productive_run_never_collapses(self):
        assert self._detect("undeclared", {"updated": 3}) is None


class TestVaultTagQuietNightsDoNotCollapse:
    """Real stored history: vault_tag tags notes on some nights and examines
    unchanged notes on the rest, so a quiet night is not a collapse."""

    def _history(self):
        for day in (4, 5, 6, 7):
            _insert_run("vault_tag", day, records_updated=19, records_processed=240)
        for day in (1, 2, 3):
            _insert_run("vault_tag", day, records_updated=0, records_processed=240)

    def test_quiet_night_with_notes_examined_is_not_a_collapse(self, temp_db):
        from scripts.run_all_syncs import _detect_yield_collapse

        self._history()

        assert _detect_yield_collapse("vault_tag", {"updated": 0, "processed": 240}) is None

    def test_night_with_nothing_examined_still_collapses(self, temp_db):
        from scripts.run_all_syncs import _detect_yield_collapse

        for day in (1, 2, 3, 4):
            _insert_run("vault_tag", day, records_updated=0, records_processed=0)
        for day in (5, 6, 7):
            _insert_run("vault_tag", day, records_updated=19, records_processed=240)

        assert _detect_yield_collapse("vault_tag", {"updated": 0, "processed": 0}) is not None
