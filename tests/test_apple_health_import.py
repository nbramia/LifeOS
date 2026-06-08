"""
Tests for the Apple Health import (issue #323).

The iOS Shortcut writes health.json; import_health upserts workouts into
workout_sessions(source=apple_health) and metrics into health_metrics, both
idempotently with UTC-normalized timestamps.
"""
import json
from pathlib import Path

import pytest

from scripts.apple_data_import import import_health, _to_utc_iso, _health_kind, _workout_summary
from api.services.fitness_store import FitnessStore

pytestmark = pytest.mark.unit


@pytest.fixture
def health_env(tmp_path, monkeypatch):
    """A temp fitness store + a temp health.json wired into the importer."""
    store = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    monkeypatch.setattr("api.services.fitness_store.get_fitness_store", lambda: store)
    export = tmp_path / "health.json"
    import config.settings as cfg
    monkeypatch.setattr(cfg.settings, "health_export_path", str(export))
    return store, export


def _write(export: Path, payload: dict):
    export.write_text(json.dumps(payload))


# -- helpers --

def test_to_utc_iso_normalizes_offset_and_z():
    assert _to_utc_iso("2026-06-07T08:00:00-04:00").startswith("2026-06-07T12:00:00+00:00")
    assert _to_utc_iso("2026-06-07T12:00:00Z").startswith("2026-06-07T12:00:00+00:00")
    assert _to_utc_iso("") == ""


def test_health_kind_maps_apple_types():
    assert _health_kind("HKWorkoutActivityTypeRunning") == "cardio"
    assert _health_kind("functionalStrengthTraining") == "strength"
    assert _health_kind("Yoga") == "mobility"
    assert _health_kind("Pickleball") == "cardio"  # default
    assert _health_kind(None) == "cardio"


def test_workout_summary_formats():
    s = _workout_summary({"distance_m": 10000, "duration_s": 3300, "avg_hr": 145})
    assert "10.0 km" in s and "55 min" in s and "145 bpm" in s


# -- import --

def test_missing_file_skips(health_env):
    store, export = health_env  # export not written
    result = import_health()
    assert result["status"] == "skipped"


def test_imports_workouts_and_metrics(health_env):
    store, export = health_env
    _write(export, {
        "workouts": [
            {"uuid": "W1", "type": "HKWorkoutActivityTypeRunning",
             "start": "2026-06-07T08:00:00-04:00", "end": "2026-06-07T08:55:00-04:00",
             "duration_s": 3300, "distance_m": 10000, "avg_hr": 145},
        ],
        "metrics": [
            {"type": "body_weight", "value": 178.4, "unit": "lb", "start": "2026-06-07T07:00:00-04:00"},
            {"type": "resting_hr", "value": 54, "unit": "bpm", "start": "2026-06-07T07:00:00-04:00"},
        ],
    })
    result = import_health()
    assert result["status"] == "ok"
    assert result["workouts_created"] == 1
    assert result["metrics_created"] == 2

    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].source == "apple_health"
    assert sessions[0].kind == "cardio"
    assert sessions[0].date == "2026-06-07"
    assert "10.0 km" in sessions[0].notes
    assert store.latest_metric("body_weight").value == 178.4


def test_idempotent_reimport(health_env):
    store, export = health_env
    payload = {
        "workouts": [{"uuid": "W1", "type": "Running", "start": "2026-06-07T08:00:00Z", "duration_s": 600}],
        "metrics": [{"type": "body_weight", "value": 178.0, "start": "2026-06-07T07:00:00Z"}],
    }
    _write(export, payload)
    import_health()
    second = import_health()
    assert second["workouts_created"] == 0 and second["workouts_skipped"] == 1
    assert second["metrics_created"] == 0 and second["metrics_skipped"] == 1
    assert len(store.list_sessions()) == 1
    assert len(store.list_metrics("body_weight")) == 1


def test_metrics_stored_in_utc(health_env):
    store, export = health_env
    _write(export, {"metrics": [{"type": "body_weight", "value": 178.0, "start": "2026-06-07T07:00:00-04:00"}]})
    import_health()
    m = store.latest_metric("body_weight")
    assert m.start_at.startswith("2026-06-07T11:00:00+00:00")  # -04:00 → UTC


def test_bad_rows_skipped(health_env):
    store, export = health_env
    _write(export, {
        "workouts": [{"type": "Running"}],                       # no uuid → skip
        "metrics": [{"type": "body_weight"}, {"value": 5}],      # no value / no type → skip
    })
    result = import_health()
    assert result["status"] == "ok"
    assert result["workouts_created"] == 0
    assert result["metrics_created"] == 0


def test_malformed_json_errors_gracefully(health_env):
    store, export = health_env
    export.write_text("{not json")
    result = import_health()
    assert result["status"] == "error"
