"""A CLI engine's own clean subprocess exit must not mark a vault card done
before the dispatch layer's earned-completion check has run.

Drives both `claude_code` and `codex` sessions through the REAL worker
dispatch methods (`_dispatch_claude_code_session` / `_dispatch_codex_session`)
against a real `TaskManager` and the real FastAPI task routes (via
`TestClient`) — not an in-process stub — so the claimed-card write guard in
`api/routes/tasks.py` is actually exercised. A stub `execute()` that hands
back a canned `ExecutorOutcome` would never call `session_store.update_status`
the way the real executors do, and would miss the interaction this module
guards against entirely.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import tasks as tasks_route
import api.services.task_manager as task_manager_module
from api.services.agent_board import COMPLETED_TAG, RUNNING_TAG
from api.services.agent_worker.lifecycle import FAILED_TAG
from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import CodexExecutor
from api.services.conversation_store import ConversationStore
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit

# The exact observed shape: a clean exit, an 8-char final message, no
# notifications — too thin to earn completion (_MIN_SUMMARY_CHARS is 20).
THIN_FINAL_TEXT = "# LifeOS"
EARNED_FINAL_TEXT = "Implemented the fix, ran the full test suite, and everything is green."


class _FakeCodexProc:
    """Minimal subprocess.Popen stand-in for a codex `--json` stream."""

    def __init__(self, lines: list[dict], returncode: int = 0, stderr: str = ""):
        import io as _io
        self.stdout = _io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n")
        self.stderr = _io.StringIO(stderr)
        self.returncode = returncode
        self.pid = 24680

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeClaudeCodeStdout:
    def __init__(self, events: list[dict]):
        self._lines = [json.dumps(e) + "\n" for e in events]
        self._idx = 0

    def readline(self) -> str:
        if self._idx >= len(self._lines):
            return ""
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def __iter__(self):
        return self

    def __next__(self):
        if self._idx >= len(self._lines):
            raise StopIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


class _FakeClaudeCodeProc:
    def __init__(self, events: list[dict], returncode: int = 0, stderr: str = ""):
        self.stdout = _FakeClaudeCodeStdout(events)
        import io as _io
        self.stderr = _io.StringIO(stderr)
        self.returncode = returncode
        self.pid = 4242

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _make_real_route_worker(tmp_path: Path, monkeypatch, *, codex_executor=None, claude_code_executor=None):
    """A fully-constructed `Worker` (so `set_status_projector` and every
    other `__init__` wiring runs) whose HTTP client is a real FastAPI
    `TestClient` backed by a real `TaskManager` over a temp vault — the
    claimed-card guard in `update_task` runs for real, not a mock.
    """
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    monkeypatch.setattr(task_manager_module, "_task_manager", manager)
    sessions = SessionStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(tasks_route, "_session_store", sessions)

    worker = Worker(
        api_base="http://placeholder",
        session_store=sessions,
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [777],
        http_client=TestClient(api_main.app),
        codex_executor=codex_executor,
        claude_code_executor=claude_code_executor,
    )
    # `_WorkerLifecycleTaskManager.update` builds `f"{worker.api_base}/api/..."`
    # dynamically off `self.worker.api_base` — safe to override post-construction.
    worker.api_base = ""
    return worker, manager, sessions


def _seed_claimed_task(manager: TaskManager, *, assignee: str) -> str:
    """A real vault task already claimed by the worker: `agent-running` tag,
    `in_progress` status — the state a live CLI session's card is in the
    moment its subprocess exits."""
    task = manager.create(
        description="Answer a short factual question",
        status="in_progress",
        tags=[assignee, RUNNING_TAG],
    )
    return task.id


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def _codex_executor(tmp_path: Path, sessions: SessionStore, transcripts: TranscriptStore, *, final_text: str, returncode: int = 0):
    def fake_spawn(cmd, **kwargs):
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write(final_text + "\n")
        lines = [
            {"type": "thread.started", "thread_id": "thread-real"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final_text}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        return _FakeCodexProc(lines, returncode=returncode)

    return CodexExecutor(
        session_store=sessions,
        transcript_store=transcripts,
        spawn_fn=fake_spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )


def test_codex_thin_completion_does_not_reach_the_card_before_dispatch_judges_it(tmp_path: Path, monkeypatch):
    """A clean exit with an 8-char final message and no notifications is
    exactly the observed field shape: the card must stay claimed and
    non-terminal, never briefly (or permanently) `done`."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="codex")
    worker._codex_executor = _codex_executor(
        tmp_path, sessions, worker.transcript_store, final_text=THIN_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="codex")

    # A fresh dispatch: the fake subprocess's own `thread.started` event
    # persists the CLI session id via CodexExecutor's `codex_init` handling,
    # so the session is resumable by the time the earned-completion check
    # runs — same shape as the observed field session.
    worker._dispatch_codex_session(session, [{"content": "what does the vault title say"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status != "done"
    assert COMPLETED_TAG not in refreshed_task.tags
    # The stated contract for the parked path: the vault tag stays exactly
    # where it was — only the session row moves.
    assert RUNNING_TAG in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_BLOCKED


def test_codex_earned_completion_still_reaches_the_card(tmp_path: Path, monkeypatch):
    """A real summary-shaped final text still reaches `done` — the ordering
    guard is not a blanket refusal to ever complete."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="codex")
    worker._codex_executor = _codex_executor(
        tmp_path, sessions, worker.transcript_store, final_text=EARNED_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="codex")

    worker._dispatch_codex_session(session, [{"content": "ship it"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status == "done"
    assert COMPLETED_TAG in refreshed_task.tags
    assert RUNNING_TAG not in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_COMPLETED


def test_codex_genuine_failure_still_fails_the_card(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="codex")
    worker._codex_executor = _codex_executor(
        tmp_path, sessions, worker.transcript_store, final_text="", returncode=1,
    )
    session = sessions.create(task_id=task_id, routing="codex")

    worker._dispatch_codex_session(session, [{"content": "ship it"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status == "cancelled"
    assert FAILED_TAG in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_FAILED


# ---------------------------------------------------------------------------
# claude_code
# ---------------------------------------------------------------------------


def _claude_code_executor(sessions: SessionStore, transcripts: TranscriptStore, *, final_text: str, returncode: int = 0):
    events = [
        {"type": "system", "subtype": "init", "session_id": "cli-sess-thin"},
        {"type": "result", "session_id": "cli-sess-thin", "total_cost_usd": 0.01, "result": final_text},
    ]

    def fake_spawn(*_args, **_kwargs):
        return _FakeClaudeCodeProc(events, returncode=returncode)

    return ClaudeCodeExecutor(
        session_store=sessions,
        transcript_store=transcripts,
        spawn_fn=fake_spawn,
        binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30,
        heartbeat_interval=9999,
    )


def test_claude_code_thin_completion_does_not_reach_the_card_before_dispatch_judges_it(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text=THIN_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "what does the vault title say"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status != "done"
    assert COMPLETED_TAG not in refreshed_task.tags
    assert RUNNING_TAG in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_BLOCKED


def test_claude_code_earned_completion_still_reaches_the_card(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text=EARNED_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "ship it"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status == "done"
    assert COMPLETED_TAG in refreshed_task.tags
    assert RUNNING_TAG not in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_COMPLETED


def test_claude_code_genuine_failure_still_fails_the_card(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text="", returncode=1,
    )
    session = sessions.create(task_id=task_id, routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "ship it"}])

    refreshed_task = manager.get(task_id)
    assert refreshed_task.status == "cancelled"
    assert FAILED_TAG in refreshed_task.tags
    assert sessions.get(task_id).status == STATUS_FAILED
