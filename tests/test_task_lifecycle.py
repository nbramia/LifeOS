"""Synthetic lifecycle coordination tests (no live services)."""
from __future__ import annotations

import threading

import pytest

from types import SimpleNamespace

from api.services.agent_board import BLOCKED_TAG, COMPLETED_TAG, RUNNING_TAG, derive_lane
from api.services.agent_worker.lifecycle import FAILED_TAG, LifecycleEvent, LifecycleProjector
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
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

BUDGET_EXCEEDED_TAG = "agent-budget-exceeded"


def _make_worker(sessions: SessionStore, manager: TaskManager):
    """A `Worker` stub wired just enough for the lifecycle-drift sweep and
    (for the handful of tests that call it) `tick()`: real store + real
    projector, no HTTP/Telegram/executors.

    The HTTP helpers read and write `manager` directly instead of hitting
    the API, each standing in for one endpoint and returning what that
    endpoint's payload would reduce to: `_list_tasks_by_tag` yields the
    `task.to_dict()` shape of `resp.json()["tasks"]`, and `_swap_tag`
    returns the route's `swapped` flag.

    This stub pins the sweep's *selection* logic cheaply — which cards it
    picks and which it leaves alone. It deliberately does not stand in for
    the route layer's own guards, so it cannot prove a write the sweep
    issues is one the API would accept; that is
    `test_drift_sweep_heals_through_the_real_task_routes`' job.
    """
    from api.services.agent_worker.worker import Worker

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker.lifecycle_projector = LifecycleProjector(sessions, manager)
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    worker._list_tasks_by_tag = lambda tag: [t.to_dict() for t in manager.list_tasks(tag=tag)]
    worker._swap_tag = lambda task_id, from_tag, to_tag: manager.swap_tag(
        task_id, from_tag, to_tag,
    )
    worker._complete_task = lambda task_id: manager.complete(task_id) is not None
    worker._set_task_status = lambda task_id, status: manager.update(
        task_id, status=status,
    ) is not None
    # `tick()` also runs the off-dispatch resource-cleanup sweep; none of
    # these tests give a session an `execution_spec`, so it never reaches
    # `_executor_registry` or `_pr_state_cache` — only the interval gate
    # needs a real value.
    worker._last_resource_cleanup = 0.0
    worker._pr_state_cache = {}
    return worker


def _make_route_worker(tmp_path, monkeypatch):
    """A `Worker` whose vault reads and writes go through the REAL FastAPI
    task routes, over a temp vault and a temp session DB.

    Returns `(worker, manager, sessions)`. `worker._http` is a `TestClient`
    bound to the app and `worker.api_base` is empty, so every
    `self._http.<verb>(f"{self.api_base}/api/tasks/...")` call in the worker
    resolves against the app itself — nothing is stubbed between the sweep
    and the route handlers, including their claim-tag guards.
    """
    from fastapi.testclient import TestClient

    from api import main as api_main
    from api.routes import tasks as tasks_route
    import api.services.task_manager as task_manager_module
    from api.services.agent_worker.worker import Worker, _WorkerLifecycleTaskManager

    manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json",
    )
    monkeypatch.setattr(task_manager_module, "_task_manager", manager)
    sessions = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(tasks_route, "_session_store", sessions)

    worker = Worker.__new__(Worker)
    worker.session_store = sessions
    worker._http = TestClient(api_main.app)
    worker.api_base = ""
    # Same wiring `Worker.__init__` uses: the projector writes through the
    # worker's own HTTP client, not straight into a TaskManager.
    worker.lifecycle_projector = LifecycleProjector(
        sessions, _WorkerLifecycleTaskManager(worker),
    )
    return worker, manager, sessions


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


def test_project_session_status_cas_guard_fires_and_terminal_status_self_heals(tmp_path):
    """`_project_session_status` — the hook `update_status` fires on every
    terminal/blocked write — must check its CAS against the task's live
    state, not the same fetch it derived `expected_version` from. Stale
    `_fetch_task` output simulates the session's view of the task being
    behind; the manager (what `transition` re-fetches through) has already
    moved on. The write must back off, leaving the vault tag stranded —
    and the drift sweep, which reads the vault's current tags rather than
    any projection bookkeeping, must then heal it on the next tick."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic terminal-status task", status="in_progress",
        tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.create(task.id, status=STATUS_RUNNING, routing="claude_code")

    worker = _make_worker(sessions, manager)
    stale_snapshot = task.to_dict()
    worker._fetch_task = lambda task_id: stale_snapshot

    # Operator edits the task after the stale snapshot was captured but
    # before the projection's CAS check runs.
    manager.update(task.id, description="Operator edited before terminal write")
    edited = manager.get(task.id)

    projected = worker._project_session_status(
        task.id, "completed", attempt_id=session.attempt_id, turn_id=session.turn_id,
    )

    refreshed = manager.get(task.id)
    assert projected is False
    assert refreshed.description == "Operator edited before terminal write"
    assert refreshed.tags == edited.tags
    assert RUNNING_TAG in refreshed.tags  # still drifted — the write backed off

    # Mirrors what `update_status` does immediately before firing this hook
    # in production: the session row itself still advances to terminal even
    # though the vault projection above was left pending.
    assert sessions.update_status(
        task.id, "completed", attempt_id=session.attempt_id, turn_id=session.turn_id,
    )

    # Next tick: the sweep sees a terminal session still tagged #agent-running
    # and heals it under its own event_id, unblocked by the conflicted one.
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    healed = worker._reconcile_lifecycle_drift()

    final = manager.get(task.id)
    assert healed == 1
    assert COMPLETED_TAG in final.tags
    assert RUNNING_TAG not in final.tags


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


def test_human_queue_wake_cas_guard_fires_and_self_heals_on_next_tick(tmp_path):
    """The wake replay's CAS check must compare against the task's live
    state, not the same fetch it derived `expected_version` from. A stale
    `_fetch_task` simulates an operator edit landing between the listing
    and the check: the projection must back off, the session must NOT
    rearm to CLAIMED, and the wait must stay resolved-but-unconsumed so the
    next tick retries it. No other mechanism recovers this path (it targets
    a non-terminal STATUS_RUNNING rearm, so the terminal drift sweep never
    sees it) — retrying with a fresh fetch on the next tick is what
    recovers it, proven here directly rather than assumed."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create("Synthetic contested wake task", status="blocked", tags=["agent-blocked", "local"])
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
    worker._dispatch = lambda payload: dispatched.append(payload)

    stale_snapshot = task.to_dict()
    worker._fetch_task = lambda task_id: stale_snapshot

    # Operator edits the task in the window between that stale snapshot
    # and the CAS check this tick performs.
    manager.update(task.id, description="Operator edited during wait")
    edited = manager.get(task.id)

    worker._replay_wait_wakeups()

    conflicted = manager.get(task.id)
    assert conflicted.description == "Operator edited during wait"
    assert conflicted.tags == edited.tags
    assert sessions.get(task.id).status == STATUS_BLOCKED  # rearm did not happen
    assert not dispatched
    resolved = sessions.list_resolved_waits()
    assert len(resolved) == 1 and resolved[0]["wait_id"] == wait_id  # left for retry

    # Next tick: a fresh, current fetch — no concurrent edit racing this time.
    worker._fetch_task = lambda task_id: (
        manager.get(task_id).to_dict() if manager.get(task_id) else None
    )
    worker._replay_wait_wakeups()

    refreshed_task = manager.get(task.id)
    refreshed_session = sessions.get(task.id)
    assert refreshed_task.status == "in_progress"
    assert refreshed_session.status == STATUS_CLAIMED
    assert dispatched and dispatched[0]["id"] == task.id
    assert sessions.list_resolved_waits() == []


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


def test_drift_sweep_does_not_clobber_a_concurrent_operator_edit(tmp_path, monkeypatch):
    """An operator pulling a card back off the agent between the sweep's
    tag listing and its write must win: the sweep must leave that card
    completely untouched, tags and status alike.

    The guard is `POST /swap-tag` refusing to swap a `from` tag the card no
    longer carries, and it is load-bearing for the status write too — a
    card whose swap did not apply must not then have its status forced to
    `cancelled`/`done` behind the operator's back. Driven through the real
    routes, since the guard lives there; the tag listing is frozen at this
    tick's (now stale) snapshot so the operator's retag genuinely lands in
    the window between the read and the write."""
    worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
    task = manager.create(
        "Synthetic concurrently-retagged task", status="blocked",
        tags=["claude_code", RUNNING_TAG],
    )
    session = sessions.create(task.id, status=STATUS_BLOCKED, routing="claude_code")
    assert sessions.mark_cancelled(
        task.id, attempt_id=session.attempt_id, reason="operator_killed",
    )

    # Freeze the sweep's tag listing at this tick's snapshot.
    stale_snapshot = worker._list_tasks_by_tag(RUNNING_TAG)
    assert [t["id"] for t in stale_snapshot] == [task.id]
    worker._list_tasks_by_tag = lambda tag: stale_snapshot if tag == RUNNING_TAG else []

    # The operator hands the card back to the queue between the listing and
    # the sweep's write.
    assert manager.swap_tag(task.id, RUNNING_TAG, "agent")
    edited = manager.get(task.id)

    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == edited.tags
    assert FAILED_TAG not in refreshed.tags
    assert refreshed.status == edited.status  # no forced `cancelled` write
    assert refreshed.updated_at == edited.updated_at  # nothing was written at all


def test_drift_sweep_heals_through_the_real_task_routes(tmp_path, monkeypatch):
    """End-to-end proof that the heal the sweep issues is a write the API
    actually accepts.

    Every card the sweep targets carries `#agent-running` or
    `#agent-blocked`, which makes it claimed as far as `agent_board` is
    concerned, and the task routes refuse a lifecycle-tag write on a
    claimed card through anything but `POST /swap-tag`. A sweep wired to a
    `TaskManager` in-process never meets that guard, so this test drives
    the sweep against the real FastAPI app over a temp vault: the writes it
    issues are exactly the HTTP requests the deployed worker sends.

    Covers both `from` tags and all three terminal session statuses, since
    the `from` tag comes from whichever listing produced the candidate and
    the status picks both the terminal tag and the follow-up status
    write."""
    worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)

    cases = [
        (RUNNING_TAG, STATUS_RUNNING, STATUS_FAILED, FAILED_TAG, "cancelled"),
        (BLOCKED_TAG, STATUS_BLOCKED, STATUS_COMPLETED, COMPLETED_TAG, "done"),
        (RUNNING_TAG, STATUS_RUNNING, STATUS_BUDGET_EXCEEDED, BUDGET_EXCEEDED_TAG, "cancelled"),
    ]
    tasks = []
    for drifted_tag, live_status, terminal_status, _tag, _status in cases:
        task = manager.create(
            f"Synthetic route-level {terminal_status} task",
            status="in_progress", tags=["claude_code", drifted_tag],
        )
        session = sessions.create(task.id, status=live_status, routing="claude_code")
        # A raw terminal status write, exactly as an operator kill's
        # `mark_cancelled` / `teardown_session` leaves the row: no projector
        # hook fires, so Markdown is never touched and the card drifts.
        assert sessions.update_status(
            task.id, terminal_status,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        assert drifted_tag in manager.get(task.id).tags
        tasks.append(task)

    healed = worker._reconcile_lifecycle_drift()

    assert healed == 3
    for task, (drifted_tag, _live, _terminal, expected_tag, expected_status) in zip(tasks, cases):
        refreshed = manager.get(task.id)
        assert expected_tag in refreshed.tags, task.description
        assert drifted_tag not in refreshed.tags, task.description
        assert refreshed.status == expected_status, task.description


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

    # The live-kill path leaves the tag terminal, so by the time the sweep runs
    # the task carries neither RUNNING_TAG nor BLOCKED_TAG and is not even a
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
    must never be touched. This includes a task reopened for a resumed
    follow-up turn, which flips the tag back to #agent-running while leaving
    its session row non-terminal — superficially similar to a
    freshly-dispatched task, but excluded by session status alone: the
    sweep only ever heals a task whose session has reached a terminal
    status.

    Spying on `_swap_tag` pins the guard itself rather than just its
    outcome: the sweep must skip a live session before issuing any write at
    all, for any of the three statuses. `healed == 0` alone is weaker —
    a swap that no-ops for an unrelated reason would also leave it at 0."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic live task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")
    sessions.create(task.id, status=live_status, routing="claude_code")

    worker = _make_worker(sessions, manager)
    swaps = []
    real_swap_tag = worker._swap_tag
    worker._swap_tag = lambda task_id, from_tag, to_tag: (
        swaps.append((task_id, from_tag, to_tag)) or real_swap_tag(task_id, from_tag, to_tag)
    )

    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert swaps == []  # the sweep's own guard must skip before ever writing
    assert healed == 0
    assert RUNNING_TAG in refreshed.tags
    assert refreshed.tags == task.tags
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_leaves_a_settled_task_alone(tmp_path):
    """A task the operator already accepted (`#accepted #agent-completed`,
    status done) must be left byte-identical, even though its session row
    is terminal and was never explicitly reconciled by anything (e.g. an
    operator edited the tags directly). The sweep's candidate set is every
    task currently carrying `RUNNING_TAG`/`BLOCKED_TAG`, and a settled task
    carries neither, so it's never even listed as a candidate."""
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


def test_drift_sweep_leaves_a_tagged_task_with_no_backing_session_alone(tmp_path):
    """A task can carry a non-terminal lifecycle tag with no backing session
    row at all — an operator hand-editing the tag directly in Markdown, or a
    session row that was pruned after the fact. `session_store.get` returns
    None for it, and the sweep must skip it without raising."""
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    task = manager.create(
        "Synthetic tag-only task", status="in_progress", tags=["claude_code", RUNNING_TAG],
    )
    sessions = SessionStore(tmp_path / "sessions.db")

    worker = _make_worker(sessions, manager)
    healed = worker._reconcile_lifecycle_drift()

    refreshed = manager.get(task.id)
    assert healed == 0
    assert refreshed.tags == task.tags
    assert refreshed.updated_at == task.updated_at


def test_drift_sweep_survives_a_transient_tag_listing_failure_and_heals_on_the_next_tick(tmp_path):
    """A failure listing tagged tasks — an API blip, a timeout, the API
    restarting mid-tick after a deploy — must corrupt nothing: the sweep
    heals nothing that tick, writes no durable state, and heals normally on
    the next tick once the API recovers."""
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
    across separate reopened executions. Each tick derives its candidate
    set fresh from the current vault tags, so a second, later drift on the
    same task heals exactly like the first."""
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
    attempt_id=<same attempt>)`. The sweep tracks no per-attempt state: the
    reopen removes the task from the RUNNING_TAG/BLOCKED_TAG candidate set
    as soon as the session goes non-terminal, and the re-kill on that same
    attempt puts it right back, so the second drift heals exactly like the
    first."""
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
        effective_cap_dollars=lambda: 0.0,
        notified_cap_dollars=lambda: 0.0,
        mark_cap_notified=lambda cap: None,
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
    real vault row, so projecting through TaskManager would 404. The
    sweep's candidate set comes from real vault tasks (the tag listing), so
    a synthetic task id is never a candidate in the first place; the gate
    still checks the session directly for defense in depth, pinned here by
    constructing a real vault task whose session row happens to be
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


class TestLifecycleProjectorRealHTTPRoute:
    """`LifecycleProjector.transition` writes through `PUT /api/tasks/{id}`
    via `_WorkerLifecycleTaskManager` (api/services/agent_worker/worker.py).
    A projector wired to an in-process `TaskManager` never meets that
    route's claimed-card guard at all, so these drive the projector against
    the real FastAPI route over a temp vault, exactly as
    `test_drift_sweep_heals_through_the_real_task_routes` does for the
    drift sweep's own `/swap-tag` writes."""

    def _claimed_task_and_session(self, manager, sessions, *, tag=RUNNING_TAG, session_status=STATUS_RUNNING):
        task = manager.create(
            "Synthetic route-level lifecycle task", status="in_progress",
            tags=["claude", tag],
        )
        session = sessions.create(task.id, status=session_status, routing="claude_code")
        return task, session

    def test_worker_completed_transition_on_claimed_card_reaches_applied(self, tmp_path, monkeypatch):
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, session = self._claimed_task_and_session(manager, sessions)
        event = LifecycleEvent(
            event_id="event-completed", task_id=task.id,
            session_id=session.session_id, attempt_id=session.attempt_id,
            target_status=STATUS_COMPLETED, expected_version=task.updated_at,
        )
        assert worker.lifecycle_projector.transition(event) is True
        refreshed = manager.get(task.id)
        assert refreshed.status == "done"
        assert COMPLETED_TAG in refreshed.tags
        assert RUNNING_TAG not in refreshed.tags
        assert sessions.projection_applied(event.event_id)

    def test_worker_failed_transition_on_claimed_card_reaches_applied(self, tmp_path, monkeypatch):
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, session = self._claimed_task_and_session(manager, sessions)
        event = LifecycleEvent(
            event_id="event-failed", task_id=task.id,
            session_id=session.session_id, attempt_id=session.attempt_id,
            target_status=STATUS_FAILED, expected_version=task.updated_at,
        )
        assert worker.lifecycle_projector.transition(event) is True
        refreshed = manager.get(task.id)
        assert refreshed.status == "cancelled"
        assert FAILED_TAG in refreshed.tags
        assert RUNNING_TAG not in refreshed.tags
        assert sessions.projection_applied(event.event_id)

    def test_worker_blocked_transition_on_claimed_card_reaches_applied(self, tmp_path, monkeypatch):
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, session = self._claimed_task_and_session(manager, sessions)
        event = LifecycleEvent(
            event_id="event-blocked", task_id=task.id,
            session_id=session.session_id, attempt_id=session.attempt_id,
            target_status=STATUS_BLOCKED, expected_version=task.updated_at,
            wait_type=WAIT_OPERATOR,
        )
        assert worker.lifecycle_projector.transition(event) is True
        refreshed = manager.get(task.id)
        assert refreshed.status == "blocked"
        assert BLOCKED_TAG in refreshed.tags
        assert RUNNING_TAG not in refreshed.tags
        assert sessions.projection_applied(event.event_id)

    def test_human_assignee_reassignment_on_claimed_card_is_still_refused(self, tmp_path, monkeypatch):
        """The worker-actor marker only ever covers the projector's own
        lifecycle-tag transition — an actual assignee/engine change on a
        claimed card, even carrying the same marker, is the human-
        reassignment move this guard exists to block."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, _session = self._claimed_task_and_session(manager, sessions)
        response = worker._http.put(f"/api/tasks/{task.id}", json={
            "tags": ["codex", RUNNING_TAG], "actor": "worker",
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        assert manager.get(task.id).tags == task.tags

    def test_worker_actor_marker_does_not_exempt_an_engine_reassignment(self, tmp_path, monkeypatch):
        """The worker-actor carve-out never applies to a request that also
        changes the assignee-tag set — even one that otherwise looks
        exactly like the worker's own terminal transition (claimed card,
        status actually changing, a tracked claim tag replacing the
        one on file). Changing the assignee tag is what a reassignment
        is; the carve-out must never cover it, marker or not."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, _session = self._claimed_task_and_session(manager, sessions)
        response = worker._http.put(f"/api/tasks/{task.id}", json={
            "status": "done", "tags": ["codex", COMPLETED_TAG], "actor": "worker",
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        refreshed = manager.get(task.id)
        assert refreshed.tags == task.tags
        assert refreshed.status == task.status

    def test_worker_actor_marker_requires_an_actual_status_change(self, tmp_path, monkeypatch):
        """The worker-actor carve-out never applies to a request whose
        `status` doesn't actually move off the task's current one — a
        claim tag can never be added on the strength of the marker alone
        without the status transition it's supposed to accompany, even
        though the assignee-tag set and the claimed state both look
        exactly like the worker's own write."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task, _session = self._claimed_task_and_session(manager, sessions)
        response = worker._http.put(f"/api/tasks/{task.id}", json={
            "status": task.status, "tags": ["claude", COMPLETED_TAG], "actor": "worker",
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        refreshed = manager.get(task.id)
        assert refreshed.tags == task.tags

    def test_worker_actor_marker_cannot_manufacture_a_claim_on_an_unclaimed_card(self, tmp_path, monkeypatch):
        """A worker-actor marker on a card the worker never actually
        claimed must not add a claim tag — the exemption only ever applies
        to a card that is already claimed."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task = manager.create("Synthetic unclaimed task", status="todo", tags=["me"])
        response = worker._http.put(f"/api/tasks/{task.id}", json={
            "status": "done", "tags": ["me", COMPLETED_TAG], "actor": "worker",
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        assert manager.get(task.id).status == "todo"


class TestDeleteTaskPurgesWorkerBookkeeping:
    """`DELETE /api/tasks/{id}` clears the worker's own session-store
    bookkeeping for the deleted task, through `SessionStore.purge_task`
    (called by the route), so nothing keeps acting on it afterward."""

    def test_delete_stops_replay_from_retrying_an_unapplied_projection(self, tmp_path, monkeypatch):
        """A crash-recovery marker left `pending` by a prior process must
        not be picked back up by `replay_pending` once its task is gone —
        proven by the projector's `transition` never being invoked for it,
        not merely by the row's absence (a row could vanish while some
        other path still retried the same transition by other means)."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task = manager.create("Synthetic task with an unapplied projection", tags=["local"])
        session = sessions.create(task.id, routing="local")
        event_id = "pending-event"
        assert sessions.begin_projection(
            event_id, task_id=task.id, session_id=session.session_id,
            attempt_id=session.attempt_id, expected_version=task.updated_at,
            target_status=STATUS_COMPLETED,
            payload={
                "wait_type": None, "wait_reason": "", "human_card_id": None,
                "dependencies": [], "reason": "",
            },
        )
        assert sessions.list_pending_projections() != []

        response = worker._http.delete(f"/api/tasks/{task.id}")
        assert response.status_code == 200

        projector = worker.lifecycle_projector
        attempted: list[str] = []
        original_transition = projector.transition

        def _spy_transition(event, **kwargs):
            attempted.append(event.event_id)
            return original_transition(event, **kwargs)

        projector.transition = _spy_transition

        replayed = projector.replay_pending()

        assert event_id not in attempted
        assert replayed == 0
        assert sessions.list_pending_projections() == []

    def test_delete_stops_the_worker_fetching_the_deleted_task(self, tmp_path, monkeypatch):
        """A live (non-terminal) session for the deleted task must not
        survive the delete — proven by `_fetch_task` never being called for
        it across a startup recovery pass, not merely by the session row's
        absence."""
        from api.services.agent_worker.transcript_store import TranscriptStore

        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        worker.transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
        task = manager.create(
            "Synthetic task with a live session", status="in_progress",
            tags=["local", RUNNING_TAG],
        )
        sessions.create(task.id, status=STATUS_RUNNING, routing="local")

        fetched: list[str] = []
        original_fetch_task = worker._fetch_task

        def _spy_fetch_task(task_id):
            fetched.append(task_id)
            return original_fetch_task(task_id)

        worker._fetch_task = _spy_fetch_task

        response = worker._http.delete(f"/api/tasks/{task.id}")
        assert response.status_code == 200
        assert sessions.get(task.id) is None

        worker.resume_pending()

        assert task.id not in fetched

    def test_delete_with_no_session_behaves_exactly_as_before(self, tmp_path, monkeypatch):
        """The common case — a task with no agent session at all — must
        stay a plain, unaffected delete."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        task = manager.create("Synthetic task with no session", tags=["local"])
        assert sessions.get(task.id) is None

        response = worker._http.delete(f"/api/tasks/{task.id}")

        assert response.status_code == 200
        assert response.json() == {"status": "deleted", "id": task.id}
        assert manager.get(task.id) is None
        assert sessions.get(task.id) is None

    def test_delete_of_missing_task_still_404s(self, tmp_path, monkeypatch):
        """A delete for a task id that was never created reports not-found,
        unaffected by the added purge step."""
        worker, _manager, _sessions = _make_route_worker(tmp_path, monkeypatch)

        response = worker._http.delete("/api/tasks/does-not-exist")

        assert response.status_code == 404

    def test_delete_leaves_an_unrelated_applied_projection_alone(self, tmp_path, monkeypatch):
        """Deleting one task must not touch an already-applied lifecycle
        projection belonging to a different, surviving task."""
        worker, manager, sessions = _make_route_worker(tmp_path, monkeypatch)
        surviving_task, surviving_session = (
            self._claimed_task_and_session(manager, sessions)
        )
        doomed_task = manager.create("Synthetic doomed task", tags=["local"])
        event = LifecycleEvent(
            event_id="surviving-event", task_id=surviving_task.id,
            session_id=surviving_session.session_id, attempt_id=surviving_session.attempt_id,
            target_status=STATUS_COMPLETED, expected_version=surviving_task.updated_at,
        )
        assert worker.lifecycle_projector.transition(event) is True
        assert sessions.projection_applied(event.event_id)

        response = worker._http.delete(f"/api/tasks/{doomed_task.id}")
        assert response.status_code == 200

        assert sessions.projection_applied(event.event_id)
        assert sessions.get(surviving_task.id) is not None
        assert manager.get(surviving_task.id) is not None

    @staticmethod
    def _claimed_task_and_session(manager, sessions, *, tag=RUNNING_TAG, session_status=STATUS_RUNNING):
        task = manager.create(
            "Synthetic route-level lifecycle task", status="in_progress",
            tags=["claude", tag],
        )
        session = sessions.create(task.id, status=session_status, routing="claude_code")
        return task, session
