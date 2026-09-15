"""Tests for the Hermes question anchor table
(api/services/hermes_question_thread_store.py).

Covers the store in isolation: per-chat scoping, TTL expiry, the row cap,
and the guarantee that no question text lands in the table. Endpoint-level
behavior lives in tests/test_hermes_proxy.py.
"""
import sqlite3

import pytest

from api.services.hermes_question_thread_store import HermesQuestionThreadStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return HermesQuestionThreadStore(db_path=str(tmp_path / "question_threads.db"))


def test_record_then_lookup_round_trips(store):
    store.record("chat-1", "590", 42)
    assert store.lookup("chat-1", "590") == 42


def test_lookup_miss_returns_none_not_an_error(store):
    assert store.lookup("chat-1", "does-not-exist") is None


def test_scoped_per_chat_no_cross_chat_collision(store):
    store.record("chat-1", "590", 42)
    store.record("chat-2", "590", 77)
    assert store.lookup("chat-1", "590") == 42
    assert store.lookup("chat-2", "590") == 77
    assert store.lookup("chat-3", "590") is None


def test_record_is_idempotent_and_updates_the_question(store):
    store.record("chat-1", "590", 42)
    store.record("chat-1", "590", 77)
    assert store.lookup("chat-1", "590") == 77


def test_no_question_text_is_ever_stored(store):
    store.record("chat-1", "590", 42)
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(question_threads)")}
    assert columns == {"chat_id", "message_id", "question_id", "created_at"}


def test_expired_row_is_a_lookup_miss_not_an_error(store, monkeypatch):
    import api.services.hermes_question_thread_store as mod

    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    store.record("chat-1", "590", 42)
    assert store.lookup("chat-1", "590") == 42

    fake_time[0] += mod._TTL_SECONDS + 1
    assert store.lookup("chat-1", "590") is None


def test_record_prunes_expired_rows(store, monkeypatch):
    import api.services.hermes_question_thread_store as mod

    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    store.record("chat-1", "590", 42)
    fake_time[0] += mod._TTL_SECONDS + 1
    store.record("chat-1", "591", 43)

    with sqlite3.connect(store.db_path) as conn:
        remaining = {row[0] for row in conn.execute("SELECT message_id FROM question_threads")}
    assert remaining == {"591"}


def test_table_is_capped_oldest_evicted_first(store, monkeypatch):
    import api.services.hermes_question_thread_store as mod

    monkeypatch.setattr(mod, "_MAX_ROWS", 3)
    fake_time = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_time[0])

    for index in range(5):
        fake_time[0] += 1
        store.record("chat-1", str(index), index)

    assert store.lookup("chat-1", "0") is None
    assert store.lookup("chat-1", "1") is None
    assert store.lookup("chat-1", "4") == 4
