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
    sweep: real store + real projector, no HTTP/Telegram/executors.

    `_list_tasks_by_tag` reads straight from `manager` instead of hitting
    the API, returning the same shape (`task.to_dict()`) the real method's
    `resp.json()["tasks"]` would.
    """
    from api.services.agent_worker.worker import Worker

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker.lifecycle_projector = LifecycleProjector(sessions, manager)
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    worker._list_tasks_by_tag = lambda tag: [t.to_dict() for t in manager.list_tasks(tag=tag)]
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

    # The live-kill path already fixed the tag before the sweep ever runs —
    # the task no longer carries RUNNING_TAG/BLOCKED_TAG, so it's not even a
    # candidate the sweep's tag listing would return.
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == reconciled.tags
    assert refreshed.updated_at == reconciled.updated_at


@pytest.mark.parametrize("live_status", [STATUS_CLAIMED, STATUS_RUNNING, STATUS_BLOCKED])
def test_drift_sweep_leaves_a_live_session_alone_regardless_of_status(tmp_path, live_status):
    """A task carrying #agent-running whose session is still non-terminal —
    CLAIMED, RUNNING, or parked at BLOCKED — is legitimately in progress and
    must never be touched. This includes a task legitimately reopened for a
    resumed follow-up turn, which flips the tag back to #agent-running while
    leaving its session row non-terminal — superficially similar to a
    freshly-dispatched task, but excluded by session status alone. This is
    the property that replaced the old attempt-scoping and reopen guards
    entirely: under the inverted sweep there's no bookkeeping to scope,
    because a live session is excluded on sight."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic live task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    sessions.create(task.id, status=live_status, routing="claude_code")

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert RUNNING_TAG in refreshed.tags
    assert refreshed.tags == task.tags
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_leaves_a_settled_task_alone(tmp_path):
    """A task the operator already accepted (`#accepted #agent-completed`,
    status done) must be left byte-identical, even though its session row
    is terminal and was never explicitly reconciled by anything (mirrors a
    historical session that predates this sweep entirely, or a task an
    operator edited directly). Under the inverted sweep this is structurally
    guaranteed rather than merely checked: the candidate set is every task
    currently carrying `RUNNING_TAG`/`BLOCKED_TAG`, and a settled task
    carries neither, so it's never even listed as a candidate in the first
    place."""
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

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == task.tags
    assert refreshed.status == task.status
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_survives_a_transient_tag_listing_failure_and_heals_on_the_next_tick(tmp_path):
    """A failure listing tagged tasks — an API blip, a timeout, the API
    restarting mid-tick after a deploy — must corrupt nothing: the sweep
    heals nothing that tick, writes no durable state, and heals normally on
    the next tick once the API recovers. This is the regression test for
    the entire class of bug (unavailable-vs-absent conflation) that
    dominated the mechanism this sweep replaced."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic flaky-listing task", status="blocked", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )
    before = manager.get(task.id)

    worker = _make_worker(sessions, manager)
    real_list_tasks_by_tag = worker._list_tasks_by_tag

    def _raise(tag):
        raise RuntimeError("simulated transient API failure")

    worker._list_tasks_by_tag = _raise
    first_healed = worker._reconcile_lifecycle_drift()
    assert first_healed == 0

    after_failure = manager.get(task.id)
    assert after_failure.tags == before.tags
    assert after_failure.updated_at == before.updated_at
    assert RUNNING_TAG in after_failure.tags  # still drifted, nothing corrupted

    worker._list_tasks_by_tag = real_list_tasks_by_tag
    second_healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


def test_drift_sweep_heals_a_second_drift_after_reopen_with_new_attempt(tmp_path):
    """A task can legitimately be killed at the same terminal status twice
    across separate reopened executions. The inverted sweep has no
    bookkeeping that could mask this: each tick re-derives its candidate set
    fresh from the current vault tags, so a second, later drift on the same
    task heals exactly like the first."""
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


def test_drift_sweep_heals_a_reopen_on_the_same_attempt_then_re_kill(tmp_path):
    """A task can also be reopened for a follow-up turn on the exact same
    attempt, with no `begin_new_execution` in between — the
    reopen-for-pending-messages path (`code_reopened_for_pending_messages`)
    does this: it swaps a terminal vault tag back to `#agent-running` and
    reclaims the row via `update_status(..., STATUS_CLAIMED,
    attempt_id=<same attempt>)`. A projection-marker keyed on
    `(task, attempt, status)` could not tell a second, genuine drift on that
    same attempt apart from the first one it already marked settled, and
    would mask it forever — the reproduction that forced this rework. The
    inverted sweep has no per-attempt state to confuse: the reopen removes
    the task from the RUNNING_TAG/BLOCKED_TAG candidate set as soon as the
    session goes non-terminal, and the re-kill puts it right back."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic same-attempt reopen task", status="cancelled",
        tags=["claude_code", FAILED_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_CLAIMED, routing="claude_code")
    attempt_id = session.attempt_id
    # Settles via the ordinary (non-kill) path — the tag is already correct
    # and the task doesn't carry RUNNING_TAG/BLOCKED_TAG, so the first sweep
    # has nothing to look at, let alone heal.
    assert sessions.update_status(
        task.id, STATUS_FAILED, attempt_id=attempt_id, turn_id=session.turn_id,
    )

    worker = _make_worker(sessions, manager)
    first_healed = worker._reconcile_lifecycle_drift()
    assert first_healed == 0

    # Reopened for a pending follow-up message on the SAME attempt: the tag
    # swaps back to running and the row reclaims to CLAIMED, exactly like
    # `code_reopened_for_pending_messages` — no new attempt id is minted.
    manager.update(task.id, status="in_progress", tags=["claude_code", RUNNING_TAG])
    assert sessions.update_status(task.id, STATUS_CLAIMED, attempt_id=attempt_id)

    # Killed while parked again — terminal a second time on the SAME
    # attempt and at the same status as the first settle.
    assert sessions.mark_cancelled(task.id, attempt_id=attempt_id, reason="operator_killed")

    second_healed = worker._reconcile_lifecycle_drift()
    refreshed = manager.get(task.id)
    assert second_healed == 1
    assert FAILED_TAG in refreshed.tags
    assert RUNNING_TAG not in refreshed.tags


# `test_drift_sweep_batch_self_advances_past_non_actionable_rows` (round 2)
# is deleted outright rather than adapted: it pinned that a bounded per-tick
# batch (`_LIFECYCLE_DRIFT_SWEEP_LIMIT`) doesn't let a backlog of settled
# rows crowd out a genuine drift. That limit no longer exists — the tag
# listing endpoint takes no limit/pagination param and returns every match,
# so there is no batch to starve and nothing left to pin.


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


class TestReconcileLifecycleDriftHasVaultTaskGate:
    """`has_vault_task` (`session.origin != 'operator'` and no
    `parent_session_id`) must exclude an operator root-spawn or a spawned
    child from being reconciled — they carry synthetic task ids with no
    real vault row, so projecting through TaskManager would 404. Under the
    inverted sweep the candidate set comes from real vault tasks (the tag
    listing), so this is ordinarily unreachable: a synthetic task id was
    never in the vault to carry a tag in the first place. It's still
    checked directly against the session for defense in depth — pinned here
    by constructing a real vault task whose session row happens to be
    operator-origin / child-parented, exercising the gate the same way
    `_reconcile_lifecycle_drift` does."""

    def test_excludes_operator_origin_session(self, tmp_path):
        manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
        task = manager.create(
            "Synthetic operator-origin task", status="blocked",
            tags=["claude_code", RUNNING_TAG],
        )
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create(task.id, status=STATUS_BLOCKED, origin="operator")
        assert sessions.mark_cancelled(
            task.id, attempt_id=session.attempt_id, reason="operator_killed",
        )

        worker = _make_worker(sessions, manager)
        healed = worker._reconcile_lifecycle_drift()

        refreshed = manager.get(task.id)
        assert healed == 0
        assert refreshed.tags == task.tags
        assert refreshed.updated_at == task.updated_at

    def test_excludes_spawned_child_session(self, tmp_path):
        manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
        task = manager.create(
            "Synthetic spawned-child task", status="blocked",
            tags=["claude_code", RUNNING_TAG],
        )
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create(
            task.id, status=STATUS_BLOCKED,
            parent_session_id="parent-session-1", root_session_id="parent-session-1",
        )
        assert sessions.mark_cancelled(
            task.id, attempt_id=session.attempt_id, reason="operator_killed",
        )

        worker = _make_worker(sessions, manager)
        healed = worker._reconcile_lifecycle_drift()

        refreshed = manager.get(task.id)
        assert healed == 0
        assert refreshed.tags == task.tags
        assert refreshed.updated_at == task.updated_at

    def test_includes_an_ordinary_vault_backed_session(self, tmp_path):
        """Control case — the gate must not accidentally exclude everything."""
        manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
        task = manager.create(
            "Synthetic ordinary task", status="blocked",
            tags=["claude_code", RUNNING_TAG],
        )
        sessions = SessionStore(tmp_path / "sessions.db")
        session = sessions.create(task.id, status=STATUS_BLOCKED)
        assert sessions.mark_cancelled(
            task.id, attempt_id=session.attempt_id, reason="operator_killed",
        )

        worker = _make_worker(sessions, manager)
        healed = worker._reconcile_lifecycle_drift()

        refreshed = manager.get(task.id)
        assert healed == 1
        assert FAILED_TAG in refreshed.tags


# `TestListTerminalUnsweptNullAttemptRows` (round 2) and
# `TestDriftSweepWatermarkInvalidation` (round 3) are deleted outright
# rather than adapted: both pinned NULL-safety and invalidation properties
# of query predicates (`p.attempt_id IS s.attempt_id`, then
# `drift_swept_at`/`last_activity_at`) that belonged entirely to the
# candidate-filter mechanisms this rework removed. The inverted sweep has
# no equivalent query to be NULL-unsafe in, and no stamp to invalidate —
# `session.attempt_id` is only ever read to build the outgoing
# `LifecycleEvent`, exactly as every other terminal-write call site in
# `worker.py` already does, so there is nothing specific to this sweep left
# to pin.
