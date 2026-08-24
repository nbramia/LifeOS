"""Regression coverage for the #652 autouse isolation fixtures in
``conftest.py`` (``_isolate_session_store_db`` / `_isolate_transcript_store_dir`).

Nothing here exercises the isolation fixtures explicitly — they're autouse,
so they're already active for every test in the suite, including these.
What's asserted is the thing #652 was actually about: a bare
``SessionStore()``/``TranscriptStore()`` built under test must never resolve
to the real repo-anchored default, because that's exactly what let the
Hermes envelope path (`_build_envelope` -> `_resolve_caller_session_id`)
write real rows into the operator's live `data/agent_sessions.db`.

``SessionStore``/``TranscriptStore`` are imported LOCALLY inside each test,
not at module top — a top-level import binds the name during collection,
before any autouse fixture has run, which would test the wrong (real,
unpatched) class. This mirrors why `tests/test_agent_worker_store_paths.py`
imports the same classes at module top: it deliberately wants the real
class to assert the *production* default is repo-anchored, which is a
different concern from this file's.
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
def test_bare_session_store_does_not_resolve_to_the_real_db():
    from api.services.agent_worker.session_store import DEFAULT_DB_PATH, SessionStore

    store = SessionStore()
    assert store.db_path != DEFAULT_DB_PATH


@pytest.mark.unit
def test_bare_transcript_store_does_not_resolve_to_the_real_dir():
    from api.services.agent_worker.transcript_store import (
        DEFAULT_TRANSCRIPTS_DIR,
        TranscriptStore,
    )

    store = TranscriptStore()
    assert store.dir != DEFAULT_TRANSCRIPTS_DIR


@pytest.mark.unit
def test_explicit_session_store_path_override_still_works(tmp_path):
    """Acceptance criterion: a test that passes its own path must behave
    exactly as today — the isolation fixture only changes the *default*."""
    from api.services.agent_worker.session_store import SessionStore

    explicit_path = tmp_path / "explicit_sessions.db"
    store = SessionStore(db_path=explicit_path)
    assert store.db_path == explicit_path


@pytest.mark.unit
def test_explicit_class_override_wins_over_the_autouse_default(tmp_path, monkeypatch):
    """An autouse default must not fight a test's own explicit monkeypatch
    of the same class name — the exact composition
    `tests/test_hermes_proxy.py::agent_session_store` relies on."""
    from api.services.agent_worker import session_store as session_store_mod

    sentinel = session_store_mod.SessionStore(str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(session_store_mod, "SessionStore", lambda *a, **kw: sentinel)

    # Re-import the (now patched) name the same way application code does —
    # locally, at call time — so this actually exercises the patched class
    # rather than a name bound before the monkeypatch above ran.
    from api.services.agent_worker.session_store import SessionStore

    assert SessionStore() is sentinel
    assert SessionStore(db_path="/some/other/path.db") is sentinel
