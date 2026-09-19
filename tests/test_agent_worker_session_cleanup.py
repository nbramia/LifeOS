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
    FinalizeResult,
    ensure_worktree,
    list_worker_worktrees,
    pull_request_state,
    remove_worker_worktree,
)
from api.services.agent_worker.session_resources import (
    cleanup_session_scratch,
    ensure_session_scratch,
    scratch_dir_for,
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
    session_id = "sess_0123456789abcdef"
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
    with session_scratch_context("sess_1111111111111111"):
        result = _tool_bash({"command": "printf '%s|%s|%s' \"$TMPDIR\" \"$TMP\" \"$TEMP\""})
    expected = str(scratch_dir_for("sess_1111111111111111"))
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


def test_cleanup_keeps_worktree_when_final_push_fails(tmp_path: Path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-push-failure", "fix: preserve synthetic work")
    worktree = Path(provisioned.working_dir)
    content = worktree / "must-survive.txt"
    content.write_text("synthetic unsaved work\n")
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.finalize_worktree_session",
        lambda *args, **kwargs: FinalizeResult(
            applicable=True, pushed=False, error="synthetic push failure",
        ),
    )

    result = remove_worker_worktree(str(worktree))

    assert result.removed is False
    assert result.error == "synthetic push failure"
    assert content.read_text() == "synthetic unsaved work\n"


def test_cleanup_refuses_primary_checkout(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)
    primary = remove_worker_worktree(str(repo))
    assert primary.removed is False
    assert repo.exists()


def test_cleanup_refuses_linked_worktree_without_ownership_marker(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)

    unmanaged = tmp_path / "project-wt-agent-unmanaged"
    assert _git(repo, "worktree", "add", "-q", "-b", "fix/unmanaged", str(unmanaged), "main").returncode == 0
    result = remove_worker_worktree(str(unmanaged))
    assert result.removed is False
    assert result.applicable is False
    assert unmanaged.exists()


def test_remote_scratch_cleanup_uses_host_runner(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    calls = []

    def runner(cmd, *, cwd=None, timeout=60, input=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.resolve_runner_for_host",
        lambda host: runner if host == "studio" else pytest.fail("wrong host"),
    )
    cleanup_session_scratch("sess_2222222222222222", host="studio")

    assert calls == [[
        "rm", "-rf", "--",
        str(scratch_dir_for("sess_2222222222222222")),
    ]]


@pytest.mark.parametrize(
    "session_id", ["..", ".", "sess_../escape", "../../etc/passwd", "sess_synthetic"],
)
def test_scratch_path_stays_contained_for_any_session_id(tmp_path, monkeypatch, session_id):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    container = (tmp_path / "lifeos-agent-worker").resolve()

    path = scratch_dir_for(session_id)

    assert path.parent == container
    assert path.is_relative_to(container)


@pytest.mark.parametrize(
    "session_id_a,session_id_b",
    [
        ("sess_abcdef0123456789", "sess_ABCDEF0123456789"),
        ("sess_synthetic", "SESS_SYNTHETIC"),
    ],
)
def test_scratch_path_is_unique_for_ids_differing_only_by_case(
    tmp_path, monkeypatch, session_id_a, session_id_b,
):
    """Two distinct session ids that differ only in case must resolve to two
    distinct directories — on a case-insensitive filesystem (default macOS),
    a mapping that preserves either id verbatim would let one session's
    cleanup delete the other's live scratch."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    assert scratch_dir_for(session_id_a) != scratch_dir_for(session_id_b)
    assert scratch_dir_for(session_id_a).name.lower() != scratch_dir_for(session_id_b).name.lower()


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
        task_id="task-synthetic", session_id="sess_8888888888888888", status="completed",
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
    worker._fetch_task = lambda _task_id, **_kwargs: task
    worker._executor_registry = SimpleNamespace(is_inflight=lambda _session: False)
    removed = []
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.pull_request_state",
        lambda working_dir, host=None: pr_state,
    )
    monkeypatch.setattr(
        "api.services.agent_worker.session_resources.cleanup_session_scratch",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.remove_worker_worktree",
        lambda working_dir, host=None: removed.append((working_dir, host))
        or SimpleNamespace(removed=True, applicable=True, error=None),
    )

    assert worker._cleanup_session_resources(force=True) == 1
    assert removed == [("/srv/project-wt-agent-task-synthetic", "studio")]


@pytest.mark.parametrize("status,inflight", [("running", False), ("completed", True)])
def test_terminal_card_does_not_remove_live_or_inflight_worktree(monkeypatch, status, inflight):
    session = SimpleNamespace(
        task_id="task-live", session_id="sess_3333333333333333", status=status,
        routing="codex", host=None, last_activity_at=0,
        execution_spec={
            "executor": "codex", "provider": "openai", "runtime": "cli",
            "working_dir": "/tmp/project-wt-agent-task-live", "billing": "subscription",
            "resolved_at": "2026-09-18T12:00:00+00:00",
        },
    )
    worker = Worker.__new__(Worker)
    worker._last_resource_cleanup = 0.0
    worker._pr_state_cache = {}
    worker.session_store = SimpleNamespace(list_sessions=lambda limit: [session])
    worker._fetch_task = lambda _task_id, **_kwargs: {"status": "cancelled", "tags": []}
    worker._executor_registry = SimpleNamespace(is_inflight=lambda _session: inflight)
    removed = []
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.remove_worker_worktree",
        lambda *args, **kwargs: removed.append(args[0]),
    )
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.list_worker_worktrees", lambda *args, **kwargs: [],
    )

    assert worker._cleanup_session_resources(force=True) == 0
    assert removed == []


def test_worktree_listing_finds_marker_owned_orphan_on_disk(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-orphan", "fix: synthetic orphan")

    assert list_worker_worktrees(str(repo)) == [provisioned.working_dir]


def test_periodic_reconciler_discovers_orphan_from_git_registry(tmp_path: Path):
    repo = _repo_with_origin(tmp_path)
    active = ensure_worktree(str(repo), "task-active", "fix: synthetic active")
    orphan = ensure_worktree(str(repo), "task-gone", "fix: synthetic orphan")
    session = SimpleNamespace(
        task_id="task-active", session_id="sess_6666666666666666", status="running",
        routing="codex", host=None, last_activity_at=int(time.time()),
        execution_spec={
            "executor": "codex", "provider": "openai", "runtime": "cli",
            "working_dir": active.working_dir, "billing": "subscription",
            "resolved_at": "2026-09-18T12:00:00+00:00",
        },
    )
    worker = Worker.__new__(Worker)
    worker._last_resource_cleanup = 0.0
    worker._pr_state_cache = {}
    worker.session_store = SimpleNamespace(list_sessions=lambda limit: [session])
    worker._fetch_task = lambda _task_id, **_kwargs: {"status": "in_progress", "tags": []}
    worker._executor_registry = SimpleNamespace(is_inflight=lambda _session: False)

    assert worker._cleanup_session_resources(force=True) == 1
    assert Path(active.working_dir).exists()
    assert not Path(orphan.working_dir).exists()


def test_periodic_reconciler_skips_transient_task_fetch_failure(monkeypatch):
    session = SimpleNamespace(
        task_id="task-fetch", session_id="sess_7777777777777777", status="completed",
        routing="codex", host=None, last_activity_at=0,
        execution_spec={
            "executor": "codex", "provider": "openai", "runtime": "cli",
            "working_dir": "/tmp/project-wt-agent-task-fetch", "billing": "subscription",
            "resolved_at": "2026-09-18T12:00:00+00:00",
        },
    )

    class Response:
        status_code = 503

        def raise_for_status(self):
            raise RuntimeError("synthetic unavailable")

    worker = Worker.__new__(Worker)
    worker.api_base = "http://synthetic.invalid"
    worker._http = SimpleNamespace(get=lambda _url: Response())
    worker._last_resource_cleanup = 0.0
    worker._pr_state_cache = {}
    worker.session_store = SimpleNamespace(list_sessions=lambda limit: [session])
    worker._executor_registry = SimpleNamespace(is_inflight=lambda _session: False)
    removed = []
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.remove_worker_worktree",
        lambda *args, **kwargs: removed.append(args[0]),
    )
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.list_worker_worktrees", lambda *args, **kwargs: [],
    )

    assert worker._cleanup_session_resources(force=True) == 0
    assert removed == []


@pytest.mark.parametrize("status", ["completed", "failed", "budget_exceeded"])
def test_terminal_projection_deletes_scratch(status, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    session = SimpleNamespace(
        task_id="task-synthetic", session_id="sess_4444444444444444", origin="operator",
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
    session = SimpleNamespace(session_id="sess_5555555555555555")
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
