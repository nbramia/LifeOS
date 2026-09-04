"""Tests for card-assignment threading (#851) into ClaudeCodeExecutor:
model/effort flags, host resolution, remote ssh wrapping + pgid capture,
and the unknown-host failure path (no ssh call).
"""
from __future__ import annotations

import json
import threading
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


def test_board_assigned_model_reaches_argv_via_set_assignment(tmp_path, monkeypatch):
    """Round 1, finding #1: the board-assignment `model` field — written by
    the real dispatch path (`SessionStore.create` then
    `SessionStore.set_assignment`, exactly like `worker._dispatch` does),
    NOT `claude_code_model` (the unrelated child-spawn escalation tier) —
    must reach `--model`. Uses a non-"opus" value so the executor's
    hardcoded `"opus"` fallback can't mask a regression back to reading
    only `claude_code_model`."""
    spawn_calls: list = []
    lines = _lines_for([_INIT_EVENT, _RESULT_EVENT])
    store, executor = _build(tmp_path, monkeypatch, spawn_calls=spawn_calls, lines=lines)
    store.create(task_id="t1", routing="claude_code")
    store.set_assignment("t1", model="sonnet", effort="high")
    session = store.get("t1")
    assert session.model == "sonnet"  # sanity: the write path actually landed
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status != STATUS_FAILED
    cmd = spawn_calls[0][0]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "sonnet"
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
    # Round 1, finding #9: prove the pgid-line strip leaves the JSON stream
    # aligned — the `_RESULT_EVENT`'s own text must still reach `final_text`
    # unscathed, not just status/argv/pgid.
    assert outcome.final_text == "done"
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


class _HangingStdout:
    """`readline()` never returns — simulates an ssh client stuck past TCP
    connect (auth stall, or a host that accepts the connection and then
    never answers). `ConnectTimeout` doesn't bound this."""

    def readline(self) -> str:
        import threading
        threading.Event().wait()  # blocks forever; the test's deadline is what ends it
        return ""  # pragma: no cover — unreachable


class _HangingProc:
    def __init__(self, pid: int = 9999):
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
    """Round 1, finding #3: a remote ssh client whose `PGID:` line never
    arrives (hung post-TCP-connect) must not block forever — the executor
    fails within the configured connect-timeout-derived deadline instead."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_ssh_connect_timeout", 0, raising=False)

    spawn_calls: list = []
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _HangingProc()

    executor = ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    session = store.create(task_id="t1", routing="claude_code", host="studio")

    import time
    start = time.monotonic()
    outcome = executor.execute(session, {"description": "do the thing"})
    elapsed = time.monotonic() - start

    assert outcome.status == STATUS_FAILED
    assert "studio" in outcome.reason
    assert elapsed < 30  # well under the test-harness timeout; proves it didn't hang


class _UnblockableStdout:
    """`readline()` blocks on a `threading.Event` until the test releases
    it, then returns `line` on the next call (and `""` after that). Unlike
    `_HangingStdout` (blocks forever), this lets a test observe executor
    state DURING the blocked read and then let it complete."""

    def __init__(self, line: str):
        self._line = line
        self._released = threading.Event()
        self._returned = False

    def unblock(self) -> None:
        self._released.set()

    def readline(self) -> str:
        self._released.wait()
        if self._returned:
            return ""
        self._returned = True
        return self._line

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if line == "":
            raise StopIteration
        return line


class _UnblockableProc:
    def __init__(self, stdout: "_UnblockableStdout", pid: int = 7777):
        self.stdout = stdout
        self.stderr = _FakeStderr()
        self.pid = pid
        self.returncode = 0
        self._terminated = False

    def poll(self):
        return None if not self._terminated else self.returncode

    def terminate(self):
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self):
        self._terminated = True


def test_remote_pid_event_recorded_before_pgid_line_arrives(tmp_path, monkeypatch):
    """Round 2, finding #2: the `claude_code_pid` transcript event must be
    recorded IMMEDIATELY after `Popen` — before the deadline-bounded pgid
    read — not only once (if) the pgid line arrives. Otherwise the
    operator-kill fallback (`inter_agent._kill_local_subprocess`) finds no
    pid event and silently no-ops for a local ssh client stuck mid-read,
    until the much longer wall-clock watchdog eventually fires.

    Runs the REAL executor on a background thread against a stdout whose
    `readline()` blocks until released, polls the transcript store while
    still blocked to prove the pid event already exists, then unblocks the
    fake so the pgid line is read and the run completes normally."""
    import time

    from config.settings import settings
    monkeypatch.setattr(settings, "agent_ssh_connect_timeout", 5, raising=False)
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    stdout = _UnblockableStdout("PGID:24680\n")
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")

    def _spawn_fn(cmd, **kwargs):
        return _UnblockableProc(stdout, pid=7777)

    executor = ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    session = store.create(task_id="t1", routing="claude_code", host="studio")

    thread = threading.Thread(target=executor.execute, args=(session, {"description": "do the thing"}))
    thread.start()
    try:
        # Poll for the pid event to appear WHILE the pgid read is still
        # blocked (well inside the 5s+5s deadline) — this is the assertion
        # that would fail before the fix, since the event used to be
        # appended only after this read returned.
        deadline = time.monotonic() + 3.0
        pid_events: list[dict] = []
        while time.monotonic() < deadline:
            pid_events = [e for e in transcripts.read(session.session_id) if e.get("kind") == "claude_code_pid"]
            if pid_events:
                break
            time.sleep(0.02)
        assert pid_events, "claude_code_pid event was not recorded before the pgid line arrived"
        payload = pid_events[0]["payload"]
        assert payload["pid"] == 7777
        assert payload["pgid"] is None
        assert payload["remote"] is True

        # Now let the blocked read complete and the executor finish.
        stdout.unblock()
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    refreshed = store.get("t1")
    assert refreshed.remote_pgid == 24680
    all_pid_events = [e for e in transcripts.read(session.session_id) if e.get("kind") == "claude_code_pid"]
    assert len(all_pid_events) == 2
    assert all_pid_events[-1]["payload"]["pgid"] == 24680


class _FakeStderrWithText:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


class _FailingSshProc:
    """A fast ssh connection failure — stdout is empty (ssh never even ran
    the remote wrapper), stderr carries ssh's own error text, non-zero
    returncode."""

    def __init__(self, stderr_text: str, returncode: int = 255, pid: int = 6060):
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
    """Round 1, finding #4: an unreachable-host ssh failure's stderr must
    land in `outcome.reason` (what `worker.py` uses verbatim for the
    #agent-failed card), not just the transcript's `stderr_tail`."""
    spawn_calls: list = []
    ssh_stderr = "ssh: connect to host studio port 22: Connection refused\n"
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    def _spawn_fn(cmd, **kwargs):
        spawn_calls.append((cmd, kwargs))
        return _FailingSshProc(ssh_stderr)

    executor = ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=_spawn_fn,
    )
    session = store.create(task_id="t1", routing="claude_code", host="studio")
    outcome = executor.execute(session, {"description": "do the thing"})
    assert outcome.status == STATUS_FAILED
    assert "studio" in outcome.reason
    assert "Connection refused" in outcome.reason


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
