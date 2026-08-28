"""Tests for the /api/jobs/* endpoints' additive `stale` field (#768).

A job stuck "running" because its owning process restarted mid-job is
reconciled to failed/orphaned at the next worker startup (see
tests/test_job_queue.py's TestOrphanReconciliation), but a caller polling a
specific job may read it before that reconciliation runs. `stale` surfaces
the same signal (JobQueue.process_start_time vs. the job's started_at)
independently, without waiting on it.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import jobs as jobs_route

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path, monkeypatch):
    from api.services.job_queue import JobQueue
    queue = JobQueue(db_path=str(tmp_path / "jobs.db"))
    monkeypatch.setattr(jobs_route, "get_job_queue", lambda: queue)
    app = FastAPI()
    app.include_router(jobs_route.router)
    yield TestClient(app), queue


def test_get_job_not_stale_when_running_normally(client):
    c, queue = client
    job_id = queue.enqueue("a")
    queue._claim_next()
    r = c.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is False


def test_get_job_stale_when_started_before_process_start(client):
    c, queue = client
    job_id = queue.enqueue("a")
    queue._claim_next()
    # Simulate a process restart: process_start_time now postdates started_at.
    queue.process_start_time = "9999-01-01T00:00:00+00:00"
    r = c.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is True


def test_get_job_not_stale_when_completed(client):
    c, queue = client
    job_id = queue.enqueue("a")
    queue._claim_next()
    queue._mark_completed(job_id, {"ok": True})
    queue.process_start_time = "9999-01-01T00:00:00+00:00"
    r = c.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is False


def test_list_jobs_includes_stale_field(client):
    c, queue = client
    job_id = queue.enqueue("a")
    queue._claim_next()
    queue.process_start_time = "9999-01-01T00:00:00+00:00"
    r = c.get("/api/jobs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"][0]["id"] == job_id
    assert body["jobs"][0]["stale"] is True
