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
        "type -t newest_code_mtime env_file_mtime last_restart_epoch mark_restarted "
        "sync_marker_in_progress api_pid wait_for_teardown start_with_retry "
        "health_check_timeout wait_for_health restart_cycle main",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("function") == 12, result.stdout


# ---------------------------------------------------------------------------
# wait_for_teardown — never start the next process before the old one is gone
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_wait_for_teardown_returns_immediately_when_already_gone(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'api_pid() { echo ""; }; wait_for_teardown 5; echo "rc=$?"')
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_wait_for_teardown_polls_until_pid_actually_gone(tmp_path: Path):
    """The previous instance takes ~1s to actually exit — api_pid must be
    polled repeatedly, not checked once (the exact collision #777 fixes)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    marker = repo / "still-alive"
    marker.write_text("1")
    result = _run_sourced(
        repo,
        f'api_pid() {{ [ -f "{marker}" ] && echo 1234 || echo ""; }}; '
        f'( sleep 1; rm -f "{marker}" ) & wait_for_teardown 5; echo "rc=$?"',
    )
    assert "rc=0" in result.stdout, result.stdout


@pytest.mark.unit
def test_wait_for_teardown_times_out_when_pid_never_goes_away(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    result = _run_sourced(repo, 'api_pid() { echo 1234; }; wait_for_teardown 1; echo "rc=$?"')
    assert "rc=1" in result.stdout, result.stdout


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
def test_restart_cycle_proceeds_even_if_teardown_wait_times_out(tmp_path: Path):
    """A previous instance that never tears down within the poll window must
    not hang the whole cycle forever — the script logs a warning and tries
    the start anyway (better to try than to wait indefinitely)."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo = _make_repo_for_macos(tmp_path)
    (repo / "logs").mkdir()
    result = _run_sourced(
        repo,
        'wait_for_teardown() { return 1; }; '
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


# Stubs launchctl/api_pid/curl to a clean, always-successful restart so tests
# that aren't specifically about the restart mechanics don't have to care.
_QUIET_RESTART_STUBS = (
    'launchctl() { case "$1" in unload) return 0;; load) return 0;; esac; }; '
    'api_pid() { echo ""; }; '
    'curl() { return 0; }; '
)


def _run_main_macos(repo: Path, extra_stubs: str = "", env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'source scripts/auto-update-macos.sh && {_QUIET_RESTART_STUBS}{extra_stubs} main'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.unit
def test_main_does_nothing_when_not_opted_in(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / ".env").write_text("LIFEOS_AUTODEPLOY_ENABLED=false\n")
    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    marker = repo / "data" / "macos-autoupdate-last-restart"
    assert not marker.exists(), "opted-out host must be completely unaffected"


@pytest.mark.unit
def test_main_skips_when_sync_marker_is_live(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / "data" / "sync_in_progress.pid").write_text(str(os.getpid()))
    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "sync in progress" in log


@pytest.mark.unit
def test_main_skips_when_not_on_main_branch(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    _git(repo, "checkout", "-qb", "some-feature")
    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "not main" in log


@pytest.mark.unit
def test_main_skips_when_working_tree_has_local_edits(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    (repo / "api" / "a.py").write_text("local edit\n")
    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "tracked files modified" in log


@pytest.mark.unit
def test_main_first_run_establishes_baseline_without_restarting(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    marker = repo / "data" / "macos-autoupdate-last-restart"
    assert marker.exists()
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "no restart baseline yet" in log
    assert "drift detected" not in log


@pytest.mark.unit
def test_main_no_restart_when_nothing_changed_since_baseline(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    first = _run_main_macos(repo)
    assert first.returncode == 0, (first.stdout, first.stderr)

    second = _run_main_macos(repo)
    assert second.returncode == 0, (second.stdout, second.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert log.count("drift detected") == 0


@pytest.mark.unit
def test_main_restarts_when_only_env_file_changed_after_baseline(tmp_path: Path):
    """#792's signal applies here too: a .env edit after the last restart
    must trigger a restart even with no new commit on main."""
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    baseline = _run_main_macos(repo)
    assert baseline.returncode == 0, (baseline.stdout, baseline.stderr)
    marker = repo / "data" / "macos-autoupdate-last-restart"
    restart_epoch = int(marker.stat().st_mtime)

    env_mtime = restart_epoch + 100
    with open(repo / ".env", "a") as f:
        f.write("# bumped\n")
    os.utime(repo / ".env", (env_mtime, env_mtime))

    result = _run_main_macos(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "drift detected" in log
    assert "restart OK, health confirmed" in log


@pytest.mark.unit
def test_main_retries_once_then_succeeds_when_first_restart_cycle_unhealthy(tmp_path: Path):
    if not AUTO_UPDATE_MACOS.exists():
        pytest.skip("scripts/auto-update-macos.sh not present")
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    baseline = _run_main_macos(repo)
    assert baseline.returncode == 0, (baseline.stdout, baseline.stderr)
    marker = repo / "data" / "macos-autoupdate-last-restart"
    restart_epoch = int(marker.stat().st_mtime)
    env_mtime = restart_epoch + 100
    with open(repo / ".env", "a") as f:
        f.write("# bumped\n")
    os.utime(repo / ".env", (env_mtime, env_mtime))

    counter = repo / "attempts"
    counter.write_text("0")
    result = _run_main_macos(
        repo,
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
    repo, _origin, _code_epoch = _make_policy_repo_macos(tmp_path)
    baseline = _run_main_macos(repo)
    assert baseline.returncode == 0, (baseline.stdout, baseline.stderr)
    marker = repo / "data" / "macos-autoupdate-last-restart"
    restart_epoch = int(marker.stat().st_mtime)
    env_mtime = restart_epoch + 100
    with open(repo / ".env", "a") as f:
        f.write("# bumped\n")
    os.utime(repo / ".env", (env_mtime, env_mtime))
    (repo / ".env").write_text(
        (repo / ".env").read_text().replace(
            "LIFEOS_AUTODEPLOY_NOTIFY=never", "LIFEOS_AUTODEPLOY_NOTIFY=failure"
        )
    )
    os.utime(repo / ".env", (env_mtime, env_mtime))

    result = _run_main_macos(
        repo,
        extra_stubs=(
            'restart_cycle() { return 1; }; '
            'send_telegram() { echo "TELEGRAM:$1" >> notify.log; }; '
        ),
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    log = (repo / "logs" / "auto-update-macos.log").read_text()
    assert "restart FAILED after retry" in log
    notify_log = (repo / "notify.log")
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
