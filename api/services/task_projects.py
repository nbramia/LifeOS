"""Derived task hierarchy and project lifecycle operations.

Projects are ordinary tasks with incoming ``fields.parent_id`` references.
This module never persists a project type or a child list: every view and
guard is rebuilt from the complete task set, while the few durable parent
fields here describe execution state only.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, TYPE_CHECKING

from api.services import agent_board
from api.services.agent_worker.assignment import extract_assignment
from api.services.agent_worker.execution import (
    Budget,
    ExecutionConstraints,
    ExecutionRequest,
    ExecutionSpec,
    parse_legacy_route_alias,
)
from api.services.agent_worker.git_worktree import derive_branch_name
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    Session,
    SessionStore,
    TERMINAL_STATUSES,
)
from api.services.agent_worker.transcript_store import TranscriptStore

if TYPE_CHECKING:
    from api.services.task_manager import Task, TaskManager


PARENT_ID_FIELD = "parent_id"
EXECUTION_PAUSED_FIELD = "execution_paused"
EXECUTION_RESERVATION_FIELD = "execution_reservation_until"
COORDINATOR_SESSION_FIELD = "project_coordinator_session_id"
COORDINATOR_REQUEST_FIELD = "project_coordinator_request_id"
# `request_project_owner_wake`'s `reason` for a Plan call against a project
# that already has a terminal, resumable owner (see `_plan_and_delegate_locked`).
PLAN_OWNER_WAKE_REASON = "plan_requested"
CANCEL_OPERATION_FIELD = "project_cancel_operation_id"
CANCEL_REQUESTED_AT_FIELD = "project_cancel_requested_at"
LAST_CANCEL_OPERATION_FIELD = "project_last_cancel_operation_id"
ABANDONED_AT_FIELD = "project_result_abandoned_at"
ABANDONED_TAG = "agent-result-abandoned"
HANDOFF_OPERATION_FIELD = "project_handoff_operation_id"
HANDOFF_SOURCE_SESSION_FIELD = "project_handoff_source_session_id"
HANDOFF_SOURCE_ATTEMPT_FIELD = "project_handoff_source_attempt_id"
HANDOFF_SOURCE_TURN_FIELD = "project_handoff_source_turn_id"
HANDOFF_REQUEST_HASH_FIELD = "project_handoff_request_hash"
HANDOFF_REQUESTED_AT_FIELD = "project_handoff_requested_at"
HANDOFF_READY_AT_FIELD = "project_handoff_ready_at"
LAST_HANDOFF_OPERATION_FIELD = "project_last_handoff_operation_id"
HANDOFF_ACTIVATED_AT_FIELD = "project_handoff_activated_at"
LAST_ABORTED_HANDOFF_FIELD = "project_last_aborted_handoff_operation_id"
INTEGRATION_BRANCH_FIELD = "project_integration_branch"

# Set/cleared only by `ProjectTaskService.pause_project`/`resume_project` —
# see `internal_fields` in `TaskManager.create` and `_guard_project_update`,
# which refuse an ordinary create/update that touches any of the three.
# While set, `TaskManager._project_claim_allowed` refuses every child's
# worker claim and interactive Open; a running child's current turn is
# unaffected and its result still lands in Review.
PROJECT_PAUSED_FIELD = "project_paused"
PROJECT_PAUSED_AT_FIELD = "project_paused_at"
PROJECT_PAUSE_REASON_FIELD = "project_pause_reason"
PROJECT_PAUSE_REASONS = frozenset({"operator", "owner_failed", "owner_budget"})

# Stamped on a project child created by an agent-attributed request (the
# curated `lifeos_task_create` proxy carrying `X-LifeOS-Agent-Session`) or by
# a handoff. Internal — see `internal_fields` in `TaskManager.create` and
# `_guard_project_update` — so an ordinary create/update can never set or
# clear either field itself; only the create-time stamping paths do.
CHILD_ORIGIN_FIELD = "project_child_origin"
CHILD_ORIGIN_AGENT = "agent"
CHILD_CREATOR_SESSION_FIELD = "project_child_creator_session"

HANDOFF_REQUEST_EVENT = "project_handoff_requested"
HANDOFF_QUIESCENT_EVENT = "project_handoff_quiescent"

# Mirrors FAILED_TAG / BUDGET_EXCEEDED_TAG in
# api/services/agent_worker/worker.py (not imported -- worker.py imports
# from this module, so the reverse import would be circular; the same
# tradeoff `agent_board.py` makes for these same two tags).
_FAILED_TAG = "agent-failed"
_BUDGET_EXCEEDED_TAG = "agent-budget-exceeded"


class ProjectConflictError(ValueError):
    """A project mutation conflicts with current hierarchy/lifecycle state."""


class ProjectHandoffError(ProjectConflictError):
    """Stable machine-readable refusal from the dedicated handoff action."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HierarchyEntry:
    task_id: str
    parent_id: str | None
    parent_title: str | None
    is_project: bool
    child_count: int
    valid: bool
    error: str | None


class TaskHierarchy:
    """Immutable projection over one complete task snapshot."""

    def __init__(self, tasks: Iterable["Task"]):
        self.tasks: dict[str, Task] = {task.id: task for task in tasks}
        self._children: dict[str, list[Task]] = {}
        for task in self.tasks.values():
            parent_id = clean_parent_id(task.fields.get(PARENT_ID_FIELD))
            if parent_id and parent_id in self.tasks and parent_id != task.id:
                self._children.setdefault(parent_id, []).append(task)
        for children in self._children.values():
            children.sort(key=lambda item: (item.created_date, item.id))
        self._entries = {task_id: self._build_entry(task) for task_id, task in self.tasks.items()}

    def _build_entry(self, task: "Task") -> HierarchyEntry:
        parent_id = clean_parent_id(task.fields.get(PARENT_ID_FIELD))
        error: str | None = None
        parent_title: str | None = None
        if parent_id:
            parent = self.tasks.get(parent_id)
            if parent_id == task.id:
                error = "self_parent"
            elif parent is None:
                error = "missing_parent"
            else:
                parent_title = parent.description
                if self._children.get(task.id):
                    error = "nested_project"
                elif clean_parent_id(parent.fields.get(PARENT_ID_FIELD)):
                    error = "nested_parent"
        children = self._children.get(task.id, [])
        if error is None and any(
            clean_parent_id(child.fields.get(PARENT_ID_FIELD)) != task.id
            or self._raw_relationship_error(child)
            for child in children
        ):
            error = "invalid_child"
        return HierarchyEntry(
            task_id=task.id,
            parent_id=parent_id,
            parent_title=parent_title,
            is_project=bool(children),
            child_count=len(children),
            valid=error is None,
            error=error,
        )

    def _raw_relationship_error(self, task: "Task") -> bool:
        parent_id = clean_parent_id(task.fields.get(PARENT_ID_FIELD))
        if not parent_id:
            return False
        parent = self.tasks.get(parent_id)
        return bool(
            parent_id == task.id
            or parent is None
            or self._children.get(task.id)
            or (parent and clean_parent_id(parent.fields.get(PARENT_ID_FIELD)))
        )

    def entry(self, task_id: str) -> HierarchyEntry:
        return self._entries[task_id]

    def is_project(self, task_id: str) -> bool:
        entry = self._entries.get(task_id)
        return bool(entry and entry.is_project)

    def children(self, task_id: str) -> list["Task"]:
        return list(self._children.get(task_id, ()))

    def child_state(self, task: "Task") -> str:
        tags = agent_board.normalize_tags(task.tags)
        if agent_board.is_review_pending(task.tags):
            return "awaiting_review"
        if task.status == "cancelled":
            return "cancelled"
        if task.status == "done":
            return "done"
        if (
            task.status == "blocked"
            or agent_board.BLOCKED_TAG in tags
            or agent_board.HUMAN_TAG in tags
            or bool(tags & agent_board.MACHINE_WAIT_TAGS)
        ):
            return "blocked"
        if task.status == "in_progress" or agent_board.RUNNING_TAG in tags:
            return "running"
        if agent_board.derive_assignee(task.tags) is None:
            return "unassigned"
        return "assigned"

    def project_summary(self, task_id: str, coordinator: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.is_project(task_id):
            return None
        task = self.tasks[task_id]
        counts = {
            "done": 0,
            "awaiting_review": 0,
            "cancelled": 0,
            "blocked": 0,
            "running": 0,
            "unassigned": 0,
            "assigned": 0,
        }
        for child in self.children(task_id):
            counts[self.child_state(child)] += 1
        resolved = counts["done"] + counts["cancelled"]
        return {
            "child_count": sum(counts.values()),
            "counts": counts,
            "resolved_count": resolved,
            "ready_to_close": resolved == sum(counts.values()) and counts["awaiting_review"] == 0,
            "execution_paused": field_truthy(task.fields.get(EXECUTION_PAUSED_FIELD)),
            "cancellation_pending": bool(task.fields.get(CANCEL_OPERATION_FIELD)),
            "handoff_pending": bool(task.fields.get(HANDOFF_OPERATION_FIELD)),
            "paused": field_truthy(task.fields.get(PROJECT_PAUSED_FIELD)),
            "pause_reason": task.fields.get(PROJECT_PAUSE_REASON_FIELD),
            "coordinator": coordinator,
        }

    def read_fields(self, task_id: str, coordinator: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = self.entry(task_id)
        parent = self.tasks.get(entry.parent_id) if entry.parent_id else None
        return {
            "parent_id": entry.parent_id,
            "parent_title": entry.parent_title,
            "is_project": entry.is_project,
            "child_count": entry.child_count,
            "hierarchy_valid": entry.valid,
            "hierarchy_error": entry.error,
            "parent_cancellation_pending": bool(
                parent and parent.fields.get(CANCEL_OPERATION_FIELD)
            ),
            "parent_handoff_pending": bool(
                parent and parent.fields.get(HANDOFF_OPERATION_FIELD)
            ),
            "parent_project_paused": bool(
                parent and field_truthy(parent.fields.get(PROJECT_PAUSED_FIELD))
            ),
            "project": self.project_summary(task_id, coordinator),
        }


def clean_parent_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def field_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def field_timestamp_future(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        observed = datetime.fromisoformat(value.strip())
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed > datetime.now(timezone.utc)
    except ValueError:
        # A malformed reservation was not created by the API. Fail closed
        # until the operator repairs the visible inline field.
        return True


def build_task_hierarchy(tasks: Iterable["Task"]) -> TaskHierarchy:
    return TaskHierarchy(tasks)


def owner_state(task: dict[str, Any]) -> str:
    """Classify one project child's state for the persistent-owner wake
    reconciler (`Worker._reconcile_project_owners`), from the raw dict
    shape the worker's `/api/tasks` fetch returns -- not a `Task` object,
    see `TaskHierarchy.child_state` for the board's equivalent
    classification over `Task` objects.

    Six states, checked in priority order the same way the board derives a
    lane (`agent_board.natural_lane`): `awaiting_review` (worker-completed,
    not yet accepted -- wins even over a terminal status) beats `failed`
    (`agent-failed`/`agent-budget-exceeded` -- both leave the task's own
    status at `cancelled`, so this must be checked before the plain
    `cancelled` case below) beats `cancelled` beats `done` beats `blocked`
    (needs a human) beats `active` (everything else: unassigned, assigned,
    genuinely running, or a machine wait -- `agent-wait-provider`/
    `agent-wait-dependency` are worker-owned and self-clearing;
    `natural_lane` routes them to In progress, not Human queue, and an
    owner wake would just burn a paid turn on a transient provider
    rate-limit). Only a non-`active` state is ever an owner-wake event.
    """
    raw_tags = task.get("tags") or []
    tags = agent_board.normalize_tags(raw_tags)
    status = str(task.get("status") or "todo").lower()
    if agent_board.is_review_pending(raw_tags):
        return "awaiting_review"
    if _FAILED_TAG in tags or _BUDGET_EXCEEDED_TAG in tags:
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "done":
        return "done"
    if (
        status == "blocked"
        or agent_board.BLOCKED_TAG in tags
        or agent_board.HUMAN_TAG in tags
    ):
        return "blocked"
    return "active"


def validate_parent_change(
    hierarchy: TaskHierarchy,
    child_id: str,
    new_parent_id: str | None,
    *,
    has_live_session: Callable[["Task"], bool] | None = None,
    has_live_coordinator: Callable[["Task"], bool] | None = None,
) -> None:
    """Validate attach/detach/reparent against a complete snapshot."""
    child = hierarchy.tasks.get(child_id)
    if child is None:
        raise ValueError(f"task {child_id} does not exist")
    old_parent_id = clean_parent_id(child.fields.get(PARENT_ID_FIELD))
    new_parent_id = clean_parent_id(new_parent_id)
    # A not-yet-persisted task is included in the proposed hierarchy with its
    # requested field already populated, so equal values still need the full
    # create-time validation. Persisted no-op updates may return immediately.
    if old_parent_id == new_parent_id and child.source_file:
        return

    if old_parent_id:
        old_parent = hierarchy.tasks.get(old_parent_id)
        old_children = hierarchy.children(old_parent_id) if old_parent else []
        if old_parent:
            if old_parent.fields.get(CANCEL_OPERATION_FIELD):
                raise ProjectConflictError("cannot change membership while project cancellation is pending")
            if old_parent.fields.get(HANDOFF_OPERATION_FIELD):
                raise ProjectConflictError("cannot change membership while project handoff is pending")
        if old_parent and len(old_children) == 1:
            if has_live_coordinator and has_live_coordinator(old_parent):
                raise ProjectConflictError("cannot remove the last child while the project coordinator is live")

    if new_parent_id is None:
        return
    if new_parent_id == child_id:
        raise ValueError("a task cannot be its own parent")
    parent = hierarchy.tasks.get(new_parent_id)
    if parent is None:
        raise ValueError(f"parent task {new_parent_id} does not exist")
    if hierarchy.children(child_id) or clean_parent_id(parent.fields.get(PARENT_ID_FIELD)):
        raise ValueError("task hierarchy supports one level; a project cannot be attached beneath another task")
    if parent.status in {"done", "cancelled"}:
        raise ProjectConflictError("reopen the parent before adding or moving child work")
    if parent.fields.get(CANCEL_OPERATION_FIELD):
        raise ProjectConflictError("project cancellation is pending")
    if parent.fields.get(HANDOFF_OPERATION_FIELD):
        raise ProjectConflictError("project handoff is pending")
    if agent_board.is_review_pending(parent.tags):
        raise ProjectConflictError("accept or reject the parent review before attaching child work")
    parent_live = bool(has_live_session and has_live_session(parent))
    if parent_live or agent_board.is_claimed(parent.status, parent.tags, parent_live):
        raise ProjectConflictError("stop or resolve the parent's live run before attaching child work")


def coordinator_is_live(task: "Task", checker: Callable[[str], bool] | None) -> bool:
    session_id = task.fields.get(COORDINATOR_SESSION_FIELD)
    return bool(session_id and checker and checker(session_id))


class ProjectTaskService:
    """Shared project actions for HTTP and in-process agent tools."""

    def __init__(
        self,
        manager: "TaskManager",
        session_store: SessionStore | None = None,
        transcript_store: TranscriptStore | None = None,
        *,
        session_teardown: Callable[[Any], Awaitable[tuple[list[str], list[dict[str, str]]]]] | None = None,
    ) -> None:
        self.manager = manager
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._session_teardown = session_teardown
        if session_store is not None:
            def live_task_session(task_id: str, _status: str, _tags: list[str]) -> bool:
                session = session_store.get(task_id)
                if session is not None and session.status not in TERMINAL_STATUSES:
                    return True
                return any(
                    cli.status != "ended"
                    for cli in session_store.list_cli_sessions_for_task(task_id)
                )

            manager._live_session_checker = live_task_session
            manager._live_coordinator_checker = self._session_id_is_live

    def hierarchy(self) -> TaskHierarchy:
        return build_task_hierarchy(self.manager.list_tasks())

    def coordinator_view(self, task: "Task") -> dict[str, Any] | None:
        session_id = task.fields.get(COORDINATOR_SESSION_FIELD)
        if not session_id or self.session_store is None:
            return None
        session = self.session_store.get_by_session_id(session_id)
        if session is None:
            return {"session_id": session_id, "status": "missing", "live": False, "result": None}
        result = None
        if self.transcript_store is not None:
            events = deque(self.transcript_store.iter_events(session_id), maxlen=100)
            for event in reversed(events):
                payload = event.get("payload") or {}
                candidate = (
                    payload.get("result")
                    or payload.get("summary")
                    or payload.get("message")
                    or payload.get("final_text")
                    or payload.get("text")
                    or payload.get("content")
                    or payload.get("reason")
                    or payload.get("error")
                )
                if isinstance(candidate, str) and candidate.strip():
                    result = candidate.strip()[:2000]
                    break
        return {
            "session_id": session_id,
            "status": session.status,
            "live": session.status not in TERMINAL_STATUSES,
            "result": result,
        }

    def read_fields(self, task_id: str) -> dict[str, Any]:
        task = self.manager.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return self.hierarchy().read_fields(task_id, self.coordinator_view(task))

    def start_project(self, task_id: str) -> "Task":
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if not hierarchy.is_project(task_id):
            raise ProjectConflictError("task is not a project")
        if task.fields.get(CANCEL_OPERATION_FIELD):
            raise ProjectConflictError("project cancellation is pending")
        if task.fields.get(HANDOFF_OPERATION_FIELD):
            raise ProjectConflictError("project handoff is pending")
        if self._task_coordinator_live(task):
            raise ProjectConflictError("project coordinator is already live")
        if task.status in {"done", "cancelled"}:
            raise ProjectConflictError("reopen the project before starting it")
        def precondition(current: "Task") -> None:
            self._require_project_current(current)
            if current.fields.get(CANCEL_OPERATION_FIELD):
                raise ProjectConflictError("project cancellation is pending")
            if self._task_coordinator_live(current):
                raise ProjectConflictError("project coordinator is already live")

        return self.manager.update(
            task_id,
            status="in_progress",
            _project_operation="start",
            _precondition=precondition,
        )

    def complete_project(
        self, task_id: str, *, acknowledge_cancelled_children: bool = False,
    ) -> "Task":
        return self.manager.update(
            task_id,
            status="done",
            _acknowledge_cancelled_children=acknowledge_cancelled_children,
            _precondition=self._require_project_current,
        )

    def resume_execution(self, task_id: str) -> "Task":
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if hierarchy.is_project(task_id):
            raise ProjectConflictError("project objectives cannot resume as ordinary task execution")
        if task.fields.get(CANCEL_OPERATION_FIELD):
            raise ProjectConflictError("project cancellation is pending")
        status = "todo" if task.status == "in_progress" else task.status
        def precondition(current: "Task") -> None:
            if self.hierarchy().is_project(current.id):
                raise ProjectConflictError("project objectives cannot resume as ordinary task execution")
            if current.fields.get(CANCEL_OPERATION_FIELD):
                raise ProjectConflictError("project cancellation is pending")

        return self.manager.update(
            task_id,
            status=status,
            fields={EXECUTION_PAUSED_FIELD: None},
            _project_operation="resume",
            _precondition=precondition,
        )

    def pause_project(self, task_id: str, *, reason: str = "operator") -> "Task":
        """Set the durable paused state on a project.

        Blocks every child's worker claim and interactive Open (see
        `TaskManager._project_claim_allowed`) without touching a child's
        current run — an already-running turn finishes and its result still
        lands in Review. Idempotent: pausing an already-paused project just
        refreshes its reason and timestamp.
        """
        if reason not in PROJECT_PAUSE_REASONS:
            raise ValueError(f"invalid pause reason: {reason!r}")
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        self._require_project_current(task)

        def precondition(current: "Task") -> None:
            self._require_project_current(current)

        updated = self.manager.update(
            task_id,
            fields={
                PROJECT_PAUSED_FIELD: "true",
                PROJECT_PAUSED_AT_FIELD: datetime.now(timezone.utc).isoformat(),
                PROJECT_PAUSE_REASON_FIELD: reason,
            },
            _project_operation="pause",
            _precondition=precondition,
        )
        if updated is None:
            raise KeyError(task_id)
        return updated

    def resume_project(self, task_id: str) -> "Task":
        """Clear the durable paused state, re-enabling child claims and Open.

        Also gives the project's persistent owner (if any) a fresh
        consecutive-failure budget: `project_owner_state.consecutive_failures`
        is worker-owned state with no other reset path, and forgiving it
        here -- at the one moment a pause transition is unambiguous -- is
        simpler than having the reconciler guess a transition happened on
        every subsequent tick. A harmless no-op for a project paused for an
        unrelated ("operator") reason, where the count is normally already
        zero.
        """
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        self._require_project_current(task)

        def precondition(current: "Task") -> None:
            self._require_project_current(current)

        updated = self.manager.update(
            task_id,
            fields={
                PROJECT_PAUSED_FIELD: None,
                PROJECT_PAUSED_AT_FIELD: None,
                PROJECT_PAUSE_REASON_FIELD: None,
            },
            _project_operation="resume-project",
            _precondition=precondition,
        )
        if updated is None:
            raise KeyError(task_id)
        if self.session_store is not None:
            self.session_store.reset_project_owner_failures(task_id)
        return updated

    def stage_handoff(
        self,
        source: Session,
        *,
        operation_id: str,
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Stage an ordinary owning turn as a fenced durable project.

        The transcript request is the bounded source payload; Markdown keeps
        only the identity/hash pointers needed to fail closed and recover.
        """
        if self.session_store is None or self.transcript_store is None:
            raise RuntimeError("session and transcript stores are required for project handoff")
        operation_id = (operation_id or "").strip()
        if not operation_id:
            raise ProjectHandoffError("invalid_arg", "operation_id is required")
        normalized = {"operation_id": operation_id, "children": children}
        request_hash = _handoff_request_hash(normalized)
        with self.manager.project_operation():
            return self._stage_handoff_locked(
                source,
                operation_id=operation_id,
                normalized=normalized,
                request_hash=request_hash,
            )

    def _stage_handoff_locked(
        self,
        source: Session,
        *,
        operation_id: str,
        normalized: dict[str, Any],
        request_hash: str,
    ) -> dict[str, Any]:
        task = self.manager.get(source.task_id)
        if task is None:
            raise ProjectHandoffError("forbidden", "source task does not exist")

        event = self._handoff_request_event(source.session_id, operation_id)
        event_hash = ((event or {}).get("payload") or {}).get("request_hash")
        if event_hash and event_hash != request_hash:
            raise ProjectHandoffError(
                "operation_mismatch", "operation_id was already used with different content",
            )

        last_operation = task.fields.get(LAST_HANDOFF_OPERATION_FIELD)
        if last_operation == operation_id:
            if event_hash != request_hash:
                raise ProjectHandoffError(
                    "operation_mismatch", "activated handoff request no longer matches its transcript",
                )
            return self._handoff_result(task, operation_id, normalized, state="activated")

        pending_operation = task.fields.get(HANDOFF_OPERATION_FIELD)
        if pending_operation:
            if pending_operation != operation_id:
                raise ProjectHandoffError(
                    "handoff_pending", f"another handoff is pending as {pending_operation}",
                )
            if task.fields.get(HANDOFF_REQUEST_HASH_FIELD) != request_hash:
                raise ProjectHandoffError(
                    "operation_mismatch", "operation_id was already used with different content",
                )
        else:
            self._validate_handoff_source(task, source)
            hierarchy = self.hierarchy()
            if hierarchy.is_project(task.id):
                raise ProjectHandoffError(
                    "already_project", "existing projects use the project Plan action",
                )
            if event is None:
                self.transcript_store.append(source.session_id, HANDOFF_REQUEST_EVENT, {
                    "project_id": task.id,
                    "operation_id": operation_id,
                    "source_attempt_id": source.attempt_id,
                    "source_turn_id": source.turn_id,
                    "request_hash": request_hash,
                    "request": normalized,
                })
            requested_at = datetime.now(timezone.utc).isoformat()

            def intent_precondition(current: "Task") -> None:
                self._validate_handoff_source(current, source)
                if self.hierarchy().is_project(current.id):
                    raise ProjectHandoffError(
                        "already_project", "existing projects use the project Plan action",
                    )
                if current.fields.get(HANDOFF_OPERATION_FIELD):
                    raise ProjectHandoffError("handoff_pending", "project handoff is already pending")

            task = self.manager.update(
                task.id,
                status="in_progress",
                fields={
                    EXECUTION_PAUSED_FIELD: "true",
                    HANDOFF_OPERATION_FIELD: operation_id,
                    HANDOFF_SOURCE_SESSION_FIELD: source.session_id,
                    HANDOFF_SOURCE_ATTEMPT_FIELD: source.attempt_id,
                    HANDOFF_SOURCE_TURN_FIELD: source.turn_id,
                    HANDOFF_REQUEST_HASH_FIELD: request_hash,
                    HANDOFF_REQUESTED_AT_FIELD: requested_at,
                },
                _project_operation="handoff-stage",
                _precondition=intent_precondition,
            )
            if task is None:
                raise ProjectHandoffError("forbidden", "source task disappeared during handoff")

        created_children: list[dict[str, Any]] = []
        for child in normalized["children"]:
            fields = {PARENT_ID_FIELD: task.id}
            execution = child.get("execution") or {}
            for request_key, field_key in (
                ("model_id", "model"),
                ("effort", "effort"),
                ("host", "host"),
                ("working_dir", "working_dir"),
            ):
                value = execution.get(request_key)
                if value is not None:
                    fields[field_key] = value
            tags = [child["assignee"]] if child.get("assignee") else []
            operation_key = _handoff_child_operation_key(task.id, operation_id, child["key"])
            child_task, created = self.manager.create_or_find_by_operation(
                operation_key,
                description=child["description"],
                context="Inbox",
                status="todo",
                tags=tags,
                notes=child.get("notes"),
                fields=fields,
                _project_handoff_operation=operation_id,
                _project_child_creator_session=source.session_id,
            )
            if (
                child_task.fields.get(PARENT_ID_FIELD) != task.id
                or child_task.description != child["description"]
                or child_task.context != "Inbox"
                or (child_task.notes or "") != (child.get("notes") or "")
                or agent_board.normalize_tags(child_task.tags) != agent_board.normalize_tags(tags)
                or any(child_task.fields.get(key) != value for key, value in fields.items())
            ):
                raise ProjectHandoffError(
                    "corrupt_state", f"staged child {child['key']} does not match its durable request",
                )
            created_children.append({
                "key": child["key"], "task_id": child_task.id, "created": created,
            })

        coordinator = self._stage_handoff_coordinator(task, source, operation_id, normalized)
        task = self.manager.update(
            task.id,
            fields={
                COORDINATOR_SESSION_FIELD: coordinator.session_id,
                COORDINATOR_REQUEST_FIELD: operation_id,
                HANDOFF_READY_AT_FIELD: datetime.now(timezone.utc).isoformat(),
            },
            _project_operation="handoff-stage",
            _precondition=lambda current: self._require_matching_handoff(
                current, source, operation_id, request_hash,
            ),
        )
        if task is None:
            raise ProjectHandoffError("corrupt_state", "handoff parent disappeared")
        self.transcript_store.append(source.session_id, "project_handoff_staged", {
            "project_id": task.id,
            "operation_id": operation_id,
            "child_task_ids": [item["task_id"] for item in created_children],
            "coordinator_session_id": coordinator.session_id,
        })
        return {
            "ok": True,
            "state": "staged",
            "project_id": task.id,
            "operation_id": operation_id,
            "source_session_id": source.session_id,
            "child_tasks": created_children,
            "coordinator_session_id": coordinator.session_id,
            "runnable": False,
            "stop_now": True,
        }

    def finalize_handoff(
        self,
        task_id: str,
        *,
        operation_id: str,
        source_session_id: str,
        source_attempt_id: str,
        source_turn_id: str,
    ) -> dict[str, Any]:
        """Release a staged handoff after exact-turn quiescence is durable."""
        if self.session_store is None or self.transcript_store is None:
            raise RuntimeError("session and transcript stores are required for project handoff")
        with self.manager.project_operation():
            task = self.manager.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.fields.get(LAST_HANDOFF_OPERATION_FIELD) == operation_id:
                return self._repair_activated_handoff(task, operation_id)
            source = self.session_store.get_by_session_id(source_session_id)
            if source is None:
                raise ProjectHandoffError("pending_quiescence", "source session is missing")
            self._require_matching_handoff(
                task, source, operation_id, task.fields.get(HANDOFF_REQUEST_HASH_FIELD) or "",
                expected_attempt=source_attempt_id, expected_turn=source_turn_id,
            )
            if self.session_store.is_cancelled(
                task.id, source_attempt_id, source_turn_id,
            ):
                raise ProjectHandoffError(
                    "cancelled", "source turn cancellation owns this transition",
                )
            if task.fields.get(CANCEL_OPERATION_FIELD):
                raise ProjectHandoffError("cancelled", "project cancellation owns this transition")
            request_event = self._handoff_request_event(source_session_id, operation_id)
            request_payload = (request_event or {}).get("payload") or {}
            normalized = request_payload.get("request")
            request_hash = request_payload.get("request_hash")
            if (
                not isinstance(normalized, dict)
                or request_hash != task.fields.get(HANDOFF_REQUEST_HASH_FIELD)
                or _handoff_request_hash(normalized) != request_hash
            ):
                raise ProjectHandoffError(
                    "corrupt_state", "handoff request transcript is missing or corrupt",
                )
            if source.status not in TERMINAL_STATUSES:
                raise ProjectHandoffError("pending_quiescence", "source session is not terminal")
            if not self._has_handoff_quiescence(
                source_session_id, operation_id, source_attempt_id, source_turn_id,
            ):
                raise ProjectHandoffError("pending_quiescence", "source turn has no quiescence proof")
            if not task.fields.get(HANDOFF_READY_AT_FIELD):
                self._stage_handoff_locked(
                    source,
                    operation_id=operation_id,
                    normalized=normalized,
                    request_hash=request_hash,
                )
                task = self.manager.get(task_id)
                if task is None or not task.fields.get(HANDOFF_READY_AT_FIELD):
                    raise ProjectHandoffError("not_ready", "handoff staging is incomplete")
            staged = self._validate_staged_handoff(task, normalized, operation_id)

            owner = _owner_tag_for_session(source, task)

            def finalized_tags(tags: list[str]) -> list[str]:
                stripped = {
                    agent_board.RUNNING_TAG,
                    agent_board.BLOCKED_TAG,
                }
                result = [tag for tag in tags if tag.lstrip("#").lower() not in stripped]
                normalized_tags = agent_board.normalize_tags(result)
                if owner and not normalized_tags.intersection(
                    {*agent_board.AGENT_EXECUTOR_TAGS, "me"}
                ):
                    result.append(owner)
                return result

            cleared = {
                HANDOFF_OPERATION_FIELD: None,
                HANDOFF_SOURCE_SESSION_FIELD: None,
                HANDOFF_SOURCE_ATTEMPT_FIELD: None,
                HANDOFF_SOURCE_TURN_FIELD: None,
                HANDOFF_REQUEST_HASH_FIELD: None,
                HANDOFF_REQUESTED_AT_FIELD: None,
                HANDOFF_READY_AT_FIELD: None,
                LAST_HANDOFF_OPERATION_FIELD: operation_id,
                HANDOFF_ACTIVATED_AT_FIELD: datetime.now(timezone.utc).isoformat(),
            }
            if not task.fields.get(INTEGRATION_BRANCH_FIELD):
                # Handoff finalize is inherently first-owner creation:
                # record the project's deterministic integration branch.
                cleared[INTEGRATION_BRANCH_FIELD] = _integration_branch_name(task)
            task = self.manager.update(
                task.id,
                status="in_progress",
                fields=cleared,
                _tags_merge=finalized_tags,
                _project_operation="handoff-finalize",
                _precondition=lambda current: self._require_matching_handoff(
                    current, source, operation_id, request_hash,
                    expected_attempt=source_attempt_id, expected_turn=source_turn_id,
                ),
            )
            if task is None:
                raise ProjectHandoffError("corrupt_state", "handoff parent disappeared")
            coordinator = staged["coordinator"]
            if coordinator.status == STATUS_BLOCKED:
                changed = self.session_store.update_status(
                    coordinator.task_id,
                    STATUS_CLAIMED,
                    attempt_id=coordinator.attempt_id,
                    turn_id=coordinator.turn_id,
                )
                if not changed:
                    raise ProjectHandoffError(
                        "coordinator_unavailable", "coordinator could not be released",
                    )
            self.transcript_store.append(source_session_id, "project_handoff_activated", {
                "project_id": task.id,
                "operation_id": operation_id,
                "coordinator_session_id": coordinator.session_id,
            })
            return self._handoff_result(task, operation_id, normalized, state="activated")

    def reconcile_handoffs(self) -> dict[str, Any]:
        """Retry only already-quiescent intents; unproved runtimes stay pending."""
        activated: list[str] = []
        pending: list[dict[str, str]] = []
        for task in self.manager.list_tasks():
            operation_id = task.fields.get(HANDOFF_OPERATION_FIELD)
            if not operation_id:
                continue
            try:
                result = self.finalize_handoff(
                    task.id,
                    operation_id=operation_id,
                    source_session_id=task.fields.get(HANDOFF_SOURCE_SESSION_FIELD, ""),
                    source_attempt_id=task.fields.get(HANDOFF_SOURCE_ATTEMPT_FIELD, ""),
                    source_turn_id=task.fields.get(HANDOFF_SOURCE_TURN_FIELD, ""),
                )
                if result.get("state") == "activated":
                    activated.append(task.id)
            except ProjectHandoffError as exc:
                pending.append({"project_id": task.id, "code": exc.code, "message": str(exc)})
        return {"activated_project_ids": activated, "pending": pending}

    def _validate_handoff_source(self, task: "Task", source: Session) -> None:
        hierarchy = self.hierarchy()
        entry = hierarchy.entry(task.id)
        tags = agent_board.normalize_tags(task.tags)
        if entry.parent_id or entry.is_project:
            code = "already_project" if entry.is_project else "not_ordinary_parent"
            raise ProjectHandoffError(code, "handoff requires an ordinary top-level task")
        if not entry.valid or task.status in {"done", "cancelled"}:
            raise ProjectHandoffError("not_ordinary_parent", "source task is not an active ordinary task")
        if agent_board.is_review_pending(task.tags) or task.fields.get(CANCEL_OPERATION_FIELD):
            raise ProjectHandoffError("forbidden", "source task is in review or cancellation")
        if task.status != "in_progress" or agent_board.RUNNING_TAG not in tags:
            raise ProjectHandoffError("forbidden", "source task is not owned by a live agent run")
        if (
            source.task_id != task.id
            or source.parent_session_id is not None
            or (source.root_session_id or source.session_id) != source.session_id
            or source.origin == "operator"
            or source.status not in {"running", "claimed"}
            or not source.attempt_id
            or not source.turn_id
            or not self.session_store.is_current_turn(task.id, source.attempt_id, source.turn_id)
        ):
            raise ProjectHandoffError("stale_turn", "caller does not own the current source turn")

    def _require_matching_handoff(
        self,
        task: "Task",
        source: Session,
        operation_id: str,
        request_hash: str,
        *,
        expected_attempt: str | None = None,
        expected_turn: str | None = None,
    ) -> None:
        attempt_id = expected_attempt or source.attempt_id
        turn_id = expected_turn or source.turn_id
        expected = {
            HANDOFF_OPERATION_FIELD: operation_id,
            HANDOFF_SOURCE_SESSION_FIELD: source.session_id,
            HANDOFF_SOURCE_ATTEMPT_FIELD: attempt_id,
            HANDOFF_SOURCE_TURN_FIELD: turn_id,
            HANDOFF_REQUEST_HASH_FIELD: request_hash,
        }
        if any(task.fields.get(key) != value for key, value in expected.items()):
            raise ProjectHandoffError("stale_turn", "handoff identity no longer matches")
        current = self.session_store.get_by_session_id(source.session_id)
        if (
            current is None
            or current.task_id != task.id
            or current.attempt_id != attempt_id
            or current.turn_id != turn_id
        ):
            raise ProjectHandoffError("stale_turn", "source attempt or turn is no longer current")

    def _stage_handoff_coordinator(
        self,
        task: "Task",
        source: Session,
        operation_id: str,
        normalized: dict[str, Any],
    ) -> Session:
        synthetic_task_id = _coordinator_task_id(task.id, operation_id)
        source_spec = (
            ExecutionSpec.from_dict(source.execution_spec)
            if source.execution_spec else None
        )
        if source_spec is None:
            raise ProjectHandoffError(
                "missing_execution_snapshot", "source execution snapshot is unavailable",
            )
        request = ExecutionRequest(
            executor=source_spec.executor,
            model_id=source_spec.model_id,
            effort=source_spec.effort,
            host=source_spec.host,
            working_dir=source_spec.working_dir,
            constraints=ExecutionConstraints(
                allowed_executors=source_spec.constraints.allowed_executors,
                required_capabilities=source_spec.constraints.required_capabilities,
                allowed_billing=source_spec.constraints.allowed_billing,
            ),
        )
        session = self.session_store.get(synthetic_task_id)
        if session is None:
            from api.services.agent_worker.operator_spawn import create_operator_session
            try:
                result = create_operator_session(
                    self.session_store,
                    self._handoff_coordination_prompt(task, operation_id, normalized),
                    explicit_routing=source_spec.executor,
                    execution_request=request,
                    task_id=synthetic_task_id,
                    dispatch_ready=False,
                )
            except sqlite3.IntegrityError:
                result = {"ok": True, "session_id": None}
            if not result.get("ok"):
                raise ProjectHandoffError(
                    "coordinator_unavailable",
                    result.get("error", "could not create handoff coordinator"),
                )
            session = self.session_store.get(synthetic_task_id)
        if session is None:
            raise ProjectHandoffError("coordinator_unavailable", "coordinator session is missing")
        if session.execution_spec is None:
            from dataclasses import asdict, replace

            coordinator_budget = Budget(**(session.budget or {})) if session.budget else None
            coordinator_spec = replace(
                source_spec,
                parent_session_id=None,
                root_session_id=session.session_id,
                budget=coordinator_budget,
            )
            self.session_store.set_execution_snapshot(
                session.task_id,
                request=asdict(request),
                spec=coordinator_spec.to_dict(),
            )
            session = self.session_store.get_by_session_id(session.session_id)
            if session is None:
                raise ProjectHandoffError(
                    "coordinator_unavailable", "coordinator session disappeared",
                )
        if session.status != STATUS_BLOCKED:
            raise ProjectHandoffError("corrupt_state", "coordinator has an invalid staged status")
        return session

    def _validate_staged_handoff(
        self, task: "Task", normalized: dict[str, Any], operation_id: str,
    ) -> dict[str, Any]:
        hierarchy = self.hierarchy()
        expected: dict[str, "Task"] = {}
        for child in normalized.get("children") or []:
            operation_key = _handoff_child_operation_key(task.id, operation_id, child["key"])
            found = self.manager.find_by_operation(operation_key)
            if found is None or clean_parent_id(found.fields.get(PARENT_ID_FIELD)) != task.id:
                raise ProjectHandoffError("not_ready", f"staged child {child['key']} is missing")
            expected[found.id] = found
        actual = hierarchy.children(task.id)
        if len(actual) != len(expected) or any(child.id not in expected for child in actual):
            raise ProjectHandoffError("corrupt_state", "staged project membership does not match request")
        coordinator_id = task.fields.get(COORDINATOR_SESSION_FIELD)
        coordinator = self.session_store.get_by_session_id(coordinator_id or "")
        if coordinator is None or coordinator.task_id != _coordinator_task_id(task.id, operation_id):
            raise ProjectHandoffError("not_ready", "staged coordinator is missing")
        if coordinator.status != STATUS_BLOCKED:
            raise ProjectHandoffError("corrupt_state", "staged coordinator status is invalid")
        return {"children": list(expected.values()), "coordinator": coordinator}

    def _repair_activated_handoff(self, task: "Task", operation_id: str) -> dict[str, Any]:
        event = self._handoff_request_event(
            task.fields.get(HANDOFF_SOURCE_SESSION_FIELD, "")
            or self._source_session_for_operation(task.id, operation_id),
            operation_id,
        )
        normalized = ((event or {}).get("payload") or {}).get("request") or {
            "operation_id": operation_id, "children": [],
        }
        coordinator_id = task.fields.get(COORDINATOR_SESSION_FIELD)
        coordinator = self.session_store.get_by_session_id(coordinator_id or "")
        if coordinator is not None and coordinator.status == STATUS_BLOCKED:
            self.session_store.update_status(
                coordinator.task_id, STATUS_CLAIMED,
                attempt_id=coordinator.attempt_id, turn_id=coordinator.turn_id,
            )
        return self._handoff_result(task, operation_id, normalized, state="activated")

    def _handoff_result(
        self, task: "Task", operation_id: str, normalized: dict[str, Any], *, state: str,
    ) -> dict[str, Any]:
        child_tasks = []
        for child in normalized.get("children") or []:
            found = self.manager.find_by_operation(
                _handoff_child_operation_key(task.id, operation_id, child["key"]),
            )
            if found:
                child_tasks.append({"key": child["key"], "task_id": found.id, "created": False})
        return {
            "ok": True,
            "state": state,
            "project_id": task.id,
            "operation_id": operation_id,
            "source_session_id": task.fields.get(HANDOFF_SOURCE_SESSION_FIELD)
            or self._source_session_for_operation(task.id, operation_id),
            "child_tasks": child_tasks,
            "coordinator_session_id": task.fields.get(COORDINATOR_SESSION_FIELD),
            "runnable": state == "activated",
            "stop_now": True,
        }

    def _handoff_request_event(self, session_id: str, operation_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        for event in reversed(self.transcript_store.read(session_id)):
            payload = event.get("payload") or {}
            if event.get("kind") == HANDOFF_REQUEST_EVENT and payload.get("operation_id") == operation_id:
                return event
        return None

    def _source_session_for_operation(self, task_id: str, operation_id: str) -> str:
        for session in self.session_store.list_sessions(limit=None):
            for event in reversed(self.transcript_store.read(session.session_id)):
                payload = event.get("payload") or {}
                if (
                    event.get("kind") == HANDOFF_REQUEST_EVENT
                    and payload.get("project_id") == task_id
                    and payload.get("operation_id") == operation_id
                ):
                    return session.session_id
        return ""

    def _has_handoff_quiescence(
        self, session_id: str, operation_id: str, attempt_id: str, turn_id: str,
    ) -> bool:
        for event in reversed(self.transcript_store.read(session_id)):
            payload = event.get("payload") or {}
            if event.get("kind") == HANDOFF_QUIESCENT_EVENT and (
                payload.get("operation_id") == operation_id
                and payload.get("attempt_id") == attempt_id
                and payload.get("turn_id") == turn_id
            ):
                return True
        return False

    @staticmethod
    def _handoff_coordination_prompt(
        task: "Task", operation_id: str, normalized: dict[str, Any],
    ) -> str:
        lines = [
            "Coordinate this LifeOS project using its already-created durable task children.",
            f"Project ID: {task.id}",
            f"Operation ID: {operation_id}",
            f"Objective: {task.description}",
            f"Notes/acceptance criteria:\n{(task.notes or '').strip()[:6000] or '(none)'}",
            "Children:",
        ]
        for child in normalized.get("children") or []:
            lines.append(
                f"- {child['key']}: {child['description']} | "
                f"assignee={child.get('assignee') or 'unassigned'}"
            )
        lines.append(
            "You are this project's persistent owner: you are woken automatically "
            "when a child's state changes -- newly blocked, failed, done, cancelled, "
            "or awaiting review -- with several such events batched into one wake. "
            "Inspect child state, help resolve scoped blockers, and use the explicit "
            "project completion/cancellation actions."
        )
        return "\n".join(lines)

    def plan_and_delegate(self, task_id: str, *, operation_id: str) -> dict[str, Any]:
        # Session staging plus parent linkage is one short shared operation.
        # The session stays BLOCKED until linked, so a crash is recoverable;
        # serializing deterministic same-ID retries also avoids duplicate
        # pending prompts before SessionStore's unique task key decides a
        # winner.
        with self.manager.project_operation():
            return self._plan_and_delegate_locked(task_id, operation_id=operation_id)

    def _plan_and_delegate_locked(
        self, task_id: str, *, operation_id: str,
    ) -> dict[str, Any]:
        operation_id = (operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        if self.session_store is None:
            raise RuntimeError("session store is required for project coordination")
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if not hierarchy.is_project(task_id):
            raise ProjectConflictError("task is not a project")
        if task.fields.get(CANCEL_OPERATION_FIELD):
            raise ProjectConflictError("project cancellation is pending")
        if field_truthy(task.fields.get(PROJECT_PAUSED_FIELD)):
            raise ProjectConflictError("project is paused")
        owner = agent_board.derive_assignee(task.tags)
        normalized_tags = agent_board.normalize_tags(task.tags)
        if owner is None:
            owner = next(
                (
                    tag
                    for tag in agent_board.MANAGED_AGENT_ASSIGNEES
                    if tag in normalized_tags
                ),
                None,
            )
        route = parse_legacy_route_alias(f"#{owner}" if owner else "")
        if not route.recognized:
            raise ProjectConflictError("Plan and delegate requires an agent-owned project")
        executor = route.request.executor
        assignment = extract_assignment(task.fields)
        managed_consent = owner in agent_board.MANAGED_AGENT_ASSIGNEES
        coordinator_working_dir = _clean_field(task.fields, "working_dir")
        if (
            coordinator_working_dir is None
            and executor in {"local", "remote", "claude_code", "codex"}
        ):
            from api.services.agent_worker.remote_spawn import api_host_name, is_local_host
            from api.services.directory_resolver import resolve_existing_location_affinity

            if executor not in {"claude_code", "codex"} or is_local_host(
                assignment.host, api_host_name(),
            ):
                coordinator_working_dir = resolve_existing_location_affinity(
                    _clean_field(task.fields, "project")
                )

        prior_request = task.fields.get(COORDINATOR_REQUEST_FIELD)
        if prior_request and prior_request != operation_id and self._task_coordinator_live(task):
            raise ProjectConflictError("project coordinator is already live")

        # A project that already has a terminal (not live), resumable owner
        # from an earlier Plan gets that owner WOKEN, not a second, competing
        # owner session. `prior_request != operation_id` distinguishes this
        # from an idempotent retry of the very call that created/linked the
        # current owner (which falls through to the ordinary path below and
        # re-links the same session it already created). The worker's
        # reconciler performs the actual wake on its next tick — this only
        # records the request, so the API never races the tick.
        existing_owner_session_id = task.fields.get(COORDINATOR_SESSION_FIELD)
        if (
            existing_owner_session_id
            and prior_request
            and prior_request != operation_id
        ):
            owner_session = self.session_store.get_by_session_id(existing_owner_session_id)
            if owner_session is not None and owner_session.status in TERMINAL_STATUSES:
                self.session_store.request_project_owner_wake(
                    task_id, owner_session_id=existing_owner_session_id,
                    reason=PLAN_OWNER_WAKE_REASON, operation_id=operation_id,
                )
                if self.transcript_store is not None:
                    self.transcript_store.append(
                        existing_owner_session_id, "project_owner_wake_requested", {
                            "project_id": task_id, "operation_id": operation_id,
                        },
                    )
                return {
                    "project_id": task_id,
                    "operation_id": operation_id,
                    "session_id": existing_owner_session_id,
                    "status": owner_session.status,
                    "created": False,
                    "wake_requested": True,
                }

        synthetic_task_id = _coordinator_task_id(task_id, operation_id)
        session = self.session_store.get(synthetic_task_id)
        created = session is None
        request = (
            ExecutionRequest(executor=executor)
            if managed_consent
            else ExecutionRequest(
                executor=executor,
                model_id=_clean_field(task.fields, "model"),
                effort=_clean_field(task.fields, "effort"),
                host=_clean_field(task.fields, "host"),
                working_dir=coordinator_working_dir,
            )
        )
        if session is None:
            from api.services.agent_worker.operator_spawn import create_operator_session

            try:
                result = create_operator_session(
                    self.session_store,
                    self._coordination_prompt(task, hierarchy, operation_id),
                    explicit_routing=executor,
                    execution_request=request,
                    task_id=synthetic_task_id,
                    dispatch_ready=False,
                )
            except sqlite3.IntegrityError:
                # A concurrent retry may have inserted the deterministic
                # session between our read and create. Re-read it below.
                result = {"ok": True, "session_id": None}
            if not result.get("ok"):
                raise ProjectConflictError(result.get("error", "could not create project coordinator"))
            if result.get("session_id"):
                session = self.session_store.get_by_session_id(result["session_id"])
            else:
                session = self.session_store.get(synthetic_task_id)
        if session is None:
            raise RuntimeError("coordinator session was not persisted")
        if managed_consent:
            self.session_store.set_assignment(
                session.task_id,
                model=route.request.model_id,
                effort=assignment.effort,
                host=assignment.host,
            )
            session = self.session_store.get_by_session_id(session.session_id)

        # The staged session is BLOCKED and therefore not dispatchable. Link
        # the authoritative parent before flipping it to CLAIMED.
        def link_precondition(current: "Task") -> None:
            self._require_project_current(current)
            pending = current.fields.get(CANCEL_OPERATION_FIELD)
            if pending:
                raise ProjectConflictError("project cancellation is pending")
            if field_truthy(current.fields.get(PROJECT_PAUSED_FIELD)):
                raise ProjectConflictError("project is paused")
            current_request = current.fields.get(COORDINATOR_REQUEST_FIELD)
            if (
                current_request
                and current_request != operation_id
                and self._task_coordinator_live(current)
            ):
                raise ProjectConflictError("project coordinator is already live")

        plan_fields = {
            EXECUTION_PAUSED_FIELD: "true",
            COORDINATOR_SESSION_FIELD: session.session_id,
            COORDINATOR_REQUEST_FIELD: operation_id,
        }
        # Record the integration branch only the moment this project first
        # gets a persistent owner — no prior coordinator request and no
        # prior handoff activation — never on a later re-Plan of a project
        # that already had one. A re-Plan must not undo an operator's
        # opt-out (clearing the field) or switch an in-flight project onto a
        # new branch mid-stream.
        is_first_owner = (
            not task.fields.get(COORDINATOR_REQUEST_FIELD)
            and not task.fields.get(LAST_HANDOFF_OPERATION_FIELD)
        )
        if is_first_owner and not task.fields.get(INTEGRATION_BRANCH_FIELD):
            plan_fields[INTEGRATION_BRANCH_FIELD] = _integration_branch_name(task)
        linked = self.manager.update(
            task_id,
            status="in_progress",
            fields=plan_fields,
            _project_operation="plan",
            _precondition=link_precondition,
        )
        if linked is None:
            raise KeyError(task_id)
        if session.status not in TERMINAL_STATUSES and session.status != "claimed":
            self.session_store.update_status(
                session.task_id,
                "claimed",
                attempt_id=session.attempt_id,
                turn_id=session.turn_id,
            )
        if self.transcript_store is not None:
            self.transcript_store.append(session.session_id, "project_coordinator_linked", {
                "project_id": task_id,
                "operation_id": operation_id,
            })
        current = self.session_store.get_by_session_id(session.session_id)
        return {
            "project_id": task_id,
            "operation_id": operation_id,
            "session_id": session.session_id,
            "status": current.status if current else session.status,
            "created": created,
        }

    def cancel_preview(self, task_id: str) -> dict[str, Any]:
        hierarchy = self.hierarchy()
        task = hierarchy.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if not hierarchy.is_project(task_id) and not task.fields.get(HANDOFF_OPERATION_FIELD):
            raise ProjectConflictError("task is not a project")
        children = []
        running_count = 0
        review_count = 0
        for child in hierarchy.children(task_id):
            state = hierarchy.child_state(child)
            if state in {"done", "cancelled"}:
                continue
            running = state == "running" or self._task_session_live(child)
            review = state == "awaiting_review"
            running_count += int(running)
            review_count += int(review)
            children.append({
                "id": child.id,
                "title": child.description,
                "state": state,
                "running": running,
                "awaiting_review": review,
            })
        return {
            "project_id": task_id,
            "operation_id": task.fields.get(CANCEL_OPERATION_FIELD),
            "cancellation_pending": bool(task.fields.get(CANCEL_OPERATION_FIELD)),
            "unfinished_count": len(children),
            "running_count": (
                running_count
                + int(self._task_coordinator_live(task))
                + int(self._handoff_source_live(task))
            ),
            "awaiting_review_count": review_count,
            "children": children,
            "confirmation_required": True,
        }

    async def cancel_project(self, task_id: str, *, operation_id: str) -> dict[str, Any]:
        operation_id = (operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        if self.session_store is None:
            raise RuntimeError("session store is required for project cancellation")
        hierarchy = self.hierarchy()
        parent = hierarchy.tasks.get(task_id)
        if parent is None:
            raise KeyError(task_id)
        if not hierarchy.is_project(task_id) and not parent.fields.get(HANDOFF_OPERATION_FIELD):
            raise ProjectConflictError("task is not a project")
        pending = parent.fields.get(CANCEL_OPERATION_FIELD)
        if pending and pending != operation_id:
            raise ProjectConflictError(f"project cancellation is already pending as {pending}")
        if not pending:
            def cancel_precondition(current: "Task") -> None:
                if (
                    not self.hierarchy().is_project(current.id)
                    and not current.fields.get(HANDOFF_OPERATION_FIELD)
                ):
                    raise ProjectConflictError("task is not a project")
                existing = current.fields.get(CANCEL_OPERATION_FIELD)
                if existing and existing != operation_id:
                    raise ProjectConflictError(
                        f"project cancellation is already pending as {existing}"
                    )

            parent = self.manager.update(
                task_id,
                fields={
                    CANCEL_OPERATION_FIELD: operation_id,
                    CANCEL_REQUESTED_AT_FIELD: datetime.now(timezone.utc).isoformat(),
                },
                _project_operation="cancel",
                _precondition=cancel_precondition,
            )

        stopped: list[str] = []
        failures: list[dict[str, str]] = []
        cancelled: list[str] = []
        preserved: list[str] = []
        abandoned: list[str] = []

        source_id = parent.fields.get(HANDOFF_SOURCE_SESSION_FIELD)
        if source_id:
            source = self.session_store.get_by_session_id(source_id)
            if source is not None and source.status not in TERMINAL_STATUSES:
                killed, errors = await self._stop_session(source)
                stopped.extend(killed)
                failures.extend(errors)
                refreshed = self.session_store.get_by_session_id(source_id)
                if refreshed is not None and refreshed.status not in TERMINAL_STATUSES:
                    failures.append({
                        "session_id": source_id,
                        "reason": "handoff source teardown could not be verified",
                    })
            handoff_operation = parent.fields.get(HANDOFF_OPERATION_FIELD)
            handoff_attempt = parent.fields.get(HANDOFF_SOURCE_ATTEMPT_FIELD)
            handoff_turn = parent.fields.get(HANDOFF_SOURCE_TURN_FIELD)
            source_quiescent = bool(
                handoff_operation
                and handoff_attempt
                and handoff_turn
                and self._has_handoff_quiescence(
                    source_id, handoff_operation, handoff_attempt, handoff_turn,
                )
            )
            if not source_quiescent and not any(
                failure.get("session_id") == source_id for failure in failures
            ):
                failures.append({
                    "session_id": source_id,
                    "reason": "handoff source stop is not yet verified",
                })

        coordinator_id = parent.fields.get(COORDINATOR_SESSION_FIELD)
        if coordinator_id:
            coordinator = self.session_store.get_by_session_id(coordinator_id)
            if coordinator is not None and coordinator.status not in TERMINAL_STATUSES:
                killed, errors = await self._stop_session(coordinator)
                stopped.extend(killed)
                failures.extend(errors)

        hierarchy = self.hierarchy()
        for child in hierarchy.children(task_id):
            state = hierarchy.child_state(child)
            if state == "done":
                preserved.append(child.id)
                continue
            if state == "cancelled":
                cancelled.append(child.id)
                continue

            child_failures: list[dict[str, str]] = []
            session = self.session_store.get(child.id)
            if session is not None and session.status not in TERMINAL_STATUSES:
                killed, errors = await self._stop_session(session)
                stopped.extend(killed)
                failures.extend(errors)
                child_failures.extend(errors)
            cli_sessions = [
                item for item in self.session_store.list_cli_sessions_for_task(child.id)
                if item.status != "ended"
            ]
            if cli_sessions:
                from api.routes.agents import _kill_cli_sessions

                try:
                    killed, errors = await _kill_cli_sessions(
                        cli_sessions, "project cancellation",
                    )
                except Exception as exc:  # noqa: BLE001 - durable partial result
                    killed = []
                    errors = [
                        {"session_id": item.session_id, "reason": str(exc)[:1000]}
                        for item in cli_sessions
                    ]
                stopped.extend(killed)
                failures.extend(errors)
                child_failures.extend(errors)
            if child_failures or self._task_session_live(child):
                continue

            review = state == "awaiting_review"

            def cancelled_tags(tags: list[str], *, review=review) -> list[str]:
                strip = {
                    agent_board.RUNNING_TAG,
                    agent_board.BLOCKED_TAG,
                    agent_board.HUMAN_TAG,
                    agent_board.COMPLETED_TAG,
                    agent_board.ACCEPTED_TAG,
                }
                result = [tag for tag in tags if tag.lstrip("#").lower() not in strip]
                if review and ABANDONED_TAG not in agent_board.normalize_tags(result):
                    result.append(ABANDONED_TAG)
                return result

            fields = {ABANDONED_AT_FIELD: datetime.now(timezone.utc).isoformat()} if review else None
            self.manager.update(
                child.id,
                status="cancelled",
                fields=fields,
                _tags_merge=cancelled_tags,
                _project_operation="cancel",
            )
            cancelled.append(child.id)
            if review:
                abandoned.append(child.id)

        hierarchy = self.hierarchy()
        unresolved = [
            child for child in hierarchy.children(task_id)
            if hierarchy.child_state(child) not in {"done", "cancelled"}
        ]
        parent = self.manager.get(task_id)
        coordinator_live = bool(parent and self._task_coordinator_live(parent))
        source_live = bool(parent and self._handoff_source_live(parent))
        complete = not unresolved and not coordinator_live and not source_live and not failures
        if complete:
            parent = self.manager.update(
                task_id,
                status="cancelled",
                fields={
                    CANCEL_OPERATION_FIELD: None,
                    CANCEL_REQUESTED_AT_FIELD: None,
                    LAST_CANCEL_OPERATION_FIELD: operation_id,
                    HANDOFF_OPERATION_FIELD: None,
                    HANDOFF_SOURCE_SESSION_FIELD: None,
                    HANDOFF_SOURCE_ATTEMPT_FIELD: None,
                    HANDOFF_SOURCE_TURN_FIELD: None,
                    HANDOFF_REQUEST_HASH_FIELD: None,
                    HANDOFF_REQUESTED_AT_FIELD: None,
                    HANDOFF_READY_AT_FIELD: None,
                    LAST_ABORTED_HANDOFF_FIELD: (
                        parent.fields.get(HANDOFF_OPERATION_FIELD)
                    ),
                },
                _project_operation="cancel",
            )
        return {
            "project_id": task_id,
            "operation_id": operation_id,
            "complete": complete,
            "pending": not complete,
            "cancelled_child_ids": sorted(set(cancelled)),
            "preserved_child_ids": sorted(set(preserved)),
            "abandoned_review_ids": sorted(set(abandoned)),
            "stopped_session_ids": sorted(set(stopped)),
            "failures": failures,
        }

    async def _stop_session(self, session) -> tuple[list[str], list[dict[str, str]]]:
        try:
            if self._session_teardown is not None:
                result = self._session_teardown(session)
                return await result if inspect.isawaitable(result) else result
            from api.routes.agents import _kill_session_subtree

            return await _kill_session_subtree(session, "project cancellation")
        except Exception as exc:  # noqa: BLE001 - returned as durable partial result
            killed = list(getattr(exc, "killed", []))
            failures = list(getattr(exc, "failures", []))
            failures.append({
                "session_id": session.session_id,
                "reason": str(exc)[:1000],
            })
            return killed, failures

    def _task_session_live(self, task: "Task") -> bool:
        if self.session_store is None:
            return False
        session = self.session_store.get(task.id)
        if session is not None and session.status not in TERMINAL_STATUSES:
            return True
        return any(item.status != "ended" for item in self.session_store.list_cli_sessions_for_task(task.id))

    def _task_coordinator_live(self, task: "Task") -> bool:
        session_id = task.fields.get(COORDINATOR_SESSION_FIELD)
        return bool(session_id and self._session_id_is_live(session_id))

    def _handoff_source_live(self, task: "Task") -> bool:
        session_id = task.fields.get(HANDOFF_SOURCE_SESSION_FIELD)
        if not session_id:
            return False
        operation_id = task.fields.get(HANDOFF_OPERATION_FIELD)
        attempt_id = task.fields.get(HANDOFF_SOURCE_ATTEMPT_FIELD)
        turn_id = task.fields.get(HANDOFF_SOURCE_TURN_FIELD)
        if operation_id and attempt_id and turn_id and self.transcript_store is not None:
            if self._has_handoff_quiescence(session_id, operation_id, attempt_id, turn_id):
                return False
        return True

    def _require_project_current(self, task: "Task") -> None:
        if not self.hierarchy().is_project(task.id):
            raise ProjectConflictError("task is not a project")

    def _session_id_is_live(self, session_id: str) -> bool:
        if self.session_store is None:
            return False
        session = self.session_store.get_by_session_id(session_id)
        return bool(session and session.status not in TERMINAL_STATUSES)

    def _coordination_prompt(
        self, project: "Task", hierarchy: TaskHierarchy, operation_id: str,
    ) -> str:
        children = hierarchy.children(project.id)
        outcomes = (
            self.session_store.list_all_card_outcomes()
            if self.session_store is not None
            else {}
        )
        child_lines = [
            f"- {child.id}: {child.description} | status={hierarchy.child_state(child)} "
            f"| assignee={agent_board.derive_assignee(child.tags) or 'unassigned'}"
            + (
                f" | outcome={str(outcomes[child.id].get('summary') or '').strip()[:500]}"
                if child.id in outcomes and outcomes[child.id].get("summary")
                else ""
            )
            for child in children[:50]
        ]
        notes = (project.notes or "").strip()[:6000]
        return (
            "Plan and delegate this LifeOS project using durable task children.\n"
            f"Project ID: {project.id}\n"
            f"Operation ID: {operation_id}\n"
            f"Objective: {project.description}\n"
            f"Notes/acceptance criteria:\n{notes or '(none)'}\n"
            f"Current children ({len(children)} total; at most 50 shown):\n"
            + ("\n".join(child_lines) if child_lines else "(none)")
            + "\nUse task hierarchy, not session ancestry. Preserve explicit child assignments "
            "and do not expand provider/cloud consent beyond this delegated scope. When creating "
            "a child, derive one stable operation_key from this project ID, operation ID, and the "
            "child's role; reuse that key on retry so the task tool recovers the same child.\n"
            "You are this project's persistent owner: you are woken automatically when a "
            "child's state changes -- newly blocked, failed, done, cancelled, or awaiting "
            "review -- with several such events batched into one wake. The project ends only "
            "through the explicit project completion/cancellation actions."
        )


def _clean_field(fields: dict[str, str], key: str) -> str | None:
    value = fields.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _coordinator_task_id(project_id: str, operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:12]
    return f"project_{project_id}_{digest}"


def _integration_branch_name(task: "Task") -> str:
    """The project's deterministic integration branch — follows the same
    `<type>/<slug>-<suffix>` convention `ensure_worktree` derives a task's
    own work branch from, but seeded so it can never collide with one. A
    handed-off project keeps the source task's own id after activation, and
    that task's own CLI worktree branch (if it has one) is derived from
    `(task.description, task.id)` directly — the exact pair this would
    collide with un-seeded, since a coding session that already pushed WIP
    to that branch would then hand children (and this task's own worktree
    finalize push) the same ref. The suffix is a short hash of the task id
    rather than the id itself, so it stays deterministic and task-specific
    (two different projects still get different suffixes) without ever
    equaling the plain id `derive_branch_name` would use for the task's own
    branch.
    """
    seed = hashlib.sha256(f"integration:{task.id}".encode("utf-8")).hexdigest()[:8]
    return derive_branch_name(task.description, seed)


def _handoff_request_hash(request: dict[str, Any]) -> str:
    encoded = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _handoff_child_operation_key(
    project_id: str, operation_id: str, child_key: str,
) -> str:
    return f"project-handoff:{project_id}:{operation_id}:{child_key}"


def _owner_tag_for_session(source: Session, task: "Task") -> str | None:
    existing = agent_board.derive_assignee(task.tags)
    if existing:
        return existing
    executor = None
    if source.execution_spec:
        try:
            executor = ExecutionSpec.from_dict(source.execution_spec).executor
        except (TypeError, ValueError):
            executor = None
    executor = executor or source.routing
    return {
        "claude": "cloud",
        "remote": "cloud",
        "claude_code": "claude",
        "codex": "codex",
        "hermes": "hermes",
        "local": "local",
    }.get(executor or "")
