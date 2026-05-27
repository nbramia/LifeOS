"""API + helper tests for operator kill (issue #134).

Covers the new POST /api/agents/sessions/{sid}/kill endpoint and the
shared `teardown_session` helper that agent-initiated kill and operator
kill both call into.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker import inter_agent
from api.services.agent_worker.inter_agent import InterAgentContext, Caps, teardown_session
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    # Disable managed-driver instantiation so the endpoint runs purely against
    # the local DB — `teardown_session` then degrades to a local-only kill.
    monkeypatch.setattr(agents_route, "_maybe_managed_driver", lambda: None)
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _make_subtree(session_store: SessionStore):
    """Build a synthetic root + two children + one grandchild."""
    root = session_store.create(task_id="root-task", status=STATUS_RUNNING, routing="claude")
    child_a = session_store.create(
        task_id="child-a",
        status=STATUS_RUNNING,
        routing="local",
        parent_session_id=root.session_id,
        root_session_id=root.session_id,
        spawn_depth=1,
    )
    child_b = session_store.create(
        task_id="child-b",
        status=STATUS_RUNNING,
        routing="local",
        parent_session_id=root.session_id,
        root_session_id=root.session_id,
        spawn_depth=1,
    )
    grandchild = session_store.create(
        task_id="grand",
        status=STATUS_RUNNING,
        routing="local",
        parent_session_id=child_a.session_id,
        root_session_id=root.session_id,
        spawn_depth=2,
    )
    return root, child_a, child_b, grandchild


@pytest.mark.unit
def test_kill_root_cascades_to_all_descendants(client, stores):
    session_store, transcript_store = stores
    root, child_a, child_b, grandchild = _make_subtree(session_store)

    r = client.post(f"/api/agents/sessions/{root.session_id}/kill", json={"reason": "operator stop"})
    assert r.status_code == 200
    body = r.json()
    killed = set(body["killed"])
    assert killed == {root.session_id, child_a.session_id, child_b.session_id, grandchild.session_id}
    assert body["failures"] == []

    for s in (root, child_a, child_b, grandchild):
        assert session_store.get_by_session_id(s.session_id).status == STATUS_FAILED


@pytest.mark.unit
def test_kill_non_root_only_kills_target_subtree(client, stores):
    """Killing a non-root target must NOT take down unrelated peers."""
    session_store, transcript_store = stores
    root, child_a, child_b, grandchild = _make_subtree(session_store)

    r = client.post(f"/api/agents/sessions/{child_a.session_id}/kill", json={})
    assert r.status_code == 200
    killed = set(r.json()["killed"])
    # Only child_a + its grandchild should be killed; root and child_b stay running.
    assert killed == {child_a.session_id, grandchild.session_id}
    assert session_store.get_by_session_id(root.session_id).status == STATUS_RUNNING
    assert session_store.get_by_session_id(child_b.session_id).status == STATUS_RUNNING
    assert session_store.get_by_session_id(child_a.session_id).status == STATUS_FAILED
    assert session_store.get_by_session_id(grandchild.session_id).status == STATUS_FAILED


@pytest.mark.unit
def test_kill_transcript_kinds(client, stores):
    session_store, transcript_store = stores
    root, child_a, _, grandchild = _make_subtree(session_store)

    client.post(f"/api/agents/sessions/{root.session_id}/kill", json={"reason": "test"})

    # Target gets operator_killed; descendants get cascade_killed.
    root_events = transcript_store.read(root.session_id)
    assert any(e["kind"] == "operator_killed" for e in root_events)
    assert root_events[-1]["payload"].get("reason") == "test"

    child_events = transcript_store.read(child_a.session_id)
    assert any(e["kind"] == "cascade_killed" for e in child_events)
    cascade_ev = next(e for e in child_events if e["kind"] == "cascade_killed")
    assert cascade_ev["payload"].get("root") == root.session_id

    grand_events = transcript_store.read(grandchild.session_id)
    assert any(e["kind"] == "cascade_killed" for e in grand_events)


@pytest.mark.unit
def test_kill_already_terminal_session_is_noop(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-done", status=STATUS_COMPLETED, routing="local")

    r = client.post(f"/api/agents/sessions/{s.session_id}/kill", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["killed"] == []
    assert body.get("reason", "").startswith("already")
    # Status untouched.
    assert session_store.get_by_session_id(s.session_id).status == STATUS_COMPLETED
    # No new transcript events.
    assert transcript_store.read(s.session_id) == []


@pytest.mark.unit
def test_kill_unknown_session_returns_404(client, stores):
    r = client.post("/api/agents/sessions/does-not-exist/kill", json={})
    assert r.status_code == 404


@pytest.mark.unit
def test_kill_calls_managed_driver_when_present(client, stores, monkeypatch):
    """When a managed driver is available, kill_session() is invoked for each
    managed remote in the subtree. Failures are reported in the response but
    do not stop the local cascade."""
    session_store, transcript_store = stores
    # Synthetic subtree where one node has a managed remote.
    parent = session_store.create(task_id="p", status=STATUS_RUNNING, routing="claude")
    session_store.set_managed_session_id(parent.task_id, "managed-parent")
    child = session_store.create(
        task_id="c", status=STATUS_RUNNING, routing="claude",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    session_store.set_managed_session_id(child.task_id, "managed-child")
    # Re-fetch so the in-memory Session objects reflect the managed IDs.
    parent = session_store.get_by_session_id(parent.session_id)
    child = session_store.get_by_session_id(child.session_id)

    driver = MagicMock()
    driver.kill_session.side_effect = [None, RuntimeError("network error")]
    monkeypatch.setattr(agents_route, "_maybe_managed_driver", lambda: driver)

    r = client.post(f"/api/agents/sessions/{parent.session_id}/kill", json={})
    assert r.status_code == 200
    body = r.json()
    assert set(body["killed"]) == {parent.session_id, child.session_id}
    assert len(body["failures"]) == 1
    assert body["failures"][0]["session_id"] == child.session_id

    # Both managed remotes called.
    assert driver.kill_session.call_count == 2
    called_remote_ids = {call.args[0] for call in driver.kill_session.call_args_list}
    assert called_remote_ids == {"managed-parent", "managed-child"}


# ---------------------------------------------------------------------------
# Shared helper — used by both inter_agent.kill (agent-to-agent) and the
# operator kill HTTP endpoint. Confirms the agent path still works.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_teardown_session_helper_flips_status_and_writes_transcript(tmp_path):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)

    result = teardown_session(
        session_store, transcript_store, s,
        transcript_kind="killed",
        transcript_payload={"by": "test", "reason": "synthetic"},
        managed_driver=None,
    )
    assert result["managed_failure"] is None
    assert session_store.get_by_session_id(s.session_id).status == STATUS_FAILED
    events = transcript_store.read(s.session_id)
    assert events[-1]["kind"] == "killed"
    assert events[-1]["payload"]["by"] == "test"


@pytest.mark.unit
def test_inter_agent_kill_still_works_via_shared_helper(tmp_path):
    """Verifies the agent-to-agent kill path (inter_agent.kill) still produces
    the same observable effect after the refactor to share `teardown_session`."""
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")

    caller = session_store.create(task_id="caller", status=STATUS_RUNNING)
    target = session_store.create(
        task_id="target",
        status=STATUS_RUNNING,
        parent_session_id=caller.session_id,
        root_session_id=caller.session_id,
        spawn_depth=1,
    )

    ctx = InterAgentContext(
        session_store=session_store,
        transcript_store=transcript_store,
        caller_session_id=caller.session_id,
        caps=Caps(),
        managed_driver=None,
    )
    result = inter_agent.kill(ctx, {"session_id": target.session_id, "reason": "test"})
    assert result["ok"] is True
    assert session_store.get_by_session_id(target.session_id).status == STATUS_FAILED
    events = transcript_store.read(target.session_id)
    assert events[-1]["kind"] == "killed"
    assert events[-1]["payload"]["by"] == caller.session_id
