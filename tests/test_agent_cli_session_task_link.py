"""Tests for #851's task_id linkage on CLI session registration: a
`session_start` event naming a `task_id` (forwarded by
`scripts/lifeos-agent-hook.sh` from `$LIFEOS_TASK_ID` — see #849) moves that
task from `todo` to `in_progress`. Board `POST /board/cards/{id}/open`
(`api/routes/agent_assignment.py`) sets `LIFEOS_TASK_ID` when it spawns the
interactive CLI, so this is what turns "opened" into "In progress" on the
board.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import SessionStore
from api.services.task_manager import TaskManager


pytestmark = pytest.mark.unit

TOKEN = "test-hook-token-value"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hook_token", TOKEN)
    yield session_store


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    import api.services.task_manager as tm_mod
    m = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "index.json")
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: m)
    return m


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _post(client, body, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    return client.post("/api/agents/cli-sessions/events", json=body, headers=headers)


def test_session_start_with_task_id_moves_todo_card_to_in_progress(client, stores, manager):
    task = manager.create(description="fix the printer", tags=["agent", "claude"])
    assert task.status == "todo"

    resp = _post(client, {
        "engine": "claude_code", "event": "session_start", "session_id": "abc-1",
        "host": "laptop-a", "cwd": "/home/x", "task_id": task.id,
    })
    assert resp.status_code == 200

    refreshed = manager.get(task.id)
    assert refreshed.status == "in_progress"


def test_session_start_without_task_id_is_unaffected(client, stores, manager):
    resp = _post(client, {
        "engine": "claude_code", "event": "session_start", "session_id": "abc-2",
        "host": "laptop-a", "cwd": "/home/x",
    })
    assert resp.status_code == 200  # no task_id -> nothing to link, no error


def test_session_start_task_already_in_progress_is_left_alone(client, stores, manager):
    task = manager.create(description="fix the printer", tags=["agent", "claude"])
    manager.update(task.id, status="in_progress")

    resp = _post(client, {
        "engine": "claude_code", "event": "session_start", "session_id": "abc-3",
        "host": "laptop-a", "cwd": "/home/x", "task_id": task.id,
    })
    assert resp.status_code == 200
    assert manager.get(task.id).status == "in_progress"


def test_session_start_unknown_task_id_does_not_fail_the_event(client, stores, manager):
    resp = _post(client, {
        "engine": "claude_code", "event": "session_start", "session_id": "abc-4",
        "host": "laptop-a", "cwd": "/home/x", "task_id": "no-such-task",
    })
    assert resp.status_code == 200


def test_user_prompt_submit_does_not_move_the_task(client, stores, manager):
    """Only session_start links+moves the card — a later user_prompt_submit
    on the same session must not re-trigger it (harmless either way, but
    confirms the gate is on the event type, not just task_id presence)."""
    task = manager.create(description="fix the printer", tags=["agent", "claude"])
    _post(client, {
        "engine": "claude_code", "event": "session_start", "session_id": "abc-5",
        "host": "laptop-a", "cwd": "/home/x", "task_id": task.id,
    })
    manager.update(task.id, status="todo")  # simulate an operator reverting it
    _post(client, {
        "engine": "claude_code", "event": "user_prompt_submit", "session_id": "abc-5",
        "host": "laptop-a", "prompt_preview": "go", "task_id": task.id,
    })
    assert manager.get(task.id).status == "todo"
