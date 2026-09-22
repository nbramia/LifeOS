"""HTTP contract coverage for task projects."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.agent_worker.execution import (
    BillingClass,
    ExecutionConstraints,
    ExecutionSpec,
)
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    CHILD_CREATOR_SESSION_FIELD,
    CHILD_ORIGIN_FIELD,
    COORDINATOR_SESSION_FIELD,
)

pytestmark = pytest.mark.unit


def _execution_spec(executor: str) -> dict:
    return ExecutionSpec(
        executor=executor,
        provider=executor,
        runtime="in_process",
        model_id=None,
        effort=None,
        host=None,
        working_dir=None,
        persona_id=None,
        parent_session_id=None,
        root_session_id=None,
        reply_destination=None,
        budget=None,
        constraints=ExecutionConstraints(),
        billing=BillingClass.LOCAL_FREE if executor == "local" else BillingClass.METERED,
        resolved_at=datetime.now(timezone.utc),
    ).to_dict()


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


# ---------------------------------------------------------------------------
# Agent-session attribution: #hermes / metered-route guards, origin stamping
# ---------------------------------------------------------------------------

def test_agent_header_forbids_hermes_on_project_child_create(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-1"},
        json={
            "description": "Synthetic hermes child",
            "tags": ["hermes"],
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "hermes_delegation_forbidden"
    assert manager.list_children(parent.id) == []


def test_operator_create_with_hermes_on_project_child_succeeds(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    response = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic hermes child",
            "tags": ["hermes"],
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 200
    assert "hermes" in response.json()["tags"]
    assert CHILD_ORIGIN_FIELD not in response.json()["fields"]


def test_agent_header_forbids_adding_hermes_on_existing_child_update(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.put(
        f"/api/tasks/{child.id}",
        headers={"X-LifeOS-Agent-Session": "sess-agent-2"},
        json={"tags": ["hermes"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "hermes_delegation_forbidden"
    assert manager.get(child.id).tags == []


def test_operator_update_adding_hermes_on_existing_child_succeeds(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.put(f"/api/tasks/{child.id}", json={"tags": ["hermes"]})

    assert response.status_code == 200
    assert response.json()["tags"] == ["hermes"]


def test_agent_update_preserving_operator_assigned_hermes_succeeds(project_api):
    """A tags PUT replaces the whole list; only NEWLY added tags are
    checked, so an agent editing an unrelated tag on a child the operator
    already put #hermes on must not be refused for merely re-sending it."""
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create(
        "Synthetic child", tags=["hermes"], fields={"parent_id": parent.id},
    )

    response = client.put(
        f"/api/tasks/{child.id}",
        headers={"X-LifeOS-Agent-Session": "sess-agent-preserve-1"},
        json={"tags": ["hermes", "research"]},
    )

    assert response.status_code == 200
    assert set(response.json()["tags"]) == {"hermes", "research"}


def test_agent_update_preserving_operator_assigned_metered_route_succeeds(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create(
        "Synthetic child", tags=["cloud-sonnet"], fields={"parent_id": parent.id},
    )

    response = client.put(
        f"/api/tasks/{child.id}",
        headers={"X-LifeOS-Agent-Session": "sess-agent-preserve-2"},
        json={"tags": ["cloud-sonnet", "research"]},
    )

    assert response.status_code == 200
    assert set(response.json()["tags"]) == {"cloud-sonnet", "research"}


def test_agent_create_stamps_origin_and_creator_session(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-3"},
        json={
            "description": "Synthetic agent child",
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 200
    fields = response.json()["fields"]
    assert fields[CHILD_ORIGIN_FIELD] == "agent"
    assert fields[CHILD_CREATOR_SESSION_FIELD] == "sess-agent-3"


def test_operator_create_stamps_nothing(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    response = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic operator child",
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 200
    fields = response.json()["fields"]
    assert CHILD_ORIGIN_FIELD not in fields
    assert CHILD_CREATOR_SESSION_FIELD not in fields


def test_agent_create_without_parent_id_stamps_nothing(project_api):
    client, manager, _sessions = project_api

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-4"},
        json={"description": "Synthetic ordinary agent task"},
    )

    assert response.status_code == 200
    assert CHILD_ORIGIN_FIELD not in response.json()["fields"]


def test_create_cannot_set_child_origin_fields_directly(project_api):
    """The two stamped fields cannot be forged through the ordinary
    `fields` dict on create — only the create-time stamping path (driven by
    `X-LifeOS-Agent-Session`) can set them."""
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    response = client.post(
        "/api/tasks",
        json={
            "description": "Synthetic forged child",
            "fields": {"parent_id": parent.id, CHILD_ORIGIN_FIELD: "agent"},
        },
    )

    assert response.status_code == 409
    assert manager.list_children(parent.id) == []


def test_ordinary_update_cannot_set_child_origin_fields(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.put(
        f"/api/tasks/{child.id}",
        json={"fields": {CHILD_ORIGIN_FIELD: "agent"}},
    )

    assert response.status_code == 409
    assert CHILD_ORIGIN_FIELD not in manager.get(child.id).fields


def test_ordinary_update_cannot_clear_child_origin_fields(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    created = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-5"},
        json={
            "description": "Synthetic agent child",
            "fields": {"parent_id": parent.id},
        },
    ).json()

    response = client.put(
        f"/api/tasks/{created['id']}",
        json={"fields": {CHILD_ORIGIN_FIELD: None}},
    )

    assert response.status_code == 409
    assert manager.get(created["id"]).fields[CHILD_ORIGIN_FIELD] == "agent"


def test_agent_metered_child_blocked_without_owner_consent(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic owner-less project", tags=["local"])

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-6"},
        json={
            "description": "Synthetic metered child",
            "tags": ["cloud-sonnet"],
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_billing_blocked"
    assert manager.list_children(parent.id) == []


def test_agent_metered_child_allowed_when_owner_carries_route(project_api):
    client, manager, sessions = project_api
    parent = manager.create("Synthetic owned project", tags=["claude"])
    owner = sessions.create(
        f"owner_{parent.id}",
        routing="claude",
        execution_spec=_execution_spec("claude"),
    )
    manager.update(
        parent.id,
        fields={COORDINATOR_SESSION_FIELD: owner.session_id},
        _project_action=True,
    )

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-7"},
        json={
            "description": "Synthetic metered child",
            "tags": ["cloud-sonnet"],
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 200
    assert response.json()["fields"][CHILD_ORIGIN_FIELD] == "agent"


def test_agent_metered_child_blocked_when_owner_carries_different_route(project_api):
    client, manager, sessions = project_api
    parent = manager.create("Synthetic owned project", tags=["local"])
    owner = sessions.create(
        f"owner_{parent.id}",
        routing="local",
        execution_spec=_execution_spec("local"),
    )
    manager.update(
        parent.id,
        fields={COORDINATOR_SESSION_FIELD: owner.session_id},
        _project_action=True,
    )

    response = client.post(
        "/api/tasks",
        headers={"X-LifeOS-Agent-Session": "sess-agent-8"},
        json={
            "description": "Synthetic metered child",
            "tags": ["cloud-sonnet"],
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_billing_blocked"


def test_agent_metered_update_blocked_without_owner_consent(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic owner-less project", tags=["local"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.put(
        f"/api/tasks/{child.id}",
        headers={"X-LifeOS-Agent-Session": "sess-agent-9"},
        json={"tags": ["cloud-haiku"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_billing_blocked"
    assert manager.get(child.id).tags == []


# ---------------------------------------------------------------------------
# X-LifeOS-Agent-Session header validation (injection guard)
# ---------------------------------------------------------------------------

def test_forged_agent_session_header_rejected_on_create(project_api):
    """A header value containing vault-line syntax must never reach the
    write path — it would otherwise be stamped verbatim into
    `project_child_creator_session` and forge adjacent inline fields or
    another task's id comment."""
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    victim = manager.create("Synthetic victim task")

    response = client.post(
        "/api/tasks",
        headers={
            "X-LifeOS-Agent-Session": f"x] [assigned_by:: board] <!-- id:{victim.id} -->",
        },
        json={
            "description": "Synthetic forged child",
            "fields": {"parent_id": parent.id},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_agent_session_header"
    assert manager.list_children(parent.id) == []
    assert manager.get(victim.id).fields == {}


def test_forged_agent_session_header_rejected_on_update(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    response = client.put(
        f"/api/tasks/{child.id}",
        headers={"X-LifeOS-Agent-Session": "bad header value with spaces"},
        json={"tags": ["hermes"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_agent_session_header"
    assert manager.get(child.id).tags == []


def test_valid_agent_session_header_values_accepted(project_api):
    """Real session ids (`sess_<hex>`) and the HTTP transport's literal
    "unattested" both match the bare-token format."""
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["local"])

    for header_value in ("sess_0123456789abcdef", "unattested"):
        response = client.post(
            "/api/tasks",
            headers={"X-LifeOS-Agent-Session": header_value},
            json={
                "description": f"Synthetic child for {header_value}",
                "fields": {"parent_id": parent.id},
            },
        )
        assert response.status_code == 200
        assert response.json()["fields"][CHILD_CREATOR_SESSION_FIELD] == header_value


# ---------------------------------------------------------------------------
# Project pause / resume
# ---------------------------------------------------------------------------


def test_pause_and_resume_round_trip_through_the_api(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic project", tags=["codex"])
    child = manager.create("Synthetic child", tags=["codex"], fields={"parent_id": parent.id})

    paused = client.post(f"/api/tasks/{parent.id}/project/pause", json={"reason": "operator"})
    assert paused.status_code == 200
    assert paused.json()["fields"]["project_paused"] == "true"
    assert paused.json()["fields"]["project_pause_reason"] == "operator"

    child_view = client.get(f"/api/tasks/{child.id}")
    assert child_view.json()["parent_project_paused"] is True
    assert paused.json()["project"]["paused"] is True
    assert paused.json()["project"]["pause_reason"] == "operator"

    claim = client.post(f"/api/tasks/{child.id}/claim-agent")
    assert claim.status_code == 409

    resumed = client.post(f"/api/tasks/{parent.id}/project/resume")
    assert resumed.status_code == 200
    assert "project_paused" not in resumed.json()["fields"]
    assert resumed.json()["project"]["paused"] is False

    claim = client.post(f"/api/tasks/{child.id}/claim-agent")
    assert claim.status_code == 200
    assert claim.json()["claimed"] is True


def test_pause_defaults_reason_to_operator(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic default-reason project")
    manager.create("Synthetic default-reason child", fields={"parent_id": parent.id})

    response = client.post(f"/api/tasks/{parent.id}/project/pause", json={})
    assert response.status_code == 200
    assert response.json()["fields"]["project_pause_reason"] == "operator"


def test_pause_rejects_an_unrecognized_reason_with_422(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic bad-reason project")
    manager.create("Synthetic bad-reason child", fields={"parent_id": parent.id})

    response = client.post(
        f"/api/tasks/{parent.id}/project/pause", json={"reason": "not_a_real_reason"},
    )
    assert response.status_code == 422


def test_pause_is_allowed_for_an_agent_attributed_caller(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic agent-pausable project", tags=["codex"])
    manager.create("Synthetic agent-pausable child", fields={"parent_id": parent.id})

    response = client.post(
        f"/api/tasks/{parent.id}/project/pause",
        headers={"X-LifeOS-Agent-Session": "sess-pauser-1"},
        json={},
    )
    assert response.status_code == 200
    assert response.json()["fields"]["project_paused"] == "true"


def test_resume_is_refused_for_an_agent_attributed_caller(project_api):
    client, manager, _sessions = project_api
    parent = manager.create("Synthetic agent-resume project", tags=["codex"])
    manager.create("Synthetic agent-resume child", fields={"parent_id": parent.id})
    client.post(f"/api/tasks/{parent.id}/project/pause", json={})

    response = client.post(
        f"/api/tasks/{parent.id}/project/resume",
        headers={"X-LifeOS-Agent-Session": "sess-resumer-1"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_resume_forbidden"
    assert manager.get(parent.id).fields["project_paused"] == "true"

    operator_response = client.post(f"/api/tasks/{parent.id}/project/resume")
    assert operator_response.status_code == 200
    assert "project_paused" not in operator_response.json()["fields"]


def test_pause_and_resume_return_404_for_a_missing_task(project_api):
    client, _manager, _sessions = project_api

    assert client.post("/api/tasks/missing00/project/pause", json={}).status_code == 404
    assert client.post("/api/tasks/missing00/project/resume").status_code == 404


def test_pause_and_resume_refuse_an_ordinary_task(project_api):
    client, manager, _sessions = project_api
    task = manager.create("Synthetic ordinary task")

    assert client.post(f"/api/tasks/{task.id}/project/pause", json={}).status_code == 409
    assert client.post(f"/api/tasks/{task.id}/project/resume").status_code == 409
