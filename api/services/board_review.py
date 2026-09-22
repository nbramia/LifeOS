"""Shared accept/reject logic for review-pending board cards.

Both the operator's board routes (`api/routes/agents.py`) and the project
owner's attested inter-agent tool (`lifeos_agent_project_owner`, see
`api/services/agent_worker/inter_agent.py`) accept and reject a
review-pending card through the functions here — `reviewer="operator"` for
the routes, `reviewer="owner:<session_id>"` for the tool — so the two paths
can never drift apart. Neither function does authorization: the caller
decides whether `reviewer` is allowed to act on the given card before
calling in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api.services import agent_board

if TYPE_CHECKING:
    from api.services.agent_worker.session_store import SessionStore
    from api.services.task_manager import Task, TaskManager

logger = logging.getLogger(__name__)

# Stamped on a card's `fields` by `accept_review`. Distinguishes an owner
# acceptance (`owner:<session_id>`) from an operator one (`operator`) — see
# `web/agents/board.js` `cardMetaHtml` for where the board surfaces it.
REVIEW_ACCEPTED_BY_FIELD = "review_accepted_by"


class BoardReviewError(Exception):
    """Raised when accept/reject cannot proceed. `code` is a stable,
    caller-facing identifier; `message` is the human-readable detail."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ReviewResult:
    task: "Task"
    followup_id: int | None = None


def _reviewer_label(reviewer: str) -> str:
    return "Project owner" if reviewer.startswith("owner:") else "Operator"


def accept_review(manager: "TaskManager", card_id: str, *, reviewer: str) -> ReviewResult:
    """Move a Review card to Done, stamping who accepted it.

    Idempotent — calling this on an already-accepted, already-done card is a
    no-op (matching the board's existing Accept route behavior); the stamp
    from the original acceptance is left untouched.
    """
    from api.services.task_manager import TaskConflictError

    task = manager.get(card_id)
    if task is None:
        raise BoardReviewError("not_found", "card not found")

    tags_norm = {t.lstrip("#").lower() for t in task.tags}
    already_accepted = agent_board.ACCEPTED_TAG in tags_norm
    if agent_board.natural_lane(task.status, task.tags) != "review" and not already_accepted:
        raise BoardReviewError("not_review", "card is not in the Review lane")

    needs_tag = not already_accepted
    needs_status = task.status != "done"
    if needs_tag or needs_status:
        def add_accepted(tags: list[str]) -> list[str]:
            normalized_latest = {str(tag).lstrip("#").lower() for tag in tags}
            if agent_board.ACCEPTED_TAG in normalized_latest:
                return list(tags)
            if not agent_board.is_review_pending(tags):
                raise TaskConflictError("card changed; refresh before accepting")
            return [*tags, agent_board.ACCEPTED_TAG]

        try:
            task = manager.update(
                card_id, status="done", _tags_merge=add_accepted,
                fields={
                    agent_board.SNOOZED_UNTIL_FIELD: None,
                    REVIEW_ACCEPTED_BY_FIELD: reviewer,
                },
            )
        except TaskConflictError as exc:
            raise BoardReviewError("conflict", str(exc)) from exc
        if task is None:
            raise BoardReviewError("not_found", "card not found")
    return ReviewResult(task=task)


def reject_review(
    manager: "TaskManager", session_store: "SessionStore", card_id: str, note: str,
    *, reviewer: str,
) -> ReviewResult:
    """Reject a Review card: return it to worker-owned execution and resume
    the prior session with `note` as a follow-up turn.

    The card's notes and the resumed session's follow-up message are both
    prefixed with the reviewer's label ("Operator" or "Project owner") so
    either read makes clear who sent the note.
    """
    from api.services.agent_worker.session_store import TERMINAL_STATUSES
    from api.services.task_manager import TaskConflictError

    note = (note or "").strip()
    if not note:
        raise BoardReviewError("invalid_arg", "note is required")

    task = manager.get(card_id)
    if task is None:
        raise BoardReviewError("not_found", "card not found")

    plan = agent_board.plan_review_action(task.status, task.tags, "reject", None)
    if plan.error is not None:
        _status_code, detail = plan.error
        raise BoardReviewError("not_review", detail)

    session = session_store.get(card_id)
    if session is None:
        raise BoardReviewError("no_session", "the prior agent session cannot be resumed")
    if session.status not in TERMINAL_STATUSES:
        raise BoardReviewError("session_running", "the prior agent session is still running")
    if session.routing == "hermes" and not session.conversation_id:
        raise BoardReviewError(
            "hermes_conversation_missing",
            "Hermes cannot continue this review because its conversation id is "
            "missing; reassign it to retry with a fresh context",
        )

    label = _reviewer_label(reviewer)
    note_addition = f"{label} note: {note}"
    # The resumed session's follow-up message stays the raw note for an
    # operator reject (unchanged behavior) and carries the same label prefix
    # for an owner reject, so the child agent can tell the note came from
    # its project owner rather than the operator.
    followup_message = note if label == "Operator" else note_addition

    old_status = task.status
    old_tags = list(task.tags)
    action_owned_tags = {
        agent_board.RUNNING_TAG, agent_board.BLOCKED_TAG,
        agent_board.COMPLETED_TAG, "agent-failed",
        "agent-budget-exceeded", agent_board.REASSIGNED_TAG,
        agent_board.ACCEPTED_TAG, *agent_board.AGENT_EXECUTOR_TAGS,
    }
    transition_version: str | None = None

    def merge_review_tags(current_tags: list[str]) -> list[str]:
        if not agent_board.is_review_pending(current_tags):
            raise TaskConflictError("card changed; refresh before applying this review action")
        tags = [str(t) for t in current_tags]
        lifecycle = {
            agent_board.RUNNING_TAG, agent_board.BLOCKED_TAG,
            agent_board.COMPLETED_TAG, "agent-failed",
            "agent-budget-exceeded", agent_board.REASSIGNED_TAG,
            agent_board.ACCEPTED_TAG,
        }
        cleaned = [t for t in tags if t.lstrip("#").lower() not in lifecycle]
        cleaned.append(agent_board.RUNNING_TAG)
        return cleaned

    def merge_note(current_notes: str) -> str:
        return f"{current_notes}\n\n{note_addition}" if current_notes else note_addition

    def restore_card() -> bool:
        if transition_version is None:
            return False

        def merge_rollback_tags(current_tags: list[str]) -> list[str]:
            restored = [
                tag for tag in old_tags
                if str(tag).lstrip("#").lower() in action_owned_tags
            ]
            return [
                tag for tag in current_tags
                if str(tag).lstrip("#").lower() not in action_owned_tags
            ] + restored

        def merge_rollback_notes(current_notes: str) -> str:
            if current_notes == note_addition:
                return ""
            suffix = f"\n\n{note_addition}"
            if current_notes.endswith(suffix):
                return current_notes[: -len(suffix)]
            raise TaskConflictError("review rollback note conflict")

        try:
            manager.update(
                card_id, status=old_status, _tags_merge=merge_rollback_tags,
                _notes_merge=merge_rollback_notes, _expected_updated_at=transition_version,
            )
            return True
        except TaskConflictError:
            logger.warning("review action rollback conflict for %s", card_id)
            return False
        except Exception:  # noqa: BLE001 — teardown must never raise past rollback
            logger.exception("review action rollback failed for %s", card_id)
            return False

    try:
        updated = manager.update(
            card_id, status=plan.status, _tags_merge=merge_review_tags,
            _notes_merge=merge_note, fields={
                agent_board.SNOOZED_UNTIL_FIELD: None,
                REVIEW_ACCEPTED_BY_FIELD: None,
            },
        )
    except (TaskConflictError, ValueError) as exc:
        raise BoardReviewError(
            "conflict" if isinstance(exc, TaskConflictError) else "invalid_arg", str(exc),
        ) from exc
    if updated is None:
        raise BoardReviewError("not_found", "card not found")
    transition_version = updated.updated_at

    followup_id: int | None = None
    try:
        followup_id = session_store.enqueue_web_followup(session.session_id, card_id, followup_message)
    except Exception as exc:  # noqa: BLE001 — translated into a BoardReviewError below
        if followup_id is not None:
            session_store.delete_pending_question(followup_id)
        rolled_back = restore_card()
        detail = f"could not queue review continuation: {type(exc).__name__}"
        if not rolled_back:
            detail += "; rollback conflict — card changed, refresh before retrying"
        raise BoardReviewError("followup_failed", detail) from exc

    return ReviewResult(task=updated, followup_id=followup_id)
