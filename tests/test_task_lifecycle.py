"""Synthetic lifecycle coordination tests (no live services)."""
from __future__ import annotations

import threading

import pytest

from api.services.agent_board import derive_lane
from api.services.agent_worker.lifecycle import LifecycleEvent, LifecycleProjector
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    OCCURRENCE_DISPATCHED,
    SessionStore,
    WAIT_DEPENDENCY,
    WAIT_OPERATOR,
    WAIT_PROVIDER,
)
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


def test_occurrence_claim_is_single_winner_and_reusable(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    key = store.occurrence_key_for("sched-a", "2026-09-10T13:00:00+00:00")
    store.ensure_occurrence("sched-a", "2026-09-10T13:00:00+00:00", occurrence_key=key)
    winners = []
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait()
        winners.append(store.claim_occurrence(key, owner))

    threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    claimed = [item for item in winners if item is not None]
    assert len(claimed) == 1
    assert winners.count(None) == 1
    assert store.link_occurrence(key, task_id="task-1", owner=claimed[0].lease_owner)
    assert store.get_occurrence(key).state == OCCURRENCE_DISPATCHED
    assert store.get_occurrence(key).task_id == "task-1"


def test_expired_occurrence_generation_cannot_link_after_recovery_claim(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    key = store.occurrence_key_for("sched-fence", "2026-09-10T13:00:00+00:00")
    store.ensure_occurrence("sched-fence", "2026-09-10T13:00:00+00:00", occurrence_key=key)
    first = store.claim_occurrence(key, "old-owner")
    with store._connect() as conn:
        conn.execute(
            "UPDATE schedule_occurrences SET lease_expires_at = 0 WHERE occurrence_key = ?",
            (key,),
        )
    second = store.claim_occurrence(key, "recovery-owner")

    assert first is not None and second is not None
    assert second.lease_generation > first.lease_generation
    assert not store.link_occurrence(
        key, task_id="stale-task", owner="old-owner",
        lease_generation=first.lease_generation,
    )
    assert store.link_occurrence(
        key, task_id="recovered-task", owner="recovery-owner",
        lease_generation=second.lease_generation,
    )
    assert store.get_occurrence(key).task_id == "recovered-task"


def test_projection_is_idempotent_and_machine_wait_stays_in_progress(tmp_path):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create("Synthetic provider task", tags=["local", "agent-running"])
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, routing="local")
    projector = LifecycleProjector(sessions, manager)
    event = LifecycleEvent(
        event_id="event-1", task_id=task.id, session_id=session.session_id,
        attempt_id=session.attempt_id, target_status="blocked",
        expected_version=task.updated_at, wait_type=WAIT_DEPENDENCY,
        wait_reason="synthetic dependency",
    )
    assert projector.transition(event)
    assert not projector.transition(event)
    refreshed = manager.get(task.id)
    assert refreshed.status == "in_progress"
    assert derive_lane(refreshed.status, refreshed.tags) == "in_progress"
    assert refreshed.fields["wait_reason"] == "synthetic dependency"
    assert sessions.list_pending_projections() == []


def test_projection_preserves_operator_edit_on_stale_version(tmp_path):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create("Synthetic operator title")
    sessions = SessionStore(tmp_path / "sessions.db")
    projector = LifecycleProjector(sessions, manager)
    manager.update(task.id, description="Operator edited title")
    event = LifecycleEvent(
        event_id="event-stale", task_id=task.id, target_status="done",
        expected_version=task.updated_at,
    )
    assert projector.transition(event) is False
    assert manager.get(task.id).description == "Operator edited title"
    assert sessions.list_pending_projections()[0]["state"] == "conflicted"


def test_resolved_operator_wake_is_durable_and_machine_wait_is_ignored(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create("wake-task")
    operator_id = sessions.record_wait(
        task_id=session.task_id, session_id=session.session_id,
        attempt_id=session.attempt_id, wait_type=WAIT_OPERATOR, card_id="card-1",
    )
    provider_id = sessions.record_wait(
        task_id=session.task_id, session_id=session.session_id,
        attempt_id=session.attempt_id, wait_type=WAIT_PROVIDER, card_id="card-1",
    )
    sessions.mark_wait_resolved(operator_id)
    sessions.mark_wait_resolved(provider_id)

    assert sessions.enqueue_wait_wakeup(operator_id)
    assert not sessions.enqueue_wait_wakeup(provider_id)
    pending = sessions.drain_pending_messages(session.session_id)
    assert [row["sender_id"] for row in pending] == ["human_queue"]
    assert sessions.list_resolved_waits()[0]["wait_id"] == operator_id
    assert sessions.complete_wait_wakeup(operator_id)
    assert sessions.list_resolved_waits()[0]["wait_id"] == provider_id


def test_human_queue_wake_replays_through_projector_and_is_single_path(tmp_path):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create("Synthetic wake task", status="blocked", tags=["agent-blocked", "local"])
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="local")
    wait_id = sessions.record_wait(
        task_id=task.id, session_id=session.session_id,
        attempt_id=session.attempt_id, wait_type=WAIT_OPERATOR, card_id="card-1",
    )
    sessions.mark_wait_resolved(wait_id)

    from api.services.agent_worker.worker import Worker

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker.lifecycle_projector = LifecycleProjector(sessions, manager)
    dispatched = []
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    worker._dispatch = lambda payload: dispatched.append(payload)

    worker._replay_wait_wakeups()

    refreshed_task = manager.get(task.id)
    refreshed_session = sessions.get(task.id)
    assert refreshed_task.status == "in_progress"
    assert "agent-running" in refreshed_task.tags
    assert refreshed_session.status == STATUS_CLAIMED
    assert dispatched and dispatched[0]["id"] == task.id
    assert sessions.list_resolved_waits() == []
    assert sessions.list_pending_projections() == []

    # The resolved marker is consumed, so a later replay cannot enqueue or
    # dispatch the same wake a second time.
    worker._replay_wait_wakeups()
    assert len(dispatched) == 1
