"""A fresh Claude Code or Codex session running inside a worker-provisioned
worktree must be told so — the prompt states the branch, the expectation
to commit, and that the worker (not the session) pushes and opens the pull
request. A session outside a worktree (a vault/home-directory task, or an
operator spawn with no worktree) gets none of that, unchanged.
"""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import CodexExecutor
from api.services.agent_worker.git_worktree import ensure_worktree
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path) -> Path:
    origin = root / "origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    assert _git(repo, "push", "-q", "origin", "main").returncode == 0
    return repo


# ---------------------------------------------------------------------------
# ClaudeCodeExecutor — instructions ride the --append-system-prompt payload.
# ---------------------------------------------------------------------------

class _ClaudeFakeStdout:
    def __init__(self, events):
        self._lines = [json.dumps(e) + "\n" for e in events]
        self._idx = 0

    def readline(self):
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


class _ClaudeFakeProc:
    def __init__(self, events, returncode=0):
        self.stdout = _ClaudeFakeStdout(events)
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = 4242

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _claude_captured_system_prompt(tmp_path: Path, working_dir: str, *, resume: bool = False) -> str:
    captured: dict = {}

    def spawn_capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ClaudeFakeProc([
            {"type": "system", "subtype": "init", "session_id": "cli-1"},
            {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.01, "result": "ok"},
        ])

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts,
        spawn_fn=spawn_capture, binary_resolver=lambda: "/usr/bin/true",
        timeout_seconds=30, heartbeat_interval=3600,
    )
    if resume:
        session = store.create(task_id="task-resume", routing="claude_code", origin="operator")
        store.set_claude_code_session_id(session.task_id, "cli-existing")
        session = store.get(session.task_id)
        executor.resume(session, "keep going", working_dir=working_dir)
    else:
        session = store.create(task_id="task-fresh", routing="claude_code", origin="operator")
        executor.execute(session, {"description": "do the thing", "working_dir": working_dir})
    cmd = captured["cmd"]
    return cmd[cmd.index("--append-system-prompt") + 1]


def test_claude_code_prompt_states_branch_and_commit_expectation_in_worktree(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-fresh", "fix the thing")

    prompt = _claude_captured_system_prompt(tmp_path, provisioned.working_dir)

    assert provisioned.branch in prompt
    assert "own git worktree" in prompt
    assert "the worker pushes your branch and opens a pull request" in prompt


def test_claude_code_prompt_has_no_git_discipline_outside_a_worktree(tmp_path: Path):
    plain_dir = tmp_path / "vault"
    plain_dir.mkdir()

    prompt = _claude_captured_system_prompt(tmp_path, str(plain_dir))

    assert "own git worktree" not in prompt
    assert "pushes your branch" not in prompt


def test_claude_code_prompt_has_no_git_discipline_in_the_primary_checkout(tmp_path: Path):
    """The primary checkout is itself a git repo, but never a linked
    worktree — the instructions must not fire for it."""
    repo = _init_repo_with_origin(tmp_path)

    prompt = _claude_captured_system_prompt(tmp_path, str(repo))

    assert "own git worktree" not in prompt


def test_claude_code_resume_does_not_repeat_git_discipline(tmp_path: Path):
    """Resume reloads the same CLI thread, which already carries the
    instructions from the opening turn — a resume must not re-inject them."""
    repo = _init_repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-resume", "fix the thing")

    prompt = _claude_captured_system_prompt(tmp_path, provisioned.working_dir, resume=True)

    assert "own git worktree" not in prompt


# ---------------------------------------------------------------------------
# CodexExecutor — instructions are prepended to the plain-text prompt.
# ---------------------------------------------------------------------------

class _CodexFakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n")
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = 12345

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _codex_captured_prompt(tmp_path: Path, working_dir: str, *, resume: bool = False) -> str:
    captured: dict = {}

    def fake_spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("ok\n")
        return _CodexFakeProc([{"type": "session.completed"}], returncode=0)

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = CodexExecutor(
        session_store=store, transcript_store=transcripts,
        spawn_fn=fake_spawn, binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    if resume:
        session = store.create(task_id="t-resume", routing="codex", origin="operator")
        store.set_claude_code_session_id(session.task_id, "codex-existing")
        session = store.get(session.task_id)
        executor.resume(session, "keep going", working_dir=working_dir)
    else:
        session = store.create(task_id="t-fresh", routing="codex", origin="operator")
        executor.execute(session, {"description": "do a thing", "working_dir": working_dir})
    return captured["cmd"][-1]


def test_codex_prompt_states_branch_and_commit_expectation_in_worktree(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-fresh-codex", "fix the thing")

    prompt = _codex_captured_prompt(tmp_path, provisioned.working_dir)

    assert provisioned.branch in prompt
    assert "own git worktree" in prompt
    assert "GIT DISCIPLINE" in prompt


def test_codex_prompt_has_no_git_discipline_outside_a_worktree(tmp_path: Path):
    plain_dir = tmp_path / "vault"
    plain_dir.mkdir()

    prompt = _codex_captured_prompt(tmp_path, str(plain_dir))

    assert "GIT DISCIPLINE" not in prompt
    assert "own git worktree" not in prompt


def test_codex_resume_does_not_repeat_git_discipline(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-resume-codex", "fix the thing")

    prompt = _codex_captured_prompt(tmp_path, provisioned.working_dir, resume=True)

    assert "GIT DISCIPLINE" not in prompt
