"""Synthetic subprocess and ownership tests for shared test capacity."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts.test_capacity import (
    CapacityCancelled,
    CapacityError,
    CapacityManager,
    _canonical_state_root,
    _configure_state,
    _main as capacity_main,
)


pytestmark = pytest.mark.unit


def test_canonical_capacity_home_ignores_synthetic_home(monkeypatch):
    class Account:
        pw_dir = "/synthetic/account-home"

    monkeypatch.setenv("HOME", "/synthetic/candidate-home")
    monkeypatch.setattr("scripts.test_capacity.pwd.getpwuid", lambda uid: Account())
    assert _canonical_state_root() == Path("/synthetic/account-home/.cache/lifeos/development-capacity")


def _hold_capacity(root: str, workers: int, active, connection) -> None:
    manager = CapacityManager.for_test(root, total_workers=2)
    with manager.acquire(workers) as lease:
        with active.get_lock():
            active.value += lease.workers
        connection.send("acquired")
        connection.recv()
        with active.get_lock():
            active.value -= lease.workers


def _terminate_holder(root: str, connection) -> None:
    manager = CapacityManager.for_test(root, total_workers=2)
    lease = manager.acquire(2)
    connection.send("acquired")
    connection.recv()
    lease.release()


def _initialize_capacity(root: str, connection) -> None:
    manager = CapacityManager.for_test(root, total_workers=2)
    connection.send(manager.total_workers)
    connection.recv()


def test_two_processes_never_exceed_total_worker_budget_and_progress_after_release(tmp_path):
    context = multiprocessing.get_context("fork")
    active = context.Value("i", 0)
    first_parent, first_child = context.Pipe()
    second_parent, second_child = context.Pipe()
    first = context.Process(target=_hold_capacity, args=(str(tmp_path), 2, active, first_child))
    second = context.Process(target=_hold_capacity, args=(str(tmp_path), 2, active, second_child))
    first.start()
    second.start()
    try:
        assert first_parent.recv() == "acquired"
        assert active.value == 2
        # The second process cannot acquire any slot while the first owns all
        # slots; receiving its marker after release proves both contention and
        # subsequent progress without a timing-only assertion.
        first_parent.send("release")
        assert second_parent.recv() == "acquired"
        assert active.value == 2
        second_parent.send("release")
    finally:
        for process in (first, second):
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_abnormal_owner_termination_releases_kernel_slots(tmp_path):
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    holder = context.Process(target=_terminate_holder, args=(str(tmp_path), child))
    holder.start()
    assert parent.recv() == "acquired"
    holder.terminate()
    holder.join(timeout=10)
    assert holder.exitcode is not None

    manager = CapacityManager.for_test(tmp_path, total_workers=2)
    with manager.acquire(2) as lease:
        assert lease.workers == 2


def test_concurrent_first_initialization_leaves_complete_atomic_config(tmp_path):
    context = multiprocessing.get_context("fork")
    first_parent, first_child = context.Pipe()
    second_parent, second_child = context.Pipe()
    first = context.Process(target=_initialize_capacity, args=(str(tmp_path), first_child))
    second = context.Process(target=_initialize_capacity, args=(str(tmp_path), second_child))
    first.start()
    second.start()
    assert first_parent.recv() == 2
    assert second_parent.recv() == 2
    first_parent.send("release")
    second_parent.send("release")
    first.join(timeout=10)
    second.join(timeout=10)
    config = json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8"))
    assert config == {"max_run_workers": 2, "total_workers": 2, "version": 2}
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_cancelled_waiter_does_not_leave_a_slot_or_stale_owner(tmp_path):
    context = multiprocessing.get_context("fork")
    active = context.Value("i", 0)
    parent, child = context.Pipe()
    holder = context.Process(target=_hold_capacity, args=(str(tmp_path), 2, active, child))
    holder.start()
    assert parent.recv() == "acquired"

    cancelled = threading.Event()
    manager = CapacityManager.for_test(tmp_path, total_workers=2)
    outcome: list[BaseException] = []

    def wait_for_capacity() -> None:
        try:
            manager.acquire(1, cancel=cancelled)
        except BaseException as exc:  # assertion below checks exact type
            outcome.append(exc)

    waiter = threading.Thread(target=wait_for_capacity)
    waiter.start()
    cancelled.set()
    waiter.join(timeout=10)
    parent.send("release")
    holder.join(timeout=10)
    assert not waiter.is_alive()
    assert isinstance(outcome[0], CapacityCancelled)
    assert holder.exitcode == 0


def test_nested_acquisition_requires_live_parent_and_borrow_does_not_release_outer(tmp_path):
    manager = CapacityManager.for_test(tmp_path, total_workers=2)
    with manager.acquire(2) as outer:
        nested = manager.acquire(1, inherited=outer)
        assert nested.active
        assert outer.active
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(CapacityError):
            manager.acquire(1, inherited=None, cancel=cancelled)
        outer.release()
        assert not nested.active
        with pytest.raises(CapacityError):
            nested.__enter__()
        nested.release()


def test_exec_descendant_cannot_keep_capacity_after_parent_release(tmp_path):
    manager = CapacityManager.for_test(tmp_path, total_workers=2)
    child_code = "import sys; print('ready', flush=True); sys.stdin.read()"
    with manager.acquire(2):
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            close_fds=False,
        )
        assert child.stdout.readline().strip() == "ready"
    # If slot descriptors leaked across exec, this acquisition would block
    # while the still-running child waits on stdin.
    with manager.acquire(2) as replacement:
        assert replacement.active
    child.stdin.close()
    assert child.wait(timeout=10) == 0


def test_remote_shaped_subprocess_uses_same_state_root(tmp_path):
    code = """
import sys
from scripts.test_capacity import CapacityManager
manager = CapacityManager.for_test(sys.argv[1], total_workers=2)
with manager.acquire(2):
    print('acquired', flush=True)
    sys.stdin.read()
"""
    first = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    second = None
    first_closed = False
    try:
        assert first.stdout.readline().strip() == "acquired"
        second = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        first.stdin.close()
        first_closed = True
        assert second.stdout.readline().strip() == "acquired"
    finally:
        if first.poll() is None and not first_closed:
            first.stdin.close()
        first.wait(timeout=10)
        if second is not None:
            if second.poll() is None:
                second.stdin.close()
            second.wait(timeout=10)


def test_capacity_root_symlink_and_shared_mode_are_not_mutated(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    target_before = set(target.iterdir())
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CapacityError):
        CapacityManager.for_test(link / "nested", total_workers=2)
    assert set(target.iterdir()) == target_before

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    mode_before = shared.stat().st_mode
    with pytest.raises(CapacityError):
        CapacityManager.for_test(shared, total_workers=2)
    assert shared.stat().st_mode == mode_before


def _hold_normal_capacity(root: str, workers: int, connection) -> None:
    manager = CapacityManager(_state_root=Path(root))
    with manager.acquire(workers):
        connection.send("acquired")
        connection.recv()


def _preconstructed_normal_manager(root: str, connection) -> None:
    manager = CapacityManager(_state_root=Path(root))
    connection.send(("constructed", manager.total_workers))
    connection.recv()
    with manager.acquire(1):
        connection.send(("acquired", manager.total_workers))
        connection.recv()


def _preconstructed_shrink_managers(root: str, active, connection) -> None:
    unpinned = CapacityManager(_state_root=Path(root))
    pinned = CapacityManager(3, _state_root=Path(root))
    connection.send(("constructed", unpinned.total_workers, pinned.total_workers))
    assert connection.recv() == "acquire-unpinned"
    with unpinned.acquire(2) as lease:
        with active.get_lock():
            active.value += lease.workers
        connection.send(("unpinned-acquired", unpinned.total_workers, lease.workers))
        assert connection.recv() == "release-unpinned"
        with active.get_lock():
            active.value -= lease.workers
    assert connection.recv() == "check-pinned"
    with pytest.raises(CapacityError, match="does not match host budget"):
        pinned.acquire(1)
    connection.send("pinned-rejected")


def _wait_for_normal_capacity(root: str, active, connection) -> None:
    manager = CapacityManager(_state_root=Path(root))
    with manager.acquire(1) as lease:
        with active.get_lock():
            active.value += lease.workers
        connection.send(("acquired", manager.total_workers, lease.workers))
        assert connection.recv() == "release"
        with active.get_lock():
            active.value -= lease.workers


def test_first_per_run_override_keeps_the_persisted_normal_cap_conservative(tmp_path):
    dedicated = CapacityManager(max_run_workers=4, _state_root=tmp_path)
    assert dedicated.total_workers == 4
    assert dedicated.max_run_workers == 4
    assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8")) == {
        "max_run_workers": 2,
        "total_workers": 4,
        "version": 2,
    }

    ordinary = CapacityManager(_state_root=tmp_path)
    assert ordinary.max_run_workers == 2
    with pytest.raises(CapacityError, match="per-run capacity"):
        ordinary.acquire(3)
    with dedicated.acquire(4) as lease:
        assert lease.workers == 4


def test_explicit_configuration_alone_changes_the_persisted_normal_cap(tmp_path):
    CapacityManager(_state_root=tmp_path)
    _configure_state(tmp_path, 4, 3)

    ordinary = CapacityManager(_state_root=tmp_path)
    assert ordinary.total_workers == 4
    assert ordinary.max_run_workers == 3
    assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8")) == {
        "max_run_workers": 3,
        "total_workers": 4,
        "version": 2,
    }


def test_active_lease_refuses_configuration_without_changing_canonical_state(tmp_path):
    CapacityManager.for_test(tmp_path, total_workers=2)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    holder = context.Process(target=_hold_normal_capacity, args=(str(tmp_path), 2, child))
    holder.start()
    try:
        assert parent.recv() == "acquired"
        with pytest.raises(CapacityError, match="leases are active"):
            _configure_state(tmp_path, 3, 2)
        assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8"))["total_workers"] == 2
    finally:
        parent.send("release")
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0


def test_quiescent_growth_and_restoration_refreshes_existing_manager_and_rejects_legacy_total(tmp_path):
    CapacityManager.for_test(tmp_path, total_workers=2)
    existing = CapacityManager(_state_root=tmp_path)
    _configure_state(tmp_path, 3, 2)
    with existing.acquire(1):
        assert existing.total_workers == 3
    with CapacityManager(max_run_workers=3, _state_root=tmp_path).acquire(3) as dedicated:
        assert dedicated.workers == 3
    with pytest.raises(CapacityError, match="does not match host budget"):
        CapacityManager(2, _state_root=tmp_path)
    _configure_state(tmp_path, 2, 2)
    with existing.acquire(2):
        assert existing.total_workers == 2
    assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8")) == {
        "max_run_workers": 2,
        "total_workers": 2,
        "version": 2,
    }


def test_preconstructed_manager_refreshes_after_configuration_before_acquisition(tmp_path):
    CapacityManager.for_test(tmp_path, total_workers=2)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_preconstructed_normal_manager, args=(str(tmp_path), child))
    process.start()
    try:
        assert parent.recv() == ("constructed", 2)
        _configure_state(tmp_path, 3, 2)
        parent.send("acquire")
        assert parent.recv() == ("acquired", 3)
        parent.send("release")
    finally:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_cross_process_shrink_refreshes_unpinned_and_rejects_pinned_managers(tmp_path):
    CapacityManager.for_test(tmp_path, total_workers=3)
    context = multiprocessing.get_context("fork")
    holder_parent, holder_child = context.Pipe()
    holder = context.Process(target=_hold_normal_capacity, args=(str(tmp_path), 3, holder_child))
    holder.start()
    child_parent = child_child = waiter_parent = waiter_child = None
    child = waiter = None
    try:
        assert holder_parent.recv() == "acquired"
        with pytest.raises(CapacityError, match="leases are active"):
            _configure_state(tmp_path, 2, 2)
        assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8"))["total_workers"] == 3
        holder_parent.send("release")
        holder.join(timeout=10)
        assert holder.exitcode == 0

        active = context.Value("i", 0)
        child_parent, child_child = context.Pipe()
        child = context.Process(
            target=_preconstructed_shrink_managers,
            args=(str(tmp_path), active, child_child),
        )
        child.start()
        assert child_parent.recv() == ("constructed", 3, 3)
        _configure_state(tmp_path, 2, 2)
        child_parent.send("acquire-unpinned")
        assert child_parent.recv() == ("unpinned-acquired", 2, 2)
        assert active.value == 2

        waiter_parent, waiter_child = context.Pipe()
        waiter = context.Process(
            target=_wait_for_normal_capacity,
            args=(str(tmp_path), active, waiter_child),
        )
        waiter.start()
        assert not waiter_parent.poll(0.2)
        assert active.value == 2
        child_parent.send("release-unpinned")
        assert waiter_parent.recv() == ("acquired", 2, 1)
        assert active.value == 1
        waiter_parent.send("release")
        child_parent.send("check-pinned")
        assert child_parent.recv() == "pinned-rejected"
    finally:
        for connection, signal in (
            (holder_parent, "release"),
            (child_parent, "release-unpinned"),
            (child_parent, "check-pinned"),
            (waiter_parent, "release"),
        ):
            if connection is not None:
                try:
                    connection.send(signal)
                except (BrokenPipeError, EOFError, OSError):
                    pass
        for process in (holder, child, waiter):
            if process is not None:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
    assert child is not None and child.exitcode == 0
    assert waiter is not None and waiter.exitcode == 0


def test_configuration_preserves_cancellation_and_owner_death_cleanup_for_normal_callers(tmp_path):
    CapacityManager.for_test(tmp_path, total_workers=2)
    _configure_state(tmp_path, 3, 2)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    holder = context.Process(target=_hold_normal_capacity, args=(str(tmp_path), 2, child))
    holder.start()
    assert parent.recv() == "acquired"

    cancelled = threading.Event()
    waiter = CapacityManager(_state_root=tmp_path)
    outcome: list[BaseException] = []

    def wait_for_capacity() -> None:
        try:
            waiter.acquire(2, cancel=cancelled)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=wait_for_capacity)
    thread.start()
    cancelled.set()
    thread.join(timeout=10)
    assert isinstance(outcome[0], CapacityCancelled)
    holder.terminate()
    holder.join(timeout=10)
    assert holder.exitcode is not None
    with CapacityManager(_state_root=tmp_path).acquire(2) as lease:
        assert lease.workers == 2


def test_configuration_cli_exposes_one_canonical_total_and_normal_cap(monkeypatch):
    called: list[tuple[int, int]] = []

    def configure(total_workers: int, *, max_run_workers: int) -> None:
        called.append((total_workers, max_run_workers))

    monkeypatch.setattr("scripts.test_capacity.configure_host_capacity", configure)
    assert capacity_main(["configure", "--total-workers", "8", "--max-run-workers", "2"]) == 0
    assert called == [(8, 2)]


def test_dedicated_runner_cli_exposes_the_explicit_per_run_override():
    runner = Path(__file__).resolve().parents[1] / "scripts" / "verify_candidate.py"
    result = subprocess.run(
        [sys.executable, str(runner), "local", "--help"],
        text=True, capture_output=True, check=True,
    )
    assert "--capacity-max-run-workers" in result.stdout
