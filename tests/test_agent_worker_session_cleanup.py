from __future__ import annotations

import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import CodexExecutor
from api.services.agent_worker.executor_lifecycle import CancelResult
from api.services.agent_worker.git_worktree import (
    ensure_worktree,
    pull_request_state,
    remove_worker_worktree,
)
from api.services.agent_worker.session_resources import (
    cleanup_session_scratch,
    ensure_session_scratch,
    session_scratch_context,
)
from api.services.agent_worker.tools import _tool_bash
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _repo_with_origin(root: Path) -> Path:
    origin = root / "origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0
    repo = root / "project"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "agent@example.test").returncode == 0
    assert _git(repo, "config", "user.name", "Synthetic Agent").returncode == 0
    (repo / "README.md").write_text("synthetic\n")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "initial").returncode == 0
    assert _git(repo, "remote", "add", "origin", str(origin)).returncode == 0
    assert _git(repo, "push", "-q", "origin", "main").returncode == 0
    return repo


def test_private_scratch_is_exported_by_both_cli_routes_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    session_id = "sess_synthetic"
    scratch = ensure_session_scratch(session_id)
    copied_auth = scratch / "auth.json"
    copied_auth.write_text('{"token":"synthetic"}')

    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    for env in (ClaudeCodeExecutor._clean_env(session_id), CodexExecutor._clean_env(session_id)):
        assert env["TMPDIR"] == str(scratch)
        assert env["TMP"] == str(scratch)
        assert env["TEMP"] == str(scratch)

    cleanup_session_scratch(session_id)
    assert not scratch.exists()
    assert not copied_auth.exists()


def test_local_bash_subprocess_receives_session_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with session_scratch_context("sess_local_synthetic"):
        result = _tool_bash({"command": "printf '%s|%s|%s' \"$TMPDIR\" \"$TMP\" \"$TEMP\""})
    expected = str(tmp_path / "lifeos-agent-worker" / "sess_local_synthetic")
    assert result.is_error is False
    assert result.output == f"{expected}|{expected}|{expected}"


def test_cleanup_commits_pushes_then_removes_worker_worktree(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-synthetic", "fix: synthetic cleanup")
    worktree = Path(provisioned.working_dir)
    assert (Path(_git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip()) / "lifeos-agent-worktree.json").is_file()
    (worktree / "result.txt").write_text("saved before cleanup\n")

    result = remove_worker_worktree(str(worktree))

    assert result.removed is True, result.error
    assert not worktree.exists()
    remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in remote.stdout
    show = _git(repo, "show", f"origin/{provisioned.branch}:result.txt")
    assert show.stdout == "saved before cleanup\n"


def test_cleanup_refuses_primary_and_unmarked_linked_worktrees(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)
    primary = remove_worker_worktree(str(repo))
    assert primary.removed is False
    assert repo.exists()

    unmanaged = tmp_path / "project-wt-agent-unmanaged"
    assert _git(repo, "worktree", "add", "-q", "-b", "fix/unmanaged", str(unmanaged), "main").returncode == 0
    result = remove_worker_worktree(str(unmanaged))
    assert result.removed is False
    assert result.applicable is False
    assert unmanaged.exists()


def test_pull_request_state_uses_injected_remote_runner():
    calls = []

    def runner(cmd, *, cwd=None, timeout=60):
        calls.append((cmd, cwd))
        if cmd[:3] == ["test", "-f", "/remote/worktree/.git"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, "fix/synthetic\n", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, "MERGED\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")

    assert pull_request_state("/remote/worktree", runner=runner) == "MERGED"
    assert any(call[0][0] == "gh" and call[1] == "/remote/worktree" for call in calls)


@pytest.mark.parametrize(
    ("task", "pr_state", "old"),
    [
        ({"status": "done", "tags": ["agent-completed", "accepted"]}, None, False),
        ({"status": "cancelled", "tags": ["agent-failed"]}, None, False),
        ({"status": "done", "tags": ["agent-completed"]}, "MERGED", False),
        (None, None, True),
    ],
    ids=["accepted", "cancelled", "merged-pr", "orphan"],
)
def test_periodic_reconciler_removes_each_eligible_worktree(monkeypatch, task, pr_state, old):
    session = SimpleNamespace(
        task_id="task-synthetic", session_id="sess-synthetic", status="completed",
        routing="codex", host="studio", last_activity_at=0 if old else int(time.time()),
        execution_spec={
            "executor": "codex", "provider": "openai", "runtime": "cli",
            "working_dir": "/srv/project-wt-agent-task-synthetic",
            "billing": "subscription", "resolved_at": "2026-09-18T12:00:00+00:00",
        },
    )
    worker = Worker.__new__(Worker)
    worker._last_resource_cleanup = 0.0
    worker._pr_state_cache = {}
    worker.session_store = SimpleNamespace(list_sessions=lambda limit: [session])
    worker._fetch_task = lambda _task_id: task
    removed = []
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.pull_request_state",
        lambda working_dir, host=None: pr_state,
    )
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.remove_worker_worktree",
        lambda working_dir, host=None: removed.append((working_dir, host))
        or SimpleNamespace(removed=True, applicable=True, error=None),
    )

    assert worker._cleanup_session_resources(force=True) == 1
    assert removed == [("/srv/project-wt-agent-task-synthetic", "studio")]


@pytest.mark.parametrize("status", ["completed", "failed", "budget_exceeded"])
def test_terminal_projection_deletes_scratch(status, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    session = SimpleNamespace(
        task_id="task-synthetic", session_id="sess-terminal", origin="operator",
        parent_session_id=None,
    )
    scratch = ensure_session_scratch(session.session_id)
    (scratch / "private.txt").write_text("synthetic secret")
    worker = Worker.__new__(Worker)
    worker.session_store = SimpleNamespace(get=lambda _task_id: session)

    assert worker._project_session_status("task-synthetic", status) is False
    assert not scratch.exists()


def test_cancel_and_kill_path_deletes_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    session = SimpleNamespace(session_id="sess-cancelled")
    scratch = ensure_session_scratch(session.session_id)
    (scratch / "credential-copy").write_text("synthetic")
    worker = Worker.__new__(Worker)
    worker.session_store = SimpleNamespace(get_by_session_id=lambda _sid: session)
    worker._lifecycle_adapter = lambda _session: object()
    worker._executor_registry = SimpleNamespace(
        cancel_once=lambda _session, _reason: CancelResult(cancelled=True, reason="killed"),
    )

    result = worker.cancel_session(session.session_id, "operator kill")

    assert result.cancelled is True
    assert not scratch.exists()
