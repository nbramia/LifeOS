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
        [_THREAD_EVENT, _TURN_COMPLETED, _SESSION_COMPLETED], pgid_line="PGID:1212\n",
    )
    store, executor = _build(
        tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines,
        agent_hosts={"studio": "user@studio.example"},
    )
    session = store.create(task_id="t1", routing="codex", host="studio", model="gpt-5.5")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
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
