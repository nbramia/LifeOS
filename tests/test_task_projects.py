"""Project hierarchy and lifecycle regression coverage."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.agent_worker.session_store import SessionStore, STATUS_CLAIMED
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    ProjectConflictError,
    ProjectTaskService,
    build_task_hierarchy,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def manager(tmp_path: Path) -> TaskManager:
    return TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda _task_id, _status, _tags: False,
    )


@pytest.fixture
def stores(tmp_path: Path):
    return (
        SessionStore(tmp_path / "sessions.db"),
        TranscriptStore(tmp_path / "transcripts"),
    )


def test_first_child_attachment_pauses_parent_and_round_trips(manager: TaskManager):
    parent = manager.create("Synthetic launch project", tags=["codex"])
    child = manager.create(
        "Draft synthetic launch notes",
        context="Work",
        fields={"parent_id": parent.id},
    )

    assert manager.get(parent.id).fields["execution_paused"] == "true"
    rebuilt = TaskManager(
        vault_path=manager.vault_path,
        index_path=manager.index_path,
        live_session_checker=lambda _task_id, _status, _tags: False,
    )
    hierarchy = build_task_hierarchy(rebuilt.list_tasks())
    assert hierarchy.is_project(parent.id)
    assert hierarchy.children(parent.id)[0].id == child.id
    assert hierarchy.entry(child.id).parent_title == "Synthetic launch project"


@pytest.mark.parametrize(
    "relationship",
    ["missing", "self", "nested"],
)
def test_invalid_api_relationships_are_rejected(manager: TaskManager, relationship: str):
    parent = manager.create("Synthetic parent")
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    if relationship == "missing":
        with pytest.raises(ValueError, match="parent task .* does not exist"):
            manager.update(child.id, fields={"parent_id": "not-real"})
    elif relationship == "self":
        with pytest.raises(ValueError, match="cannot be its own parent"):
            manager.update(child.id, fields={"parent_id": child.id})
    else:
        with pytest.raises(ValueError, match="one level"):
            manager.create("Synthetic grandchild", fields={"parent_id": child.id})


def test_external_dangling_link_stays_visible_and_blocks_claim(manager: TaskManager):
    task = manager.create("Synthetic externally edited task", tags=["codex"])
    manager.update(task.id, fields={"parent_id": "missing00"}, _skip_project_validation=True)

    hierarchy = build_task_hierarchy(manager.list_tasks())
    assert hierarchy.entry(task.id).valid is False
    assert hierarchy.entry(task.id).error == "missing_parent"
    assert manager.claim_for_agent(
        task.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (False, False)


def test_observed_direct_vault_attachment_repairs_parent_pause(manager: TaskManager):
    parent = manager.create("Synthetic parent", tags=["codex"])
    child = manager.create("Synthetic externally attached child")
    path = Path(child.source_file)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            f"<!-- id:{child.id} -->",
            f"[parent_id:: {parent.id}] <!-- id:{child.id} -->",
        ),
        encoding="utf-8",
    )

    manager.reindex_file(str(path))

    assert build_task_hierarchy(manager.list_tasks()).is_project(parent.id)
    assert manager.get(parent.id).fields["execution_paused"] == "true"


def test_pause_repair_lock_order_and_reentry_stay_bounded(tmp_path: Path, monkeypatch):
    """Pause repair cannot invert the writer locks or recurse on CAS conflicts."""
    import api.services.task_manager as task_manager_module
    from api.services.operation_lock import exclusive_operation_lock

    vault = tmp_path / "vault"
    seed = TaskManager(
        vault_path=vault,
        index_path=tmp_path / "seed-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = seed.create("Synthetic observed project", tags=["codex"])
    seed.create("Synthetic observed child", fields={"parent_id": parent.id})
    source = Path(seed.get(parent.id).source_file)

    manager = TaskManager(
        vault_path=vault,
        index_path=tmp_path / "repair-index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace("[execution_paused:: true] ", ""),
        encoding="utf-8",
    )

    real_mtime = task_manager_module._mtime_or_none
    probes = {"count": 0}

    def conflicting_mtime(path):
        probes["count"] += 1
        value = real_mtime(path)
        if probes["count"] % 2 == 0:
            return (value or 0) + probes["count"]
        return value

    monkeypatch.setattr(task_manager_module, "_mtime_or_none", conflicting_mtime)

    repair_lock_attempted = threading.Event()
    real_lock = manager._lock

    class ObservedRLock:
        def __enter__(self):
            if threading.current_thread().name == "synthetic-rebuilder":
                repair_lock_attempted.set()
            real_lock.acquire()
            return self

        def __exit__(self, *_args):
            real_lock.release()

    manager._lock = ObservedRLock()
    holding_lock = threading.Event()
    release_writer = threading.Event()
    writer_done = threading.Event()
    reindex_calls = {"count": 0}
    real_reindex = manager.reindex_file

    def observed_reindex(path):
        reindex_calls["count"] += 1
        return real_reindex(path)

    manager.reindex_file = observed_reindex

    def writer():
        with manager._lock:
            holding_lock.set()
            release_writer.wait(timeout=5)
            with exclusive_operation_lock(
                manager.index_path.parent / ".task-operation.lock"
            ):
                pass
        writer_done.set()

    writer_thread = threading.Thread(target=writer, name="synthetic-writer", daemon=True)
    writer_thread.start()
    assert holding_lock.wait(timeout=2)

    rebuild_thread = threading.Thread(
        target=manager.rebuild_index,
        name="synthetic-rebuilder",
        daemon=True,
    )
    rebuild_thread.start()
    assert repair_lock_attempted.wait(timeout=2)
    release_writer.set()

    writer_thread.join(timeout=3)
    rebuild_thread.join(timeout=3)
    assert writer_done.is_set()
    assert not writer_thread.is_alive()
    assert not rebuild_thread.is_alive()
    assert reindex_calls["count"] == task_manager_module._CAS_MAX_RETRIES


def test_swap_tag_api_refuses_lifecycle_claim_on_project(manager: TaskManager, monkeypatch):
    from api.routes import tasks as tasks_route

    parent = manager.create("Synthetic guarded project", tags=["codex"])
    manager.create("Synthetic guarded child", fields={"parent_id": parent.id})
    monkeypatch.setattr(tasks_route, "get_task_manager", lambda: manager)

    response = TestClient(app).post(
        f"/api/tasks/{parent.id}/swap-tag",
        params={"from": "codex", "to": "agent-running"},
    )

    assert response.status_code == 409
    assert "lifecycle" in response.json()["detail"]
    assert manager.get(parent.id).tags == ["codex"]


def test_project_and_execution_pause_block_worker_claim(manager: TaskManager):
    parent = manager.create("Synthetic project", tags=["codex"])
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})

    assert manager.claim_for_agent(
        parent.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (False, False)

    manager.update(child.id, fields={"parent_id": None})
    assert not build_task_hierarchy(manager.list_tasks()).is_project(parent.id)
    assert manager.get(parent.id).fields["execution_paused"] == "true"
    assert manager.claim_for_agent(
        parent.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (False, False)
    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.update(parent.id, fields={"execution_paused": None})

    resumed = ProjectTaskService(manager).resume_execution(parent.id)
    assert "execution_paused" not in resumed.fields
    assert manager.claim_for_agent(
        parent.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (True, False)


def test_first_child_attachment_and_claim_are_serialized(tmp_path: Path):
    vault = tmp_path / "vault"
    index = tmp_path / "index" / "tasks.json"
    seed = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    parent = seed.create("Synthetic race project", tags=["codex"])
    attach_manager = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    claim_manager = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def attach():
        barrier.wait()
        try:
            outcome["child"] = attach_manager.create(
                "Synthetic child", fields={"parent_id": parent.id},
            )
        except ProjectConflictError as exc:
            outcome["attach_error"] = str(exc)

    def claim():
        barrier.wait()
        outcome["claim"] = claim_manager.claim_for_agent(
            parent.id,
            pickup_tags={"codex"},
            exclusion_tags=set(),
            eligible_statuses={"todo"},
        )[0]

    threads = [threading.Thread(target=attach), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert ("child" in outcome) != bool(outcome["claim"])
    rebuilt = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    if "child" in outcome:
        assert build_task_hierarchy(rebuilt.list_tasks()).is_project(parent.id)
        assert rebuilt.get(parent.id).fields["execution_paused"] == "true"
    else:
        assert "live run" in outcome["attach_error"]


def test_interactive_open_reservation_blocks_first_child_attachment(manager: TaskManager):
    parent = manager.create("Synthetic interactive task", tags=["codex"])

    reserved = manager.reserve_execution_start(parent.id, seconds=60)
    assert reserved is not None
    with pytest.raises(ProjectConflictError, match="live run"):
        manager.create("Synthetic child", fields={"parent_id": parent.id})

    manager.release_execution_start(parent.id)
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})
    assert child.fields["parent_id"] == parent.id


def test_last_child_detach_refused_during_live_coordinator(manager: TaskManager):
    parent = manager.create("Synthetic project")
    child = manager.create("Synthetic child", fields={"parent_id": parent.id})
    manager.update(
        parent.id,
        fields={"project_coordinator_session_id": "sess_synthetic"},
        _project_action=True,
    )
    manager._live_coordinator_checker = lambda _session_id: True

    with pytest.raises(ProjectConflictError, match="coordinator"):
        manager.update(child.id, fields={"parent_id": None})


def test_first_child_attachment_rejects_live_session_even_before_status_projection(
    manager: TaskManager, stores,
):
    sessions, transcripts = stores
    parent = manager.create("Synthetic live ordinary task", tags=["codex"])
    sessions.create(task_id=parent.id, status="claimed", routing="codex")
    ProjectTaskService(manager, sessions, transcripts)

    with pytest.raises(ProjectConflictError, match="live run"):
        manager.create("Synthetic child", fields={"parent_id": parent.id})


def test_complete_project_requires_resolved_children_and_cancel_ack(manager: TaskManager):
    service = ProjectTaskService(manager)
    parent = manager.create("Synthetic project")
    done = manager.create("Synthetic accepted child", status="done", fields={"parent_id": parent.id})
    open_child = manager.create("Synthetic open child", fields={"parent_id": parent.id})

    with pytest.raises(ProjectConflictError, match="unresolved children"):
        service.complete_project(parent.id)

    manager.update(open_child.id, status="cancelled", _project_action=True)
    with pytest.raises(ProjectConflictError, match="acknowledge"):
        service.complete_project(parent.id)

    completed = service.complete_project(parent.id, acknowledge_cancelled_children=True)
    assert completed.status == "done"
    assert manager.get(done.id).status == "done"
    assert manager.get(open_child.id).status == "cancelled"


def test_raw_parent_cancel_and_delete_are_guarded(manager: TaskManager):
    parent = manager.create("Synthetic project")
    manager.create("Synthetic child", fields={"parent_id": parent.id})

    with pytest.raises(ProjectConflictError, match="project cancel"):
        manager.update(parent.id, status="cancelled")
    with pytest.raises(ProjectConflictError, match="children"):
        manager.delete(parent.id)


def test_plan_and_delegate_is_idempotent_and_links_before_claim(
    manager: TaskManager, stores, monkeypatch, tmp_path: Path,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create(
        "Synthetic release project",
        tags=["codex"],
        notes="Acceptance: synthetic build passes.",
        fields={
            "model": "gpt-synthetic",
            "effort": "high",
            "host": "api",
            "project": "synthetic-release",
        },
    )
    coordinator_dir = tmp_path / "SyntheticRelease"
    coordinator_dir.mkdir()
    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_existing_location_affinity",
        lambda affinity: str(coordinator_dir) if affinity == "synthetic-release" else None,
    )
    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_location_affinity",
        lambda _affinity: pytest.fail("coordinator affinity must not use GitHub catalog lookup"),
    )
    monkeypatch.setattr("api.services.agent_worker.remote_spawn.api_host_name", lambda: "api")
    manager.create("Synthetic implementation", fields={"parent_id": parent.id})
    child = manager.list_children(parent.id)[0]
    sessions.record_card_outcome(
        child.id,
        session_id="sess_child_synthetic",
        engine_label="Codex",
        summary="Synthetic implementation verified.",
    )

    first = service.plan_and_delegate(parent.id, operation_id="op-synthetic-1")
    second = service.plan_and_delegate(parent.id, operation_id="op-synthetic-1")

    assert first["created"] is True
    assert second["created"] is False
    assert first["session_id"] == second["session_id"]
    with pytest.raises(ProjectConflictError, match="coordinator is already live"):
        service.plan_and_delegate(parent.id, operation_id="op-synthetic-2")
    linked = manager.get(parent.id)
    assert linked.fields["project_coordinator_session_id"] == first["session_id"]
    session = sessions.get_by_session_id(first["session_id"])
    assert session.status == STATUS_CLAIMED
    assert session.origin == "operator"
    assert session.execution_request == {
        "executor": "codex",
        "model_id": "gpt-synthetic",
        "effort": "high",
        "host": "api",
        "working_dir": str(coordinator_dir),
        "budget": None,
        "constraints": {
            "allowed_executors": [],
            "required_capabilities": [],
            "allowed_billing": [],
        },
    }
    opening = sessions.peek_pending_messages(session.session_id)[0]["content"]
    assert "Operation ID: op-synthetic-1" in opening
    assert f"- {child.id}: Synthetic implementation" in opening
    assert "status=unassigned" in opening
    assert "assignee=unassigned" in opening
    assert "outcome=Synthetic implementation verified." in opening
    assert "derive one stable operation_key" in opening
    transcripts.append(
        session.session_id,
        "codex_completed",
        {"final_text": "Synthetic coordination result."},
    )
    assert service.coordinator_view(linked)["result"] == "Synthetic coordination result."


def test_plan_and_delegate_records_the_integration_branch(
    manager: TaskManager, stores, monkeypatch, tmp_path: Path,
):
    from api.services.task_projects import INTEGRATION_BRANCH_FIELD, _integration_branch_name

    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic branch-recording project", tags=["codex"])
    manager.create("Synthetic branch-recording child", fields={"parent_id": parent.id})

    service.plan_and_delegate(parent.id, operation_id="op-branch-1")

    linked = manager.get(parent.id)
    assert linked.fields[INTEGRATION_BRANCH_FIELD] == _integration_branch_name(parent)


def test_replan_after_a_prior_coordinator_does_not_record_the_integration_branch(
    manager: TaskManager, stores,
):
    """A re-Plan of a project that already had a coordinator request — an
    owner whose integration-branch field is absent, or whose field the
    operator cleared — must not record the field: only true first-owner
    creation does."""
    from api.services.task_projects import COORDINATOR_REQUEST_FIELD, INTEGRATION_BRANCH_FIELD

    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic pre-existing owner project", tags=["codex"])
    manager.create("Synthetic pre-existing owner child", fields={"parent_id": parent.id})
    manager.update(
        parent.id,
        fields={COORDINATOR_REQUEST_FIELD: "op-prior"},
        _skip_project_validation=True,
    )

    service.plan_and_delegate(parent.id, operation_id="op-replan")

    linked = manager.get(parent.id)
    assert INTEGRATION_BRANCH_FIELD not in linked.fields


def test_plan_and_delegate_does_not_overwrite_an_existing_integration_branch(
    manager: TaskManager, stores,
):
    from api.services.task_projects import INTEGRATION_BRANCH_FIELD

    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic preset-branch project", tags=["codex"])
    manager.create("Synthetic preset-branch child", fields={"parent_id": parent.id})
    manager.update(
        parent.id,
        fields={INTEGRATION_BRANCH_FIELD: "feat/operator-chosen-deadbeef"},
        _skip_project_validation=True,
    )

    service.plan_and_delegate(parent.id, operation_id="op-branch-2")

    linked = manager.get(parent.id)
    assert linked.fields[INTEGRATION_BRANCH_FIELD] == "feat/operator-chosen-deadbeef"


def test_integration_branch_field_is_guarded_from_ordinary_updates(manager: TaskManager):
    from api.services.task_projects import INTEGRATION_BRANCH_FIELD

    task = manager.create("Synthetic guarded task", tags=["codex"])

    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.update(task.id, fields={INTEGRATION_BRANCH_FIELD: "feat/forged-deadbeef"})
    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.create(
            "Synthetic guarded create",
            fields={INTEGRATION_BRANCH_FIELD: "feat/forged-deadbeef"},
        )


def test_coordinator_view_streams_only_the_bounded_transcript_tail(
    manager: TaskManager, stores, monkeypatch,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic bounded-summary project", tags=["codex"])
    manager.create("Synthetic bounded-summary child", fields={"parent_id": parent.id})
    result = service.plan_and_delegate(parent.id, operation_id="bounded-summary")
    linked = manager.get(parent.id)

    transcripts.append(result["session_id"], "old", {"summary": "Too old"})
    for index in range(100):
        transcripts.append(result["session_id"], "noise", {"sequence": index})
    monkeypatch.setattr(
        transcripts,
        "read",
        lambda _session_id: pytest.fail("coordinator summary must use iter_events"),
    )
    assert service.coordinator_view(linked)["result"] is None

    transcripts.append(result["session_id"], "latest", {"summary": "Synthetic latest"})
    assert service.coordinator_view(linked)["result"] == "Synthetic latest"


@pytest.mark.parametrize(
    ("tag", "model"),
    [
        ("cloud-haiku", "claude-haiku-4-5"),
        ("cloud-sonnet", "claude-sonnet-5"),
    ],
)
def test_plan_and_delegate_supports_managed_cloud_aliases(
    manager: TaskManager, stores, tag: str, model: str, monkeypatch,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create(
        "Synthetic managed coordination",
        tags=[tag],
        fields={
            "model": "synthetic-ignored",
            "effort": "high",
            "host": "api",
            "project": "synthetic-managed-affinity",
        },
    )
    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_existing_location_affinity",
        lambda _affinity: pytest.fail("Managed aliases do not accept a working directory"),
    )
    manager.create("Synthetic managed child", fields={"parent_id": parent.id})

    result = service.plan_and_delegate(parent.id, operation_id=f"plan-{tag}")

    session = sessions.get_by_session_id(result["session_id"])
    assert session.routing == "claude"
    assert session.model == model
    assert session.effort == "high"
    assert session.host == "api"
    assert session.execution_request["executor"] == "claude"
    assert session.execution_request["model_id"] is None
    assert session.execution_request["working_dir"] is None


@pytest.mark.parametrize("owner", ["codex", "claude"])
def test_remote_cli_project_coordinator_withholds_api_host_affinity(
    manager: TaskManager, stores, monkeypatch, owner: str,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create(
        "Synthetic remote coordination",
        tags=[owner],
        fields={"host": "studio", "project": "synthetic-repo"},
    )
    manager.create("Synthetic remote child", fields={"parent_id": parent.id})
    monkeypatch.setattr("api.services.agent_worker.remote_spawn.api_host_name", lambda: "api")
    monkeypatch.setattr(
        "api.services.directory_resolver.resolve_existing_location_affinity",
        lambda _affinity: pytest.fail("remote coordinator must not resolve API-host affinity"),
    )

    result = service.plan_and_delegate(parent.id, operation_id="remote-coordinator")

    session = sessions.get_by_session_id(result["session_id"])
    assert session.execution_request["host"] == "studio"
    assert session.execution_request["working_dir"] is None


def test_cancel_preview_and_confirm_abandons_review_and_preserves_done(
    manager: TaskManager, stores,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic project", tags=["me"])
    done = manager.create("Synthetic complete child", status="done", fields={"parent_id": parent.id})
    review = manager.create(
        "Synthetic review child",
        status="done",
        tags=["codex", "agent-completed"],
        fields={"parent_id": parent.id},
    )
    open_child = manager.create("Synthetic human child", fields={"parent_id": parent.id})

    preview = service.cancel_preview(parent.id)
    assert preview["operation_id"] is None
    assert preview["cancellation_pending"] is False
    assert preview["unfinished_count"] == 2
    assert preview["awaiting_review_count"] == 1
    assert manager.get(parent.id).fields.get("project_cancel_operation_id") is None

    result = asyncio.run(service.cancel_project(parent.id, operation_id="cancel-synthetic-1"))
    assert result["complete"] is True
    assert result["pending"] is False
    assert result["abandoned_review_ids"] == [review.id]
    assert manager.get(done.id).status == "done"
    assert manager.get(review.id).status == "cancelled"
    assert "agent-result-abandoned" in manager.get(review.id).tags
    assert manager.get(open_child.id).status == "cancelled"
    assert manager.get(parent.id).status == "cancelled"
    assert "project_cancel_operation_id" not in manager.get(parent.id).fields


def test_partial_cancel_keeps_intent_and_retry_finishes(manager: TaskManager, stores):
    sessions, transcripts = stores
    parent = manager.create("Synthetic project", tags=["codex"])
    child = manager.create("Synthetic running child", tags=["codex"], fields={"parent_id": parent.id})
    session = sessions.create(task_id=child.id, status="running", routing="codex")
    failures = {session.session_id}

    async def stop(candidate):
        if candidate.session_id in failures:
            return [], [{"session_id": candidate.session_id, "reason": "stop on its host"}]
        sessions.update_status(candidate.task_id, "failed")
        return [candidate.session_id], []

    service = ProjectTaskService(manager, sessions, transcripts, session_teardown=stop)
    first = asyncio.run(service.cancel_project(parent.id, operation_id="cancel-synthetic-2"))
    assert first["pending"] is True
    assert manager.get(parent.id).fields["project_cancel_operation_id"] == "cancel-synthetic-2"
    assert manager.get(child.id).status != "cancelled"
    retry_preview = service.cancel_preview(parent.id)
    assert retry_preview["operation_id"] == "cancel-synthetic-2"
    assert retry_preview["cancellation_pending"] is True

    with pytest.raises(ProjectConflictError, match="already pending"):
        asyncio.run(service.cancel_project(parent.id, operation_id="different-operation"))
    with pytest.raises(ProjectConflictError, match="cancellation is pending"):
        service.plan_and_delegate(parent.id, operation_id="plan-during-cancel")

    failures.clear()
    restarted = TaskManager(
        vault_path=manager.vault_path,
        index_path=manager.index_path,
        live_session_checker=lambda task_id, status, tags: sessions.has_live_session(
            task_id, status=status, tags=tags,
        ),
    )
    restarted_service = ProjectTaskService(
        restarted, sessions, transcripts, session_teardown=stop,
    )
    second = asyncio.run(
        restarted_service.cancel_project(parent.id, operation_id="cancel-synthetic-2")
    )
    assert second["complete"] is True
    assert restarted.get(child.id).status == "cancelled"
    assert restarted.get(parent.id).status == "cancelled"


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


def test_pause_blocks_claim_and_resume_reenables_it(manager: TaskManager):
    from api.services.task_projects import (
        PROJECT_PAUSED_AT_FIELD,
        PROJECT_PAUSED_FIELD,
        PROJECT_PAUSE_REASON_FIELD,
    )

    service = ProjectTaskService(manager)
    parent = manager.create("Synthetic pausable project", tags=["codex"])
    manager.create("Synthetic pausable child", tags=["codex"], fields={"parent_id": parent.id})

    paused = service.pause_project(parent.id)
    assert paused.fields[PROJECT_PAUSED_FIELD] == "true"
    assert paused.fields[PROJECT_PAUSE_REASON_FIELD] == "operator"
    assert PROJECT_PAUSED_AT_FIELD in paused.fields

    # The project task itself is never claimable regardless of pause (it's
    # a project, not ordinary work) — that's the pre-existing "is_project"
    # refusal, not the pause-specific one, so it stays the soft (False,
    # False) shape rather than raising.
    assert manager.claim_for_agent(
        parent.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (False, False)
    child = manager.list_children(parent.id)[0]
    with pytest.raises(ProjectConflictError, match="paused"):
        manager.claim_for_agent(
            child.id,
            pickup_tags={"codex"},
            exclusion_tags=set(),
            eligible_statuses={"todo"},
        )
    assert manager.can_start_execution(child.id) is False

    resumed = service.resume_project(parent.id)
    assert PROJECT_PAUSED_FIELD not in resumed.fields
    assert PROJECT_PAUSE_REASON_FIELD not in resumed.fields
    assert manager.claim_for_agent(
        child.id,
        pickup_tags={"codex"},
        exclusion_tags=set(),
        eligible_statuses={"todo"},
    ) == (True, False)


def test_resume_project_gives_the_owner_a_fresh_failure_budget(manager: TaskManager, stores):
    """`consecutive_failures` on `project_owner_state` is worker-owned state
    with no other reset path -- `resume_project` is the one place a pause
    transition is unambiguous, so it forgives a prior owner-failure streak
    there rather than the reconciler guessing a transition happened on
    every later tick (see Worker._reconcile_project_owners)."""
    session_store, _transcript_store = stores
    service = ProjectTaskService(manager, session_store=session_store)
    parent = manager.create("Synthetic owner-failure project", tags=["codex"])
    manager.create("Synthetic owner-failure child", tags=["codex"], fields={"parent_id": parent.id})
    owner = session_store.create(task_id="project_owner_op1", routing="claude_code", status="completed", origin="operator")
    session_store.ensure_project_owner_state(
        parent.id, owner_session_id=owner.session_id, baseline_states={},
    )
    session_store.record_project_owner_wake_failure(parent.id)
    session_store.record_project_owner_wake_failure(parent.id)
    assert session_store.get_project_owner_state(parent.id)["consecutive_failures"] == 2

    service.pause_project(parent.id, reason="owner_failed")
    service.resume_project(parent.id)

    assert session_store.get_project_owner_state(parent.id)["consecutive_failures"] == 0


def test_pause_rejects_an_unrecognized_reason(manager: TaskManager):
    service = ProjectTaskService(manager)
    parent = manager.create("Synthetic reason project")
    manager.create("Synthetic reason child", fields={"parent_id": parent.id})

    with pytest.raises(ValueError, match="invalid pause reason"):
        service.pause_project(parent.id, reason="not_a_real_reason")


def test_pause_and_resume_require_a_project(manager: TaskManager):
    service = ProjectTaskService(manager)
    task = manager.create("Synthetic ordinary task")

    with pytest.raises(ProjectConflictError, match="not a project"):
        service.pause_project(task.id)
    with pytest.raises(ProjectConflictError, match="not a project"):
        service.resume_project(task.id)


def test_pause_and_pause_fields_are_guarded_from_ordinary_updates(manager: TaskManager):
    from api.services.task_projects import PROJECT_PAUSED_FIELD

    task = manager.create("Synthetic guarded pause task")

    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.update(task.id, fields={PROJECT_PAUSED_FIELD: "true"})
    with pytest.raises(ProjectConflictError, match="explicit project lifecycle"):
        manager.create("Synthetic guarded pause create", fields={PROJECT_PAUSED_FIELD: "true"})


def test_plan_and_delegate_is_refused_while_paused(manager: TaskManager, stores):
    from api.services.task_projects import _coordinator_task_id

    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create("Synthetic paused planning project", tags=["codex"])
    manager.create("Synthetic paused planning child", fields={"parent_id": parent.id})
    service.pause_project(parent.id)

    with pytest.raises(ProjectConflictError, match="paused"):
        service.plan_and_delegate(parent.id, operation_id="plan-while-paused")

    # Refused before staging a coordinator session, not merely at the
    # linking write — a paused project should never spin one up at all.
    synthetic_task_id = _coordinator_task_id(parent.id, "plan-while-paused")
    assert sessions.get(synthetic_task_id) is None


def test_a_child_already_mid_turn_still_lands_in_review_while_paused(manager: TaskManager):
    """Pausing a project must not interrupt a child's already-running turn —
    only new claims and Open are refused. The ordinary worker transition into
    Review (status/tags writes with no `_project_action`) still succeeds."""
    service = ProjectTaskService(manager)
    parent = manager.create("Synthetic finish-in-flight project", tags=["codex"])
    child = manager.create(
        "Synthetic running child", tags=["codex", "agent-running"],
        status="in_progress", fields={"parent_id": parent.id},
    )
    service.pause_project(parent.id)

    landed = manager.swap_tag(child.id, "agent-running", "agent-completed")
    assert landed is True
    assert "agent-completed" in manager.get(child.id).tags


def test_pause_and_resume_are_available_while_a_child_is_running(manager: TaskManager):
    """Cancel and operator Complete stay available while paused (design);
    pausing itself must also not be blocked by ordinary running work."""
    service = ProjectTaskService(manager)
    parent = manager.create("Synthetic live-child project", tags=["codex"])
    manager.create(
        "Synthetic live child", tags=["codex", "agent-running"],
        status="in_progress", fields={"parent_id": parent.id},
    )

    paused = service.pause_project(parent.id)
    assert paused.fields["project_paused"] == "true"
    resumed = service.resume_project(parent.id)
    assert "project_paused" not in resumed.fields


def test_pause_races_a_concurrent_claim_under_the_task_operation_lock(tmp_path: Path):
    """Contention test: whichever of pause or claim wins the shared
    `.task-operation.lock` first, the outcome stays consistent — a winning
    claim is never retroactively undone by the losing pause, and a winning
    pause always leaves the claim refused with a 409-mapped error."""
    vault = tmp_path / "vault"
    index = tmp_path / "index" / "tasks.json"
    seed = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    parent = seed.create("Synthetic pause-race project", tags=["codex"])
    child = seed.create("Synthetic pause-race child", fields={"parent_id": parent.id})
    pause_manager = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    claim_manager = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    pause_service = ProjectTaskService(pause_manager)
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def pause():
        barrier.wait()
        pause_service.pause_project(parent.id)

    def claim():
        barrier.wait()
        try:
            outcome["claim"] = claim_manager.claim_for_agent(
                child.id,
                pickup_tags={"codex"},
                exclusion_tags=set(),
                eligible_statuses={"todo"},
            )[0]
        except ProjectConflictError:
            outcome["claim"] = False
            outcome["claim_refused_paused"] = True

    threads = [threading.Thread(target=pause), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    rebuilt = TaskManager(vault_path=vault, index_path=index, live_session_checker=lambda *_: False)
    paused_after = rebuilt.get(parent.id).fields.get("project_paused") == "true"
    assert paused_after is True
    if outcome["claim"]:
        # The claim won the race before the pause took effect — a live claim
        # is never retroactively undone by a pause that lands afterward.
        assert rebuilt.get(child.id).status == "in_progress"
    else:
        # The pause won (or the claim otherwise lost) — the child was never
        # claimed, and a paused-specific loss is reported distinctly.
        assert rebuilt.get(child.id).status == "todo"
