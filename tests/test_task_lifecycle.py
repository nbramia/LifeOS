"""Synthetic lifecycle coordination tests (no live services)."""
from __future__ import annotations

import threading
import time

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
    sweep: real store + real projector, no HTTP/Telegram/executors.

    `_fetch_task_or_absent` treats a task missing from `manager` as a
    definitive absence (`is_absent=True`) — the vault-backed equivalent of a
    404 — matching how these tests simulate a deleted task by simply never
    creating it. A test that needs to simulate a merely *unavailable* fetch
    (transient error, not a confirmed deletion) overrides
    `worker._fetch_task_or_absent` directly to return `(None, False)`.
    """
    from api.services.agent_worker.worker import Worker

    def _fetch_task_or_absent(task_id):
        task = manager.get(task_id)
        return (None, True) if task is None else (task.to_dict(), False)

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker.lifecycle_projector = LifecycleProjector(sessions, manager)
    worker._fetch_task_or_absent = _fetch_task_or_absent
    worker._fetch_task = lambda task_id: _fetch_task_or_absent(task_id)[0]
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
    sweep must recognize the tag is already settled and leave it alone
    rather than projecting the same transition a second time."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic live-kill task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
    worker = _make_worker(sessions, manager)
    sessions.set_status_projector(worker._project_session_status)

    # Kill flips the row FAILED (bypassing the hook — mark_cancelled is a raw
    # status write), then the executor's own next poll observes the terminal
    # row and calls update_status, which *does* fire the hook this time.
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
    )
    reconciled = manager.get(task.id)
    assert FAILED_TAG in reconciled.tags
    assert RUNNING_TAG not in reconciled.tags

    # The row hasn't been swept yet (`drift_swept_at` is still NULL) — it's
    # a candidate — but the live-kill path already fixed the tag before the
    # sweep ever runs, so there's nothing left for it to do.
    assert sessions.list_terminal_unswept(since=0, limit=10) != []
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == reconciled.tags
    assert refreshed.updated_at == reconciled.updated_at
    # Now stamped as examined, so a later tick doesn't re-fetch it either.
    assert sessions.list_terminal_unswept(since=0, limit=10) == []


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
    """`list_terminal_unswept`'s "never examined" candidate signature also
    matches every historical terminal session that predates the sweep
    itself — none of those has a `drift_swept_at` stamp either. The real
    drift signature is the task's *current* vault tag, not the absence of a
    stamp: a task the operator already accepted (`#accepted
    #agent-completed`, status done) must be left byte-identical even though
    its session row is terminal and never swept."""
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
    assert sessions.list_terminal_unswept(since=0, limit=10) != []  # candidate, not yet swept

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == task.tags
    assert refreshed.status == task.status
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_marks_a_settled_row_resolved_so_it_stops_being_a_candidate(tmp_path):
    """Unlike a genuinely-drifted row, a settled row's vault tag is already
    terminal and never flips back to running/blocked on its own — so once
    the sweep confirms it isn't drifted, it must stamp `drift_swept_at` the
    same way the `task is None` branch does. Without that stamp, the row
    matches `list_terminal_unswept` on every future tick forever,
    permanently holding a slot in the bounded per-tick batch."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic settled task", status="done",
        tags=["claude_code", COMPLETED_TAG, "accepted"],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
    sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
    )
    assert sessions.list_terminal_unswept(since=0, limit=10) != []  # candidate, not yet swept

    worker = _make_worker(sessions, manager)
    first_healed = worker._reconcile_lifecycle_drift()
    assert first_healed == 0
    # The stamp must be durable, not just an in-memory skip — a fresh query
    # against the store shows the row is no longer a candidate.
    assert sessions.list_terminal_unswept(since=0, limit=10) == []

    second_healed = worker._reconcile_lifecycle_drift()
    assert second_healed == 0
    assert manager.get(task.id).tags == task.tags


def test_drift_sweep_marks_a_definitively_deleted_task_resolved(tmp_path):
    """A definitive 404 means the task is gone from the vault — nothing
    left to reconcile. The row must get a `drift_swept_at` stamp so a
    second sweep neither re-fetches it nor re-stamps it. Pins half (a) of
    the stamp write: the definitively-absent path."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create("deleted-task", status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        session.task_id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    # No corresponding task was ever created in `manager` — `_make_worker`'s
    # `_fetch_task_or_absent` reports that as a definitive absence, the
    # vault-backed equivalent of a 404.
    assert sessions.list_terminal_unswept(since=0, limit=10) != []  # candidate

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()
    assert healed == 0
    assert sessions.list_terminal_unswept(since=0, limit=10) == []

    second_healed = worker._reconcile_lifecycle_drift()
    assert second_healed == 0


def test_drift_sweep_does_not_heal_or_mark_resolved_on_a_transient_fetch_failure(tmp_path):
    """A `_fetch_task_or_absent` call that fails without a definitive 404 —
    a transient error, timeout, or malformed payload, e.g. the API
    restarting mid-tick after a deploy — must NOT be treated as "task
    deleted". Stamping `drift_swept_at` in that case would permanently
    suppress healing of genuine drift behind a routine one-tick blip. This
    is the regression test for a defect introduced by an earlier round-1
    fix: the row must stay an unstamped candidate through the failure and
    heal on a later tick once the fetch succeeds."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic flaky-fetch task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )

    worker = _make_worker(sessions, manager)
    real_fetch = worker._fetch_task_or_absent
    worker._fetch_task_or_absent = lambda task_id: (None, False)  # unavailable, not a 404

    first_healed = worker._reconcile_lifecycle_drift()
    assert first_healed == 0
    assert RUNNING_TAG in manager.get(task.id).tags  # still drifted
    # No stamp was written for the failed fetch — still a candidate for the
    # next tick.
    assert sessions.list_terminal_unswept(since=0, limit=10) != []

    worker._fetch_task_or_absent = real_fetch
    second_healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_drift_sweep_heals_a_second_drift_after_reopen_with_new_attempt(tmp_path):
    """A task can legitimately be killed at the same terminal status twice
    across separate reopened executions. The first heal's watermark stamp
    must not mask the second, later drift — `begin_new_execution` and the
    second `mark_cancelled` both bump `last_activity_at` past the stamp, so
    the row re-qualifies as a candidate automatically."""
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
    # `last_activity_at` is second-granularity, and the reopen/second-kill
    # above can land in the same wall-clock second as the first sweep's
    # stamp in a fast test run. Force a deterministic advance past the
    # stamp rather than relying on real time to pass, since the property
    # under test is the comparison, not the clock.
    with sessions._connect() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity_at = last_activity_at + 1 WHERE task_id = ?",
            (task.id,),
        )

    second_healed = worker._reconcile_lifecycle_drift()
    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_drift_sweep_heals_a_reopen_on_the_same_attempt_after_being_marked_settled(tmp_path):
    """A task can also be reopened for a follow-up turn on the exact same
    attempt, with no `begin_new_execution` in between — the
    reopen-for-pending-messages path (`code_reopened_for_pending_messages`)
    does this: it swaps a terminal vault tag back to `#agent-running` and
    reclaims the row via `update_status(..., STATUS_CLAIMED,
    attempt_id=<same attempt>)`. A projection-marker keyed on
    `(task, attempt, status)` could not tell a second, genuine drift on that
    same attempt apart from the first one it already marked settled, and
    would mask it forever — the reproduction that forced this rework. The
    watermark can tell them apart, because both the reopen and the second
    kill bump `last_activity_at` past the first sweep's stamp."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic same-attempt reopen task", status="cancelled",
        tags=["claude_code", FAILED_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_CLAIMED, routing="claude_code")
    attempt_id = session.attempt_id
    # Settles via the ordinary (non-kill) path — the tag is already correct,
    # so the first sweep has nothing to heal, only to examine and stamp.
    assert sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=attempt_id, turn_id=session.turn_id,
    )

    worker = _make_worker(sessions, manager)
    first_healed = worker._reconcile_lifecycle_drift()
    assert first_healed == 0
    assert sessions.list_terminal_unswept(since=0, limit=10) == []

    # Reopened for a pending follow-up message on the SAME attempt: the tag
    # swaps back to running and the row reclaims to CLAIMED, exactly like
    # `code_reopened_for_pending_messages` — no new attempt id is minted.
    manager.update(task.id, status="in_progress", tags=["claude_code", RUNNING_TAG])
    assert sessions.update_status(task.id, STATUS_CLAIMED, attempt_id=attempt_id)

    # Killed while parked again — terminal a second time on the SAME
    # attempt and at the same status as the first settle.
    assert sessions.mark_cancelled(task.id, attempt_id=attempt_id, reason="operator_killed")
    # Force a deterministic advance past the first stamp for the same
    # same-second-collision reason as the cross-attempt version of this
    # test above; the property under test is the comparison, not the clock.
    with sessions._connect() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity_at = last_activity_at + 1 WHERE task_id = ?",
            (task.id,),
        )

    second_healed = worker._reconcile_lifecycle_drift()
    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_drift_sweep_batch_self_advances_past_non_actionable_rows(tmp_path):
    """A backlog of settled (non-actionable) candidate rows must not
    permanently crowd a genuinely-drifted row out of the bounded per-tick
    batch. Seed more settled rows than `_LIFECYCLE_DRIFT_SWEEP_LIMIT`, all
    with an older `last_activity_at` than one genuinely-drifted row, and
    confirm the drifted row is still reached and healed within a bounded
    number of ticks: `list_terminal_unswept` orders oldest-first, so
    each settled row gets a `drift_swept_at` stamp and clears the way for
    the next tick's batch to reach further down the queue."""
    from api.services.agent_worker.worker import _LIFECYCLE_DRIFT_SWEEP_LIMIT

    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    base_time = int(time.time()) - 10_000

    backlog_size = _LIFECYCLE_DRIFT_SWEEP_LIMIT + 5
    for i in range(backlog_size):
        task = manager.create(
            f"Synthetic settled task {i}", status="done",
            tags=["claude_code", COMPLETED_TAG, "accepted"],
        )
        session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
        sessions.update_status(
            task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        with sessions._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE task_id = ?",
                (base_time + i, task.id),
            )

    drifted_task = manager.create(
        "Synthetic genuinely drifted task", status="blocked",
        tags=["claude_code", RUNNING_TAG],
    )
    drifted_session = sessions.create(
        drifted_task.id, status=STATUS_BLOCKED, routing="claude_code",
    )
    assert sessions.mark_cancelled(
        drifted_task.id, attempt_id=drifted_session.attempt_id, reason="operator_killed",
    )
    with sessions._connect() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity_at = ? WHERE task_id = ?",
            (base_time + backlog_size + 100, drifted_task.id),
        )

    worker = _make_worker(sessions, manager)
    healed_total = 0
    for _ in range(3):  # bounded: one tick to drain the backlog, one to reach the drift
        healed_total += worker._reconcile_lifecycle_drift()
        if FAILED_TAG in manager.get(drifted_task.id).tags:
            break

    refreshed = manager.get(drifted_task.id)
    assert healed_total == 1
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


def test_tick_runs_the_lifecycle_drift_sweep_even_when_the_spend_cap_blocks(tmp_path, monkeypatch):
    """The sweep reconciles existing state and never starts new work, so it
    must run even on a tick where `can_start_task` blocks everything else
    (daily cap reached, or the worker paused) — a parked drift must still
    heal on a day the worker can't afford to start anything.
    `test_tick_wires_in_the_lifecycle_drift_sweep` above hardcodes
    `can_start_task=lambda estimate: True`, so it cannot catch a regression
    that moves the sweep call after the cap gate; this test pins that the
    call site stays before it."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic capped-day task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )

    worker = _make_worker(sessions, manager)
    worker.spend_tracker = SimpleNamespace(
        can_start_task=lambda estimate: False,
        daily_cap_dollars=0.0,
        today_total=lambda: 0.0,
    )
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

    handled = worker.tick()

    refreshed = manager.get(task.id)
    assert handled == 0  # the cap gate still short-circuits the rest of the tick
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


class TestListTerminalUnsweptHasVaultTaskGate:
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
        assert sessions.list_terminal_unswept(since=0, limit=10) == []

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
        assert sessions.list_terminal_unswept(since=0, limit=10) == []

    def test_includes_an_ordinary_vault_backed_session(self, tmp_path):
        """Control case — the gate must not accidentally exclude everything."""
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create("normal-task")
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        rows = sessions.list_terminal_unswept(since=0, limit=10)
        assert {row.task_id for row in rows} == {"normal-task"}


class TestListTerminalUnsweptNullAttemptRows:
    """A legacy session with no attempt recorded at all (`attempt_id IS
    NULL`, e.g. a row from before the attempt/turn columns were backfilled)
    is exactly as good a drift-sweep candidate as any other terminal
    session, and exactly as excludable once examined. The watermark
    predicate compares `drift_swept_at`/`last_activity_at` on the row
    itself and never joins on `attempt_id`, so — unlike the projection-dedupe
    subquery this mechanism replaced — there is no NULL-safety hazard here
    to begin with; this pins that the NULL-attempt case still behaves
    correctly under the new mechanism."""

    def test_null_attempt_row_is_a_candidate_and_is_excluded_once_swept(self, tmp_path):
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create("null-attempt-task")
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        with sessions._connect() as conn:
            conn.execute(
                "UPDATE sessions SET attempt_id = NULL WHERE task_id = ?",
                (session.task_id,),
            )

        rows = sessions.list_terminal_unswept(since=0, limit=10)
        assert {row.task_id for row in rows} == {"null-attempt-task"}
        assert rows[0].attempt_id is None

        sessions.mark_drift_swept(session.session_id)

        assert sessions.list_terminal_unswept(since=0, limit=10) == []


class TestDriftSweepWatermarkInvalidation:
    """The watermark's whole safety property is comparative, not just
    presence: a stamped row must stay excluded from the candidate query
    while `last_activity_at` is unchanged (the cheap-tick property this
    mechanism exists for — no repeat HTTP fetch on a settled row), and must
    become a candidate again as soon as `last_activity_at` advances past the
    stamp. Reverting the predicate to `drift_swept_at IS NOT NULL` (dropping
    the comparison against `last_activity_at`) would exclude a stamped row
    forever, even after a genuine change, and reddens both tests below."""

    def test_stamped_row_stays_excluded_until_last_activity_at_advances(self, tmp_path):
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create("watermark-task")
        sessions.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        assert sessions.list_terminal_unswept(since=0, limit=10) != []

        sessions.mark_drift_swept(session.session_id)
        # Cheap-tick property: nothing has changed since the stamp, so the
        # row is no longer a candidate.
        assert sessions.list_terminal_unswept(since=0, limit=10) == []

        # A genuine change bumps `last_activity_at` past the stamp. Advance
        # it deterministically rather than relying on wall-clock time
        # passing between two calls in the same test.
        with sessions._connect() as conn:
            stamped_at = conn.execute(
                "SELECT drift_swept_at FROM sessions WHERE task_id = ?",
                (session.task_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE task_id = ?",
                (stamped_at + 1, session.task_id),
            )
        rows = sessions.list_terminal_unswept(since=0, limit=10)
        assert {row.task_id for row in rows} == {"watermark-task"}

    def test_worker_does_not_refetch_a_stamped_row_until_last_activity_at_advances(self, tmp_path):
        manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
        task = manager.create(
            "Synthetic watermark cost task", status="done",
            tags=["claude_code", COMPLETED_TAG, "accepted"],
        )
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")
        sessions.update_status(
            task.id, STATUS_FAILED, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        worker = _make_worker(sessions, manager)
        fetch_calls = []
        real_fetch = worker._fetch_task_or_absent

        def counting_fetch(task_id):
            fetch_calls.append(task_id)
            return real_fetch(task_id)

        worker._fetch_task_or_absent = counting_fetch

        first_healed = worker._reconcile_lifecycle_drift()
        assert first_healed == 0
        assert fetch_calls == [task.id]

        # A second tick with nothing changed must not re-fetch — this is
        # the watermark's whole cost argument (see `_LIFECYCLE_DRIFT_SWEEP_
        # WINDOW_S`'s comment on why a wide window is safe).
        second_healed = worker._reconcile_lifecycle_drift()
        assert second_healed == 0
        assert fetch_calls == [task.id]

        # A genuine change (reopened, then killed again) advances
        # `last_activity_at` past the stamp and must trigger a re-fetch.
        reopened = sessions.begin_new_execution(task.id)
        manager.update(task.id, status="in_progress", tags=["claude_code", RUNNING_TAG])
        assert sessions.mark_cancelled(
            task.id, attempt_id=reopened.attempt_id, reason="operator_killed",
        )
        with sessions._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity_at = last_activity_at + 1 WHERE task_id = ?",
                (task.id,),
            )

        third_healed = worker._reconcile_lifecycle_drift()
        assert third_healed == 1
        assert fetch_calls == [task.id, task.id]
