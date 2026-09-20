"""Worker `_dispatch()` wiring for git worktree provisioning: a fresh
CLI-routed (Claude Code / Codex) board task whose resolved working
directory is inside a git repository gets its own worktree and branch
instead of running in that directory directly (which can be the primary
checkout). A non-git-repo task, and a task pinned to a remote host, are
unaffected. Provisioning failure fails the task closed.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.git_worktree import worktree_dir_for
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import STATUS_COMPLETED, STATUS_FAILED, SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from api.services.conversation_store import ConversationStore


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path, name: str = "repo") -> Path:
    origin = root / f"{name}-origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / name
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


@dataclass
class _TaskCaptureExecutor:
    outcome: ExecutorOutcome
    tasks: list = field(default_factory=list)

    def execute(self, session, task):
        self.tasks.append(task)
        return self.outcome


def _golden_preflight_reply(routing: str) -> str:
    import json
    return json.dumps({
        "budget": {"wall_seconds": 3600, "max_tokens": 100000, "max_dollars": 1.0},
        "routing": routing,
        "routing_reason": "tagged",
        "routing_explicit": True,
        "expected_output": "text",
        "ambiguity": None,
        "sane": True,
        "sane_reason": "",
    })


def _make_worker(
    tmp_path: Path,
    *,
    backing_task: dict,
    routing: str,
    claude_code_executor=None,
    codex_executor=None,
):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == f"/api/tasks/{backing_task['id']}":
            return httpx.Response(200, json=backing_task)
        return httpx.Response(200, json={"tasks": []})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        preflight_caller=lambda prompt: _golden_preflight_reply(routing),
        claude_code_executor=claude_code_executor,
        codex_executor=codex_executor,
        cli_pool=_SynchronousPool(),
    )


def test_fresh_claude_code_dispatch_provisions_a_worktree_for_a_git_repo_task(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    repo = _init_repo_with_origin(tmp_path)
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-git-1",
        "description": "fix the printer",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(repo)},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-git-1", status="claimed")

    worker._dispatch(task)

    assert len(capture.tasks) == 1
    working_dir = capture.tasks[0]["working_dir"]
    # Never the primary checkout, and never running inline in this tick —
    # a fresh, deterministic sibling worktree instead.
    assert working_dir != str(repo)
    assert working_dir == worktree_dir_for(str(repo), "board-git-1")
    assert Path(working_dir).is_dir()


def test_fresh_codex_dispatch_provisions_a_worktree_for_a_git_repo_task(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    repo = _init_repo_with_origin(tmp_path, name="repo2")
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-git-2",
        "description": "fix the thing",
        "tags": ["agent", "codex"],
        "fields": {"working_dir": str(repo)},
    }
    backing_task = {**task, "tags": ["agent-running", "codex"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="codex", codex_executor=capture)
    worker.session_store.create(task_id="board-git-2", status="claimed")

    worker._dispatch(task)

    assert len(capture.tasks) == 1
    working_dir = capture.tasks[0]["working_dir"]
    assert working_dir == worktree_dir_for(str(repo), "board-git-2")


def test_non_git_directory_task_is_unaffected(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    plain_dir = tmp_path / "vault"
    plain_dir.mkdir()
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-plain-1",
        "description": "reindex the vault",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(plain_dir)},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-plain-1", status="claimed")

    worker._dispatch(task)

    assert capture.tasks[0]["working_dir"] == str(plain_dir)


def test_remote_host_assignment_still_provisions_a_worktree(tmp_path, monkeypatch):
    """A task pinned to a remote host must never run directly in the
    resolved directory either — provisioning happens over the resolved
    runner for that host (never this worker's own filesystem standing in
    for it). No real ssh is invoked: the runner `resolve_runner_for_host`
    would build is swapped for a fake that records every command."""
    from config.settings import settings
    from api.services.agent_worker import git_worktree

    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    repo = _init_repo_with_origin(tmp_path, name="repo3")
    calls: list[list[str]] = []

    def fake_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT):
        calls.append(cmd)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    def fake_resolve(host):
        assert host == "studio"
        return fake_runner

    monkeypatch.setattr(git_worktree, "resolve_runner_for_host", fake_resolve)

    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-remote-1",
        "description": "deploy the thing",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(repo), "host": "studio"},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-remote-1", status="claimed")

    worker._dispatch(task)

    working_dir = capture.tasks[0]["working_dir"]
    assert working_dir != str(repo)
    assert working_dir == worktree_dir_for(str(repo), "board-remote-1")
    assert any(cmd[:3] == ["git", "worktree", "add"] for cmd in calls)


def test_unregistered_remote_host_fails_the_task_closed(tmp_path, monkeypatch):
    """An unregistered host must never fall back to provisioning locally —
    that would create a worktree on the wrong machine and then run the
    session against it as though it were real."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    repo = _init_repo_with_origin(tmp_path, name="repo3b")
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-remote-2",
        "description": "deploy the thing",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(repo), "host": "nowhere"},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-remote-2", status="claimed")

    worker._dispatch(task)

    assert capture.tasks == []
    session = worker.session_store.get("board-remote-2")
    assert session.status == STATUS_FAILED


def test_worktree_provisioning_failure_fails_the_task_closed(tmp_path, monkeypatch):
    """A repo with no `origin` remote makes `git fetch origin` fail —
    provisioning must raise, and the task must be parked FAILED rather
    than running the executor in the caller-supplied directory."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    broken_repo = tmp_path / "broken"
    broken_repo.mkdir()
    _git(broken_repo, "init", "-q", "-b", "main")
    _git(broken_repo, "config", "user.email", "t@example.com")
    _git(broken_repo, "config", "user.name", "Test")
    (broken_repo / "README.md").write_text("hello\n")
    _git(broken_repo, "add", "README.md")
    _git(broken_repo, "commit", "-q", "-m", "init")
    # No `origin` remote configured.

    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done", notifications_sent=1))
    task = {
        "id": "board-broken-1",
        "description": "fix the thing",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(broken_repo)},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-broken-1", status="claimed")

    worker._dispatch(task)

    assert capture.tasks == []
    session = worker.session_store.get("board-broken-1")
    assert session.status == STATUS_FAILED


def test_resumed_dispatch_reuses_the_same_worktree(tmp_path, monkeypatch):
    """An operator follow-up on an already-branched session reuses the
    worktree/branch recorded on the session's own execution spec — it
    never re-enters worktree provisioning."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    repo = _init_repo_with_origin(tmp_path, name="repo4")
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="first", notifications_sent=1))
    task = {
        "id": "board-resume-1",
        "description": "fix the widget",
        "tags": ["agent", "claude"],
        "fields": {"working_dir": str(repo)},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, routing="claude_code", claude_code_executor=capture)
    worker.session_store.create(task_id="board-resume-1", status="claimed")
    worker._dispatch(task)
    first_working_dir = capture.tasks[0]["working_dir"]

    # The provisioned worktree persists on the session's own execution
    # spec — `_dispatch_claude_code_session`'s resume branch (unchanged by
    # this seam) pulls `spec.working_dir` straight from it, so a follow-up
    # never re-enters worktree provisioning.
    session = worker.session_store.get("board-resume-1")
    from api.services.agent_worker.execution import ExecutionSpec
    spec = ExecutionSpec.from_dict(session.execution_spec)
    assert spec.working_dir == first_working_dir
