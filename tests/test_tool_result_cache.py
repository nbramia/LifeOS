"""Tests for the per-session tool result cache (#139 §4)."""
from __future__ import annotations

import pytest

from api.services.agent_worker.tool_result_cache import SessionToolResultCache


pytestmark = pytest.mark.unit


def test_get_returns_none_when_empty():
    cache = SessionToolResultCache()
    assert cache.get("sess_1", "lifeos_calendar_upcoming", {"days": 7}) is None


def test_put_then_get_returns_stored_result():
    cache = SessionToolResultCache()
    result = {"events": [{"title": "standup"}]}
    cache.put("sess_1", "lifeos_calendar_upcoming", {"days": 7}, result)
    assert cache.get("sess_1", "lifeos_calendar_upcoming", {"days": 7}) == result


def test_args_key_is_order_independent():
    """`{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` must collide."""
    cache = SessionToolResultCache()
    cache.put("sess_1", "lifeos_search", {"a": 1, "b": 2}, {"hits": [1]})
    # Same logical args, different insertion order.
    assert cache.get("sess_1", "lifeos_search", {"b": 2, "a": 1}) == {"hits": [1]}


def test_different_args_do_not_collide():
    cache = SessionToolResultCache()
    cache.put("sess_1", "lifeos_search", {"q": "X"}, {"hits": ["x"]})
    assert cache.get("sess_1", "lifeos_search", {"q": "Y"}) is None


def test_different_sessions_do_not_share():
    """Session A's cache MUST NOT leak into session B's request."""
    cache = SessionToolResultCache()
    cache.put("sess_A", "lifeos_calendar_upcoming", {"days": 7}, {"a": 1})
    assert cache.get("sess_B", "lifeos_calendar_upcoming", {"days": 7}) is None


def test_ttl_expires_stale_entries():
    """An entry older than ttl_seconds returns None."""
    fake_now = [1000.0]
    cache = SessionToolResultCache(ttl_seconds=60, clock=lambda: fake_now[0])
    cache.put("sess_1", "lifeos_search", {"q": "X"}, {"hits": [1]})
    # Within TTL — still hits.
    fake_now[0] = 1059.0
    assert cache.get("sess_1", "lifeos_search", {"q": "X"}) == {"hits": [1]}
    # Past TTL — miss, and the stale entry is dropped.
    fake_now[0] = 1061.0
    assert cache.get("sess_1", "lifeos_search", {"q": "X"}) is None
    assert len(cache) == 0


def test_lru_eviction_when_capacity_exceeded():
    """Oldest entry is evicted when capacity fills."""
    cache = SessionToolResultCache(max_entries=2)
    cache.put("sess_1", "tool_a", {}, "A")
    cache.put("sess_1", "tool_b", {}, "B")
    cache.put("sess_1", "tool_c", {}, "C")
    # Oldest (tool_a) was evicted.
    assert cache.get("sess_1", "tool_a", {}) is None
    assert cache.get("sess_1", "tool_b", {}) == "B"
    assert cache.get("sess_1", "tool_c", {}) == "C"


def test_get_touches_lru_so_recently_used_survives_eviction():
    """A get on tool_a should refresh its position so the next put evicts
    tool_b instead."""
    cache = SessionToolResultCache(max_entries=2)
    cache.put("sess_1", "tool_a", {}, "A")
    cache.put("sess_1", "tool_b", {}, "B")
    # Touch tool_a — now tool_b is the oldest.
    cache.get("sess_1", "tool_a", {})
    cache.put("sess_1", "tool_c", {}, "C")
    assert cache.get("sess_1", "tool_a", {}) == "A"
    assert cache.get("sess_1", "tool_b", {}) is None
    assert cache.get("sess_1", "tool_c", {}) == "C"


def test_clear_session_drops_only_that_sessions_entries():
    cache = SessionToolResultCache()
    cache.put("sess_A", "t1", {}, "A1")
    cache.put("sess_A", "t2", {}, "A2")
    cache.put("sess_B", "t1", {}, "B1")
    cleared = cache.clear_session("sess_A")
    assert cleared == 2
    assert cache.get("sess_A", "t1", {}) is None
    assert cache.get("sess_A", "t2", {}) is None
    # Other session untouched.
    assert cache.get("sess_B", "t1", {}) == "B1"


def test_empty_session_id_bypasses_cache():
    """An empty session_id never caches or retrieves (defensive: callers that
    don't know their session shouldn't share cached results)."""
    cache = SessionToolResultCache()
    cache.put("", "lifeos_search", {"q": "X"}, "leaked")
    assert cache.get("", "lifeos_search", {"q": "X"}) is None
    assert len(cache) == 0
