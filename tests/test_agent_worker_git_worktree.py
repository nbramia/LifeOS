"""Tests for `git_worktree.py`'s provisioning half: `ensure_worktree`,
`describe_worktree`, and the pure branch/path-derivation helpers.

Every test runs against a real temporary git repository (`git init` plus a
local bare "origin" remote) — no network access, no interaction with the
real repository this worker runs from.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from api.services.agent_worker import git_worktree
from api.services.agent_worker.git_worktree import (
    WorktreeError,
    derive_branch_name,
    describe_worktree,
    ensure_worktree,
    is_linked_worktree,
    resolve_runner_for_host,
    worktree_dir_for,
)


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path, name: str = "repo") -> Path:
    """A working repo plus a local bare "origin" remote with main pushed —
    real git, no network access."""
    origin = root / f"{name}-origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / name
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "t@example.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    (repo / "README.md").write_text("hello\n")
    assert _git(repo, "add", "README.md").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "init").returncode == 0
    assert _git(repo, "remote", "add", "origin", str(origin)).returncode == 0
    push = _git(repo, "push", "-q", "origin", "main")
    assert push.returncode == 0, push.stderr

    return repo


# ---------------------------------------------------------------------------
# derive_branch_name / worktree_dir_for — pure, no filesystem/git required.
# ---------------------------------------------------------------------------

def test_derive_branch_name_uses_conventional_type_prefix():
    branch = derive_branch_name("fix: the printer jams", "task-abc12345")
    assert branch.startswith("fix/the-printer-jams-")
    assert branch.endswith("abc12345")


def test_derive_branch_name_infers_allowed_type_from_leading_keyword():
    branch = derive_branch_name("clean up the reports folder", "task-xyz98765")
    assert branch.startswith("refactor/clean-up-the-reports-folder-")


def test_derive_branch_name_defaults_to_feat_never_agent(tmp_path=None):
    branch = derive_branch_name("summarize the weekly digest", "task-xyz98765")
    assert branch.startswith("feat/summarize-the-weekly-digest-")
    assert not branch.startswith("agent/")


def test_derive_branch_name_type_is_always_an_allowed_branch_type():
    from api.services.agent_worker.git_worktree import ALLOWED_BRANCH_TYPES

    for title in ("clean up the reports folder", "summarize the weekly digest", "fix: the bug", ""):
        branch_type = derive_branch_name(title, "task-abc12345").split("/", 1)[0]
        assert branch_type in ALLOWED_BRANCH_TYPES


def test_derive_branch_name_never_collides_on_same_title_different_task():
    a = derive_branch_name("fix the bug", "task-aaaaaaaa")
    b = derive_branch_name("fix the bug", "task-bbbbbbbb")
    assert a != b


def test_worktree_dir_for_is_a_sibling_of_the_repo_never_inside_it():
    result = worktree_dir_for("/home/nathanramia/Code/LifeOS", "claude_code_deadbeef")
    assert result == "/home/nathanramia/Code/LifeOS-wt-agent-claude_code_deadbeef"


# ---------------------------------------------------------------------------
# ensure_worktree
# ---------------------------------------------------------------------------

def test_ensure_worktree_non_git_directory_is_unchanged(tmp_path: Path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    result = ensure_worktree(str(plain_dir), "task-1", "some task")

    assert result.is_git is False
    assert result.working_dir == str(plain_dir)
    assert result.branch is None


def test_ensure_worktree_creates_isolated_worktree_off_origin_main(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)

    result = ensure_worktree(str(repo), "task-42", "fix the printer")

    assert result.is_git is True
    assert result.reused is False
    # Never the primary checkout — a distinct, sibling directory.
    assert result.working_dir != str(repo)
    assert Path(result.working_dir).parent == repo.parent
    assert os.path.isdir(result.working_dir)
    assert is_linked_worktree(result.working_dir)
    assert result.branch.startswith("fix/the-printer-")

    # HEAD in the new worktree matches origin/main's commit.
    head = _git(Path(result.working_dir), "rev-parse", "HEAD").stdout.strip()
    origin_main = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_main

    # Branch tracks the derived name, not "main".
    branch = _git(Path(result.working_dir), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == result.branch


def test_ensure_worktree_is_idempotent_for_the_same_task(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)

    first = ensure_worktree(str(repo), "task-77", "clean up logs")
    second = ensure_worktree(str(repo), "task-77", "clean up logs")

    assert second.reused is True
    assert second.working_dir == first.working_dir
    assert second.branch == first.branch
    # Only one worktree was ever registered for this task.
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    assert listing.count(f"worktree {first.working_dir}") == 1


def test_ensure_worktree_fetches_before_branching_off_origin_main(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)

    # Advance origin/main from a second clone, without the first repo ever
    # fetching it directly.
    other_clone = tmp_path / "other-clone"
    origin = repo.parent / f"{repo.name}-origin.git"
    assert _git(tmp_path, "clone", "-q", str(origin), str(other_clone)).returncode == 0
    _git(other_clone, "config", "user.email", "t@example.com")
    _git(other_clone, "config", "user.name", "Test")
    (other_clone / "NEW.md").write_text("new content\n")
    assert _git(other_clone, "add", "NEW.md").returncode == 0
    assert _git(other_clone, "commit", "-q", "-m", "advance main").returncode == 0
    assert _git(other_clone, "push", "-q", "origin", "main").returncode == 0

    result = ensure_worktree(str(repo), "task-99", "pick up the update")

    assert (Path(result.working_dir) / "NEW.md").exists()


def test_ensure_worktree_raises_worktree_error_when_fetch_fails(tmp_path: Path):
    repo = tmp_path / "no-origin"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    # No `origin` remote configured at all — fetch must fail closed.

    with pytest.raises(WorktreeError):
        ensure_worktree(str(repo), "task-err", "a task with no origin")

    # Nothing left behind — no worktree, no directory.
    sibling = worktree_dir_for(str(repo), "task-err")
    assert not os.path.exists(sibling)


def test_ensure_worktree_fails_closed_without_touching_the_primary_checkout(tmp_path: Path):
    """A failure must never leave the caller running inside the directory
    it passed in (which could be the primary checkout), and must not
    register any worktree against it."""
    repo = tmp_path / "broken"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))

    with pytest.raises(WorktreeError):
        ensure_worktree(str(repo), "task-broken", "some task")

    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    assert listing.count("worktree ") == 1  # only the primary checkout itself


# ---------------------------------------------------------------------------
# base_branch — a project's integration branch. Lazily created on origin
# off the default branch when missing, used as the worktree's base instead
# of the default branch, and recorded in the ownership marker.
# ---------------------------------------------------------------------------

def test_ensure_worktree_with_base_branch_creates_it_lazily_and_bases_worktree_on_it(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path, name="repo-integration")

    result = ensure_worktree(
        str(repo), "task-int-1", "fix the thing", base_branch="feat/integration-deadbeef",
    )

    assert result.is_git is True
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", "feat/integration-deadbeef").stdout
    assert "feat/integration-deadbeef" in ls_remote
    # Created off origin/main's current tip.
    head = _git(Path(result.working_dir), "rev-parse", "HEAD").stdout.strip()
    origin_main = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_main
    marker = git_worktree._read_worker_marker(
        result.working_dir, runner=None, timeout=git_worktree.DEFAULT_TIMEOUT,
    )
    assert marker["base_branch"] == "feat/integration-deadbeef"


def test_ensure_worktree_uses_an_already_existing_base_branch_without_recreating(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path, name="repo-existing-base")
    origin = repo.parent / "repo-existing-base-origin.git"
    other_clone = tmp_path / "other-clone-existing-base"
    assert _git(tmp_path, "clone", "-q", str(origin), str(other_clone)).returncode == 0
    _git(other_clone, "config", "user.email", "t@example.com")
    _git(other_clone, "config", "user.name", "Test")
    assert _git(other_clone, "checkout", "-q", "-b", "feat/integration-cafebabe").returncode == 0
    (other_clone / "INTEGRATION.md").write_text("integration-only content\n")
    assert _git(other_clone, "add", "INTEGRATION.md").returncode == 0
    assert _git(other_clone, "commit", "-q", "-m", "integration branch work").returncode == 0
    assert _git(other_clone, "push", "-q", "origin", "feat/integration-cafebabe").returncode == 0

    result = ensure_worktree(
        str(repo), "task-int-2", "fix the thing", base_branch="feat/integration-cafebabe",
    )

    # Based on the existing integration branch's own content, not recreated
    # off main (which never has INTEGRATION.md).
    assert (Path(result.working_dir) / "INTEGRATION.md").exists()
    head = _git(Path(result.working_dir), "rev-parse", "HEAD").stdout.strip()
    origin_integration = _git(repo, "rev-parse", "origin/feat/integration-cafebabe").stdout.strip()
    assert head == origin_integration


def test_ensure_remote_branch_treats_a_racing_creation_as_success(tmp_path: Path):
    """Two children racing to create the same integration branch must both
    succeed: even when this call's own push loses the race — the branch
    already exists on origin by the time it runs — a re-fetch that finds
    the ref is treated as success, never a failure."""
    repo = _init_repo_with_origin(tmp_path, name="repo-race-base")

    def losing_push(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT, input=None):
        if cmd[:2] == ["git", "push"]:
            # Stand in for a concurrent winner: the branch lands on origin
            # out-of-band, and this call's own push reports failure anyway.
            _git(repo, "push", "-q", "origin", "origin/main:refs/heads/feat/integration-raced")
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="stale info")
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    git_worktree._ensure_remote_branch(
        str(repo), "feat/integration-raced", "main",
        runner=losing_push, timeout=git_worktree.DEFAULT_TIMEOUT,
    )

    ls_remote = _git(repo, "ls-remote", "--heads", "origin", "feat/integration-raced").stdout
    assert "feat/integration-raced" in ls_remote


def test_ensure_remote_branch_raises_when_the_ref_is_still_missing_after_refetch(tmp_path: Path):
    """A genuine push-rights/connectivity failure — the ref is still
    missing even after the re-fetch — is a real error, not a race."""
    repo = _init_repo_with_origin(tmp_path, name="repo-real-failure")

    def always_failing_push(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT, input=None):
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="permission denied")
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    with pytest.raises(WorktreeError):
        git_worktree._ensure_remote_branch(
            str(repo), "feat/integration-unreachable", "main",
            runner=always_failing_push, timeout=git_worktree.DEFAULT_TIMEOUT,
        )


def test_ensure_worktree_reuse_keeps_the_originally_recorded_base_branch(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path, name="repo-reuse-base")

    first = ensure_worktree(
        str(repo), "task-int-reuse", "fix the thing", base_branch="feat/integration-first",
    )
    assert first.reused is False

    # A later call for the same task with a different (or no) base_branch
    # must not disturb the worktree's original base.
    second = ensure_worktree(
        str(repo), "task-int-reuse", "fix the thing", base_branch="feat/integration-different",
    )

    assert second.reused is True
    assert second.working_dir == first.working_dir
    marker = git_worktree._read_worker_marker(
        second.working_dir, runner=None, timeout=git_worktree.DEFAULT_TIMEOUT,
    )
    assert marker["base_branch"] == "feat/integration-first"


def test_ensure_worktree_without_base_branch_records_none(tmp_path: Path):
    """A task with no recorded integration branch (a non-project task, or a
    project without the field) behaves exactly as before: no base_branch
    is recorded and nothing extra is created on origin."""
    repo = _init_repo_with_origin(tmp_path, name="repo-no-base")

    result = ensure_worktree(str(repo), "task-no-base", "fix the thing")

    marker = git_worktree._read_worker_marker(
        result.working_dir, runner=None, timeout=git_worktree.DEFAULT_TIMEOUT,
    )
    assert marker["base_branch"] is None


def test_integration_branch_never_collides_with_the_handed_off_source_tasks_own_branch(
    tmp_path: Path,
):
    """A handed-off project keeps the source task's own id. That task's own
    CLI worktree branch (if it has one) is derived from
    `(description, task.id)` directly by `ensure_worktree` — the recorded
    integration branch must never equal it, or children (and the source's
    own worktree finalize push) would collide on the same ref."""
    from api.services.task_manager import Task
    from api.services.task_projects import _integration_branch_name

    repo = _init_repo_with_origin(tmp_path, name="repo-collision")
    task = Task(id="task-collide1", description="fix the launch pipeline")

    # The source task's own CLI session already has a worktree with pushed
    # in-flight work on its own branch.
    source = ensure_worktree(str(repo), task.id, task.description)
    (Path(source.working_dir) / "wip.txt").write_text("source task's own in-flight work\n")
    _git(Path(source.working_dir), "add", "wip.txt")
    _git(Path(source.working_dir), "commit", "-q", "-m", "source WIP")
    assert _git(Path(source.working_dir), "push", "-q", "-u", "origin", source.branch).returncode == 0

    integration_branch = _integration_branch_name(task)
    assert integration_branch != source.branch

    child = ensure_worktree(
        str(repo), "task-collide-child", "add the launch step",
        base_branch=integration_branch,
    )

    # A fresh branch off main, not the source's own pushed WIP.
    assert not (Path(child.working_dir) / "wip.txt").exists()
    head = _git(Path(child.working_dir), "rev-parse", "HEAD").stdout.strip()
    origin_integration = _git(repo, "rev-parse", f"origin/{integration_branch}").stdout.strip()
    origin_main = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_integration == origin_main


# ---------------------------------------------------------------------------
# describe_worktree — consulted by the executors' prompt assembly.
# ---------------------------------------------------------------------------

def test_describe_worktree_none_for_primary_checkout(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    assert describe_worktree(str(repo)) is None


def test_describe_worktree_none_for_non_git_directory(tmp_path: Path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    assert describe_worktree(str(plain_dir)) is None


def test_describe_worktree_detects_a_linked_worktree(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    result = ensure_worktree(str(repo), "task-describe", "fix the thing")

    context = describe_worktree(result.working_dir)

    assert context is not None
    assert context.branch == result.branch
    # A linked worktree is its own git toplevel (it shares the common .git
    # dir with the primary checkout, but not a working-tree root).
    assert context.repo_toplevel == result.working_dir


# ---------------------------------------------------------------------------
# Remote-host provisioning — resolve_runner_for_host / make_ssh_runner /
# ensure_worktree(host=...). No real ssh is ever invoked: either the
# resolved runner is swapped for a fake, or the ssh argv is inspected via a
# faked `subprocess.run`.
# ---------------------------------------------------------------------------

def test_resolve_runner_for_host_local_returns_the_local_runner(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    runner = resolve_runner_for_host(None)

    assert runner is git_worktree._local_runner


def test_resolve_runner_for_host_unregistered_fails_closed(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    with pytest.raises(WorktreeError):
        resolve_runner_for_host("no-such-host")


def test_resolve_runner_for_host_remote_builds_an_ssh_runner(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(git_worktree.subprocess, "run", fake_run)

    runner = resolve_runner_for_host("studio")
    runner(["git", "status"], cwd="/remote/path/repo", timeout=5)

    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "user@studio.example" in argv
    # cwd folds into a `cd ... &&` prefix on the remote command — ssh has
    # no cwd concept of its own.
    remote_command = argv[-1]
    assert remote_command.startswith("cd /remote/path/repo &&")
    assert "git status" in remote_command


def test_ensure_worktree_routes_every_command_through_the_resolved_runner(tmp_path: Path, monkeypatch):
    """A registered remote host provisions through whatever runner
    `resolve_runner_for_host` returns — never falls back to the plain
    local subprocess standing in for a machine this worker isn't."""
    repo = _init_repo_with_origin(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT):
        calls.append(cmd)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    def fake_resolve(host):
        assert host == "studio"
        return fake_runner

    monkeypatch.setattr(git_worktree, "resolve_runner_for_host", fake_resolve)

    result = ensure_worktree(str(repo), "task-remote-1", "fix the thing", host="studio")

    assert result.is_git is True
    assert Path(result.working_dir).is_dir()
    # Every git call provisioning made went through the resolved runner.
    assert any(cmd[:2] == ["git", "fetch"] for cmd in calls)
    assert any(cmd[:3] == ["git", "worktree", "add"] for cmd in calls)


def test_ensure_worktree_fails_closed_for_an_unregistered_remote_host(tmp_path: Path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    repo = _init_repo_with_origin(tmp_path, name="repo-unreg")

    with pytest.raises(WorktreeError):
        ensure_worktree(str(repo), "task-remote-2", "fix the thing", host="no-such-host")


# ---------------------------------------------------------------------------
# Race tolerance — after `git worktree add` fails, re-read the registered
# worktree list and reuse it if a concurrent dispatch landed it first.
# ---------------------------------------------------------------------------

def test_ensure_worktree_reuses_a_racing_concurrent_provision(tmp_path: Path, monkeypatch):
    """Simulates the exact race the review's probe hit: the primary reuse
    check misses (as it would for two ticks racing the same task, both
    starting before either's worktree is registered), `git worktree add`
    then fails because the worktree already exists, and `ensure_worktree`
    must reuse it instead of raising."""
    repo = _init_repo_with_origin(tmp_path, name="repo-race")

    # Pre-create the branch AND the worktree at the exact deterministic
    # path/branch `ensure_worktree` would itself compute — standing in for
    # "a concurrent dispatch already won the race."
    branch = derive_branch_name("fix the thing", "task-race-1")
    expected_dir = worktree_dir_for(str(repo), "task-race-1")
    assert _git(repo, "worktree", "add", "-b", branch, expected_dir, "origin/main").returncode == 0
    git_worktree._write_worker_marker(
        expected_dir,
        {
            "version": 1, "task_id": "task-race-1", "repo_toplevel": str(repo),
            "worktree_dir": expected_dir, "branch": branch, "state": "ready",
        },
        runner=None, timeout=git_worktree.DEFAULT_TIMEOUT,
    )

    real_probe = git_worktree._registered_worktree_branch
    call_count = {"n": 0}

    def probe_missing_then_real(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # the primary reuse check "misses" the race
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(git_worktree, "_registered_worktree_branch", probe_missing_then_real)

    result = ensure_worktree(str(repo), "task-race-1", "fix the thing")

    assert result.is_git is True
    assert result.reused is True
    assert result.branch == branch
    assert result.working_dir == expected_dir
    assert call_count["n"] == 2  # primary check (missed) + post-failure re-probe (found)


# ---------------------------------------------------------------------------
# Runner-aware detection + stdin forwarding — plumbing the completion path
# (finalize_worktree_session) needs to detect/operate on a remote worktree.
# ---------------------------------------------------------------------------

def test_is_linked_worktree_checks_through_a_given_runner(tmp_path: Path):
    """When a runner is supplied, detection goes through it (`test -f`)
    instead of the local filesystem — required for a remote worktree,
    where the path doesn't exist on this machine at all."""
    repo = _init_repo_with_origin(tmp_path)
    result = ensure_worktree(str(repo), "task-runner-detect", "fix the thing")

    calls = []

    def fake_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT):
        calls.append(cmd)
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    assert is_linked_worktree(result.working_dir, runner=fake_runner) is True
    assert any(cmd[:2] == ["test", "-f"] for cmd in calls)


def test_run_only_forwards_input_when_given(tmp_path: Path):
    """A runner that doesn't declare an `input` parameter must keep
    working for every call that doesn't need one — `_run` only adds
    `input` to the call when a caller actually passes it."""
    def strict_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT):
        # Would raise TypeError if `_run` always forwarded `input=None`.
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    result = git_worktree._run(["true"], runner=strict_runner)
    assert result.returncode == 0


def test_run_forwards_input_when_given():
    captured = {}

    def capturing_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT, input=None):
        captured["input"] = input
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    git_worktree._run(["cat"], runner=capturing_runner, input="hello body")
    assert captured["input"] == "hello body"
