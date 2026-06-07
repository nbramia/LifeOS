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
