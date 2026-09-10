#!/usr/bin/env python3
"""User-scoped host capacity for development test runners.

This module is an allocation primitive only.  It never starts a test or
worker, and it does not implement a scheduler.  The outer runner should hold
one lease around the complete invocation, then use ``lease.waited_seconds``
with the development metrics adapter before measuring execution separately.

The production manager uses a canonical per-user state directory.  The
``for_test`` constructor is the only supported alternate root and is intended
for synthetic subprocess tests; callers must not use it to evade the shared
host budget.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import json
import os
import pwd
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Event

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.development_metrics import MetricsError, _open_private, _private_file


PROVISIONAL_TOTAL_WORKERS = 4
PROVISIONAL_MAX_RUN_WORKERS = 2
STATE_VERSION = 2
_MAX_TOTAL_WORKERS = 256
_ACTIVE_LEASES: set["CapacityLease"] = set()


class CapacityError(RuntimeError):
    """Base class for capacity allocation failures."""


class CapacityCancelled(CapacityError):
    """Raised when a waiting acquisition is cancelled."""


def _validate_workers(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CapacityError(f"{field} must be a positive integer")
    if value > _MAX_TOTAL_WORKERS:
        raise CapacityError(f"{field} is too large")
    return value


def _canonical_state_root() -> Path:
    # Do not derive this from HOME or another caller-provided environment
    # variable: isolated candidate runs may use synthetic HOME values while
    # still needing the same host-wide capacity namespace.
    account_home = pwd.getpwuid(os.getuid()).pw_dir
    return Path(account_home) / ".cache" / "lifeos" / "development-capacity"


def _release_forked_leases() -> None:
    # flock descriptors are inherited across fork.  A forked descendant must
    # not accidentally keep its parent's capacity after the parent exits.
    for lease in tuple(_ACTIVE_LEASES):
        lease._release_in_child()


try:
    os.register_at_fork(after_in_child=_release_forked_leases)
except AttributeError:  # pragma: no cover - supported project platforms have it
    pass


def _prepare_root(root: Path) -> None:
    """Create and validate the owner-only canonical state directory."""

    # Validate each component before creating a missing one; this avoids
    # creating a child through an adopted symlink target.
    current = Path(root.anchor or ".")
    for component in root.parts:
        if component in (root.anchor, ""):
            continue
        current /= component
        try:
            root_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                root_stat = current.lstat()
            except OSError as exc:
                raise CapacityError("unable to prepare capacity state") from exc
        except OSError as exc:
            raise CapacityError("unable to inspect capacity state") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise CapacityError("capacity state contains a symlink")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise CapacityError("capacity state is not a directory")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise CapacityError("unable to inspect capacity state") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        raise CapacityError("capacity state must be an owner-only directory")


@contextmanager
def _locked_state(root: Path, *, cancel: Event | None = None):
    """Hold the one state lock that fences construction and acquisition."""

    _prepare_root(root)
    init_path = root / "initialize.lock"
    _private_file(init_path)
    init_fd = _open_private(init_path, os.O_RDWR)
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise CapacityCancelled("capacity acquisition cancelled")
            try:
                fcntl.flock(init_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if cancel is not None:
                    cancel.wait(0.02)
                else:
                    time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(init_fd, fcntl.LOCK_UN)
        finally:
            os.close(init_fd)


def _read_config_locked(root: Path) -> tuple[int, int, int]:
    """Read one compatible state configuration while ``initialize.lock`` is held."""

    config_path = root / "capacity.json"
    _private_file(config_path)
    config_fd = _open_private(config_path, os.O_RDWR)
    try:
        raw = os.read(config_fd, 4096)
    finally:
        os.close(config_fd)
    if not raw:
        return 0, 0, 0
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityError("capacity configuration is corrupt") from exc
    if not isinstance(config, dict):
        raise CapacityError("capacity configuration is corrupt")
    version = config.get("version")
    total = config.get("total_workers")
    if version == 1:
        total = _validate_workers(total, field="total_workers")
        # Version 1 deliberately had no persistent normal-run cap.  New
        # callers retain its conservative behaviour until an explicit config.
        return total, min(PROVISIONAL_MAX_RUN_WORKERS, total), version
    if version != STATE_VERSION:
        raise CapacityError("capacity configuration version is unsupported")
    total = _validate_workers(total, field="total_workers")
    normal_max = _validate_workers(config.get("max_run_workers"), field="max_run_workers")
    if normal_max > total:
        raise CapacityError("capacity configuration is corrupt")
    return total, normal_max, version


def _write_config_locked(root: Path, total_workers: int, max_run_workers: int) -> None:
    """Atomically replace the canonical configuration while state is fenced."""

    encoded = json.dumps(
        {"version": STATE_VERSION, "total_workers": total_workers, "max_run_workers": max_run_workers},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    config_path = root / "capacity.json"
    temp_path = root / f"capacity-{uuid.uuid4().hex}.tmp"
    temp_fd = os.open(
        temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(temp_fd, view) :]
        os.fsync(temp_fd)
    finally:
        os.close(temp_fd)
    os.replace(temp_path, config_path)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _slot_paths_locked(root: Path, total_workers: int) -> tuple[Path, ...]:
    slot_paths: list[Path] = []
    for index in range(total_workers):
        slot_path = root / f"slot-{index:03d}.lock"
        _private_file(slot_path)
        slot_paths.append(slot_path)
    return tuple(slot_paths)


def _prepare_state(
    root: Path,
    requested_total: int | None,
    requested_max_run: int | None,
    initial_normal_max_run: int | None,
) -> tuple[tuple[Path, ...], int, int]:
    """Read or initialize canonical state and return current allocation paths."""

    with _locked_state(root):
        total_workers, normal_max, version = _read_config_locked(root)
        if version == 0:
            total_workers = requested_total or PROVISIONAL_TOTAL_WORKERS
            # A caller's requested maximum is a lease-local override.  It
            # must not turn an otherwise ordinary first caller into a host
            # policy writer.  Only the synthetic test constructor supplies
            # an explicit initial normal maximum for its isolated root.
            normal_max = (
                initial_normal_max_run
                if initial_normal_max_run is not None
                else min(PROVISIONAL_MAX_RUN_WORKERS, total_workers)
            )
            if normal_max > total_workers:
                raise CapacityError("max_run_workers exceeds total_workers")
            _write_config_locked(root, total_workers, normal_max)
        if requested_total is not None and requested_total != total_workers:
            raise CapacityError("capacity configuration does not match host budget")
        max_run_workers = requested_max_run if requested_max_run is not None else normal_max
        if max_run_workers > total_workers:
            raise CapacityError("max_run_workers exceeds total_workers")
        return _slot_paths_locked(root, total_workers), total_workers, max_run_workers


def configure_host_capacity(
    total_workers: int,
    *,
    max_run_workers: int = PROVISIONAL_MAX_RUN_WORKERS,
) -> None:
    """Atomically change the canonical host budget only while every slot is idle.

    This is intentionally the sole production migration API.  It never waits
    for a lease: a busy slot rejects the operation and leaves the old config.
    """

    total_workers = _validate_workers(total_workers, field="total_workers")
    max_run_workers = _validate_workers(max_run_workers, field="max_run_workers")
    if max_run_workers > total_workers:
        raise CapacityError("max_run_workers exceeds total_workers")
    root = _canonical_state_root()
    _configure_state(root, total_workers, max_run_workers)


def _configure_state(root: Path, total_workers: int, max_run_workers: int) -> None:
    """Synthetic-root companion for deterministic migration tests."""

    with _locked_state(root):
        current_total, _current_max, version = _read_config_locked(root)
        if version == 0:
            current_total = 0
        held_fds: list[int] = []
        try:
            for slot_path in _slot_paths_locked(root, current_total):
                fd = _open_private(slot_path, os.O_RDWR | os.O_CLOEXEC)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    os.close(fd)
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise CapacityError("cannot configure capacity while leases are active") from None
                    raise
                held_fds.append(fd)
            _slot_paths_locked(root, total_workers)
            _write_config_locked(root, total_workers, max_run_workers)
        finally:
            for fd in held_fds:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@dataclasses.dataclass(eq=False)
class CapacityLease:
    """A live kernel-held allocation, released by context exit or close."""

    manager: "CapacityManager"
    workers: int
    waited_seconds: float
    _fds: list[int] = dataclasses.field(default_factory=list, repr=False)
    _borrowed: bool = False
    _parent: "CapacityLease | None" = dataclasses.field(default=None, repr=False)
    _released: bool = False

    def __post_init__(self) -> None:
        _ACTIVE_LEASES.add(self)

    @property
    def active(self) -> bool:
        if self._released:
            return False
        if self._borrowed:
            return self._parent is not None and self._parent.active
        return bool(self._fds)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _ACTIVE_LEASES.discard(self)
        if self._borrowed:
            return
        for fd in self._fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    close = release

    def _release_in_child(self) -> None:
        # Called only after fork, before child code can use an inherited lease.
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        self._released = True
        _ACTIVE_LEASES.discard(self)

    def __enter__(self) -> "CapacityLease":
        if not self.active:
            raise CapacityError("capacity lease is not active")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.release()
        return False


class CapacityManager:
    """Acquire bounded host-wide worker slots exactly once at an outer edge."""

    def __init__(
        self,
        total_workers: int | None = None,
        *,
        max_run_workers: int | None = None,
        _state_root: Path | None = None,
        _initial_normal_max_run: int | None = None,
    ):
        self._requested_total = (
            _validate_workers(total_workers, field="total_workers") if total_workers is not None else None
        )
        self._requested_max_run = (
            _validate_workers(max_run_workers, field="max_run_workers") if max_run_workers is not None else None
        )
        if (
            self._requested_total is not None
            and self._requested_max_run is not None
            and self._requested_max_run > self._requested_total
        ):
            raise CapacityError("max_run_workers exceeds total_workers")
        if _initial_normal_max_run is not None:
            _initial_normal_max_run = _validate_workers(
                _initial_normal_max_run, field="max_run_workers",
            )
            if self._requested_total is not None and _initial_normal_max_run > self._requested_total:
                raise CapacityError("max_run_workers exceeds total_workers")
        self.state_root = Path(_state_root) if _state_root is not None else _canonical_state_root()
        try:
            self.slot_paths, self.total_workers, self.max_run_workers = _prepare_state(
                self.state_root, self._requested_total, self._requested_max_run, _initial_normal_max_run,
            )
        except (CapacityError, MetricsError):
            raise
        except OSError as exc:
            raise CapacityError("unable to prepare capacity state") from exc

    @classmethod
    def for_test(cls, state_root: str | os.PathLike[str], *, total_workers: int = 2) -> "CapacityManager":
        """Create isolated synthetic state for focused subprocess tests only."""

        return cls(
            total_workers=total_workers,
            max_run_workers=total_workers,
            _state_root=Path(state_root),
            _initial_normal_max_run=total_workers,
        )

    def _refresh_locked(self) -> tuple[tuple[Path, ...], int]:
        """Re-read the fenced config so a pre-existing manager cannot go stale."""

        total, normal_max, _version = _read_config_locked(self.state_root)
        if self._requested_total is not None and self._requested_total != total:
            raise CapacityError("capacity configuration does not match host budget")
        max_run = self._requested_max_run if self._requested_max_run is not None else normal_max
        if max_run > total:
            raise CapacityError("max_run_workers exceeds total_workers")
        slot_paths = _slot_paths_locked(self.state_root, total)
        self.slot_paths = slot_paths
        self.total_workers = total
        self.max_run_workers = max_run
        return slot_paths, max_run

    def acquire(
        self,
        workers: int = 1,
        *,
        cancel: Event | None = None,
        poll_seconds: float = 0.02,
        inherited: CapacityLease | None = None,
    ) -> CapacityLease:
        """Acquire all slots atomically, or wait/cancel without partial state."""

        workers = _validate_workers(workers, field="workers")
        if poll_seconds <= 0:
            raise CapacityError("poll_seconds must be positive")

        if inherited is not None:
            if inherited.manager is not self or not inherited.active:
                raise CapacityError("nested allocation lacks a live inherited lease")
            if workers > inherited.workers:
                raise CapacityError("nested allocation exceeds inherited lease")
            return CapacityLease(self, workers, waited_seconds=0.0, _borrowed=True, _parent=inherited)

        started = time.monotonic()
        while True:
            if cancel is not None and cancel.is_set():
                raise CapacityCancelled("capacity acquisition cancelled")
            fds: list[int] = []
            try:
                # Configuration holds this lock while checking every old slot
                # and replacing capacity.json.  Keeping it through slot
                # acquisition closes the construction/acquisition migration
                # race, including a shrinking restoration.
                with _locked_state(self.state_root, cancel=cancel):
                    slot_paths, max_run_workers = self._refresh_locked()
                    if workers > max_run_workers:
                        raise CapacityError("requested workers exceed per-run capacity")
                    for slot_path in slot_paths:
                        try:
                            fd = _open_private(slot_path, os.O_RDWR | os.O_CLOEXEC)
                        except MetricsError as exc:
                            raise CapacityError("capacity slot is no longer private") from exc
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except OSError as exc:
                            os.close(fd)
                            if exc.errno in (errno.EACCES, errno.EAGAIN):
                                continue
                            raise
                        fds.append(fd)
                        if len(fds) == workers:
                            break
                    if len(fds) < workers:
                        raise BlockingIOError
            except BlockingIOError:
                for fd in fds:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                if cancel is not None:
                    cancel.wait(poll_seconds)
                else:
                    time.sleep(poll_seconds)
                continue
            except BaseException:
                for fd in fds:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                raise
            return CapacityLease(self, workers, time.monotonic() - started, fds)


__all__ = [
    "CapacityCancelled",
    "CapacityError",
    "CapacityLease",
    "CapacityManager",
    "PROVISIONAL_MAX_RUN_WORKERS",
    "PROVISIONAL_TOTAL_WORKERS",
    "configure_host_capacity",
]


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Configure the canonical LifeOS development test capacity.")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="replace the canonical budget only while all slots are idle")
    configure.add_argument("--total-workers", type=int, required=True)
    configure.add_argument("--max-run-workers", type=int, default=PROVISIONAL_MAX_RUN_WORKERS)
    args = parser.parse_args(argv)
    try:
        configure_host_capacity(args.total_workers, max_run_workers=args.max_run_workers)
    except CapacityError as exc:
        parser.exit(1, f"capacity configuration failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
