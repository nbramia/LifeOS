"""HTTP contract coverage for task projects."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def project_api(tmp_path: Path, monkeypatch):
    from api.routes import tasks as tasks_route

    sessions = SessionStore(tmp_path / "sessions.db")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda task_id, status, tags: sessions.has_live_session(
            task_id, status=status, tags=tags,
        ),
    )
    monkeypatch.setattr(tasks_route, "get_task_manager", lambda: manager)
    monkeypatch.setattr(tasks_route, "_session_store", sessions)
    monkeypatch.setattr(tasks_route, "_transcript_store", TranscriptStore(tmp_path / "transcripts"))
    return TestClient(app), manager, sessions


def test_task_list_summary_is_computed_before_filters(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["codex"])
    open_child = manager.create("Synthetic open child", fields={"parent_id": parent.id})
    done_child = manager.create(
        "Synthetic done child", status="done", fields={"parent_id": parent.id},
    )

    response = client.get("/api/tasks", params={"status": "todo"})
    assert response.status_code == 200
    by_id = {task["id"]: task for task in response.json()["tasks"]}
    assert done_child.id not in by_id
    assert by_id[parent.id]["is_project"] is True
    assert by_id[parent.id]["child_count"] == 2
    assert by_id[parent.id]["project"]["counts"]["done"] == 1
    assert by_id[open_child.id]["parent_id"] == parent.id
    assert by_id[open_child.id]["parent_title"] == "Synthetic project"


def test_repository_affinity_is_not_hierarchy_and_survives_child_attachment(project_api):
    client, manager, _sessions = project_api
    parent = manager.create(
        "Synthetic repository work",
        fields={"project": "synthetic-repository"},
    )

    affinity_only = client.get(f"/api/tasks/{parent.id}")
    assert affinity_only.status_code == 200
    assert affinity_only.json()["fields"]["project"] == "synthetic-repository"
    assert affinity_only.json()["parent_id"] is None
    assert affinity_only.json()["is_project"] is False
    assert affinity_only.json()["child_count"] == 0
    assert affinity_only.json()["project"] is None

    child = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic repository child",
            "fields": {
                "parent_id": parent.id,
                "project": "synthetic-repository",
            },
        },
    )
    linked_parent = client.get(f"/api/tasks/{parent.id}")

    assert child.status_code == 200
    assert child.json()["parent_id"] == parent.id
    assert child.json()["fields"] == {
        "parent_id": parent.id,
        "project": "synthetic-repository",
    }
    assert linked_parent.status_code == 200
    assert linked_parent.json()["fields"]["project"] == "synthetic-repository"
    assert linked_parent.json()["is_project"] is True
    assert linked_parent.json()["child_count"] == 1
    assert linked_parent.json()["project"]["child_count"] == 1
    assert linked_parent.json()["project"]["counts"]["unassigned"] == 1


def test_children_endpoint_returns_terminal_children(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project")
    children = [
        manager.create("Synthetic A", fields={"parent_id": parent.id}),
        manager.create("Synthetic B", status="cancelled", fields={"parent_id": parent.id}),
    ]

    response = client.get(f"/api/tasks/{parent.id}/children", params={"limit": 1, "offset": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert payload["tasks"][0]["id"] == sorted(child.id for child in children)[1]


def test_project_complete_and_generic_cancel_share_guards(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project")
    manager.create("Synthetic cancelled child", status="cancelled", fields={"parent_id": parent.id})

    raw_cancel = client.put(f"/api/tasks/{parent.id}", json={"status": "cancelled"})
    assert raw_cancel.status_code == 409
    reduced_scope = client.post(
        f"/api/tasks/{parent.id}/project/complete",
        json={"acknowledge_cancelled_children": False},
    )
    assert reduced_scope.status_code == 409
    accepted = client.post(
        f"/api/tasks/{parent.id}/project/complete",
        json={"acknowledge_cancelled_children": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "done"


def test_cancel_preview_is_non_mutating(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project")
    manager.create(
        "Synthetic review child",
        status="done",
        tags=["codex", "agent-completed"],
        fields={"parent_id": parent.id},
    )

    response = client.post(f"/api/tasks/{parent.id}/project/cancel", json={"confirm": False})
    assert response.status_code == 200
    assert response.json()["awaiting_review_count"] == 1
    assert "project_cancel_operation_id" not in manager.get(parent.id).fields


def test_cancel_rejects_blank_operation_id_without_server_error(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project")
    manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.post(
        f"/api/tasks/{parent.id}/project/cancel",
        json={"confirm": True, "operation_id": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "operation_id is required"


def test_plan_endpoint_is_idempotent(project_api):
    client, manager, sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    manager.create("Synthetic child", fields={"parent_id": parent.id})
    body = {"operation_id": "synthetic-plan-request"}

    first = client.post(f"/api/tasks/{parent.id}/project/plan", json=body)
    second = client.post(f"/api/tasks/{parent.id}/project/plan", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    assert second.json()["created"] is False
    assert sessions.get_by_session_id(first.json()["session_id"]).status == "claimed"


def test_create_child_operation_key_is_idempotent(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project")
    other_parent = manager.create("Synthetic other project")
    body = {
        "description": "Synthetic retry-safe child",
        "operation_key": f"project:{parent.id}:plan:synthetic:child:1",
        "fields": {"parent_id": parent.id},
    }

    first = client.post("/api/tasks", json=body)
    second = client.post("/api/tasks", json=body)
    conflicting_retry = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic changed retry payload",
            "operation_key": body["operation_key"],
            "fields": {"parent_id": other_parent.id},
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert conflicting_retry.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert conflicting_retry.json()["id"] == first.json()["id"]
    assert conflicting_retry.json()["description"] == body["description"]
    assert conflicting_retry.json()["parent_id"] == parent.id
    assert len(manager.list_children(parent.id)) == 1
    assert manager.list_children(other_parent.id) == []


def test_create_child_under_closed_parent_reports_lifecycle_conflict(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic closed parent", status="done")

    response = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic invalid child",
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "reopen the parent before adding or moving child work"
