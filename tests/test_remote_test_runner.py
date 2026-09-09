"""Synthetic transport proof for remote candidate-verifier dispatch."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.development_metrics import read_records

REPO = Path(__file__).resolve().parent.parent
BOUNDED_EXEC = REPO / "scripts" / "_bounded_exec.py"


def _synthetic_remote_checkout(
    tmp_path: Path, *, failing: bool = False, initial_branch: str | None = None,
) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "tests").mkdir()
    for name in (
        "remote-test.sh", "test.sh", "test-lanes.sh", "test_lane_registry.py",
        "test_lane_plugin.py", "verify_candidate.py", "verification_evidence.py",
        "candidate_snapshot.py", "development_metrics.py", "test_capacity.py",
        "test_instance.py", "_supervised_exec_bootstrap.py", "_candidate_owner_bootstrap.py",
        "_bounded_exec.py", "_remote_git_bundle.py", "_remote_exclude_list.py",
    ):
        shutil.copy2(REPO / "scripts" / name, checkout / "scripts" / name)
    (checkout / "requirements.txt").write_text("synthetic-dependency==1\n")
    (checkout / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    body = "assert False" if failing else "assert True"
    (checkout / "tests" / "test_remote_candidate.py").write_text(
        f"import pytest\n@pytest.mark.unit\ndef test_remote_candidate(): {body}\n"
    )
    init_args = ["git", "init", "-q"]
    if initial_branch is not None:
        init_args.append(f"--initial-branch={initial_branch}")
    subprocess.run(init_args, cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic Remote"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "remote@example.invalid"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "synthetic remote"], cwd=checkout, check=True)
    return checkout


def _fake_transport_bin(tmp_path: Path, *, ssh_body: str | None = None) -> Path:
    """A bounded, local synthetic shim standing in for a real remote host:
    rsync copies a tree locally, ssh runs the quoted remote command in a
    local bash. This proves the wrapper's own transfer/connect/execution and
    metrics logic; it does NOT prove behavior against a genuinely separate,
    networked remote host — that remains unverified by this or any test in
    this file (no such host is available in this environment)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "rsync").write_text(
        "#!/bin/bash\nset -eu\n"
        "source=${@: -2:1}; target=${@: -1}\n"
        "target=${target#fakehost:}\n"
        "if [ -d \"$source\" ]; then\n"
        "    mkdir -p \"$target\"\n"
        "    cp -a \"${source%/}\"/. \"$target\"/\n"
        "else\n"
        "    mkdir -p \"$(dirname \"$target\")\"\n"
        "    cp -a \"$source\" \"$target\"\n"
        "fi\n"
    )
    (fake_bin / "ssh").write_text(ssh_body or "#!/bin/bash\nset -eu\n[ \"$1\" = fakehost ]\nexec bash -c \"$2\"\n")
    for executable in (fake_bin / "rsync", fake_bin / "ssh"):
        executable.chmod(0o755)
    return fake_bin


def _synthetic_env(tmp_path: Path, fake_bin: Path, home: Path) -> dict:
    activate = home / ".venvs" / "lifeos" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(f'export PATH="{Path(sys.executable).parent}:$PATH"\n')
    return {
        **os.environ, "HOME": str(home), "TMPDIR": str(tmp_path),
        "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        "LIFEOS_REMOTE_HOST": "fakehost", "LIFEOS_REMOTE_TEST_DIR": str(tmp_path / "remote"),
        "LIFEOS_TEST_PARALLEL_WORKERS": "1",
    }


_HOOK_SENTINEL = "SOURCE_HOOK_SENTINEL_MUST_NOT_TRANSFER"


def _synthetic_main_repo(tmp_path: Path) -> Path:
    """A main checkout suitable for creating linked worktrees from: same
    script/test file set as _synthetic_remote_checkout, plus a gitignore
    pattern for the secret-sentinel proof and a source-identifying git
    identity + hook that must never appear in a transferred copy."""
    main = tmp_path / "main"
    (main / "scripts").mkdir(parents=True)
    (main / "tests").mkdir()
    for name in (
        "remote-test.sh", "test.sh", "test-lanes.sh", "test_lane_registry.py",
        "test_lane_plugin.py", "verify_candidate.py", "verification_evidence.py",
        "candidate_snapshot.py", "development_metrics.py", "test_capacity.py",
        "test_instance.py", "_supervised_exec_bootstrap.py", "_candidate_owner_bootstrap.py",
        "_bounded_exec.py", "_remote_git_bundle.py", "_remote_exclude_list.py",
    ):
        shutil.copy2(REPO / "scripts" / name, main / "scripts" / name)
    (main / "requirements.txt").write_text("synthetic-dependency==1\n")
    (main / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    (main / ".gitignore").write_text("secret-sentinel-*.txt\n")
    (main / "tests" / "test_remote_candidate.py").write_text(
        "import pytest\n@pytest.mark.unit\ndef test_remote_candidate(): assert True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    # Identifying source-host identity: must never appear on the transferred
    # side — proves no git config/credential export, not just no .git copy.
    subprocess.run(["git", "config", "user.name", "Source Host Identity"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "source-secret@host.example"], cwd=main, check=True)
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "synthetic remote"], cwd=main, check=True)
    # A worktree's .git file has no hooks of its own — hooks live only in
    # the shared main .git/hooks, exactly what must never transfer.
    hooks_dir = main / ".git" / "hooks"
    (hooks_dir / "pre-commit").write_text(f"#!/bin/sh\necho {_HOOK_SENTINEL}\n")
    (hooks_dir / "pre-commit").chmod(0o755)
    return main


def _dirty_linked_worktree(main: Path, wt_dir: Path, branch: str, sentinel_suffix: str) -> dict:
    """Create a linked worktree off ``main``, then dirty it: modify the
    tracked test file, add an untracked-but-unignored file, and add a
    gitignored secret-sentinel file that rsync's exclude list must catch."""
    subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(wt_dir)], cwd=main, check=True)
    assert (wt_dir / ".git").is_file(), "a linked worktree's .git must be a file, not a directory"

    dirty_content = f"import pytest\n@pytest.mark.unit\ndef test_remote_candidate(): assert True  # dirty-{sentinel_suffix}\n"
    (wt_dir / "tests" / "test_remote_candidate.py").write_text(dirty_content)
    untracked_content = f"untracked but not ignored - {sentinel_suffix}\n"
    (wt_dir / "tests" / f"untracked_note_{sentinel_suffix}.txt").write_text(untracked_content)
    secret_content = f"SECRET_SENTINEL_{sentinel_suffix}_MUST_NOT_TRANSFER\n"
    (wt_dir / f"secret-sentinel-{sentinel_suffix}.txt").write_text(secret_content)
    return {
        "dirty_content": dirty_content,
        "untracked_name": f"untracked_note_{sentinel_suffix}.txt",
        "untracked_content": untracked_content,
        "secret_name": f"secret-sentinel-{sentinel_suffix}.txt",
        "secret_marker": f"SECRET_SENTINEL_{sentinel_suffix}_MUST_NOT_TRANSFER",
    }


def _real_rsync_transport_bin(tmp_path: Path, name: str) -> Path:
    """Local loopback transport using the REAL system rsync — no fake rsync
    binary is placed on PATH, so rsync's actual --exclude/--exclude-from
    engine runs for real. The fake `ssh` dispatches on argument count: one
    trailing argument is a pre-built shell command STRING (remote-test.sh's
    own direct ssh calls all pass exactly this), while multiple trailing
    arguments are executed directly as argv (how real rsync's own internal
    `ssh host rsync --server ...` invocation looks). This is still a local
    loopback standing in for a genuinely separate host — see
    _fake_transport_bin's docstring; the same limitation applies here."""
    fake_bin = tmp_path / name
    fake_bin.mkdir()
    (fake_bin / "ssh").write_text(
        "#!/bin/bash\nset -eu\n"
        "[ \"$1\" = fakehost ]\nshift\n"
        "if [ \"$#\" -eq 1 ]; then\n"
        "    exec bash -c \"$1\"\n"
        "else\n"
        "    exec \"$@\"\n"
        "fi\n"
    )
    (fake_bin / "ssh").chmod(0o755)
    return fake_bin


def _forbidden_timeout_bin(tmp_path: Path) -> Path:
    """A 'timeout' binary that always errors if invoked — placed first on
    PATH, this makes the test fail immediately if remote-test.sh ever shells
    out to the external GNU coreutils `timeout`, which is not guaranteed
    present on macOS (this wrapper's own documented target platform)."""
    trap_bin = tmp_path / "trap-bin"
    trap_bin.mkdir()
    forbidden = trap_bin / "timeout"
    forbidden.write_text(
        "#!/bin/bash\necho 'FORBIDDEN: remote-test.sh must not call external timeout(1)' >&2\nexit 127\n"
    )
    forbidden.chmod(0o755)
    return trap_bin


@pytest.mark.unit
def test_remote_candidate_transfer_executes_remote_snapshot_verifier(tmp_path):
    """The transfer script reaches a real candidate body, not a local receipt."""
    checkout = _synthetic_remote_checkout(tmp_path)
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[remote-test] DONE rc=0 (tests passed)" in result.stdout
    assert list((home / ".cache" / "lifeos" / "verification-evidence").glob("*.json"))


@pytest.mark.unit
def test_remote_transfer_handles_source_branch_matching_remote_default(tmp_path):
    """The source branch may match a fresh remote repository's default branch.

    A fresh repository has that branch checked out but unborn, so materializing
    a bundle must not fetch directly into it before checkout.
    """
    checkout = _synthetic_remote_checkout(tmp_path, initial_branch="master")
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    git_config = tmp_path / "gitconfig"
    git_config.write_text("[init]\n\tdefaultBranch = master\n")
    environment = _synthetic_env(tmp_path, fake_bin, home)
    environment.update({"GIT_CONFIG_GLOBAL": str(git_config), "GIT_CONFIG_NOSYSTEM": "1"})

    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=environment,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[remote-test] DONE rc=0 (tests passed)" in result.stdout


@pytest.mark.unit
def test_bounded_metrics_execution_preserves_fast_output_exits_and_deadlines():
    """The remote helper promptly captures success and preserves failure and timeout exits."""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(BOUNDED_EXEC), "cat"], input="synthetic stdin\n",
        text=True, capture_output=True, timeout=5,
    )
    fast_elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert result.stdout == "synthetic stdin\n"
    assert fast_elapsed < 1, f"fast metrics capture took {fast_elapsed:.3f}s"

    failed = subprocess.run(
        [sys.executable, str(BOUNDED_EXEC), "bash", "-c", "exit 7"],
        text=True, capture_output=True, timeout=5,
    )
    assert failed.returncode == 7

    signalled = subprocess.run(
        [sys.executable, str(BOUNDED_EXEC), "bash", "-c", "kill -TERM $$"],
        text=True, capture_output=True, timeout=5,
    )
    assert signalled.returncode == 143

    started = time.monotonic()
    hung = subprocess.run(
        [sys.executable, str(BOUNDED_EXEC), "bash", "-c", "sleep 30"],
        text=True, capture_output=True, timeout=5,
    )
    hung_elapsed = time.monotonic() - started
    assert hung.returncode == 124
    assert 2.5 <= hung_elapsed < 4.5, f"hung command took {hung_elapsed:.3f}s"


@pytest.mark.unit
def test_bounded_metrics_execution_reaps_pipe_holding_descendant():
    """A successful shell leader cannot leave its captured stdout open past the deadline."""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(BOUNDED_EXEC), "bash", "-c", "sleep 4 & exit 0"],
        text=True, capture_output=True, timeout=5,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert elapsed < 1, f"exited leader left output capture open for {elapsed:.3f}s"


@pytest.mark.unit
def test_bounded_metrics_execution_forwards_interrupt_as_cancelled_exit():
    """Interrupting the supervisor terminates its child process group."""
    proc = subprocess.Popen(
        [sys.executable, str(BOUNDED_EXEC), "bash", "-c", "sleep 30"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGINT)
        assert proc.wait(timeout=5) == 130
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


@pytest.mark.unit
def test_remote_metrics_record_connect_transfer_execution_with_shared_format(tmp_path):
    """The remote runner records shared connect, transfer, and execution
    metrics. This test exercises the bounded local shim."""
    checkout = _synthetic_remote_checkout(tmp_path)
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip()
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    records = read_records(home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl")
    by_phase = {r.phase: r for r in records}
    # The wrapper's own local connect/transfer/execution envelope timings...
    local_phases = {"remote-connect", "remote-transfer", "remote-execution"}
    for phase in local_phases:
        record = by_phase[phase]
        assert record.result == "success", phase
        assert record.exit_status is None, phase
        assert record.elapsed_seconds is not None and record.elapsed_seconds >= 0, phase
        assert record.suite == "candidate", phase
        # Opaque identity only: never the raw checkout path or branch text —
        # this is what distinguishes an opaque ID from REMOTE_DIR, which is
        # allowed (and needs) to be branch-readable.
        assert str(checkout) not in record.candidate_id
        assert branch not in record.candidate_id
        assert record.candidate_id.startswith("remote-")
    # remote-connect is an SSH handshake, never a real capacity-queue wait —
    # must never be recorded with the "waiting" phase_kind that would
    # conflate it with verify_candidate.py's own real queue measurement.
    assert by_phase["remote-connect"].phase_kind == "transfer"

    # ...are distinct from the REMOTE host's own real queue/execution
    # records, correlated back and imported verbatim (the synthetic remote's
    # verify_candidate.py genuinely recorded these against its own snapshot
    # identity, not this wrapper's candidate_id).
    assert by_phase["verification-queue"].phase_kind == "waiting"
    assert by_phase["verification-execution"].phase_kind == "execution"
    assert by_phase["verification-queue"].candidate_id != by_phase["remote-execution"].candidate_id

    # One shared run_id groups every phase — local envelope and the
    # correlated real remote records alike — together as one dispatch.
    assert len({r.run_id for r in records}) == 1


@pytest.mark.unit
def test_remote_execution_failure_is_recorded_distinctly_from_transfer(tmp_path):
    """A remote test failure must be a distinguishable, real metric — not
    silently dropped — while the (successful) connect/transfer phases before
    it are still recorded as their own real outcome, not blended together."""
    checkout = _synthetic_remote_checkout(tmp_path, failing=True)
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode != 0
    assert "tests failed" in result.stdout

    records = read_records(home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl")
    by_phase = {r.phase: r for r in records}
    assert by_phase["remote-connect"].result == "success"
    assert by_phase["remote-transfer"].result == "success"
    assert by_phase["remote-execution"].result == "failure"
    assert by_phase["remote-execution"].exit_status == result.returncode
    assert by_phase["remote-execution"].exit_status != 0


@pytest.mark.unit
def test_remote_interrupt_during_execution_records_cancelled_and_still_prints_done(tmp_path):
    """An interrupt during remote execution records cancellation while
    preserving the script's DONE-marker contract."""
    checkout = _synthetic_remote_checkout(tmp_path)
    # Only the remote *execution* leg (running test.sh) hangs; mkdir -p still
    # returns immediately, so the interrupt lands specifically inside the
    # "remote-execution" phase rather than an earlier one.
    fake_bin = _fake_transport_bin(
        tmp_path,
        ssh_body=(
            "#!/bin/bash\nset -eu\n[ \"$1\" = fakehost ]\n"
            "case \"$2\" in\n"
            "  *test.sh*) sleep 30 ;;\n"
            "  *) exec bash -c \"$2\" ;;\n"
            "esac\n"
        ),
    )
    home = tmp_path / "home"
    proc = subprocess.Popen(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_synthetic_env(tmp_path, fake_bin, home),
        start_new_session=True,  # own process group, so SIGINT below reaches
                                 # every descendant exactly as a real Ctrl-C
                                 # in a terminal would, not just this PID.
    )
    try:
        deadline = time.time() + 30
        saw_running_line = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "running ./scripts/test.sh" in line:
                saw_running_line = True
                break
        assert saw_running_line, "never reached the remote-execution phase"
        time.sleep(0.3)  # let the SECONDS=0/METRICS_ACTIVE_PHASE assignment land
        os.killpg(proc.pid, signal.SIGINT)
        remaining_output = proc.stdout.read()
        returncode = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    assert returncode == 130
    assert "[remote-test] DONE rc=130 (interrupted)" in remaining_output

    records = read_records(home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl")
    by_phase = {r.phase: r for r in records}
    assert by_phase["remote-connect"].result == "success"
    assert by_phase["remote-transfer"].result == "success"
    assert by_phase["remote-execution"].result == "cancelled"


@pytest.mark.unit
def test_remote_instrumentation_never_shells_out_to_external_timeout(tmp_path):
    """Remote instrumentation uses the bash-only bounded-execution helper,
    keeping metrics collection portable to macOS; a PATH timeout trap verifies
    no external timeout command is used."""
    checkout = _synthetic_remote_checkout(tmp_path)
    fake_bin = _fake_transport_bin(tmp_path)
    trap_bin = _forbidden_timeout_bin(tmp_path)
    home = tmp_path / "home"
    env = _synthetic_env(tmp_path, fake_bin, home)
    env["PATH"] = f"{trap_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORBIDDEN" not in result.stdout
    assert "FORBIDDEN" not in result.stderr
    # The run still genuinely completed real metrics work with `timeout`
    # unavailable — this isn't just "the script didn't crash," the actual
    # instrumented phases (including remote correlation) still worked.
    records = read_records(home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl")
    assert {r.phase for r in records} >= {"remote-connect", "remote-transfer", "remote-execution"}


@pytest.mark.unit
def test_remote_timing_is_a_deterministic_fake_clock_not_wall_time(tmp_path):
    """Elapsed time uses supplied monotonic readings, not wall-clock
    arithmetic; a fixed fake clock makes all recorded elapsed values 0.0."""
    checkout = _synthetic_remote_checkout(tmp_path)
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    env = _synthetic_env(tmp_path, fake_bin, home)
    env["LIFEOS_DEV_METRICS_FAKE_MONOTONIC"] = "1000.0"

    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    records = read_records(home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl")
    by_phase = {r.phase: r for r in records}
    for phase in ("remote-connect", "remote-transfer", "remote-execution"):
        assert by_phase[phase].elapsed_seconds == pytest.approx(0.0), phase


@pytest.mark.unit
def test_remote_candidate_id_never_leaks_the_branch_name(tmp_path):
    """Exported candidate IDs never contain the branch name, including a
    sanitized form."""
    checkout = _synthetic_remote_checkout(tmp_path)
    identifying_branch = "nathan-fix-sensitive-billing-migration"
    subprocess.run(["git", "checkout", "-qb", identifying_branch], cwd=checkout, check=True)
    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    metrics_path = home / ".cache" / "lifeos" / "remote-test-metrics" / "metrics.jsonl"
    raw_text = metrics_path.read_text()
    assert identifying_branch not in raw_text
    assert "nathan" not in raw_text
    assert "billing" not in raw_text
    for record in read_records(metrics_path):
        assert identifying_branch not in record.candidate_id


def _tree_contains_string(root: Path, needle: str) -> bool:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle in path.read_text(errors="ignore"):
                return True
        except OSError:
            continue
    return False


def _find_remote_dir(remote_base: Path, branch: str) -> Path:
    matches = list(remote_base.glob(f"{branch}-*"))
    assert len(matches) == 1, f"expected exactly one remote dir for branch {branch!r}, got {matches}"
    return matches[0]


@pytest.mark.unit
def test_linked_worktree_transfer_materializes_independent_git_after_source_removed(tmp_path):
    """A linked worktree's .git is a FILE pointing at the main checkout's
    administrative directory (.git/worktrees/<name>), which does not exist
    on a genuinely separate remote host. This exercises the real
    rsync exclude engine (not the cp -a shim _fake_transport_bin uses) from
    two linked worktrees of the same main repo, then deletes the main
    checkout entirely and re-verifies both transferred copies — proving
    each remote git identity is self-contained and has zero runtime
    dependency on the (now-gone) source administrative path. Still a local
    loopback transport, not a genuinely separate networked/macOS host — see
    _real_rsync_transport_bin's docstring.
    """
    main = _synthetic_main_repo(tmp_path)
    wt1_dir = tmp_path / "wt1"
    wt2_dir = tmp_path / "wt2"
    wt1 = _dirty_linked_worktree(main, wt1_dir, "worktree-branch-one", "ALPHA")
    wt2 = _dirty_linked_worktree(main, wt2_dir, "worktree-branch-two", "BETA")

    remote_base = tmp_path / "remote"

    def _run_and_verify(wt_dir: Path, branch: str, expectations: dict) -> Path:
        fake_bin = _real_rsync_transport_bin(tmp_path, f"real-rsync-bin-{branch}")
        home = tmp_path / f"home-{branch}"
        env = _synthetic_env(tmp_path, fake_bin, home)
        env["LIFEOS_REMOTE_TEST_DIR"] = str(remote_base)
        result = subprocess.run(
            ["bash", "scripts/remote-test.sh", "candidate"], cwd=wt_dir,
            text=True, capture_output=True, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[remote-test] DONE rc=0 (tests passed)" in result.stdout

        remote_dir = _find_remote_dir(remote_base, branch)
        assert (remote_dir / ".git").is_dir(), "remote .git must be a real directory, not a worktree file"

        branch_out = subprocess.run(
            ["git", "-C", str(remote_dir), "branch", "--show-current"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch_out == branch

        status = subprocess.run(
            ["git", "-C", str(remote_dir), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "tests/test_remote_candidate.py" in status  # dirty tracked edit present
        assert expectations["untracked_name"] in status    # untracked-unignored file present
        assert expectations["secret_name"] not in status   # secret sentinel excluded

        assert not (remote_dir / expectations["secret_name"]).exists()
        assert (remote_dir / "tests" / "test_remote_candidate.py").read_text() == expectations["dirty_content"]
        assert (remote_dir / "tests" / expectations["untracked_name"]).read_text() == expectations["untracked_content"]

        assert not _tree_contains_string(remote_dir, expectations["secret_marker"])
        assert not _tree_contains_string(remote_dir, _HOOK_SENTINEL)

        git_name = subprocess.run(
            ["git", "-C", str(remote_dir), "config", "user.name"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_email = subprocess.run(
            ["git", "-C", str(remote_dir), "config", "user.email"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert git_name == "LifeOS Remote Test"
        assert git_email == "remote-test@lifeos.invalid"
        assert not _tree_contains_string(remote_dir, "Source Host Identity")
        assert not _tree_contains_string(remote_dir, "source-secret@host.example")

        return remote_dir

    remote1 = _run_and_verify(wt1_dir, "worktree-branch-one", wt1)
    remote2 = _run_and_verify(wt2_dir, "worktree-branch-two", wt2)

    # Two linked worktrees of the same main repo use distinct, non-interfering
    # remote directories.
    assert remote1 != remote2
    assert (remote1 / "tests" / wt1["untracked_name"]).exists()
    assert not (remote1 / "tests" / wt2["untracked_name"]).exists()
    assert (remote2 / "tests" / wt2["untracked_name"]).exists()
    assert not (remote2 / "tests" / wt1["untracked_name"]).exists()

    # Remove the source administrative directory and confirm each transferred
    # copy remains a functional independent Git repository.
    shutil.rmtree(main)
    for remote_dir, branch in ((remote1, "worktree-branch-one"), (remote2, "worktree-branch-two")):
        head = subprocess.run(
            ["git", "-C", str(remote_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert head.returncode == 0, head.stderr
        status_after = subprocess.run(
            ["git", "-C", str(remote_dir), "status", "--porcelain"],
            capture_output=True, text=True,
        )
        assert status_after.returncode == 0, status_after.stderr


@pytest.mark.unit
def test_detached_head_transfer_preserves_origin_main_base(tmp_path):
    """_remote_git_bundle.py separates fields with ASCII Unit Separator
    (0x1F), which git forbids inside any ref name, so bash's `IFS=$'\\t'
    read` (which would otherwise collapse a run of consecutive tabs exactly
    like it collapses spaces) can never misparse a detached HEAD's empty
    branch field into eating the next field; this proves the real shape
    survives correctly: a detached HEAD with an origin/main base one
    commit behind it, both distinct and both present after transfer.

    Uses the real-rsync transport, not _fake_transport_bin's cp -a shim:
    that shim ignores every --exclude flag and blindly copies the whole
    source tree, including .git — which would silently overwrite the
    freshly bundle-materialized remote .git with the *source's* real one,
    making every assertion below trivially true regardless of whether
    git materialization actually works (both sides would just be looking
    at the same real .git). Real rsync actually honors --exclude='.git',
    so the remote .git checked below can only have come from the bundle."""
    checkout = _synthetic_remote_checkout(tmp_path)
    (checkout / "tests" / "second.py").write_text(
        "import pytest\n@pytest.mark.unit\ndef test_second(): assert True\n"
    )
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "second commit"], cwd=checkout, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip()
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip()
    assert base_sha != head_sha
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_sha], cwd=checkout, check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", head_sha], cwd=checkout, check=True)
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip()
    assert branch_check == "", "setup must actually be detached, or this test proves nothing"

    fake_bin = _real_rsync_transport_bin(tmp_path, "real-rsync-bin-detached-head")
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    remote_dir = _find_remote_dir(tmp_path / "remote", "detached")
    remote_git_name = subprocess.run(
        ["git", "-C", str(remote_dir), "config", "user.name"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_git_name == "LifeOS Remote Test", (
        "remote .git identity must be the throwaway one from bundle "
        "materialization, not 'Synthetic Remote' (the source checkout's "
        "own identity) — a value equal to the source's would mean --exclude "
        "'.git' wasn't honored and the source's real .git got copied over "
        "the freshly materialized one, invalidating every check below"
    )
    remote_branch = subprocess.run(
        ["git", "-C", str(remote_dir), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_branch == "", "remote must also be detached, not on a wrongly-named branch"

    remote_head = subprocess.run(
        ["git", "-C", str(remote_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_head == head_sha

    remote_base = subprocess.run(
        ["git", "-C", str(remote_dir), "merge-base", "HEAD", "refs/remotes/origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_base == base_sha, "origin/main base must survive transfer intact and distinct from HEAD"


@pytest.mark.unit
def test_bundle_metadata_field_parsing_preserves_empty_detached_branch_field():
    """Isolates the exact parsing bug from the end-to-end scenario above: a
    detached HEAD with an origin/main base produces a middle (branch) field
    that is genuinely empty, and git's own checkout ambiguity handling can
    coincidentally self-correct the end-to-end symptom in some repo shapes
    (a 40-hex-char branch name is a real commit id too, and `git checkout`
    on an unborn default branch can resolve it as the object instead of the
    ref) — masking the bug there. This test instead reproduces
    remote-test.sh's exact `IFS=$'\\x1f' read` construct directly against
    real `_remote_git_bundle.py` output, with no git ref resolution in the
    way to mask a regression back to a whitespace separator."""
    tmp_path_str = subprocess.run(
        ["mktemp", "-d"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    tmp_path = Path(tmp_path_str)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
        (repo / "b.txt").write_text("b\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=repo, text=True, capture_output=True, check=True,
        ).stdout.strip()
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_sha], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "--detach", head_sha], cwd=repo, check=True)

        bundle_out = tmp_path / "out.bundle"
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "_remote_git_bundle.py"),
             "--repo", str(repo), "--bundle-out", str(bundle_out)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        bundle_info = result.stdout.rstrip("\n")

        parsed = subprocess.run(
            ["bash", "-c", 'IFS=$\'\\x1f\' read -r HEAD_SHA REAL_BRANCH ORIGIN_MAIN_SHA <<< "$1"; '
                           'printf "%s\\n%s\\n%s\\n" "$HEAD_SHA" "$REAL_BRANCH" "$ORIGIN_MAIN_SHA"',
             "_", bundle_info],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        parsed_head, parsed_branch, parsed_origin_main = parsed[0], parsed[1], parsed[2] if len(parsed) > 2 else ""

        assert parsed_head == head_sha
        assert parsed_branch == "", (
            f"detached HEAD's branch field must parse as empty, got {parsed_branch!r} — "
            "a whitespace field separator would collapse it into the next field instead"
        )
        assert parsed_origin_main == base_sha, (
            f"origin/main base must survive parsing, got {parsed_origin_main!r} — "
            "a whitespace field separator would lose it entirely once the empty "
            "branch field collapses into it"
        )
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.unit
def test_remote_transfer_excludes_gitignored_filename_containing_glob_characters(tmp_path):
    """Root-review-found defect: rsync's --exclude-from treats each line as
    a glob pattern, not a literal path. The prior exclude-list construction
    (plain `git ls-files` piped straight into the exclude file) left an
    ignored filename containing rsync glob metacharacters — e.g.
    "synthetic[1].private" — unescaped: rsync reads "[1]" there as a
    character class matching the single character "1", not the literal
    substring "[1]", so the intended exclude silently matched nothing and
    the real ignored file was transferred anyway. This is deliberately a
    SEPARATE test from the plain-filename secret-sentinel proof elsewhere
    in this file — a plain filename can pass with either the old or the
    fixed exclude-list logic, so it cannot demonstrate this specific
    defect is fixed. Exercises the real rsync exclude engine end to end
    through remote-test.sh itself, not a synthetic escaping check in
    isolation."""
    checkout = _synthetic_remote_checkout(tmp_path)
    glob_secret_name = "synthetic[1].private"
    (checkout / ".gitignore").write_text("synthetic\\[1\\].private\n")
    (checkout / glob_secret_name).write_text("SECRET_GLOB_FILENAME_MUST_NOT_TRANSFER\n")
    ignored = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout
    assert f"!! {glob_secret_name}" in ignored, "setup must actually gitignore the bracketed filename"

    fake_bin = _real_rsync_transport_bin(tmp_path, "real-rsync-bin-glob-exclude")
    home = tmp_path / "home"
    env = _synthetic_env(tmp_path, fake_bin, home)
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip() or "detached"
    remote_dir = _find_remote_dir(tmp_path / "remote", branch)
    assert not (remote_dir / glob_secret_name).exists(), (
        f"gitignored filename {glob_secret_name!r} (containing rsync glob "
        "metacharacters) was not excluded — it transferred anyway"
    )
    assert not _tree_contains_string(remote_dir, "SECRET_GLOB_FILENAME_MUST_NOT_TRANSFER")


@pytest.mark.unit
def test_remote_exclude_list_escapes_and_emits_nul_delimited_patterns(tmp_path):
    """Root-review-found defects in _remote_exclude_list.py itself, isolated
    from the git/rsync end-to-end path above:

    1. rsync (per rsync(1) WILDCARD MATCHING RULES) does a plain, unescaped
       *string* match for any pattern containing none of `*`, `?`, `[` —
       backslash has no special meaning in that mode. An early fix
       unconditionally doubled every backslash, which broke the common
       case of a literal backslash in a filename that has no wildcard
       character in it at all: doubling made the pattern require TWO
       backslashes to match a file that only has one.
    2. A filename containing a literal newline can't be represented as one
       *line* in --exclude-from's default line-oriented pattern-file
       format — an early fix routed such entries to a second, separate
       output for use as individual --exclude=PATTERN arguments, but
       root proved the simpler fix: NUL-delimited output paired with
       rsync's own --from0 flag keeps a newline-containing pattern intact
       as one entry, with a single output stream and no argv-size concern.
    """
    script = REPO / "scripts" / "_remote_exclude_list.py"
    names = [
        "synthetic[1].private",       # wildcard char -> full escape mode
        "synthetic\\name.private",    # bare backslash, no wildcard -> untouched
        "synthetic\nname.private",    # embedded newline -> stays one NUL-delimited entry
        "synthetic*.private",         # wildcard char -> full escape mode
        "plain.txt",                  # nothing special -> untouched
    ]
    stdin_data = b"\0".join(n.encode() for n in names) + b"\0"
    result = subprocess.run(
        [sys.executable, str(script)], input=stdin_data, capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    entries = [e.decode() for e in result.stdout.split(b"\0") if e]
    assert "/synthetic\\[1].private" in entries
    assert "/synthetic\\name.private" in entries, (
        "a bare backslash with no wildcard character present must be left "
        "alone — rsync does a plain string match here, so doubling it "
        "would require two backslashes to match a file with only one"
    )
    assert "/synthetic\nname.private" in entries, (
        "the embedded newline must survive as part of one NUL-delimited "
        "entry, not get split by it"
    )
    assert "/synthetic\\*.private" in entries
    assert "/plain.txt" in entries
    assert len(entries) == len(names)


@pytest.mark.unit
def test_remote_transfer_excludes_gitignored_filenames_with_backslash_and_embedded_newline(tmp_path):
    """End-to-end companion to the unit test above: a broad gitignore glob
    (`synthetic*`) makes git report both a bare-backslash filename and a
    genuinely newline-containing filename as ignored — sidestepping
    whether either is representable as a literal .gitignore pattern line,
    which is a separate question from what this test actually checks: that
    remote-test.sh's real rsync invocation, using the NUL-delimited
    exclude file plus --from0, actually excludes both from a real
    transfer."""
    checkout = _synthetic_remote_checkout(tmp_path)
    (checkout / ".gitignore").write_text("synthetic*\n")
    backslash_name = "synthetic\\backslash.private"
    newline_name = "synthetic\nnewline.private"
    (checkout / backslash_name).write_text("SECRET_BACKSLASH_MUST_NOT_TRANSFER\n")
    (checkout / newline_name).write_bytes(b"SECRET_NEWLINE_MUST_NOT_TRANSFER\n")
    ignored = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=checkout, capture_output=True, check=True,
    ).stdout
    ignored_names = {e.decode() for e in ignored.split(b"\0") if e}
    assert {backslash_name, newline_name} <= ignored_names, "setup must actually gitignore both filenames"

    fake_bin = _real_rsync_transport_bin(tmp_path, "real-rsync-bin-edge-exclude")
    home = tmp_path / "home"
    env = _synthetic_env(tmp_path, fake_bin, home)
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip() or "detached"
    remote_dir = _find_remote_dir(tmp_path / "remote", branch)
    assert not (remote_dir / backslash_name).exists(), (
        f"gitignored filename {backslash_name!r} (bare backslash, no wildcard) "
        "was not excluded — it transferred anyway"
    )
    assert not (remote_dir / newline_name).exists(), (
        f"gitignored filename {newline_name!r} (embedded newline) was not "
        "excluded — it transferred anyway"
    )
    assert not _tree_contains_string(remote_dir, "SECRET_BACKSLASH_MUST_NOT_TRANSFER")
    assert not _tree_contains_string(remote_dir, "SECRET_NEWLINE_MUST_NOT_TRANSFER")


@pytest.mark.unit
def test_repeated_transfer_deletes_files_removed_from_source(tmp_path):
    """--delete must still remove a file from the remote once it's
    genuinely deleted from the source — proving --delete's exact-mirror
    guarantee survived switching the exclude file to NUL-delimited +
    --from0. (Not tested here: a file that becomes newly gitignored
    without being deleted. Plain --delete does not remove an excluded
    file already present on the receiver — that needs --delete-excluded,
    which this wrapper does not pass — so that case is a different
    scenario, not covered by this test.) Real rsync, not the cp -a shim,
    since --delete's actual scanning/pruning behavior is exactly what a
    blind copy can't exercise."""
    checkout = _synthetic_remote_checkout(tmp_path)
    extra_file = checkout / "tests" / "will_be_removed.py"
    extra_file.write_text("import pytest\n@pytest.mark.unit\ndef test_extra(): assert True\n")

    fake_bin = _real_rsync_transport_bin(tmp_path, "real-rsync-bin-repeat")
    home = tmp_path / "home"
    env = _synthetic_env(tmp_path, fake_bin, home)

    result1 = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result1.returncode == 0, result1.stdout + result1.stderr
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=checkout, text=True, capture_output=True, check=True,
    ).stdout.strip() or "detached"
    remote_dir = _find_remote_dir(tmp_path / "remote", branch)
    assert (remote_dir / "tests" / "will_be_removed.py").exists(), "first transfer must include the extra file"

    extra_file.unlink()
    result2 = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert not (remote_dir / "tests" / "will_be_removed.py").exists(), (
        "a file removed from the source must be deleted from the remote "
        "on the next transfer — --delete's exact-mirror guarantee"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "setup,expected_message",
    [
        ("no_git", "could not compute gitignore excludes"),
        ("broken_exclude_builder", "could not compute gitignore excludes"),
        ("no_commits", "could not prepare a self-contained git bundle"),
    ],
    ids=["git_ls_files_failure", "exclude_builder_failure", "bundle_prep_failure"],
)
def test_pre_transfer_failures_never_touch_the_remote(tmp_path, setup, expected_message):
    """Each of these three checks runs strictly before the first network
    call (ssh mkdir -p for remote-connect) — a failure in any of them must
    refuse to sync with a clear message and leave no remote directory at
    all, never a partial or stale transfer."""
    checkout = _synthetic_remote_checkout(tmp_path)
    if setup == "no_git":
        shutil.rmtree(checkout / ".git")
    elif setup == "broken_exclude_builder":
        (checkout / "scripts" / "_remote_exclude_list.py").write_text(
            "#!/usr/bin/env python3\nraise SystemExit(1)\n"
        )
    elif setup == "no_commits":
        shutil.rmtree(checkout / ".git")
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)

    fake_bin = _fake_transport_bin(tmp_path)
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "scripts/remote-test.sh", "candidate"], cwd=checkout,
        text=True, capture_output=True, env=_synthetic_env(tmp_path, fake_bin, home),
    )
    assert result.returncode != 0
    assert expected_message in result.stdout, result.stdout + result.stderr
    assert "[remote-test] DONE rc=1" in result.stdout
    assert not (tmp_path / "remote").exists(), (
        "a pre-transfer failure must never create the remote directory at "
        "all — the first ssh call (remote-connect's mkdir -p) must never "
        "be reached"
    )
