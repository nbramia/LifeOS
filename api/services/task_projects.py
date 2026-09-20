"""Derived task hierarchy and project lifecycle operations.

Projects are ordinary tasks with incoming ``fields.parent_id`` references.
This module never persists a project type or a child list: every view and
guard is rebuilt from the complete task set, while the few durable parent
fields here describe execution state only.
"""
from __future__ import annotations

import hashlib
import inspect
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, TYPE_CHECKING

from api.services import agent_board
from api.services.agent_worker.assignment import extract_assignment
from api.services.agent_worker.execution import ExecutionRequest, parse_legacy_route_alias
from api.services.agent_worker.session_store import SessionStore, TERMINAL_STATUSES
from api.services.agent_worker.transcript_store import TranscriptStore

if TYPE_CHECKING:
    from api.services.task_manager import Task, TaskManager


PARENT_ID_FIELD = "parent_id"
EXECUTION_PAUSED_FIELD = "execution_paused"
EXECUTION_RESERVATION_FIELD = "execution_reservation_until"
COORDINATOR_SESSION_FIELD = "project_coordinator_session_id"
COORDINATOR_REQUEST_FIELD = "project_coordinator_request_id"
CANCEL_OPERATION_FIELD = "project_cancel_operation_id"
CANCEL_REQUESTED_AT_FIELD = "project_cancel_requested_at"
LAST_CANCEL_OPERATION_FIELD = "project_last_cancel_operation_id"
ABANDONED_AT_FIELD = "project_result_abandoned_at"
ABANDONED_TAG = "agent-result-abandoned"


class ProjectConflictError(ValueError):
    """A project mutation conflicts with current hierarchy/lifecycle state."""


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
            from api.services.directory_resolver import resolve_location_affinity

            if executor not in {"claude_code", "codex"} or is_local_host(
                assignment.host, api_host_name(),
            ):
                coordinator_working_dir = resolve_location_affinity(
                    _clean_field(task.fields, "project")
                )

        prior_request = task.fields.get(COORDINATOR_REQUEST_FIELD)
        if prior_request and prior_request != operation_id and self._task_coordinator_live(task):
            raise ProjectConflictError("project coordinator is already live")

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
            current_request = current.fields.get(COORDINATOR_REQUEST_FIELD)
            if (
                current_request
                and current_request != operation_id
                and self._task_coordinator_live(current)
            ):
                raise ProjectConflictError("project coordinator is already live")

        linked = self.manager.update(
            task_id,
            status="in_progress",
            fields={
                EXECUTION_PAUSED_FIELD: "true",
                COORDINATOR_SESSION_FIELD: session.session_id,
                COORDINATOR_REQUEST_FIELD: operation_id,
            },
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
        if not hierarchy.is_project(task_id):
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
            "running_count": running_count + int(self._task_coordinator_live(task)),
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
        if not hierarchy.is_project(task_id):
            raise ProjectConflictError("task is not a project")
        pending = parent.fields.get(CANCEL_OPERATION_FIELD)
        if pending and pending != operation_id:
            raise ProjectConflictError(f"project cancellation is already pending as {pending}")
        if not pending:
            def cancel_precondition(current: "Task") -> None:
                self._require_project_current(current)
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
        complete = not unresolved and not coordinator_live and not failures
        if complete:
            parent = self.manager.update(
                task_id,
                status="cancelled",
                fields={
                    CANCEL_OPERATION_FIELD: None,
                    CANCEL_REQUESTED_AT_FIELD: None,
                    LAST_CANCEL_OPERATION_FIELD: operation_id,
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
            "child's role; reuse that key on retry so the task tool recovers the same child."
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
