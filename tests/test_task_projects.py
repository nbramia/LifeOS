"""Project hierarchy and lifecycle regression coverage."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

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
    manager: TaskManager, stores,
):
    sessions, transcripts = stores
    service = ProjectTaskService(manager, sessions, transcripts)
    parent = manager.create(
        "Synthetic release project",
        tags=["codex"],
        notes="Acceptance: synthetic build passes.",
        fields={"model": "gpt-synthetic", "effort": "high", "host": "api"},
    )
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
        "working_dir": None,
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
