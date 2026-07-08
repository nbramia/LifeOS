"""Worker dispatch wiring for routing='claude_code' sessions.

Verifies that ``_dispatch_spawned_sessions`` invokes the injected
``ClaudeCodeExecutor`` for operator-origin sessions with ``routing='claude_code'``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from api.services.conversation_store import ConversationStore


pytestmark = pytest.mark.unit


@dataclass
class _StubClaudeCodeExecutor:
    """Minimal ClaudeCodeExecutor stand-in: records calls + returns a canned outcome."""
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)
    resume_calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome

    def resume(self, session, message):
        self.resume_calls.append((session.task_id, message))
        return self.outcome


def _make_worker(tmp_path: Path, claude_code_executor):
    # No #agent task pickup happens in these tests — the worker only runs
    # spawned-session dispatch. A 404-everywhere transport is enough so the
    # `list_agent_tasks` and `_fetch_task` calls don't raise.
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        # Isolate the conversation DB to a tmp file: the default ConversationStore()
        # resolves to the production data/conversations.db, which a test must never
        # open or migrate.
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
        cli_pool=_SynchronousPool(),
    )


def _seed_code_session(store: SessionStore, *, task_id: str = "code-1"):
    session = store.create(
        task_id=task_id,
        routing="claude_code",
        origin="operator",
    )
    # Mirror the spawn surface contract: the prompt for the first
    # turn lives in pending_messages on the session row.
    store.enqueue_message(session.session_id, sender_id="operator", content="print hello")
    return session


def _recording_worker(tmp_path: Path, claude_code_executor):
    """Like ``_make_worker`` but records the vault-mutating HTTP calls so a
    test can assert the task was completed + tag-swapped (or not)."""
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={"tasks": []})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        # Isolate the conversation DB (default resolves to prod data/conversations.db).
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
        cli_pool=_SynchronousPool(),
    )
    return worker, calls


def _capturing_worker(tmp_path: Path, claude_code_executor):
    """Worker whose Telegram senders record every message, so a test can assert
    the exact operator-facing message count (#349)."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    sent: list[str] = []
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=store,
        # Isolate the conversation DB (default resolves to prod data/conversations.db).
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=transcripts,
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        telegram_send_with_id=lambda text: sent.append(text) or [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
        cli_pool=_SynchronousPool(),
    )
    return worker, store, transcripts, sent


def test_spawned_child_completion_does_not_telegram_operator(tmp_path: Path):
    """A spawned claude_code child (has a parent) must NOT send its completion
    text to the operator — the parent relays it in one message (#349)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="Match 1: A vs B at noon.")
    )
    worker, store, _, sent = _capturing_worker(tmp_path, claude_code_executor=stub)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    store.enqueue_message(child.session_id, "agent", "list today's matches")

    worker._dispatch_claude_code_session(child, [{"content": "list today's matches"}])

    assert sent == []  # nothing reached the operator


def test_operator_claude_session_still_telegrams_on_completion(tmp_path: Path):
    """Contrast: an operator /claude session (no parent) still surfaces its
    final text to the operator."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="All done.")
    )
    worker, store, _, sent = _capturing_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="op-1", routing="claude_code", origin="operator")
    store.enqueue_message(session.session_id, "operator", "do a thing")

    worker._dispatch_claude_code_session(session, [{"content": "do a thing"}])

    assert any("All done." in s for s in sent)


def test_child_final_text_reads_claude_code_completed_event(tmp_path: Path):
    """The parent pulls a claude_code child's final_text from the
    claude_code_completed transcript event — the child's only path out now that
    it stays silent to the operator (#349)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="")
    )
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=stub)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    transcripts.append(child.session_id, "claude_code_completed", {
        "final_chars": 23, "final_text": "Match 1: A vs B at noon.",
    })

    assert "Match 1: A vs B at noon." in worker._child_final_text(child)


def test_child_final_text_reads_codex_completed_event(tmp_path: Path):
    """The parent pulls a codex child's final_text from the codex_completed
    transcript event — the child's only path out now that the codex completion
    send is child-gated too (#429, parity with #349)."""
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=None)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="codex", parent_session_id=parent.session_id,
    )
    transcripts.append(child.session_id, "codex_completed", {
        "final_chars": 13, "final_text": "Child result.",
    })

    assert worker._child_final_text(child) == "Child result."


def test_child_final_text_legacy_codex_event_without_key_does_not_clobber(tmp_path: Path):
    """A legacy `codex_completed` event that predates #429 (final_chars only,
    no `final_text` key) must not wipe a real value from an earlier event —
    the key-presence guard is per-event, not per-kind."""
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=None)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="codex", parent_session_id=parent.session_id,
    )
    transcripts.append(child.session_id, "codex_completed", {
        "final_chars": 13, "final_text": "Child result.",
    })
    transcripts.append(child.session_id, "codex_completed", {
        "final_chars": 0,  # legacy: no final_text key at all
    })

    assert worker._child_final_text(child) == "Child result."


def test_resume_delivers_all_pending_messages(tmp_path: Path):
    """A resume dispatch must carry EVERY drained pending message, in order —
    not just pending[0]. Reopen-on-send (#428) makes multi-enqueue likely
    (e.g. a parent sends twice before the dispatch tick claims the reopened
    child), and each send already returned delivered=true."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="resumed.")
    )
    worker, store, _, _ = _capturing_worker(tmp_path, claude_code_executor=stub)
    parent = store.create(task_id="parent-1", routing="local")
    store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    # A persisted CLI session id is what routes dispatch into the resume branch.
    store.set_claude_code_session_id("child-1", "cli-uuid-1")
    child = store.get("child-1")

    worker._dispatch_claude_code_session(child, [
        {"content": "use the staging repo"},
        {"content": "and target the v2 branch"},
    ])

    assert stub.calls == []  # resume, never a fresh execute
    assert stub.resume_calls == [
        ("child-1", "use the staging repo\n\nand target the v2 branch"),
    ]


def test_child_final_text_latest_completed_event_wins_even_when_empty(tmp_path: Path):
    """The LATEST completed event's final_text wins even when empty (#428):
    a reopened child whose second run completes with no final text must not
    re-deliver the first run's '[needs clarification] …' question to the
    parent — that would invite a re-answer loop."""
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=None)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    transcripts.append(child.session_id, "claude_code_completed", {
        "final_chars": 33, "final_text": "[needs clarification] which repo?",
    })
    transcripts.append(child.session_id, "claude_code_completed", {
        "final_chars": 0, "final_text": "",
    })

    assert worker._child_final_text(child) == ""


def test_child_final_text_legacy_event_without_key_does_not_clobber(tmp_path: Path):
    """A legacy completed event that never carried a `final_text` key (pre-#349
    payloads recorded final_chars only) must not wipe a real value from an
    earlier event."""
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=None)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    transcripts.append(child.session_id, "claude_code_completed", {
        "final_chars": 24, "final_text": "Match 1: A vs B at noon.",
    })
    transcripts.append(child.session_id, "claude_code_completed", {
        "final_chars": 0,  # legacy: no final_text key at all
    })

    assert worker._child_final_text(child) == "Match 1: A vs B at noon."


def test_vault_claude_task_marked_complete_on_finish(tmp_path: Path):
    """A vault-routed ``#agent #claude`` task (origin != 'operator', no parent)
    must be reconciled in the vault when the Claude Code session completes:
    PUT .../complete and a #agent-running → #agent-completed swap. Regression
    for CLI tasks stranded at ``[/]`` / ``#agent-running`` forever (the CLI
    dispatch path bypasses ``_handle_outcome``)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, calls = _recording_worker(tmp_path, claude_code_executor=stub)
    # origin defaults to None → vault-backed task (not an operator spawn).
    session = worker.session_store.create(task_id="vault-1", routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "print hello"}])

    assert ("PUT", "/api/tasks/vault-1/complete") in calls
    assert ("POST", "/api/tasks/vault-1/swap-tag") in calls


def test_operator_spawn_does_not_touch_vault_on_finish(tmp_path: Path):
    """Operator-spawned /claude sessions have no backing #agent vault row, so
    completion must NOT issue complete / swap-tag calls (they would 404)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, calls = _recording_worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="spawn-1", routing="claude_code", origin="operator"
    )

    worker._dispatch_claude_code_session(session, [{"content": "print hello"}])

    assert not any(path.endswith(("/complete", "/swap-tag")) for _, path in calls)


def test_dispatch_calls_claude_code_executor(tmp_path: Path):
    """``_dispatch_spawned_sessions`` invokes the injected ClaudeCodeExecutor for
    an operator-origin routing='claude_code' session, draining the prompt from
    ``pending_messages`` as the task description.

    Status transitions are the executor's responsibility (the real
    ``ClaudeCodeExecutor`` calls ``update_status``; the stub doesn't, so we don't
    assert on ``session.status`` here).
    """
    # The dispatch path expects the spawn payload (a JSON-encoded dict)
    # produced by ``claude_code_spawn.spawn_claude_code_session``; the stub call's task
    # description is the decoded ``prompt`` field.
    stub = _StubClaudeCodeExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    w = _make_worker(tmp_path, claude_code_executor=stub)
    _seed_code_session(w.session_store, task_id="code-1")

    w._dispatch_spawned_sessions()

    # The seeded pending message is the bare string "print hello"; the
    # JSON-decode falls back to treating the whole content as the prompt.
    assert stub.calls == [("code-1", "print hello")]


def test_already_launched_subprocess_does_not_reexecute_prompt(tmp_path: Path):
    """A re-dispatch of a session that ALREADY launched a subprocess must NOT
    re-run the original prompt (#400).

    Simulates the spawn-before-init window: a routing='claude_code' session whose
    subprocess actually launched once (a `claude_code_spawn` transcript event with
    NO following `claude_code_binary_not_found`) but whose `claude_code_session_id`
    never persisted. On a non-restart re-dispatch the fresh-spawn fork would
    naively call execute() with the original prompt again — which for a doctor turn
    can re-file a GitHub issue or restart /implement. Assert execute()/resume() are
    NOT called, the session is marked FAILED (not left non-terminal, which would
    re-pick every tick), the aversion is recorded with the prior launch count, and
    the operator is notified so they can re-trigger deliberately."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should never run")
    )
    worker, store, transcripts, sent = _capturing_worker(tmp_path, claude_code_executor=stub)
    # Operator spawn: no backing vault row, so no complete/swap-tag side effects.
    session = store.create(task_id="op-1", routing="claude_code", origin="operator")
    # A subprocess launched once (spawn event, no binary-not-found) — but
    # claude_code_session_id is still NULL because the CLI init never persisted.
    transcripts.append(session.session_id, "claude_code_spawn", {
        "resume": False, "plan_mode": False, "working_dir": "/repo",
    })

    worker._dispatch_claude_code_session(session, [{"content": "file the doctor issue"}])

    # The original prompt was NOT re-run (no execute(), and not coerced to resume()).
    assert stub.calls == []
    assert stub.resume_calls == []
    # Session terminated FAILED rather than left non-terminal (which would be
    # re-picked every tick → hot loop / permanent strand) or silently no-op'd.
    assert store.get("op-1").status == STATUS_FAILED
    # The aversion is auditable (with the prior launch count), and the operator
    # was told so they can re-trigger deliberately.
    events = transcripts.read(session.session_id)
    averted = [e for e in events if e["kind"] == "claude_code_reexecute_averted"]
    assert len(averted) == 1
    assert averted[0]["payload"]["prior_launch_count"] == 1
    assert any("already started" in s for s in sent)


def test_binary_not_found_does_not_loop_or_misdiagnose(tmp_path: Path):
    """A missing `claude` binary must end the session FAILED without re-dispatch
    looping, and must NOT trip the already-launched guard (#400).

    The executor writes `claude_code_spawn` *before* the spawn call, then on a
    missing binary writes `claude_code_binary_not_found` and returns FAILED — no
    subprocess ever launched, so there are no side effects and re-execute would be
    safe. Two things must hold: (1) the terminal tail persists FAILED to the
    session row so the next tick does NOT re-pick it (Fix A — root cause of the
    real-world loop), and (2) when the transcript already carries spawn +
    binary-not-found from a prior attempt, the guard does NOT misdiagnose it as a
    side-effecting interruption (no `claude_code_reexecute_averted`)."""
    from api.services.agent_worker.claude_code_executor import REASON_BINARY_NOT_FOUND

    # 1. A fresh dispatch whose executor returns the binary-not-found FAILED.
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_FAILED, reason=REASON_BINARY_NOT_FOUND)
    )
    worker, store, transcripts, _ = _capturing_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="bnf-1", routing="claude_code", origin="operator")

    worker._dispatch_claude_code_session(session, [{"content": "do a thing"}])

    # The terminal tail persisted FAILED to the row — so it won't re-dispatch.
    assert store.get("bnf-1").status == STATUS_FAILED
    assert stub.calls == [("bnf-1", "do a thing")]

    # 2. Re-dispatch with a transcript that already shows spawn + binary-not-found
    #    (the prior attempt's events). The guard must NOT fire: subtracting the
    #    not-found from the spawn leaves zero real launches.
    transcripts.append(session.session_id, "claude_code_spawn", {"resume": False})
    transcripts.append(session.session_id, "claude_code_binary_not_found", {"error": "no claude"})
    stub.calls.clear()

    worker._dispatch_claude_code_session(session, [{"content": "do a thing"}])

    # No misdiagnosis event, and the prompt was allowed to re-execute (safe — no
    # subprocess ever launched).
    kinds = [e["kind"] for e in transcripts.read(session.session_id)]
    assert "claude_code_reexecute_averted" not in kinds
    assert stub.calls == [("bnf-1", "do a thing")]


def test_fresh_spawn_with_no_prior_spawn_event_still_executes(tmp_path: Path):
    """Guardrail: the crash-before-init check must not break the normal first
    dispatch. With no prior `claude_code_spawn` event, execute() still runs (#400)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, store, _, _ = _capturing_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="op-2", routing="claude_code", origin="operator")

    worker._dispatch_claude_code_session(session, [{"content": "do a thing"}])

    assert stub.calls == [("op-2", "do a thing")]


# =============================================================================
# Web-thread result mirroring (#311)
# =============================================================================


def _mirroring_worker(tmp_path: Path, claude_code_executor):
    """Worker with an isolated ConversationStore so a test can assert the
    spawned session's output is mirrored into the linked conversation (#311)."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    conv_store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=store,
        conversation_store=conv_store,
        transcript_store=transcripts,
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
        cli_pool=_SynchronousPool(),
    )
    return worker, store, conv_store


def test_mirror_to_conversation_writes_when_linked(tmp_path: Path):
    """_mirror_to_conversation writes an assistant message into the conversation
    linked to a session_id."""
    worker, _, conv_store = _mirroring_worker(tmp_path, claude_code_executor=None)
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, "sess-xyz")

    worker._mirror_to_conversation("sess-xyz", "  progress update  ")

    msgs = conv_store.get_messages(conv.id)
    assert [(m.role, m.content) for m in msgs] == [("assistant", "progress update")]


def test_mirror_to_conversation_noop_when_unlinked(tmp_path: Path):
    """AC2: a Telegram-origin session is unlinked, so the mirror is a no-op —
    no conversation is written and no crash."""
    worker, _, conv_store = _mirroring_worker(tmp_path, claude_code_executor=None)
    conv = conv_store.create_conversation(title="Unrelated thread")

    worker._mirror_to_conversation("sess-not-linked", "should not land anywhere")

    assert conv_store.get_messages(conv.id) == []


def test_mirror_to_conversation_noop_on_empty_text(tmp_path: Path):
    worker, _, conv_store = _mirroring_worker(tmp_path, claude_code_executor=None)
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, "sess-xyz")

    worker._mirror_to_conversation("sess-xyz", "   ")
    worker._mirror_to_conversation("", "text")

    assert conv_store.get_messages(conv.id) == []


def test_completion_mirrors_final_text_into_linked_conversation(tmp_path: Path):
    """A completed web-spawned claude_code session lands its final text in the
    linked conversation thread (in addition to Telegram)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="All done — opened PR #5.")
    )
    worker, store, conv_store = _mirroring_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="web-1", routing="claude_code", origin="operator")
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, session.session_id)

    worker._dispatch_claude_code_session(session, [{"content": "do the thing"}])

    msgs = conv_store.get_messages(conv.id)
    assert any(m.role == "assistant" and "All done — opened PR #5." in m.content for m in msgs)


def test_completion_does_not_mirror_when_unlinked(tmp_path: Path):
    """An operator/Telegram-origin completion writes nothing to any conversation
    (AC2)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, store, conv_store = _mirroring_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="tg-1", routing="claude_code", origin="operator")
    conv = conv_store.create_conversation(title="Some other thread")  # not linked

    worker._dispatch_claude_code_session(session, [{"content": "do the thing"}])

    assert conv_store.get_messages(conv.id) == []


def test_failure_mirrors_notice_into_linked_conversation(tmp_path: Path):
    """A FAILED web-spawned session mirrors its failure notice into the thread."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom")
    )
    worker, store, conv_store = _mirroring_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="web-fail-1", routing="claude_code", origin="operator")
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, session.session_id)

    worker._dispatch_claude_code_session(session, [{"content": "do the thing"}])

    msgs = conv_store.get_messages(conv.id)
    assert any(m.role == "assistant" and "failed" in m.content.lower() for m in msgs)


def test_child_completion_does_not_mirror(tmp_path: Path):
    """A spawned child session (has a parent) stays silent to the operator and
    must not mirror into any conversation — even if one is linked."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="child result")
    )
    worker, store, conv_store = _mirroring_worker(tmp_path, claude_code_executor=stub)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-1", routing="claude_code", parent_session_id=parent.session_id,
    )
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, child.session_id)

    worker._dispatch_claude_code_session(child, [{"content": "list matches"}])

    assert conv_store.get_messages(conv.id) == []


def test_child_failure_sends_no_operator_notice(tmp_path: Path):
    """A spawned child (parent set) that FAILS must not send the operator a
    "⚠️ … failed" Telegram notice (#431) — the parent's resume turn already
    carries the child's [failed] status header. FAILED is still persisted."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom")
    )
    worker, store, _, sent = _capturing_worker(tmp_path, claude_code_executor=stub)
    parent = store.create(task_id="parent-1", routing="local")
    child = store.create(
        task_id="child-fail", routing="claude_code",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )

    worker._dispatch_claude_code_session(child, [{"content": "sub-task"}])

    assert not any("failed" in s.lower() for s in sent)
    assert store.get("child-fail").status == STATUS_FAILED


# =============================================================================
# #379 — operator-killed session emits no post-kill notice
# =============================================================================


def test_operator_killed_failed_outcome_sends_no_telegram_notice(tmp_path: Path):
    """A FAILED outcome carrying reason=REASON_KILLED is the executor's signal
    that the operator deliberately killed the session mid-run. The dispatch path
    must NOT send the "⚠️ Code session failed" Telegram notice (the operator
    knows they killed it; the kill endpoint already wrote operator_killed)."""
    from api.services.agent_worker.claude_code_executor import REASON_KILLED

    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)
    )
    worker, store, _, sent = _capturing_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="op-killed-1", routing="claude_code", origin="operator")
    store.enqueue_message(session.session_id, "operator", "long task")

    worker._dispatch_claude_code_session(session, [{"content": "long task"}])

    # No operator-facing failure notice.
    assert not any("failed" in s.lower() for s in sent)
    # The session row is still persisted terminal (idempotent with the kill's flip).
    assert store.get("op-killed-1").status == STATUS_FAILED


def test_operator_killed_failed_outcome_does_not_mirror_to_web(tmp_path: Path):
    """Parity for the web/voice thread: a REASON_KILLED FAILED outcome must NOT
    mirror a failure notice into the linked conversation (#379 + #311)."""
    from api.services.agent_worker.claude_code_executor import REASON_KILLED

    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)
    )
    worker, store, conv_store = _mirroring_worker(tmp_path, claude_code_executor=stub)
    session = store.create(task_id="web-killed-1", routing="claude_code", origin="operator")
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, session.session_id)

    worker._dispatch_claude_code_session(session, [{"content": "long task"}])

    # Nothing about a failure landed in the thread.
    assert conv_store.get_messages(conv.id) == []
