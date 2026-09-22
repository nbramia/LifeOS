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
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    Session,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.task_projects import (
    COORDINATOR_SESSION_FIELD,
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


def _make_worker(tmp_path: Path, api: FakeApi, *, daily_cap_dollars: float = 100.0) -> Worker:
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []

    def _send(text, chat_id=None, bot=None):
        sent.append(text)
        return True

    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=daily_cap_dollars),
        poll_seconds=0.01,
        telegram_send=_send,
        http_client=client,
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
) -> tuple[Worker, FakeApi, Session]:
    """Build a worker with one agent-owned project, its persistent owner
    session (`owner.session_id` is what `COORDINATOR_SESSION_FIELD` stores —
    NOT the owner's synthetic task id), and the given children."""
    api = FakeApi(tasks=[])
    w = _make_worker(tmp_path, api, daily_cap_dollars=daily_cap_dollars)
    owner = w.session_store.create(
        task_id=_OWNER_TASK_ID, routing="claude_code", status=owner_status, origin="operator",
        execution_request=execution_request or {"executor": "claude_code", "working_dir": "/repo/wt-1"},
    )
    w.session_store.set_claude_code_session_id(_OWNER_TASK_ID, "cli-owner-1")
    owner = w.session_store.get(_OWNER_TASK_ID)
    api.tasks[project_id] = _project(
        project_id, project_description, owner_session_id=owner.session_id, paused=paused,
    )
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

    def test_handoff_coordination_prompt_describes_a_persistent_owner(self):
        from api.services.task_projects import ProjectTaskService

        prompt = ProjectTaskService._handoff_coordination_prompt(
            SimpleNamespace(id="p1", description="Big project", notes=""),
            "op-1", {"children": []},
        )
        assert "persistent owner" in prompt
        assert "bounded coordinator run" not in prompt
        assert "not a persistent monitor" not in prompt
