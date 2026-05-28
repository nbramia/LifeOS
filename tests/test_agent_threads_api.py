"""API tests for the web /chat agent-threads endpoints (#236, Phase 3).

Covers GET /api/agents/threads, GET /api/agents/threads/{id},
POST /api/agents/threads/{id}/reply, and POST /api/agents/spawn.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore

pytestmark = pytest.mark.unit


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# GET /threads
# ---------------------------------------------------------------------------


def test_list_threads_returns_only_root_sessions(stores, client):
    session_store, _ = stores
    root = session_store.create(task_id="root", status=STATUS_COMPLETED, routing="local")
    session_store.create(
        task_id="child", status=STATUS_RUNNING, routing="local",
        parent_session_id=root.session_id, root_session_id=root.session_id, spawn_depth=1,
    )

    resp = client.get("/api/agents/threads")
    assert resp.status_code == 200
    data = resp.json()
    ids = {t["session_id"] for t in data["threads"]}
    assert root.session_id in ids
    # The spawned child is internal — not surfaced as a conversable thread.
    assert all(t["parent_session_id"] is None for t in data["threads"])
    # Completed root is resumable.
    root_t = next(t for t in data["threads"] if t["session_id"] == root.session_id)
    assert root_t["resumable"] is True


def test_running_thread_not_resumable(stores, client):
    session_store, _ = stores
    session_store.create(task_id="r", status=STATUS_RUNNING, routing="local")
    resp = client.get("/api/agents/threads")
    t = resp.json()["threads"][0]
    assert t["resumable"] is False


# ---------------------------------------------------------------------------
# GET /threads/{id}
# ---------------------------------------------------------------------------


def test_get_thread_detail(stores, client):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t", status=STATUS_COMPLETED, routing="claude")
    transcript_store.append(s.session_id, "claim", {"task_id": "t"})

    resp = client.get(f"/api/agents/threads/{s.session_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread"]["session_id"] == s.session_id
    assert data["total"] == 1
    assert data["events"][0]["kind"] == "claim"


def test_get_thread_404(stores, client):
    resp = client.get("/api/agents/threads/sess_does_not_exist")
    assert resp.status_code == 404


def test_thread_conversation_from_messages(stores, client):
    """The thread detail reconstructs a user/assistant conversation (with tool
    calls attached) from the local messages table."""
    session_store, _ = stores
    s = session_store.create(task_id="t", status=STATUS_COMPLETED, routing="local")
    sid = s.session_id
    session_store.append_message(sid, "system", "you are an agent")
    session_store.append_message(sid, "user", "research the best CRMs")
    session_store.append_message(sid, "assistant", [
        {"type": "text", "text": "Here are the top 3."},
        {"type": "tool_use", "id": "x", "name": "lifeos_search", "input": {"q": "CRM"}},
    ])
    # Tool results fed back as a user turn — internal plumbing, should be hidden.
    session_store.append_message(sid, "user", [
        {"type": "tool_result", "tool_use_id": "x", "content": "12 results"},
    ])
    session_store.append_message(sid, "assistant", [{"type": "text", "text": "HubSpot fits best."}])

    conv = client.get(f"/api/agents/threads/{sid}").json()["conversation"]
    assert [t["role"] for t in conv] == ["user", "assistant", "assistant"]
    assert conv[0]["text"] == "research the best CRMs"
    assert conv[1]["text"] == "Here are the top 3."
    assert conv[1]["tools"] == [{"name": "lifeos_search", "input": {"q": "CRM"}}]
    assert conv[2]["text"] == "HubSpot fits best."


def test_reconstruct_conversation_from_managed_events():
    """Cloud sessions (no messages-table rows) reconstruct from transcript."""
    from api.routes.agents import _reconstruct_conversation
    events = [
        {"kind": "managed_event_agent.tool_use", "payload": {"name": "drive_search", "input": {"q": "x"}}},
        {"kind": "managed_event_agent.message", "payload": {"content": [{"text": "Found 3 files."}]}},
        {"kind": "managed_event_agent.message", "payload": {"content": [{"text": "Done."}]}},
    ]
    conv = _reconstruct_conversation([], events)
    assert conv[0]["role"] == "assistant"
    assert conv[0]["tools"] == [{"name": "drive_search", "input": {"q": "x"}}]
    assert conv[0]["text"] == "Found 3 files."
    assert conv[1]["text"] == "Done." and conv[1]["tools"] == []


def test_reconstruct_conversation_empty():
    from api.routes.agents import _reconstruct_conversation
    assert _reconstruct_conversation([], []) == []


# ---------------------------------------------------------------------------
# POST /threads/{id}/reply
# ---------------------------------------------------------------------------


def test_reply_to_completed_thread_queues_followup(stores, client):
    session_store, _ = stores
    s = session_store.create(task_id="t", status=STATUS_COMPLETED, routing="local")

    resp = client.post(f"/api/agents/threads/{s.session_id}/reply", json={"text": "also CC Jane"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    # A pre-answered followup row is now waiting for the worker to resume.
    pending = session_store.list_answered_unprocessed_questions()
    assert len(pending) == 1
    assert pending[0]["kind"] == "followup"
    assert pending[0]["answer"] == "also CC Jane"
    assert pending[0]["session_id"] == s.session_id


def test_reply_to_running_thread_409(stores, client):
    session_store, _ = stores
    s = session_store.create(task_id="t", status=STATUS_RUNNING, routing="local")
    resp = client.post(f"/api/agents/threads/{s.session_id}/reply", json={"text": "hi"})
    assert resp.status_code == 409


def test_reply_unknown_thread_404(stores, client):
    resp = client.post("/api/agents/threads/nope/reply", json={"text": "hi"})
    assert resp.status_code == 404


def test_reply_empty_text_400(stores, client):
    session_store, _ = stores
    s = session_store.create(task_id="t", status=STATUS_COMPLETED, routing="local")
    resp = client.post(f"/api/agents/threads/{s.session_id}/reply", json={"text": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /spawn
# ---------------------------------------------------------------------------


def test_spawn_explicit_claude(stores, client):
    session_store, _ = stores
    resp = client.post("/api/agents/spawn", json={"prompt": "refactor the parser", "routing": "claude"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] and data["routing"] == "claude"
    # The session landed in the store as an operator root-spawn.
    s = session_store.get_by_session_id(data["session_id"])
    assert s is not None and s.origin == "operator" and s.routing == "claude"


def test_spawn_empty_prompt_400(stores, client):
    resp = client.post("/api/agents/spawn", json={"prompt": "  "})
    assert resp.status_code == 400


def test_spawn_auto_routing_uses_preflight(stores, client, monkeypatch):
    # "auto" → no explicit routing → create_operator_session runs preflight.
    # Patch it so the test doesn't make a network call.
    import api.services.agent_worker.operator_spawn as op

    def fake_preflight(title, tags=None, caller=None):
        from api.services.agent_worker.preflight import PreflightResult, PreflightBudget
        return PreflightResult(
            budget=PreflightBudget(wall_seconds=60, max_tokens=100, max_dollars=1.0),
            routing="local", routing_reason="t", expected_output="text",
            ambiguity=None, sane=True, sane_reason="", raw={},
        )

    monkeypatch.setattr(op, "run_preflight", fake_preflight)
    resp = client.post("/api/agents/spawn", json={"prompt": "summarize my week", "routing": "auto"})
    assert resp.status_code == 200
    assert resp.json()["routing"] == "local"


def test_spawn_auto_ambiguous_returns_409_and_cleans_up(stores, client, monkeypatch):
    """When preflight is ambiguous (ROUTE_ASK), the web spawn has no inline
    clarification flow — it must 409 and not leave a parked session behind."""
    session_store, _ = stores
    import api.services.agent_worker.operator_spawn as op

    def ask_preflight(title, tags=None, caller=None):
        from api.services.agent_worker.preflight import PreflightResult, PreflightBudget
        return PreflightResult(
            budget=PreflightBudget(wall_seconds=60, max_tokens=100, max_dollars=1.0),
            routing="ask", routing_reason="ambiguous", expected_output="text",
            ambiguity=None, sane=True, sane_reason="", raw={},
        )

    monkeypatch.setattr(op, "run_preflight", ask_preflight)
    resp = client.post("/api/agents/spawn", json={"prompt": "do the thing", "routing": "auto"})
    assert resp.status_code == 409
    # No parked operator session was left behind.
    assert session_store.list_sessions(status="blocked") == []
