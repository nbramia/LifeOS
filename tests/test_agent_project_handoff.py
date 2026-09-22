"""Exact-turn ordinary-task to durable-project handoff regressions."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services import agent_board
from api.services.agent_worker.execution import (
    BillingClass,
    ExecutionConstraints,
    ExecutionSpec,
)
from api.services.agent_worker.executor_lifecycle import normalize_outcome
from api.services.agent_worker.hermes_executor import HermesExecutor
from api.services.agent_worker.inter_agent import (
    Caps,
    InterAgentContext,
    dispatch,
    teardown_session,
)
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.worker import PROJECT_HANDOFF_RETURN_PENDING_EVENT, Worker
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    CHILD_CREATOR_SESSION_FIELD,
    CHILD_ORIGIN_FIELD,
    HANDOFF_OPERATION_FIELD,
    HANDOFF_QUIESCENT_EVENT,
    HANDOFF_READY_AT_FIELD,
    HANDOFF_REQUEST_EVENT,
    HANDOFF_SOURCE_ATTEMPT_FIELD,
    HANDOFF_SOURCE_TURN_FIELD,
    LAST_ABORTED_HANDOFF_FIELD,
    LAST_HANDOFF_OPERATION_FIELD,
    ProjectHandoffError,
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


def _task_payload(manager: TaskManager, task_id: str) -> dict | None:
    task = manager.get(task_id)
    if task is None:
        return None
    return {
        "id": task.id,
        "description": task.description,
        "status": task.status,
        "tags": task.tags,
        "fields": task.fields,
    }


def _handoff_worker(
    tmp_path: Path,
    manager: TaskManager,
    store: SessionStore,
    transcripts: TranscriptStore,
    service: ProjectTaskService,
) -> tuple[Worker, list[dict]]:
    finalizers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            tasks = [
                payload
                for task in manager.list_tasks()
                if (payload := _task_payload(manager, task.id)) is not None
            ]
            return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})
        if request.method == "GET" and request.url.path.startswith("/api/tasks/"):
            task_id = request.url.path.rsplit("/", 1)[-1]
            payload = _task_payload(manager, task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith(
            "/project/handoff/finalize"
        ):
            task_id = request.url.path.split("/api/tasks/", 1)[1].split("/", 1)[0]
            body = json.loads(request.content)
            finalizers.append(body)
            try:
                result = service.finalize_handoff(task_id, **body)
            except ProjectHandoffError as exc:
                return httpx.Response(
                    409,
                    json={"detail": {"code": exc.code, "message": str(exc)}},
                )
            return httpx.Response(200, json=result)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://synthetic-api",
    )
    return Worker(
        api_base="http://synthetic-api",
        session_store=store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(
            db_path=tmp_path / "worker-spend.db", daily_cap_dollars=100,
        ),
        http_client=client,
    ), finalizers


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


def test_handoff_staged_children_are_stamped_agent_origin(handoff):
    manager, _store, _transcripts, source, ctx = handoff

    result = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    assert result["ok"] is True
    for item in result["child_tasks"]:
        child = manager.get(item["task_id"])
        assert child.fields[CHILD_ORIGIN_FIELD] == "agent"
        assert child.fields[CHILD_CREATOR_SESSION_FIELD] == source.session_id


def test_handoff_rejects_hermes_assignee_before_any_write(handoff):
    manager, store, transcripts, source, ctx = handoff
    request = _request()
    request["children"][0]["assignee"] = "hermes"
    del request["children"][0]["execution"]

    result = dispatch(ctx, "lifeos_agent_project_handoff", request)

    assert result == {
        "ok": False,
        "error": "hermes_delegation_forbidden",
        "message": (
            "child research cannot be assigned to hermes; the operator can "
            "assign it from the board"
        ),
    }
    assert [task.id for task in manager.list_tasks()] == [source.task_id]
    assert store.list_sessions() == [source]
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert transcripts.read(source.session_id) == []


def test_handoff_rejects_hermes_executor_before_any_write(handoff):
    manager, store, transcripts, source, ctx = handoff
    request = _request()
    request["children"][0]["assignee"] = None
    request["children"][0]["execution"] = {"executor": "hermes"}

    result = dispatch(ctx, "lifeos_agent_project_handoff", request)

    assert result == {
        "ok": False,
        "error": "hermes_delegation_forbidden",
        "message": (
            "child research cannot execute on hermes; the operator can "
            "assign it from the board"
        ),
    }
    assert [task.id for task in manager.list_tasks()] == [source.task_id]
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields


def test_handoff_schema_omits_hermes_from_assignee_and_executor_enums():
    from api.services.agent_worker.inter_agent import INTER_AGENT_TOOL_SCHEMAS

    schema = next(
        item for item in INTER_AGENT_TOOL_SCHEMAS
        if item["name"] == "lifeos_agent_project_handoff"
    )
    child_schema = schema["input_schema"]["properties"]["children"]["items"]["properties"]
    assert "hermes" not in child_schema["assignee"]["enum"]
    assert "hermes" not in child_schema["execution"]["properties"]["executor"]["enum"]


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


def test_local_operator_kill_return_reconciliation_keeps_handoff_fenced(
    handoff, tmp_path: Path,
):
    """A direct local Kill cannot be converted into child-release proof."""
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    teardown_session(
        store,
        transcripts,
        source,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "synthetic operator stop"},
        managed_driver=None,
    )
    service = ProjectTaskService(manager, store, transcripts)
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts, service,
    )
    worker._handle_outcome(
        store.get(source.task_id),
        _task_payload(manager, source.task_id),
        ExecutorOutcome(
            status=STATUS_COMPLETED,
            session_id=source.session_id,
            attempt_id=source.attempt_id,
            turn_id=source.turn_id,
            executor="local",
        ),
    )

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_FAILED
    assert any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        for event in transcripts.read(source.session_id)
    )
    assert worker._reconcile_project_handoffs() == 0
    assert len(finalizers) == 1
    assert manager.get(source.task_id).fields[HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


@pytest.mark.asyncio
async def test_cancellation_wins_then_returned_turn_releases_no_staged_work(
    handoff, tmp_path: Path,
):
    """A cancelled exact turn retains stop proof without activating work."""
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    stopped: list[str] = []

    async def stop_session(session):
        stopped.append(session.session_id)
        result = teardown_session(
            store,
            transcripts,
            session,
            transcript_kind="operator_killed",
            transcript_payload={"reason": "project cancellation"},
            managed_driver=None,
        )
        failures = []
        if result["managed_failure"]:
            failures.append({
                "session_id": session.session_id,
                "reason": result["managed_failure"],
            })
        return [session.session_id], failures

    service = ProjectTaskService(
        manager,
        store,
        transcripts,
        session_teardown=stop_session,
    )
    first = await service.cancel_project(
        source.task_id, operation_id="cancel-synthetic-v1",
    )

    assert first["complete"] is False
    assert first["pending"] is True
    assert first["failures"]
    assert source.session_id in stopped
    assert staged["coordinator_session_id"] in stopped
    assert manager.get(source.task_id).status != "cancelled"
    assert all(
        child.status == "cancelled"
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )

    worker, finalizers = _handoff_worker(tmp_path, manager, store, transcripts, service)
    returned_source = store.get(source.task_id)
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    worker._handle_outcome(
        returned_source,
        _task_payload(manager, source.task_id),
        outcome,
    )

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_FAILED
    assert any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"]["operation_id"] == staged["operation_id"]
        and event["payload"]["attempt_id"] == source.attempt_id
        and event["payload"]["turn_id"] == source.turn_id
        for event in transcripts.read(source.session_id)
    )
    pending_parent = manager.get(source.task_id)
    assert pending_parent.status == "in_progress"
    assert pending_parent.fields[HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )
    assert worker._reconcile_project_handoffs() == 0
    assert len(finalizers) == 1
    assert store.get(source.task_id).status == STATUS_FAILED
    pending_parent = manager.get(source.task_id)
    assert pending_parent.fields[HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )

    result = await service.cancel_project(
        source.task_id, operation_id="cancel-synthetic-v1",
    )

    assert result["complete"] is True
    parent = manager.get(source.task_id)
    assert parent.status == "cancelled"
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert all(
        child.status == "cancelled"
        for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
    )


def test_exact_cancellation_guard_blocks_handoff_before_terminal_status(
    handoff, tmp_path: Path, monkeypatch,
):
    """The durable turn fence is authoritative before status terminalization."""
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO cancellation_guards "
            "(session_id, task_id, attempt_id, turn_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source.session_id,
                source.task_id,
                source.attempt_id,
                source.turn_id,
                "synthetic pre-terminal cancellation",
                int(datetime.now(timezone.utc).timestamp()),
            ),
        )
    assert store.get(source.task_id).status == STATUS_RUNNING
    assert store.is_cancelled(source.task_id, source.attempt_id, source.turn_id)

    update_status = store.update_status

    def forbid_source_completion(task_id, status, **kwargs):
        if task_id == source.task_id and status == STATUS_COMPLETED:
            raise AssertionError("cancelled source completion was attempted")
        return update_status(task_id, status, **kwargs)

    monkeypatch.setattr(store, "update_status", forbid_source_completion)
    service = ProjectTaskService(manager, store, transcripts)
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts, service,
    )
    worker._handle_outcome(
        store.get(source.task_id),
        _task_payload(manager, source.task_id),
        ExecutorOutcome(
            status=STATUS_COMPLETED,
            session_id=source.session_id,
            attempt_id=source.attempt_id,
            turn_id=source.turn_id,
            executor="local",
        ),
    )

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_RUNNING
    assert manager.get(source.task_id).fields[HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )
    with pytest.raises(ProjectHandoffError) as exc_info:
        service.finalize_handoff(
            source.task_id,
            operation_id=staged["operation_id"],
            source_session_id=source.session_id,
            source_attempt_id=source.attempt_id,
            source_turn_id=source.turn_id,
        )
    assert exc_info.value.code == "cancelled"
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


@pytest.mark.asyncio
async def test_cancelled_return_with_mismatched_persisted_turn_stays_fenced(
    handoff, tmp_path: Path,
):
    """A returned stale turn's quiescence proof is scoped to its own attempt
    and turn — it cannot attest a newer persisted handoff identity."""
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    def stop_session(session):
        teardown_session(
            store,
            transcripts,
            session,
            transcript_kind="operator_killed",
            transcript_payload={"reason": "project cancellation"},
            managed_driver=None,
        )
        return [session.session_id], []

    service = ProjectTaskService(
        manager, store, transcripts, session_teardown=stop_session,
    )
    first = await service.cancel_project(
        source.task_id, operation_id="cancel-mismatched-turn",
    )
    assert first["pending"] is True
    manager.update(
        source.task_id,
        fields={HANDOFF_SOURCE_TURN_FIELD: "turn-newer-synthetic"},
        _project_operation="cancel",
    )
    worker, finalizers = _handoff_worker(tmp_path, manager, store, transcripts, service)

    worker._handle_outcome(
        store.get(source.task_id),
        _task_payload(manager, source.task_id),
        ExecutorOutcome(
            status=STATUS_COMPLETED,
            session_id=source.session_id,
            attempt_id=source.attempt_id,
            turn_id=source.turn_id,
            executor="local",
        ),
    )

    assert finalizers == []
    # The exact old turn's own return is recorded as quiescence proof — proof
    # that turn stopped, not that it satisfies the newer persisted identity.
    assert any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
        for event in transcripts.read(source.session_id)
    )
    assert not any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("turn_id") == "turn-newer-synthetic"
        for event in transcripts.read(source.session_id)
    )
    retry = await service.cancel_project(
        source.task_id, operation_id="cancel-mismatched-turn",
    )
    assert retry["pending"] is True
    assert retry["failures"]


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


def _stale_view_worker(
    tmp_path: Path,
    manager: TaskManager,
    store: SessionStore,
    transcripts: TranscriptStore,
    service: ProjectTaskService,
    source_task_id: str,
    stale_snapshot: dict,
    fresh: dict,
) -> tuple[Worker, list[dict]]:
    """A worker whose GET of `source_task_id` serves a stale snapshot until
    `fresh["enabled"]` is set — modeling the file-watcher debounce window
    between the MCP server's handoff-staging write and the API's task index
    picking it up."""
    finalizers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            tasks = [
                payload
                for task in manager.list_tasks()
                if (payload := _task_payload(manager, task.id)) is not None
            ]
            return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})
        if request.method == "GET" and request.url.path == f"/api/tasks/{source_task_id}":
            if not fresh["enabled"]:
                return httpx.Response(200, json=stale_snapshot)
            payload = _task_payload(manager, source_task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "GET" and request.url.path.startswith("/api/tasks/"):
            task_id = request.url.path.rsplit("/", 1)[-1]
            payload = _task_payload(manager, task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith(
            "/project/handoff/finalize"
        ):
            task_id = request.url.path.split("/api/tasks/", 1)[1].split("/", 1)[0]
            body = json.loads(request.content)
            finalizers.append(body)
            try:
                result = service.finalize_handoff(task_id, **body)
            except ProjectHandoffError as exc:
                return httpx.Response(
                    409,
                    json={"detail": {"code": exc.code, "message": str(exc)}},
                )
            return httpx.Response(200, json=result)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://synthetic-api",
    )
    return Worker(
        api_base="http://synthetic-api",
        session_store=store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(
            db_path=tmp_path / "worker-spend.db", daily_cap_dollars=100,
        ),
        http_client=client,
    ), finalizers


def test_worker_stale_return_then_reconcile_finalizes_once_view_catches_up(
    handoff, tmp_path: Path,
):
    """A turn that returns before the task view shows the handoff retries via
    reconciliation once the view is fresh, instead of losing the handoff."""
    manager, store, transcripts, source, ctx = handoff
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )

    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_RUNNING
    pending = [
        event for event in transcripts.read(source.session_id)
        if event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
    ]
    assert len(pending) == 1
    # The exact turn already carries the route's stop evidence, so the return
    # itself is recorded as quiescence proof even while the view is stale —
    # it's proof the source stopped, not activation of the handoff.
    quiescent = [
        event for event in transcripts.read(source.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
    ]
    assert len(quiescent) == 1

    # A repeat stale return for the exact same turn must not duplicate either event.
    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True
    assert len([
        event for event in transcripts.read(source.session_id)
        if event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
    ]) == 1
    assert len([
        event for event in transcripts.read(source.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
    ]) == 1

    fresh["enabled"] = True
    worker._reconcile_project_handoffs()

    assert len([
        event for event in transcripts.read(source.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
    ]) == 1
    assert store.get(source.task_id).status == STATUS_COMPLETED
    assert finalizers == [{
        "operation_id": staged["operation_id"],
        "source_session_id": source.session_id,
        "source_attempt_id": source.attempt_id,
        "source_turn_id": source.turn_id,
    }]
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert all(
        manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


def test_worker_stale_return_then_cancellation_keeps_reconciliation_fenced(
    handoff, tmp_path: Path,
):
    """Cancelling the exact turn after a stale return still blocks reconciliation
    from completing the source or releasing children."""
    manager, store, transcripts, source, ctx = handoff
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True
    assert any(
        event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
        for event in transcripts.read(source.session_id)
    )

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO cancellation_guards "
            "(session_id, task_id, attempt_id, turn_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source.session_id,
                source.task_id,
                source.attempt_id,
                source.turn_id,
                "synthetic post-return cancellation",
                int(datetime.now(timezone.utc).timestamp()),
            ),
        )
    assert store.is_cancelled(source.task_id, source.attempt_id, source.turn_id)

    fresh["enabled"] = True
    worker._reconcile_project_handoffs()

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_RUNNING
    assert manager.get(source.task_id).fields[HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


@pytest.mark.asyncio
async def test_worker_stale_return_then_cancel_project_completes_via_recorded_quiescence(
    handoff, tmp_path: Path,
):
    """The exact turn's quiescence proof recorded on a stale return lets
    `cancel_project` verify the handoff source stopped and complete, instead
    of waiting forever on proof that was already available."""
    manager, store, transcripts, source, ctx = handoff
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    fresh = {"enabled": False}

    async def stop_session(session):
        teardown_session(
            store,
            transcripts,
            session,
            transcript_kind="operator_killed",
            transcript_payload={"reason": "project cancellation"},
            managed_driver=None,
        )
        return [session.session_id], []

    service = ProjectTaskService(
        manager, store, transcripts, session_teardown=stop_session,
    )
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True
    assert any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
        for event in transcripts.read(source.session_id)
    )
    assert finalizers == []

    result = await service.cancel_project(
        source.task_id, operation_id="cancel-stale-return-v1",
    )

    assert result["complete"] is True
    assert result["pending"] is False
    assert result["failures"] == []
    parent = manager.get(source.task_id)
    assert parent.status == "cancelled"
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert all(
        child.status == "cancelled"
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


@pytest.mark.asyncio
async def test_hermes_stale_return_without_done_seen_records_no_return_pending(
    tmp_path: Path, monkeypatch,
):
    """Hermes stop evidence is required before a stale return is even attested;
    without it, reconciliation has nothing to retry."""
    from api.routes import hermes_proxy
    from api.services.conversation_store import ConversationStore
    from api.services.usage_store import UsageStore
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://hermes.example")
    monkeypatch.setattr(settings, "hermes_backend_token", "synthetic-backend-token")
    monkeypatch.setattr(settings, "mcp_bearer_token", "synthetic-turn-secret")
    monkeypatch.setattr(settings, "claude_timeout_seconds", 3600)
    conversations = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hermes_proxy, "get_store", lambda: conversations)
    monkeypatch.setattr(hermes_proxy, "get_usage_store", lambda: usage)
    monkeypatch.setattr(hermes_proxy, "schedule_retitle", lambda _conversation_id: None)

    store = SessionStore(tmp_path / "hermes-sessions.db")
    transcripts = TranscriptStore(tmp_path / "hermes-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "hermes-vault",
        index_path=tmp_path / "hermes-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Coordinate the synthetic Hermes launch",
        status="in_progress",
        tags=["hermes", "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing="hermes",
        execution_spec=_spec("hermes"),
    )
    upstream = _HandoffHermesStream("disconnect")
    executor = HermesExecutor(
        session_store=store,
        transcript_store=transcripts,
        http_client_factory=lambda: _HandoffHermesClient(upstream),
    )
    outcomes: list[ExecutorOutcome] = []
    executor_thread = threading.Thread(
        target=lambda: outcomes.append(
            executor.execute(
                source,
                {"description": "Coordinate synthetic Hermes work"},
            )
        ),
        daemon=True,
    )
    executor_thread.start()
    assert upstream.opened.wait(5)
    running = store.get(parent.id)
    context = InterAgentContext(
        store,
        transcripts,
        running.session_id,
        Caps(),
        caller_attempt_id=running.attempt_id,
        caller_turn_id=running.turn_id,
        task_manager=manager,
    )
    staged = dispatch(context, "lifeos_agent_project_handoff", _request())
    assert staged["ok"] is True
    stale_snapshot = {
        "id": parent.id,
        "description": parent.description,
        "status": "in_progress",
        "tags": list(parent.tags),
        "fields": {},
    }
    upstream.released.set()
    executor_thread.join(5)
    assert outcomes, "Hermes executor did not return"
    outcome = outcomes[0]
    assert outcome.termination_evidence.get("done_seen") is False
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        parent.id, stale_snapshot, fresh,
    )
    refreshed = store.get(parent.id)
    normalized = normalize_outcome(
        outcome, refreshed, route="hermes", transcript_store=transcripts,
    )

    # The stale view's mismatched fields would normally earn a return-pending
    # attestation, but Hermes without a real `done` event has no positive
    # stop evidence, so nothing is recorded and no POST is attempted.
    assert worker._maybe_finalize_project_handoff(refreshed, normalized) is True

    assert finalizers == []
    assert not any(
        event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
        for event in transcripts.read(running.session_id)
    )

    # The Hermes executor already terminalized the source session on its own
    # (independent of the handoff), so reconciliation takes the ordinary
    # terminal-source path here rather than the return-pending retry path —
    # and the server rejects it because no quiescence proof was ever
    # recorded above, leaving the handoff fenced.
    fresh["enabled"] = True
    worker._reconcile_project_handoffs()

    assert not any(
        event["kind"] == HANDOFF_QUIESCENT_EVENT
        for event in transcripts.read(running.session_id)
    )
    assert manager.get(parent.id).fields.get(HANDOFF_OPERATION_FIELD) == (
        staged["operation_id"]
    )
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
    )


def test_hermes_stale_return_with_done_seen_reconciles_via_return_pending_evidence(
    tmp_path: Path,
):
    """Hermes's positive stop evidence recorded on a stale return threads
    through to reconcile's synthetic retry outcome so a later fresh view
    can finalize — the return-pending event must carry the real evidence,
    not an empty one."""
    manager = TaskManager(
        vault_path=tmp_path / "hermes-vault",
        index_path=tmp_path / "hermes-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    store = SessionStore(tmp_path / "hermes-sessions.db")
    transcripts = TranscriptStore(tmp_path / "hermes-transcripts")
    parent = manager.create(
        "Coordinate the synthetic Hermes launch",
        status="in_progress",
        tags=["hermes", "agent-running"],
    )
    source = store.create(
        parent.id, status=STATUS_CLAIMED, routing="hermes", execution_spec=_spec("hermes"),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    source = store.get(parent.id)
    ctx = InterAgentContext(
        store, transcripts, source.session_id, Caps(),
        caller_attempt_id=source.attempt_id, caller_turn_id=source.turn_id,
        task_manager=manager,
    )
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="hermes",
        termination_evidence={
            "done_seen": True, "error_seen": False, "terminal_success": True,
        },
    )

    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True

    assert finalizers == []
    assert store.get(source.task_id).status == STATUS_RUNNING
    pending = [
        event for event in transcripts.read(source.session_id)
        if event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
    ]
    assert len(pending) == 1
    assert pending[0]["payload"]["termination_evidence"]["done_seen"] is True

    fresh["enabled"] = True
    worker._reconcile_project_handoffs()

    assert store.get(source.task_id).status == STATUS_COMPLETED
    assert finalizers == [{
        "operation_id": staged["operation_id"],
        "source_session_id": source.session_id,
        "source_attempt_id": source.attempt_id,
        "source_turn_id": source.turn_id,
    }]
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert all(
        manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(source.task_id)
    )


def test_reconcile_does_not_retry_a_newer_turn_over_a_stale_return_pending_event(
    handoff, tmp_path: Path,
):
    """A return-pending event stamped with an old turn must not be re-attempted
    once the session has advanced to a newer turn under the same handoff."""
    manager, store, transcripts, source, ctx = handoff
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    worker, finalizers = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True
    assert any(
        event["kind"] == PROJECT_HANDOFF_RETURN_PENDING_EVENT
        for event in transcripts.read(source.session_id)
    )
    # The old turn's own quiescence proof is recorded on return — that's
    # unaffected by the session moving on below and isn't what reconciliation
    # would need to retry finalization for the newer turn.
    quiescent_before = [
        event for event in transcripts.read(source.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
    ]
    assert len(quiescent_before) == 1

    # The session moves on to a newer turn under the same attempt — the
    # stale return-pending event above still names the old turn.
    advanced = store.begin_executor_turn(
        source.task_id, "execute", session=store.get(source.task_id),
    )
    assert advanced.turn_id != source.turn_id

    fresh["enabled"] = True
    worker._reconcile_project_handoffs()

    assert finalizers == []
    assert [
        event for event in transcripts.read(source.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
    ] == quiescent_before
    assert manager.get(source.task_id).fields[HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )


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


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_resume_pending_repairs_interrupted_child_staging(
    handoff, tmp_path: Path, monkeypatch, fail_on_call,
):
    manager, store, transcripts, source, ctx = handoff
    create = manager.create_or_find_by_operation
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("synthetic child persistence interruption")
        return create(*args, **kwargs)

    monkeypatch.setattr(manager, "create_or_find_by_operation", fail_once)
    failed = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    assert failed["ok"] is False
    assert failed["error"] == "crashed"
    assert HANDOFF_READY_AT_FIELD not in manager.get(source.task_id).fields
    assert len(build_task_hierarchy(manager.list_tasks()).children(source.task_id)) == (
        fail_on_call - 1
    )

    monkeypatch.setattr(manager, "create_or_find_by_operation", create)
    service = ProjectTaskService(manager, store, transcripts)
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts, service,
    )

    worker.resume_pending()

    parent = manager.get(source.task_id)
    assert parent.fields[LAST_HANDOFF_OPERATION_FIELD] == _request()["operation_id"]
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert len(build_task_hierarchy(manager.list_tasks()).children(source.task_id)) == 2
    assert len(finalizers) == 1
    assert store.get(source.task_id).status == STATUS_COMPLETED
    assert store.list_all_card_outcomes() == {}


def test_resume_pending_reconciles_quiescent_handoff_without_rolling_back_coordinator(
    handoff, tmp_path: Path,
):
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
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts,
        ProjectTaskService(manager, store, transcripts),
    )

    worker.resume_pending()

    assert len(finalizers) == 1
    assert manager.get(source.task_id).fields[LAST_HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )
    coordinator = store.get_by_session_id(staged["coordinator_session_id"])
    assert coordinator.status == STATUS_CLAIMED


def test_resume_pending_skips_a_source_the_reconcile_snapshot_already_finalized(
    handoff, tmp_path: Path, monkeypatch,
):
    """resume_pending snapshots non-terminal sessions before reconciliation
    finalizes a handoff — a source that reconciliation completes between the
    snapshot and the loop must not be rolled back as an orphan."""
    manager, store, transcripts, source, ctx = handoff
    stale_snapshot = _task_payload(manager, source.task_id)
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    fresh = {"enabled": False}
    finalizers: list[dict] = []
    puts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.startswith("/api/tasks/"):
            puts.append({
                "task_id": request.url.path.rsplit("/", 1)[-1],
                "body": json.loads(request.content),
            })
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and request.url.path == "/api/tasks":
            tasks = [
                payload
                for task in manager.list_tasks()
                if (payload := _task_payload(manager, task.id)) is not None
            ]
            return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})
        if request.method == "GET" and request.url.path == f"/api/tasks/{source.task_id}":
            if not fresh["enabled"]:
                return httpx.Response(200, json=stale_snapshot)
            payload = _task_payload(manager, source.task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "GET" and request.url.path.startswith("/api/tasks/"):
            task_id = request.url.path.rsplit("/", 1)[-1]
            payload = _task_payload(manager, task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith(
            "/project/handoff/finalize"
        ):
            task_id = request.url.path.split("/api/tasks/", 1)[1].split("/", 1)[0]
            body = json.loads(request.content)
            finalizers.append(body)
            try:
                result = service.finalize_handoff(task_id, **body)
            except ProjectHandoffError as exc:
                return httpx.Response(
                    409,
                    json={"detail": {"code": exc.code, "message": str(exc)}},
                )
            return httpx.Response(200, json=result)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://synthetic-api",
    )
    worker = Worker(
        api_base="http://synthetic-api",
        session_store=store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(
            db_path=tmp_path / "worker-spend.db", daily_cap_dollars=100,
        ),
        http_client=client,
    )
    notices: list[str] = []
    monkeypatch.setattr(worker, "_notify", lambda text, bot=None: notices.append(text))
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    # The exact turn returned while the task view was still stale (the
    # worker process then crashed/restarted before the view caught up),
    # leaving the session row RUNNING with the return-pending proof already
    # on its transcript.
    assert worker._maybe_finalize_project_handoff(store.get(source.task_id), outcome) is True
    assert store.get(source.task_id).status == STATUS_RUNNING

    fresh["enabled"] = True
    worker.resume_pending()

    assert store.get(source.task_id).status == STATUS_COMPLETED
    assert len(finalizers) == 1
    assert not any(
        event["kind"] == "resume_failed"
        for event in transcripts.read(source.session_id)
    )
    assert not any("could not be safely resumed" in text for text in notices)
    assert not any(put["task_id"] == source.task_id for put in puts)
    assert manager.get(source.task_id).fields[LAST_HANDOFF_OPERATION_FIELD] == (
        staged["operation_id"]
    )


def test_handoff_retry_repairs_coordinator_without_execution_snapshot(
    handoff, monkeypatch,
):
    manager, store, _transcripts, source, ctx = handoff
    persist_snapshot = store.set_execution_snapshot
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic coordinator snapshot interruption")
        return persist_snapshot(*args, **kwargs)

    monkeypatch.setattr(store, "set_execution_snapshot", fail_once)
    failed = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    assert failed["ok"] is False
    assert failed["error"] == "crashed"

    coordinator = next(
        session for session in store.list_non_terminal()
        if session.task_id != source.task_id
    )
    assert coordinator.execution_spec is None

    retried = dispatch(ctx, "lifeos_agent_project_handoff", _request())

    assert retried["ok"] is True
    assert retried["state"] == "staged"
    repaired = store.get_by_session_id(coordinator.session_id)
    assert repaired.execution_spec is not None
    assert repaired.status == STATUS_BLOCKED


@pytest.mark.asyncio
async def test_zero_child_pending_handoff_can_be_cancelled(handoff, monkeypatch):
    from api.routes import tasks as task_routes

    manager, store, transcripts, source, ctx = handoff

    def fail_first_child(*_args, **_kwargs):
        raise RuntimeError("synthetic interruption before first child")

    monkeypatch.setattr(manager, "create_or_find_by_operation", fail_first_child)
    assert dispatch(ctx, "lifeos_agent_project_handoff", _request())["ok"] is False
    assert build_task_hierarchy(manager.list_tasks()).children(source.task_id) == []
    fields = ProjectTaskService(manager, store, transcripts).read_fields(source.task_id)
    assert fields["is_project"] is False
    policy = agent_board.project_action_policy(
        manager.get(source.task_id).status,
        manager.get(source.task_id).tags,
        fields["project"],
        execution_paused=True,
        handoff_pending=bool(
            manager.get(source.task_id).fields.get(HANDOFF_OPERATION_FIELD)
        ),
    )
    assert policy["can_cancel_project"] is True
    assert policy["can_resume_execution"] is False
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", store)
    monkeypatch.setattr(task_routes, "_transcript_store", transcripts)
    response = TestClient(app).get(f"/api/tasks/{source.task_id}")
    assert response.status_code == 200
    assert response.json()["is_project"] is False
    assert response.json()["fields"][HANDOFF_OPERATION_FIELD] == _request()["operation_id"]
    stopped: list[str] = []

    async def stop_session(session):
        stopped.append(session.session_id)
        store.update_status(
            session.task_id,
            STATUS_FAILED,
            attempt_id=session.attempt_id,
            turn_id=session.turn_id,
            project=False,
        )
        return [session.session_id], []

    service = ProjectTaskService(
        manager, store, transcripts, session_teardown=stop_session,
    )
    preview = service.cancel_preview(source.task_id)
    first = await service.cancel_project(
        source.task_id, operation_id="cancel-interrupted-handoff",
    )

    assert preview["unfinished_count"] == 0
    assert preview["running_count"] == 1
    assert first["pending"] is True
    assert first["failures"]
    transcripts.append(source.session_id, HANDOFF_QUIESCENT_EVENT, {
        "project_id": source.task_id,
        "operation_id": _request()["operation_id"],
        "attempt_id": source.attempt_id,
        "turn_id": source.turn_id,
        "executor": source.routing,
    })
    result = await service.cancel_project(
        source.task_id, operation_id="cancel-interrupted-handoff",
    )

    assert result["complete"] is True
    assert stopped == [source.session_id]
    parent = manager.get(source.task_id)
    assert parent.status == "cancelled"
    assert parent.fields[LAST_ABORTED_HANDOFF_FIELD] == _request()["operation_id"]
    assert HANDOFF_OPERATION_FIELD not in parent.fields


def test_board_cancel_refuses_zero_child_handoff_before_teardown(handoff, monkeypatch):
    """Ordinary Cancel routes a zero-child handoff away before side effects."""
    from api.routes import agents as agent_routes

    manager, store, transcripts, source, ctx = handoff

    def fail_first_child(*_args, **_kwargs):
        raise RuntimeError("synthetic interruption before first child")

    monkeypatch.setattr(manager, "create_or_find_by_operation", fail_first_child)
    assert dispatch(ctx, "lifeos_agent_project_handoff", _request())["ok"] is False
    assert build_task_hierarchy(manager.list_tasks()).children(source.task_id) == []
    teardown_calls: list[str] = []

    async def teardown_spy(session, _reason):
        teardown_calls.append(session.session_id)
        return [session.session_id], []

    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_routes, "_session_store", store)
    monkeypatch.setattr(agent_routes, "_transcript_store", transcripts)
    monkeypatch.setattr(agent_routes, "_kill_session_subtree", teardown_spy)

    response = TestClient(app).post(
        f"/api/agents/board/cards/{source.task_id}/cancel",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "pending handoffs use the Cancel handoff action"
    )
    assert teardown_calls == []
    assert store.get(source.task_id).status == STATUS_RUNNING
    parent = manager.get(source.task_id)
    assert parent.status == "in_progress"
    assert parent.fields[HANDOFF_OPERATION_FIELD] == _request()["operation_id"]


class _HandoffHermesStream:
    """Synthetic upstream whose terminal evidence is controlled by the test."""

    def __init__(self, mode: str):
        self.mode = mode
        self.opened = threading.Event()
        self.released = threading.Event()
        self.closed = threading.Event()
        self.upstream_finished = False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield b'data: {"type":"conversation_id","conversation_id":"conv-synthetic"}\n\n'
        self.opened.set()
        if self.mode == "deadline":
            while not self.closed.is_set():
                time.sleep(0.005)
                yield b""
            return
        assert self.released.wait(5), "synthetic Hermes stream was never released"
        if self.mode == "done":
            self.upstream_finished = True
            yield b'data: {"type":"content","content":"handoff complete"}\n\n'
            yield b'data: {"type":"done"}\n\n'
            return
        raise httpx.ReadError("synthetic upstream connection dropped")


class _HandoffHermesClient:
    def __init__(self, stream: _HandoffHermesStream):
        self.stream_response = stream

    def stream(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self.stream_response

    def __exit__(self, *_args):
        return False

    def close(self):
        self.stream_response.closed.set()
        self.stream_response.released.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["cancelled", "disconnect", "deadline", "done"])
async def test_hermes_handoff_requires_positive_done_evidence(
    tmp_path: Path, monkeypatch, termination: str,
):
    """Only a real Hermes done event proves that the upstream turn stopped."""
    from api.routes import hermes_proxy
    from api.services.conversation_store import ConversationStore
    from api.services.usage_store import UsageStore
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://hermes.example")
    monkeypatch.setattr(settings, "hermes_backend_token", "synthetic-backend-token")
    monkeypatch.setattr(settings, "mcp_bearer_token", "synthetic-turn-secret")
    monkeypatch.setattr(settings, "claude_timeout_seconds", 3600)
    conversations = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hermes_proxy, "get_store", lambda: conversations)
    monkeypatch.setattr(hermes_proxy, "get_usage_store", lambda: usage)
    monkeypatch.setattr(hermes_proxy, "schedule_retitle", lambda _conversation_id: None)

    store = SessionStore(tmp_path / "hermes-sessions.db")
    transcripts = TranscriptStore(tmp_path / "hermes-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "hermes-vault",
        index_path=tmp_path / "hermes-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Coordinate the synthetic Hermes launch",
        status="in_progress",
        tags=["hermes", "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing="hermes",
        budget={"wall_seconds": 0.3} if termination == "deadline" else None,
        execution_spec=_spec("hermes"),
    )
    upstream = _HandoffHermesStream(
        "done" if termination == "done" else termination,
    )
    executor = HermesExecutor(
        session_store=store,
        transcript_store=transcripts,
        http_client_factory=lambda: _HandoffHermesClient(upstream),
    )
    outcomes: list[ExecutorOutcome] = []
    executor_thread = threading.Thread(
        target=lambda: outcomes.append(
            executor.execute(
                source,
                {"description": "Coordinate synthetic Hermes work"},
            )
        ),
        daemon=True,
    )
    executor_thread.start()
    assert upstream.opened.wait(5)
    running = store.get(parent.id)
    assert running.routing == "hermes"
    assert running.status == STATUS_RUNNING
    context = InterAgentContext(
        store,
        transcripts,
        running.session_id,
        Caps(),
        caller_attempt_id=running.attempt_id,
        caller_turn_id=running.turn_id,
        task_manager=manager,
    )
    staged = dispatch(context, "lifeos_agent_project_handoff", _request())
    assert staged["ok"] is True

    def stop_session(session):
        result = teardown_session(
            store,
            transcripts,
            session,
            transcript_kind="operator_killed",
            transcript_payload={"reason": "synthetic project cancellation"},
            managed_driver=None,
        )
        failures = []
        if result["managed_failure"]:
            failures.append({
                "session_id": session.session_id,
                "reason": result["managed_failure"],
            })
        return [session.session_id], failures

    service = ProjectTaskService(
        manager, store, transcripts, session_teardown=stop_session,
    )
    cancel_operation = "cancel-hermes-synthetic"
    if termination == "cancelled":
        pending = await service.cancel_project(
            parent.id, operation_id=cancel_operation,
        )
        assert pending["pending"] is True
        assert pending["failures"]
    elif termination != "deadline":
        upstream.released.set()

    executor_thread.join(5)
    assert outcomes, "Hermes executor did not return"
    outcome = outcomes[0]
    assert outcome.executor == "hermes"
    assert outcome.termination_evidence.get("done_seen") is (termination == "done")
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts, service,
    )
    refreshed = store.get(parent.id)
    worker._handle_outcome(
        refreshed,
        _task_payload(manager, parent.id),
        normalize_outcome(
            outcome,
            refreshed,
            route="hermes",
            transcript_store=transcripts,
        ),
    )
    proof = [
        event
        for event in transcripts.read(running.session_id)
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
    ]

    if termination == "done":
        assert upstream.upstream_finished is True
        assert len(proof) == 1
        assert len(finalizers) == 1
        assert HANDOFF_OPERATION_FIELD not in manager.get(parent.id).fields
        assert all(
            manager.can_start_execution(child.id)
            for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
        )
        return

    assert upstream.upstream_finished is False
    assert proof == []
    assert finalizers == []
    assert worker._reconcile_project_handoffs() == 0
    assert len(finalizers) == 1
    fenced = manager.get(parent.id)
    assert fenced.fields[HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    assert all(
        not manager.can_start_execution(child.id)
        for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
    )
    pending = await service.cancel_project(
        parent.id, operation_id=cancel_operation,
    )
    assert pending["pending"] is True
    assert pending["failures"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["terminal", "absent"])
async def test_cancel_does_not_treat_terminal_or_absent_source_as_stop_proof(
    handoff, monkeypatch, source_state,
):
    manager, store, transcripts, source, ctx = handoff

    def fail_first_child(*_args, **_kwargs):
        raise RuntimeError("synthetic interruption before first child")

    monkeypatch.setattr(manager, "create_or_find_by_operation", fail_first_child)
    assert dispatch(ctx, "lifeos_agent_project_handoff", _request())["ok"] is False
    if source_state == "terminal":
        assert store.update_status(
            source.task_id,
            STATUS_FAILED,
            attempt_id=source.attempt_id,
            turn_id=source.turn_id,
            project=False,
        )
    else:
        with store._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (source.session_id,))

    result = await ProjectTaskService(manager, store, transcripts).cancel_project(
        source.task_id, operation_id=f"cancel-{source_state}-source",
    )

    assert result["complete"] is False
    assert result["pending"] is True
    assert result["failures"] == [{
        "session_id": source.session_id,
        "reason": "handoff source stop is not yet verified",
    }]
    parent = manager.get(source.task_id)
    assert parent.status != "cancelled"
    assert parent.fields[HANDOFF_OPERATION_FIELD] == _request()["operation_id"]


@pytest.mark.parametrize(
    "operator_first,remote_status,identity_mutation,expected_complete",
    [
        (True, "cancelled", None, True),
        (False, "cancelled", None, True),
        (False, "running", None, False),
        (False, None, None, False),
        (False, "cancelled", "attempt", False),
        (False, "cancelled", "turn", False),
    ],
)
def test_stop_and_cancel_entrypoints_only_accept_positive_managed_proof(
    tmp_path: Path,
    monkeypatch,
    operator_first: bool,
    remote_status: str | None,
    identity_mutation: str | None,
    expected_complete: bool,
):
    """Real stop/cancel routes accept only an exact positive Managed probe."""
    from api.routes import agents as agent_routes
    from api.routes import tasks as task_routes

    store = SessionStore(tmp_path / "managed-kill.db")
    transcripts = TranscriptStore(tmp_path / "managed-kill-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "managed-kill-vault",
        index_path=tmp_path / "managed-kill-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Synthetic managed handoff kill",
        status="in_progress",
        tags=["cloud-sonnet", "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing="claude",
        execution_spec=_spec("claude"),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    store.set_managed_session_id(parent.id, "managed-kill-synthetic")
    source = store.get(parent.id)
    staged = ProjectTaskService(manager, store, transcripts).stage_handoff(
        source,
        operation_id="managed-kill-handoff",
        children=_request()["children"],
    )
    if identity_mutation == "attempt":
        manager.update(
            parent.id,
            fields={HANDOFF_SOURCE_ATTEMPT_FIELD: "attempt-newer-synthetic"},
            _project_operation="handoff-stage",
        )
    elif identity_mutation == "turn":
        manager.update(
            parent.id,
            fields={HANDOFF_SOURCE_TURN_FIELD: "turn-newer-synthetic"},
            _project_operation="handoff-stage",
        )
    driver = _ManagedRecoveryDriver(remote_status) if remote_status is not None else None
    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    monkeypatch.setattr(agent_routes, "_session_store", store)
    monkeypatch.setattr(agent_routes, "_transcript_store", transcripts)
    monkeypatch.setattr(agent_routes, "_maybe_managed_driver", lambda: driver)
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", store)
    monkeypatch.setattr(task_routes, "_transcript_store", transcripts)
    client = TestClient(app)

    if operator_first:
        response = client.post(
            f"/api/agents/sessions/{source.session_id}/kill",
            json={"reason": "synthetic operator stop"},
        )

        assert response.status_code == 200
        if remote_status == "running":
            assert response.json()["failures"] == [{
                "session_id": source.session_id,
                "reason": "managed runtime still reports running",
            }]
        if remote_status == "cancelled" and identity_mutation is None:
            worker, finalizers = _handoff_worker(
                tmp_path,
                manager,
                store,
                transcripts,
                ProjectTaskService(manager, store, transcripts),
            )
            assert worker._reconcile_project_handoffs() == 0
            assert len(finalizers) == 1
            assert manager.get(parent.id).fields[HANDOFF_OPERATION_FIELD] == (
                staged["operation_id"]
            )
            assert all(
                not manager.can_start_execution(child.id)
                for child in build_task_hierarchy(manager.list_tasks()).children(parent.id)
            )
    cancel_response = client.post(
        f"/api/tasks/{parent.id}/project/cancel",
        json={"confirm": True, "operation_id": "cancel-after-managed-stop"},
    )

    assert cancel_response.status_code == 200
    result = cancel_response.json()
    events = transcripts.read(source.session_id)
    matching = [
        event for event in events
        if event["kind"] == HANDOFF_QUIESCENT_EVENT
        and event["payload"].get("operation_id") == staged["operation_id"]
        and event["payload"].get("attempt_id") == source.attempt_id
        and event["payload"].get("turn_id") == source.turn_id
    ]
    assert bool(matching) is expected_complete

    assert result["complete"] is expected_complete
    assert result["pending"] is (not expected_complete)
    assert bool(result["failures"]) is (not expected_complete)
    current = manager.get(parent.id)
    assert (current.status == "cancelled") is expected_complete
    assert (HANDOFF_OPERATION_FIELD not in current.fields) is expected_complete


class _ManagedRecoveryDriver:
    def __init__(self, status: str):
        self.status = status
        self.kills: list[tuple[str, str]] = []

    def kill_session(self, session_id: str, reason: str = "") -> None:
        self.kills.append((session_id, reason))

    def get_session_state(self, session_id: str):
        return SimpleNamespace(session_id=session_id, status=self.status)


@pytest.mark.parametrize(
    "route,verified",
    [("local", True), ("remote", True), ("claude_code", False), ("codex", False),
     ("hermes", False)],
)
def test_resume_pending_only_recovers_routes_with_restart_stop_proof(
    tmp_path: Path, route: str, verified: bool,
):
    store = SessionStore(tmp_path / f"{route}.db")
    transcripts = TranscriptStore(tmp_path / f"{route}-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / f"{route}-vault",
        index_path=tmp_path / f"{route}-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        f"Synthetic {route} handoff",
        status="in_progress",
        tags=[route, "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing=route,
        execution_spec=_spec(route),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    source = store.get(parent.id)
    result = ProjectTaskService(manager, store, transcripts).stage_handoff(
        source,
        operation_id=f"{route}-restart",
        children=_request()["children"],
    )
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts,
        ProjectTaskService(manager, store, transcripts),
    )

    worker.resume_pending()

    current = store.get(parent.id)
    if verified:
        assert current.status == STATUS_COMPLETED
        assert len(finalizers) == 1
        assert manager.get(parent.id).fields[LAST_HANDOFF_OPERATION_FIELD] == result["operation_id"]
    else:
        assert current.status == STATUS_RUNNING
        assert finalizers == []
        assert manager.get(parent.id).fields[HANDOFF_OPERATION_FIELD] == result["operation_id"]
        assert store.get_by_session_id(result["coordinator_session_id"]).status == STATUS_BLOCKED
        assert transcripts.read(source.session_id)[-1]["kind"] == (
            "project_handoff_recovery_pending"
        )


@pytest.mark.parametrize("remote_status,activated", [("cancelled", True), ("running", False)])
def test_resume_pending_managed_requires_post_kill_remote_stop_proof(
    tmp_path: Path, remote_status: str, activated: bool,
):
    store = SessionStore(tmp_path / "managed.db")
    transcripts = TranscriptStore(tmp_path / "managed-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "managed-vault",
        index_path=tmp_path / "managed-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Synthetic managed handoff",
        status="in_progress",
        tags=["cloud-sonnet", "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing="claude",
        execution_spec=_spec("claude"),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    store.set_managed_session_id(parent.id, "managed-synthetic")
    source = store.get(parent.id)
    staged = ProjectTaskService(manager, store, transcripts).stage_handoff(
        source,
        operation_id="managed-restart",
        children=_request()["children"],
    )
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts,
        ProjectTaskService(manager, store, transcripts),
    )
    driver = _ManagedRecoveryDriver(remote_status)
    worker._managed_executor = SimpleNamespace(driver=driver)

    worker.resume_pending()

    assert driver.kills == [
        ("managed-synthetic", "project_handoff_restart_recovery"),
    ]
    current = store.get(parent.id)
    if activated:
        assert current.status == STATUS_COMPLETED
        assert len(finalizers) == 1
        assert manager.get(parent.id).fields[LAST_HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    else:
        assert current.status == STATUS_RUNNING
        assert finalizers == []
        assert manager.get(parent.id).fields[HANDOFF_OPERATION_FIELD] == staged["operation_id"]
        assert store.get_by_session_id(staged["coordinator_session_id"]).status == STATUS_BLOCKED


def test_resume_pending_does_not_rearm_a_newer_turn_over_stale_handoff_identity(
    handoff, tmp_path: Path,
):
    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    manager.update(
        source.task_id,
        fields={HANDOFF_SOURCE_TURN_FIELD: "turn-stale-synthetic"},
        _project_operation="handoff-stage",
    )
    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts,
        ProjectTaskService(manager, store, transcripts),
    )

    worker.resume_pending()

    assert store.get(source.task_id).status == STATUS_RUNNING
    assert finalizers == []
    parent = manager.get(source.task_id)
    assert parent.status == "in_progress"
    assert parent.fields[HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    assert "agent-running" in parent.tags


def test_resume_pending_recovers_a_restart_inside_the_stale_view_window(
    handoff, tmp_path: Path, monkeypatch,
):
    """A restart landing inside the task-view debounce window must not roll
    the handoff source back to FAILED: the bounded re-fetch in restart
    recovery must see the view catch up and finalize normally."""
    from api.services.agent_worker import worker as worker_module

    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    service = ProjectTaskService(manager, store, transcripts)
    parent_before = manager.get(source.task_id)
    stale_snapshot = {
        "id": source.task_id,
        "description": parent_before.description,
        "status": "in_progress",
        "tags": list(parent_before.tags),
        "fields": {},
    }

    # A prior process already returned this exact turn while the view was
    # stale — recording quiescence + return-pending proof and leaving the
    # source RUNNING, as `_maybe_finalize_project_handoff` does.
    fresh = {"enabled": False}
    stale_worker, _ = _stale_view_worker(
        tmp_path, manager, store, transcripts, service,
        source.task_id, stale_snapshot, fresh,
    )
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        session_id=source.session_id,
        attempt_id=source.attempt_id,
        turn_id=source.turn_id,
        executor="local",
    )
    assert stale_worker._maybe_finalize_project_handoff(
        store.get(source.task_id), outcome,
    ) is True
    assert store.get(source.task_id).status == STATUS_RUNNING

    # A new Worker process (the restart) whose task view for this source is
    # still stale on the first calls, then catches up partway through the
    # bounded re-fetch — modeling the file-watcher debounce resolving mid-wait.
    calls = {"n": 0}
    fresh_after = 3
    finalizers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            tasks = [
                payload
                for task in manager.list_tasks()
                if (payload := _task_payload(manager, task.id)) is not None
            ]
            return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})
        if request.method == "GET" and request.url.path == f"/api/tasks/{source.task_id}":
            calls["n"] += 1
            if calls["n"] <= fresh_after:
                return httpx.Response(200, json=stale_snapshot)
            payload = _task_payload(manager, source.task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "GET" and request.url.path.startswith("/api/tasks/"):
            task_id = request.url.path.rsplit("/", 1)[-1]
            payload = _task_payload(manager, task_id)
            return httpx.Response(200, json=payload) if payload else httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith(
            "/project/handoff/finalize"
        ):
            task_id = request.url.path.split("/api/tasks/", 1)[1].split("/", 1)[0]
            body = json.loads(request.content)
            finalizers.append(body)
            try:
                result = service.finalize_handoff(task_id, **body)
            except ProjectHandoffError as exc:
                return httpx.Response(
                    409,
                    json={"detail": {"code": exc.code, "message": str(exc)}},
                )
            return httpx.Response(200, json=result)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://synthetic-api",
    )
    restarted = Worker(
        api_base="http://synthetic-api",
        session_store=store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(
            db_path=tmp_path / "restart-spend.db", daily_cap_dollars=100,
        ),
        http_client=client,
    )
    notices: list[str] = []
    monkeypatch.setattr(restarted, "_notify", lambda text, bot=None: notices.append(text))
    monkeypatch.setattr(worker_module, "_HANDOFF_RECOVERY_STALE_VIEW_ATTEMPTS", 5)
    monkeypatch.setattr(worker_module, "_HANDOFF_RECOVERY_STALE_VIEW_RETRY_DELAY_S", 0)

    recovered = restarted.resume_pending()

    assert recovered == 0
    assert calls["n"] > fresh_after, "the fix must retry past the stale view"
    current = store.get(source.task_id)
    assert current.status == STATUS_COMPLETED
    assert finalizers == [{
        "operation_id": staged["operation_id"],
        "source_session_id": source.session_id,
        "source_attempt_id": source.attempt_id,
        "source_turn_id": source.turn_id,
    }]
    parent = manager.get(source.task_id)
    assert HANDOFF_OPERATION_FIELD not in parent.fields
    assert parent.fields[LAST_HANDOFF_OPERATION_FIELD] == staged["operation_id"]
    assert parent.status != "todo"
    assert not any(
        event["kind"] == "resume_failed" for event in transcripts.read(source.session_id)
    )
    assert not any("could not be safely resumed" in notice for notice in notices)


def test_resume_pending_skips_the_stale_view_wait_without_a_request_event(
    tmp_path: Path, monkeypatch,
):
    """An ordinary crashed session — no handoff ever staged for its turn —
    takes the existing orphan rollback immediately; the stale-view wait added
    for handoff restart recovery must never trigger for it."""
    store = SessionStore(tmp_path / "ordinary-sessions.db")
    transcripts = TranscriptStore(tmp_path / "ordinary-transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "ordinary-vault",
        index_path=tmp_path / "ordinary-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    task = manager.create(
        "Synthetic ordinary task", status="in_progress", tags=["local", "agent-running"],
    )
    session = store.create(
        task.id, status=STATUS_CLAIMED, routing="local", execution_spec=_spec(),
    )
    session = store.begin_executor_turn(task.id, "execute", session=session)
    assert store.mark_executor_turn_running(task.id, session.attempt_id, session.turn_id)

    worker, finalizers = _handoff_worker(
        tmp_path, manager, store, transcripts, ProjectTaskService(manager, store, transcripts),
    )
    notices: list[str] = []
    monkeypatch.setattr(worker, "_notify", lambda text, bot=None: notices.append(text))
    fetch_calls = {"n": 0}
    original_fetch_task = worker._fetch_task

    def counting_fetch_task(task_id, **kwargs):
        fetch_calls["n"] += 1
        return original_fetch_task(task_id, **kwargs)

    monkeypatch.setattr(worker, "_fetch_task", counting_fetch_task)

    started = time.monotonic()
    recovered = worker.resume_pending()
    elapsed = time.monotonic() - started

    # No retry loop was entered: the fetch count matches the ordinary
    # orphan-rollback path exactly (one in `_recover_project_handoff_session`,
    # two inside the FAILED-status projection, one for the rollback's task
    # snapshot) — never the extra fetches a stale-view wait would add.
    assert fetch_calls["n"] == 4
    assert elapsed < 1.0
    assert recovered == 1
    assert finalizers == []
    current = store.get(task.id)
    assert current.status == STATUS_FAILED
    assert transcripts.read(session.session_id)[-1]["kind"] == "resume_failed"
    assert any("could not be safely resumed" in notice for notice in notices)


def test_remove_tag_returns_conflict_for_handoff_fences_and_allows_ordinary_task(
    handoff, monkeypatch,
):
    from api.routes import tasks as task_routes

    manager, store, transcripts, source, ctx = handoff
    staged = dispatch(ctx, "lifeos_agent_project_handoff", _request())
    child_id = staged["child_tasks"][0]["task_id"]
    child = manager.get(child_id)
    manager.update(
        child_id,
        tags=[*child.tags, "agent-running"],
        _project_operation="handoff-stage",
    )
    ordinary = manager.create("Synthetic ordinary running task", tags=["agent-running"])
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", store)
    monkeypatch.setattr(task_routes, "_transcript_store", transcripts)
    client = TestClient(app, raise_server_exceptions=False)

    parent_response = client.post(
        f"/api/tasks/{source.task_id}/remove-tag", params={"tag": "agent-running"},
    )
    child_response = client.post(
        f"/api/tasks/{child_id}/remove-tag", params={"tag": "agent-running"},
    )
    ordinary_response = client.post(
        f"/api/tasks/{ordinary.id}/remove-tag", params={"tag": "agent-running"},
    )

    assert parent_response.status_code == 409
    assert child_response.status_code == 409
    assert ordinary_response.status_code == 200
    assert ordinary_response.json() == {"ok": True, "reason": None}
