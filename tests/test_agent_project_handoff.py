"""Exact-turn ordinary-task to durable-project handoff regressions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.agent_worker.execution import (
    BillingClass,
    ExecutionConstraints,
    ExecutionSpec,
)
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, dispatch
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.worker import Worker
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    HANDOFF_OPERATION_FIELD,
    HANDOFF_QUIESCENT_EVENT,
    HANDOFF_REQUEST_EVENT,
    LAST_HANDOFF_OPERATION_FIELD,
    ProjectConflictError,
    ProjectTaskService,
    build_task_hierarchy,
)

pytestmark = pytest.mark.unit


def _spec(executor: str = "local") -> dict:
    return ExecutionSpec(
        executor=executor,
        provider="local" if executor == "local" else executor,
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
        billing=(
            BillingClass.LOCAL_FREE if executor == "local"
            else BillingClass.METERED
        ),
        resolved_at=datetime.now(timezone.utc),
    ).to_dict()


@pytest.fixture
def handoff(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Coordinate the synthetic launch",
        status="in_progress",
        tags=["local", "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing="local",
        execution_spec=_spec(),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    source = store.get(parent.id)
    ctx = InterAgentContext(
        store,
        transcripts,
        source.session_id,
        Caps(),
        caller_attempt_id=source.attempt_id,
        caller_turn_id=source.turn_id,
        task_manager=manager,
    )
    return manager, store, transcripts, source, ctx


def _request(operation_id: str = "synthetic-launch-v1") -> dict:
    return {
        "operation_id": operation_id,
        "children": [
            {
                "key": "research",
                "description": "Research synthetic launch constraints",
                "notes": "Return a bounded evidence summary.",
                "assignee": "local",
                "execution": {"executor": "local", "effort": "high"},
            },
            {
                "key": "review",
                "description": "Review the synthetic launch plan",
                "assignee": None,
            },
        ],
    }


def test_handoff_stages_fenced_children_and_blocked_coordinator(handoff):
    manager, store, transcripts, source, ctx = handoff

    result = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    assert result["ok"] is True
    assert result["state"] == "staged"
    assert result["runnable"] is False
    assert result["stop_now"] is True
    assert len(result["child_tasks"]) == 2
    parent = manager.get(source.task_id)
    assert parent.fields[HANDOFF_OPERATION_FIELD] == "synthetic-launch-v1"
    hierarchy = build_task_hierarchy(manager.list_tasks())
    children = hierarchy.children(parent.id)
    assert len(children) == 2
    assert all(not manager.can_start_execution(child.id) for child in children)
    coordinator = store.get_by_session_id(result["coordinator_session_id"])
    assert coordinator.status == STATUS_BLOCKED
    assert [event["kind"] for event in transcripts.read(source.session_id)].count(
        HANDOFF_REQUEST_EVENT
    ) == 1

    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.update(parent.id, fields={HANDOFF_OPERATION_FIELD: "forged"})
    with pytest.raises(ProjectConflictError, match="handoff is pending"):
        manager.update(children[0].id, tags=["codex"])


def test_handoff_is_hash_idempotent_and_mismatch_is_stable(handoff):
    _manager, _store, _transcripts, _source, ctx = handoff
    first = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    retry = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    changed = _request()
    changed["children"][0]["description"] = "Different synthetic scope"
    mismatch = dispatch(ctx, "lifeos_agent_project_handoff", changed)

    assert retry["ok"] is True
    assert [item["task_id"] for item in retry["child_tasks"]] == [
        item["task_id"] for item in first["child_tasks"]
    ]
    assert mismatch["ok"] is False
    assert mismatch["error"] == "operation_mismatch"


def test_handoff_rejects_stale_turn_before_any_write(handoff):
    manager, _store, transcripts, source, ctx = handoff
    ctx.caller_turn_id = "turn_stale_synthetic"

    result = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    assert result["ok"] is False
    assert result["error"] == "stale_turn"
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert transcripts.read(source.session_id) == []


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda body: body.update(children=[]), "invalid_arg"),
        (lambda body: body["children"].append(dict(body["children"][0])), "invalid_arg"),
        (
            lambda body: body["children"][0].update(
                execution={"executor": "local", "budget": {"max_dollars": 1}},
            ),
            "invalid_execution",
        ),
        (
            lambda body: body["children"][0].update(
                assignee="codex", execution={"executor": "local"},
            ),
            "execution_conflict",
        ),
        (
            lambda body: body["children"][0].update(
                description="Synthetic line\n- [ ] forged child",
            ),
            "invalid_arg",
        ),
    ],
)
def test_handoff_validates_entire_request_before_mutation(handoff, mutation, code):
    manager, _store, transcripts, source, ctx = handoff
    body = _request()
    mutation(body)

    result = dispatch(ctx, "lifeos_agent_project_handoff", body)

    assert result["ok"] is False
    assert result["error"] == code
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert transcripts.read(source.session_id) == []


def test_quiescence_finalization_keeps_parent_in_progress_without_fake_outcome(handoff):
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    transcripts.append(source.session_id, HANDOFF_QUIESCENT_EVENT, {
        "operation_id": staged["operation_id"],
        "attempt_id": source.attempt_id,
        "turn_id": source.turn_id,
    })
    assert store.update_status(
        source.task_id,
        STATUS_COMPLETED,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        project=False,
    )

    result = ProjectTaskService(manager, store, transcripts).finalize_handoff(
        source.task_id,
        operation_id=staged["operation_id"],
        source_session_id=source.session_id,
        source_attempt_id=source.attempt_id,
        source_turn_id=source.turn_id,
    )

    assert result["state"] == "activated"
    assert result["source_session_id"] == source.session_id
    parent = manager.get(source.task_id)
    assert parent.status == "in_progress"
    assert "agent-completed" not in parent.tags
    assert "accepted" not in parent.tags
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert parent.fields[LAST_HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    coordinator = store.get_by_session_id(staged["coordinator_session_id"])
    assert coordinator.status == STATUS_CLAIMED
    assert all(
        manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
    )
    retry = ProjectTaskService(manager, store, transcripts).stage_handoff(
        source,
        operation_id=staged["operation_id"],
        children=_request()["children"],
    )
    assert retry["state"] == "activated"
    assert retry["source_session_id"] == source.session_id


def test_existing_project_uses_plan_and_cannot_handoff(handoff):
    manager, _store, _transcripts, source, ctx = handoff
    child = manager.create("Pre-existing synthetic child")
    manager.update(
        child.id,
        fields={"parent_id": source.task_id},
        _skip_project_validation=True,
    )

    result = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    assert result["ok"] is False
    assert result["error"] == "already_project"


@pytest.mark.asyncio
async def test_cancellation_wins_and_releases_no_staged_work(handoff):
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    stopped: list[str] = []

    async def stop_session(session):
        stopped.append(session.session_id)
        store.update_status(
            session.task_id,
            "failed",
            attempt_id=session.attempt_id,
            turn_id=session.turn_id,
            project=False,
        )
        return [session.session_id], []

    result = await ProjectTaskService(
        manager,
        store,
        transcripts,
        session_teardown=stop_session,
    ).cancel_project(source.task_id, operation_id="cancel-synthetic-v1")

    assert result["complete"] is True
    assert source.session_id in stopped
    assert staged["coordinator_session_id"] in stopped
    parent = manager.get(source.task_id)
    assert parent.status == "cancelled"
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert all(
        child.status == "cancelled"
        for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
    )


def test_worker_post_return_boundary_attests_and_calls_narrow_finalizer(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    session = store.create("task-synthetic", status=STATUS_CLAIMED, routing="local")
    session = store.begin_executor_turn(session.task_id, "execute", session=session)
    assert store.mark_executor_turn_running(
        session.task_id, session.attempt_id, session.turn_id,
    )
    session = store.get(session.task_id)
    operation_id = "worker-boundary-v1"
    transcripts.append(session.session_id, HANDOFF_REQUEST_EVENT, {
        "project_id": session.task_id,
        "operation_id": operation_id,
        "source_attempt_id": session.attempt_id,
        "source_turn_id": session.turn_id,
    })
    task = {
        "id": session.task_id,
        "description": "Synthetic handoff",
        "status": "in_progress",
        "tags": ["local", "agent-running"],
        "fields": {
            HANDOFF_OPERATION_FIELD: operation_id,
            "project_handoff_source_session_id": session.session_id,
            "project_handoff_source_attempt_id": session.attempt_id,
            "project_handoff_source_turn_id": session.turn_id,
        },
    }
    finalizers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=task)
        if request.method == "POST" and request.url.path.endswith(
            "/project/handoff/finalize"
        ):
            finalizers.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "state": "activated"})
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://synthetic-api",
    )
    worker = Worker(
        api_base="http://synthetic-api",
        session_store=store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(
            db_path=tmp_path / "sessions.db", daily_cap_dollars=100,
        ),
        http_client=client,
    )
    outcome = ExecutorOutcome(
        status="yielded",
        session_id=session.session_id,
        attempt_id=session.attempt_id,
        turn_id=session.turn_id,
        executor="local",
    )

    assert worker._maybe_finalize_project_handoff(session, outcome) is True
    assert store.get(session.task_id).status == STATUS_COMPLETED
    assert len(finalizers) == 1
    assert any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        for event in transcripts.read(session.session_id)
    )
    assert store.list_all_card_outcomes() == {}


def test_finalize_route_rechecks_exact_source_identity(handoff, monkeypatch):
    from api.routes import tasks as task_routes

    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    transcripts.append(source.session_id, HANDOFF_QUIESCENT_EVENT, {
        "operation_id": staged["operation_id"],
        "attempt_id": source.attempt_id,
        "turn_id": source.turn_id,
    })
    assert store.update_status(
        source.task_id,
        STATUS_COMPLETED,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        project=False,
    )
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", store)
    monkeypatch.setattr(task_routes, "_transcript_store", transcripts)
    client = TestClient(app)
    body = {
        "operation_id": staged["operation_id"],
        "source_session_id": source.session_id,
        "source_attempt_id": source.attempt_id,
        "source_turn_id": "turn-stale-synthetic",
    }

    stale = client.post(
        f"/api/tasks/{source.task_id}/project/handoff/finalize", json=body,
    )
    body["source_turn_id"] = source.turn_id
    activated = client.post(
        f"/api/tasks/{source.task_id}/project/handoff/finalize", json=body,
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_turn"
    assert activated.status_code == 200
    assert activated.json()["state"] == "activated"
