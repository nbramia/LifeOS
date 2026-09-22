"""Hierarchy-aware worker listing and child prompt context."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.session_store import STATUS_COMPLETED
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


def _worker(tmp_path: Path, handler, *, local_executor=None) -> Worker:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(tmp_path / "transcripts"),
        http_client=client,
        local_executor=local_executor,
        preflight_caller=lambda _prompt: json.dumps({
            "budget": {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 0.1},
            "routing": "local",
            "routing_reason": "explicit task tag",
            "routing_explicit": True,
            "expected_output": "text",
            "ambiguity": None,
            "sane": True,
            "sane_reason": "",
        }),
    )


def test_candidate_filter_excludes_projects_paused_and_invalid(tmp_path: Path):
    tasks = [
        {"id": "plain001", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": False, "hierarchy_valid": True},
        {"id": "project1", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": True, "hierarchy_valid": True},
        {"id": "paused01", "status": "todo", "tags": ["codex"], "fields": {"execution_paused": "true"}, "is_project": False, "hierarchy_valid": True},
        {"id": "invalid1", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": False, "hierarchy_valid": False},
        {"id": "cancel01", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": False, "hierarchy_valid": True, "parent_cancellation_pending": True},
        {"id": "handoff01", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": False, "hierarchy_valid": True, "parent_handoff_pending": True},
        {"id": "prjpaused1", "status": "todo", "tags": ["codex"], "fields": {}, "is_project": False, "hierarchy_valid": True, "parent_project_paused": True},
    ]

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"tasks": tasks})
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    assert [task["id"] for task in worker._list_agent_tasks()] == ["plain001"]


def test_claim_current_checks_do_not_reject_a_paused_project_parent(tmp_path: Path):
    """`_task_claim_is_current`/`_claim_is_current`/`_revalidate_task_resume`
    gate every tick of continuing ALREADY in-flight work (Managed polling,
    sleeping-session wakes, spawned-child waits, operator replies) — unlike
    a pending cancellation or handoff, a pause must never fail these closed,
    or a running child would be stranded and its remote session abandoned
    instead of finishing into Review (D5, AC3). Pause is enforced only at
    NEW claims (`_list_agent_tasks`, the server's `_project_claim_allowed`),
    covered by `test_candidate_filter_excludes_projects_paused_and_invalid`
    above."""
    task = {
        "id": "child01", "status": "in_progress", "tags": ["codex", "agent-running"],
        "fields": {}, "is_project": False, "hierarchy_valid": True,
        "parent_project_paused": True,
    }

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/child01":
            return httpx.Response(200, json=task)
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    assert worker._task_claim_is_current(task) is True
    assert worker._claim_is_current("child01") is True

    from api.services.agent_worker.session_store import Session

    session = Session(
        task_id="child01", session_id="sess-child01",
        status="blocked", started_at=0, last_activity_at=0,
    )
    assert worker._revalidate_task_resume(session, "phase", {"codex"}) == task


def test_paused_parent_does_not_stop_managed_polling_of_a_running_child(tmp_path: Path):
    """A live `#cloud` child of a paused project keeps being polled — Pause
    must not mark it FAILED out from under an in-flight remote session."""
    from api.services.agent_worker.local_executor import ExecutorOutcome
    from api.services.agent_worker.session_store import STATUS_RUNNING

    task = {
        "id": "child01", "status": "in_progress", "tags": ["cloud", "agent-running"],
        "fields": {}, "is_project": False, "hierarchy_valid": True,
        "parent_project_paused": True,
    }
    polled: list[str] = []

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/child01":
            return httpx.Response(200, json=task)
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    worker.session_store.create("child01", status=STATUS_RUNNING, routing="claude")
    worker.session_store.set_managed_session_id("child01", "managed-child01")

    class _StubManagedExecutor:
        def poll(self, session):
            polled.append(session.task_id)
            return ExecutorOutcome(
                status=STATUS_RUNNING,
                session_id=session.session_id,
                attempt_id=session.attempt_id,
                turn_id=session.turn_id,
            )

    worker._get_managed_executor = lambda: _StubManagedExecutor()

    worker._poll_managed_sessions()

    assert polled == ["child01"]
    assert worker.session_store.get("child01").status == STATUS_RUNNING


def test_paused_parent_does_not_block_an_operator_answer_to_a_blocked_child(tmp_path: Path):
    """Resuming an existing turn (an operator answer to a blocked question)
    continues in-flight work rather than making a new claim, so it must
    still be processed for a child of a paused project."""
    from api.services.agent_worker.session_store import STATUS_BLOCKED, Session

    task = {
        "id": "child01", "status": "blocked", "tags": ["codex", "agent-blocked"],
        "fields": {}, "is_project": False, "hierarchy_valid": True,
        "parent_project_paused": True,
    }

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/child01":
            return httpx.Response(200, json=task)
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    session = Session(
        task_id="child01", session_id="sess-child01",
        status=STATUS_BLOCKED, started_at=0, last_activity_at=0,
    )

    revalidated = worker._revalidate_task_resume(session, "answer", {"agent-blocked"})

    assert revalidated == task


def test_child_dispatch_context_is_bounded_to_its_project(tmp_path: Path):
    parent = {
        "id": "parent01",
        "description": "Synthetic project objective",
        "notes": "Acceptance: synthetic verification passes.",
        "child_count": 2,
    }
    children = [
        {"id": "child01", "status": "in_progress", "tags": ["codex"]},
        {"id": "child02", "status": "todo", "tags": ["me"]},
    ]

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/parent01":
            return httpx.Response(200, json=parent)
        if request.url.path == "/api/tasks/parent01/children":
            return httpx.Response(200, json={"tasks": children, "total": 2})
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    enriched = worker._with_project_context({
        "id": "child01",
        "description": "Synthetic child",
        "notes": "Child-only instructions.",
        "fields": {"parent_id": "parent01"},
    })

    assert enriched["project_context"] == {
        "parent_id": "parent01",
        "parent_title": "Synthetic project objective",
        "parent_notes": "Acceptance: synthetic verification passes.",
        "children_total": 2,
        "children": [
            {"id": "child01", "status": "in_progress", "assignee": "codex"},
            {"id": "child02", "status": "todo", "assignee": "me"},
        ],
        "children_partial": False,
    }
    assert "Child-only instructions" in enriched["notes"]
    assert "Synthetic project objective" in enriched["notes"]
    assert "Acceptance: synthetic verification passes" in enriched["notes"]


def test_project_location_context_carries_the_recorded_integration_branch(tmp_path: Path):
    parent = {
        "id": "parent-int",
        "description": "Synthetic integration project",
        "notes": "",
        "child_count": 1,
        "fields": {"project_integration_branch": "feat/synthetic-integration-abc12345"},
    }
    children = [{"id": "child-int", "status": "todo", "tags": ["claude"]}]

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/parent-int":
            return httpx.Response(200, json=parent)
        if request.url.path == "/api/tasks/parent-int/children":
            return httpx.Response(200, json={"tasks": children, "total": 1})
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    enriched = worker._with_project_context({
        "id": "child-int",
        "description": "Synthetic child",
        "fields": {"parent_id": "parent-int"},
    })

    assert enriched["_project_location_context"]["integration_branch"] == (
        "feat/synthetic-integration-abc12345"
    )


def test_project_location_context_has_no_integration_branch_when_unrecorded(tmp_path: Path):
    parent = {
        "id": "parent-no-int",
        "description": "Synthetic ordinary project",
        "notes": "",
        "child_count": 1,
        "fields": {},
    }
    children = [{"id": "child-no-int", "status": "todo", "tags": ["claude"]}]

    def handler(request: httpx.Request):
        if request.url.path == "/api/tasks/parent-no-int":
            return httpx.Response(200, json=parent)
        if request.url.path == "/api/tasks/parent-no-int/children":
            return httpx.Response(200, json={"tasks": children, "total": 1})
        return httpx.Response(404)

    worker = _worker(tmp_path, handler)
    enriched = worker._with_project_context({
        "id": "child-no-int",
        "description": "Synthetic child",
        "fields": {"parent_id": "parent-no-int"},
    })

    assert enriched["_project_location_context"]["integration_branch"] is None


def test_actual_local_executor_receives_bounded_project_context(tmp_path: Path, monkeypatch):
    child = {
        "id": "child01",
        "description": "Synthetic child",
        "status": "in_progress",
        "notes": "Child-only instructions.",
        "tags": ["local", "agent-running"],
        "fields": {"parent_id": "parent01"},
        "parent_id": "parent01",
    }
    parent = {
        "id": "parent01",
        "description": "Synthetic project objective",
        "notes": "Acceptance: synthetic verification passes.",
        "child_count": 1,
    }
    seen: list[dict] = []
    preflight_calls: list[dict] = []

    class CaptureExecutor:
        def execute(self, _session, task):
            seen.append(task)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="synthetic done")

    def handler(request: httpx.Request):
        if request.method == "GET" and request.url.path == "/api/tasks/child01":
            return httpx.Response(200, json=child)
        if request.method == "GET" and request.url.path == "/api/tasks/parent01":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/api/tasks/parent01/children":
            return httpx.Response(200, json={"tasks": [child], "total": 1})
        if request.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={})

    worker = _worker(tmp_path, handler, local_executor=CaptureExecutor())
    from api.services.agent_worker import worker as worker_module

    original_preflight = worker_module.run_preflight

    def capture_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return original_preflight(**kwargs)

    monkeypatch.setattr(worker_module, "run_preflight", capture_preflight)
    worker.session_store.create(task_id="child01", status="claimed", routing="local")
    worker._dispatch(child)

    assert len(seen) == 1
    assert seen[0]["project_context"]["parent_id"] == "parent01"
    assert seen[0]["project_context"]["children"] == [
        {"id": "child01", "status": "in_progress", "assignee": "local"},
    ]
    assert "Synthetic project objective" in seen[0]["notes"]
    assert "Acceptance: synthetic verification passes" in seen[0]["notes"]
    assert len(preflight_calls) == 1
    assert preflight_calls[0]["title"] == "Synthetic child"
    safety_context = preflight_calls[0]["safety_context"]
    assert "Synthetic child" in safety_context
    assert "Child-only instructions" in safety_context
    assert "Synthetic project objective" in safety_context
    assert "Acceptance: synthetic verification passes" in safety_context
    assert "Child status summary" not in safety_context
    assert "_preflight_safety_context" not in seen[0]
    assert "_project_location_context" not in seen[0]


def test_child_affinity_precedes_compatible_parent_location(tmp_path: Path, monkeypatch):
    child_dir = tmp_path / "child-repo"
    child_dir.mkdir()
    parent_dir = tmp_path / "parent-repo"
    parent_dir.mkdir()
    child = {
        "id": "child-location",
        "description": "Implement synthetic phase",
        "status": "in_progress",
        "tags": ["local", "agent-running"],
        "fields": {"parent_id": "parent-location", "project": "child-affinity"},
        "parent_id": "parent-location",
    }
    parent = {
        "id": "parent-location",
        "description": "Synthetic location project",
        "notes": "Acceptance: use the child repository.",
        "child_count": 1,
        "fields": {"working_dir": str(parent_dir), "project": "parent-affinity"},
    }

    def handler(request: httpx.Request):
        if request.method == "GET" and request.url.path == "/api/tasks/child-location":
            return httpx.Response(200, json=child)
        if request.method == "GET" and request.url.path == "/api/tasks/parent-location":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/api/tasks/parent-location/children":
            return httpx.Response(200, json={"tasks": [child], "total": 1})
        if request.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        "api.services.directory_resolver._location_options",
        lambda: [
            ("child-affinity", "synthetic child repository", str(child_dir)),
            ("parent-affinity", "synthetic parent repository", str(parent_dir)),
        ],
    )

    class CaptureExecutor:
        def execute(self, _session, _task):
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="synthetic done")

    worker = _worker(tmp_path, handler, local_executor=CaptureExecutor())
    worker.session_store.create(task_id=child["id"], status="claimed", routing="local")
    worker._dispatch(child)

    session = worker.session_store.get(child["id"])
    assert session.execution_spec["working_dir"] == str(child_dir)


def test_parent_affinity_precedes_title_fallback_for_local_child(tmp_path: Path, monkeypatch):
    parent_dir = tmp_path / "parent-repo"
    parent_dir.mkdir()
    title_dir = tmp_path / "title-repo"
    title_dir.mkdir()
    child = {
        "id": "child-parent-affinity",
        "description": "Implement synthetic phase",
        "status": "in_progress",
        "tags": ["local", "agent-running"],
        "fields": {"parent_id": "parent-affinity"},
        "parent_id": "parent-affinity",
    }
    parent = {
        "id": "parent-affinity",
        "description": "Synthetic affinity project",
        "notes": "Acceptance: use the parent repository.",
        "child_count": 1,
        "fields": {"project": "parent-repo"},
    }

    def handler(request: httpx.Request):
        if request.method == "GET" and request.url.path == "/api/tasks/child-parent-affinity":
            return httpx.Response(200, json=child)
        if request.method == "GET" and request.url.path == "/api/tasks/parent-affinity":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/api/tasks/parent-affinity/children":
            return httpx.Response(200, json={"tasks": [child], "total": 1})
        if request.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_location_affinity",
        lambda affinity: str(parent_dir) if affinity == "parent-repo" else None,
    )
    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_working_directory",
        lambda _title, **_kwargs: str(title_dir),
    )

    class CaptureExecutor:
        def execute(self, _session, _task):
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="synthetic done")

    worker = _worker(tmp_path, handler, local_executor=CaptureExecutor())
    worker.session_store.create(task_id=child["id"], status="claimed", routing="local")
    worker._dispatch(child)

    session = worker.session_store.get(child["id"])
    assert session.execution_spec["working_dir"] == str(parent_dir)


def test_non_cli_child_skips_missing_inferred_locations(tmp_path: Path, monkeypatch):
    missing_affinity = tmp_path / "missing-affinity"
    missing_title = tmp_path / "missing-title"
    child = {
        "id": "child-missing-affinity",
        "description": "Implement synthetic phase",
        "status": "in_progress",
        "tags": ["local", "agent-running"],
        "fields": {"project": "missing-repo"},
    }
    seen: list[dict] = []

    def handler(request: httpx.Request):
        if request.method == "GET" and request.url.path == "/api/tasks/child-missing-affinity":
            return httpx.Response(200, json=child)
        if request.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_location_affinity",
        lambda affinity: str(missing_affinity) if affinity == "missing-repo" else None,
    )
    resolver_calls: list[bool] = []

    def resolve_title(_title, *, allow_uncloned=True):
        resolver_calls.append(allow_uncloned)
        return str(missing_title)

    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_working_directory",
        resolve_title,
    )

    class CaptureExecutor:
        def execute(self, _session, task):
            seen.append(task)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="synthetic done")

    worker = _worker(tmp_path, handler, local_executor=CaptureExecutor())
    worker.session_store.create(task_id=child["id"], status="claimed", routing="local")
    worker._dispatch(child)

    session = worker.session_store.get(child["id"])
    assert resolver_calls == [False]
    assert session.execution_spec["working_dir"] is None
    assert seen[0].get("working_dir") is None


def test_cancellation_intent_retires_claim_before_executor_side_effect(tmp_path: Path):
    child = {
        "id": "child02",
        "description": "Synthetic cancelling child",
        "status": "in_progress",
        "tags": ["local", "agent-running"],
        "fields": {"parent_id": "parent01"},
        "parent_id": "parent01",
        "parent_cancellation_pending": True,
    }
    seen: list[dict] = []

    class CaptureExecutor:
        def execute(self, _session, task):
            seen.append(task)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run")

    def handler(request: httpx.Request):
        if request.method == "GET" and request.url.path == "/api/tasks/child02":
            return httpx.Response(200, json=child)
        return httpx.Response(200, json={})

    worker = _worker(tmp_path, handler, local_executor=CaptureExecutor())
    session = worker.session_store.create(task_id="child02", status="claimed", routing="local")
    worker._dispatch(child)

    assert seen == []
    assert worker.session_store.get("child02").status == "failed"
    assert any(
        event["kind"] == "claim_retired_before_dispatch"
        for event in worker.transcript_store.read(session.session_id)
    )


def test_worker_can_finish_an_already_claimed_project_child(tmp_path: Path, monkeypatch):
    from api.routes import tasks as tasks_route

    sessions = SessionStore(tmp_path / "api-sessions.db")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda task_id, status, tags: sessions.has_live_session(
            task_id, status=status, tags=tags,
        ),
    )
    parent = manager.create("Synthetic worker project", tags=["local"])
    child = manager.create(
        "Synthetic worker child",
        tags=["local"],
        fields={"parent_id": parent.id},
    )
    assert manager.claim_for_agent(
        child.id,
        pickup_tags={"local"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (True, False)

    monkeypatch.setattr(tasks_route, "get_task_manager", lambda: manager)
    monkeypatch.setattr(tasks_route, "_session_store", sessions)
    worker = _worker(
        tmp_path,
        lambda _request: httpx.Response(404),
    )
    worker._http.close()
    worker._http = TestClient(app)

    assert worker._swap_tag(child.id, "agent-running", "agent-completed") is True
    assert "agent-completed" in manager.get(child.id).tags
