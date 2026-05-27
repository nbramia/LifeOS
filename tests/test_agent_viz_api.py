"""API tests for the read-only agent activity visualization (issue #133).

Covers /api/agents/snapshot, /api/agents/sessions/{sid}/events, and the
per-session SSE transcript tail. Uses temp-dir-backed stores via monkeypatch
so the real data/ directory is never touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Point the agents route at temp-dir-backed stores for the duration of the test."""
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    # Clear the label cache so labels are recomputed against the test fixtures.
    agents_route._label_cache.clear()
    # Disable the Claude Code union so these tests only exercise the LifeOS
    # agent path — otherwise the snapshot would mix in real CC sessions from
    # ~/.claude/projects/ on the test machine.
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.mark.unit
def test_snapshot_empty(client, stores):
    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert body["edges"] == []
    assert "generated_at" in body


@pytest.mark.unit
def test_snapshot_sessions_and_edges(client, stores):
    session_store, transcript_store = stores
    parent = session_store.create(task_id="root-task", status=STATUS_RUNNING, routing="claude")
    # Spawned child under the same root.
    child = session_store.create(
        task_id="child-task",
        status=STATUS_RUNNING,
        routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    transcript_store.append(parent.session_id, "claim", {"description": "Synthesize the briefing"})
    transcript_store.append(parent.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(parent.session_id, "tool_call", {"name": "lifeos_calendar_upcoming"})
    transcript_store.append(child.session_id, "spawn", {"prompt": "Sub-task: find conflicts"})
    transcript_store.append(child.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(child.session_id, "managed_failed", {"reason": "synthetic test"})

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    sessions = {s["session_id"]: s for s in body["sessions"]}
    assert parent.session_id in sessions
    assert child.session_id in sessions

    parent_s = sessions[parent.session_id]
    assert parent_s["label"] == "Synthesize the briefing"
    assert parent_s["tool_call_count"] == 2
    assert parent_s["error_count"] == 0
    assert parent_s["last_event_kind"] == "tool_call"
    assert parent_s["routing"] == "claude"

    child_s = sessions[child.session_id]
    assert child_s["tool_call_count"] == 1
    # `managed_failed` should be counted as an error.
    assert child_s["error_count"] == 1
    assert child_s["last_event_kind"] == "managed_failed"

    edges = body["edges"]
    assert {"from": parent.session_id, "to": child.session_id, "type": "spawn"} in edges


@pytest.mark.unit
def test_events_endpoint_returns_tail(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    for i in range(10):
        transcript_store.append(s.session_id, "tool_call", {"i": i})

    r = client.get(f"/api/agents/sessions/{s.session_id}/events?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10
    assert len(body["events"]) == 3
    # Tail should be the most-recent 3, in order.
    assert [ev["payload"]["i"] for ev in body["events"]] == [7, 8, 9]


@pytest.mark.unit
def test_events_endpoint_rejects_traversal(client, stores):
    r = client.get("/api/agents/sessions/..%2Fetc/events")
    # FastAPI normalizes %2F so the traversal token reaches the handler;
    # either 400 (validation) or 404 (path mismatch) is acceptable so long as
    # the endpoint does not return file contents from outside the transcripts dir.
    assert r.status_code in (400, 404)

    # Direct traversal segment in the session_id parameter.
    r2 = client.get("/api/agents/sessions/foo..bar/events")
    assert r2.status_code == 400


@pytest.mark.unit
def test_events_endpoint_missing_session(client, stores):
    r = client.get("/api/agents/sessions/missing-session/events")
    assert r.status_code == 200
    assert r.json() == {"session_id": "missing-session", "events": [], "total": 0}


@pytest.mark.unit
def test_per_session_stream_backfill_and_terminate_on_completed(client, stores):
    """Verify the per-session SSE stream sends backfill events and closes on terminal status."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "claim", {"description": "Test session"})
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})

    # Mark the session terminal so the stream closes promptly when the
    # terminal-check fires inside the generator.
    session_store.update_status("t1", STATUS_COMPLETED)

    with client.stream("GET", f"/api/agents/sessions/{s.session_id}/stream?backfill=5") as resp:
        assert resp.status_code == 200
        chunks: list[str] = []
        # Collect output until the stream closes (it should, after the
        # generator hits the terminal-status check). Bail out defensively
        # after a reasonable amount of data.
        for chunk in resp.iter_text():
            chunks.append(chunk)
            if "event: closed" in chunk:
                break
            if sum(len(c) for c in chunks) > 50_000:
                pytest.fail("stream did not close on terminal status")
        body = "".join(chunks)
        assert "event: transcript_event" in body
        assert '"kind": "claim"' in body
        assert '"kind": "tool_call"' in body
        assert "event: closed" in body


@pytest.mark.unit
def test_per_session_stream_rejects_traversal(client, stores):
    r = client.get("/api/agents/sessions/foo..bar/stream")
    assert r.status_code == 400


@pytest.mark.unit
def test_per_session_stream_emits_events_before_close(client, stores):
    """Verifies the stream emits transcript_event chunks before the terminal close event."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-mid", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "claim", {"description": "Running session"})
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})
    session_store.update_status("t-mid", STATUS_COMPLETED)

    transcript_seen = False
    closed = False
    with client.stream("GET", f"/api/agents/sessions/{s.session_id}/stream?backfill=5") as resp:
        assert resp.status_code == 200
        for chunk in resp.iter_text():
            if "event: transcript_event" in chunk and not closed:
                transcript_seen = True
            if "event: closed" in chunk:
                closed = True
                break
    assert transcript_seen, "transcript_event chunks should arrive before the close event"
    assert closed, "stream should emit event: closed when the session is terminal"


@pytest.mark.unit
def test_label_falls_back_to_task_manager_for_root_sessions(client, stores, monkeypatch):
    """When transcript events don't carry a description (the real worker case),
    the label should come from the task manager rather than being cached as
    the task_id fallback."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-tm", status=STATUS_RUNNING)
    # Real worker emits this payload — no description.
    transcript_store.append(s.session_id, "claim", {"task_id": "t-tm", "worker": "agent-worker"})

    # Stub the task manager so the label lookup hits a known value.
    class StubTask:
        description = "Review the Q4 budget"

    class StubManager:
        def get(self, task_id):
            return StubTask() if task_id == "t-tm" else None

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: StubManager(),
    )

    r = client.get("/api/agents/snapshot")
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["label"] == "Review the Q4 budget"


@pytest.mark.unit
def test_error_count_includes_killed_kinds(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-killed", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(s.session_id, "killed", {"by": "operator"})
    transcript_store.append(s.session_id, "cascade_killed", {"reason": "parent killed"})

    r = client.get("/api/agents/snapshot")
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["error_count"] == 2
    assert sess["tool_call_count"] == 1


@pytest.mark.unit
def test_label_cache_does_not_pin_fallback(client, stores, monkeypatch):
    """If the first snapshot tick fires before any descriptive event lands,
    the fallback (`task_id`) must not get cached. A subsequent tick with the
    real label available should pick it up."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-race", status=STATUS_RUNNING)
    # No transcript yet — simulate the race.

    class _MissingManager:
        def get(self, task_id):
            return None

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: _MissingManager(),
    )

    r1 = client.get("/api/agents/snapshot")
    sess1 = next(x for x in r1.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess1["label"] == "t-race"  # fallback to task_id

    # Now the real description becomes available (later claim event lands, OR
    # task_manager would resolve it). The cache must NOT have pinned the
    # fallback, so the new label takes effect.
    class _StubTask:
        description = "Real label arrives late"

    class _StubManager:
        def get(self, task_id):
            return _StubTask()

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: _StubManager(),
    )

    r2 = client.get("/api/agents/snapshot")
    sess2 = next(x for x in r2.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess2["label"] == "Real label arrives late"


@pytest.mark.unit
def test_snapshot_filters_yield_waiting_for(client, stores):
    """Ensure yield_waiting_for survives JSON round-trip as an array."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_BLOCKED)
    session_store.set_yield_waiting_for(s.task_id, ["child-1", "child-2"])

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["yield_waiting_for"] == ["child-1", "child-2"]


@pytest.mark.unit
def test_label_truncates_long_descriptions(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    long_desc = "x" * 200
    transcript_store.append(s.session_id, "claim", {"description": long_desc})

    r = client.get("/api/agents/snapshot")
    sess = r.json()["sessions"][0]
    assert len(sess["label"]) <= 60
    assert sess["label"].endswith("…")


@pytest.mark.unit
def test_model_label_local_routing(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t-local", status=STATUS_RUNNING, routing="local")
    r = client.get("/api/agents/snapshot")
    sess = r.json()["sessions"][0]
    assert sess["model_label"] == "Local"


@pytest.mark.unit
def test_model_label_claude_routing_derives_from_settings(client, stores, monkeypatch):
    """Claude-routed sessions surface a short label derived from the configured
    `agent_managed_model` setting — Sonnet / Haiku / Opus / Claude."""
    from config import settings as settings_mod
    session_store, _ = stores
    session_store.create(task_id="t-claude", status=STATUS_RUNNING, routing="claude")

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-haiku-4-5")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Haiku"

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-sonnet-4-6")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Sonnet"

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-opus-4-7")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Opus"
