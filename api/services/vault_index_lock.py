"""Advisory lock keeping the vault index job and the nightly vault reindex apart.

Both rewrite the same index state and the same chunks. This is ``flock`` on a
fixed host-wide file, so the kernel releases it when the holder exits, however
it exits. It is separate from ``sync.lock``, which every sync holds shared and
so excludes only auto-deploy.
"""
import fcntl
import logging
import time
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

VAULT_INDEX_LOCK_PATH = Path.home() / ".lifeos" / "vault_index.lock"

_POLL_SECONDS = 1.0


def try_flock(path: Path, shared: bool = False) -> IO | None:
    """Take a flock without waiting.

    Returns:
        The open file holding the lock, which the caller keeps referenced for as
        long as it needs the lock, or None when another process holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a")
    try:
        fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def wait_for_vault_index_lock(timeout_seconds: float) -> IO | None:
    """Wait for the vault index lock.

    Returns:
        The open file holding the lock, or None if it stayed held for the whole
        timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    logged_wait = False
    while True:
        handle = try_flock(VAULT_INDEX_LOCK_PATH)
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            return None
        if not logged_wait:
            logger.info(f"Vault index job is running; waiting up to {timeout_seconds:.0f}s for it to finish")
            logged_wait = True
        time.sleep(_POLL_SECONDS)
