"""Regression test for scripts/create-worktree.sh (#812).

On GNU/Linux, `stat -f %m "$lock_dir"` doesn't mean "read mtime" — `-f` means
FILESYSTEM status, so GNU coreutils treats `%m` as a filename, fails to find
it, and (depending on version) can still print a multi-line filesystem
report to stdout with exit 0. The `||` fallback to `stat -c %Y` never runs,
that blob lands inside `$(( ... ))`, and under `set -u` bash dies with
"File: unbound variable" — right after `git worktree add` already created
the worktree, orphaning it.

This test forces the port-lock contention path (a pre-existing, stale
`.port-lock` dir) that triggers the mtime read, and asserts the script
survives it cleanly: exit 0, worktree present, nothing "unbound variable"
in stderr.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "create-worktree.sh"


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )


def _init_repo_with_origin(root: Path) -> Path:
    """A working repo plus a local bare "origin" remote with main pushed —
    enough for create-worktree.sh's `git fetch origin` / `origin/main`
    branch-resolution path, with no real network access."""
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
    push = _git(repo, "push", "-q", "origin", "main")
    assert push.returncode == 0, push.stderr

    return repo


def test_stale_port_lock_contention_does_not_orphan_worktree(tmp_path: Path):
    """A pre-existing, stale `.port-lock` dir forces the script down the
    lock-contention branch that reads the lock's mtime. Acceptance: the
    script must exit 0, hand back a real worktree, and never die on an
    "unbound variable" from a wrong-platform `stat` invocation."""
    repo = _init_repo_with_origin(tmp_path)

    # Isolate $HOME so worktree_dir / .worktree-info / logs land entirely
    # under tmp_path, never the real ~/.claude/worktrees.
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    repo_name = repo.name  # "repo" — basename create-worktree.sh derives it as
    worktree_dir = fake_home / ".claude" / "worktrees" / repo_name
    worktree_dir.mkdir(parents=True)

    # Pre-existing stale lock: mkdir already exists (forces contention) and
    # its mtime is pushed well past the 300s staleness threshold.
    lock_dir = worktree_dir / ".port-lock"
    lock_dir.mkdir()
    stale_time = time.time() - 400
    os.utime(lock_dir, (stale_time, stale_time))

    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    # GIT_* vars are for CI hook invocation, not relevant to a direct
    # subprocess call, but scrub defensively in case the test host has one set.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(var, None)

    worktree_path = None
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=str(repo),
            input='{"name": "worktree-lock-test"}\n',
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "unbound variable" not in result.stderr
        assert "File:" not in result.stderr  # the stray `stat -f` filesystem report

        worktree_path = Path(result.stdout.strip().splitlines()[-1])
        assert worktree_path.is_dir(), result.stderr
        assert (worktree_path / ".git").exists()
        assert (worktree_path / ".worktree-info").exists()
    finally:
        if worktree_path and worktree_path.is_dir():
            _git(repo, "worktree", "remove", "--force", str(worktree_path))
        shutil.rmtree(tmp_path, ignore_errors=True)
