"""
Tests for the fitness workout store (issue #320).

Covers session logging with set-count expansion, exercise normalization,
date defaulting, corrections (update latest / by id), health metrics
(body weight), training profile, and history/volume queries.
"""
import pytest

from api.services.fitness_store import FitnessStore, _today

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return FitnessStore(db_path=str(tmp_path / "fitness.db"))


# -- sessions / sets --

class TestSessions:
    def test_single_set_defaults_to_today(self, store):
        s = store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}])
        assert s.date == _today()
        assert len(s.sets) == 1
        assert s.sets[0].exercise == "Bench Press"   # normalized
        assert s.sets[0].reps == 8 and s.sets[0].weight == 135
        assert s.sets[0].set_index == 1

    def test_count_expands_to_n_rows(self, store):
        s = store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185, "count": 3}])
        assert len(s.sets) == 3
        assert [x.set_index for x in s.sets] == [1, 2, 3]
        assert all(x.exercise == "Back Squat" and x.reps == 5 and x.weight == 185 for x in s.sets)

    def test_explicit_date_honored(self, store):
        s = store.add_session(sets=[{"exercise": "deadlift", "reps": 5, "weight": 315}], date="2026-06-05")
        assert s.date == "2026-06-05"

    def test_unknown_exercise_title_cased(self, store):
        s = store.add_session(sets=[{"exercise": "zercher carry", "reps": 10}])
        assert s.sets[0].exercise == "Zercher Carry"

    def test_multi_exercise_session(self, store):
        s = store.add_session(sets=[
            {"exercise": "bench", "reps": 8, "weight": 135},
            {"exercise": "squats", "reps": 5, "weight": 185, "count": 3},
            {"exercise": "run", "notes": "4 mi, 32:10"},
        ])
        assert len(s.sets) == 5  # 1 + 3 + 1
        assert s.sets[-1].exercise == "Run"

    def test_unweighted_work_gets_no_unit(self, store):
        s = store.add_session(sets=[{"exercise": "run", "notes": "5k"}])
        assert s.sets[0].unit == ""  # no weight → no lb
        assert s.sets[0].reps is None

    def test_weighted_set_defaults_to_lb(self, store):
        # The LLM may send unit: null explicitly — a weighted set still gets lb.
        s = store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135, "unit": None}])
        assert s.sets[0].unit == "lb"

    def test_legacy_weight_unit_key_accepted(self, store):
        s = store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 60, "weight_unit": "kg"}])
        assert s.sets[0].unit == "kg"

    def test_counted_work_unit(self, store):
        s = store.add_session(sets=[{"exercise": "stairs", "reps": 500, "unit": "steps", "duration_seconds": 421}])
        assert s.sets[0].unit == "steps"

    def test_list_sessions_newest_first(self, store):
        store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}], date="2026-06-01")
        store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185}], date="2026-06-08")
        sessions = store.list_sessions()
        assert len(sessions) == 2
        assert sessions[0].date == "2026-06-08"


class TestUpdate:
    def test_get_latest_session(self, store):
        store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}], date="2026-06-01")
        s2 = store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185}], date="2026-06-02")
        latest = store.get_latest_session()
        assert latest.id == s2.id

    def test_update_latest_replaces_sets(self, store):
        store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}])
        updated = store.update_session(target="latest", sets=[{"exercise": "bench", "reps": 8, "weight": 145}])
        assert updated.sets[0].weight == 145
        assert len(updated.sets) == 1

    def test_update_by_id(self, store):
        s = store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}])
        store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185}])  # newer
        updated = store.update_session(session_id=s.id, notes="felt easy")
        assert updated.id == s.id and updated.notes == "felt easy"

    def test_update_no_session_returns_none(self, store):
        assert store.update_session(target="latest", notes="x") is None


# -- duration --

class TestDuration:
    def test_duration_roundtrip(self, store):
        s = store.add_session(sets=[{"exercise": "stairs", "reps": 500, "duration_seconds": 421}])
        assert s.sets[0].exercise == "Stairs"
        assert s.sets[0].reps == 500
        assert s.sets[0].duration_seconds == 421

    def test_duration_defaults_to_none(self, store):
        s = store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}])
        assert s.sets[0].duration_seconds is None

    def test_exercise_history_includes_duration(self, store):
        store.add_session(sets=[{"exercise": "stairs", "reps": 500, "duration_seconds": 421}], date="2026-06-10")
        hist = store.exercise_history("stairs")
        assert hist[0]["duration_seconds"] == 421

    def test_migrates_pre_duration_db(self, tmp_path):
        # A workout_sets table from before duration_seconds/unit must be
        # migrated on open: duration column added, weight_unit renamed to unit,
        # and the old unconditional 'lb' cleared on weightless rows.
        import sqlite3
        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE workout_sets (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                exercise TEXT NOT NULL,
                set_index INTEGER NOT NULL,
                reps INTEGER,
                weight REAL,
                weight_unit TEXT DEFAULT 'lb',
                rpe REAL,
                notes TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO workout_sets (id, session_id, exercise, set_index, reps, weight, weight_unit) "
            "VALUES ('a', 's1', 'Stairs', 1, 500, NULL, 'lb')"
        )
        conn.execute(
            "INSERT INTO workout_sets (id, session_id, exercise, set_index, reps, weight, weight_unit) "
            "VALUES ('b', 's1', 'Bench Press', 1, 8, 135, 'lb')"
        )
        conn.commit()
        conn.close()
        store = FitnessStore(db_path=db)
        s = store.add_session(sets=[{"exercise": "stairs", "reps": 500, "duration_seconds": 421}])
        assert store.get_session(s.id).sets[0].duration_seconds == 421
        conn = sqlite3.connect(db)
        units = dict(conn.execute("SELECT id, unit FROM workout_sets WHERE id IN ('a', 'b')"))
        conn.close()
        assert units["a"] == ""    # weightless row: lb cleared
        assert units["b"] == "lb"  # weighted row: unit kept


# -- metrics --

class TestMetrics:
    def test_log_and_list_body_weight(self, store):
        store.log_metric("body_weight", 178.4, unit="lb")
        rows = store.list_metrics("body_weight")
        assert len(rows) == 1 and rows[0].value == 178.4 and rows[0].unit == "lb"

    def test_latest_metric(self, store):
        store.log_metric("body_weight", 178.0, unit="lb", start_at="2026-06-01T08:00:00")
        store.log_metric("body_weight", 177.2, unit="lb", start_at="2026-06-08T08:00:00")
        latest = store.latest_metric("body_weight")
        assert latest.value == 177.2

    def test_metrics_isolated_by_type(self, store):
        store.log_metric("body_weight", 178.0)
        store.log_metric("resting_hr", 54)
        assert len(store.list_metrics("body_weight")) == 1
        assert len(store.list_metrics("resting_hr")) == 1


class TestDailyMetricTotals:
    def test_sums_intraday_samples_per_day(self, store):
        # Several midday buckets on one day (midday is tz-stable) collapse to one total.
        for h, v in ((10, 10), (12, 20), (14, 30)):
            store.log_metric("steps", v, unit="count", start_at=f"2026-06-07T{h}:00:00+00:00")
        store.log_metric("steps", 5, unit="count", start_at="2026-06-08T12:00:00+00:00")
        days = store.daily_metric_totals("steps")
        assert days[0] == {"date": "2026-06-08", "value": 5, "unit": "count", "samples": 1}
        assert days[1] == {"date": "2026-06-07", "value": 60, "unit": "count", "samples": 3}

    def test_newest_first_and_day_limit(self, store):
        for d in range(1, 6):
            store.log_metric("steps", 100, start_at=f"2026-06-0{d}T12:00:00+00:00")
        days = store.daily_metric_totals("steps", limit=2)
        assert [r["date"] for r in days] == ["2026-06-05", "2026-06-04"]

    def test_local_day_range_filter(self, store):
        for d in (1, 2, 3):
            store.log_metric("steps", 100, start_at=f"2026-06-0{d}T12:00:00+00:00")
        days = store.daily_metric_totals("steps", start="2026-06-02", end="2026-06-02")
        assert len(days) == 1 and days[0]["date"] == "2026-06-02"


# -- profile --

class TestProfile:
    def test_set_get_roundtrip(self, store):
        store.set_profile("goals", "half marathon in fall")
        store.set_profile("injuries", "left knee")
        prof = store.get_profile()
        assert prof["goals"] == "half marathon in fall"
        assert prof["injuries"] == "left knee"

    def test_set_profile_upserts(self, store):
        store.set_profile("goals", "strength")
        store.set_profile("goals", "hypertrophy")
        assert store.get_profile()["goals"] == "hypertrophy"


# -- queries --

class TestQueries:
    def test_exercise_history_newest_first(self, store):
        store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}], date="2026-06-01")
        store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 145}], date="2026-06-08")
        hist = store.exercise_history("bench")
        assert hist[0]["date"] == "2026-06-08" and hist[0]["weight"] == 145

    def test_volume_summary_tonnage(self, store):
        store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185, "count": 3}])
        summary = store.volume_summary(exercise="squats")
        assert summary["sets"] == 3
        assert summary["reps"] == 15
        assert summary["tonnage"] == 185 * 5 * 3
        assert summary["sessions"] == 1
