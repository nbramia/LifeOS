"""Tests for the #851 remote-kill path: `POST /sessions/{id}/kill` for a
session whose `host` names a machine other than the API host runs
`ssh <target> kill -- -<pgid>` through the injectable runner instead of a
local `killpg` — and never touches the network in tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import STATUS_RUNNING, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    monkeypatch.setattr(agents_route, "_maybe_managed_driver", lambda: None)
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)
    agents_route._label_cache.clear()
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_kill_remote_session_calls_injected_runner(client, stores, monkeypatch):
    session_store, transcript_store = stores
    session = session_store.create(
        task_id="remote-task", status=STATUS_RUNNING, routing="claude_code", host="studio",
    )
    session_store.set_remote_pgid("remote-task", 55555)
    transcript_store.append(session.session_id, "claude_code_spawn", {"host": "studio", "remote": True})

    calls = []

    class _FakeResult:
        returncode = 0

    def _fake_runner(argv):
        calls.append(argv)
        return _FakeResult()

    monkeypatch.setattr(agents_route, "_remote_kill_runner", _fake_runner)

    resp = client.post(f"/api/agents/sessions/{session.session_id}/kill", json={"reason": "test"})
    assert resp.status_code == 200
    assert resp.json()["killed"] == [session.session_id]

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "ssh"
    assert "user@studio.example" in argv
    assert argv[-3:] == ["kill", "--", "-55555"]

    events = [e["kind"] for e in transcript_store.read(session.session_id)]
    assert "remote_subprocess_kill_attempted" in events


def test_kill_remote_session_unregistered_host_is_best_effort_noop(client, stores, monkeypatch):
    """A session whose host isn't (or is no longer) in the registry must
    not crash the kill endpoint — it degrades to a DB-only kill."""
    session_store, transcript_store = stores
    session = session_store.create(
        task_id="remote-task-2", status=STATUS_RUNNING, routing="codex", host="vanished-host",
    )
    session_store.set_remote_pgid("remote-task-2", 111)

    calls = []
    monkeypatch.setattr(agents_route, "_remote_kill_runner", lambda argv: calls.append(argv))

    resp = client.post(f"/api/agents/sessions/{session.session_id}/kill", json={"reason": "test"})
    assert resp.status_code == 200
    assert calls == []  # no ssh call — host isn't registered
    refreshed = session_store.get("remote-task-2")
    assert refreshed.status == "failed"


def test_kill_local_session_unaffected_by_remote_path(client, stores, monkeypatch):
    """A session with no host still goes through the local killpg path,
    never the ssh runner."""
    session_store, transcript_store = stores
    session = session_store.create(task_id="local-task", status=STATUS_RUNNING, routing="claude_code")

    calls = []
    monkeypatch.setattr(agents_route, "_remote_kill_runner", lambda argv: calls.append(argv))

    resp = client.post(f"/api/agents/sessions/{session.session_id}/kill", json={"reason": "test"})
    assert resp.status_code == 200
    assert calls == []
