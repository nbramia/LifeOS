"""Regression tests for SessionStore/TranscriptStore default path anchoring
(#640 review).

`mcp_server.py` is run as a stdio MCP child by CLI agents (codex/claude_code
with a `-C` working dir) AND by Hermes (its own cwd, e.g. `~/.hermes`) —
neither is the repo root. `_handle_inter_agent` already protects itself by
passing explicit, repo-anchored `AGENT_SESSIONS_DB`/`AGENT_TRANSCRIPTS_DIR`
paths (see `tests/test_agent_worker_mcp_exposure.py`), so that dispatch path
was never actually broken. But `SessionStore`/`TranscriptStore`'s own
DEFAULT (used by every other caller: API routes, the worker, scripts) was a
bare `Path("data/...")`, silently correct only because every one of those
callers happens to run with cwd already at the repo root. These tests pin
the hardened defaults directly, independent of any one caller's cwd
discipline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.session_store import DEFAULT_DB_PATH, SessionStore
from api.services.agent_worker.transcript_store import (
    DEFAULT_TRANSCRIPTS_DIR,
    TranscriptStore,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.unit
def test_session_store_default_db_path_is_repo_anchored():
    assert DEFAULT_DB_PATH.is_absolute()
    assert DEFAULT_DB_PATH == _REPO_ROOT / "data" / "agent_sessions.db"


@pytest.mark.unit
def test_transcript_store_default_dir_is_repo_anchored():
    assert DEFAULT_TRANSCRIPTS_DIR.is_absolute()
    assert DEFAULT_TRANSCRIPTS_DIR == _REPO_ROOT / "data" / "agent_transcripts"


@pytest.mark.unit
def test_session_store_default_resolves_same_file_regardless_of_cwd(tmp_path, monkeypatch):
    """The exact bug shape reported against #640: a bare `SessionStore()`
    instantiated from a directory other than the repo root must still land
    on the real repo db, not a phantom sibling under that other directory."""
    other_cwd = tmp_path / "not-the-repo"
    other_cwd.mkdir()

    monkeypatch.chdir(_REPO_ROOT)
    from_repo_root = SessionStore().db_path

    monkeypatch.chdir(other_cwd)
    from_elsewhere = SessionStore().db_path

    assert from_repo_root == from_elsewhere
    assert from_elsewhere.is_absolute()
    # And it must not have created a phantom copy under the foreign cwd.
    assert not (other_cwd / "data" / "agent_sessions.db").exists()


@pytest.mark.unit
def test_transcript_store_default_resolves_same_dir_regardless_of_cwd(tmp_path, monkeypatch):
    other_cwd = tmp_path / "not-the-repo"
    other_cwd.mkdir()

    monkeypatch.chdir(_REPO_ROOT)
    from_repo_root = TranscriptStore().dir

    monkeypatch.chdir(other_cwd)
    from_elsewhere = TranscriptStore().dir

    assert from_repo_root == from_elsewhere
    assert from_elsewhere.is_absolute()
    assert not (other_cwd / "data" / "agent_transcripts").exists()


@pytest.mark.unit
def test_session_store_explicit_relative_path_still_resolves_against_cwd(tmp_path, monkeypatch):
    """The anchored DEFAULT must not change behavior for a caller that passes
    its own relative path explicitly (e.g. a test fixture sandboxing into
    tmp_path) — only the no-argument default moved."""
    monkeypatch.chdir(tmp_path)
    store = SessionStore(db_path="explicit/sessions.db")
    assert store.db_path == Path("explicit/sessions.db")
    assert (tmp_path / "explicit" / "sessions.db").parent.exists()


@pytest.mark.unit
def test_transcript_store_explicit_relative_dir_still_resolves_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = TranscriptStore(transcripts_dir="explicit/transcripts")
    assert store.dir == Path("explicit/transcripts")
    assert (tmp_path / "explicit" / "transcripts").exists()
