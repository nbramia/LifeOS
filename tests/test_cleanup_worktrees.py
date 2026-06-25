"""Idempotency / stale-collision tests for scripts/cleanup-worktrees.sh (#400).

The doctor lifecycle creates an integration worktree off origin/main up front
and removes it at end-of-goal. A crashed run leaves the worktree directory and
its branch behind, so the next `git worktree add <same path>` fails with
"already exists" / "already checked out". cleanup-worktrees.sh is called
pre-flight to clear that; these tests exercise it against a real throwaway repo.
"""
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cleanup-worktrees.sh"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )


def _init_repo(root: Path) -> Path:
    """A minimal git repo with one commit on main — enough for worktrees."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_cleanup(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def test_prune_only_is_a_noop_on_clean_repo(tmp_path: Path):
    """No-arg cleanup succeeds on a repo with nothing stale (idempotent base)."""
    repo = _init_repo(tmp_path)
    result = _run_cleanup(repo)
    assert result.returncode == 0, result.stderr


def test_stale_worktree_preflight_clears_collision(tmp_path: Path):
    """The acceptance path: a stale worktree dir + branch from a 'crashed' run
    exist, and a fresh `git worktree add` at that path would fail. After the
    pre-flight cleanup the add succeeds — no 'already exists'/'already checked
    out' error."""
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "doctor-issue-123"
    branch = "doctor/issue-123"

    # Simulate the prior (crashed) run: worktree + branch created, never removed.
    add = _git(repo, "worktree", "add", "-b", branch, str(wt), "main")
    assert add.returncode == 0, add.stderr

    # Sanity: a naive re-add at the same path/branch collides right now.
    collide = _git(repo, "worktree", "add", "-b", branch, str(wt), "main")
    assert collide.returncode != 0

    # Pre-flight cleanup clears the stale worktree + branch.
    result = _run_cleanup(repo, str(wt), branch)
    assert result.returncode == 0, result.stderr
    assert not wt.exists()

    # The add the doctor would run next now succeeds.
    re_add = _git(repo, "worktree", "add", "-b", branch, str(wt), "main")
    assert re_add.returncode == 0, re_add.stderr


def test_cleanup_is_idempotent_when_already_clean(tmp_path: Path):
    """Calling cleanup for a worktree/branch that doesn't exist must not error —
    the recipe is safe to run unconditionally pre-flight."""
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "never-created"

    first = _run_cleanup(repo, str(wt), "doctor/never")
    assert first.returncode == 0, first.stderr
    # Second call over the same already-clean state is still a success.
    second = _run_cleanup(repo, str(wt), "doctor/never")
    assert second.returncode == 0, second.stderr


def test_stale_dir_without_git_tracking_is_removed(tmp_path: Path):
    """A leftover directory git no longer tracks (e.g. metadata pruned but the
    dir survived) is still cleared so the path is free for a fresh add."""
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "orphan"
    wt.mkdir(parents=True)
    (wt / "stale.txt").write_text("leftover\n")

    result = _run_cleanup(repo, str(wt))
    assert result.returncode == 0, result.stderr
    assert not wt.exists()
