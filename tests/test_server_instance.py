"""
Tests for the owned, isolated candidate-instance mechanism:
``scripts/candidate_snapshot.py`` and ``scripts/test_instance.py``.

Most of these are synthetic subprocess/manifest tests that never boot the
real LifeOS app (fast, deterministic). Exactly one test (marked ``slow``)
boots two real ``api.main:app`` candidate instances concurrently — a
bounded real-candidate smoke test, not a substitute fake app — to prove two
different candidate revisions behave differently and don't interfere.

Run focused, not as part of a broad suite:
    pytest -n 2 tests/test_server_instance.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.candidate_snapshot import (  # noqa: E402
    build_snapshot,
    verify_snapshot_unmodified,
    UnsafeSymlinkError,
)
from scripts.test_instance import (  # noqa: E402
    TestInstance,
    InstanceManifest,
    WrongCandidateError,
    _chroma_executable,
    _process_start_time,
    _free_port,
    stop_from_manifest,
)


def _init_synthetic_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def test_chroma_executable_follows_selected_instance_python(tmp_path):
    """Synthetic HOME must not make an owned instance pick PATH's Chroma."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    selected_python = bin_dir / "python"
    selected_python.touch()
    selected_chroma = bin_dir / "chroma"
    selected_chroma.touch()
    selected_chroma.chmod(0o755)

    assert _chroma_executable(str(selected_python)) == str(selected_chroma)


# ---------------------------------------------------------------------------
# candidate_snapshot.py — synthetic repo, no real source touched
# ---------------------------------------------------------------------------

def test_snapshot_excludes_runtime_data_preserves_modes_includes_untracked(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)

    (src / "run.sh").write_text("#!/bin/bash\necho hi\n")
    os.chmod(src / "run.sh", 0o755)
    (src / ".env").write_text("SECRET=synthetic-not-real\n")
    (src / "data").mkdir()
    (src / "data" / "real.db").write_text("synthetic")
    (src / "config").mkdir()
    (src / "config" / "credentials.json").write_text("{}")
    subprocess.run(["git", "add", "run.sh", ".env", "data/real.db", "config/credentials.json"],
                    cwd=src, check=True)
    # .env/data/*.db/credentials.json are excluded by our own denylist even
    # though nothing here relies on a real .gitignore existing.
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)

    (src / "untracked.txt").write_text("untracked but not ignored")

    dest = tmp_path / "snapshot"
    result = build_snapshot(src, dest)
    rel_paths = {f.rel_path for f in result.files}

    assert "run.sh" in rel_paths
    assert "untracked.txt" in rel_paths
    assert ".env" not in rel_paths
    assert "data/real.db" not in rel_paths
    assert "config/credentials.json" not in rel_paths
    assert not (dest / ".env").exists()
    assert not (dest / "data").exists()

    mode = os.stat(dest / "run.sh").st_mode & 0o777
    assert mode == 0o755, f"executable bit not preserved: {oct(mode)}"

    assert result.git_head is not None
    assert len(result.candidate_id) > 0


def test_snapshot_excludes_a_runtime_directory_that_is_a_symlink(tmp_path):
    """A checkout whose `data` directory is a link to storage elsewhere --
    an operator pointing it at another filesystem, or candidate
    verification pointing it at verifier-owned storage -- carries runtime
    state, not source, so the link is excluded like the directory it names
    instead of failing the escaping-symlink rule."""
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    external = tmp_path / "elsewhere" / "data"
    external.mkdir(parents=True)
    (external / "real.db").write_text("synthetic runtime state")
    (src / "app.py").write_text("VALUE = 1\n")
    (src / "data").symlink_to(external, target_is_directory=True)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "linked data directory"], cwd=src, check=True)

    result = build_snapshot(src, tmp_path / "snapshot")

    assert {f.rel_path for f in result.files} == {"app.py"}
    assert not os.path.lexists(tmp_path / "snapshot" / "data")


def test_snapshot_rejects_unsafe_external_symlink_without_dereferencing(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "sub").mkdir()
    (src / "sub" / "escape.txt").symlink_to("../../../etc/hosts")
    subprocess.run(["git", "add", "sub/escape.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unsafe symlink"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    with pytest.raises(UnsafeSymlinkError):
        build_snapshot(src, dest)
    # Failure must not leave a partial snapshot behind.
    assert not dest.exists()


def test_snapshot_accepts_internal_symlink(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "sub").mkdir()
    (src / "sub" / "target.txt").write_text("hello")
    (src / "link.txt").symlink_to("sub/target.txt")
    subprocess.run(["git", "add", "sub/target.txt", "link.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "internal symlink"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    build_snapshot(src, dest)
    assert os.path.islink(dest / "link.txt")
    assert (dest / "link.txt").read_text() == "hello"


def test_snapshot_rejects_symlink_whose_target_passes_through_another_escaping_symlink(tmp_path):
    """Regression: `link -> nested/sentinel.txt` where `nested` (one segment
    of the *target*, not the tracked path itself) is a symlink to outside
    the root. A check that only asks "does the final joined string start
    with root" passes here even though reaching it requires the kernel to
    traverse through the escaping `nested` symlink first — this must be
    rejected, and without ever reading the outside file's content.
    """
    src = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("should never be reachable")
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "nested").symlink_to(outside, target_is_directory=True)
    (src / "link").symlink_to("nested/sentinel.txt")
    subprocess.run(["git", "add", "nested", "link"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ancestor-swap via target segment"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    with pytest.raises(UnsafeSymlinkError):
        build_snapshot(src, dest)
    assert not dest.exists()


def test_snapshot_mutation_is_detected(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "a.txt").write_text("original")
    subprocess.run(["git", "add", "a.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    result = build_snapshot(src, dest)
    ok, mismatches = verify_snapshot_unmodified(result)
    assert ok and not mismatches

    (dest / "a.txt").write_text("mutated after snapshot")
    ok2, mismatches2 = verify_snapshot_unmodified(result)
    assert not ok2
    assert any(m.rel_path == "a.txt" for m in mismatches2)


def test_snapshot_mode_change_is_detected(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "a.txt").write_text("original")
    subprocess.run(["git", "add", "a.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    result = build_snapshot(src, dest)
    os.chmod(dest / "a.txt", 0o755)  # content unchanged, executable bit flipped
    ok, mismatches = verify_snapshot_unmodified(result)
    assert not ok
    assert any(m.kind == "mode_changed" for m in mismatches)


def test_dirty_to_dirty_source_mutation_during_copy_is_detected(tmp_path, monkeypatch):
    """A file that is ALREADY dirty (modified, uncommitted) when the snapshot
    starts, and is edited AGAIN (still dirty — same git status category)
    while the copy is in progress, must still be caught. `git status`
    text alone can't see this since the category never changes; this
    exercises the actual content re-verification path that replaced it."""
    import scripts.candidate_snapshot as cs

    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "a.txt").write_text("committed")
    subprocess.run(["git", "add", "a.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)
    (src / "a.txt").write_text("dirty v1")  # already dirty before the snapshot starts

    real_copy2 = cs.shutil.copy2

    def racing_copy2(source, dest, *args, **kwargs):
        result = real_copy2(source, dest, *args, **kwargs)
        if Path(source) == src / "a.txt":
            # Simulate a concurrent editor changing the file again, still
            # dirty (same `git status` category: " M a.txt" both times).
            (src / "a.txt").write_text("dirty v2 — changed again mid-copy")
        return result

    monkeypatch.setattr(cs.shutil, "copy2", racing_copy2)

    dest = tmp_path / "snapshot"
    with pytest.raises(cs.SourceMutatedError):
        build_snapshot(src, dest)
    assert not dest.exists(), "a detected mutation must not leave a partial snapshot behind"


def test_verify_reports_unsafe_path_instead_of_reading_through_a_parent_swap(tmp_path):
    """After a snapshot is built, if one of its included file's parent
    directories gets replaced by a symlink pointing outside the snapshot
    root, verification must report that as a mismatch — never silently
    read through the swap to compare content."""
    src = tmp_path / "repo"
    src.mkdir()
    _init_synthetic_repo(src)
    (src / "sub").mkdir()
    (src / "sub" / "a.txt").write_text("original")
    subprocess.run(["git", "add", "sub/a.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)

    dest = tmp_path / "snapshot"
    result = build_snapshot(src, dest)
    ok, mismatches = verify_snapshot_unmodified(result)
    assert ok and not mismatches

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("attacker-controlled content")
    shutil.rmtree(dest / "sub")
    (dest / "sub").symlink_to(outside, target_is_directory=True)

    ok2, mismatches2 = verify_snapshot_unmodified(result)
    assert not ok2
    assert any(m.rel_path == "sub/a.txt" and m.kind == "unsafe_path" for m in mismatches2)


# ---------------------------------------------------------------------------
# test_instance.py — synthetic subprocess ownership tests (no real app boot)
# ---------------------------------------------------------------------------

def _sleeper(duration: float = 30.0, cwd: Path | None = None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({duration})"], cwd=cwd)


def _owned_instance_dir(tmp_path, token: str) -> Path:
    """A directory that passes `_validate_manifest_ownership`: the right name
    prefix, the manifest as its direct child, and a matching sentinel file."""
    from scripts.test_instance import INSTANCE_ROOT_PREFIX, _OWNER_SENTINEL_NAME
    instance_dir = tmp_path / f"{INSTANCE_ROOT_PREFIX}unittest"
    instance_dir.mkdir(exist_ok=True)
    (instance_dir / "src").mkdir(exist_ok=True)
    (instance_dir / _OWNER_SENTINEL_NAME).write_text(token)
    return instance_dir


def _bare_manifest(tmp_path, *, token: str = "tok", owned: bool = True, **overrides) -> InstanceManifest:
    instance_dir = _owned_instance_dir(tmp_path, token) if owned else (tmp_path / "instance")
    if not owned:
        instance_dir.mkdir(exist_ok=True)
        (instance_dir / "src").mkdir(exist_ok=True)
    defaults = dict(
        token=token, candidate_id="cand", source_root=str(tmp_path),
        snapshot_root=str(instance_dir / "src"), instance_dir=str(instance_dir),
        api_port=0, base_url="http://127.0.0.1:0",
        vault_path=str(instance_dir / "vault"), chroma_path=str(instance_dir / "chromadb"),
        data_dir=str(instance_dir / "data"), log_path=str(instance_dir / "server.log"),
        llm_stub_url=None,
        parent_pid=os.getpid(), parent_start_time=_process_start_time(os.getpid()),
        child_pid=None, child_start_time=None, created_at=time.time(),
    )
    defaults.update(overrides)
    return InstanceManifest(**defaults)


def test_verify_rejects_stale_start_time_marker(tmp_path):
    """A manifest whose recorded start-time differs from the live process
    (the PID-reuse signature) is rejected rather than treated as owned."""
    proc = _sleeper()
    try:
        real_start = _process_start_time(proc.pid)
        manifest = _bare_manifest(
            tmp_path, child_pid=proc.pid, child_start_time=real_start - 9999,
        )
        inst = TestInstance(tmp_path)
        inst.manifest = manifest
        with pytest.raises(WrongCandidateError, match="start-time marker mismatch"):
            inst.verify()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_verify_rejects_foreign_listener(tmp_path):
    """A healthy listener on the manifest's port that belongs to a DIFFERENT
    PID than the one we own must never satisfy verification."""
    foreign = subprocess.Popen(
        [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    owned = _sleeper()  # our "owned" process does NOT listen on anything
    try:
        # Find the ephemeral port http.server actually bound to.
        foreign_port = _discover_listen_port(foreign.pid, timeout=5)
        manifest = _bare_manifest(
            tmp_path,
            child_pid=owned.pid, child_start_time=_process_start_time(owned.pid),
            api_port=foreign_port, base_url=f"http://127.0.0.1:{foreign_port}",
        )
        inst = TestInstance(tmp_path)
        inst.manifest = manifest
        with pytest.raises(WrongCandidateError, match="not our owned PID"):
            inst.verify()
    finally:
        foreign.terminate()
        owned.terminate()
        foreign.wait(timeout=5)
        owned.wait(timeout=5)


def test_verify_rejects_missing_listener(tmp_path):
    proc = _sleeper()
    try:
        port = _free_port()  # guaranteed nothing is listening here
        manifest = _bare_manifest(
            tmp_path, child_pid=proc.pid, child_start_time=_process_start_time(proc.pid),
            api_port=port, base_url=f"http://127.0.0.1:{port}",
        )
        inst = TestInstance(tmp_path)
        inst.manifest = manifest
        with pytest.raises(WrongCandidateError, match="nothing is listening"):
            inst.verify()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_verify_rejects_identity_endpoint_for_different_candidate(tmp_path):
    """A listener with the owned PID is still rejected unless it returns the
    exact manifest candidate and token at the private identity endpoint.
    """
    instance_dir = _owned_instance_dir(tmp_path, "tok")
    snapshot_root = instance_dir / "src"
    port = _free_port()
    server = subprocess.Popen(
        [
            sys.executable, "-c",
            "import http.server,json,sys\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200); self.end_headers(); "
            "  self.wfile.write(json.dumps({'candidate_id':'foreign','token':'foreign'}).encode())\n"
            " def log_message(self,*args): pass\n"
            "http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()",
            str(port),
        ],
        cwd=snapshot_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while _listener_pid_for_test(port) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        manifest = _bare_manifest(
            tmp_path, token="tok", child_pid=server.pid,
            child_start_time=_process_start_time(server.pid), api_port=port,
            base_url=f"http://127.0.0.1:{port}",
        )
        inst = TestInstance(tmp_path)
        inst.manifest = manifest
        with pytest.raises(WrongCandidateError, match="identity endpoint does not match"):
            inst.verify()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_stop_terminates_only_the_owned_pid_and_removes_instance_dir(tmp_path):
    # The owned process must run with cwd == the manifest's snapshot_root —
    # exactly like a real candidate — since stop now also checks that
    # before signaling (PID + start-time alone isn't a strong enough
    # identity claim for a manifest that could have been loaded from disk).
    instance_dir = _owned_instance_dir(tmp_path, "tok")
    snapshot_root = instance_dir / "src"
    owned = _sleeper(cwd=snapshot_root)
    sibling = _sleeper()  # an unrelated process that must survive
    try:
        manifest = _bare_manifest(
            tmp_path, token="tok",
            child_pid=owned.pid, child_start_time=_process_start_time(owned.pid),
        )
        manifest_path = Path(manifest.instance_dir) / "manifest.json"
        manifest.save_atomic(manifest_path)

        stop_from_manifest(manifest_path)

        owned.wait(timeout=5)
        assert owned.poll() is not None, "owned process was not terminated"
        assert sibling.poll() is None, "an unrelated process was killed — broad kill, not owned-PID kill"
        assert not Path(manifest.instance_dir).exists(), "instance dir was not cleaned up"
    finally:
        sibling.terminate()
        sibling.wait(timeout=5)
        if owned.poll() is None:
            owned.terminate()
            owned.wait(timeout=5)


def test_direct_owned_popen_teardown_reaps_without_zombie_timeout():
    """A direct child becomes a zombie until its Popen is waited, so teardown
    must reap it rather than spending the configured stop timeout polling its
    still-present PID.
    """
    from scripts.test_instance import _terminate_pid

    proc = _sleeper()
    started = time.monotonic()
    _terminate_pid(proc.pid, process=proc)
    assert proc.poll() is not None
    assert time.monotonic() - started < 1.5


def test_candidate_owner_treats_zombie_group_as_exited():
    """The durable owner must not retain capacity waiting for PID 1 to reap a
    process that has already exited.
    """
    from scripts._candidate_owner_bootstrap import _registered_groups_gone

    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    started = _process_start_time(proc.pid)
    try:
        deadline = time.monotonic() + 5
        observed_zombie = False
        while time.monotonic() < deadline:
            try:
                if psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE:
                    observed_zombie = True
                    break
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.05)
        assert observed_zombie
        assert _registered_groups_gone({proc.pid: started})
    finally:
        proc.wait(timeout=5)


def test_stop_rejects_manifest_whose_instance_dir_lacks_our_prefix(tmp_path):
    from scripts.test_instance import UnownedManifestError
    manifest = _bare_manifest(tmp_path, owned=False)  # no lifeos-test-instance- prefix
    manifest_path = Path(manifest.instance_dir) / "manifest.json"
    manifest.save_atomic(manifest_path)
    with pytest.raises(UnownedManifestError, match="prefix"):
        stop_from_manifest(manifest_path)
    assert Path(manifest.instance_dir).exists(), "should not have deleted an unowned directory"


def test_stop_rejects_manifest_with_mismatched_sentinel_token(tmp_path):
    from scripts.test_instance import UnownedManifestError
    manifest = _bare_manifest(tmp_path, token="tok-a")
    # Tamper with the sentinel after the fact — simulates a corrupted/foreign directory.
    sentinel = Path(manifest.instance_dir) / ".owner"
    sentinel.write_text("tok-b")
    manifest_path = Path(manifest.instance_dir) / "manifest.json"
    manifest.save_atomic(manifest_path)
    with pytest.raises(UnownedManifestError, match="sentinel"):
        stop_from_manifest(manifest_path)
    assert Path(manifest.instance_dir).exists(), "should not have deleted a directory that failed sentinel check"


def test_stop_rejects_manifest_not_living_in_its_own_claimed_dir(tmp_path):
    from scripts.test_instance import UnownedManifestError
    manifest = _bare_manifest(tmp_path)
    # Save the manifest somewhere OTHER than inside its claimed instance_dir.
    elsewhere = tmp_path / "elsewhere.json"
    manifest.save_atomic(elsewhere)
    with pytest.raises(UnownedManifestError, match="not its own parent directory"):
        stop_from_manifest(elsewhere)
    assert Path(manifest.instance_dir).exists()


def test_test_instance_stop_method_does_not_delete_on_ownership_rejection(tmp_path):
    """A failed ownership check leaves the instance directory intact.

    `TestInstance.stop()` and `stop_from_manifest` both preserve the directory
    when `_validate_manifest_ownership` rejects the manifest.
    """
    from scripts.test_instance import UnownedManifestError

    manifest = _bare_manifest(tmp_path, owned=False)  # fails the prefix check
    manifest_path = Path(manifest.instance_dir) / "manifest.json"
    manifest.save_atomic(manifest_path)

    inst = TestInstance(tmp_path)
    inst.manifest = manifest
    inst._manifest_path = manifest_path

    with pytest.raises(UnownedManifestError):
        inst.stop()
    assert Path(manifest.instance_dir).exists(), "ownership rejection must not delete the directory"


def test_run_supervised_command_runs_to_completion_and_reports_exit_code(tmp_path):
    """The reusable supervised-exec primitive (for a caller that already has
    its own snapshot dest_root/env and just wants the owner-liveness
    supervision, without starting a server or re-snapshotting)."""
    from scripts.test_instance import run_supervised_command

    ok = run_supervised_command(
        [sys.executable, "-c", "print('hello')"], cwd=str(tmp_path), env=os.environ.copy(),
    )
    assert ok.returncode == 0

    failing = run_supervised_command(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=str(tmp_path), env=os.environ.copy(),
    )
    assert failing.returncode == 3


def test_run_supervised_command_reaps_descendant_after_normal_exit(tmp_path):
    """A command that spawns its own background child and then exits
    normally must not leak that child — the whole process group is reaped,
    not just the direct command process."""
    from scripts.test_instance import run_supervised_command

    pid_file = tmp_path / "child_pid.txt"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.exit(0)\n"
    )
    result = run_supervised_command([sys.executable, "-c", script], cwd=str(tmp_path), env=os.environ.copy())
    assert result.returncode == 0

    child_pid = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(child_pid), "descendant survived the supervised command's normal exit"


def test_run_supervised_command_reaps_group_if_caller_force_kills_the_wrapper(tmp_path, monkeypatch):
    """If the caller has to kill the wrapper process itself (e.g. because
    its own `.wait()` raised for some external reason), the wrapper's own
    `finally`-based cleanup never gets a chance to run under that kill —
    `run_supervised_command`'s own exception path must reach the same
    process group directly, not just stop the wrapper and leak the command
    and its descendant.
    """
    from scripts.test_instance import run_supervised_command

    pid_file = tmp_path / "child_pid.txt"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )

    original_wait = subprocess.Popen.wait
    calls = {"n": 0}

    def raising_wait(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            deadline = time.time() + 10
            while time.time() < deadline and not pid_file.exists():
                time.sleep(0.05)
            raise KeyboardInterrupt("simulated external interrupt")
        return original_wait(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", raising_wait)

    with pytest.raises(KeyboardInterrupt):
        run_supervised_command([sys.executable, "-c", script], cwd=str(tmp_path), env=os.environ.copy())

    assert pid_file.exists(), "the inner script never got far enough to spawn its child"
    child_pid = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(child_pid), "descendant survived a caller-side force-kill of the wrapper"


def test_network_guard_blocks_external_allows_loopback():
    """Unit-level check of the pre-import network guard installed by
    ``_test_instance_bootstrap.py`` in the candidate child process — run here
    in-process (not inside a booted candidate) since the guard itself has no
    dependency on the application. The guard monkeypatches module-level
    `socket` functions, so this test restores the originals afterward —
    otherwise it would leak into every other test sharing this worker.
    """
    import importlib
    boot = importlib.import_module("scripts._test_instance_bootstrap")
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    try:
        boot._install_network_guard()
        with pytest.raises(PermissionError):
            socket.create_connection(("8.8.8.8", 53), timeout=2)
        # A known LifeOS production port must be blocked even on loopback —
        # the whole point is never letting an instance silently reach the
        # operator's real lifeos-api/ChromaDB/local-LLM services.
        with pytest.raises(PermissionError):
            socket.create_connection(("127.0.0.1", 8000), timeout=2)
        # A non-production loopback port must still be reachable (attempted
        # connect refused only because nothing listens there, never blocked
        # by the guard itself).
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(1)
            with pytest.raises(ConnectionRefusedError):
                s.connect(("127.0.0.1", 1))
        finally:
            s.close()
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection


def _discover_listen_port(pid: int, timeout: float = 5.0) -> int:
    import psutil
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for c in psutil.Process(pid).net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    return c.laddr.port
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        time.sleep(0.1)
    raise TimeoutError(f"pid {pid} never started listening")


def _listener_pid_for_test(port: int) -> int | None:
    for connection in psutil.net_connections(kind="inet"):
        if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port:
            return connection.pid
    return None


# ---------------------------------------------------------------------------
# Bounded real-candidate smoke test — two concurrent real api.main:app
# instances, deliberately made observably different revisions.
# ---------------------------------------------------------------------------

def _patch_candidate_marker(snapshot_root: Path, marker: str) -> None:
    """Simulate 'a different candidate revision' by editing ONE line inside a
    private snapshot copy — never the shared checkout — so this candidate's
    /health response is observably distinct from an unpatched one."""
    main_py = snapshot_root / "api" / "main.py"
    text = main_py.read_text()
    patched = text.replace('"service": "lifeos"', f'"service": "{marker}"')
    assert patched != text, "expected health_check's service field to be patchable"
    main_py.write_text(patched)


def _make_independent_source_checkout(tmp_path: Path, name: str, marker: str | None) -> Path:
    """A standalone git repo containing a real copy of this repo's tracked +
    working-tree content, optionally with one line patched in `api/main.py`
    BEFORE it's committed — so two candidates built from two such checkouts
    are genuinely different *source*, not the same content patched after
    snapshotting (which candidate_id, correctly, does not chase — see
    test_dirty_to_dirty_source_mutation_during_copy_is_detected).

    A normal direct run uses the regular Git-backed snapshot procedure. The
    outer verifier instead supplies a clean candidate snapshot without
    `.git`; only in that already-sanitized case do we copy its bytes into a
    disposable Git checkout for the nested-candidate test.
    """
    dest = tmp_path / name
    if (REPO_ROOT / ".git").exists():
        build_snapshot(REPO_ROOT, dest)
    else:
        shutil.copytree(REPO_ROOT, dest, symlinks=True)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=dest, check=True)
    if marker is not None:
        _patch_candidate_marker(dest, marker)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate checkout for test"], cwd=dest, check=True)
    return dest


# A minimal "controlled client script" (per the required real-candidate
# shape): reads the base URL `run` injects into its environment, hits
# /health, and reports the response back to the test via stdout.
_CLIENT_SCRIPT = (
    "import os, urllib.request\n"
    "url = os.environ['LIFEOS_TEST_BASE_URL'] + '/health'\n"
    "with urllib.request.urlopen(url, timeout=10) as r:\n"
    "    print('CLIENT_BODY=' + r.read().decode())\n"
    "with urllib.request.urlopen(os.environ['LIFEOS_CHROMA_URL'] + '/api/v2/heartbeat', timeout=10) as r:\n"
    "    assert r.status == 200\n"
    "print('CLIENT_CHROMA_URL=' + os.environ['LIFEOS_CHROMA_URL'])\n"
    "print('CLIENT_CHROMA_PID=' + os.environ['LIFEOS_CHROMA_PID'])\n"
    "print('CLIENT_VAULT_PATH=' + os.environ['LIFEOS_VAULT_PATH'])\n"
    "print('CLIENT_MANIFEST_PATH=' + os.environ['LIFEOS_TEST_MANIFEST_PATH'])\n"
)


def _run_sh_popen(source: Path, client_script: str = _CLIENT_SCRIPT) -> subprocess.Popen:
    server_sh = REPO_ROOT / "scripts" / "server.sh"
    return subprocess.Popen(
        [str(server_sh), "test-instance", "run", "--source", str(source), "--timeout", "90",
         "--", sys.executable, "-c", client_script],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@pytest.mark.slow
def test_two_concurrent_real_candidates_differ_and_dont_interfere(tmp_path):
    """Two independently-sourced candidates — one with a genuinely different
    `api/main.py` line, committed BEFORE its snapshot is taken — each run
    concurrently through the one supported entry point (`server.sh
    test-instance run`, which starts the instance, runs a controlled client
    script against it, and always stops it). Proves distinct candidate
    fingerprints (a real source difference, not a post-hoc patch), distinct
    real observable behavior, no interference between the two concurrent
    runs, and that neither ever touches the operator's port 8000. Wrong-
    candidate/foreign-listener rejection is exercised by the cheaper
    synthetic tests above, not duplicated here.
    """
    source_a = _make_independent_source_checkout(tmp_path, "candidate-a", marker=None)
    source_b = _make_independent_source_checkout(tmp_path, "candidate-b", marker="lifeos-candidate-b")

    proc_a = _run_sh_popen(source_a)
    proc_b = _run_sh_popen(source_b)  # started while A is still starting — genuine concurrency
    try:
        out_a, _ = proc_a.communicate(timeout=150)
        out_b, _ = proc_b.communicate(timeout=150)
    finally:
        for p in (proc_a, proc_b):
            if p.poll() is None:
                p.kill()

    assert proc_a.returncode == 0, out_a
    assert proc_b.returncode == 0, out_b

    def _parse(output: str) -> dict:
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    fields_a, fields_b = _parse(out_a), _parse(out_b)

    # Genuinely different candidate identity — real source content differs,
    # not a patch applied after the fingerprint was already taken.
    assert fields_a["candidate_id"] != fields_b["candidate_id"]
    assert fields_a["base_url"] != fields_b["base_url"]
    assert ":8000" not in fields_a["base_url"]
    assert ":8000" not in fields_b["base_url"]
    assert fields_a["chroma_url"] != fields_b["chroma_url"]
    assert ":8001" not in fields_a["chroma_url"]
    assert ":8001" not in fields_b["chroma_url"]
    assert fields_a["chroma_pid"] == fields_a["CLIENT_CHROMA_PID"]
    assert fields_b["chroma_pid"] == fields_b["CLIENT_CHROMA_PID"]
    assert fields_a["vault_path"] == fields_a["CLIENT_VAULT_PATH"]
    assert fields_b["vault_path"] == fields_b["CLIENT_VAULT_PATH"]
    assert fields_a["vault_path"] != fields_b["vault_path"]
    assert fields_a["manifest_path"] == fields_a["CLIENT_MANIFEST_PATH"]
    assert fields_b["manifest_path"] == fields_b["CLIENT_MANIFEST_PATH"]

    # Distinct real observable behavior from each concurrently-running
    # candidate, as reported by its own client script — not a hardcoded
    # fixture and not read after the fact from a single shared instance.
    body_a = json.loads(fields_a["CLIENT_BODY"])
    body_b = json.loads(fields_b["CLIENT_BODY"])
    assert body_a["service"] == "lifeos"
    assert body_b["service"] == "lifeos-candidate-b"


@pytest.mark.slow
def test_cancelling_one_owned_candidate_keeps_other_chroma_healthy(tmp_path):
    """SIGKILL of one server.sh lifecycle reaps only its API/Chroma group.

    The other real candidate remains usable on its independently allocated
    Chroma port, proving cancellation never falls back to a shared listener
    or a broad process-name kill.
    """
    source_a = _make_independent_source_checkout(tmp_path, "candidate-a", marker=None)
    source_b = _make_independent_source_checkout(tmp_path, "candidate-b", marker="lifeos-candidate-b")
    sleeper = (
        "import os,time,urllib.request\n"
        "for url in (os.environ['LIFEOS_TEST_BASE_URL'] + '/health', os.environ['LIFEOS_CHROMA_URL'] + '/api/v2/heartbeat'):\n"
        " with urllib.request.urlopen(url, timeout=10) as r: assert r.status == 200\n"
        "print('CLIENT_READY=1', flush=True)\n"
        "time.sleep(30)\n"
    )
    survivor = (
        "import os,time,urllib.request\n"
        "for url in (os.environ['LIFEOS_TEST_BASE_URL'] + '/health', os.environ['LIFEOS_CHROMA_URL'] + '/api/v2/heartbeat'):\n"
        " with urllib.request.urlopen(url, timeout=10) as r: assert r.status == 200\n"
        "print('CLIENT_READY=1', flush=True)\n"
        "time.sleep(2)\n"
        "for url in (os.environ['LIFEOS_TEST_BASE_URL'] + '/health', os.environ['LIFEOS_CHROMA_URL'] + '/api/v2/heartbeat'):\n"
        " with urllib.request.urlopen(url, timeout=10) as r: assert r.status == 200\n"
        "print('CLIENT_SURVIVED=1', flush=True)\n"
    )
    proc_a = _run_sh_popen(source_a, sleeper)
    proc_b = _run_sh_popen(source_b, survivor)

    def read_until_ready(process: subprocess.Popen) -> list[str]:
        lines: list[str] = []
        deadline = time.time() + 120
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("CLIENT_READY="):
                return lines
        raise AssertionError("candidate did not become ready: " + "".join(lines))

    try:
        lines_a = read_until_ready(proc_a)
        read_until_ready(proc_b)
        fields_a = dict(line.strip().split("=", 1) for line in lines_a if "=" in line)
        manifest_a = InstanceManifest.load(Path(fields_a["manifest_path"]))
        proc_a.kill()
        assert proc_a.wait(timeout=10) == -signal.SIGKILL

        output_b, _ = proc_b.communicate(timeout=30)
        assert proc_b.returncode == 0, output_b
        assert "CLIENT_SURVIVED=1" in output_b

        owned_pids = tuple(pid for pid in (
            manifest_a.child_pid, manifest_a.chroma_pid, manifest_a.chroma_supervisor_pid,
        ) if pid is not None)
        assert len(owned_pids) == 3
        deadline = time.time() + 20
        while time.time() < deadline and any(
            psutil.pid_exists(pid) for pid in owned_pids
        ):
            time.sleep(0.1)
        assert not any(psutil.pid_exists(pid) for pid in owned_pids)
        assert not Path(manifest_a.instance_dir).exists()
    finally:
        for process in (proc_a, proc_b):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.mark.slow
def test_owner_sigkill_triggers_child_and_dir_cleanup_via_liveness_pipe(tmp_path):
    """A SIGKILLed orchestrator can't run atexit or signal handlers — nothing
    in it executes ever again. This proves the owner-liveness pipe (not
    process supervision code) is what still cleans up: both the owned child
    process AND its instance directory, on a plain SIGKILL of the owner.
    """
    source = _make_independent_source_checkout(tmp_path, "candidate-owner", marker=None)
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from scripts.test_instance import TestInstance\n"
        "inst = TestInstance(%r, start_timeout=90)\n"
        "m = inst.start()\n"
        "print('MANIFEST=' + m.instance_dir + '/manifest.json', flush=True)\n"
        "import time; time.sleep(300)\n"
    ) % (str(REPO_ROOT), str(source))

    orchestrator = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
    )
    manifest_path = None
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            line = orchestrator.stdout.readline()
            if not line:
                break
            if line.startswith("MANIFEST="):
                manifest_path = Path(line.strip().split("=", 1)[1])
                break
        assert manifest_path is not None, "orchestrator never printed a manifest path"

        manifest = InstanceManifest.load(manifest_path)
        child_pid = manifest.child_pid
        chroma_pid = manifest.chroma_pid
        chroma_supervisor_pid = manifest.chroma_supervisor_pid
        instance_dir = Path(manifest.instance_dir)
        assert psutil.pid_exists(child_pid)
        assert chroma_pid and psutil.pid_exists(chroma_pid)
        assert chroma_supervisor_pid and psutil.pid_exists(chroma_supervisor_pid)

        orchestrator.kill()  # SIGKILL — no atexit/signal handler in it can ever run
        orchestrator.wait(timeout=5)

        # The child's liveness-pipe watcher must notice EOF and self-clean —
        # both itself and its instance directory — with no help from the
        # (now-dead) orchestrator.
        cleanup_deadline = time.time() + 20
        while time.time() < cleanup_deadline and psutil.pid_exists(child_pid):
            time.sleep(0.2)
        assert not psutil.pid_exists(child_pid), "candidate child survived its SIGKILLed orchestrator"
        while time.time() < cleanup_deadline and (
            psutil.pid_exists(chroma_pid) or psutil.pid_exists(chroma_supervisor_pid)
        ):
            time.sleep(0.2)
        assert not psutil.pid_exists(chroma_pid), "owned Chroma survived its SIGKILLed orchestrator"
        assert not psutil.pid_exists(chroma_supervisor_pid), "owned Chroma supervisor survived SIGKILL"

        while time.time() < cleanup_deadline and instance_dir.exists():
            time.sleep(0.2)
        assert not instance_dir.exists(), "instance dir survived its SIGKILLed orchestrator"
    finally:
        if orchestrator.poll() is None:
            orchestrator.kill()
            orchestrator.wait(timeout=5)
