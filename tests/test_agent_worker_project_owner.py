"""Persistent project owner core: the durable `project_owner_state` table,
`Worker._reconcile_project_owners`'s per-tick wake reconciler, native CLI
continuation, and restart safety.

Covers:
  * `owner_state()` classification, including that a failed/budget-exceeded
    child (task status "cancelled" with an `agent-failed`/
    `agent-budget-exceeded` tag) is reported as "failed", not "cancelled".
  * Several child events within the quiet window coalesce into one wake
    listing all of them.
  * An event while the owner is mid-turn produces no wake until the turn
    ends, then exactly one.
  * `begin_new_execution`'s CAS: concurrent callers, only one wins.
  * A paused project accumulates events without waking; resuming wakes
    once if anything piled up, not at all otherwise.
  * The daily spend cap blocks wakes (the reconciler never runs while
    capped).
  * A failed wake redelivers the same events; two consecutive failures
    auto-pause the project with one notice; a budget-exceeded turn pauses
    immediately.
  * `begin_new_execution` is called with the owner's prior
    `execution_request`, so its `working_dir` survives the CAS (regression
    for the spec-clearing trap).
  * `resume_pending` does not fail an operator-origin CLAIMED session whose
    wake was queued but never dispatched before a restart.
  * The owner guidance/prompt text describes a persistent owner, not a
    bounded coordinator.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from api.services.agent_worker.delegation import PROJECT_TASK_GUIDANCE
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    Session,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from api.services.task_projects import (
    CANCEL_OPERATION_FIELD,
    COORDINATOR_SESSION_FIELD,
    HANDOFF_OPERATION_FIELD,
    PARENT_ID_FIELD,
    PROJECT_PAUSE_REASON_FIELD,
    PROJECT_PAUSED_FIELD,
    owner_state,
)

pytestmark = pytest.mark.unit

_OWNER_TASK_ID = "project_p1_op1"


class FakeApi:
    """In-memory stand-in for `/api/tasks` and `/project/pause` — the only
    endpoints the owner reconciler calls."""

    def __init__(self, tasks=None):
        self.tasks = {t["id"]: t for t in (tasks or [])}
        self.pause_calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            return httpx.Response(
                200, json={"tasks": list(self.tasks.values()), "total": len(self.tasks)},
            )
        if request.method == "POST" and request.url.path.endswith("/project/pause"):
            task_id = request.url.path.split("/")[-3]
            payload = json.loads(request.content) if request.content else {}
            self.pause_calls.append((task_id, payload))
            task = self.tasks.get(task_id)
            if task is not None:
                task["fields"][PROJECT_PAUSED_FIELD] = "true"
                task["fields"][PROJECT_PAUSE_REASON_FIELD] = payload.get("reason", "operator")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


def _child(task_id: str, project_id: str, description: str, *, status="todo", tags=None) -> dict:
    return {
        "id": task_id,
        "description": description,
        "status": status,
        "tags": list(tags or []),
        "fields": {PARENT_ID_FIELD: project_id},
    }


def _project(
    task_id: str, description: str, *, owner_session_id: str, tags=("claude",),
    paused: bool = False, pause_reason: str | None = None,
) -> dict:
    fields = {COORDINATOR_SESSION_FIELD: owner_session_id}
    if paused:
        fields[PROJECT_PAUSED_FIELD] = "true"
        fields[PROJECT_PAUSE_REASON_FIELD] = pause_reason or "operator"
    return {
        "id": task_id, "description": description, "status": "in_progress",
        "tags": list(tags), "fields": fields,
    }


def _make_worker(
    tmp_path: Path, api: FakeApi, *, daily_cap_dollars: float = 100.0,
    local_executor=None, remote_executor=None, hermes_executor=None,
    managed_executor=None, cli_pool=None,
) -> Worker:
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []

    def _send(text, chat_id=None, bot=None):
        sent.append(text)
        return True

    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    # Every stub executor in this file honors the real contract of calling
    # `begin_executor_turn` itself -- wire the store into any that need it
    # (it doesn't exist yet at stub-construction time).
    for stub in (local_executor, remote_executor, hermes_executor, managed_executor):
        if stub is not None and getattr(stub, "session_store", "unset") is None:
            stub.session_store = session_store

    w = Worker(
        api_base="http://api",
        session_store=session_store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=daily_cap_dollars),
        poll_seconds=0.01,
        telegram_send=_send,
        http_client=client,
        local_executor=local_executor,
        remote_executor=remote_executor,
        hermes_executor=hermes_executor,
        managed_executor=managed_executor,
        cli_pool=cli_pool,
    )
    w._sent = sent  # type: ignore[attr-defined]
    return w


def _setup(
    tmp_path: Path,
    *,
    children: list[dict],
    project_id: str = "p1",
    project_description: str = "Project",
    owner_status: str = STATUS_COMPLETED,
    execution_request: dict | None = None,
    paused: bool = False,
    daily_cap_dollars: float = 100.0,
    project_tags: tuple[str, ...] = ("claude",),
    project_status: str = "in_progress",
    owner_routing: str = "claude_code",
    cancel_pending: bool = False,
    handoff_pending: bool = False,
    local_executor=None,
    remote_executor=None,
    hermes_executor=None,
    managed_executor=None,
    cli_pool=None,
    conversation_id: str | None = None,
    managed_agent_session_id: str | None = None,
    set_cli_session_id: bool = True,
) -> tuple[Worker, FakeApi, Session]:
    """Build a worker with one agent-owned project, its persistent owner
    session (`owner.session_id` is what `COORDINATOR_SESSION_FIELD` stores —
    NOT the owner's synthetic task id), and the given children."""
    api = FakeApi(tasks=[])
    w = _make_worker(
        tmp_path, api, daily_cap_dollars=daily_cap_dollars,
        local_executor=local_executor, remote_executor=remote_executor,
        hermes_executor=hermes_executor, managed_executor=managed_executor,
        cli_pool=cli_pool,
    )
    owner = w.session_store.create(
        task_id=_OWNER_TASK_ID, routing=owner_routing, status=owner_status, origin="operator",
        execution_request=execution_request or {"executor": "claude_code", "working_dir": "/repo/wt-1"},
    )
    if owner_routing in ("claude_code", "codex") and set_cli_session_id:
        w.session_store.set_claude_code_session_id(_OWNER_TASK_ID, "cli-owner-1")
    if conversation_id is not None:
        w.session_store.set_conversation_id(_OWNER_TASK_ID, conversation_id)
    if managed_agent_session_id is not None:
        w.session_store.set_managed_session_id(_OWNER_TASK_ID, managed_agent_session_id)
    owner = w.session_store.get(_OWNER_TASK_ID)
    project = _project(
        project_id, project_description, owner_session_id=owner.session_id,
        paused=paused, tags=project_tags,
    )
    project["status"] = project_status
    if cancel_pending:
        project["fields"][CANCEL_OPERATION_FIELD] = "op-cancel-1"
    if handoff_pending:
        project["fields"][HANDOFF_OPERATION_FIELD] = "op-handoff-1"
    api.tasks[project_id] = project
    for child in children:
        api.tasks[child["id"]] = child
    # Seed the durable baseline while every child is still in its initial
    # state, so a caller that mutates a child afterward sees a genuine
    # diff -- mirroring "Create the row if missing" always running before
    # any event a test cares about.
    w._reconcile_project_owners()
    return w, api, owner


def _age_anchor(w: Worker, project_id: str, seconds_ago: int) -> None:
    w.session_store.set_project_owner_anchor(project_id, int(time.time()) - seconds_ago)


def _owner(w: Worker) -> Session:
    return w.session_store.get(_OWNER_TASK_ID)


# ---------------------------------------------------------------------------
# owner_state() classification
# ---------------------------------------------------------------------------

class TestOwnerStateClassification:
    def test_awaiting_review_wins_over_a_terminal_status(self):
        task = {"id": "c1", "status": "done", "tags": ["agent-completed"]}
        assert owner_state(task) == "awaiting_review"

    def test_accepted_review_is_not_awaiting_review(self):
        task = {"id": "c1", "status": "done", "tags": ["agent-completed", "accepted"]}
        assert owner_state(task) == "done"

    def test_failed_tag_beats_the_cancelled_status_it_leaves_the_task_at(self):
        """A failed/budget-exceeded child's task status is 'cancelled' (see
        LifecycleProjector) -- owner_state must check the failure tag first
        or it would misreport a real failure as a plain cancellation."""
        task = {"id": "c1", "status": "cancelled", "tags": ["agent-failed"]}
        assert owner_state(task) == "failed"

    def test_budget_exceeded_tag_also_reports_failed(self):
        task = {"id": "c1", "status": "cancelled", "tags": ["agent-budget-exceeded"]}
        assert owner_state(task) == "failed"

    def test_plain_cancellation_without_a_failure_tag(self):
        task = {"id": "c1", "status": "cancelled", "tags": []}
        assert owner_state(task) == "cancelled"

    def test_done(self):
        task = {"id": "c1", "status": "done", "tags": []}
        assert owner_state(task) == "done"

    def test_blocked_by_tag(self):
        task = {"id": "c1", "status": "in_progress", "tags": ["agent-blocked"]}
        assert owner_state(task) == "blocked"

    def test_active_covers_unassigned_assigned_and_running(self):
        for status, tags in (("todo", []), ("todo", ["claude"]), ("in_progress", ["agent-running"])):
            assert owner_state({"id": "c1", "status": status, "tags": tags}) == "active"

    def test_machine_waits_are_active_not_blocked(self):
        """`agent-wait-provider`/`agent-wait-dependency` are worker-owned,
        self-clearing waits -- `natural_lane` routes them to In progress,
        not Human queue, and an owner shouldn't burn a paid turn on a
        transient provider rate-limit."""
        for tag in ("agent-wait-provider", "agent-wait-dependency"):
            task = {"id": "c1", "status": "in_progress", "tags": [tag]}
            assert owner_state(task) == "active"


# ---------------------------------------------------------------------------
# Debounce / coalescing
# ---------------------------------------------------------------------------

class TestDebounceAndCoalescing:
    def test_three_events_within_the_window_coalesce_into_one_wake(self, tmp_path):
        w, api, owner = _setup(tmp_path, project_description="Big migration", children=[
            _child("c1", "p1", "Design"),
            _child("c2", "p1", "Backend"),
            _child("c3", "p1", "Frontend"),
        ])

        # Baseline: all children active, nothing to wake for yet.
        assert w._reconcile_project_owners() == 0
        assert not w.session_store.has_pending_messages(owner.session_id)

        # Three children change state "at once" (between two ticks).
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        api.tasks["c2"]["status"] = "cancelled"
        api.tasks["c2"]["tags"] = ["agent-failed"]
        api.tasks["c3"]["tags"] = ["agent-completed"]

        assert w._reconcile_project_owners() == 0  # anchor just set, window not elapsed
        # Immediately retrying still doesn't fire -- the anchor is barely
        # any age at all, nowhere near the quiet window.
        assert w._reconcile_project_owners() == 0
        assert not w.session_store.has_pending_messages(owner.session_id)
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert _owner(w).status == STATUS_CLAIMED
        pending = w.session_store.peek_pending_messages(owner.session_id)
        assert len(pending) == 1
        body = pending[0]["content"]
        for child_id in ("c1", "c2", "c3"):
            assert child_id in body

        # No second wake this same pass -- the owner is now live.
        assert w._reconcile_project_owners() == 0

    def test_no_events_never_sets_an_anchor(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])

        assert w._reconcile_project_owners() == 0
        state = w.session_store.get_project_owner_state("p1")
        assert state["first_unseen_at"] is None


# ---------------------------------------------------------------------------
# Mid-turn owner: no wake until the turn ends
# ---------------------------------------------------------------------------

class TestNoWakeWhileOwnerIsLive:
    def test_event_while_owner_is_running_waits_then_wakes_exactly_once(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Long project",
            children=[_child("c1", "p1", "Design")], owner_status=STATUS_CLAIMED,
        )

        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        assert w._reconcile_project_owners() == 0
        assert not w.session_store.has_pending_messages(owner.session_id)

        # Even once the quiet window has clearly elapsed, a still-live
        # (CLAIMED) owner is never woken -- the "wait while mid-turn" gate
        # is checked independently of the debounce.
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 0
        assert not w.session_store.has_pending_messages(owner.session_id)

        # The turn ends -- exactly one wake follows (the already-elapsed
        # anchor from above means it fires the moment the owner is idle).
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_COMPLETED)
        assert w._reconcile_project_owners() == 1
        assert w.session_store.has_pending_messages(owner.session_id)
        assert w._reconcile_project_owners() == 0  # no second wake -- the owner is live again


# ---------------------------------------------------------------------------
# begin_new_execution CAS: never two owner turns
# ---------------------------------------------------------------------------

class TestNeverTwoOwnerTurns:
    def test_concurrent_begin_new_execution_only_one_wins(self, tmp_path):
        store = SessionStore(db_path=tmp_path / "sessions.db")
        store.create(task_id=_OWNER_TASK_ID, routing="claude_code", status=STATUS_COMPLETED, origin="operator")

        results: list[object] = []
        barrier = threading.Barrier(2)

        def _attempt():
            barrier.wait()
            try:
                store.begin_new_execution(_OWNER_TASK_ID)
                results.append("ok")
            except ValueError:
                results.append("lost")

        threads = [threading.Thread(target=_attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == ["lost", "ok"]
        session = store.get(_OWNER_TASK_ID)
        assert session.status == STATUS_CLAIMED
        assert session.attempt_number == 2  # exactly one new attempt was created

    def test_a_lost_cas_after_the_message_is_already_enqueued_still_leaves_it_queued(self, tmp_path, monkeypatch):
        """The wake message is enqueued *before* `begin_new_execution` (the
        CAS) -- so a lost race there (or a crash in the same spot) leaves
        the message harmlessly queued for the next retry rather than
        dropping it. Simulated here by making the CAS always lose."""
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        def _always_loses(*a, **kw):
            raise ValueError("owner is live")

        monkeypatch.setattr(w.session_store, "begin_new_execution", _always_loses)

        assert w._reconcile_project_owners() == 0
        assert _owner(w).status == STATUS_COMPLETED  # unchanged -- the CAS never landed
        assert w.session_store.has_pending_messages(owner.session_id)
        assert w.session_store.get_project_owner_state("p1")["wake_attempt_id"] is None


# ---------------------------------------------------------------------------
# Pause / resume interaction
# ---------------------------------------------------------------------------

class TestPauseAndResume:
    def test_events_accumulate_while_paused_then_wake_once_on_resume(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Paused project",
            children=[_child("c1", "p1", "Design")], paused=True,
        )

        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        assert w._reconcile_project_owners() == 0
        state = w.session_store.get_project_owner_state("p1")
        assert state["first_unseen_at"] is None  # never touched while paused

        # Operator resumes the project.
        api.tasks["p1"]["fields"].pop(PROJECT_PAUSED_FIELD, None)
        api.tasks["p1"]["fields"].pop(PROJECT_PAUSE_REASON_FIELD, None)

        assert w._reconcile_project_owners() == 0  # anchor just started
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1

    def test_resume_with_nothing_accumulated_does_not_wake(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Paused project",
            children=[_child("c1", "p1", "Design")], paused=True,
        )

        assert w._reconcile_project_owners() == 0
        api.tasks["p1"]["fields"].pop(PROJECT_PAUSED_FIELD, None)
        assert w._reconcile_project_owners() == 0
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 0


# ---------------------------------------------------------------------------
# Spend cap
# ---------------------------------------------------------------------------

class TestSpendCap:
    def test_no_wake_while_the_daily_spend_cap_is_reached(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Capped project",
            children=[_child("c1", "p1", "Design")], daily_cap_dollars=0.0,
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        assert w.tick() == 0
        assert _owner(w).status == STATUS_COMPLETED  # untouched -- reconciler never ran
        assert not w.session_store.has_pending_messages(owner.session_id)


# ---------------------------------------------------------------------------
# Failure / auto-pause
# ---------------------------------------------------------------------------

class TestFailureAndAutoPause:
    def _wake_once(self, w: Worker, project_id: str = "p1") -> None:
        assert w._reconcile_project_owners() == 0
        _age_anchor(w, project_id, 31)
        assert w._reconcile_project_owners() == 1

    def test_a_failed_wake_redelivers_the_same_event_and_does_not_pause_on_the_first_failure(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Flaky project", children=[_child("c1", "p1", "Design")],
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        self._wake_once(w)
        assert _owner(w).status == STATUS_CLAIMED

        # The turn fails. The reconciler processes the failure this same
        # pass (bumping the failure count, redelivering the same event
        # later) -- the retry goes through its own fresh quiet window, same
        # as any other wake.
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_FAILED)
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1")["consecutive_failures"] == 1
        assert api.pause_calls == []

        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1
        assert _owner(w).status == STATUS_CLAIMED
        pending = w.session_store.peek_pending_messages(owner.session_id)
        assert any("c1" in p["content"] for p in pending)

    def test_two_consecutive_failures_auto_pause_with_one_notice(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Flaky project", children=[_child("c1", "p1", "Design")],
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        self._wake_once(w)
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_FAILED)
        assert w._reconcile_project_owners() == 0  # 1st failure -> fresh quiet window
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1  # 1st retry wake

        w.session_store.update_status(_OWNER_TASK_ID, STATUS_FAILED)
        assert w._reconcile_project_owners() == 0  # 2nd failure -> auto-pause, no wake

        assert api.pause_calls == [("p1", {"reason": "owner_failed"})]
        assert len(w._sent) == 1
        assert "Flaky project" in w._sent[0]

    def test_budget_exceeded_pauses_immediately_without_needing_a_second_failure(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Pricey project", children=[_child("c1", "p1", "Design")],
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        self._wake_once(w)
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_BUDGET_EXCEEDED)
        assert w._reconcile_project_owners() == 0

        assert api.pause_calls == [("p1", {"reason": "owner_budget"})]
        assert len(w._sent) == 1


# ---------------------------------------------------------------------------
# Working-directory regression (spec-clearing trap)
# ---------------------------------------------------------------------------

class TestWorkingDirectorySurvivesTheWake:
    def test_begin_new_execution_is_passed_the_prior_execution_request(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, project_description="Repo project", children=[_child("c1", "p1", "Design")],
            execution_request={"executor": "claude_code", "working_dir": "/repo/wt-owner"},
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        assert w._reconcile_project_owners() == 0
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1

        reopened = _owner(w)
        assert reopened.execution_request is not None
        assert reopened.execution_request.get("working_dir") == "/repo/wt-owner"

    def test_forgetting_the_prior_request_loses_the_working_dir(self, tmp_path):
        """Mutation witness: calling `begin_new_execution` with no `request`
        (the trap this issue calls out) clears it -- proving the assertion
        above is load-bearing, not vacuous."""
        store = SessionStore(db_path=tmp_path / "sessions.db")
        store.create(
            task_id=_OWNER_TASK_ID, routing="claude_code", status=STATUS_COMPLETED,
            origin="operator", execution_request={"executor": "claude_code", "working_dir": "/repo/wt-owner"},
        )
        store.begin_new_execution(_OWNER_TASK_ID)  # no request= passed
        reopened = store.get(_OWNER_TASK_ID)
        assert reopened.execution_request is None


# ---------------------------------------------------------------------------
# Restart safety
# ---------------------------------------------------------------------------

class TestRestartSafety:
    def test_resume_pending_does_not_fail_a_queued_undispatched_owner_wake(self, tmp_path):
        w = _make_worker(tmp_path, FakeApi(tasks=[]))
        session = w.session_store.create(
            task_id=_OWNER_TASK_ID, routing="claude_code", status=STATUS_CLAIMED, origin="operator",
        )
        w.session_store.set_claude_code_session_id(_OWNER_TASK_ID, "cli-owner-1")
        w.session_store.enqueue_message(
            session.session_id, "operator", "wake message",
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )

        w.resume_pending()

        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.status == STATUS_CLAIMED
        assert w.session_store.has_pending_messages(session.session_id)

    def test_resume_pending_still_fails_an_ordinary_claimed_session_with_no_pending_message(self, tmp_path):
        w = _make_worker(tmp_path, FakeApi(tasks=[]))
        w.session_store.create(
            task_id=_OWNER_TASK_ID, routing="claude_code", status=STATUS_CLAIMED, origin="operator",
        )

        w.resume_pending()

        assert w.session_store.get(_OWNER_TASK_ID).status == STATUS_FAILED


# ---------------------------------------------------------------------------
# Owner guidance / prompt wording
# ---------------------------------------------------------------------------

class TestOwnerGuidanceWording:
    def test_project_task_guidance_describes_a_persistent_owner(self):
        assert "persistent owner" in PROJECT_TASK_GUIDANCE
        assert "bounded coordinator" not in PROJECT_TASK_GUIDANCE
        assert "does not wake automatically" not in PROJECT_TASK_GUIDANCE

    def test_project_task_guidance_names_the_owner_review_tool(self):
        assert "lifeos_agent_project_owner" in PROJECT_TASK_GUIDANCE
        assert "accept_child" in PROJECT_TASK_GUIDANCE
        assert "complete_project" in PROJECT_TASK_GUIDANCE

    def test_handoff_coordination_prompt_describes_a_persistent_owner(self):
        from api.services.task_projects import ProjectTaskService

        prompt = ProjectTaskService._handoff_coordination_prompt(
            SimpleNamespace(id="p1", description="Big project", notes=""),
            "op-1", {"children": []},
        )
        assert "persistent owner" in prompt
        assert "bounded coordinator run" not in prompt
        assert "not a persistent monitor" not in prompt


# ---------------------------------------------------------------------------
# A1: a repeated non-active state must wake again (reject/rework, reassign)
# ---------------------------------------------------------------------------

class TestRepeatedNonActiveEventsWakeAgain:
    def test_reject_rework_awaiting_review_wakes_again(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "done"
        api.tasks["c1"]["tags"] = ["agent-completed"]

        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_COMPLETED)
        assert w._reconcile_project_owners() == 0  # ack: acked[c1] = "awaiting_review"

        # Operator rejects -- the child resumes as ordinary work.
        api.tasks["c1"]["status"] = "in_progress"
        api.tasks["c1"]["tags"] = []
        assert w._reconcile_project_owners() == 0  # re-baselines c1 -> active, no wake
        assert w.session_store.get_project_owner_state("p1")["acked_states"]["c1"] == "active"

        # The child reaches awaiting_review again.
        api.tasks["c1"]["status"] = "done"
        api.tasks["c1"]["tags"] = ["agent-completed"]
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1  # wakes again for the repeat

    def test_failed_reassign_failed_wakes_again(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "cancelled"
        api.tasks["c1"]["tags"] = ["agent-failed"]

        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_COMPLETED)
        assert w._reconcile_project_owners() == 0  # ack: acked[c1] = "failed"

        # Reassigned/reworked -- back to ordinary active work.
        api.tasks["c1"]["status"] = "todo"
        api.tasks["c1"]["tags"] = ["claude"]
        assert w._reconcile_project_owners() == 0  # re-baselines c1 -> active

        # Fails again.
        api.tasks["c1"]["status"] = "cancelled"
        api.tasks["c1"]["tags"] = ["agent-failed"]
        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1


# ---------------------------------------------------------------------------
# A2: a refused enqueue counts as a wake failure (never a silent, permanent
# wedge)
# ---------------------------------------------------------------------------

class TestRefusedEnqueueCountsAsFailure:
    def test_a_refused_enqueue_counts_as_a_wake_failure_and_eventually_auto_pauses(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        # Simulate an operator cancelling the owner's (already-terminal)
        # last turn from Telegram (see telegram.py): a cancellation_guards
        # row lands for its exact current attempt/turn without changing
        # its (already terminal) status -- enqueue_message refuses forever
        # afterward, since nothing rotates the attempt without a
        # successful enqueue.
        w.session_store.mark_cancelled(
            _OWNER_TASK_ID, attempt_id=owner.attempt_id, turn_id=owner.turn_id, reason="operator_cancel",
        )

        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1")["consecutive_failures"] == 1
        assert not w.session_store.has_pending_messages(owner.session_id)
        assert api.pause_calls == []
        # The enqueue failure never reached the CAS: no new attempt, no
        # in-flight wake bookkeeping recorded.
        after_first = w.session_store.get(_OWNER_TASK_ID)
        assert after_first.attempt_number == owner.attempt_number
        assert after_first.status == owner.status
        state = w.session_store.get_project_owner_state("p1")
        assert state["wake_attempt_id"] is None
        assert state["delivered_states"] is None

        assert w._reconcile_project_owners() == 0
        assert api.pause_calls == [("p1", {"reason": "owner_failed"})]
        assert len(w._sent) == 1


# ---------------------------------------------------------------------------
# A3: ack is pinned -- a successful wake must not repeat forever
# ---------------------------------------------------------------------------

class TestAckIsPinnedToDeliveredStates:
    def test_successful_ack_prevents_further_wakes_for_unchanged_states(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]

        _age_anchor(w, "p1", 31)
        assert w._reconcile_project_owners() == 1
        w.session_store.update_status(_OWNER_TASK_ID, STATUS_COMPLETED)
        assert w._reconcile_project_owners() == 0  # processes the ack

        for _ in range(5):
            _age_anchor(w, "p1", 31)
            assert w._reconcile_project_owners() == 0
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.attempt_number == 2  # never rotated again
        assert after.status == STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Pin: a wake_attempt_id that doesn't match the owner's current attempt is
# never treated as this pass's outcome
# ---------------------------------------------------------------------------

class TestWakeAttemptIdPinning:
    def test_a_foreign_wake_attempt_id_is_not_treated_as_this_owners_outcome(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        # A stale/foreign wake_attempt_id -- the owner never actually
        # resumed for it; it's still sitting on its original attempt.
        w.session_store.record_project_owner_wake(
            "p1", delivered_states={"c1": "blocked"}, wake_attempt_id="attempt_not_real", wake_turn_id=None,
        )
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1  # wakes normally, ignoring the mismatched id
        assert w.session_store.get_project_owner_state("p1")["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# B3: selection gates
# ---------------------------------------------------------------------------

class TestSelectionGates:
    def test_done_project_is_never_reconciled(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design", status="blocked", tags=["agent-blocked"])],
            project_status="done",
        )
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1") is None

    def test_cancelled_project_is_never_reconciled(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design", status="blocked", tags=["agent-blocked"])],
            project_status="cancelled",
        )
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1") is None

    def test_cancellation_pending_project_is_skipped(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design", status="blocked", tags=["agent-blocked"])],
            cancel_pending=True,
        )
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1") is None

    def test_handoff_pending_project_is_skipped(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design", status="blocked", tags=["agent-blocked"])],
            handoff_pending=True,
        )
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1") is None

    def test_operator_owned_me_project_never_wakes(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design", status="blocked", tags=["agent-blocked"])],
            project_tags=("me",),
        )
        assert w._reconcile_project_owners() == 0
        assert w.session_store.get_project_owner_state("p1") is None

    def test_unsupported_route_is_skipped_not_woken(self, tmp_path, monkeypatch):
        """Every route this issue supports (local/remote/hermes/claude, plus
        claude_code/codex) is now woken -- see the per-route tests below.
        A route the reconciler has never heard of still gets a safe
        skip, not a crash, so a future new route degrades gracefully."""
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")], owner_routing="some_future_route",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        calls = []
        orig = w._continue_session
        monkeypatch.setattr(
            w, "_continue_session",
            lambda *a, **kw: (calls.append(1), orig(*a, **kw))[1],
        )

        assert w._reconcile_project_owners() == 0
        # The explicit route gate short-circuits before ever reaching
        # delivery -- not merely surviving a NotImplementedError raised
        # from inside it.
        assert calls == []
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.status == STATUS_COMPLETED  # unchanged -- no CAS attempted
        assert after.attempt_number == 1


# ---------------------------------------------------------------------------
# B3: one project's reconciliation failure must never abort the whole tick
# ---------------------------------------------------------------------------

class TestReconcilerIsolatesPerProjectFailures:
    def test_one_projects_exception_does_not_stop_another_from_waking(self, tmp_path, monkeypatch):
        w, api, owner1 = _setup(tmp_path, project_id="p1", children=[_child("c1", "p1", "Design")])
        owner2 = w.session_store.create(
            task_id="project_p2_op1", routing="claude_code", status=STATUS_COMPLETED, origin="operator",
            execution_request={"executor": "claude_code", "working_dir": "/repo/wt-2"},
        )
        w.session_store.set_claude_code_session_id("project_p2_op1", "cli-owner-2")
        owner2 = w.session_store.get("project_p2_op1")
        api.tasks["p2"] = _project("p2", "Second project", owner_session_id=owner2.session_id)
        api.tasks["c2"] = _child("c2", "p2", "Other work")
        w._reconcile_project_owners()  # seed p2's baseline too

        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        api.tasks["c2"]["status"] = "blocked"
        api.tasks["c2"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)
        _age_anchor(w, "p2", 31)

        orig = w._reconcile_one_project_owner

        def _boom(project_id, *a, **kw):
            if project_id == "p1":
                raise RuntimeError("boom")
            return orig(project_id, *a, **kw)

        monkeypatch.setattr(w, "_reconcile_one_project_owner", _boom)

        assert w._reconcile_project_owners() == 1  # p1 blew up; p2 still woke
        assert w.session_store.get("project_p2_op1").status == STATUS_CLAIMED


# ---------------------------------------------------------------------------
# B4: a reply arriving after an owner wake rotated the attempt is not silent
# ---------------------------------------------------------------------------

class TestStaleOwnerFollowupReply:
    def test_reply_after_a_wake_rotated_the_attempt_is_recorded_and_notified(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        qid = w.session_store.register_completion_followup(
            owner.session_id, _OWNER_TASK_ID, [12345], label="Big migration",
        )
        w.session_store.deposit_answer_by_id(qid, "looks good, keep going")

        # An owner wake rotates the attempt before the reply is processed.
        w.session_store.begin_new_execution(_OWNER_TASK_ID, request=owner.execution_request)

        w._process_clarification_answers()

        events = w.transcript_store.read(owner.session_id)
        assert any(e.get("kind") == "owner_followup_reply_stale" for e in events)
        assert any("arrived after" in text for text in w._sent)
        assert w.session_store.claim_answered_unprocessed_questions() == []


# ---------------------------------------------------------------------------
# B6: project_owner_state.owner_session_id stays fresh across a re-Plan
# ---------------------------------------------------------------------------

class TestOwnerSessionIdStaysFreshOnReplan:
    def test_ensure_project_owner_state_updates_a_stale_owner_session_id(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        state = w.session_store.get_project_owner_state("p1")
        assert state["owner_session_id"] == owner.session_id

        new_owner = w.session_store.create(
            task_id="project_p1_op2", routing="claude_code", status=STATUS_COMPLETED, origin="operator",
        )
        w.session_store.ensure_project_owner_state(
            "p1", owner_session_id=new_owner.session_id, baseline_states={"c1": "active"},
        )

        refreshed = w.session_store.get_project_owner_state("p1")
        assert refreshed["owner_session_id"] == new_owner.session_id
        # Everything else -- acked_states in particular -- is untouched by
        # a re-observation's owner_session_id sync.
        assert refreshed["acked_states"] == state["acked_states"]


# ---------------------------------------------------------------------------
# B1: the wake message must point the owner at the attested tool it can
# actually use mid-wake — `lifeos_agent_project_owner`'s complete_project
# action is allowed while this exact turn is live (see
# `ProjectTaskService.complete_project`'s `owner_session` exemption), unlike
# the operator-only `lifeos_project_complete`.
# ---------------------------------------------------------------------------

class TestWakeMessagePointsAtTheAttestedOwnerTool:
    def test_wake_message_names_the_attested_owner_tool(self, tmp_path):
        w, api, owner = _setup(tmp_path, children=[_child("c1", "p1", "Design")])
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        pending = w.session_store.peek_pending_messages(owner.session_id)
        body = pending[0]["content"]
        assert "lifeos_agent_project_owner" in body
        assert "refuses while this turn is live" not in body


# ---------------------------------------------------------------------------
# Owner continuation on the other routes, and the fresh-context fallback
# when no native handle exists. Stub executors mirror the real
# ones' call shape (LocalExecutor.execute, HermesExecutor.execute,
# ManagedExecutor.start / .driver.post_user_message) closely enough to
# exercise Worker._continue_session / _deliver_owner_wake without any I/O.
# ---------------------------------------------------------------------------

class _StubLocalExecutor:
    """`session_store` is wired by `_make_worker` after construction (the
    real store doesn't exist yet when a test builds this stub) -- every
    stub in this section honors the real executors' own
    `begin_executor_turn` call before doing anything else, so a caller
    that hands it a stale (already-turn-rotated) session raises exactly
    like `LocalExecutor.execute`/`HermesExecutor.execute`/
    `ManagedExecutor.start` would, instead of silently succeeding and
    hiding a real defect a looser stub would let through unnoticed."""

    def __init__(self):
        self.session_store = None
        self.calls: list[dict] = []

    def execute(self, session, task):
        session = self.session_store.begin_executor_turn(
            session.task_id, "execute", session=session,
        )
        self.calls.append({"session": session, "task": task})
        return ExecutorOutcome(
            status=STATUS_COMPLETED, final_text="local turn done",
            session_id=session.session_id, attempt_id=session.attempt_id,
            turn_id=session.turn_id, executor=session.routing,
        )


class _StubHermesExecutor:
    def __init__(self):
        self.session_store = None
        self.calls: list[dict] = []

    def execute(self, session, task, prompt=None):
        session = self.session_store.begin_executor_turn(
            session.task_id, "execute", session=session,
        )
        self.calls.append({
            "session": session, "task": task, "prompt": prompt,
            "conversation_id": session.conversation_id,
        })
        return ExecutorOutcome(
            status=STATUS_COMPLETED, final_text="hermes turn done",
            session_id=session.session_id, attempt_id=session.attempt_id,
            turn_id=session.turn_id, executor="hermes",
            continuation_id=session.conversation_id or "conv-fresh-1",
        )


class _StubManagedDriver:
    def __init__(self, *, raise_on_post: bool = False):
        self.posted: list[tuple[str, str]] = []
        self.raise_on_post = raise_on_post

    def post_user_message(self, session_id, content):
        if self.raise_on_post:
            raise RuntimeError("managed session not found (404)")
        self.posted.append((session_id, content))


class _StubManagedExecutor:
    def __init__(self, driver: "_StubManagedDriver | None" = None):
        self.session_store = None
        self.driver = driver if driver is not None else _StubManagedDriver()
        self.start_calls: list[dict] = []

    def start(self, session, task):
        session = self.session_store.begin_executor_turn(
            session.task_id, "start", session=session,
        )
        # Mirrors real `ManagedExecutor.start`, which flips the row to
        # RUNNING right after minting its turn, before any remote call.
        self.session_store.update_status(
            session.task_id, STATUS_RUNNING,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        self.start_calls.append({"session": session, "task": task})
        return ExecutorOutcome(
            status=STATUS_COMPLETED, final_text="managed fresh start done",
            session_id=session.session_id, attempt_id=session.attempt_id,
            turn_id=session.turn_id, executor="claude",
            continuation_id="remote-fresh-1",
        )


class TestLocalRemoteNativeContinuation:
    def test_local_owner_wake_appends_and_runs_inline(self, tmp_path):
        local = _StubLocalExecutor()
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="local", execution_request={"executor": "local"},
            local_executor=local, cli_pool=_SynchronousPool(),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(local.calls) == 1
        # The wake diff (child id + prev->new state) reached the executor as
        # a real conversation turn, appended to the session's own history.
        history = [m["content"] for m in w.session_store.get_messages(owner.session_id)]
        assert any("c1" in m for m in history)
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.routing == "local"  # route never changes
        assert after.status == STATUS_COMPLETED  # the stub ran synchronously to completion

    def test_remote_owner_wake_is_native_too(self, tmp_path):
        remote = _StubLocalExecutor()
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="remote", execution_request={"executor": "remote"},
            remote_executor=remote, cli_pool=_SynchronousPool(),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(remote.calls) == 1
        assert w.session_store.get(_OWNER_TASK_ID).routing == "remote"


class TestHermesContinuation:
    def test_native_continuation_uses_the_stored_conversation_id(self, tmp_path):
        hermes = _StubHermesExecutor()
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="hermes", execution_request={"executor": "hermes"},
            hermes_executor=hermes, cli_pool=_SynchronousPool(),
            conversation_id="conv-existing-1",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(hermes.calls) == 1
        call = hermes.calls[0]
        assert call["conversation_id"] == "conv-existing-1"
        assert "c1" in call["prompt"]  # the terse wake diff, not a fallback briefing
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.routing == "hermes"
        assert after.status == STATUS_COMPLETED

    def test_fallback_briefing_when_no_conversation_is_stored(self, tmp_path):
        """No native handle (never established a conversation) -- a fresh
        turn on the SAME route, seeded with the bounded owner briefing
        instead of the terse wake diff."""
        hermes = _StubHermesExecutor()
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design")],
            owner_routing="hermes", execution_request={"executor": "hermes"},
            hermes_executor=hermes, cli_pool=_SynchronousPool(),
            conversation_id=None,
            project_description="Ship the migration",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(hermes.calls) == 1
        call = hermes.calls[0]
        assert call["conversation_id"] is None
        briefing = call["prompt"]
        assert "Fresh start for project owner" in briefing
        assert "Ship the migration" in briefing
        assert "c1" in briefing
        # Never the terse wake diff's own opening line for this project.
        assert "Project owner wake:" not in briefing
        assert w.session_store.get(_OWNER_TASK_ID).routing == "hermes"  # route never changes


class TestManagedContinuation:
    def test_native_continuation_posts_a_fresh_per_turn_proof(self, tmp_path, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "mcp_bearer_token", "synthetic-turn-secret", raising=False)
        driver = _StubManagedDriver()
        managed = _StubManagedExecutor(driver=driver)
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            managed_executor=managed, cli_pool=_SynchronousPool(),
            managed_agent_session_id="remote-sess-1",
            project_tags=("cloud-sonnet",),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        first_attempt = w.session_store.get(_OWNER_TASK_ID).attempt_id
        assert w._reconcile_project_owners() == 1
        assert len(driver.posted) == 1
        remote_id, body = driver.posted[0]
        assert remote_id == "remote-sess-1"
        assert "c1" in body
        assert "lifeos_attempt_id=" in body and "lifeos_turn_proof=" in body
        # The proof is for the NEW turn the CAS just minted, not the one
        # that just ended.
        new_attempt = w.session_store.get(_OWNER_TASK_ID).attempt_id
        assert new_attempt != first_attempt
        assert new_attempt in body
        assert w.session_store.get(_OWNER_TASK_ID).routing == "claude"
        assert managed.start_calls == []  # native path only, no fallback

    def test_fallback_starts_a_fresh_managed_session_on_a_404(self, tmp_path):
        driver = _StubManagedDriver(raise_on_post=True)
        managed = _StubManagedExecutor(driver=driver)
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            managed_executor=managed, cli_pool=_SynchronousPool(),
            managed_agent_session_id="remote-sess-stale",
            project_tags=("cloud-sonnet",),
            project_description="Ship the migration",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(driver.posted) == 0  # the post attempt failed, nothing landed
        assert len(managed.start_calls) == 1
        briefing = managed.start_calls[0]["task"].get("notes") or ""
        assert "Fresh start for project owner" in briefing
        assert "Ship the migration" in briefing
        assert "c1" in briefing
        assert w.session_store.get(_OWNER_TASK_ID).routing == "claude"  # route never changes

    def test_fallback_starts_fresh_when_no_remote_session_id_at_all(self, tmp_path):
        driver = _StubManagedDriver()
        managed = _StubManagedExecutor(driver=driver)
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            managed_executor=managed, cli_pool=_SynchronousPool(),
            managed_agent_session_id=None,
            project_tags=("cloud-sonnet",),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        assert len(driver.posted) == 0
        assert len(managed.start_calls) == 1
        assert w.session_store.get(_OWNER_TASK_ID).routing == "claude"

    def test_managed_owner_never_runs_without_the_cloud_consent_tag(self, tmp_path):
        """`_project_is_agent_owned` gates Managed continuation on the
        project still carrying a #cloud-* tag -- a Managed-routed owner
        session on a project whose tags don't include one is never even
        looked up."""
        driver = _StubManagedDriver()
        managed = _StubManagedExecutor(driver=driver)
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            managed_executor=managed, cli_pool=_SynchronousPool(),
            managed_agent_session_id="remote-sess-1",
            project_tags=("me",),  # no engine assignee, no #cloud-* tag
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 0
        assert driver.posted == []
        assert managed.start_calls == []


class TestManagedWakeDoesNotDoubleStart:
    """`tick()` runs `_reconcile_project_owners()` immediately before
    `_dispatch_spawned_sessions()`. A successful native
    Managed continuation that left the owner at CLAIMED (whatever
    `begin_new_execution`'s CAS set it to) would still be sitting right
    there for the dispatcher's `list_by_status(STATUS_CLAIMED)` scan --
    origin='operator' with no parent doesn't hit that dispatcher's skip
    guard -- so it would reach `_execute_start` -> `ManagedExecutor.start`
    and spin up a SECOND remote sandbox in the same tick, overwriting
    `managed_agent_session_id` and orphaning the one just posted to."""

    def test_reconcile_then_dispatch_in_one_tick_starts_at_most_one_sandbox(self, tmp_path):
        driver = _StubManagedDriver()
        managed = _StubManagedExecutor(driver=driver)
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            managed_executor=managed, cli_pool=_SynchronousPool(),
            managed_agent_session_id="remote-sess-1",
            project_tags=("cloud-sonnet",),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        # Step 1 of tick(): the reconciler wakes the owner natively.
        assert w._reconcile_project_owners() == 1
        assert len(driver.posted) == 1
        assert managed.start_calls == []  # no fresh sandbox from the native path itself
        after_reconcile = w.session_store.get(_OWNER_TASK_ID)
        assert after_reconcile.status == STATUS_RUNNING
        assert after_reconcile.managed_agent_session_id == "remote-sess-1"

        # Step 2 of the SAME tick: the dispatcher must find nothing left to
        # claim for this owner -- exactly one managed sandbox this tick.
        w._dispatch_spawned_sessions()
        assert managed.start_calls == []
        final = w.session_store.get(_OWNER_TASK_ID)
        assert final.managed_agent_session_id == "remote-sess-1"  # never overwritten


class TestLocalRemoteWakeUsesUserRole:
    """`append_message`'s 2nd positional argument is the conversation ROLE
    an LLM provider sees, not a sender label --
    passing the default `sender_id` ("operator") there would reach the
    provider as `role="operator"`, which every real provider rejects,
    failing the owner turn and (after two such failures) auto-pausing the
    project."""

    def test_wake_diff_is_appended_with_role_user(self, tmp_path):
        local = _StubLocalExecutor()
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="local", execution_request={"executor": "local"},
            local_executor=local, cli_pool=_SynchronousPool(),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        messages = w.session_store.get_messages(owner.session_id)
        assert messages, "expected the wake diff to be appended to history"
        assert messages[-1]["role"] == "user"
        assert "c1" in messages[-1]["content"]


class TestUndeliveredWakeIsMarkedFailed:
    """Every delivery path (native continuation AND the fresh-start
    fallback) failing outright must not leave the reopened
    owner stuck live forever -- `_reconcile_one_project_owner`'s
    `if not delivered: self._mark_failed(...)` is what lets the NEXT tick's
    outcome check (`wake_attempt_id == owner.attempt_id`) recognize this as
    a failed turn and feed the consecutive-failure/auto-pause counter."""

    def test_fully_undelivered_wake_is_marked_failed_and_counted(self, tmp_path):
        # No managed_executor injected: `_get_managed_executor()` returns
        # None (Managed Agents isn't configured in this test process), so
        # neither the native post nor the fresh-start fallback can deliver
        # anything at all.
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")],
            owner_routing="claude", execution_request={"executor": "claude"},
            cli_pool=_SynchronousPool(),
            managed_agent_session_id=None,
            project_tags=("cloud-sonnet",),
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.status == STATUS_FAILED  # never left stuck live

        state = w.session_store.get_project_owner_state("p1")
        assert state["wake_attempt_id"] == after.attempt_id
        assert state["consecutive_failures"] == 0  # not yet counted this pass

        # The next pass recognizes the failed turn against the recorded
        # wake_attempt_id and counts it toward auto-pause.
        assert w._reconcile_project_owners() == 0
        state_after = w.session_store.get_project_owner_state("p1")
        assert state_after["consecutive_failures"] == 1


class TestCliFallbackBriefing:
    def test_missing_cli_session_id_falls_back_to_a_fresh_bounded_briefing(self, tmp_path):
        w, api, owner = _setup(
            tmp_path,
            children=[_child("c1", "p1", "Design")],
            owner_routing="claude_code", set_cli_session_id=False,
            project_description="Ship the migration",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        pending = w.session_store.peek_pending_messages(owner.session_id)
        assert len(pending) == 1
        briefing = pending[0]["content"]
        # The bounded briefing itself: objective/child-table content, built
        # and actually delivered through the real reconciler call path
        # (`_reconcile_one_project_owner` -> `_owner_fallback_briefing`),
        # not just constructed and discarded.
        assert "Fresh start for project owner" in briefing
        assert "Ship the migration" in briefing
        assert "c1" in briefing
        assert "Project owner wake:" not in briefing
        # The owner-review/completion capability this briefing must describe
        # -- a stale reference here (e.g. the old operator-only
        # `lifeos_project_complete` wording, or a bad name entirely) would
        # either surface as wrong guidance or, if it names an undefined
        # symbol, crash `_owner_fallback_briefing` outright; either way the
        # wake must still be recorded as delivered with the right content.
        assert "lifeos_agent_project_owner" in briefing
        assert "complete_project" in briefing
        assert "accept_child" in briefing
        after = w.session_store.get(_OWNER_TASK_ID)
        assert after.routing == "claude_code"  # route never changes
        assert after.status == STATUS_CLAIMED  # still enqueued + CAS'd like the native path
        # The wake is recorded delivered: `wake_attempt_id`/`wake_turn_id`
        # are stamped for this exact new attempt so the reconciler's next
        # pass can reconcile its outcome (ack on success, redeliver on
        # failure) rather than treating it as never having been sent.
        state = w.session_store.get_project_owner_state("p1")
        assert state["wake_attempt_id"] == after.attempt_id
        assert state["wake_turn_id"] == after.turn_id

    def test_present_cli_session_id_still_gets_the_terse_wake_diff(self, tmp_path):
        w, api, owner = _setup(
            tmp_path, children=[_child("c1", "p1", "Design")], owner_routing="claude_code",
        )
        api.tasks["c1"]["status"] = "blocked"
        api.tasks["c1"]["tags"] = ["agent-blocked"]
        _age_anchor(w, "p1", 31)

        assert w._reconcile_project_owners() == 1
        pending = w.session_store.peek_pending_messages(owner.session_id)
        body = pending[0]["content"]
        assert body.startswith("Project owner wake:")
        assert "Fresh start for project owner" not in body
