"""scripts/post-commit: timestamp normalization only, never a restart.

Production restart is a deployment concern exclusively (scripts/deploy.sh
for a direct commit, scripts/auto-deploy.sh for drift after a merge lands —
see tests/test_deploy_drift.py). This hook fires on every local commit in
every checkout, so it must never have a restart side effect of its own.
"""
from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POST_COMMIT = REPO_ROOT / "scripts" / "post-commit"

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "api").mkdir(parents=True)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "post-commit").write_text(POST_COMMIT.read_text(), encoding="utf-8")
    (scripts / "post-commit").chmod(0o755)
    # A hostile/no-op server.sh: if post-commit ever called it again, this
    # would prove it via the sentinel file rather than by trusting stdout.
    sentinel = repo / "server-sh-was-called"
    (scripts / "server.sh").write_text(f"#!/bin/bash\ntouch {sentinel}\nexit 0\n", encoding="utf-8")
    (scripts / "server.sh").chmod(scripts.joinpath("server.sh").stat().st_mode | stat.S_IEXEC)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    return repo


def _run_post_commit(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/post-commit"], cwd=repo,
        capture_output=True, text=True, timeout=30,
    )


def test_post_commit_normalizes_timestamp_and_never_restarts(tmp_path: Path):
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo(tmp_path)
    (repo / "api" / "handler.py").write_text("# change\n")
    _git(repo, "add", "-A")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2026-01-05T14:23:11-0500"
    env["GIT_COMMITTER_DATE"] = "2026-01-05T14:23:11-0500"
    subprocess.run(["git", "commit", "-qm", "touch api/"], cwd=repo, env=env, check=True)

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr

    author_date = subprocess.run(
        ["git", "log", "-1", "--format=%aI"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    # git's strict-ISO-8601 %aI rendering of a UTC offset varies by version
    # ("+00:00" vs. "Z" -- both denote the same instant), so parse it rather
    # than compare the formatted string.
    assert datetime.fromisoformat(author_date.replace("Z", "+00:00")) == datetime(
        2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc
    ), "timestamp must still be normalized to noon UTC"

    assert not (repo / "server-sh-was-called").exists(), (
        "post-commit must never invoke server.sh — restart is deployment-owned"
    )
    assert "restart" not in (result.stdout + result.stderr).lower()


def test_post_commit_merge_commit_also_normalized_and_never_restarts(tmp_path: Path):
    """Merge commits are normalized without restarting the server."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-qb", "feature")
    (repo / "api" / "handler.py").write_text("# feature change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature: touch api/")

    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main moved on\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated main commit")

    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr
    assert not (repo / "server-sh-was-called").exists()
    assert "restart" not in (result.stdout + result.stderr).lower()
