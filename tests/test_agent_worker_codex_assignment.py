"""Tests for card-assignment threading (#851) into CodexExecutor:
model/effort flags, host resolution, remote ssh wrapping + pgid capture,
and the unknown-host failure path (no ssh call).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest

from api.services.agent_worker.codex_executor import CodexExecutor
from api.services.agent_worker.session_store import STATUS_FAILED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = list(lines)
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
        line = self.readline()
        if line == "":
            raise StopIteration
        return line


class _FakeStderr:
    def read(self) -> str:
        return ""


class _FakeProc:
    def __init__(self, lines: list[str], pid: int = 5151, returncode: int = 0):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.pid = pid
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _lines_for(events: Iterable[dict], pgid_line: str | None = None) -> list[str]:
    lines = [json.dumps(e) + "\n" for e in events]
    return ([pgid_line] if pgid_line else []) + lines


_THREAD_EVENT = {"type": "thread.started", "thread_id": "cx-thread-1"}
_TURN_COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
_SESSION_COMPLETED = {"type": "session.completed"}
_AGENT_MESSAGE_COMPLETED = {
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "done remotely"},
}


def _build(tmp_path: Path, monkeypatch, *, spawn_calls: list, lines: list[str],
           agent_hosts: dict | None = None):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")

    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", agent_hosts or {}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _FakeProc(lines)

    executor = CodexExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    return store, executor


def test_model_and_effort_flags_in_argv(tmp_path, monkeypatch):
    """AC2: a codex-tagged task with model/effort fields spawns with
    `--model gpt-5.5` and `-c model_reasoning_effort=high`."""
    spawn_calls: list = []
    lines = _lines_for([_THREAD_EVENT, _TURN_COMPLETED, _SESSION_COMPLETED])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    session = store.create(task_id="t1", routing="codex", model="gpt-5.5", effort="high")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert "-c" in cmd
    assert "model_reasoning_effort=high" in cmd


def test_board_assigned_model_reaches_argv_via_set_assignment(tmp_path, monkeypatch):
    """Round 1, finding #1 (Codex mirror): drives the real dispatch write
    path — `SessionStore.create` then `SessionStore.set_assignment`, like
    `worker._dispatch` does — rather than passing `model=` straight to
    `create()`, so a regression in how `CodexExecutor` reads `session.model`
    would be caught the same way the Claude Code test catches it."""
    spawn_calls: list = []
    lines = _lines_for([_THREAD_EVENT, _TURN_COMPLETED, _SESSION_COMPLETED])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    store.create(task_id="t1", routing="codex")
    store.set_assignment("t1", model="gpt-5.5", effort="high")
    session = store.get("t1")
    assert session.model == "gpt-5.5"  # sanity: the write path actually landed
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert "model_reasoning_effort=high" in cmd


def test_no_model_field_omits_flag(tmp_path, monkeypatch):
    spawn_calls: list = []
    lines = _lines_for([_THREAD_EVENT, _TURN_COMPLETED, _SESSION_COMPLETED])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    session = store.create(task_id="t1", routing="codex")
    executor.execute(session, {"description": "do the thing"})
    cmd = spawn_calls[0][0]
    assert "--model" not in cmd
    assert not any("model_reasoning_effort" in part for part in cmd)


def test_max_effort_maps_to_xhigh(tmp_path, monkeypatch):
    spawn_calls: list = []
    lines = _lines_for([_THREAD_EVENT, _TURN_COMPLETED, _SESSION_COMPLETED])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    session = store.create(task_id="t1", routing="codex", effort="max")
    executor.execute(session, {"description": "do the thing"})
    cmd = spawn_calls[0][0]
    assert "model_reasoning_effort=xhigh" in cmd


def test_remote_host_wraps_argv_in_ssh_and_captures_pgid(tmp_path, monkeypatch):
    """AC5 for codex: host field maps to a registered ssh target."""
    spawn_calls: list = []
    lines = _lines_for(
        [_THREAD_EVENT, _AGENT_MESSAGE_COMPLETED, _TURN_COMPLETED, _SESSION_COMPLETED],
        pgid_line="PGID:1212\n",
    )
    store, executor = _build(
        tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines,
        agent_hosts={"studio": "user@studio.example"},
    )
    session = store.create(task_id="t1", routing="codex", host="studio", model="gpt-5.5")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    # Round 1, finding #9: prove the pgid-line strip leaves the JSON stream
    # aligned for codex too — a real completion event's text must still
    # reach `final_text`.
    assert outcome.final_text == "done remotely"
    cmd = spawn_calls[0][0]
    assert cmd[0] == "ssh"
    assert "user@studio.example" in cmd
    remote_command = cmd[-1]
    assert "env -u" in remote_command
    assert "setsid bash -c" in remote_command

    refreshed = store.get("t1")
    assert refreshed.remote_pgid == 1212


def test_unknown_host_fails_without_ssh_call(tmp_path, monkeypatch):
    spawn_calls: list = []
    store, executor = _build(
        tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=[],
        agent_hosts={"studio": "user@studio.example"},
    )
    session = store.create(task_id="t1", routing="codex", host="nonexistent-box")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status == STATUS_FAILED
    assert "nonexistent-box" in outcome.reason
    assert spawn_calls == []


class _FakeStderrWithText:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


class _FailingSshProc:
    def __init__(self, stderr_text: str, returncode: int = 255, pid: int = 6161):
        self.stdout = _FakeStdout([])
        self.stderr = _FakeStderrWithText(stderr_text)
        self.pid = pid
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def test_remote_ssh_failure_reason_includes_stderr(tmp_path, monkeypatch):
    """Round 1, finding #4 (Codex mirror): an unreachable-host ssh
    failure's stderr must land in `outcome.reason`."""
    spawn_calls: list = []
    ssh_stderr = "ssh: connect to host studio port 22: Connection refused\n"
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _FailingSshProc(ssh_stderr)

    executor = CodexExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    session = store.create(task_id="t1", routing="codex", host="studio")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status == STATUS_FAILED
    assert "studio" in outcome.reason
    assert "Connection refused" in outcome.reason


class _HangingStdout:
    """`readline()` never returns — simulates an ssh client stuck past TCP
    connect. `ConnectTimeout` doesn't bound this. Codex mirror of the
    ClaudeCodeExecutor test double."""

    def readline(self) -> str:
        import threading
        threading.Event().wait()
        return ""  # pragma: no cover — unreachable


class _HangingProc:
    def __init__(self, pid: int = 8888):
        self.stdout = _HangingStdout()
        self.stderr = _FakeStderr()
        self.pid = pid
        self.returncode = None
        self._terminated = False

    def poll(self):
        return None if not self._terminated else -15

    def terminate(self):
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return -15

    def kill(self):
        self._terminated = True


def test_remote_host_unresponsive_pgid_read_fails_within_deadline(tmp_path, monkeypatch):
    """Round 1, finding #3 (Codex mirror): a hung ssh client whose `PGID:`
    line never arrives must not block the executor forever."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_ssh_connect_timeout", 0, raising=False)

    spawn_calls: list = []
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _HangingProc()

    executor = CodexExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    session = store.create(task_id="t1", routing="codex", host="studio")

    import time
    start = time.monotonic()
    outcome = executor.execute(session, {"description": "do the thing"})
    elapsed = time.monotonic() - start

    assert outcome.status == STATUS_FAILED
    assert "studio" in outcome.reason
    assert elapsed < 30
