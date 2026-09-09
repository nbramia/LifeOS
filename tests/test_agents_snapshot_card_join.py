"""API tests for the /agents snapshot's card-join fields (`card_id`,
`card_title`, `assignee`, `card_tags`) — the fields `web/agents/graph.js`
groups sessions by to build a card anchor. Uses temp-dir-backed stores via
monkeypatch, mirroring `tests/test_agents_board_api.py`'s fixture so a real
`TaskManager` (not a stub) backs the join.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
import api.services.task_manager as task_manager_module
from api.services.task_manager import TaskManager
from api.services.agent_worker.session_store import STATUS_RUNNING, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore

pytestmark = pytest.mark.unit


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))

    task_manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json",
    )
    monkeypatch.setattr(task_manager_module, "_task_manager", task_manager)

    yield task_manager, session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_snapshot_reports_card_join_fields_for_linked_session(client, stores):
    task_manager, session_store, _ = stores
    task = task_manager.create("Synthesize the weekly briefing", tags=["codex", "urgent"])
    session_store.create(task_id=task.id, status=STATUS_RUNNING, routing="codex")

    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["card_id"] == task.id
    assert sess["card_title"] == "Synthesize the weekly briefing"
    assert sess["assignee"] == "codex"
    assert sorted(sess["card_tags"]) == ["codex", "urgent"]


def test_snapshot_card_join_fields_are_null_for_unlinked_session(client, stores):
    _, session_store, _ = stores
    session_store.create(task_id=None, status=STATUS_RUNNING, routing="local")

    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["card_id"] is None
    assert sess["card_title"] is None
    assert sess["assignee"] is None
    assert sess["card_tags"] == []
    # Deliberate deviation from a literal "null for all three [incl. lane]"
    # reading of the acceptance criterion: `lane` keeps its own
    # session-status-derived value for an unlinked row (agent_board's own
    # derivation), never null — the node fill colour and lane legend depend
    # on every row carrying one.
    assert sess["lane"] is not None


def test_snapshot_card_join_fields_null_when_linked_task_not_found(client, stores):
    _, session_store, _ = stores
    session_store.create(task_id="t-does-not-exist", status=STATUS_RUNNING, routing="local")

    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["card_id"] is None
    assert sess["card_title"] is None
    assert sess["assignee"] is None
    assert sess["card_tags"] == []


def test_snapshot_card_join_reflects_unassigned_task_with_no_tags(client, stores):
    task_manager, session_store, _ = stores
    task = task_manager.create("Untagged task")
    session_store.create(task_id=task.id, status=STATUS_RUNNING, routing="local")

    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["card_id"] == task.id
    assert sess["card_title"] == "Untagged task"
    assert sess["assignee"] is None
    assert sess["card_tags"] == []


def test_snapshot_card_join_multiple_sessions_same_task_one_lookup(client, stores, monkeypatch):
    """Two sessions linked to the same task must both carry its card fields
    — proves the shared `tasks_by_id` dict is keyed correctly rather than
    silently only covering the first row it's built from. `SessionStore`
    itself enforces one row per `task_id` (a unique constraint — see
    `test_agents_snapshot_card_join.py`'s sibling tests, which all use it),
    so the two sessions here come from the Claude Code CLI ingest union
    instead, which carries no such constraint (a task can be worked by
    several CC sessions/subagents over time)."""
    task_manager, session_store, _ = stores
    task = task_manager.create("Shared card", tags=["me"])
    cc_rows = [
        {"session_id": "cc:shared-1", "task_id": task.id, "status": STATUS_RUNNING, "last_activity_at": 1000.0},
        {"session_id": "cc:shared-2", "task_id": task.id, "status": STATUS_RUNNING, "last_activity_at": 2000.0},
    ]
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: (cc_rows, []))

    get_calls = []
    real_get = task_manager.get

    def counting_get(task_id):
        get_calls.append(task_id)
        return real_get(task_id)

    monkeypatch.setattr(task_manager, "get", counting_get)

    body = client.get("/api/agents/snapshot").json()
    assert len(body["sessions"]) == 2
    for sess in body["sessions"]:
        assert sess["card_id"] == task.id
        assert sess["card_title"] == "Shared card"
        assert sess["assignee"] == "me"
    # One task id, looked up once — not once per session row.
    assert get_calls == [task.id]
