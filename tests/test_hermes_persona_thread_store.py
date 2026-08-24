"""Tests for the Hermes-Telegram reply-thread persona mapping
(api/services/hermes_persona_thread_store.py, #644 follow-up).

Endpoint-level behavior (resolution order, the register-persona-message
call) lives in tests/test_hermes_proxy.py; this file covers the store in
isolation: per-chat scoping, TTL expiry, and the row cap.
"""
import sqlite3

import pytest

from api.services.hermes_persona_thread_store import HermesPersonaThreadStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return HermesPersonaThreadStore(db_path=str(tmp_path / "threads.db"))


def test_record_then_lookup_round_trips(store):
    store.record("chat-1", "msg-1", "doctor")
    assert store.lookup("chat-1", "msg-1") == "doctor"


def test_lookup_miss_returns_none_not_an_error(store):
    assert store.lookup("chat-1", "does-not-exist") is None


def test_scoped_per_chat_no_cross_chat_collision(store):
    # Telegram message ids are unique only within a chat -- the same
    # message_id in two different chats must resolve independently.
    store.record("chat-1", "msg-1", "doctor")
    store.record("chat-2", "msg-1", "fitness")
    assert store.lookup("chat-1", "msg-1") == "doctor"
    assert store.lookup("chat-2", "msg-1") == "fitness"
    # A chat that never recorded this message_id gets nothing, even though
    # the same id exists in another chat.
    assert store.lookup("chat-3", "msg-1") is None


def test_record_is_idempotent_and_updates_persona(store):
    store.record("chat-1", "msg-1", "doctor")
    store.record("chat-1", "msg-1", "fitness")
    assert store.lookup("chat-1", "msg-1") == "fitness"


def test_no_message_content_is_ever_stored(store, tmp_path):
    # Only ids and a persona label belong in this table -- confirm the
    # schema itself has no room for message text, not just that callers
    # never pass it.
    store.record("chat-1", "msg-1", "doctor")
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(persona_threads)")}
    assert columns == {"chat_id", "message_id", "persona_id", "created_at"}


def test_expired_row_is_a_lookup_miss_not_an_error(store, monkeypatch):
    import api.services.hermes_persona_thread_store as mod

    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    store.record("chat-1", "msg-1", "doctor")
    assert store.lookup("chat-1", "msg-1") == "doctor"

    # Advance past the TTL -- the row is still physically present (no sweep
    # has run) but must read back as a miss, the same as one that was never
    # recorded, so the caller's fallback path can't tell them apart.
    fake_time[0] += mod._TTL_SECONDS + 1
    assert store.lookup("chat-1", "msg-1") is None


def test_record_prunes_expired_rows(store, monkeypatch):
    import api.services.hermes_persona_thread_store as mod

    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    store.record("chat-1", "old-msg", "doctor")
    fake_time[0] += mod._TTL_SECONDS + 1
    # A later record() call sweeps out the now-expired row as a side effect.
    store.record("chat-1", "new-msg", "fitness")

    with sqlite3.connect(store.db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM persona_threads WHERE message_id = 'old-msg'"
        ).fetchone()
    assert count == 0


def test_record_caps_total_rows_evicting_oldest_first(store, monkeypatch):
    import api.services.hermes_persona_thread_store as mod

    monkeypatch.setattr(mod, "_MAX_ROWS", 3)
    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    for i in range(3):
        store.record("chat-1", f"msg-{i}", "doctor")
        fake_time[0] += 1

    # A 4th row pushes the table over the cap -- the oldest (msg-0) is
    # evicted, not the newest.
    store.record("chat-1", "msg-3", "doctor")

    assert store.lookup("chat-1", "msg-0") is None
    assert store.lookup("chat-1", "msg-1") == "doctor"
    assert store.lookup("chat-1", "msg-2") == "doctor"
    assert store.lookup("chat-1", "msg-3") == "doctor"
