"""Small cross-process guard for idempotent Markdown operation creation."""
from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_thread_state = threading.local()


@contextmanager
def exclusive_operation_lock(path: Path) -> Iterator[None]:
    """Hold a re-entrant, exclusive process-safe operation lock.

    Task/project mutations compose public TaskManager methods (for example a
    first child attachment pauses its parent before creating the child).  A
    nested ``flock`` on a second file descriptor can wait on the process's own
    outer lock, so keep one descriptor per path for the current thread and let
    only the outermost scope acquire/release the OS lock.
    """
    resolved = str(path.resolve())
    held = getattr(_thread_state, "held", None)
    if held is None:
        held = _thread_state.held = {}
    if resolved in held:
        held[resolved] += 1
        try:
            yield
        finally:
            held[resolved] -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+b", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[resolved] = 1
            try:
                yield
            finally:
                held.pop(resolved, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(fd)
