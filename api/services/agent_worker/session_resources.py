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


def scratch_dir_for(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)
    return Path(tempfile.gettempdir()) / "lifeos-agent-worker" / safe


def ensure_session_scratch(session_id: str) -> Path:
    path = scratch_dir_for(session_id)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def scratch_env(session_id: str) -> dict[str, str]:
    value = str(ensure_session_scratch(session_id))
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def cleanup_session_scratch(session_id: str) -> None:
    shutil.rmtree(scratch_dir_for(session_id), ignore_errors=True)


def active_scratch_env() -> dict[str, str] | None:
    return _active_scratch_env.get()


@contextmanager
def session_scratch_context(session_id: str):
    token = _active_scratch_env.set(scratch_env(session_id))
    try:
        yield
    finally:
        _active_scratch_env.reset(token)
