"""Private scratch lifecycle for board-dispatched agent sessions."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_active_scratch_env: ContextVar[dict[str, str] | None] = ContextVar(
    "agent_session_scratch_env", default=None,
)

_SESSION_ID_RE = re.compile(r"sess_[0-9a-f]{16}\Z")


def scratch_dir_for(session_id: str) -> Path:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid internal session id: {session_id!r}")
    container = (Path(tempfile.gettempdir()) / "lifeos-agent-worker").resolve()
    path = (container / session_id).resolve()
    if path.parent != container:
        raise ValueError(f"session scratch path escaped its container: {session_id!r}")
    return path


def ensure_session_scratch(session_id: str) -> Path:
    path = scratch_dir_for(session_id)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def scratch_env(session_id: str) -> dict[str, str]:
    value = str(ensure_session_scratch(session_id))
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def cleanup_session_scratch(session_id: str, *, host: str | None = None) -> None:
    from api.services.agent_worker.git_worktree import WorktreeError, resolve_runner_for_host

    path = scratch_dir_for(session_id)
    try:
        runner = resolve_runner_for_host(host)
    except WorktreeError:
        runner = None
    if runner is not None:
        runner(["rm", "-rf", "--", str(path)])
    if host:
        shutil.rmtree(path, ignore_errors=True)


def active_scratch_env() -> dict[str, str] | None:
    return _active_scratch_env.get()


@contextmanager
def session_scratch_context(session_id: str):
    token = _active_scratch_env.set(scratch_env(session_id))
    try:
        yield
    finally:
        _active_scratch_env.reset(token)
