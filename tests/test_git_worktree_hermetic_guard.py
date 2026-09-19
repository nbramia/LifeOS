"""Verifies the conftest-level guard that keeps `git_worktree.py`'s real
`git`/`gh` subprocesses confined to a temp directory.

`ensure_worktree`/`finalize_worktree_session` run real git against whatever
working directory (and, for `git push`/`gh`, whatever configured `origin`
remote) they're handed. The guard installed in `tests/conftest.py` wraps
`git_worktree._run` -- the single seam every call in the module funnels
through -- and fails loudly when a cwd, an absolute-path argument, or a
push/`gh` target isn't confined to `tempfile.gettempdir()`. This module is
the canary: it proves the guard actually trips on a real-looking path and a
non-local remote, and that it leaves a genuine tmp-path repo alone.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api.services.agent_worker import git_worktree


pytestmark = pytest.mark.unit

_GUARD_MESSAGE = "outside a temp directory"
_REMOTE_GUARD_MESSAGE = "non-local remote"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def test_guard_blocks_a_real_looking_checkout_path():
    """A cwd outside the temp root must never reach the real subprocess --
    the repository this test file itself lives in is real-looking (a real
    git worktree, not a throwaway path) and must be rejected."""
    real_checkout = str(Path(__file__).resolve().parents[1])

    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        git_worktree._run(["git", "status"], cwd=real_checkout)


def test_guard_blocks_an_absolute_path_argument_outside_temp():
    """Commands that carry the target path in argv instead of `cwd` (the
    ownership-marker `test -f`/`cat`/`mkdir -p` calls) are checked the same
    way."""
    real_checkout = str(Path(__file__).resolve().parents[1])

    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        git_worktree._run(["test", "-f", f"{real_checkout}/AGENTS.md"])


def test_guard_allows_a_tmp_path_repo(tmp_path: Path):
    """A real command against a real tmp_path git repo runs unmodified."""
    assert _git(tmp_path, "init", "-q", "-b", "main").returncode == 0

    result = git_worktree._run(["git", "rev-parse", "--show-toplevel"], cwd=str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_guard_skips_when_no_path_evidence_is_present():
    """A call with neither a cwd nor an absolute-path argument (only
    `_run`'s own timeout/OSError-conversion unit tests call it this way,
    against a fake runner that never touches the filesystem) has nothing to
    check and is left alone."""
    result = git_worktree._run(["true"])
    assert result.returncode == 0


def test_guard_blocks_push_to_a_non_local_remote(tmp_path: Path):
    assert _git(tmp_path, "init", "-q", "-b", "main").returncode == 0
    assert _git(tmp_path, "config", "user.email", "t@example.com").returncode == 0
    assert _git(tmp_path, "config", "user.name", "Test").returncode == 0
    (tmp_path / "f.txt").write_text("x\n")
    assert _git(tmp_path, "add", "f.txt").returncode == 0
    assert _git(tmp_path, "commit", "-q", "-m", "init").returncode == 0
    assert _git(tmp_path, "remote", "add", "origin", "https://example.invalid/repo.git").returncode == 0

    with pytest.raises(RuntimeError, match=_REMOTE_GUARD_MESSAGE):
        git_worktree._run(["git", "push", "origin", "main"], cwd=str(tmp_path))


def test_guard_allows_push_to_a_local_bare_origin_under_temp(tmp_path: Path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "t@example.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    (repo / "f.txt").write_text("x\n")
    assert _git(repo, "add", "f.txt").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "init").returncode == 0
    assert _git(repo, "remote", "add", "origin", str(origin)).returncode == 0

    result = git_worktree._run(["git", "push", "origin", "main"], cwd=str(repo))

    assert result.returncode == 0, result.stderr
