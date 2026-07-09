"""Tests for the agent worker's ClaudeCodeExecutor.

Drives the executor against a fake subprocess whose stdout emits a scripted
sequence of stream-json events. No real Claude CLI is invoked. Exercises:
  * init event → CLI session UUID captured + persisted
  * [NOTIFY] events → notification callback fired, transcript appended
  * [CLARIFY] events → BLOCKED outcome, session status updated
  * plan-mode result → BLOCKED outcome (awaiting approval)
  * happy-path result → COMPLETED outcome with final text
  * cost-cap exceeded → BUDGET_EXCEEDED outcome
  * non-zero exit without terminal event → FAILED outcome
  * binary not found → FAILED outcome with a stable reason code
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest

from api.services.agent_worker.claude_code_executor import (
    REASON_AWAITING_CLARIFICATION,
    REASON_AWAITING_GOAL_APPROVAL,
    REASON_AWAITING_PLAN_APPROVAL,
    REASON_BINARY_NOT_FOUND,
    REASON_KILLED,
    ClaudeCodeExecutor,
)
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Iterable that yields scripted stream-json lines, then EOFs."""

    def __init__(self, events: Iterable[dict]):
        self._lines = [json.dumps(e) + "\n" for e in events]
        self._idx = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._idx >= len(self._lines):
            raise StopIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


class _FakeStderr:
    def __init__(self, payload: str = ""):
        self._payload = payload

    def read(self) -> str:
        return self._payload


class _FakeProc:
    """Stand-in for `subprocess.Popen` with predetermined stdout + returncode.

    `pid` is set so the executor's #379 pid-recording (os.getpgid(proc.pid))
    has something to read; an `on_wait` hook lets a test simulate the operator
    flipping the session row to FAILED while `wait()` blocks (the kill-mid-run
    case).
    """

    def __init__(self, events: Iterable[dict], returncode: int = 0, stderr: str = "",
                 pid: int = 4242, on_wait=None):
        self.stdout = _FakeStdout(events)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.pid = pid
        self._on_wait = on_wait
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._on_wait is not None:
            self._on_wait()
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _spawn_with(events, returncode: int = 0, stderr: str = "", pid: int = 4242, on_wait=None):
    """Build a `spawn_fn` callable that returns a fresh _FakeProc per call."""

    def _spawn_fn(*_args, **_kwargs):
        return _FakeProc(events, returncode=returncode, stderr=stderr, pid=pid, on_wait=on_wait)

    return _spawn_fn


def _build_executor(
    tmp_path: Path,
    *,
    spawn_fn,
    notifications: list[str] | None = None,
    mirror_calls: list[tuple[str, str]] | None = None,
    operator_sends: list[tuple[str, str]] | None = None,
):
    db_path = tmp_path / "sessions.db"
    transcript_dir = tmp_path / "transcripts"
    store = SessionStore(db_path=db_path)
    transcripts = TranscriptStore(transcripts_dir=transcript_dir)

    notify = (lambda msg: notifications.append(msg)) if notifications is not None else None
    # #311: a fake (session_id, body) sink so a test can assert the executor
    # mirrors each streamed [NOTIFY]/[CLARIFY]/[GOAL] into the web thread.
    mirror = (lambda sid, body: mirror_calls.append((sid, body))) if mirror_calls is not None else None
    # #458: preferred operator sender — receives (session, body) so the worker
    # can register reply anchors. Tests capture (session_id, body).
    op_send = (
        (lambda session, body: operator_sends.append((session.session_id, body)))
        if operator_sends is not None else None
    )
    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        notification_callback=notify,
        operator_send=op_send,
        conversation_mirror=mirror,
        spawn_fn=spawn_fn,
        binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30,
        heartbeat_interval=3600,
    )
    return executor, store, transcripts


def _seed_session(store: SessionStore, *, task_id: str = "task-1"):
    """Drop a routing='claude_code' operator-origin session into the store, the
    shape the spawn surface produces.
    """
    return store.create(
        task_id=task_id,
        routing="claude_code",
        origin="operator",
    )


def _seed_child_session(
    store: SessionStore, *, task_id: str = "child-1", claude_code_model: str | None = None,
):
    """A claude_code session spawned by another agent — has a parent, so it
    reports back to that parent rather than streaming to the operator (#349)."""
    return store.create(
        task_id=task_id,
        routing="claude_code",
        parent_session_id="sess_parent",
        claude_code_model=claude_code_model,
    )


def _read_transcript(transcripts: TranscriptStore, session_id: str) -> list[dict]:
    return transcripts.read(session_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_init_event_captures_and_persists_cli_session_id(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-sess-abc"},
        {"type": "result", "session_id": "cli-sess-abc", "total_cost_usd": 0.01, "result": "All set."},
    ]
    executor, store, transcripts = _build_executor(tmp_path, spawn_fn=_spawn_with(events))
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "Print a haiku"})

    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "All set."
    refreshed = store.get(session.task_id)
    assert refreshed is not None
    assert refreshed.claude_code_session_id == "cli-sess-abc"
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    assert "claude_code_init" in kinds
    assert "claude_code_completed" in kinds


def test_notify_invokes_callback_and_records_transcript(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Read the file."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Done."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.05, "result": "[NOTIFY] Done."},
    ]
    notifications: list[str] = []
    executor, store, transcripts = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "Read the file"})

    assert outcome.status == STATUS_COMPLETED
    assert notifications == ["Read the file.", "Done."]
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    assert kinds.count("claude_code_notify") == 2
    # When the assistant emits only [NOTIFY] blocks (no narrative prose), the
    # bodies are already streamed via the callback and final_text stays empty
    # so the worker won't repeat the same content in a terminal summary.
    assert outcome.final_text == ""


def test_assistant_narrative_outside_notify_is_kept_as_final_text(tmp_path: Path):
    """Mixed narrative + [NOTIFY] preserves the narrative for the summary."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Found a typo.\n[NOTIFY] Fixed it."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "fix the typo"})
    assert outcome.status == STATUS_COMPLETED
    assert "Found a typo." in outcome.final_text
    assert "NOTIFY" not in outcome.final_text
    assert notifications == ["Fixed it."]


def test_child_notify_not_streamed_and_folded_into_final_text(tmp_path: Path):
    """A spawned child (#349) must NOT stream [NOTIFY] to the operator. Instead
    the bodies are folded into final_text so the parent — which only reads
    final_text — still receives the substance the child reported."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Match 1: A vs B."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Match 2: C vs D."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.05, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, transcripts = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_child_session(store)

    outcome = executor.execute(session, {"description": "list today's matches"})

    assert outcome.status == STATUS_COMPLETED
    # Nothing streamed to the operator's Telegram.
    assert notifications == []
    # Both bodies are present in the text the parent receives.
    assert "Match 1: A vs B." in outcome.final_text
    assert "Match 2: C vs D." in outcome.final_text
    # Audit trail still records the bodies as transcript events.
    events = _read_transcript(transcripts, session.session_id)
    kinds = [e["kind"] for e in events]
    assert kinds.count("claude_code_notify") == 2
    # The completion event persists the folded text so the parent can read it
    # via _child_final_text (the child never streamed it to Telegram).
    completed = [e for e in events if e["kind"] == "claude_code_completed"]
    assert completed
    persisted = completed[0]["payload"]["final_text"]
    assert "Match 1: A vs B." in persisted and "Match 2: C vs D." in persisted


def test_child_clarify_folds_into_final_text_and_does_not_block(tmp_path: Path):
    """#356: a spawned child's [CLARIFY] must NOT message the operator and must
    NOT go BLOCKED (which would strand the yielded parent — it only resumes once
    every child is terminal). The question is folded into final_text, prefixed
    [needs clarification], so the parent reads it on resume and decides; the
    child's turn completes normally."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[CLARIFY] Use API v1 or v2?"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.05, "result": ""},
    ]
    notifications: list[str] = []
    mirror_calls: list[tuple[str, str]] = []
    executor, store, transcripts = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events),
        notifications=notifications, mirror_calls=mirror_calls,
    )
    session = _seed_child_session(store)

    outcome = executor.execute(session, {"description": "wire the API call"})

    # Completes (not BLOCKED) so the parent isn't stranded.
    assert outcome.status == STATUS_COMPLETED
    # Nothing streamed to the operator — neither Telegram nor the web thread.
    assert notifications == []
    assert mirror_calls == []
    # The question is folded into the text the parent reads, marked as a request.
    assert "[needs clarification] Use API v1 or v2?" in outcome.final_text
    # Audit trail: the folded-clarify event, and NOT the operator-facing clarify.
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    assert "claude_code_child_clarify_folded" in kinds
    assert "claude_code_clarify" not in kinds
    # The completion persists the folded text so the parent reads it via
    # _child_final_text (the child never streamed it anywhere).
    completed = [
        e for e in _read_transcript(transcripts, session.session_id)
        if e["kind"] == "claude_code_completed"
    ]
    assert completed
    assert "[needs clarification] Use API v1 or v2?" in completed[0]["payload"]["final_text"]


def test_conversation_mirror_invoked_for_each_streamed_body(tmp_path: Path):
    """#311: when a conversation_mirror is wired, the executor calls it with
    (session_id, body) for each streamed [NOTIFY]/[CLARIFY]/[GOAL] so the web
    thread mirrors live progress. Only [NOTIFY] also relays to Telegram here —
    [CLARIFY] and [GOAL] deliver via the worker's single anchored
    body + reply-instructions message at block time (#456, #458)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Reading the file."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[CLARIFY] Which repo?"}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[GOAL] Tests pass."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.05, "result": ""},
    ]
    notifications: list[str] = []
    mirror_calls: list[tuple[str, str]] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events),
        notifications=notifications, mirror_calls=mirror_calls,
    )
    session = _seed_session(store)

    executor.execute(session, {"description": "do the thing"})

    # Every streamed body is mirrored with the session id, in order. Telegram
    # received only the [NOTIFY] — clarify and goal deliver via the worker's
    # anchored block-time messages instead.
    assert mirror_calls == [
        (session.session_id, "Reading the file."),
        (session.session_id, "Which repo?"),
        (session.session_id, "Tests pass."),
    ]
    assert notifications == ["Reading the file."]


def test_child_session_does_not_mirror(tmp_path: Path):
    """#311: a child session stays silent to the operator, and its [NOTIFY]
    bodies are folded into final_text — so they must NOT be mirrored to a web
    thread either (parity with the no-Telegram gate)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] child progress"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    mirror_calls: list[tuple[str, str]] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events),
        notifications=notifications, mirror_calls=mirror_calls,
    )
    session = _seed_child_session(store)

    executor.execute(session, {"description": "list matches"})

    assert notifications == []
    assert mirror_calls == []


def test_raising_conversation_mirror_does_not_abort_run(tmp_path: Path):
    """#311 (review): a conversation_mirror that raises (e.g. a transient DB
    lock) must NOT kill the assistant-event loop — the session still completes,
    later bodies still stream to Telegram, and the terminal result is produced.
    Mirrors the existing guard around the Telegram notification callback."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] First update."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Second update."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.02, "result": "All done."},
    ]
    notifications: list[str] = []

    def _boom(_sid, _body):
        raise RuntimeError("mirror sink exploded")

    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path=db_path)
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        notification_callback=lambda msg: notifications.append(msg),
        conversation_mirror=_boom,
        spawn_fn=_spawn_with(events),
        binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30,
        heartbeat_interval=3600,
    )
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "do the thing"})

    # The raising mirror was swallowed: the run reached its terminal result...
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "All done."
    # ...and BOTH notifies still streamed to Telegram (the loop never aborted).
    assert notifications == ["First update.", "Second update."]


def test_build_command_uses_session_model_tier(tmp_path: Path):
    """A child seeded with claude_code_model='haiku' runs the CLI with
    --model haiku; the default (None) falls back to opus (#349)."""
    captured: dict = {}

    def _spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc([
            {"type": "system", "subtype": "init", "session_id": "cli-1"},
            {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
        ])

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_capture)
    session = _seed_child_session(store, claude_code_model="haiku")
    executor.execute(session, {"description": "simple lookup"})
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_build_command_defaults_to_opus(tmp_path: Path):
    captured: dict = {}

    def _spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc([
            {"type": "system", "subtype": "init", "session_id": "cli-1"},
            {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
        ])

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_capture)
    session = _seed_session(store)  # operator session, no tier set
    executor.execute(session, {"description": "anything"})
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "opus"


def _captured_system_prompt(tmp_path: Path, session_factory) -> str:
    """Run the executor with a capture spawn_fn and return the value passed
    to --append-system-prompt."""
    captured: dict = {}

    def _spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc([
            {"type": "system", "subtype": "init", "session_id": "cli-1"},
            {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
        ])

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_capture)
    session = session_factory(store)
    executor.execute(session, {"description": "anything"})
    cmd = captured["cmd"]
    return cmd[cmd.index("--append-system-prompt") + 1]


def test_operator_system_prompt_keeps_pause_and_relay_clarify_wording(tmp_path: Path):
    """Operator /claude sessions do pause on [CLARIFY] (BLOCKED + Telegram
    question) — their prompt must keep promising exactly that."""
    prompt = _captured_system_prompt(tmp_path, _seed_session)
    assert "Your session will pause and the user's" in prompt
    assert "answer will be relayed back to you" in prompt
    assert "parent agent" not in prompt


def test_child_system_prompt_describes_parent_clarify_routing(tmp_path: Path):
    """A spawned child's [CLARIFY] folds into its output and the turn ends
    (#356) — its prompt must describe that honestly instead of promising an
    operator pause-and-relay that never happens."""
    prompt = _captured_system_prompt(tmp_path, _seed_child_session)
    assert "Your session will pause and the user's" not in prompt
    assert "parent agent" in prompt
    # The child is still told to stop after asking.
    assert "STOP" in prompt


def test_tool_use_records_transcript_event(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x.md"}}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "done"},
    ]
    executor, store, transcripts = _build_executor(tmp_path, spawn_fn=_spawn_with(events))
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "do a thing"})
    assert outcome.status == STATUS_COMPLETED
    events = _read_transcript(transcripts, session.session_id)
    tool_events = [e for e in events if e["kind"] == "claude_code_tool_use"]
    assert tool_events and tool_events[0]["payload"]["name"] == "Read"


# ---------------------------------------------------------------------------
# Blocked-on-input outcomes
# ---------------------------------------------------------------------------


def test_clarify_returns_blocked_with_question(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[CLARIFY] Which file did you mean?"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "edit the file"})

    assert outcome.status == STATUS_BLOCKED
    assert outcome.reason == REASON_AWAITING_CLARIFICATION
    # The question rides the outcome; the worker composes the single anchored
    # question + reply-instructions message at block time (#458) — no separate
    # streamed Telegram message.
    assert outcome.final_text == "Which file did you mean?"
    assert notifications == []
    assert store.get(session.task_id).status == STATUS_BLOCKED


def test_plan_mode_result_returns_blocked_with_plan(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Step 1. Step 2."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.02, "result": ""},
    ]
    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_with(events))
    session = _seed_session(store)

    outcome = executor.execute(
        session, {"description": "refactor X", "plan_mode": True},
    )

    assert outcome.status == STATUS_BLOCKED
    assert outcome.reason == REASON_AWAITING_PLAN_APPROVAL
    assert "Step 1" in outcome.final_text
    assert store.get(session.task_id).status == STATUS_BLOCKED


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_high_cost_does_not_cap_subscription_route(tmp_path: Path, monkeypatch):
    """Claude Code is subscription-billed — a high reported cost must NOT cap the
    task. Only the managed/API route enforces a dollar cap. Even with a tiny
    claude_max_cost_usd, a $0.99 turn completes normally (cost is still tracked
    for /agents reporting, just not enforced)."""
    monkeypatch.setattr(
        "api.services.agent_worker.claude_code_executor.settings.claude_max_cost_usd",
        0.10,
    )
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.99, "result": "done"},
    ]
    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_with(events))
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "expensive task"})
    assert outcome.status == STATUS_COMPLETED
    assert store.get(session.task_id).status == STATUS_COMPLETED


def test_binary_not_found_returns_failed(tmp_path: Path):
    def _raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError("claude")

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_raise_fnf)
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "anything"})
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == REASON_BINARY_NOT_FOUND


def test_nonzero_exit_without_terminal_returns_failed(tmp_path: Path):
    # No init / result events — stdout just closes; subprocess exits 2.
    events: list[dict] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events, returncode=2, stderr="boom"),
    )
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "broken thing"})
    assert outcome.status == STATUS_FAILED
    assert "exited with code 2" in outcome.reason


def test_empty_prompt_returns_failed(tmp_path: Path):
    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_with([]))
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "   "})
    assert outcome.status == STATUS_FAILED
    assert "empty prompt" in outcome.reason


# ---------------------------------------------------------------------------
# Resume entry point
# ---------------------------------------------------------------------------


def test_resume_without_code_session_id_returns_failed(tmp_path: Path):
    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_with([]))
    session = _seed_session(store)
    outcome = executor.resume(session, "follow-up message")
    assert outcome.status == STATUS_FAILED
    assert "no claude_code_session_id" in outcome.reason


def test_resume_passes_session_id_via_resume_flag(tmp_path: Path):
    captured: dict = {}

    def _spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(
            [
                {"type": "system", "subtype": "init", "session_id": "cli-1"},
                {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
            ],
        )

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_capture)
    session = _seed_session(store)
    store.set_claude_code_session_id(session.task_id, "cli-original")
    session = store.get(session.task_id)

    outcome = executor.resume(session, "what about edge cases?")

    assert outcome.status == STATUS_COMPLETED
    assert "-r" in captured["cmd"]
    assert "cli-original" in captured["cmd"]


def test_system_prompt_carries_session_id_for_delegation(tmp_path: Path):
    """The appended system prompt tells the agent its LifeOS session id and how
    to delegate (so it can pass caller_session_id to lifeos_agent_spawn)."""
    captured: dict = {}

    def _spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(
            [
                {"type": "system", "subtype": "init", "session_id": "cli-1"},
                {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
            ],
        )

    executor, store, _ = _build_executor(tmp_path, spawn_fn=_spawn_capture)
    session = _seed_session(store)
    store.enqueue_message(session.session_id, "operator", "do a thing")

    executor.execute(session, {"id": session.task_id, "description": "do a thing"})

    # The --append-system-prompt value is the arg right after the flag.
    cmd = captured["cmd"]
    appended = cmd[cmd.index("--append-system-prompt") + 1]
    assert session.session_id in appended
    assert "lifeos_agent_spawn" in appended


# ---------------------------------------------------------------------------
# Protocol tag hardening (#402): fence-aware scan + malformed-tag tolerance
# ---------------------------------------------------------------------------


def test_scan_splits_adjacent_and_interleaved_tags():
    """Adjacent/interleaved well-formed tags split into discrete bodies and are
    stripped from the narrative (the lazy regex + lookahead, no regression)."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("before [NOTIFY] alpha [CLARIFY] beta [NOTIFY] gamma")
    assert scan.notify == ["alpha", "gamma"]
    assert scan.clarify == ["beta"]
    assert scan.narrative.strip() == "before"


def test_scan_ignores_tags_inside_code_fence():
    """A tag inside a fenced code block is illustrative — not extracted, and
    preserved verbatim in the narrative."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    text = "ship it [NOTIFY] real\n```python\n[NOTIFY] example only\n```\nthe end"
    scan = _scan_protocol_tags(text)
    assert scan.notify == ["real"]  # the fenced one is NOT extracted
    assert "[NOTIFY] example only" in scan.narrative  # preserved inside the fence
    assert "example only" not in " ".join(scan.notify)


def test_scan_scrubs_malformed_unclosed_tag():
    """An unclosed/malformed tag marker does not leak into the narrative, while
    a well-formed tag in the same text is still extracted."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("note [NOTIFY oops no bracket, then [NOTIFY] real one")
    assert scan.notify == ["real one"]
    assert "[NOTIFY" not in scan.narrative


def test_scan_ignores_empty_body_tags():
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("[NOTIFY]   [CLARIFY]   ")
    assert scan.notify == []
    assert scan.clarify == []
    assert scan.narrative.strip() == ""


def test_notify_inside_code_fence_not_streamed(tmp_path: Path):
    """End-to-end: a [NOTIFY] inside a fenced block must NOT fire the operator
    notification callback; only the real one outside the fence does (#402)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text",
                "text": "```\n[NOTIFY] inside fence\n```\n[NOTIFY] outside fence"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "do a thing"})
    assert outcome.status == STATUS_COMPLETED
    assert notifications == ["outside fence"]


def test_scan_inline_backticks_in_tag_body_not_treated_as_fence():
    """An inline triple-backtick span inside a tag body is NOT a fence (fences
    are line-anchored), so the body is extracted intact — not truncated."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("[CLARIFY] should I use ```black``` or autopep8?")
    assert scan.clarify == ["should I use ```black``` or autopep8?"]
    assert scan.notify == []


def test_scan_ignores_tags_inside_tilde_fence():
    """The ~~~ fence variant is handled the same as ```."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("do it [NOTIFY] go\n~~~\n[NOTIFY] sample\n~~~\ndone")
    assert scan.notify == ["go"]  # fenced one not extracted
    assert "[NOTIFY] sample" in scan.narrative


def test_scan_orphan_scrub_does_not_eat_partial_words():
    """The orphan-marker scrub must not strip the prefix of an unrelated word
    that merely starts with NOTIFY/CLARIFY (e.g. '[NOTIFYING ...]')."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("the agent is [NOTIFYING you] about it")
    assert scan.notify == []
    assert "[NOTIFYING you]" in scan.narrative


def test_result_event_fenced_tag_preserved_in_final_text(tmp_path: Path):
    """The result-event path is fence-aware too: a fenced tag in the result
    text survives in final_text rather than being stripped."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01,
         "result": "Summary:\n```\n[NOTIFY] example\n```\nall done"},
    ]
    notifications: list[str] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)
    outcome = executor.execute(session, {"description": "x"})
    assert outcome.status == STATUS_COMPLETED
    assert "[NOTIFY] example" in outcome.final_text  # fenced tag preserved
    assert notifications == []  # result path never streams


def test_operator_send_preferred_for_notify_bodies(tmp_path: Path):
    """#458: when operator_send is wired, streamed [NOTIFY] bodies go through
    it with the session (so the worker can register reply anchors) and the
    legacy notification_callback is NOT used."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[NOTIFY] Reading the file."}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "done"},
    ]
    notifications: list[str] = []
    operator_sends: list[tuple[str, str]] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events),
        notifications=notifications, operator_sends=operator_sends,
    )
    session = _seed_session(store)

    executor.execute(session, {"description": "read it"})

    assert operator_sends == [(session.session_id, "Reading the file.")]
    assert notifications == []  # legacy path bypassed when operator_send set


# ---------------------------------------------------------------------------
# [GOAL] tag (#398)
# ---------------------------------------------------------------------------


def test_scan_extracts_goal_body_and_strips_from_narrative():
    """A [GOAL] body is extracted and removed from the operator-facing narrative
    just like [NOTIFY]/[CLARIFY]."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("here's the plan [GOAL] all tests pass")
    assert scan.goal == ["all tests pass"]
    assert scan.notify == []
    assert scan.clarify == []
    assert scan.narrative.strip() == "here's the plan"


def test_scan_splits_goal_interleaved_with_notify_and_clarify():
    """GOAL interleaved with NOTIFY/CLARIFY splits into discrete bodies and none
    bleeds into another tag's body (shared lookahead)."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags(
        "lead [NOTIFY] alpha [GOAL] beta [CLARIFY] gamma [GOAL] delta"
    )
    assert scan.notify == ["alpha"]
    assert scan.goal == ["beta", "delta"]
    assert scan.clarify == ["gamma"]
    assert scan.narrative.strip() == "lead"


def test_scan_ignores_goal_inside_code_fence():
    """A [GOAL] inside a fenced block is illustrative — not extracted, preserved
    verbatim in the narrative."""
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    text = "do it [GOAL] real\n```\n[GOAL] example only\n```\nend"
    scan = _scan_protocol_tags(text)
    assert scan.goal == ["real"]  # fenced one not extracted
    assert "[GOAL] example only" in scan.narrative


def test_scan_ignores_empty_goal_body():
    from api.services.agent_worker.claude_code_executor import _scan_protocol_tags

    scan = _scan_protocol_tags("[GOAL]   ")
    assert scan.goal == []
    assert scan.narrative.strip() == ""


def test_goal_returns_blocked_without_streaming_and_records(tmp_path: Path):
    """End-to-end: an assistant [GOAL] then a result event yields a BLOCKED
    outcome awaiting approval, carrying the condition as final_text, and the
    transcript records it. The body is NOT streamed as its own Telegram
    notification — the worker delivers goal + reply instructions as ONE
    anchored message at block time, so the operator replies to the message
    that shows the goal itself."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[GOAL] all tests pass"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, transcripts = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "make the suite green"})

    assert outcome.status == STATUS_BLOCKED
    assert outcome.reason == REASON_AWAITING_GOAL_APPROVAL
    # The condition rides the outcome so the worker can compose the single
    # goal + instructions message the operator replies to.
    assert outcome.final_text == "all tests pass"
    assert notifications == []  # no separate streamed goal message
    assert store.get(session.task_id).status == STATUS_BLOCKED
    events_out = _read_transcript(transcripts, session.session_id)
    awaiting = [e for e in events_out if e["kind"] == "claude_code_awaiting_goal_approval"]
    assert awaiting and awaiting[0]["payload"]["condition"] == "all tests pass"


def test_child_goal_not_streamed_to_operator(tmp_path: Path):
    """A spawned child's [GOAL] stays silent to the operator (like [NOTIFY])
    while still blocking and recording the condition."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[GOAL] benchmark beats baseline"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, _ = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_child_session(store)

    outcome = executor.execute(session, {"description": "tune the model"})

    assert outcome.status == STATUS_BLOCKED
    assert outcome.reason == REASON_AWAITING_GOAL_APPROVAL
    assert notifications == []  # child stays silent


def test_clarify_takes_precedence_over_goal_in_same_turn(tmp_path: Path):
    """When one assistant turn emits BOTH [CLARIFY] and [GOAL], clarification
    wins — the session blocks awaiting the answer and the goal is NOT locked
    (it must be re-proposed once the clarification resolves)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text",
                "text": "[CLARIFY] which suite? [GOAL] all tests pass"}]},
        },
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": ""},
    ]
    notifications: list[str] = []
    executor, store, transcripts = _build_executor(
        tmp_path, spawn_fn=_spawn_with(events), notifications=notifications,
    )
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "make it green"})

    assert outcome.status == STATUS_BLOCKED
    assert outcome.reason == REASON_AWAITING_CLARIFICATION  # clarification wins
    assert outcome.final_text == "which suite?"
    # The goal proposal did NOT produce a goal-approval block this turn.
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    assert "claude_code_awaiting_goal_approval" not in kinds


# ---------------------------------------------------------------------------
# #379 — operator kill terminates the local subprocess promptly
# ---------------------------------------------------------------------------


def test_spawn_records_pid_event_and_new_session(tmp_path: Path):
    """#379: a successful spawn records a `claude_code_pid` event carrying the
    subprocess pid/pgid, and passes `start_new_session=True` to the spawn_fn so
    the subprocess is its own process-group leader (killable via killpg without
    touching the worker)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "done."},
    ]
    captured_kwargs: dict = {}

    def _capturing_spawn(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeProc(events, pid=98765)

    executor, store, transcripts = _build_executor(tmp_path, spawn_fn=_capturing_spawn)
    session = _seed_session(store)

    outcome = executor.execute(session, {"description": "print a haiku"})

    assert outcome.status == STATUS_COMPLETED
    # The spawn was asked to start a new session/process-group.
    assert captured_kwargs.get("start_new_session") is True
    # The pid event was recorded with both pid and pgid.
    pid_events = [
        e for e in _read_transcript(transcripts, session.session_id)
        if e["kind"] == "claude_code_pid"
    ]
    assert len(pid_events) == 1
    assert pid_events[0]["payload"]["pid"] == 98765
    assert "pgid" in pid_events[0]["payload"]


def test_operator_kill_mid_run_exits_silently(tmp_path: Path):
    """#379: when the operator kill flips the session row to FAILED while the
    subprocess runs, `proc.wait()` returns and the executor must exit silently —
    REASON_KILLED, a `claude_code_killed` event, and NEITHER a
    `claude_code_failed` nor a COMPLETED transition."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
    ]
    # Build the store/session first so the on_wait hook can flip its status,
    # simulating the operator kill endpoint reaching the row mid-run.
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    session = _seed_session(store)

    def _flip_to_failed():
        store.update_status(session.task_id, STATUS_FAILED)

    # A negative returncode mimics a process killed by a signal (killpg/SIGKILL).
    spawn_fn = _spawn_with(events, returncode=-9, on_wait=_flip_to_failed)
    notifications: list[str] = []
    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        notification_callback=lambda msg: notifications.append(msg),
        spawn_fn=spawn_fn,
        binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30,
        heartbeat_interval=3600,
    )

    outcome = executor.execute(session, {"description": "long task"})

    assert outcome.status == STATUS_FAILED
    assert outcome.reason == REASON_KILLED
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    assert "claude_code_killed" in kinds
    # The spurious failure / completion paths must NOT have run.
    assert "claude_code_failed" not in kinds
    assert "claude_code_completed" not in kinds


def test_clean_completion_wins_over_raced_failed_flip(tmp_path: Path):
    """#379 cascade-race guard: the row can be flipped to FAILED mid-run by a
    second legitimate writer (LocalExecutor._cascade_kill_lineage on a
    lineage-budget breach, which is routing-agnostic). If our subprocess exits 0
    at that instant — it finished its work — the COMPLETED path must win so the
    final_text is persisted (a parent reads it via _child_final_text). The
    returncode gate (FAILED *and* returncode != 0 → REASON_KILLED) keeps the
    silent guard from clobbering a clean completion."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-1"},
        {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "all done."},
    ]
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    session = _seed_session(store)

    def _flip_to_failed():
        # The cascade races our clean exit, flipping the row FAILED.
        store.update_status(session.task_id, STATUS_FAILED)

    # returncode=0 → the subprocess completed cleanly despite the FAILED flip.
    spawn_fn = _spawn_with(events, returncode=0, on_wait=_flip_to_failed)
    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        spawn_fn=spawn_fn,
        binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30,
        heartbeat_interval=3600,
    )

    outcome = executor.execute(session, {"description": "task"})

    assert outcome.status == STATUS_COMPLETED
    assert outcome.reason != REASON_KILLED
    assert outcome.final_text == "all done."
    kinds = [e["kind"] for e in _read_transcript(transcripts, session.session_id)]
    # The completion path ran and persisted final_text; the silent guard did not.
    assert "claude_code_completed" in kinds
    assert "claude_code_killed" not in kinds
    completed = [e for e in _read_transcript(transcripts, session.session_id)
                 if e["kind"] == "claude_code_completed"]
    assert completed[0]["payload"]["final_text"] == "all done."
