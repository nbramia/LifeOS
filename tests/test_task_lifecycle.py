"""Synthetic lifecycle coordination tests (no live services)."""
from __future__ import annotations

import threading

import pytest

from types import SimpleNamespace

from api.services.agent_board import COMPLETED_TAG, RUNNING_TAG, derive_lane
from api.services.agent_worker.lifecycle import FAILED_TAG, LifecycleEvent, LifecycleProjector
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_FAILED,
    STATUS_RUNNING,
    OCCURRENCE_DISPATCHED,
    SessionStore,
    WAIT_DEPENDENCY,
    WAIT_OPERATOR,
    WAIT_PROVIDER,
)
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


def _make_worker(sessions: SessionStore, manager: TaskManager):
    """A tick-free `Worker` stub wired just enough for the lifecycle-drift
    sweep: real store + real projector, no HTTP/Telegram/executors."""
    from api.services.agent_worker.worker import Worker

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker.lifecycle_projector = LifecycleProjector(sessions, manager)
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    return worker


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


def test_drift_sweep_reconciles_a_kill_parked_at_blocked(tmp_path):
    """A kill landing on a session parked at BLOCKED bypasses the projector
    (`mark_cancelled` is a raw status write) and there's no poll left on a
    parked session to reconcile the vault the ordinary way — the drift
    sweep is the only thing that still fixes the tag."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic parked task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")

    # Operator kill: flips the row terminal without touching Markdown.
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    assert RUNNING_TAG in manager.get(task.id).tags  # drifted: still #agent-running

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags
    assert refreshed.status == "cancelled"


def test_drift_sweep_reconciles_a_kill_that_landed_while_worker_was_down(tmp_path):
    """A kill can land via the operator HTTP endpoint at any time, including
    while the worker process itself isn't running to poll anything. On the
    next tick after restart, the sweep must still find and fix the drift
    purely from durable state — no in-memory carryover required."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic offline-kill task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")

    # The kill lands (e.g. via the operator HTTP endpoint) while nothing is
    # polling this session — simulated here by never touching update_status.
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    assert RUNNING_TAG in manager.get(task.id).tags  # drifted before "restart"

    # "Restart": a brand-new worker object, same durable stores — the sweep
    # must not depend on anything the old process held in memory.
    restarted_worker = _make_worker(sessions, manager)
    healed = restarted_worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_drift_sweep_does_not_double_project_a_live_kill_already_reconciled(tmp_path):
    """Killing a session that's still being actively polled already
    reconciles correctly today: the executor observes the terminal row and
    calls `update_status`, which fires the projector hook on its own. The
    sweep must recognize that projection and leave it alone rather than
    projecting the same transition a second time."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic live-kill task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
    worker = _make_worker(sessions, manager)
    sessions.set_status_projector(worker._project_session_status)

    # Kill flips the row FAILED (no projection yet — mark_cancelled bypasses
    # the hook), then the executor's own next poll observes the terminal row
    # and calls update_status, which *does* fire the hook this time.
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
    )
    reconciled = manager.get(task.id)
    assert FAILED_TAG in reconciled.tags
    assert RUNNING_TAG not in reconciled.tags

    # Nothing left for the sweep to do — the live-kill path already applied
    # the projection for this exact (task, status).
    assert sessions.list_terminal_unprojected(since=0, limit=10) == []
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == reconciled.tags
    assert refreshed.updated_at == reconciled.updated_at


def test_drift_sweep_leaves_a_reopened_followup_session_alone(tmp_path):
    """A task legitimately reopened to #agent-running for a resumed
    follow-up turn leaves its session row non-terminal. The sweep only ever
    looks at terminal rows, so it must never touch this — even though the
    vault tag and the (still-live) session look superficially similar to a
    freshly-dispatched task."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic reopened task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    sessions.create(task.id, status=STATUS_CLAIMED, routing="claude_code")

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert RUNNING_TAG in refreshed.tags
    assert refreshed.tags == task.tags
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_leaves_a_settled_task_alone_when_no_projection_was_ever_recorded(tmp_path):
    """`list_terminal_unprojected`'s "no applied projection" candidate
    signature also matches every historical terminal session that predates
    the projector itself — none of those ever recorded a row either. The
    real drift signature is the task's *current* vault tag, not the
    presence of a projections row: a task the operator already accepted
    (`#accepted #agent-completed`, status done) must be left byte-identical
    even though its session row is terminal and unprojected."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic already-settled task", status="done",
        tags=["claude_code", COMPLETED_TAG, "accepted"],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
    # A later status write on this same attempt/turn that never went through
    # a projector hook — mirrors a historical session that predates this
    # sweep. No hook is wired on this store, so no projection row exists,
    # matching production's 0-applied-rows state for old sessions.
    sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
    )
    assert sessions.list_pending_projections() == []
    assert sessions.list_terminal_unprojected(since=0, limit=10) != []  # candidate, not yet filtered

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == task.tags
    assert refreshed.status == task.status
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_heals_a_second_drift_after_reopen_with_new_attempt(tmp_path):
    """A task can legitimately be killed at the same terminal status twice
    across separate reopened executions. The first kill's applied
    projection must not mask the second one — the dedupe has to be scoped
    to the attempt, not just the (task, status) pair."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic twice-killed task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    first_session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=first_session.attempt_id, reason="operator_killed",
    )
    worker = _make_worker(sessions, manager)
    first_healed = worker._reconcile_lifecycle_drift()
    first_reconciled = manager.get(task.id)
    assert first_healed == 1
    assert FAILED_TAG in first_reconciled.tags

    # Reopen for a follow-up turn: a new attempt id, session goes back to
    # non-terminal. The real reopen path also swaps the vault tag back to
    # #agent-running — mirrored here directly since this test only
    # exercises the session-store/projector half of that path.
    reopened = sessions.begin_new_execution(task.id)
    manager.update(task.id, status="in_progress", tags=["claude_code", RUNNING_TAG])

    # Parked and killed again — same terminal status as before, different
    # attempt.
    assert sessions.mark_cancelled(
        task.id, attempt_id=reopened.attempt_id, reason="operator_killed",
    )

    second_healed = worker._reconcile_lifecycle_drift()
    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_tick_wires_in_the_lifecycle_drift_sweep(tmp_path, monkeypatch):
    """`tick()` must actually invoke `_reconcile_lifecycle_drift` — every
    other tick step is stubbed to a no-op so this isolates just that
    wiring, following the pattern of
    `test_human_queue_wake_replays_through_projector_and_is_single_path`
    above and `TestTickInvokesHumanQueue` in
    test_agent_worker_human_queue.py."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic tick-wired task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )

    worker = _make_worker(sessions, manager)
    worker.spend_tracker = SimpleNamespace(can_start_task=lambda estimate: True)
    for step in (
        "_process_human_queue",
        "_replay_wait_wakeups",
        "_wake_sleeping_sessions",
        "_poll_managed_sessions",
        "_resume_yielded_for_children",
        "_dispatch_spawned_sessions",
        "_process_clarification_answers",
        "_timeout_stale_clarifications",
    ):
        monkeypatch.setattr(worker, step, lambda: None)
    monkeypatch.setattr(worker, "_list_agent_tasks", lambda: [])

    worker.tick()

    refreshed = manager.get(task.id)
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


class TestListTerminalUnprojectedHasVaultTaskGate:
    """`has_vault_task` (origin != 'operator' and no parent_session_id) must
    exclude operator root-spawns and spawned children from the drift-sweep
    candidate query — they carry synthetic task ids with no vault row, so
    reconciling them would 404."""

    def test_excludes_operator_origin_session(self, tmp_path):
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create("operator-task", origin="operator")
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        assert sessions.list_terminal_unprojected(since=0, limit=10) == []

    def test_excludes_spawned_child_session(self, tmp_path):
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create(
            "child-task", parent_session_id="parent-session-1",
            root_session_id="parent-session-1",
        )
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        assert sessions.list_terminal_unprojected(since=0, limit=10) == []

    def test_includes_an_ordinary_vault_backed_session(self, tmp_path):
        """Control case — the gate must not accidentally exclude everything."""
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create("normal-task")
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        rows = sessions.list_terminal_unprojected(since=0, limit=10)
        assert {row.task_id for row in rows} == {"normal-task"}
