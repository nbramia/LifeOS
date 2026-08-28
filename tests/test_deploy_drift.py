"""Deploy-drift coverage (#631).

Three services (lifeos-api, lifeos-mcp-http, lifeos-agent-worker) were found
running three-day-old code while autodeploy fired cleanly every 10 minutes the
whole time. Three independent mechanisms each declined to act for individually
defensible reasons:

1. `scripts/post-commit`'s change detection used `git diff-tree -r HEAD`,
   which is blind to merge commits (no output without `-m`/`--first-parent`),
   so a local merge restarted nothing.
2. `scripts/auto-deploy.sh` inferred "services are current" from "nothing to
   pull" (`LOCAL == REMOTE`) — false the instant a merge is pushed from the
   canonical checkout itself.
3. The agent worker had no restart path at all in that workflow, and no
   explicit busy/idle policy.

This file covers (1) via subprocess (bash isn't pytest-covered except by
driving the real script, per the established pattern in
test_agent_worker_self_restart.py), and the decision helpers behind (2)/(3) —
`newest_code_mtime`, `service_active_since_epoch`, `worker_busy` — by
`source`-ing scripts/auto-deploy.sh (its operational body is wrapped in
`main()` and guarded so sourcing only defines functions, never fetches,
pulls, or restarts anything for real).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    SessionStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
POST_COMMIT = REPO_ROOT / "scripts" / "post-commit"
AUTO_DEPLOY = REPO_ROOT / "scripts" / "auto-deploy.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _stub_bin(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


# ---------------------------------------------------------------------------
# Gap 1 — post-commit's merge blindness
# ---------------------------------------------------------------------------
def _make_repo_with_post_commit(tmp_path: Path) -> Path:
    """A synthetic repo with the real post-commit script + a stubbed
    server.sh, systemctl, and sudo so nothing real is ever restarted."""
    repo = tmp_path / "repo"
    (repo / "api").mkdir(parents=True)
    (repo / "config").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "post-commit").write_text(POST_COMMIT.read_text(), encoding="utf-8")
    (scripts / "post-commit").chmod(0o755)
    # No-op server.sh — the hook only checks `-x` and backgrounds a call to it.
    _stub_bin(scripts, "server.sh", "exit 0\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _run_post_commit(repo: Path) -> subprocess.CompletedProcess:
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "systemctl", "exit 1\n")  # lifeos-mcp-http.service "not found"
    _stub_bin(bindir, "sudo", 'exec "$@"\n')
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", "scripts/post-commit"], cwd=repo, env=env,
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.unit
def test_post_commit_detects_merge_touching_api(tmp_path: Path):
    """A merge commit touching api/ must still trigger the API restart —
    this is the exact scenario that silently restarted nothing (#631)."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo_with_post_commit(tmp_path)

    _git(repo, "checkout", "-qb", "feature")
    (repo / "api" / "handler.py").write_text("# feature change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature: touch api/")

    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main moved on\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated main commit")

    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")
    # Sanity: this is genuinely a merge commit non-git-diff-tree-visible case.
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert len(parents) == 3, "expected a real 2-parent merge commit"

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr
    assert "Server restart triggered" in result.stdout, result.stdout
    assert "No server restart needed" not in result.stdout


@pytest.mark.unit
def test_post_commit_merge_touching_docs_only_restarts_nothing(tmp_path: Path):
    """A merge whose only changes are under docs/ must still restart nothing
    (today's non-merge behavior, preserved for merges too)."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo_with_post_commit(tmp_path)

    _git(repo, "checkout", "-qb", "feature")
    (repo / "docs" / "notes.md").write_text("feature docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature: docs only")

    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main moved on\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated main commit")

    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr
    assert "No server restart needed" in result.stdout, result.stdout
    assert "Server restart triggered" not in result.stdout
    assert "MCP HTTP service restart triggered" not in result.stdout


@pytest.mark.unit
def test_post_commit_merge_touching_mcp_server_triggers_mcp_restart(tmp_path: Path):
    """A merge touching mcp_server.py must restart lifeos-mcp-http."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo_with_post_commit(tmp_path)
    (repo / "mcp_server.py").write_text("# base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add mcp_server.py")

    _git(repo, "checkout", "-qb", "feature")
    (repo / "mcp_server.py").write_text("# feature change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature: touch mcp_server.py")

    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main moved on\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated main commit")

    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")

    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    # This time report the mcp-http unit as installed so the hook takes the branch.
    _stub_bin(bindir, "systemctl", 'exit 0\n')
    _stub_bin(bindir, "sudo", 'exec "$@"\n')
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", "scripts/post-commit"], cwd=repo, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "MCP HTTP service restart triggered" in result.stdout, result.stdout


@pytest.mark.unit
def test_post_commit_normal_commit_docs_only_restarts_nothing(tmp_path: Path):
    """Regression guard: an ordinary (non-merge) docs-only commit must keep
    restarting nothing, unaffected by the `-m --first-parent` change."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo_with_post_commit(tmp_path)
    (repo / "docs" / "notes.md").write_text("more docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs only")

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr
    assert "No server restart needed" in result.stdout, result.stdout


@pytest.mark.unit
def test_post_commit_normal_commit_api_change_still_restarts(tmp_path: Path):
    """Regression guard: an ordinary (non-merge) api/ commit must still
    restart, unaffected by the `-m --first-parent` change."""
    if not POST_COMMIT.exists():
        pytest.skip("scripts/post-commit not present")
    repo = _make_repo_with_post_commit(tmp_path)
    (repo / "api" / "handler.py").write_text("# change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch api/")

    result = _run_post_commit(repo)
    assert result.returncode == 0, result.stderr
    assert "Server restart triggered" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Gap 2/3 — auto-deploy.sh's drift-check helpers (sourced, not executed)
# ---------------------------------------------------------------------------
def _run_sourced(repo: Path, call: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source auto-deploy.sh (defines functions only — see module docstring)
    then run `call`. cwd=repo so PROJECT_DIR-relative git/file operations
    inside the helpers see the synthetic repo, not the real one.

    SYNC_LOCK_FILE is fixed at `$HOME/.lifeos/sync.lock` (found on
    re-review: an env-var override would let a `.env`-set path silently
    diverge from run_all_syncs.py's own lock, since that script alone
    reads .env) — so isolation from the real machine's actual lock file
    means pointing $HOME itself at a directory under the synthetic repo,
    the same technique test_lock_is_visible_across_two_different_checkouts_
    sharing_home already uses to prove the real default formula, not a
    test-only escape hatch. A test that specifically wants the process's
    real $HOME passes its own env_extra with that key set back."""
    script = repo / "scripts" / "auto-deploy.sh"
    fake_home = repo / "home"
    fake_home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'source "{script}" && {call}'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


def _make_repo_for_drift(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "api").mkdir(parents=True)
    (repo / "config").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")
    (repo / "scripts" / "auto-deploy.sh").chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    return repo


@pytest.mark.unit
def test_sourcing_auto_deploy_does_not_run_main(tmp_path: Path):
    """Sourcing must only define functions — no fetch/pull/restart/exit."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(repo, "echo did-not-exit")
    assert result.returncode == 0, result.stderr
    assert "did-not-exit" in result.stdout


@pytest.mark.unit
def test_newest_code_mtime_ignores_docs_and_untracked_files(tmp_path: Path):
    """Only tracked files under api/, config/, mcp_server.py count — an
    untracked file (e.g. a .pyc-like build artifact) and docs/ changes must
    not move the watermark forward or backward."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    (repo / "api" / "a.py").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    before = _run_sourced(repo, "newest_code_mtime").stdout.strip()
    assert before, "expected a real mtime for a tracked api/ file"

    # Untracked file under a watched dir (simulates a __pycache__/*.pyc) —
    # must be invisible to the watermark.
    (repo / "api" / "__pycache__").mkdir()
    (repo / "api" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    os.utime(repo / "api" / "__pycache__" / "a.cpython-312.pyc", (9_999_999_999, 9_999_999_999))
    after_untracked = _run_sourced(repo, "newest_code_mtime").stdout.strip()
    assert after_untracked == before

    # docs/ change — not a watched path at all. Add the docs file explicitly
    # (not `-A`) so the untracked pycache junk above doesn't get swept in and
    # invalidate the "untracked files don't count" half of this test.
    (repo / "docs" / "notes.md").write_text("docs\n")
    _git(repo, "add", "docs/notes.md")
    _git(repo, "commit", "-qm", "docs")
    after_docs = _run_sourced(repo, "newest_code_mtime").stdout.strip()
    assert after_docs == before


@pytest.mark.unit
def test_newest_code_mtime_reflects_real_api_edit(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    (repo / "api" / "a.py").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    before = int(_run_sourced(repo, "newest_code_mtime").stdout.strip())

    (repo / "config" / "c.py").write_text("changed\n")
    os.utime(repo / "config" / "c.py", (before + 1000, before + 1000))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "config change")
    after = int(_run_sourced(repo, "newest_code_mtime").stdout.strip())
    assert after >= before + 1000


# ---------------------------------------------------------------------------
# #792 — .env's mtime is a second, independent drift signal
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_env_file_mtime_reflects_env_edit(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    (repo / ".env").write_text("LIFEOS_AUTODEPLOY_ENABLED=true\n")
    os.utime(repo / ".env", (12345, 12345))
    result = _run_sourced(repo, "env_file_mtime")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "12345"


@pytest.mark.unit
def test_env_file_mtime_empty_and_no_error_when_env_file_absent(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(repo, 'env_file_mtime; echo "rc=$?"')
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout
    # Nothing printed before the "rc=0" line.
    assert result.stdout.strip() == "rc=0"


@pytest.mark.unit
def test_service_active_since_epoch_unknown_for_inactive_unit(tmp_path: Path):
    """systemctl reporting no ActiveEnterTimestamp (inactive/nonexistent
    unit) must be treated as unknown (empty output, nonzero exit) — never a
    guessed value."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir()
    _stub_bin(bindir, "systemctl", "echo -n ''\nexit 0\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", "-c", 'source scripts/auto-deploy.sh && service_active_since_epoch some.service; echo "rc=$?"'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_service_active_since_epoch_parses_systemd_timestamp(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir()
    _stub_bin(bindir, "systemctl", "echo 'Thu 2026-08-20 12:00:00 UTC'\nexit 0\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, "service_active_since_epoch some.service", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1787227200"  # 2026-08-20T12:00:00Z


def _venv_with_python(tmp_path: Path) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    return venv


@pytest.mark.unit
def test_worker_busy_true_when_non_terminal_session_exists(tmp_path: Path):
    """Authoritative source of truth: the session store. A claimed or
    running row means busy (yielded does not — see #636)."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    workdir = tmp_path / "work"
    (workdir / "data").mkdir(parents=True)
    (workdir / "scripts").mkdir()
    (workdir / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")

    db_path = workdir / "data" / "agent_sessions.db"
    store = SessionStore(db_path=db_path)
    store.create(task_id="t-1", session_id="s-1", status=STATUS_CLAIMED)

    venv = _venv_with_python(tmp_path)
    env = dict(os.environ)
    env["LIFEOS_VENV"] = str(venv)
    env["LIFEOS_AGENT_SESSIONS_DB"] = str(db_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        ["bash", "-c", 'source scripts/auto-deploy.sh && worker_busy; echo "rc=$?"'],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=30,
    )
    assert "rc=0" in result.stdout, (result.stdout, result.stderr)


@pytest.mark.unit
def test_worker_busy_false_when_no_sessions(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    workdir = tmp_path / "work"
    (workdir / "data").mkdir(parents=True)
    (workdir / "scripts").mkdir()
    (workdir / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")
    # Create the store (and its schema) with zero sessions.
    db_path = workdir / "data" / "agent_sessions.db"
    SessionStore(db_path=db_path)

    venv = _venv_with_python(tmp_path)
    env = dict(os.environ)
    env["LIFEOS_VENV"] = str(venv)
    env["LIFEOS_AGENT_SESSIONS_DB"] = str(db_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        ["bash", "-c", 'source scripts/auto-deploy.sh && worker_busy; echo "rc=$?"'],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=30,
    )
    assert "rc=1" in result.stdout, (result.stdout, result.stderr)


@pytest.mark.unit
def test_worker_busy_defaults_to_busy_on_query_failure(tmp_path: Path):
    """No DB, broken venv, whatever — an inability to determine busy-ness
    must default to busy (defer), not idle (restart and risk killing an
    in-flight #agent session)."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "scripts").mkdir()
    (workdir / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")
    # No data/ dir, no venv at all — LIFEOS_VENV points nowhere.
    env = dict(os.environ)
    env["LIFEOS_VENV"] = str(workdir / "no-such-venv")
    result = subprocess.run(
        ["bash", "-c", 'source scripts/auto-deploy.sh && worker_busy; echo "rc=$?"'],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=30,
    )
    assert "rc=0" in result.stdout, (result.stdout, result.stderr)


@pytest.mark.unit
def test_auto_deploy_syntax_and_sourced_functions_defined(tmp_path: Path):
    """bash -n passes, and sourcing exposes exactly the decision helpers a
    drift check needs."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    syntax = subprocess.run(["bash", "-n", str(AUTO_DEPLOY)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        "type -t newest_code_mtime env_file_mtime env_mtime_applied_file "
        "env_stale_for_unit mark_env_mtime_applied service_active_since_epoch "
        "worker_busy sync_in_progress_systemd sync_in_progress_lock_acquire "
        "sync_in_progress_lock_release main",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("function") == 11, result.stdout


def _wait_until_lock_held(lock_path: Path, timeout: float = 5.0) -> None:
    """Poll until some other process holds an exclusive-incompatible lock on
    lock_path — i.e. until a background holder has actually acquired, not
    just been spawned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(["flock", "-x", "-n", str(lock_path), "-c", "true"])
        if probe.returncode != 0:
            return
        time.sleep(0.05)
    raise TimeoutError(f"lock at {lock_path} was never acquired by the holder")


def _start_shared_lock_holder(lock_path: Path, seconds: float = 5.0) -> subprocess.Popen:
    """Spawn a single process — no fork/exec split — that opens lock_path,
    holds a SHARED flock, and sleeps. Deliberately not the `flock` CLI: it
    forks a child to run the given command while the parent (the pid Popen
    would return) keeps the fd open, so killing that parent alone does not
    release the lock (the forked child inherits its own copy of the fd).
    This one-liner IS the process holding the lock, so killing this exact
    pid releases it immediately — mirroring what
    run_all_syncs.py's _acquire_sync_lock() actually does."""
    return subprocess.Popen([
        sys.executable, "-c",
        f"import fcntl, time\n"
        f"f = open({str(lock_path)!r}, 'w')\n"
        f"fcntl.flock(f.fileno(), fcntl.LOCK_SH)\n"
        f"time.sleep({seconds})\n",
    ])


# ---------------------------------------------------------------------------
# #793 — sync_in_progress_lock_acquire/_release: a shared/exclusive flock on
# data/sync.lock, replacing an earlier pid-in-a-file marker design that
# review found to be TOCTOU (checked once, then restarted later), vulnerable
# to pid reuse (a dead sync's marker misread as a live, unrelated process),
# and unable to represent two overlapping manual syncs (one global marker,
# overwritten and cleared by whichever sync writes/exits last). A kernel-held
# flock has none of those: any number of processes can each hold the shared
# side independently, the kernel releases it the instant a holder exits for
# ANY reason (including SIGKILL) with no cleanup code involved, and
# auto-deploy.sh holds its own exclusive lock for its *entire* restart
# section (not just a point-in-time check), closing the TOCTOU gap.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_sync_in_progress_systemd_true_when_unit_active(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "systemctl", "echo active\nexit 0\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, 'sync_in_progress_systemd; echo "rc=$?"', env)
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_sync_in_progress_systemd_false_when_unit_inactive(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "systemctl", "echo inactive\nexit 0\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, 'sync_in_progress_systemd; echo "rc=$?"', env)
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_lock_acquire_succeeds_when_no_sync_holds_it(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_lock_acquire_defers_while_a_sync_holds_the_shared_lock(tmp_path: Path):
    """The exact mechanism run_all_syncs.py uses: a shared flock held for a
    process's whole lifetime. While held, the exclusive/non-blocking acquire
    here must fail — sync in progress, matching acceptance criterion #1."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    # Matches _run_sourced()'s fake $HOME/.lifeos/sync.lock.
    lock_path = repo / "home" / ".lifeos" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _start_shared_lock_holder(lock_path, seconds=5)
    try:
        _wait_until_lock_held(lock_path)
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=0" in result.stdout, result.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.unit
def test_lock_released_immediately_when_holder_is_sigkilled(tmp_path: Path):
    """A hard-killed sync must not defer forever — the kernel releases the
    flock the instant the holding process dies, with no cleanup code
    required (acceptance criterion: a stale marker can never defer
    forever, now true by construction rather than by a pid-liveness check)."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    # Matches _run_sourced()'s fake $HOME/.lifeos/sync.lock.
    lock_path = repo / "home" / ".lifeos" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _start_shared_lock_holder(lock_path, seconds=30)
    try:
        _wait_until_lock_held(lock_path)
        holder.kill()  # SIGKILL — no signal handler, no cleanup path runs
        holder.wait(timeout=5)
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=1" in result.stdout, result.stdout
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


@pytest.mark.unit
def test_lock_acquire_ignores_unrelated_process_mentioning_script_name(tmp_path: Path):
    """The original false-positive this whole mechanism replaced: a process
    whose command line merely mentions the sync script's name as a plain
    argument — and never touches the lock file — must not be treated as a
    genuinely running sync."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(5)", "run_all_syncs.py"]
    )
    try:
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=1" in result.stdout, result.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.unit
def test_two_overlapping_syncs_are_both_recognized_as_in_progress(tmp_path: Path):
    """Multiple concurrent manual syncs must each be recognized — no single
    global marker for one to overwrite or clear out from under the other."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    # Matches _run_sourced()'s fake $HOME/.lifeos/sync.lock.
    lock_path = repo / "home" / ".lifeos" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    a = _start_shared_lock_holder(lock_path, seconds=5)
    b = _start_shared_lock_holder(lock_path, seconds=5)
    try:
        _wait_until_lock_held(lock_path)
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=0" in result.stdout, result.stdout
        b.terminate()
        b.wait(timeout=5)
        # a is still holding its own shared lock, independent of b's.
        result2 = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=0" in result2.stdout, result2.stdout
    finally:
        for p in (a, b):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)


@pytest.mark.unit
def test_lock_is_visible_across_two_different_checkouts_sharing_home(tmp_path: Path):
    """Finding 2 (review): the lock must be host-wide, not keyed to
    $PROJECT_DIR/data — this repo is routinely worked in multiple git
    worktrees, each with its own checkout-local data/ directory, so a lock
    keyed to PROJECT_DIR is invisible across checkouts. A sync launched
    from checkout B must still defer a deploy running from checkout A.
    Two independent synthetic checkouts share $HOME (so the fixed
    $HOME/.lifeos/sync.lock path — not configurable, per Finding NEW-2 —
    resolves identically for both) but each has its own PROJECT_DIR,
    proving cross-checkout visibility without touching the real machine's
    actual lock file (HOME points at an isolated sandbox for the duration
    of this test)."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    shared_home = tmp_path / "home"
    shared_home.mkdir()
    checkout_a = _make_repo_for_drift(tmp_path / "checkout-a")
    checkout_b = _make_repo_for_drift(tmp_path / "checkout-b")

    default_lock_path = shared_home / ".lifeos" / "sync.lock"
    default_lock_path.parent.mkdir(parents=True)

    env = dict(os.environ)
    env["HOME"] = str(shared_home)

    holder = _start_shared_lock_holder(default_lock_path, seconds=5)
    try:
        _wait_until_lock_held(default_lock_path)
        script_a = checkout_a / "scripts" / "auto-deploy.sh"
        result = subprocess.run(
            ["bash", "-c", f'source "{script_a}" && sync_in_progress_lock_acquire; echo "rc=$?"'],
            cwd=checkout_a, env=env, capture_output=True, text=True, timeout=30,
        )
        assert "rc=0" in result.stdout, result.stdout  # deferred: sees checkout B's sync

        # And checkout B's OWN auto-deploy.sh sees it too, of course.
        script_b = checkout_b / "scripts" / "auto-deploy.sh"
        result_b = subprocess.run(
            ["bash", "-c", f'source "{script_b}" && sync_in_progress_lock_acquire; echo "rc=$?"'],
            cwd=checkout_b, env=env, capture_output=True, text=True, timeout=30,
        )
        assert "rc=0" in result_b.stdout, result_b.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    # Once released, both checkouts see it as free again.
    result_after = subprocess.run(
        ["bash", "-c", f'source "{checkout_a / "scripts" / "auto-deploy.sh"}" && sync_in_progress_lock_acquire; echo "rc=$?"'],
        cwd=checkout_a, env=env, capture_output=True, text=True, timeout=30,
    )
    assert "rc=1" in result_after.stdout, result_after.stdout


@pytest.mark.unit
def test_lock_release_allows_a_subsequent_acquire(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        'sync_in_progress_lock_acquire; echo "first=$?"; '
        'sync_in_progress_lock_release; '
        'sync_in_progress_lock_acquire; echo "second=$?"',
    )
    assert "first=1" in result.stdout, result.stdout
    assert "second=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_lock_acquire_logs_distinct_error_when_flock_itself_fails(tmp_path: Path):
    """Found on review: every flock failure used to look identical to 'sync
    in progress'. A genuine tool/environment failure (here simulated by a
    broken `flock`) must still defer — the safe default — but log something
    an operator can actually diagnose, not the generic sync-busy message."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    # Exit 2 — anything other than flock's own conflict-exit-code (75) — is
    # an unexpected failure, not "someone else holds it".
    _stub_bin(bindir, "flock", "exit 2\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"', env)
    assert "rc=0" in result.stdout, result.stdout  # still defers
    log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "flock failed unexpectedly" in log, log


@pytest.mark.unit
def test_mark_env_mtime_applied_logs_error_and_leaves_no_marker_on_write_failure(tmp_path: Path):
    """A kill, full disk, or permission problem during the write must not
    leave a corrupt/partial marker — the atomic-rename-after-verify design
    means the target either has the full correct value or doesn't exist."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        'ENV_MTIME=100; mv() { return 1; }; mark_env_mtime_applied lifeos-api',
    )
    assert result.returncode == 0, result.stderr
    assert not (repo / "data" / "env-mtime-applied-lifeos-api").exists()
    log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "could not durably record" in log, log


# ---------------------------------------------------------------------------
# env_stale_for_unit / mark_env_mtime_applied — fixes a future-.env-mtime
# restart loop found on review: comparing a unit's active_since against
# ENV_MTIME directly breaks if ENV_MTIME is ever in the future (clock skew,
# `rsync -t`, a bad manual `touch`) — active_since stays "before" a future
# mtime forever, so the unit would restart on every tick indefinitely.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_env_stale_for_unit_bootstrap_true_when_no_applied_marker_yet(tmp_path: Path):
    """No applied-marker recorded yet (fresh install, or upgrading to this
    check for the first time): falls back to the original active_since
    comparison, so behavior is unchanged for an already-configured install."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        'ENV_MTIME=200; env_stale_for_unit lifeos-api 100; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_env_stale_for_unit_bootstrap_false_when_active_since_already_newer(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        'ENV_MTIME=100; env_stale_for_unit lifeos-api 200; echo "rc=$?"',
    )
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_mark_env_mtime_applied_then_env_stale_for_unit_is_false_even_with_future_mtime(
    tmp_path: Path,
):
    """The actual bug: a future ENV_MTIME must restart exactly once, not on
    every tick forever. After mark_env_mtime_applied records the value that
    was just restarted for, a later check against the SAME (still-future)
    ENV_MTIME must report NOT stale — the comparison is now value-equality,
    not a wall-clock '<'."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    future = int(time.time()) + 3600
    result = _run_sourced(
        repo,
        f'ENV_MTIME={future}; '
        f'mark_env_mtime_applied lifeos-api; '
        f'env_stale_for_unit lifeos-api 999999999999; echo "rc=$?"',
    )
    assert "rc=1" in result.stdout, result.stdout
    applied_file = repo / "data" / "env-mtime-applied-lifeos-api"
    assert applied_file.read_text().strip() == str(future)


@pytest.mark.unit
def test_env_stale_for_unit_true_when_env_mtime_changes_again_after_being_applied(
    tmp_path: Path,
):
    """A genuinely new .env edit after one was already applied must still be
    detected — the marker isn't a one-time 'never restart again' switch.
    active_since is realistically BEFORE both edits: env_stale_for_unit now
    requires active_since < ENV_MTIME too (found on review — the marker
    alone isn't enough, see the function's own comment), so an active_since
    after ENV_MTIME would correctly read as "already current" regardless of
    the marker, which is not what this test is checking."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(
        repo,
        'ENV_MTIME=100; mark_env_mtime_applied lifeos-api; '
        'ENV_MTIME=200; env_stale_for_unit lifeos-api 50; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_env_stale_for_unit_false_when_no_env_mtime_at_all(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    result = _run_sourced(repo, 'ENV_MTIME=""; env_stale_for_unit lifeos-api 100; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_mark_env_mtime_applied_is_a_noop_when_env_mtime_unset(tmp_path: Path):
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo = _make_repo_for_drift(tmp_path)
    _run_sourced(repo, 'ENV_MTIME=""; mark_env_mtime_applied lifeos-api')
    assert not (repo / "data" / "env-mtime-applied-lifeos-api").exists()


# ---------------------------------------------------------------------------
# Gap 2/3 integration — the actual per-service loop inside main(), not just
# the helpers in isolation: restart-when-idle/defer-when-busy in one tick,
# and no-thrash across two consecutive ticks with no new commits.
# ---------------------------------------------------------------------------
def _make_policy_repo(tmp_path: Path) -> tuple[Path, Path, int]:
    """A real git repo (with a real 'origin' remote so `main()`'s fetch/pull
    guards are genuinely exercised) laid out like the canonical checkout, plus
    a stub bin/ for systemctl and sudo whose fake service state lives in
    files under `state/` (so a stubbed restart can move a unit's recorded
    start time forward, the way a real restart would). Returns
    (repo, state_dir, code_epoch) where code_epoch is the fixed mtime given
    to the one tracked api/ file, an hour in the past.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-qb", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "api").mkdir()
    (repo / "config").mkdir()
    (repo / "scripts").mkdir()
    (repo / "data").mkdir()
    (repo / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")
    (repo / "scripts" / "auto-deploy.sh").chmod(0o755)
    (repo / "api" / "a.py").write_text("code\n")
    (repo / ".env").write_text(
        "LIFEOS_AUTODEPLOY_ENABLED=true\nLIFEOS_AUTODEPLOY_NOTIFY=never\n"
    )
    # Matches the real repo's .gitignore: logs/ and data/ are runtime output,
    # not source — without this, main()'s own `mkdir -p logs` and the
    # SessionStore db it creates would show up as untracked files and trip
    # the "working tree dirty" guard before the drift check ever runs.
    (repo / ".gitignore").write_text("logs/\ndata/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main")

    import time as _time
    code_epoch = int(_time.time()) - 3600  # "code changed an hour ago"
    os.utime(repo / "api" / "a.py", (code_epoch, code_epoch))

    state = tmp_path / "state"
    state.mkdir()
    return repo, state, code_epoch


_SYSTEMCTL_STUB = r"""
STATE="$LIFEOS_TEST_STATE"
case "$1" in
    show)
        # sync_in_progress's lifeos-sync.service query is a `show` too —
        # distinguish by unit name ($2), not by the -p property (which
        # shifts position depending on whether --value comes before/after).
        if [ "$2" = "lifeos-sync.service" ]; then
            echo "inactive"; exit 0
        fi
        unit="$2"
        f="$STATE/since_$unit"
        [ -f "$f" ] && echo "@$(cat "$f")"
        exit 0
        ;;
    is-active)
        unit="${*: -1}"
        [ -f "$STATE/active_$unit" ] && exit 0 || exit 3
        ;;
    restart)
        unit="$2"
        date +%s > "$STATE/since_$unit"
        echo "restart $unit" >> "$STATE/restart.log"
        exit 0
        ;;
esac
exit 1
"""

_SUDO_STUB = 'while [[ "$1" == -* ]]; do shift; done\nexec "$@"\n'


def _write_policy_stubs(state: Path) -> Path:
    bindir = state / "bin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "systemctl", _SYSTEMCTL_STUB)
    _stub_bin(bindir, "sudo", _SUDO_STUB)
    _stub_bin(bindir, "curl", "exit 0\n")  # only reached if something restarted
    return bindir


def _run_main(repo: Path, state: Path, venv: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{_write_policy_stubs(state)}:{env['PATH']}"
    env["LIFEOS_TEST_STATE"] = str(state)
    env["LIFEOS_VENV"] = str(venv)
    env["LIFEOS_AGENT_SESSIONS_DB"] = str(repo / "data" / "agent_sessions.db")
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Isolated by default — see _run_sourced()'s comment on why this is a
    # $HOME override, not a sync-lock-specific one.
    fake_home = repo / "home"
    fake_home.mkdir(exist_ok=True)
    env["HOME"] = str(fake_home)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", "source scripts/auto-deploy.sh && main"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.unit
def test_main_restarts_stale_services_and_defers_busy_worker(tmp_path: Path):
    """One tick, three active-but-stale services: api and mcp-http restart;
    the worker — mid-session — defers instead (#631 acceptance: never
    silently stay stale, never kill an in-flight #agent session)."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    stale = code_epoch - 3600  # started 2h ago, an hour before the code changed
    for unit in ("lifeos-api", "lifeos-mcp-http", "lifeos-agent-worker"):
        (state / f"active_{unit}").touch()
        (state / f"since_{unit}").write_text(str(stale))

    store = SessionStore(db_path=repo / "data" / "agent_sessions.db")
    store.create(task_id="t-1", session_id="s-1", status=STATUS_CLAIMED)

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)

    restart_log = (state / "restart.log").read_text() if (state / "restart.log").exists() else ""
    assert "restart lifeos-api" in restart_log
    assert "restart lifeos-mcp-http" in restart_log
    assert "restart lifeos-agent-worker" not in restart_log

    deploy_log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "defer" in deploy_log and "lifeos-agent-worker" in deploy_log


@pytest.mark.unit
def test_main_no_thrash_on_repeat_run_with_no_new_commits(tmp_path: Path):
    """A stale, active-only-as-api service gets restarted on tick 1. Tick 2,
    with no new commits, must not restart it again — the point of comparing
    against the service's own (now-current) start time instead of re-deriving
    from 'did a pull just happen'."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    stale = code_epoch - 3600
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(stale))
    # mcp-http and the worker are simply not active — the loop skips them.

    venv = _venv_with_python(tmp_path)

    first = _run_main(repo, state, venv)
    assert first.returncode == 0, (first.stdout, first.stderr)
    restart_log = (state / "restart.log").read_text()
    assert restart_log.count("restart lifeos-api") == 1

    second = _run_main(repo, state, venv)
    assert second.returncode == 0, (second.stdout, second.stderr)
    restart_log_after = (state / "restart.log").read_text()
    assert restart_log_after.count("restart lifeos-api") == 1, (
        "second tick with no new commits restarted an already-current service"
    )


@pytest.mark.unit
def test_main_restarts_when_only_env_file_changed(tmp_path: Path):
    """#792: editing .env (config, not tracked source) must still be
    treated as drift — a service started after the last code change but
    before a later .env edit must still restart."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    # Started well after the code last changed (not code-stale)...
    active_since = code_epoch + 100
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(active_since))
    # ...but .env was edited after that (env-stale).
    env_mtime = active_since + 200
    os.utime(repo / ".env", (env_mtime, env_mtime))

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)

    restart_log = (state / "restart.log").read_text() if (state / "restart.log").exists() else ""
    assert "restart lifeos-api" in restart_log, (result.stdout, result.stderr, restart_log)
    deploy_log = (repo / "logs" / "auto-deploy.log").read_text()
    assert ".env changed" in deploy_log, deploy_log


@pytest.mark.unit
def test_main_does_not_restart_again_after_a_manual_restart_already_picked_up_env(
    tmp_path: Path,
):
    """Finding 1 (review): a marker-only check is wrong once a marker
    exists. Scenario reproduced end-to-end: tick 1 restarts lifeos-api for
    code drift, recording an env-mtime-applied marker for whatever .env
    said at that moment. The operator then edits .env AND manually runs
    `sudo systemctl restart lifeos-api` themselves (never going through
    auto-deploy.sh, so the marker is never updated) — simulated here by
    bumping the stub's recorded start time forward without calling
    _run_main. Tick 2 must NOT restart again: active_since already
    postdates the .env edit, so it's already current regardless of what
    the (now-stale) marker says."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    venv = _venv_with_python(tmp_path)
    stale = code_epoch - 3600  # started well before the code last changed
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(stale))

    first = _run_main(repo, state, venv)
    assert first.returncode == 0, (first.stdout, first.stderr)
    restart_log_after_first = (state / "restart.log").read_text()
    assert restart_log_after_first.count("restart lifeos-api") == 1, restart_log_after_first
    applied_marker = repo / "data" / "env-mtime-applied-lifeos-api"
    assert applied_marker.exists(), "tick 1's restart must record an applied marker"

    # Operator edits .env, then manually restarts the unit themselves —
    # never through auto-deploy.sh, so the marker above is never touched.
    manual_restart_time = int(time.time()) + 1000
    env_mtime = manual_restart_time - 100  # .env edited before the manual restart
    os.utime(repo / ".env", (env_mtime, env_mtime))
    (state / "since_lifeos-api").write_text(str(manual_restart_time))

    second = _run_main(repo, state, venv)
    assert second.returncode == 0, (second.stdout, second.stderr)
    restart_log_after_second = (state / "restart.log").read_text()
    assert restart_log_after_second.count("restart lifeos-api") == 1, (
        "must not restart again — the manual restart already picked up the .env edit",
        restart_log_after_second,
    )


@pytest.mark.unit
def test_main_no_restart_when_neither_code_nor_env_changed_since_start(tmp_path: Path):
    """Regression guard: the new .env signal must not become 'always
    restart' — a service started after both the code and .env's last
    change stays put."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    env_mtime = code_epoch - 100  # .env last changed before the code did
    os.utime(repo / ".env", (env_mtime, env_mtime))
    active_since = code_epoch + 100  # service started after both
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(active_since))

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)

    restart_log = (state / "restart.log").read_text() if (state / "restart.log").exists() else ""
    assert "restart lifeos-api" not in restart_log, restart_log


@pytest.mark.unit
def test_main_proceeds_when_only_untracked_files_are_present(tmp_path: Path):
    """#634: an untracked path must not block the deploy.

    `.worktrees/` is the conventional location for worktree-based development
    here — `.git/hooks/post-commit` already expects worktrees to exist — and it
    is untracked. While the guard counted untracked paths, its mere presence
    made `git status --porcelain` non-empty, so auto-deploy skipped every tick
    silently: 62 consecutive skips on the real host, during which #631's drift
    check never executed at all. The service stayed stale and the only evidence
    was a log line nobody reads.
    """
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    stale = code_epoch - 3600
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(stale))

    # Exactly the real-world shape: an untracked directory at the repo root.
    (repo / ".worktrees" / "issue-999").mkdir(parents=True)
    (repo / ".worktrees" / "issue-999" / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip(), "fixture must actually leave the tree untracked-dirty"

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)

    deploy_log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "not auto-deploying over local edits" not in deploy_log, deploy_log
    restart_log = (state / "restart.log").read_text() if (state / "restart.log").exists() else ""
    assert "restart lifeos-api" in restart_log, (deploy_log, restart_log)


@pytest.mark.unit
def test_main_still_skips_when_a_tracked_file_is_modified(tmp_path: Path):
    """The guard's actual purpose survives #634's narrowing: uncommitted edits
    to tracked code still stop the deploy. Loosening to
    `--untracked-files=no` must not become "never skip"."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, code_epoch = _make_policy_repo(tmp_path)
    stale = code_epoch - 3600
    (state / "active_lifeos-api").touch()
    (state / "since_lifeos-api").write_text(str(stale))

    tracked = next(
        p for p in (repo / "api").rglob("*.py")
        if subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(p.relative_to(repo))],
            cwd=repo, capture_output=True,
        ).returncode == 0
    )
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)

    deploy_log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "not auto-deploying over local edits" in deploy_log, deploy_log
    restart_log = (state / "restart.log").read_text() if (state / "restart.log").exists() else ""
    assert "restart lifeos-api" not in restart_log, restart_log


@pytest.mark.unit
def test_main_lock_is_released_on_an_early_exit_after_acquisition(tmp_path: Path):
    """Regression guard for the trap-based release (found on review: relying
    only on an explicit release call at the very end of main() is fragile —
    an early exit, like the branch guard here, happens well before that
    line). Checking out a non-main branch trips that guard AFTER the lock is
    acquired; the lock must still be free immediately afterward."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    repo, state, _code_epoch = _make_policy_repo(tmp_path)
    _git(repo, "checkout", "-qb", "some-feature")

    result = _run_main(repo, state, _venv_with_python(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)
    deploy_log = (repo / "logs" / "auto-deploy.log").read_text()
    assert "not main" in deploy_log, deploy_log

    # Matches _run_main()'s fake $HOME/.lifeos/sync.lock.
    lock_path = repo / "home" / ".lifeos" / "sync.lock"
    assert lock_path.exists()
    probe = subprocess.run(["flock", "-x", "-n", str(lock_path), "-c", "true"])
    assert probe.returncode == 0, "lock was left held after an early exit"


def _worker_busy_rc(tmp_path: Path, statuses: list[str]) -> str:
    """Run auto-deploy.sh's `worker_busy` against a store seeded with exactly
    `statuses`. Returns the shell rc line ("rc=0" busy, "rc=1" idle)."""
    workdir = tmp_path / f"work_{'_'.join(statuses) or 'empty'}"
    (workdir / "data").mkdir(parents=True)
    (workdir / "scripts").mkdir()
    (workdir / "scripts" / "auto-deploy.sh").write_text(AUTO_DEPLOY.read_text(), encoding="utf-8")
    db_path = workdir / "data" / "agent_sessions.db"
    store = SessionStore(db_path=db_path)
    for i, st in enumerate(statuses):
        store.create(task_id=f"t-{i}", session_id=f"s-{i}", status=st)
    env = dict(os.environ)
    env["LIFEOS_VENV"] = str(_venv_with_python(tmp_path))
    env["LIFEOS_AGENT_SESSIONS_DB"] = str(db_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        ["bash", "-c", 'source scripts/auto-deploy.sh && worker_busy; echo "rc=$?"'],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=30,
    )
    return result.stdout


@pytest.mark.unit
def test_worker_busy_excludes_yielded_sessions(tmp_path: Path):
    """#636: a yielded session must NOT block a restart.

    The worker's own recovery path skips yielded sessions ("Sleeping sessions
    are healthy — main loop will wake them"), so a restart does not harm them.
    Counting them meant one abandoned yielded session — 82 days old, on the
    real host — deferred the worker on every tick forever, logging a deferral
    each time and never updating. `list_non_terminal()` answers "which
    sessions must I look at?", which is a wider set than "which would a
    restart harm?".
    """
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    assert "rc=1" in _worker_busy_rc(tmp_path, [STATUS_YIELDED])


@pytest.mark.unit
def test_worker_busy_still_true_for_a_running_session(tmp_path: Path):
    """The narrowing must not become "never busy": genuinely-active work
    still defers."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    assert "rc=0" in _worker_busy_rc(tmp_path, [STATUS_RUNNING])


@pytest.mark.unit
def test_worker_busy_true_when_active_work_sits_alongside_a_yielded_session(tmp_path: Path):
    """The mixed case is the one a naive filter gets wrong: excluding yielded
    must not cause a claimed session in the same store to be overlooked."""
    if not AUTO_DEPLOY.exists():
        pytest.skip("scripts/auto-deploy.sh not present")
    assert "rc=0" in _worker_busy_rc(tmp_path, [STATUS_YIELDED, STATUS_CLAIMED])
