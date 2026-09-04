"""
Tests for the CRM aggregate response cache (#876).

`AggregateCache` memoizes a route handler's return value, keyed on its
resolved query parameters plus both databases' `PRAGMA data_version`
counters, so a second identical request is served instantly until either
database is written to (from any connection, in this process or another) or
a short TTL backstop expires. These tests build isolated `AggregateCache`
instances against temporary SQLite files -- the same pattern
`tests/test_person_entity.py` uses for `PersonEntityStore(db_path)` -- rather
than the process-wide `get_aggregate_cache()` singleton, so nothing here
touches real data or depends on run order relative to other test files.
"""
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from api.services.aggregate_cache import AggregateCache

pytestmark = pytest.mark.unit


def _make_db(path) -> str:
    """A minimal but valid SQLite file at `path` -- AggregateCache only ever
    reads PRAGMA data_version from it, so the schema doesn't matter."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture
def cache(tmp_path):
    """A fresh, isolated AggregateCache backed by two temporary databases."""
    crm_db = _make_db(tmp_path / "crm.db")
    interactions_db = _make_db(tmp_path / "interactions.db")
    return AggregateCache(crm_db_path=crm_db, interactions_db_path=interactions_db)


def _commit_external_write(db_path: str) -> None:
    """Write to `db_path` from a brand-new connection -- models a write from
    another process/request, which is exactly what PRAGMA data_version is
    meant to detect."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO marker (value) VALUES ('external-write')")
        conn.commit()
    finally:
        conn.close()


class TestCacheHit:
    """A second call with identical parameters is served from cache, fast,
    with an identical body -- and does not recompute."""

    def test_second_call_is_fast_and_identical_and_does_not_recompute(self, cache):
        calls = []

        @cache.cached()
        def endpoint(days_back: int = 30, trend_period: str = "quarter"):
            calls.append(1)
            return {"days_back": days_back, "trend_period": trend_period, "call_number": len(calls)}

        first = endpoint(days_back=30, trend_period="quarter")

        start = time.perf_counter()
        second = endpoint(days_back=30, trend_period="quarter")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert second == first
        assert len(calls) == 1, "second call must be served from cache, not recomputed"
        assert elapsed_ms < 20, f"cache hit took {elapsed_ms:.2f}ms, expected under 20ms"


class TestPerParameterKeys:
    """Different parameters get independent cache entries; revisiting an
    earlier parameter set still hits its own cached entry."""

    def test_distinct_parameters_are_not_conflated(self, cache):
        calls = []

        @cache.cached()
        def endpoint(x: int = 0):
            calls.append(x)
            return {"x": x, "call_number": len(calls)}

        result_a = endpoint(x=1)
        result_b = endpoint(x=2)
        assert result_a != result_b
        assert len(calls) == 2

        # Revisiting x=1's parameters hits its own cached entry.
        result_a_again = endpoint(x=1)
        assert result_a_again == result_a
        assert len(calls) == 2


class TestDataVersionInvalidation:
    """A commit from a separate SQLite connection to either database
    invalidates every cached entry (the next lookup for an affected key
    misses and recomputes)."""

    def test_external_write_to_crm_db_invalidates(self, cache):
        calls = []

        @cache.cached()
        def endpoint():
            calls.append(1)
            return {"call_number": len(calls)}

        first = endpoint()
        assert len(calls) == 1

        # Unchanged databases: still a hit.
        assert endpoint() == first
        assert len(calls) == 1

        _commit_external_write(cache.crm_db_path)

        second = endpoint()
        assert len(calls) == 2, "a crm.db commit from another connection must invalidate the cache"
        assert second != first

    def test_external_write_to_interactions_db_invalidates(self, cache):
        calls = []

        @cache.cached()
        def endpoint():
            calls.append(1)
            return {"call_number": len(calls)}

        endpoint()
        assert len(calls) == 1

        _commit_external_write(cache.interactions_db_path)

        endpoint()
        assert len(calls) == 2, (
            "an interactions.db commit from another connection must invalidate the cache"
        )

    def test_ttl_expiry_forces_recompute_even_with_unchanged_data_version(self, cache):
        """The TTL is a backstop bound, independent of data_version."""
        calls = []

        @cache.cached(ttl_seconds=0.01)
        def endpoint():
            calls.append(1)
            return {"call_number": len(calls)}

        endpoint()
        time.sleep(0.05)
        endpoint()
        assert len(calls) == 2


class TestErrorsAreNeverCached:
    """A raised exception (an HTTPException for a non-200 response, or any
    other) is never cached -- the next identical call re-executes the
    handler rather than replaying the failure or, worse, a stale success."""

    def test_raised_http_exception_is_not_cached(self, cache):
        calls = []

        @cache.cached()
        def endpoint(should_fail: bool = True):
            calls.append(1)
            if should_fail:
                raise HTTPException(status_code=400, detail="bad request")
            return {"ok": True}

        with pytest.raises(HTTPException):
            endpoint(should_fail=True)
        with pytest.raises(HTTPException):
            endpoint(should_fail=True)

        assert len(calls) == 2, "an error response must never be cached"
        assert cache.stats()["entries"] == 0

    def test_success_after_a_failed_call_is_cached_normally(self, cache):
        calls = []

        @cache.cached()
        def endpoint(should_fail: bool = False):
            calls.append(1)
            if should_fail:
                raise HTTPException(status_code=500, detail="boom")
            return {"ok": True, "call_number": len(calls)}

        with pytest.raises(HTTPException):
            endpoint(should_fail=True)

        first = endpoint(should_fail=False)
        second = endpoint(should_fail=False)
        assert second == first
        assert len(calls) == 2  # one failure + one success; the retry hit cache


class TestBounds:
    """The cache never grows past its configured entry-count or byte bound."""

    def test_entry_count_is_bounded(self, tmp_path):
        crm_db = _make_db(tmp_path / "crm.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        cache = AggregateCache(crm_db_path=crm_db, interactions_db_path=interactions_db,
                                max_entries=3, max_total_bytes=10 * 1024 * 1024)

        @cache.cached()
        def endpoint(x: int):
            return {"x": x, "padding": "a" * 100}

        for i in range(10):
            endpoint(x=i)

        assert cache.stats()["entries"] <= 3

    def test_total_bytes_is_bounded(self, tmp_path):
        crm_db = _make_db(tmp_path / "crm.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        # Each entry is roughly 1-2KB; a 5KB cap should hold only a few.
        cache = AggregateCache(crm_db_path=crm_db, interactions_db_path=interactions_db,
                                max_entries=1000, max_total_bytes=5 * 1024)

        @cache.cached()
        def endpoint(x: int):
            return {"x": x, "padding": "a" * 1000}

        for i in range(50):
            endpoint(x=i)

        stats = cache.stats()
        assert stats["total_bytes"] <= 5 * 1024
        assert stats["entries"] < 50, "the byte bound must have evicted older entries"

    def test_clear_empties_the_cache(self, cache):
        @cache.cached()
        def endpoint():
            return {"ok": True}

        endpoint()
        assert cache.stats()["entries"] == 1

        cache.clear()
        assert cache.stats() == {"entries": 0, "total_bytes": 0}


class TestThreadSafety:
    """Concurrent calls (same and different parameters) from many threads
    never raise or corrupt the cache -- the lock genuinely serializes access
    to the shared dict and both data_version connections."""

    def test_concurrent_calls_do_not_raise_or_corrupt_state(self, cache):
        calls = []
        call_lock = threading.Lock()
        errors = []

        @cache.cached()
        def endpoint(x: int):
            with call_lock:
                calls.append(1)
            return {"x": x}

        def worker(thread_id):
            try:
                for i in range(30):
                    endpoint(x=(thread_id + i) % 5)
            except Exception as exc:  # noqa: BLE001 -- want to see any failure
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent access raised: {errors}"
        # At most 5 distinct parameter values were ever requested.
        assert cache.stats()["entries"] <= 5


class TestFastAPIIntegration:
    """The decorator works through FastAPI's own request handling, not just
    as a plain Python function call: Query() parameters are still resolved
    (functools.wraps' __wrapped__ preserves the signature FastAPI inspects),
    and a real HTTP round trip hits the cache on a repeat request."""

    @pytest.fixture
    def api_client(self, cache):
        calls = []
        app = FastAPI()

        @app.get("/widgets")
        @cache.cached()
        def list_widgets(count: int = Query(default=1), label: str = Query(default="a")):
            calls.append(1)
            return {"count": count, "label": label, "call_number": len(calls)}

        return TestClient(app), calls

    def test_query_params_still_resolve_and_repeat_request_hits_cache(self, api_client):
        client, calls = api_client

        first = client.get("/widgets?count=5&label=x")
        assert first.status_code == 200
        assert first.json()["count"] == 5
        assert first.json()["label"] == "x"
        assert len(calls) == 1

        second = client.get("/widgets?count=5&label=x")
        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(calls) == 1, "identical request must be served from cache"

        # A different query param is an independent entry.
        third = client.get("/widgets?count=5&label=y")
        assert third.status_code == 200
        assert third.json()["label"] == "y"
        assert len(calls) == 2
