"""
Tests for the manage_workouts orchestrator tool (issue #320).

The fitness bot logs/queries via this tool (not the MCP server). Verifies the
dispatcher, the log/update/history/summary/metric/profile actions, and the
compact session summary the bot echoes back.
"""
import pytest

import api.services.fitness_store as fs
from api.services.fitness_store import FitnessStore, WorkoutSession, WorkoutSet
from api.services.agent_tools import _tool_manage_workouts, _summarize_session, TOOL_DEFINITIONS, _TOOL_HANDLERS

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    monkeypatch.setattr(fs, "_store_instance", store)
    return store


def test_tool_is_registered():
    assert "manage_workouts" in _TOOL_HANDLERS
    assert any(t["name"] == "manage_workouts" for t in TOOL_DEFINITIONS)


class TestDispatch:
    def test_log_records_and_confirms(self, temp_store):
        out = _tool_manage_workouts({"action": "log", "sets": [{"exercise": "bench", "reps": 8, "weight": 135}]})
        assert out.startswith("Logged —")
        assert "Bench Press 135×8" in out or "Bench Press 8 @135" in out
        assert temp_store.get_latest_session() is not None

    def test_log_requires_sets(self, temp_store):
        out = _tool_manage_workouts({"action": "log", "sets": []})
        assert out.startswith("Error")

    def test_update_targets_latest(self, temp_store):
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "bench", "reps": 8, "weight": 135}]})
        out = _tool_manage_workouts({"action": "update", "sets": [{"exercise": "bench", "reps": 8, "weight": 145}]})
        assert out.startswith("Updated —")
        assert temp_store.get_latest_session().sets[0].weight == 145

    def test_update_no_session(self, temp_store):
        out = _tool_manage_workouts({"action": "update", "notes": "x"})
        assert out.startswith("Error")

    def test_list_exposes_session_ids(self, temp_store):
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "bench", "reps": 8, "weight": 135}]})
        out = _tool_manage_workouts({"action": "list"})
        latest_id = temp_store.get_latest_session().id
        assert latest_id in out  # id surfaced so the orchestrator can target an older session

    def test_update_older_session_by_id(self, temp_store):
        # Log an older session, then a newer one; correcting the OLDER by id must
        # not touch the latest (the threaded-reply-to-older-session criterion).
        old = temp_store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}], date="2026-06-01")
        temp_store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185}], date="2026-06-08")
        out = _tool_manage_workouts({
            "action": "update", "session_id": old.id,
            "sets": [{"exercise": "bench", "reps": 8, "weight": 145}],
        })
        assert out.startswith("Updated")
        assert temp_store.get_session(old.id).sets[0].weight == 145
        # latest (squats) untouched
        assert temp_store.get_latest_session().sets[0].exercise == "Back Squat"

    def test_history_exposes_session_id(self, temp_store):
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "squats", "reps": 5, "weight": 185}]})
        out = _tool_manage_workouts({"action": "history", "exercise": "squats"})
        assert temp_store.get_latest_session().id in out

    def test_log_metric_accepts_string_value(self, temp_store):
        # Schema types `value` as string; numeric metrics must still parse.
        out = _tool_manage_workouts({"action": "log_metric", "metric_type": "body_weight", "value": "178.4", "unit": "lb"})
        assert "178.4" in out
        assert temp_store.latest_metric("body_weight").value == 178.4

    def test_log_metric_body_weight(self, temp_store):
        out = _tool_manage_workouts({"action": "log_metric", "metric_type": "body_weight", "value": 178.4, "unit": "lb"})
        assert "178.4" in out
        assert temp_store.latest_metric("body_weight").value == 178.4

    def test_metrics_lists(self, temp_store):
        _tool_manage_workouts({"action": "log_metric", "metric_type": "body_weight", "value": 178, "unit": "lb"})
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight"})
        assert "body weight" in out.lower()

    def test_history(self, temp_store):
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "squats", "reps": 5, "weight": 185}]})
        out = _tool_manage_workouts({"action": "history", "exercise": "squats"})
        assert "Back Squat" in out

    def test_summary(self, temp_store):
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "squats", "reps": 5, "weight": 185, "count": 3}]})
        out = _tool_manage_workouts({"action": "summary", "exercise": "squats"})
        assert "3 sets" in out and "15 reps" in out

    def test_profile_set_get(self, temp_store):
        _tool_manage_workouts({"action": "set_profile", "key": "goals", "value": "5k PR"})
        out = _tool_manage_workouts({"action": "get_profile"})
        assert "goals: 5k PR" in out

    def test_unknown_action(self, temp_store):
        assert _tool_manage_workouts({"action": "bogus"}).startswith("Error")


class TestReadiness:
    def test_readiness_empty_degrades_gracefully(self, temp_store):
        out = _tool_manage_workouts({"action": "readiness"})
        assert "Readiness snapshot" in out
        assert "none logged" in out          # no sessions
        assert "none yet" in out             # no recovery metrics
        assert "not set" in out              # no profile

    def test_readiness_aggregates_volume_metrics_profile(self, temp_store):
        from api.services.fitness_store import _today
        _tool_manage_workouts({"action": "log", "sets": [{"exercise": "squats", "reps": 5, "weight": 185, "count": 3}], "date": _today()})
        _tool_manage_workouts({"action": "log_metric", "metric_type": "body_weight", "value": "178.4", "unit": "lb"})
        _tool_manage_workouts({"action": "set_profile", "key": "goals", "value": "strength"})
        out = _tool_manage_workouts({"action": "readiness"})
        assert "Sessions (14d): 1" in out
        assert "3 sets" in out               # volume
        assert "body weight: 178.4 lb" in out
        assert "goals=strength" in out

    def test_readiness_includes_recovery_when_present(self, temp_store):
        _tool_manage_workouts({"action": "log_metric", "metric_type": "resting_hr", "value": "54", "unit": "bpm"})
        out = _tool_manage_workouts({"action": "readiness"})
        assert "resting hr: 54" in out


class TestSummaryFormatting:
    def test_groups_identical_sets(self):
        session = WorkoutSession(id="x", date="2026-06-07", sets=[
            WorkoutSet(exercise="Back Squat", set_index=1, reps=5, weight=185),
            WorkoutSet(exercise="Back Squat", set_index=2, reps=5, weight=185),
            WorkoutSet(exercise="Back Squat", set_index=3, reps=5, weight=185),
        ])
        out = _summarize_session(session)
        assert "Back Squat 3×5 @185 lb" in out

    def test_single_set_no_count(self):
        session = WorkoutSession(id="x", date="2026-06-07", sets=[
            WorkoutSet(exercise="Bench Press", set_index=1, reps=8, weight=135),
        ])
        out = _summarize_session(session)
        assert "Bench Press 8 @135 lb" in out

    def test_cardio_no_reps_weight(self):
        session = WorkoutSession(id="x", date="2026-06-07", sets=[
            WorkoutSet(exercise="Run", set_index=1, notes="4 mi"),
        ])
        out = _summarize_session(session)
        assert "Run" in out
