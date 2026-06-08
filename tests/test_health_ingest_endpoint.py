"""
Tests for the authenticated Apple Health ingest endpoint and service (#333).

POST /api/fitness/health/ingest shares the ingest core with the file importer;
this covers the bearer-auth gate and that a posted payload lands in fitness.db.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.services.fitness_store as fs
from api.services.fitness_store import FitnessStore
from api.services.health_import import ingest_health
from api.routes import fitness as fitness_route

pytestmark = pytest.mark.unit


def _payload():
    return {
        "workouts": [{"uuid": "W1", "type": "Running", "start": "2026-06-07T08:00:00Z", "duration_s": 600}],
        "metrics": [{"type": "body_weight", "value": 178.4, "unit": "lb", "start": "2026-06-07T07:00:00Z"}],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    monkeypatch.setattr(fs, "_store_instance", store)  # get_fitness_store() → this
    app = FastAPI()
    app.include_router(fitness_route.router)
    client = TestClient(app)
    return client, store, monkeypatch


# -- service core --

class TestIngestService:
    def test_ingest_payload_lands_rows(self, env):
        _, store, _ = env
        result = ingest_health(_payload())
        assert result["workouts_created"] == 1
        assert result["metrics_created"] == 1
        assert len(store.list_sessions()) == 1
        assert store.latest_metric("body_weight").value == 178.4

    def test_ingest_idempotent(self, env):
        _, store, _ = env
        ingest_health(_payload())
        second = ingest_health(_payload())
        assert second["workouts_created"] == 0 and second["workouts_skipped"] == 1
        assert second["metrics_created"] == 0 and second["metrics_skipped"] == 1


# -- endpoint auth --

class TestIngestEndpoint:
    def test_disabled_without_token(self, env):
        client, _, monkeypatch = env
        monkeypatch.setattr(fitness_route.settings, "health_ingest_token", "")
        resp = client.post("/api/fitness/health/ingest", json=_payload())
        assert resp.status_code == 503

    def test_missing_bearer_rejected(self, env):
        client, _, monkeypatch = env
        monkeypatch.setattr(fitness_route.settings, "health_ingest_token", "secret-token")
        resp = client.post("/api/fitness/health/ingest", json=_payload())
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, env):
        client, _, monkeypatch = env
        monkeypatch.setattr(fitness_route.settings, "health_ingest_token", "secret-token")
        resp = client.post(
            "/api/fitness/health/ingest", json=_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_valid_token_ingests(self, env):
        client, store, monkeypatch = env
        monkeypatch.setattr(fitness_route.settings, "health_ingest_token", "secret-token")
        resp = client.post(
            "/api/fitness/health/ingest", json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workouts_created"] == 1 and body["metrics_created"] == 1
        assert len(store.list_sessions()) == 1

    def test_empty_payload_ok(self, env):
        client, _, monkeypatch = env
        monkeypatch.setattr(fitness_route.settings, "health_ingest_token", "secret-token")
        resp = client.post(
            "/api/fitness/health/ingest", json={},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["workouts_created"] == 0
