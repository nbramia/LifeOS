"""Shared task/session lifecycle coordination.

Markdown remains the operator-facing task authority while ``SessionStore``
holds execution identity and cross-store recovery markers.  This module is a
small projector, deliberately not an event-sourcing framework: every event is
idempotent, carries the attempt identity, and is applied through TaskManager's
id-addressed/CAS write path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from api.services.agent_board import BLOCKED_TAG, COMPLETED_TAG, RUNNING_TAG
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    WAIT_DEPENDENCY,
    WAIT_OPERATOR,
    WAIT_PROVIDER,
    SessionStore,
)

FAILED_TAG = "agent-failed"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleEvent:
    """A desired task projection with an optimistic expected task version."""

    event_id: str
    task_id: str
    session_id: str | None = None
    attempt_id: str | None = None
    target_status: str = STATUS_RUNNING
    expected_version: str | None = None
    wait_type: str | None = None
    wait_reason: str = ""
    human_card_id: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "wait_type": self.wait_type,
            "wait_reason": self.wait_reason,
            "human_card_id": self.human_card_id,
            "dependencies": list(self.dependencies),
            "reason": self.reason,
        }


def _tags(task: Any) -> list[str]:
    return [str(value) for value in (getattr(task, "tags", None) or [])]


def _fields(task: Any) -> dict[str, str]:
    return dict(getattr(task, "fields", None) or {})


def _with_tag(tags: list[str], remove: set[str], add: str | None) -> list[str]:
    result = [tag for tag in tags if tag.lstrip("#").lower() not in remove]
    if add and not any(tag.lstrip("#").lower() == add for tag in result):
        result.append(add)
    return result


class LifecycleProjector:
    """Project one lifecycle transition, with crash-safe acknowledgement."""

    def __init__(self, session_store: SessionStore, task_manager: Any):
        self.session_store = session_store
        self.task_manager = task_manager

    @staticmethod
    def event_id(task_id: str, attempt_id: str | None, target_status: str,
                 *, suffix: str = "") -> str:
        return ":".join((task_id, attempt_id or "legacy", target_status, suffix))

    def transition(
        self, event: LifecycleEvent, *, task: Any | None = None,
    ) -> bool:
        """Apply a task transition, returning whether Markdown was updated.

        A mismatch with ``expected_version`` is left pending and returns False;
        the operator's newer content is never overwritten.  A later retry may
        supply a fresh event/version after reconciling the task.
        """
        if event.wait_type not in (None, WAIT_OPERATOR, WAIT_PROVIDER, WAIT_DEPENDENCY):
            raise ValueError(f"invalid wait type: {event.wait_type}")
        if not self.session_store.begin_projection(
            event.event_id,
            task_id=event.task_id,
            session_id=event.session_id,
            attempt_id=event.attempt_id,
            expected_version=event.expected_version,
            target_status=event.target_status,
            payload=event.payload(),
        ):
            return False

        current = task or self.task_manager.get(event.task_id)
        if current is None:
            self.session_store.acknowledge_projection(event.event_id, error="task missing")
            return False
        actual_version = getattr(current, "updated_at", None)
        if event.expected_version is not None and actual_version != event.expected_version:
            self.session_store.acknowledge_projection(
                event.event_id,
                error=f"stale expected version {event.expected_version!r}; got {actual_version!r}",
            )
            return False

        target_status = event.target_status
        wait_type = event.wait_type
        if wait_type in (WAIT_PROVIDER, WAIT_DEPENDENCY):
            # Machine waits are visibly in-progress, not Human Queue.
            target_status = "in_progress"
        elif wait_type == WAIT_OPERATOR:
            target_status = STATUS_BLOCKED

        # Session statuses are internal execution values; TaskManager uses
        # the public checkbox vocabulary. Keep this translation in the
        # projector so every worker terminal path shares one CAS write.
        task_status = {
            STATUS_COMPLETED: "done",
            STATUS_FAILED: "cancelled",
            STATUS_BUDGET_EXCEEDED: "cancelled",
            STATUS_BLOCKED: "blocked",
            STATUS_RUNNING: "in_progress",
        }.get(target_status, target_status)

        terminal_tag = {
            STATUS_COMPLETED: COMPLETED_TAG,
            STATUS_FAILED: FAILED_TAG,
            STATUS_BUDGET_EXCEEDED: "agent-budget-exceeded",
            STATUS_BLOCKED: BLOCKED_TAG,
        }.get(target_status)
        if terminal_tag:
            tags = _with_tag(_tags(current), {RUNNING_TAG, COMPLETED_TAG, FAILED_TAG,
                                               "agent-budget-exceeded", BLOCKED_TAG,
                                               "agent-wait-provider", "agent-wait-dependency"}, terminal_tag)
        elif task_status == "in_progress":
            tags = _with_tag(_tags(current), {COMPLETED_TAG, FAILED_TAG,
                                               "agent-budget-exceeded", BLOCKED_TAG,
                                               "agent-wait-provider", "agent-wait-dependency"}, RUNNING_TAG)
            if wait_type in (WAIT_PROVIDER, WAIT_DEPENDENCY):
                tags.append(f"agent-wait-{wait_type}")
        else:
            tags = _tags(current)

        fields = _fields(current)
        for key in ("wait_reason", "wait_type", "human_card_id", "dependencies"):
            fields.pop(key, None)
        if wait_type:
            fields["wait_type"] = wait_type
            fields["wait_reason"] = event.wait_reason or event.reason
            if event.human_card_id:
                fields["human_card_id"] = event.human_card_id
            if event.dependencies:
                # TaskManager field values intentionally reject JSON brackets;
                # the authoritative wait row keeps the lossless dependency list.
                fields["dependencies"] = ",".join(event.dependencies)

        try:
            updated = self.task_manager.update(
                event.task_id, status=task_status, tags=tags, fields=fields,
            )
        except Exception as exc:
            # Keep the marker retryable; a CAS conflict or transient write
            # failure must never be acknowledged as applied.
            logger.warning("lifecycle projection failed for %s: %s", event.task_id, exc)
            return False
        if updated is None:
            return False
        self.session_store.acknowledge_projection(event.event_id)
        return True

    def replay_pending(self) -> int:
        """Retry projections left pending by a process crash.

        Projection rows are the durable handoff between the session database
        and Markdown. Replaying in creation order preserves transition order;
        the stored expected version still protects an operator edit via CAS.
        """
        applied = 0
        for row in self.session_store.list_pending_projections():
            try:
                payload = json.loads(row.get("payload_json") or "{}")
                event = LifecycleEvent(
                    event_id=row["event_id"], task_id=row["task_id"],
                    session_id=row.get("session_id"), attempt_id=row.get("attempt_id"),
                    target_status=row["target_status"],
                    expected_version=row.get("expected_version"),
                    wait_type=payload.get("wait_type"),
                    wait_reason=payload.get("wait_reason") or "",
                    human_card_id=payload.get("human_card_id"),
                    dependencies=tuple(payload.get("dependencies") or ()),
                    reason=payload.get("reason") or "",
                )
                if self.transition(event):
                    applied += 1
            except Exception:  # pragma: no cover - recovery must continue
                logger.exception("lifecycle projection replay failed for %s", row.get("event_id"))
        return applied

    def projection_applied(self, event_id: str) -> bool:
        return self.session_store.projection_applied(event_id)

    def record_wait(self, event: LifecycleEvent) -> str:
        if not event.session_id or not event.attempt_id or not event.wait_type:
            raise ValueError("typed waits require session, attempt, and wait_type")
        wait_id = self.session_store.record_wait(
            task_id=event.task_id,
            session_id=event.session_id,
            attempt_id=event.attempt_id,
            wait_type=event.wait_type,
            reason=event.wait_reason or event.reason,
            card_id=event.human_card_id,
            dependencies=list(event.dependencies),
            wait_id=event.event_id,
        )
        self.transition(event)
        return wait_id
