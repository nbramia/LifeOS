"""Board projection and interactive-open guards for projects."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import agents as agent_routes
from api.routes.agents import _task_card
from api.services.agent_worker.session_store import STATUS_RUNNING, SessionStore
from api.services.task_manager import TaskManager
from api.services.task_projects import ProjectTaskService, build_task_hierarchy

pytestmark = pytest.mark.unit


def test_project_card_contains_compact_summary_and_explicit_actions(tmp_path: Path):
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda task_id, status, tags: sessions.has_live_session(
            task_id, status=status, tags=tags,
        ),
    )
    parent = manager.create("Synthetic project", tags=["codex"])
    manager.create("Synthetic child", status="done", fields={"parent_id": parent.id})
    hierarchy = build_task_hierarchy(manager.list_tasks())
    service = ProjectTaskService(manager, sessions)
    fields = hierarchy.read_fields(parent.id, service.coordinator_view(parent))

    card = _task_card(parent, {}, {}, sessions, {}, {}, fields)

    assert card["is_project"] is True
    assert card["child_count"] == 1
    assert card["project"]["counts"]["done"] == 1
    assert card["policy"]["can_start_project"] is True
    assert card["policy"]["can_plan_project"] is True
    assert card["policy"]["can_complete_project"] is True
    assert card["policy"]["cancel"]["allowed"] is False
    assert "Cancel project" in card["policy"]["cancel"]["reason"]


def test_interactive_open_refuses_project(tmp_path: Path, monkeypatch):
    from api.routes import agent_assignment

    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create("Synthetic project", tags=["codex"])
    manager.create("Synthetic child", fields={"parent_id": parent.id})
    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_assignment, "_session_store", SessionStore(tmp_path / "sessions.db"))

    response = TestClient(app).post(f"/api/agents/board/cards/{parent.id}/open")

    assert response.status_code == 409
    assert "project" in response.json()["detail"]


def test_pending_project_cancellation_remains_retryable(tmp_path: Path):
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create("Synthetic project", tags=["codex"])
    manager.create("Synthetic child", fields={"parent_id": parent.id})
    manager.update(
        parent.id,
        fields={"project_cancel_operation_id": "cancel-synthetic-retry"},
        _project_operation="cancel",
    )
    hierarchy = build_task_hierarchy(manager.list_tasks())
    fields = hierarchy.read_fields(parent.id, None)

    card = _task_card(parent, {}, {}, sessions, {}, {}, fields)

    assert card["project"]["cancellation_pending"] is True
    assert card["policy"]["can_cancel_project"] is True


@pytest.mark.parametrize("cancel_child", [False, True])
def test_legacy_cancel_refuses_hierarchy_conflicts_before_session_teardown(
    tmp_path: Path, monkeypatch, cancel_child: bool,
):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    parent = manager.create("Synthetic project", tags=["codex"])
    child = manager.create("Synthetic child", tags=["codex"], fields={"parent_id": parent.id})
    target = child if cancel_child else parent
    if cancel_child:
        manager.update(
            parent.id,
            fields={"project_cancel_operation_id": "cancel-synthetic"},
            _project_operation="cancel",
        )
    sessions.create(target.id, status=STATUS_RUNNING)
    stopped = []

    async def no_teardown(*_args):
        stopped.append(True)
        return [], []

    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_routes, "_session_store", sessions)
    monkeypatch.setattr(agent_routes, "_kill_session_subtree", no_teardown)

    response = TestClient(app).post(f"/api/agents/board/cards/{target.id}/cancel")

    assert response.status_code == 409
    assert manager.get(target.id).status == "todo"
    assert stopped == []


def test_legacy_cancel_rechecks_hierarchy_before_write(tmp_path: Path, monkeypatch):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    task = manager.create("Synthetic task", tags=["codex"])
    reads = 0

    def hierarchy_facts(_task_id):
        nonlocal reads
        reads += 1
        return {
            "is_project": reads > 1,
            "hierarchy_valid": True,
            "parent_cancellation_pending": False,
            "project": None,
        }

    monkeypatch.setattr(manager, "project_read_fields", hierarchy_facts)
    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_routes, "_session_store", sessions)

    response = TestClient(app).post(f"/api/agents/board/cards/{task.id}/cancel")

    assert response.status_code == 409
    assert manager.get(task.id).status == "todo"
    assert reads >= 2


def test_lane_move_refuses_project_without_writing_assignee(tmp_path: Path, monkeypatch):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    parent = manager.create("Synthetic project", tags=["codex"])
    manager.create("Synthetic child", fields={"parent_id": parent.id})
    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_routes, "_session_store", sessions)

    response = TestClient(app).put(
        f"/api/agents/board/cards/{parent.id}/lane", json={"lane": "unassigned"},
    )

    assert response.status_code == 409
    assert manager.get(parent.id).tags == ["codex"]
