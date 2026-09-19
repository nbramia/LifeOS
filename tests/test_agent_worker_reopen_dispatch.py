"""A reopened top-level claude_code/codex session must be reachable by a
dispatch tick, without double-dispatching a session the same tick's
top-level claim loop just claimed fresh; a session that stays stuck at
CLAIMED past a threshold gets a one-time operator alert (or is silently
retired once its card isn't actively claimed); and a resume sends
a truthful "resumed" confirmation only once a subprocess is confirmed to
have actually launched, never losing the queued note on a failure that
happens before that.

Covers:
  * ``_dispatch_spawned_sessions`` admitting a top-level claude_code/codex
    session carrying an undelivered pending message (the reopen signature),
    while still skipping a top-level session with no pending message (the
    fresh-claim shape).
  * ``_reconcile_stuck_claimed_sessions`` alerting once per stuck episode
    for a session that stays CLAIMED with an undelivered pending message
    past the configured threshold, staying silent for one still inside its
    window, and silently retiring one whose card isn't actively
    claimed.
  * ``_confirm_resume_or_requeue``: the "Resumed" confirmation and the
    queued note's delivery both depend on a confirmed subprocess launch,
    not merely on the resume attempt returning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from config.settings import settings

pytestmark = pytest.mark.unit


@dataclass
class _StubCliExecutor:
    """Stub CLI executor for dispatch tests.

    Also appends the transcript spawn-marker event a real executor writes
    immediately before ``Popen`` (``claude_code_spawn``/``codex_spawn``),
    because the resume dispatch confirms a subprocess genuinely launched
    (via ``_cli_subprocess_launch_count``) before marking a queued note
    delivered or sending "Resumed" — set ``launches=False`` to simulate a
    pre-start failure (nothing ever launched) and/or ``raises`` to
    simulate a crash.
    """
    outcome: ExecutorOutcome
    tmp_path: Path
    spawn_kind: str = "claude_code_spawn"
    launches: bool = True
    raises: Exception | None = None
    execute_calls: list = field(default_factory=list)
    resume_calls: list = field(default_factory=list)

    def _mark_launch(self, session) -> None:
        TranscriptStore(transcripts_dir=self.tmp_path / "transcripts").append(
            session.session_id, self.spawn_kind, {},
        )

    def execute(self, session, task):
        self.execute_calls.append((session.task_id, task.get("description")))
        if self.launches:
            self._mark_launch(session)
        if self.raises is not None:
            raise self.raises
        return self.outcome

    def resume(self, session, message, working_dir=None):
        self.resume_calls.append((session.task_id, message))
        if self.launches:
            self._mark_launch(session)
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _make_handler(not_running_task_ids: frozenset[str] = frozenset()):
    """A top-level (vault-task-backed) session's dispatch re-validates task
    ownership before proceeding (`_task_claim_is_current`) — answer any task
    lookup as a live, currently-claimed #agent-running card so that gate
    passes, and any tag-swap/status write as a no-op success. Task ids in
    `not_running_task_ids` come back without the running tag, simulating a
    card the operator cancelled/retagged/reassigned out from under a stuck
    session."""
    def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.startswith("/api/tasks/"):
            task_id = req.url.path.rsplit("/", 1)[-1]
            tags = [] if task_id in not_running_task_ids else ["agent-running"]
            return httpx.Response(200, json={
                "id": task_id, "description": f"task {task_id}", "tags": tags,
            })
        if req.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={"tasks": []})
    return _handler


def _make_worker(
    tmp_path: Path, *, claude_code_executor=None, codex_executor=None,
    not_running_task_ids: frozenset[str] = frozenset(),
):
    transport = httpx.MockTransport(_make_handler(not_running_task_ids))
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    sent_with_ids: list[tuple[int, str]] = []

    def _send(text, chat_id=None):
        sent.append(text)
        return True

    def _send_with_id(text, chat_id=None):
        msg_id = len(sent_with_ids) + 9000
        sent_with_ids.append((msg_id, text))
        return [msg_id]

    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=_send,
        telegram_send_with_id=_send_with_id,
        http_client=client,
        claude_code_executor=claude_code_executor,
        codex_executor=codex_executor,
        cli_pool=_SynchronousPool(),
    )
    w._sent = sent  # type: ignore[attr-defined]
    w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
    return w


def _seed_reopened_top_level_session(store: SessionStore, *, task_id, routing, cli_id):
    """Mirror the shape of a top-level session a reopen path left behind:
    no parent, no operator origin (a real #agent vault task backs it),
    CLAIMED, a persisted CLI id (so dispatch resumes rather than executes),
    and an undelivered pending message enqueued by the reopen."""
    session = store.create(task_id=task_id, routing=routing, status=STATUS_CLAIMED)
    store.set_claude_code_session_id(task_id, cli_id)
    store.enqueue_message(session.session_id, "operator", "one more thing, please")
    return store.get(task_id)


# ---------------------------------------------------------------------------
# Reopened top-level session dispatch reachability
# ---------------------------------------------------------------------------

class TestReopenedTopLevelSessionIsDispatched:
    def test_reopened_claude_code_session_with_pending_message_is_dispatched(self, tmp_path):
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-reopen", routing="claude_code", cli_id="cli-reopen-1",
        )
        assert session.parent_session_id is None
        assert session.origin != "operator"
        assert w.session_store.has_pending_messages(session.session_id)

        w._dispatch_spawned_sessions()

        assert stub.execute_calls == []
        assert stub.resume_calls == [("cc-reopen", "one more thing, please")]
        # The reopen's pending row was marked delivered once the launch was
        # confirmed, not left stuck.
        assert not w.session_store.has_pending_messages(session.session_id)

    def test_reopened_codex_session_with_pending_message_is_dispatched(self, tmp_path):
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"),
            tmp_path=tmp_path, spawn_kind="codex_spawn",
        )
        w = _make_worker(tmp_path, codex_executor=stub)
        _seed_reopened_top_level_session(
            w.session_store, task_id="cx-reopen", routing="codex", cli_id="cli-reopen-2",
        )

        w._dispatch_spawned_sessions()

        assert stub.execute_calls == []
        assert stub.resume_calls == [("cx-reopen", "one more thing, please")]

    def test_fresh_top_level_claim_with_no_pending_message_is_not_dispatched(self, tmp_path):
        """A top-level session claimed fresh (this tick's own `_dispatch`)
        has no `claude_code_session_id` yet and never carries a
        pending_messages row — `_dispatch` synthesizes its first-turn
        payload in-memory. This is the exact shape `_dispatch_spawned_sessions`
        must NOT pick up, or a fresh claim would be double-dispatched."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        w.session_store.create(task_id="cc-fresh", routing="claude_code", status=STATUS_CLAIMED)
        session = w.session_store.get("cc-fresh")
        assert session.claude_code_session_id is None
        assert not w.session_store.has_pending_messages(session.session_id)

        w._dispatch_spawned_sessions()

        assert stub.execute_calls == []
        assert stub.resume_calls == []
        # Left exactly as claimed — still eligible for `_dispatch` to run it.
        assert w.session_store.get("cc-fresh").status == STATUS_CLAIMED

    def test_spawned_child_still_dispatches_without_pending_message(self, tmp_path):
        """Regression: a spawned child (parent set) is unconditionally
        eligible regardless of pending-message state — it doesn't need a
        pending message to qualify."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        parent = w.session_store.create(task_id="parent-1", routing="claude_code", status=STATUS_COMPLETED)
        w.session_store.create(
            task_id="child-1", routing="claude_code", status=STATUS_CLAIMED,
            parent_session_id=parent.session_id, root_session_id=parent.session_id,
        )

        w._dispatch_spawned_sessions()

        assert [c[0] for c in stub.execute_calls] == ["child-1"]

    def test_operator_root_spawn_still_dispatches_without_pending_message(self, tmp_path):
        """Regression: an operator root-spawn (no parent, origin='operator')
        is unconditionally eligible regardless of pending-message state."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        w.session_store.create(task_id="op-1", routing="claude_code", status=STATUS_CLAIMED, origin="operator")

        w._dispatch_spawned_sessions()

        assert [c[0] for c in stub.execute_calls] == ["op-1"]


# ---------------------------------------------------------------------------
# Stuck-claimed self-heal alert
# ---------------------------------------------------------------------------

class TestStuckClaimedSessionSweep:
    def test_session_past_threshold_gets_exactly_one_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "agent_stuck_session_timeout_minutes", -1_000_000)
        w = _make_worker(tmp_path)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="stuck-1", routing="claude_code", cli_id="cli-stuck-1",
        )

        count = w._reconcile_stuck_claimed_sessions()
        assert count == 1
        assert any("stuck-1" in t and session.session_id in t for t in w._sent)

        # A second sweep tick must not re-alert the same episode.
        count2 = w._reconcile_stuck_claimed_sessions()
        assert count2 == 0
        assert len(w._sent) == 1

    def test_session_inside_its_window_is_left_alone(self, tmp_path):
        # Default threshold (15 minutes) — a session just reopened is well
        # inside its normal pickup window.
        w = _make_worker(tmp_path)
        _seed_reopened_top_level_session(
            w.session_store, task_id="fresh-reopen", routing="claude_code", cli_id="cli-fresh-1",
        )

        count = w._reconcile_stuck_claimed_sessions()

        assert count == 0
        assert w._sent == []

    def test_session_without_pending_message_is_not_a_candidate(self, tmp_path, monkeypatch):
        """A CLAIMED session with no undelivered message isn't this sweep's
        concern — it's an ordinary fresh top-level claim about to run."""
        monkeypatch.setattr(settings, "agent_stuck_session_timeout_minutes", -1_000_000)
        w = _make_worker(tmp_path)
        w.session_store.create(task_id="fresh-claim", routing="claude_code", status=STATUS_CLAIMED)

        count = w._reconcile_stuck_claimed_sessions()

        assert count == 0
        assert w._sent == []

    def test_reopened_episode_after_a_resolved_alert_gets_a_fresh_alert(self, tmp_path, monkeypatch):
        """`last_activity_at` moves every time the session is reopened to
        CLAIMED — a second, later stuck episode is not silenced by the
        first episode's alert."""
        monkeypatch.setattr(settings, "agent_stuck_session_timeout_minutes", -1_000_000)
        w = _make_worker(tmp_path)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="stuck-2", routing="claude_code", cli_id="cli-stuck-2",
        )
        assert w._reconcile_stuck_claimed_sessions() == 1

        # Simulate recovery + a second, later reopen. `last_activity_at` has
        # second granularity and every write here happens within the same
        # wall-clock second in a fast test run, so force the bump directly
        # rather than relying on real elapsed time.
        w.session_store.drain_pending_messages(session.session_id)
        w.session_store.update_status("stuck-2", STATUS_COMPLETED)
        w.session_store.update_status("stuck-2", STATUS_CLAIMED)
        with w.session_store._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity_at = last_activity_at + 1000 WHERE task_id = ?",
                ("stuck-2",),
            )
        w.session_store.enqueue_message(session.session_id, "operator", "second episode")

        assert w._reconcile_stuck_claimed_sessions() == 1
        assert len(w._sent) == 2

    def test_card_no_longer_actively_claimed_is_silently_retired(self, tmp_path, monkeypatch):
        """The operator can cancel/retag/reassign a card after a reopen
        queued the session's note. A stuck CLAIMED session whose backing
        card lacks the running tag is not the operator's
        problem to be alerted about — it's retired without a Telegram
        message."""
        monkeypatch.setattr(settings, "agent_stuck_session_timeout_minutes", -1_000_000)
        w = _make_worker(tmp_path, not_running_task_ids=frozenset({"stuck-retired"}))
        _seed_reopened_top_level_session(
            w.session_store, task_id="stuck-retired", routing="claude_code", cli_id="cli-retired-1",
        )

        count = w._reconcile_stuck_claimed_sessions()

        assert count == 0
        assert w._sent == []

    def test_actively_claimed_card_is_unaffected_by_the_running_check(self, tmp_path, monkeypatch):
        """Regression: the ordinary alert path still fires when the card IS
        still actively claimed (the running check doesn't itself suppress
        every alert)."""
        monkeypatch.setattr(settings, "agent_stuck_session_timeout_minutes", -1_000_000)
        w = _make_worker(tmp_path)  # no task id in not_running_task_ids
        _seed_reopened_top_level_session(
            w.session_store, task_id="stuck-active", routing="claude_code", cli_id="cli-active-1",
        )

        count = w._reconcile_stuck_claimed_sessions()

        assert count == 1
        assert w._sent


# ---------------------------------------------------------------------------
# Truthful resume confirmation — the note is never lost, "Resumed" is never
# sent before a subprocess is confirmed to have actually launched
# ---------------------------------------------------------------------------

class TestResumedConfirmation:
    def test_resume_confirmation_sent_once_launch_is_confirmed(self, tmp_path):
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-confirm", routing="claude_code", cli_id="cli-confirm-1",
        )

        w._dispatch_spawned_sessions()

        assert any("Resumed" in text for _id, text in w._sent_with_ids)
        # It's still sent before the completion notice that follows it in
        # the same turn (the executor stub "launches" synchronously, so
        # confirmation and completion land in the same dispatch call).
        resumed_index = next(i for i, (_id, t) in enumerate(w._sent_with_ids) if "Resumed" in t)
        assert stub.resume_calls  # the turn did in fact run
        assert resumed_index == 0
        # A confirmed launch is what marks the note delivered.
        assert not w.session_store.has_pending_messages(session.session_id)

    def test_resume_that_never_launches_leaves_note_queued_and_reports_failure(self, tmp_path):
        """A resume attempt that returns an ordinary FAILED outcome without
        ever starting a subprocess (a resolution failure inside the
        executor, no `claude_code_spawn` transcript event) must not claim
        the session resumed or lose the operator's note. Since the
        executor returned a real outcome (no exception), the failure is
        reported by the ordinary shared FAILED-outcome handling — with the
        executor's own accurate reason — rather than a separate notice."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_FAILED, reason="no claude_code_session_id on record"),
            tmp_path=tmp_path, launches=False,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-never-launched", routing="claude_code", cli_id="cli-never-1",
        )

        w._dispatch_spawned_sessions()

        assert not any("Resumed" in text for _id, text in w._sent_with_ids)
        assert not any("Resumed" in text for text in w._sent)
        assert w.session_store.get("cc-never-launched").status == STATUS_FAILED
        assert any("no claude_code_session_id on record" in text for text in w._sent) or any(
            "no claude_code_session_id on record" in text for _id, text in w._sent_with_ids
        )
        # The note was never confirmed delivered — it's still queued.
        assert w.session_store.has_pending_messages(session.session_id)

    def test_resume_crash_before_launch_leaves_note_queued_and_reports_failure(self, tmp_path):
        """The exact scenario the review probed: `_execute_resume` raises
        before any subprocess ever started. The prior behavior sent
        "Resumed" anyway and silently dropped the note (marked delivered
        by the caller before dispatch even ran)."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="unused"),
            tmp_path=tmp_path, launches=False, raises=RuntimeError("boom-before-launch"),
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-crash-before", routing="claude_code", cli_id="cli-crash-1",
        )

        w._dispatch_spawned_sessions()

        assert w.session_store.get("cc-crash-before").status == STATUS_FAILED
        assert not any("Resumed" in text for _id, text in w._sent_with_ids)
        assert not any("Resumed" in text for text in w._sent)
        assert any("didn't start" in text for text in w._sent) or any(
            "didn't start" in text for _id, text in w._sent_with_ids
        )
        assert w.session_store.has_pending_messages(session.session_id)

    def test_resume_crash_after_launch_still_confirms_and_delivers(self, tmp_path):
        """A crash in worker-side glue AFTER a subprocess genuinely started
        is a different case: the resume really did happen, so the note is
        consumed and "Resumed" is truthful, even though the session still
        ends FAILED."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="unused"),
            tmp_path=tmp_path, launches=True, raises=RuntimeError("boom-after-launch"),
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-crash-after", routing="claude_code", cli_id="cli-crash-2",
        )

        w._dispatch_spawned_sessions()

        assert w.session_store.get("cc-crash-after").status == STATUS_FAILED
        assert any("Resumed" in text for _id, text in w._sent_with_ids)
        assert not w.session_store.has_pending_messages(session.session_id)

    def test_permission_error_on_spawn_is_not_counted_as_a_launch(self, tmp_path):
        """The real `ClaudeCodeExecutor` writes its `claude_code_spawn`
        transcript marker unconditionally before `Popen` (a deliberate
        crash-safety margin), then must compensate that marker on ANY
        spawn-time OS failure — not just `FileNotFoundError`. A
        `PermissionError` (binary exists, isn't executable) is exactly as
        real a non-launch as a missing binary: uncompensated, it would
        make `_cli_subprocess_launch_count` miscount a launch that never
        happened, and `_confirm_resume_or_requeue` would send a false
        "Resumed" while marking the note delivered."""
        from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor

        def _raise_permission_error(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        session_store = SessionStore(db_path=tmp_path / "sessions.db")
        transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
        executor = ClaudeCodeExecutor(
            session_store=session_store, transcript_store=transcript_store,
            spawn_fn=_raise_permission_error,
        )
        w = _make_worker(tmp_path, claude_code_executor=executor)
        session = _seed_reopened_top_level_session(
            w.session_store, task_id="cc-permission-error", routing="claude_code", cli_id="cli-perm-1",
        )

        w._dispatch_spawned_sessions()

        assert w.session_store.get("cc-permission-error").status == STATUS_FAILED
        assert not any("Resumed" in text for _id, text in w._sent_with_ids)
        assert not any("Resumed" in text for text in w._sent)
        # The note must not be lost — no subprocess actually launched.
        assert w.session_store.has_pending_messages(session.session_id)

    def test_spawned_child_resume_does_not_send_a_resumed_confirmation(self, tmp_path):
        """Children stay silent to the operator, same as every other
        message this dispatch sends — the parent's own turn reports for
        them."""
        stub = _StubCliExecutor(
            outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"), tmp_path=tmp_path,
        )
        w = _make_worker(tmp_path, claude_code_executor=stub)
        parent = w.session_store.create(task_id="parent-2", routing="claude_code", status=STATUS_COMPLETED)
        child = w.session_store.create(
            task_id="child-2", routing="claude_code", status=STATUS_CLAIMED,
            parent_session_id=parent.session_id, root_session_id=parent.session_id,
        )
        w.session_store.set_claude_code_session_id("child-2", "cli-child-1")
        w.session_store.enqueue_message(child.session_id, "operator", "child follow-up")

        w._dispatch_spawned_sessions()

        assert stub.resume_calls == [("child-2", "child follow-up")]
        assert not any("Resumed" in text for _id, text in w._sent_with_ids)
        assert w._sent == []
