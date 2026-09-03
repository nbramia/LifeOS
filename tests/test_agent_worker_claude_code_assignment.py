"""Tests for card-assignment threading (#851) into ClaudeCodeExecutor:
model/effort flags, host resolution, remote ssh wrapping + pgid capture,
and the unknown-host failure path (no ssh call).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest

from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.session_store import STATUS_FAILED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


class _FakeStdout:
    """Iterable + `readline()`-capable stand-in for a Popen stdout pipe."""

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
    def __init__(self, lines: list[str], pid: int = 4242, returncode: int = 0):
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


_INIT_EVENT = {"type": "system", "subtype": "init", "session_id": "cli-sess-1"}
_RESULT_EVENT = {
    "type": "result", "session_id": "cli-sess-1", "total_cost_usd": 0.01, "result": "done",
}


def _build(tmp_path: Path, monkeypatch, *, spawn_calls: list, lines: list[str],
           agent_hosts: dict | None = None):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path=db_path)
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")

    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", agent_hosts or {}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _FakeProc(lines)

    executor = ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    return store, executor


def test_model_and_effort_flags_in_argv(tmp_path, monkeypatch):
    """AC1: a claude-tagged task with model/effort fields spawns with
    `--model opus --effort high`."""
    spawn_calls: list = []
    lines = _lines_for([_INIT_EVENT, _RESULT_EVENT])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    session = store.create(task_id="t1", routing="claude_code", claude_code_model="opus", effort="high")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"


def test_no_effort_field_omits_flag(tmp_path, monkeypatch):
    spawn_calls: list = []
    lines = _lines_for([_INIT_EVENT, _RESULT_EVENT])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    session = store.create(task_id="t1", routing="claude_code")
    executor.execute(session, {"description": "do the thing"})
    cmd = spawn_calls[0][0]
    assert "--effort" not in cmd


def test_remote_host_wraps_argv_in_ssh_and_captures_pgid(tmp_path, monkeypatch):
    """AC5: a task with host: <name> mapped in the registry invokes ssh to
    that target with the same argv, and the session records host + the
    remote pgid captured from the wrapper's first stdout line."""
    spawn_calls: list = []
    lines = _lines_for([_INIT_EVENT, _RESULT_EVENT], pgid_line="PGID:98765\n")
    store, executor = _build(
        tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines,
        agent_hosts={"studio": "user@studio.example"},
    )
    session = store.create(task_id="t1", routing="claude_code", host="studio", claude_code_model="opus")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert cmd[0] == "ssh"
    assert "user@studio.example" in cmd
    assert any("--model opus" in part or ("--model" in part and "opus" in part) for part in cmd)
    remote_command = cmd[-1]
    assert "env -u" in remote_command
    assert "setsid bash -c" in remote_command
    assert "PGID:$$" in remote_command

    refreshed = store.get("t1")
    assert refreshed.remote_pgid == 98765


def test_unknown_host_fails_without_ssh_call(tmp_path, monkeypatch):
    """AC6: an unregistered host fails the task with a reason naming the
    host, and spawn_fn (ssh) is never invoked."""
    spawn_calls: list = []
    store, executor = _build(
        tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=[],
        agent_hosts={"studio": "user@studio.example"},
    )
    session = store.create(task_id="t1", routing="claude_code", host="nonexistent-box")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status == STATUS_FAILED
    assert "nonexistent-box" in outcome.reason
    assert spawn_calls == []


def test_local_host_name_matches_api_host_runs_locally(tmp_path, monkeypatch):
    """A host equal to this API's own hostname behaves like no host at
    all — local spawn, no ssh wrapping."""
    import socket
    spawn_calls: list = []
    lines = _lines_for([_INIT_EVENT, _RESULT_EVENT])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    local_name = socket.gethostname().split(".")[0]
    session = store.create(task_id="t1", routing="claude_code", host=local_name)
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert cmd[0] != "ssh"
