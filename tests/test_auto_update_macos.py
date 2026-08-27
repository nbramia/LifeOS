"""Tests for scripts/auto-update-macos.sh (#777) — the macOS analog of
scripts/auto-deploy.sh's opt-in redeploy timer.

Follows tests/test_deploy_drift.py's established pattern: `source` the real
script (its operational body is wrapped in `main()` and guarded so sourcing
only defines functions — never fetches, pulls, or touches a real launch
agent), then drive its decision helpers directly. `launchctl` and `curl` are
never real here — they're shadowed by bash functions defined after sourcing,
the same technique used to isolate `service_active_since_epoch` from a real
systemd in test_deploy_drift.py, just via a function override instead of a
PATH-stubbed binary (there's no columnar `launchctl list` format worth
emulating for these tests since every call site is a single named command).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTO_UPDATE_MACOS = REPO_ROOT / "scripts" / "auto-update-macos.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _stub_bin(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


def _make_repo_for_macos(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "auto-update-macos.sh").write_text(
        AUTO_UPDATE_MACOS.read_text(), encoding="utf-8"
    )
    (repo / "scripts" / "auto-update-macos.sh").chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    return repo


def _run_sourced(repo: Path, call: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source auto-update-macos.sh (defines functions only) then run `call`.
    cwd=repo so PROJECT_DIR-relative operations see the synthetic repo."""
    script = repo / "scripts" / "auto-update-macos.sh"
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'source "{script}" && {call}'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


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
    release the lock. This one-liner IS the process holding the lock."""
    return subprocess.Popen([
        sys.executable, "-c",
        f"import fcntl, time\n"
        f"f = open({str(lock_path)!r}, 'w')\n"
        f"fcntl.flock(f.fileno(), fcntl.LOCK_SH)\n"
        f"time.sleep({seconds})\n",
    ])


@pytest.mark.unit
def test_sourcing_does_not_run_main(tmp_path: Path):
    """Sourcing must only define functions — no fetch/pull/restart/exit."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, "echo did-not-exit")
    assert result.returncode == 0, result.stderr
    assert "did-not-exit" in result.stdout


@pytest.mark.unit
def test_auto_update_macos_syntax_and_sourced_functions_defined(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    syntax = subprocess.run(["bash", "-n", str(AUTO_UPDATE_MACOS)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo,
        "type -t newest_code_mtime env_file_mtime api_pid api_active_since_epoch "
        "env_stale mark_env_mtime_applied sync_in_progress_lock_acquire "
        "sync_in_progress_lock_release wait_for_pid_gone start_with_retry "
        "health_check_timeout wait_for_health restart_cycle main",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("function") == 14, result.stdout


# ---------------------------------------------------------------------------
# api_active_since_epoch — ground truth from the OS, no self-tracked marker
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_api_active_since_epoch_unknown_when_not_running(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo, 'api_pid() { echo ""; }; api_active_since_epoch; echo "rc=$?"'
    )
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_api_active_since_epoch_reflects_a_real_process_start_time(tmp_path: Path):
    """No self-tracked marker to fake — spawn a real process, point api_pid
    at it, and confirm the reported epoch is close to when it actually
    started (within a generous tolerance for ps/date parsing overhead)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    before = int(time.time())
    proc = subprocess.Popen(["sleep", "10"])
    try:
        result = _run_sourced(
            repo, f'api_pid() {{ echo {proc.pid}; }}; api_active_since_epoch'
        )
        assert result.returncode == 0, result.stderr
        reported = int(result.stdout.strip())
        after = int(time.time())
        assert before - 2 <= reported <= after + 2, (before, reported, after)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# env_stale / mark_env_mtime_applied — fixes a future-.env-mtime restart
# loop: comparing active_since against ENV_MTIME with wall-clock '<' breaks
# if ENV_MTIME is ever in the future (clock skew, `rsync -t`, a bad manual
# `touch`) — active_since stays "before" a future mtime forever.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_env_stale_bootstrap_true_when_no_applied_marker_yet(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'ENV_MTIME=200; env_stale 100; echo "rc=$?"')
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_env_stale_bootstrap_false_when_active_since_already_newer(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'ENV_MTIME=100; env_stale 200; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_mark_env_mtime_applied_then_env_stale_is_false_even_with_future_mtime(tmp_path: Path):
    """The actual bug: a future ENV_MTIME must restart exactly once, not on
    every tick forever."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    future = int(time.time()) + 3600
    result = _run_sourced(
        repo,
        f'ENV_MTIME={future}; mark_env_mtime_applied; '
        f'env_stale 999999999999; echo "rc=$?"',
    )
    assert "rc=1" in result.stdout, result.stdout
    applied_file = repo / "data" / "macos-env-mtime-applied"
    assert applied_file.read_text().strip() == str(future)


@pytest.mark.unit
def test_env_stale_true_when_env_mtime_changes_again_after_being_applied(tmp_path: Path):
    """active_since is realistically BEFORE both edits: env_stale now
    requires active_since < ENV_MTIME too (found on review), so an
    active_since after ENV_MTIME would correctly read as "already
    current" regardless of the marker."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo,
        'ENV_MTIME=100; mark_env_mtime_applied; '
        'ENV_MTIME=200; env_stale 50; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_env_stale_false_when_no_env_mtime_at_all(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'ENV_MTIME=""; env_stale 100; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# sync_in_progress_lock_acquire/_release — shared with scripts/auto-deploy.sh
# and scripts/run_all_syncs.py (#793); also gives this script mutual
# exclusion between two overlapping invocations of itself.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_lock_acquire_succeeds_when_no_sync_holds_it(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_lock_acquire_defers_while_a_sync_holds_the_shared_lock(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    (repo / "data").mkdir(exist_ok=True)
    lock_path = repo / "data" / "sync.lock"
    holder = _start_shared_lock_holder(lock_path, seconds=5)
    try:
        _wait_until_lock_held(lock_path)
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=0" in result.stdout, result.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.unit
def test_lock_acquire_defers_when_another_instance_of_this_script_holds_it(tmp_path: Path):
    """The mutual-exclusion use of the same lock: two overlapping runs of
    this script must not interleave launchctl unload/load calls."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    (repo / "data").mkdir(exist_ok=True)
    lock_path = repo / "data" / "sync.lock"
    holder = subprocess.Popen(
        ["bash", "-c",
         f'source "{repo / "scripts" / "auto-update-macos.sh"}" && '
         'sync_in_progress_lock_acquire; sleep 5'],
        cwd=repo,
    )
    try:
        _wait_until_lock_held(lock_path)
        result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"')
        assert "rc=0" in result.stdout, result.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.unit
def test_lock_release_allows_a_subsequent_acquire(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo,
        'sync_in_progress_lock_acquire; echo "first=$?"; '
        'sync_in_progress_lock_release; '
        'sync_in_progress_lock_acquire; echo "second=$?"',
    )
    assert "first=1" in result.stdout, result.stdout
    assert "second=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_lock_acquire_logs_distinct_error_when_the_lock_helper_fails(tmp_path: Path):
    """Not `flock`(1) here — see sync_in_progress_lock_acquire()'s comment
    for why that command isn't used on macOS at all. python3 is the
    helper whose unexpected failure must still defer (safe default) while
    logging something diagnosable, not the generic sync-busy message."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "python3", "exit 2\n")  # anything but 0 or 75
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, 'sync_in_progress_lock_acquire; echo "rc=$?"', env)
    assert "rc=0" in result.stdout, result.stdout  # still defers
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "lock acquisition failed unexpectedly" in log, log


@pytest.mark.unit
def test_lock_acquire_never_shells_out_to_flock(tmp_path: Path):
    """macOS doesn't ship the `flock`(1) command (util-linux, Linux-only) —
    using it here would make the lock silently always fail to acquire on
    the actual target platform. Confirm sync_in_progress_lock_acquire and
    sync_in_progress_lock_release never invoke it."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "flock", 'echo "flock was called: $*" >> "$PWD/flock-calls.log"; exit 1\n')
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(
        repo,
        'sync_in_progress_lock_acquire; echo "acquire=$?"; '
        'sync_in_progress_lock_release; echo "released"',
        env,
    )
    assert "acquire=1" in result.stdout, result.stdout
    assert not (repo / "flock-calls.log").exists(), "flock(1) must never be invoked on macOS"


@pytest.mark.unit
def test_mark_env_mtime_applied_logs_error_and_leaves_no_marker_on_write_failure(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'ENV_MTIME=100; mv() { return 1; }; mark_env_mtime_applied')
    assert result.returncode == 0, result.stderr
    assert not (repo / "data" / "macos-env-mtime-applied").exists()
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "could not durably record" in log, log


# ---------------------------------------------------------------------------
# wait_for_pid_gone — never start the next process before the old one is
# actually gone (poll the pid captured before unload, not `launchctl list`
# re-queried after — the bind-race #777 fixes).
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_wait_for_pid_gone_returns_immediately_for_empty_pid(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'wait_for_pid_gone "" 5; echo "rc=$?"')
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_wait_for_pid_gone_polls_until_process_actually_exits(tmp_path: Path):
    """`kill -0` reports a pid as alive until it's actually reaped — an
    unreaped zombie child would make this test pass for the wrong reason
    (looking permanently "alive" to `kill -0` even after it exits), so a
    background thread reaps it the instant it exits, same as any real
    process's true parent would."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    proc = subprocess.Popen(["sleep", "1"])
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    try:
        result = _run_sourced(repo, f'wait_for_pid_gone {proc.pid} 5; echo "rc=$?"')
        assert "rc=0" in result.stdout, result.stdout
    finally:
        if proc.poll() is None:
            proc.terminate()
        reaper.join(timeout=5)


@pytest.mark.unit
def test_wait_for_pid_gone_times_out_when_process_never_exits(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        result = _run_sourced(repo, f'wait_for_pid_gone {proc.pid} 1; echo "rc=$?"')
        assert "rc=1" in result.stdout, result.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# start_with_retry — one retry on a transient-looking failure, never kickstart
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_start_with_retry_succeeds_on_first_attempt(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'launchctl() { return 0; }; start_with_retry; echo "rc=$?"')
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_start_with_retry_retries_once_then_succeeds(tmp_path: Path):
    """A bare error on the first `load` is transient — the retry must
    actually happen (#777 acceptance criterion)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    counter = repo / "attempts"
    counter.write_text("0")
    result = _run_sourced(
        repo,
        f'launchctl() {{ n=$(cat "{counter}"); n=$((n+1)); echo "$n" > "{counter}"; '
        f'if [ "$n" -eq 1 ]; then echo "Input/output error" >&2; return 1; fi; return 0; }}; '
        'start_with_retry; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout
    assert counter.read_text().strip() == "2", "expected exactly one retry"


@pytest.mark.unit
def test_start_with_retry_fails_after_both_attempts_fail(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    counter = repo / "attempts"
    counter.write_text("0")
    result = _run_sourced(
        repo,
        f'launchctl() {{ n=$(cat "{counter}"); n=$((n+1)); echo "$n" > "{counter}"; return 1; }}; '
        'start_with_retry; echo "rc=$?"',
    )
    assert "rc=1" in result.stdout, result.stdout
    assert counter.read_text().strip() == "2", "must not retry more than once"


@pytest.mark.unit
def test_start_with_retry_never_uses_kickstart(tmp_path: Path):
    """kickstart has wedged the API service before — always a clean load.
    The header comment explains this using the exact phrase "launchctl
    kickstart", so check for a real invocation (unquoted, not preceded by a
    comment marker) rather than the bare substring."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    code_lines = [
        line for line in AUTO_UPDATE_MACOS.read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    assert not any("kickstart" in line for line in code_lines)


# ---------------------------------------------------------------------------
# health_check_timeout — scaled to on-disk vault size, not a fixed window
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_health_check_timeout_baseline_when_no_vault_configured(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, "health_check_timeout")
    assert result.stdout.strip() == "60"


@pytest.mark.unit
def test_health_check_timeout_scales_with_vault_file_count(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (repo / ".env").write_text(f"LIFEOS_VAULT_PATH={vault}\n")
    # `find` is overridden (not a real 1000-file tree) so this stays fast.
    result = _run_sourced(
        repo,
        'find() { for i in $(seq 1 1000); do echo "f$i"; done; }; health_check_timeout',
    )
    assert result.stdout.strip() == "65"  # 60 + 1000/200


@pytest.mark.unit
def test_health_check_timeout_capped_at_600(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (repo / ".env").write_text(f"LIFEOS_VAULT_PATH={vault}\n")
    result = _run_sourced(
        repo,
        'find() { for i in $(seq 1 500000); do echo "f$i"; done; }; health_check_timeout',
    )
    assert result.stdout.strip() == "600"


# ---------------------------------------------------------------------------
# wait_for_health
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_wait_for_health_returns_immediately_when_healthy(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'curl() { return 0; }; wait_for_health 5; echo "rc=$?"')
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_wait_for_health_polls_until_healthy():
    """A slow-starting server (larger dataset) must not false-fail — the
    poll has to actually retry, not check once and give up."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo_for_macos(Path(td))
        counter = repo / "curl_attempts"
        counter.write_text("0")
        result = _run_sourced(
            repo,
            f'curl() {{ n=$(cat "{counter}"); n=$((n+1)); echo "$n" > "{counter}"; '
            f'[ "$n" -ge 2 ] && return 0 || return 1; }}; wait_for_health 20; echo "rc=$?"',
        )
        assert "rc=0" in result.stdout, result.stdout
        assert int(counter.read_text().strip()) >= 2


@pytest.mark.unit
def test_wait_for_health_times_out_when_never_healthy(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'curl() { return 1; }; wait_for_health 0; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# restart_cycle — the full unload -> wait -> load -> health-poll sequence
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_restart_cycle_success_path(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo,
        'launchctl() { case "$1" in unload) return 0;; load) return 0;; esac; }; '
        'api_pid() { echo ""; }; '
        'curl() { return 0; }; '
        'restart_cycle; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_restart_cycle_captures_old_pid_before_unload(tmp_path: Path):
    """The exact bind-race fix: the pid to wait for is captured BEFORE
    unload runs, not re-derived from launchctl afterward (which can show
    empty the instant unload is called, well before the process is
    actually gone)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    seen = repo / "seen-pid"
    result = _run_sourced(
        repo,
        'api_pid() { echo 4242; }; '
        'launchctl() { case "$1" in unload) return 0;; load) return 0;; esac; }; '
        f'wait_for_pid_gone() {{ echo "$1" > "{seen}"; return 0; }}; '
        'curl() { return 0; }; '
        'restart_cycle; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout
    assert seen.read_text().strip() == "4242"


@pytest.mark.unit
def test_restart_cycle_fails_when_health_never_comes_up(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(
        repo,
        'health_check_timeout() { echo 0; }; '
        'launchctl() { return 0; }; '
        'api_pid() { echo ""; }; '
        'curl() { return 1; }; '
        'restart_cycle; echo "rc=$?"',
    )
    assert "rc=1" in result.stdout, result.stdout


@pytest.mark.unit
def test_restart_cycle_proceeds_even_if_pid_wait_times_out(tmp_path: Path):
    """A previous instance that never tears down within the poll window must
    not hang the whole cycle forever — the script logs a warning and tries
    the start anyway (better to try than to wait indefinitely)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    (repo / "logs").mkdir()
    result = _run_sourced(
        repo,
        'api_pid() { echo 4242; }; '
        'wait_for_pid_gone() { return 1; }; '
        'launchctl() { return 0; }; '
        'curl() { return 0; }; '
        'restart_cycle; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "did not tear down" in log


# ---------------------------------------------------------------------------
# main() — opt-in gate, guards, and the drift-detect/restart/retry/alert flow
# ---------------------------------------------------------------------------
def _make_policy_repo_macos(tmp_path: Path) -> tuple[Path, Path, int]:
    """A real git repo with a real 'origin' remote (main()'s fetch/pull is
    genuinely exercised) laid out like the canonical checkout."""
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
    (repo / "scripts").mkdir()
    (repo / "data").mkdir()
    (repo / "scripts" / "auto-update-macos.sh").write_text(
        AUTO_UPDATE_MACOS.read_text(), encoding="utf-8"
    )
    (repo / "scripts" / "auto-update-macos.sh").chmod(0o755)
    (repo / "api" / "a.py").write_text("code\n")
    # .env is gitignored here the same way it is in the real repo (it holds
    # secrets and is deliberately untracked) — editing it later must not trip
    # main()'s "tracked files modified" guard, only its drift check.
    (repo / ".gitignore").write_text("logs/\ndata/\nattempts\nnotify.log\n.env\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main")

    (repo / ".env").write_text(
        "LIFEOS_AUTODEPLOY_ENABLED=true\nLIFEOS_AUTODEPLOY_NOTIFY=never\n"
    )

    code_epoch = int(time.time()) - 3600
    os.utime(repo / "api" / "a.py", (code_epoch, code_epoch))
    return repo, origin, code_epoch


def _run_main_macos(
    repo: Path, active_since: int, extra_stubs: str = "", env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    """Run main() with api_active_since_epoch() overridden to a fixed,
    test-controlled value — the ground-truth replacement for the old
    self-tracked restart marker means tests can no longer fake "the service
    started at time X" by writing a marker file's mtime; overriding the
    function directly is the direct analog of test_deploy_drift.py's
    stubbed systemctl reporting a unit's ActiveEnterTimestamp. launchctl and
    curl are stubbed to a clean, always-successful restart by default so
    tests that aren't specifically about the restart mechanics don't have to
    care."""
    quiet_stubs = (
        f'api_active_since_epoch() {{ echo {active_since}; }}; '
        'launchctl() { case "$1" in unload) return 0;; load) return 0;; esac; }; '
        'api_pid() { echo ""; }; '
        'curl() { return 0; }; '
    )
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'source scripts/auto-update-macos.sh && {quiet_stubs}{extra_stubs} main'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.unit
def test_main_does_nothing_when_not_opted_in(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / ".env").write_text("LIFEOS_AUTODEPLOY_ENABLED=false\n")
    result = _run_main_macos(repo, active_since=code_epoch - 3600)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log_path = repo / "logs" / "auto-update-macos.log"
    assert not log_path.exists() or log_path.read_text() == "", "opted-out host must be completely unaffected"


@pytest.mark.unit
def test_main_skips_when_sync_lock_is_held(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    lock_path = repo / "data" / "sync.lock"
    holder = _start_shared_lock_holder(lock_path, seconds=5)
    try:
        _wait_until_lock_held(lock_path)
        result = _run_main_macos(repo, active_since=code_epoch - 3600)
        assert result.returncode == 0, (result.stdout, result.stderr)
        log = (repo / "logs" / "auto-update-macos.log").read_text()
        assert "sync" in log and "progress" in log
    finally:
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.unit
def test_main_skips_when_not_on_main_branch(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    _git(repo, "checkout", "-qb", "some-feature")
    result = _run_main_macos(repo, active_since=code_epoch - 3600)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "not main" in log


@pytest.mark.unit
def test_main_skips_when_working_tree_has_local_edits(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / "api" / "a.py").write_text("local edit\n")
    result = _run_main_macos(repo, active_since=code_epoch - 3600)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "tracked files modified" in log


@pytest.mark.unit
def test_main_skips_when_api_service_not_running(tmp_path: Path):
    """No ground truth available (service never started) — nothing to
    restart, and no false 'drift' report either."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    result = subprocess.run(
        ["bash", "-c",
         'source scripts/auto-update-macos.sh && '
         'api_active_since_epoch() { return 1; }; '
         'launchctl() { return 0; }; api_pid() { echo ""; }; curl() { return 0; }; main'],
        cwd=repo, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "not running" in log


@pytest.mark.unit
def test_main_lock_is_released_when_api_service_not_running(tmp_path: Path):
    """Regression guard for the trap-based release: this exact early exit
    (service not running) previously had NO release call at all before the
    lock was acquired — found and fixed on review. The lock must be free
    immediately afterward."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    result = subprocess.run(
        ["bash", "-c",
         'source scripts/auto-update-macos.sh && '
         'api_active_since_epoch() { return 1; }; '
         'launchctl() { return 0; }; api_pid() { echo ""; }; curl() { return 0; }; main'],
        cwd=repo, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    lock_path = repo / "data" / "sync.lock"
    assert lock_path.exists()
    probe = subprocess.run(["flock", "-x", "-n", str(lock_path), "-c", "true"])
    assert probe.returncode == 0, "lock was left held after an early exit"


@pytest.mark.unit
def test_main_no_restart_when_service_started_after_code_and_env(tmp_path: Path):
    """The service already reflects current code/.env — no first-run
    bootstrap ambiguity, since active_since is real ground truth, not a
    self-tracked marker that needs a first run to establish."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    active_since = code_epoch + 100
    # .env must predate active_since too — _make_policy_repo_macos writes it
    # at "now" (real wall-clock), well after any of these synthetic epochs.
    os.utime(repo / ".env", (active_since - 50, active_since - 50))
    result = _run_main_macos(repo, active_since=active_since)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log_path = repo / "logs" / "auto-update-macos.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "drift detected" not in log


@pytest.mark.unit
def test_main_restarts_on_first_run_when_code_pulled_is_newer_than_running_service(tmp_path: Path):
    """The exact bug found on review: a pull happening in THIS invocation
    must not be silently treated as 'already applied' just because it's the
    first time the script has ever run — the running process's real start
    time (well before this pull) proves it's still stale."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    result = _run_main_macos(repo, active_since=code_epoch - 3600)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "drift detected" in log
    assert "restart OK, health confirmed" in log


@pytest.mark.unit
def test_main_restarts_when_only_env_file_changed(tmp_path: Path):
    """#792's signal applies here too: a .env edit after the service's real
    start time must trigger a restart even with no new commit on main."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    active_since = code_epoch + 100  # started after the code last changed...
    env_mtime = active_since + 200   # ...but .env was edited after that.
    with open(repo / ".env", "a") as f:
        f.write("# bumped\n")
    os.utime(repo / ".env", (env_mtime, env_mtime))

    result = _run_main_macos(repo, active_since=active_since)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "drift detected" in log
    assert "restart OK, health confirmed" in log


@pytest.mark.unit
def test_main_no_restart_on_second_run_after_env_restart_even_with_future_mtime(tmp_path: Path):
    """The future-mtime acceptance criterion end-to-end: after restarting
    once for a (possibly future-dated) .env edit, the SAME edit must not
    trigger a second restart on the very next tick."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    active_since = code_epoch + 100
    future_env_mtime = int(time.time()) + 3600
    with open(repo / ".env", "a") as f:
        f.write("# bumped\n")
    os.utime(repo / ".env", (future_env_mtime, future_env_mtime))

    first = _run_main_macos(repo, active_since=active_since)
    assert first.returncode == 0, (first.stdout, first.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert log.count("drift detected") == 1

    # Second tick: still the same (still-future) ENV_MTIME, active_since
    # unchanged (as if the restart hadn't actually moved the real clock) —
    # must NOT restart again now that it's recorded as applied.
    second = _run_main_macos(repo, active_since=active_since)
    assert second.returncode == 0, (second.stdout, second.stderr)
    log_after = (repo / "logs" / "auto-update-macos.log").read_text()
    assert log_after.count("drift detected") == 1, log_after


@pytest.mark.unit
def test_main_retries_once_then_succeeds_when_first_restart_cycle_unhealthy(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    counter = repo / "attempts"
    counter.write_text("0")
    result = _run_main_macos(
        repo,
        active_since=code_epoch - 3600,
        extra_stubs=(
            f'restart_cycle() {{ n=$(cat "{counter}"); n=$((n+1)); echo "$n" > "{counter}"; '
            f'[ "$n" -ge 2 ] && return 0 || return 1; }}; '
        ),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert counter.read_text().strip() == "2"
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "retrying once" in log
    assert "retry restart OK" in log


@pytest.mark.unit
def test_main_alerts_and_exits_nonzero_when_both_restart_cycles_fail(tmp_path: Path):
    """Never restart indefinitely, never silently stay down — alert instead."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / ".env").write_text(
        "LIFEOS_AUTODEPLOY_ENABLED=true\nLIFEOS_AUTODEPLOY_NOTIFY=failure\n"
    )

    result = _run_main_macos(
        repo,
        active_since=code_epoch - 3600,
        extra_stubs=(
            'restart_cycle() { return 1; }; '
            'send_telegram() { echo "TELEGRAM:$1" >> notify.log; }; '
        ),
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "restart FAILED after retry" in log
    notify_log = repo / "notify.log"
    assert notify_log.exists(), "must alert a human when both cycles fail"
    assert "Restarted" in notify_log.read_text()


@pytest.mark.unit
def test_main_never_installs_itself_or_a_timer(tmp_path: Path):
    """This is only ever the operational script — nothing in it schedules
    itself (no crontab/launchctl-timer install of *this* script). The header
    comment mentions "crontab" in prose (pointing at where an operator adds
    one themselves), so check for an actual invocation, not the substring."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    text = AUTO_UPDATE_MACOS.read_text()
    assert "crontab -" not in text
    assert "StartInterval" not in text and "StartCalendarInterval" not in text
