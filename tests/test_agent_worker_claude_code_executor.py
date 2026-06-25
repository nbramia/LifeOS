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
    """Stand-in for `subprocess.Popen` with predetermined stdout + returncode."""

    def __init__(self, events: Iterable[dict], returncode: int = 0, stderr: str = ""):
        self.stdout = _FakeStdout(events)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _spawn_with(events, returncode: int = 0, stderr: str = ""):
    """Build a `spawn_fn` callable that returns a fresh _FakeProc per call."""

    def _spawn_fn(*_args, **_kwargs):
        return _FakeProc(events, returncode=returncode, stderr=stderr)

    return _spawn_fn


def _build_executor(
    tmp_path: Path,
    *,
    spawn_fn,
    notifications: list[str] | None = None,
    mirror_calls: list[tuple[str, str]] | None = None,
):
    db_path = tmp_path / "sessions.db"
    transcript_dir = tmp_path / "transcripts"
    store = SessionStore(db_path=db_path)
    transcripts = TranscriptStore(transcripts_dir=transcript_dir)

    notify = (lambda msg: notifications.append(msg)) if notifications is not None else None
    # #311: a fake (session_id, body) sink so a test can assert the executor
    # mirrors each streamed [NOTIFY]/[CLARIFY]/[GOAL] into the web thread.
    mirror = (lambda sid, body: mirror_calls.append((sid, body))) if mirror_calls is not None else None
    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        notification_callback=notify,
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


def test_conversation_mirror_invoked_for_each_streamed_body(tmp_path: Path):
    """#311: when a conversation_mirror is wired, the executor calls it with
    (session_id, body) for each streamed [NOTIFY]/[CLARIFY]/[GOAL] — the same
    bodies it relays to Telegram — so the web thread mirrors live progress."""
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

    # Every streamed body is mirrored with the session id, in order, matching
    # exactly what Telegram received.
    assert mirror_calls == [
        (session.session_id, "Reading the file."),
        (session.session_id, "Which repo?"),
        (session.session_id, "Tests pass."),
    ]
    assert [b for _, b in mirror_calls] == notifications


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
    assert outcome.final_text == "Which file did you mean?"
    assert notifications == ["Which file did you mean?"]
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


def test_goal_returns_blocked_streams_and_records(tmp_path: Path):
    """End-to-end: an assistant [GOAL] then a result event yields a BLOCKED
    outcome awaiting approval, the body is streamed to the operator, and the
    transcript records the proposed condition."""
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
    assert outcome.final_text == "all tests pass"
    # The operator must SEE the proposed goal to approve it.
    assert notifications == ["all tests pass"]
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
