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

from api.services.agent_worker.git_worktree import (
    WorktreeError,
    derive_branch_name,
    describe_worktree,
    ensure_worktree,
    is_linked_worktree,
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


def test_derive_branch_name_defaults_to_agent_type_with_no_prefix():
    branch = derive_branch_name("clean up the reports folder", "task-xyz98765")
    assert branch.startswith("agent/clean-up-the-reports-folder-")


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
