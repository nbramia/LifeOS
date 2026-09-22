"""Attested project-owner review/completion (`lifeos_agent_project_owner`).

Covers:
  * The attestation/authorization matrix: an exact-turn agent-owned
    project's owner session can accept/reject a review-pending child of its
    own project, or complete its own project even while its own turn is
    live; every other caller is refused with a stable error code.
  * `api/services/board_review.py`'s `accept_review`/`reject_review` — the
    logic shared with the operator's board routes.
  * The completion guard's owner-turn exemption
    (`TaskManager._guard_project_update`'s `owner_turn_completion_session_id`)
    is scoped to exactly the attested caller, never any other live session.
  * Merge-on-accept and the `integration_unmerged` completion gate
    (`api/services/agent_worker/inter_agent.py`'s `_merge_child_pr_into_
    integration_branch`/`_integration_unmerged_error`), against a stubbed
    `gh` seam -- no network, no real git worktree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services import agent_board
from api.services.agent_worker import git_worktree
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, dispatch
from api.services.agent_worker.session_store import STATUS_CLAIMED, STATUS_COMPLETED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.board_review import BoardReviewError, REVIEW_ACCEPTED_BY_FIELD, accept_review, reject_review
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    CANCEL_OPERATION_FIELD,
    COORDINATOR_SESSION_FIELD,
    INTEGRATION_BRANCH_FIELD,
    ProjectConflictError,
    ProjectTaskService,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path):
    manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    return manager, store, transcripts


def _begin_turn(store: SessionStore, task_id: str, *, routing: str = "claude_code"):
    """Create a live (RUNNING) session for `task_id` and return it, with
    `attempt_id`/`turn_id` set — the same pattern `test_agent_project_
    handoff.py`'s `handoff` fixture uses to mint an exact live turn."""
    session = store.create(task_id, status=STATUS_CLAIMED, routing=routing)
    session = store.begin_executor_turn(task_id, "execute", session=session)
    assert store.mark_executor_turn_running(task_id, session.attempt_id, session.turn_id)
    return store.get(task_id)


def _make_project(manager: TaskManager, store: SessionStore, *, tags=("claude",)) -> tuple:
    """An agent-owned, in-progress project with a live attested owner turn
    and one review-pending child of its own. Returns
    (project_id, child_id, owner_session)."""
    project = manager.create("Synthetic owner project", tags=list(tags), status="in_progress")
    child = manager.create(
        "Synthetic owner child", tags=["codex", "agent-completed"], status="done",
        fields={"parent_id": project.id},
    )
    store.create(task_id=child.id, status=STATUS_COMPLETED, routing="codex")
    owner = _begin_turn(store, f"owner-task-{project.id}")
    manager.update(
        project.id, fields={COORDINATOR_SESSION_FIELD: owner.session_id}, _project_action=True,
    )
    return project.id, child.id, owner


def _set_integration_branch(manager: TaskManager, project_id: str, branch: str) -> None:
    manager.update(
        project_id, fields={INTEGRATION_BRANCH_FIELD: branch}, _project_action=True,
    )


def _record_pr(store: SessionStore, child_id: str, pr_url: str) -> None:
    """Record `child_id`'s completion outcome carrying `pr_url` — the same
    `card_outcomes` row `_merge_child_pr_into_integration_branch`/
    `_integration_unmerged_error` read a child's recorded pull request
    from."""
    store.record_card_outcome(
        child_id, session_id="synthetic-session", engine_label="codex",
        summary="Synthetic completion.", branch="feat/synthetic-child", pr_urls=[pr_url],
    )


def _ctx(store: SessionStore, transcripts: TranscriptStore, manager: TaskManager, caller) -> InterAgentContext:
    return InterAgentContext(
        session_store=store, transcript_store=transcripts, caller_session_id=caller.session_id,
        caps=Caps(), caller_attempt_id=caller.attempt_id, caller_turn_id=caller.turn_id,
        task_manager=manager,
    )


def _call(ctx: InterAgentContext, **args) -> dict:
    return dispatch(ctx, "lifeos_agent_project_owner", args)


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------

class TestAuthorizationMatrix:
    def test_owner_on_its_own_current_turn_accepts_its_own_review_pending_child(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True
        child = manager.get(child_id)
        assert "accepted" in child.tags
        assert child.status == "done"

    def test_owner_on_its_own_current_turn_rejects_its_own_review_pending_child(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(
            ctx, action="reject_child", project_id=project_id, child_task_id=child_id,
            note="Please add a synthetic edge case.",
        )

        assert result["ok"] is True
        child = manager.get(child_id)
        assert child.status == "in_progress"
        assert "agent-running" in child.tags
        assert "Please add a synthetic edge case." in (child.notes or "")

    def test_different_projects_child_is_refused_not_owner(self, env):
        manager, store, transcripts = env
        project_id, _child_id, owner = _make_project(manager, store)
        other_project_id, other_child_id, _other_owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=other_child_id)

        assert result == {
            "ok": False, "error": "not_owner",
            "message": "child does not belong to this project",
        }

    def test_stale_turn_is_refused(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)
        # A stale/replayed value: the caller's live session row carries a
        # different turn id than this one.
        ctx.caller_turn_id = "stale-turn"

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "stale_turn"

    def test_unknown_caller_session_is_refused_stale_turn(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = InterAgentContext(
            session_store=store, transcript_store=transcripts, caller_session_id="sess-ghost",
            caps=Caps(), caller_attempt_id="a1", caller_turn_id="t1", task_manager=manager,
        )

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "stale_turn"

    def test_non_owner_session_entirely_is_refused_not_owner(self, env):
        manager, store, transcripts = env
        project_id, child_id, _owner = _make_project(manager, store)
        bystander = _begin_turn(store, "bystander-task")
        ctx = _ctx(store, transcripts, manager, bystander)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "not_owner"

    def test_non_review_child_is_refused_not_review(self, env):
        manager, store, transcripts = env
        project_id, _child_id, owner = _make_project(manager, store)
        todo_child = manager.create(
            "Synthetic todo child", tags=["codex"], fields={"parent_id": project_id},
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=todo_child.id)

        assert result == {
            "ok": False, "error": "not_review", "message": "child is not review-pending",
        }

    def test_reject_while_paused_is_refused_paused(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ProjectTaskService(manager, store, transcripts).pause_project(project_id)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(
            ctx, action="reject_child", project_id=project_id, child_task_id=child_id,
            note="Retry.",
        )

        assert result == {"ok": False, "error": "paused", "message": "project is paused"}
        # A paused project does not restrict accept.
        accept_result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)
        assert accept_result["ok"] is True

    def test_finished_project_is_refused_forbidden(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        manager.update(project_id, status="cancelled", _project_operation="cancel")
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "forbidden"

    def test_project_id_not_found_is_refused_not_found(self, env):
        manager, store, transcripts = env
        _project_id, _child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id="nope", child_task_id="also-nope")

        assert result == {"ok": False, "error": "not_found", "message": "project nope not found"}

    def test_missing_action_is_invalid_arg(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="bogus", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "invalid_arg"

    def test_reject_without_note_is_invalid_arg(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="reject_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "invalid_arg"


# ---------------------------------------------------------------------------
# complete_project: allowed on the owner's own live turn
# ---------------------------------------------------------------------------

class TestCompleteProject:
    def test_owner_completes_its_own_project_while_its_own_turn_is_live(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        # Resolve the review so completion's other guards are satisfied.
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is True
        assert manager.get(project_id).status == "done"

    def test_operator_completion_is_still_refused_while_the_same_owner_turn_is_live(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")
        service = ProjectTaskService(manager, store, transcripts)

        with pytest.raises(ProjectConflictError, match="project coordinator is live"):
            service.complete_project(project_id)

    def test_a_different_live_session_never_exempts_completion(self, env):
        """The exemption must name the exact attested caller — a second live
        session (even one that also claims to be an 'owner') must not slip
        through the guard."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")
        impostor = _begin_turn(store, "impostor-task")
        service = ProjectTaskService(manager, store, transcripts)

        with pytest.raises(ProjectConflictError, match="project coordinator is live"):
            service.complete_project(project_id, owner_session=impostor)

    def test_complete_project_still_refuses_an_unresolved_child(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is False
        assert result["error"] == "conflict"
        assert manager.get(project_id).status != "done"

    def test_complete_project_requires_cancelled_children_acknowledgement(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")
        extra_child = manager.create(
            "Synthetic cancelled child", tags=["codex"], fields={"parent_id": project_id},
        )
        manager.update(extra_child.id, status="cancelled")
        ctx = _ctx(store, transcripts, manager, owner)

        refused = _call(ctx, action="complete_project", project_id=project_id)
        assert refused["ok"] is False
        assert refused["error"] == "conflict"

        acked = _call(
            ctx, action="complete_project", project_id=project_id,
            acknowledge_cancelled_children=True,
        )
        assert acked["ok"] is True
        assert manager.get(project_id).status == "done"


# ---------------------------------------------------------------------------
# board_review.accept_review / reject_review — shared logic
# ---------------------------------------------------------------------------

class TestBoardReviewSharedLogic:
    def test_accept_review_is_idempotent_and_preserves_the_original_stamp(self, env):
        manager, _store, _transcripts = env
        task = manager.create("Synthetic review card", tags=["codex", "agent-completed"], status="done")

        first = accept_review(manager, task.id, reviewer="owner:sess-1")
        assert first.task.fields.get("review_accepted_by") == "owner:sess-1"

        second = accept_review(manager, task.id, reviewer="operator")
        assert second.task.updated_at == first.task.updated_at
        assert second.task.fields.get("review_accepted_by") == "owner:sess-1"

    def test_accept_review_refuses_a_non_review_card(self, env):
        manager, _store, _transcripts = env
        task = manager.create("Synthetic todo card", tags=["codex"])

        with pytest.raises(BoardReviewError) as excinfo:
            accept_review(manager, task.id, reviewer="operator")
        assert excinfo.value.code == "not_review"

    def test_reject_review_labels_the_note_for_the_owner_and_the_operator_differently(self, env):
        manager, store, _transcripts = env
        task = manager.create("Synthetic reject card", tags=["codex", "agent-completed"], status="done")
        store.create(task_id=task.id, status=STATUS_COMPLETED, routing="codex")

        result = reject_review(manager, store, task.id, "Fix the edge case.", reviewer="owner:sess-9")

        assert "Project owner note: Fix the edge case." in (result.task.notes or "")
        queued = store.list_answered_unprocessed_questions()
        assert queued[0]["answer"] == "Project owner note: Fix the edge case."

    def test_reject_review_keeps_the_raw_note_as_the_followup_for_the_operator(self, env):
        manager, store, _transcripts = env
        task = manager.create("Synthetic reject card", tags=["codex", "agent-completed"], status="done")
        store.create(task_id=task.id, status=STATUS_COMPLETED, routing="codex")

        result = reject_review(manager, store, task.id, "Fix the edge case.", reviewer="operator")

        assert "Operator note: Fix the edge case." in (result.task.notes or "")
        queued = store.list_answered_unprocessed_questions()
        assert queued[0]["answer"] == "Fix the edge case."

    def test_reject_review_without_a_prior_session_is_refused(self, env):
        manager, store, _transcripts = env
        task = manager.create("Synthetic orphan card", tags=["codex", "agent-completed"], status="done")

        with pytest.raises(BoardReviewError) as excinfo:
            reject_review(manager, store, task.id, "Retry.", reviewer="owner:sess-9")
        assert excinfo.value.code == "no_session"

    def test_reject_review_clears_a_stale_acceptance_stamp(self, env):
        """A card can carry a stale `review_accepted_by` from an earlier
        acceptance round (e.g. reassigned and completed again) without
        currently being accepted. Rejecting it must not leave that stamp
        behind for the new round."""
        manager, store, _transcripts = env
        task = manager.create("Synthetic re-reviewed card", tags=["codex", "agent-completed"], status="done")
        store.create(task_id=task.id, status=STATUS_COMPLETED, routing="codex")
        accept_review(manager, task.id, reviewer="owner:sess-1")
        # Simulate a fresh round landing back in Review while the stale
        # stamp from the first acceptance lingers on the task's fields.
        manager.update(task.id, tags=["codex", "agent-completed"])
        assert manager.get(task.id).fields.get(REVIEW_ACCEPTED_BY_FIELD) == "owner:sess-1"

        result = reject_review(manager, store, task.id, "Retry.", reviewer="operator")

        assert result.task.fields.get(REVIEW_ACCEPTED_BY_FIELD) is None


# ---------------------------------------------------------------------------
# Owner-facing error codes stay within the documented closed set
# ---------------------------------------------------------------------------

class TestOwnerFacingErrorCodesAreDocumented:
    def test_reject_child_with_no_prior_session_maps_to_a_documented_code(self, env):
        """`board_review.reject_review` raises its own granular `no_session`
        code; the tool must fold that onto a documented code
        (`invalid_arg`/`not_found`/`stale_turn`/`not_owner`/`not_review`/
        `paused`/`forbidden`/`conflict`) rather than forward it verbatim."""
        manager, store, transcripts = env
        project_id, _child_id, owner = _make_project(manager, store)
        # A second review-pending child that never got a session row, so
        # reject_review's own lookup finds nothing to resume.
        orphan_child = manager.create(
            "Synthetic orphan child", tags=["codex", "agent-completed"], status="done",
            fields={"parent_id": project_id},
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(
            ctx, action="reject_child", project_id=project_id, child_task_id=orphan_child.id,
            note="Retry.",
        )

        assert result["ok"] is False
        assert result["error"] in {
            "invalid_arg", "not_found", "stale_turn", "not_owner",
            "not_review", "paused", "forbidden", "conflict",
        }
        assert result["error"] != "no_session"


# ---------------------------------------------------------------------------
# Project-state guards the tool enforces before ever touching a child
# ---------------------------------------------------------------------------

class TestProjectStateGuards:
    def test_cancellation_pending_is_refused_forbidden_not_crashed(self, env):
        """Without this guard, `accept_review`'s own task write raises a bare
        `ProjectConflictError` (the child's parent has cancellation pending)
        that `accept_review` does not catch — it would otherwise escape
        `project_owner()` entirely and surface as `dispatch()`'s generic
        `crashed` result instead of a clean refusal."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        manager.update(project_id, fields={CANCEL_OPERATION_FIELD: "op-1"}, _project_action=True)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result == {
            "ok": False, "error": "forbidden",
            "message": "project cancellation or handoff is pending",
        }

    def test_operator_owned_project_is_refused_forbidden(self, env):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store, tags=("me",))
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result == {
            "ok": False, "error": "forbidden", "message": "project is not agent-owned",
        }


# ---------------------------------------------------------------------------
# Merge-on-accept and the integration_unmerged completion gate
# ---------------------------------------------------------------------------

class TestMergeOnAccept:
    def test_accepting_an_integration_targeted_pr_merges_it_first(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        merge_calls = []

        def fake_pr_base_and_state(pr_url, **kwargs):
            return {"baseRefName": "feat/integration-abc123", "state": "OPEN"}, None

        def fake_merge_pull_request(pr_url, **kwargs):
            merge_calls.append(pr_url)
            return True, None

        monkeypatch.setattr(git_worktree, "pr_base_and_state", fake_pr_base_and_state)
        monkeypatch.setattr(git_worktree, "merge_pull_request", fake_merge_pull_request)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True
        assert merge_calls == ["https://github.com/acme/widgets/pull/9"]
        assert manager.get(child_id).status == "done"

    def test_merge_failure_fails_the_call_and_leaves_the_card_unaccepted(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        monkeypatch.setattr(
            git_worktree, "pr_base_and_state",
            lambda pr_url, **kw: ({"baseRefName": "feat/integration-abc123", "state": "OPEN"}, None),
        )
        monkeypatch.setattr(
            git_worktree, "merge_pull_request",
            lambda pr_url, **kw: (False, "merge conflict"),
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is False
        assert result["error"] == "merge_failed"
        assert "merge conflict" in result["message"]
        child = manager.get(child_id)
        # Unmerged and unaccepted: the card is still exactly where it was.
        assert child.status == "done"
        assert "accepted" not in child.tags
        assert agent_board.is_review_pending(child.tags)

    def test_pr_targeting_a_different_branch_is_never_merged(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        monkeypatch.setattr(
            git_worktree, "pr_base_and_state",
            lambda pr_url, **kw: ({"baseRefName": "some/other-branch", "state": "OPEN"}, None),
        )

        def unexpected_merge(pr_url, **kw):
            raise AssertionError("must never merge a PR whose base isn't the integration branch")

        monkeypatch.setattr(git_worktree, "merge_pull_request", unexpected_merge)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True
        assert manager.get(child_id).status == "done"
        assert "accepted" in manager.get(child_id).tags

    def test_merge_pull_request_false_skips_the_merge_entirely(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        def unexpected_call(pr_url, **kw):
            raise AssertionError("merge_pull_request=false must skip the base-branch check entirely")

        monkeypatch.setattr(git_worktree, "pr_base_and_state", unexpected_call)
        monkeypatch.setattr(git_worktree, "merge_pull_request", unexpected_call)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(
            ctx, action="accept_child", project_id=project_id, child_task_id=child_id,
            merge_pull_request=False,
        )

        assert result["ok"] is True

    def test_no_integration_branch_never_attempts_a_merge(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        def unexpected_call(pr_url, **kw):
            raise AssertionError("a project with no integration branch has nothing to merge into")

        monkeypatch.setattr(git_worktree, "pr_base_and_state", unexpected_call)
        monkeypatch.setattr(git_worktree, "merge_pull_request", unexpected_call)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True

    def test_already_merged_pr_is_not_merged_again(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        monkeypatch.setattr(
            git_worktree, "pr_base_and_state",
            lambda pr_url, **kw: ({"baseRefName": "feat/integration-abc123", "state": "MERGED"}, None),
        )

        def unexpected_merge(pr_url, **kw):
            raise AssertionError("an already-merged PR must not be merged again")

        monkeypatch.setattr(git_worktree, "merge_pull_request", unexpected_merge)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True

    def test_closed_unmerged_pr_is_treated_as_nothing_to_merge(self, env, monkeypatch):
        """`gh` would refuse to merge a closed PR -- treating that as
        `merge_failed` would make a default `accept_child` call
        permanently unable to accept the card unless the owner remembered
        `merge_pull_request=false` every time."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        monkeypatch.setattr(
            git_worktree, "pr_base_and_state",
            lambda pr_url, **kw: ({"baseRefName": "feat/integration-abc123", "state": "CLOSED"}, None),
        )

        def unexpected_merge(pr_url, **kw):
            raise AssertionError("a closed, unmerged PR must never be merged")

        monkeypatch.setattr(git_worktree, "merge_pull_request", unexpected_merge)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True
        assert manager.get(child_id).status == "done"
        assert "accepted" in manager.get(child_id).tags

    def test_a_successful_merge_is_recorded_independent_of_the_accept_outcome(
        self, env, monkeypatch,
    ):
        """A merge that lands must leave a durable transcript record even
        when the accept step right after it somehow fails -- the record of
        the pull request actually being merged must never live only in the
        return value of a call whose accept half then failed."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")

        monkeypatch.setattr(
            git_worktree, "pr_base_and_state",
            lambda pr_url, **kw: ({"baseRefName": "feat/integration-abc123", "state": "OPEN"}, None),
        )
        monkeypatch.setattr(git_worktree, "merge_pull_request", lambda pr_url, **kw: (True, None))
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="accept_child", project_id=project_id, child_task_id=child_id)

        assert result["ok"] is True
        events = [e for e in transcripts.iter_events(owner.session_id) if e.get("kind") == "project_owner_merge"]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["project_id"] == project_id
        assert payload["child_task_id"] == child_id
        assert payload["pr_url"] == "https://github.com/acme/widgets/pull/9"
        assert payload["integration_branch"] == "feat/integration-abc123"


class TestIntegrationUnmergedCompletionGate:
    def test_refuses_completion_while_the_integration_branch_is_ahead(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        monkeypatch.setattr(git_worktree, "repo_default_branch", lambda slug, **kw: ("main", None))
        monkeypatch.setattr(git_worktree, "repo_branch_exists", lambda slug, branch, **kw: (True, None))
        monkeypatch.setattr(
            git_worktree, "repo_compare_ahead_by", lambda slug, base, head, **kw: (3, None),
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is False
        assert result["error"] == "integration_unmerged"
        assert "feat/integration-abc123" in result["message"]
        assert manager.get(project_id).status != "done"

    def test_completes_once_the_integration_branch_is_fully_merged(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        monkeypatch.setattr(git_worktree, "repo_default_branch", lambda slug, **kw: ("main", None))
        monkeypatch.setattr(git_worktree, "repo_branch_exists", lambda slug, branch, **kw: (True, None))
        monkeypatch.setattr(
            git_worktree, "repo_compare_ahead_by", lambda slug, base, head, **kw: (0, None),
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is True
        assert manager.get(project_id).status == "done"

    def test_completes_once_the_integration_branch_has_been_deleted(self, env, monkeypatch):
        """This repository's documented merge process deletes the source
        branch once it's merged (`scripts/candidate_publisher.py`'s
        `delete_source`). A branch confirmed gone is the terminal "fully
        merged" state, not a check failure -- otherwise the moment the
        owner does exactly what the guidance tells it to, completion would
        refuse forever with an unrecoverable `integration_unmerged`."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        monkeypatch.setattr(git_worktree, "repo_default_branch", lambda slug, **kw: ("main", None))
        monkeypatch.setattr(git_worktree, "repo_branch_exists", lambda slug, branch, **kw: (False, None))

        def unexpected_compare(*args, **kwargs):
            raise AssertionError("a confirmed-deleted branch has nothing left to compare")

        monkeypatch.setattr(git_worktree, "repo_compare_ahead_by", unexpected_compare)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is True
        assert manager.get(project_id).status == "done"

    def test_no_coding_children_skips_the_gate_entirely(self, env, monkeypatch):
        """An integration branch with no recorded child pull request yet
        (nothing was ever merged onto it) has nothing for the gate to
        check against -- it must never block completion."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        def unexpected_call(*args, **kwargs):
            raise AssertionError("no coding-child PR on record -- the gate must not call gh at all")

        monkeypatch.setattr(git_worktree, "repo_default_branch", unexpected_call)
        monkeypatch.setattr(git_worktree, "repo_branch_exists", unexpected_call)
        monkeypatch.setattr(git_worktree, "repo_compare_ahead_by", unexpected_call)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is True
        assert manager.get(project_id).status == "done"

    def test_missing_gh_fails_closed_with_integration_unmerged(self, env, monkeypatch):
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        monkeypatch.setattr(
            git_worktree, "repo_default_branch",
            lambda slug, **kw: (None, "gh: command not found"),
        )
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is False
        assert result["error"] == "integration_unmerged"
        assert manager.get(project_id).status != "done"

    def test_network_error_confirming_branch_existence_still_refuses(self, env, monkeypatch):
        """The default branch resolves fine (the repository exists), but
        the branch-existence probe itself fails (a network blip, not a
        confirmed 404) -- this must still refuse, never quietly treat an
        unconfirmed check as "the branch is gone"."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        _set_integration_branch(manager, project_id, "feat/integration-abc123")
        _record_pr(store, child_id, "https://github.com/acme/widgets/pull/9")
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        monkeypatch.setattr(git_worktree, "repo_default_branch", lambda slug, **kw: ("main", None))
        monkeypatch.setattr(
            git_worktree, "repo_branch_exists",
            lambda slug, branch, **kw: (None, "connection reset by peer"),
        )

        def unexpected_compare(*args, **kwargs):
            raise AssertionError("an unconfirmed existence check must not fall through to compare")

        monkeypatch.setattr(git_worktree, "repo_compare_ahead_by", unexpected_compare)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is False
        assert result["error"] == "integration_unmerged"
        assert manager.get(project_id).status != "done"

    def test_no_integration_branch_skips_the_gate(self, env, monkeypatch):
        """A project with no recorded integration branch at all is
        unaffected by this gate."""
        manager, store, transcripts = env
        project_id, child_id, owner = _make_project(manager, store)
        accept_review(manager, child_id, reviewer=f"owner:{owner.session_id}")

        def unexpected_call(*args, **kwargs):
            raise AssertionError("a project with no integration branch has nothing to check")

        monkeypatch.setattr(git_worktree, "repo_default_branch", unexpected_call)
        monkeypatch.setattr(git_worktree, "repo_branch_exists", unexpected_call)
        monkeypatch.setattr(git_worktree, "repo_compare_ahead_by", unexpected_call)
        ctx = _ctx(store, transcripts, manager, owner)

        result = _call(ctx, action="complete_project", project_id=project_id)

        assert result["ok"] is True
