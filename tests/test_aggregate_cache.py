"""
Tests for the CRM aggregate response cache (#876, #917).

`AggregateCache` memoizes a route handler's return value, keyed on its
resolved query parameters, invalidated whenever either watched database's
`PRAGMA data_version` counter changes, so a second identical request is
served instantly until either database is written to (from any connection,
in this process or another) or a short TTL backstop expires. These tests
build isolated `AggregateCache` instances against temporary SQLite files --
the same pattern `tests/test_person_entity.py` uses for
`PersonEntityStore(db_path)` -- rather than the process-wide
`get_aggregate_cache()` singleton, so nothing here touches real data or
depends on run order relative to other test files.
"""
import os
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from api.services.aggregate_cache import AggregateCache, _default_crm_db_paths

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
    return AggregateCache(crm_db_paths=[crm_db], interactions_db_path=interactions_db)


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


def _best_of(fn, n=3):
    """Fastest of `n` timed calls to `fn()`, in milliseconds -- reduces
    flakiness from an occasional scheduling hiccup on a loaded box (#917
    review finding 10) versus asserting on a single sample."""
    best = None
    result = None
    for _ in range(n):
        start = time.perf_counter()
        result = fn()
        elapsed_ms = (time.perf_counter() - start) * 1000
        if best is None or elapsed_ms < best:
            best = elapsed_ms
    return best, result


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

        best_ms, second = _best_of(lambda: endpoint(days_back=30, trend_period="quarter"))

        assert second == first
        assert len(calls) == 1, "second call must be served from cache, not recomputed"
        assert best_ms < 20, f"best-of-3 cache hit took {best_ms:.2f}ms, expected under 20ms"


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

        _commit_external_write(cache.crm_db_paths[0])

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

    def test_version_change_drops_entries_outright_not_just_orphans_them(self, cache):
        """The data_version pair is a generation stamp, not part of the
        cache key: a write must clear stale entries immediately rather than
        leaving them counted against the entry/byte bounds until LRU
        pressure happens to reclaim them (#917 review finding 8)."""
        @cache.cached()
        def endpoint_a(x: int):
            return {"x": x}

        @cache.cached()
        def endpoint_b(y: int):
            return {"y": y}

        endpoint_a(x=1)
        endpoint_b(y=2)
        assert cache.stats()["entries"] == 2

        _commit_external_write(cache.crm_db_paths[0])

        # The generation check happens lazily, on the next call that reads
        # data_version -- but that one call must drop *every* stale entry
        # outright (not just make its own key's old version unreachable),
        # so calling endpoint_a again leaves only its fresh entry, not two.
        endpoint_a(x=1)
        assert cache.stats()["entries"] == 1


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
        cache = AggregateCache(crm_db_paths=[crm_db], interactions_db_path=interactions_db,
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
        cache = AggregateCache(crm_db_paths=[crm_db], interactions_db_path=interactions_db,
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

    def test_entry_larger_than_cap_is_skipped_not_flushed(self, tmp_path):
        """An entry that alone exceeds the byte cap must be skipped, not
        stored at the cost of evicting everything else already cached
        (#917 review finding 8)."""
        crm_db = _make_db(tmp_path / "crm.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        cache = AggregateCache(crm_db_paths=[crm_db], interactions_db_path=interactions_db,
                                max_entries=1000, max_total_bytes=1000)

        @cache.cached()
        def small_endpoint(x: int):
            return {"x": x}

        @cache.cached()
        def huge_endpoint():
            return {"padding": "a" * 5000}

        small_endpoint(x=1)
        small_endpoint(x=2)
        small_endpoint(x=3)
        entries_before = cache.stats()["entries"]
        assert entries_before == 3

        huge_endpoint()  # far bigger than max_total_bytes

        stats = cache.stats()
        assert stats["entries"] == entries_before, (
            "an oversized entry must be skipped, not flush the existing cache"
        )
        assert stats["total_bytes"] <= 1000


class TestMutationSafety:
    """A caller mutating its own returned object can never poison the cache
    for a later caller, and a cache hit can never hand out a reference a
    concurrent caller could poison either (#917 review finding 5)."""

    def test_mutating_a_returned_dict_does_not_poison_the_cache(self, cache):
        @cache.cached()
        def endpoint():
            return {"items": ["original"]}

        first = endpoint()
        first["items"].append("MUTATED-BY-CALLER")
        first["items"][0] = "ALSO-MUTATED"

        second = endpoint()
        assert second == {"items": ["original"]}, (
            f"cached entry was poisoned by a caller mutation: {second}"
        )

    def test_two_hits_return_independent_objects(self, cache):
        @cache.cached()
        def endpoint():
            return {"items": ["original"]}

        endpoint()  # populate the cache
        a = endpoint()
        b = endpoint()
        assert a == b
        assert a is not b, "each call must return its own independent copy"


class TestVersionReadFailure:
    """A PRAGMA data_version read failure (missing file, mid-replacement
    file, any other sqlite3.Error) must never turn a request that would
    otherwise succeed into a failure -- it falls through to computing
    uncached (#917 review finding 1)."""

    def test_missing_crm_db_directory_falls_through_to_uncached(self, tmp_path):
        missing_path = str(tmp_path / "does" / "not" / "exist" / "crm.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        cache = AggregateCache(crm_db_paths=[missing_path], interactions_db_path=interactions_db)

        calls = []

        @cache.cached()
        def endpoint():
            calls.append(1)
            return {"ok": True, "call_number": len(calls)}

        # Must not raise -- this is the "endpoint still returns 200" case.
        result = endpoint()
        assert result == {"ok": True, "call_number": 1}
        assert len(calls) == 1

        # Since the version read can't succeed, nothing is ever cached --
        # a second call recomputes too, rather than the process crashing or
        # (worse) serving something stale forever.
        result2 = endpoint()
        assert result2 == {"ok": True, "call_number": 2}
        assert len(calls) == 2
        assert cache.stats()["entries"] == 0

    def test_recovers_once_the_file_appears(self, tmp_path):
        """The connection is dropped and reopened on the next call after a
        failure, so a file created after the fact (e.g. by a sync process)
        is picked up without restarting the server."""
        crm_db_path = str(tmp_path / "crm.db")  # does not exist yet
        interactions_db = _make_db(tmp_path / "interactions.db")
        cache = AggregateCache(crm_db_paths=[crm_db_path], interactions_db_path=interactions_db)

        calls = []

        @cache.cached()
        def endpoint():
            calls.append(1)
            return {"call_number": len(calls)}

        endpoint()
        assert len(calls) == 1
        assert cache.stats()["entries"] == 0  # never cached -- crm.db didn't exist

        _make_db(crm_db_path)  # now it exists

        endpoint()  # first successful version read -- establishes a generation, computes once more
        endpoint()  # now a real hit
        assert len(calls) == 2
        assert cache.stats()["entries"] == 1


class TestMultipleCrmPaths:
    """The stores behind `/statistics` and `/people` (SourceEntityStore,
    RelationshipStore) resolve crm.db through `get_crm_db_path()`, while
    `PersonEntityStore` uses a hardcoded path -- the two are the same file
    by default but can diverge under a non-default `LIFEOS_CHROMA_PATH`.
    When they do, a write through *either* must invalidate (#917 review
    finding 2)."""

    def test_write_through_either_watched_crm_path_invalidates(self, tmp_path):
        crm_db_a = _make_db(tmp_path / "crm_a.db")
        crm_db_b = _make_db(tmp_path / "crm_b.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        cache = AggregateCache(crm_db_paths=[crm_db_a, crm_db_b],
                                interactions_db_path=interactions_db)

        calls = []

        @cache.cached()
        def endpoint():
            calls.append(1)
            return {"call_number": len(calls)}

        endpoint()
        assert len(calls) == 1
        assert endpoint() == {"call_number": 1}  # still a hit

        _commit_external_write(crm_db_b)
        endpoint()
        assert len(calls) == 2, "a write through the second watched crm.db path must invalidate"

        _commit_external_write(crm_db_a)
        endpoint()
        assert len(calls) == 3, "a write through the first watched crm.db path must invalidate"

    def test_identical_paths_are_deduped_to_one_connection(self, tmp_path):
        crm_db = _make_db(tmp_path / "crm.db")
        interactions_db = _make_db(tmp_path / "interactions.db")
        # The same file, named two different (but equivalent) ways.
        same_file_again = str(tmp_path) + os.sep + "." + os.sep + "crm.db"
        cache = AggregateCache(crm_db_paths=[crm_db, same_file_again],
                                interactions_db_path=interactions_db)
        assert len(cache.crm_db_paths) == 1

    def test_default_discovery_watches_both_when_settings_diverge(self, tmp_path, monkeypatch):
        """Simulates a relocated LIFEOS_CHROMA_PATH: PersonEntityStore's
        hardcoded path and get_crm_db_path()'s settings-derived path point
        at two different files. AggregateCache's auto-discovery (used by
        the process-wide singleton, not the `cache` fixture above, which
        always passes crm_db_paths explicitly) must watch both."""
        import api.services.aggregate_cache as aggregate_cache_module
        from api.services.person_entity import PersonEntityStore

        person_entity_path = tmp_path / "person_entity_crm.db"
        relocated_path = tmp_path / "relocated_crm.db"
        _make_db(person_entity_path)
        _make_db(relocated_path)

        monkeypatch.setattr(PersonEntityStore, "CRM_DB_PATH", person_entity_path)
        monkeypatch.setattr(aggregate_cache_module, "get_crm_db_path", lambda: str(relocated_path))

        paths = _default_crm_db_paths()
        resolved = {os.path.realpath(p) for p in paths}
        assert resolved == {os.path.realpath(str(person_entity_path)),
                             os.path.realpath(str(relocated_path))}


class TestSingleFlight:
    """Concurrent misses for the same key compute once; the rest wait for
    the first caller and reuse its result (#917 review finding 9)."""

    def test_concurrent_misses_for_the_same_key_compute_once(self, cache):
        calls = []
        call_lock = threading.Lock()
        started = threading.Event()

        @cache.cached()
        def slow_endpoint():
            with call_lock:
                calls.append(1)
            started.set()
            time.sleep(0.3)  # long enough that followers arrive while this runs
            return {"call_number": len(calls)}

        results = []
        results_lock = threading.Lock()

        def worker():
            result = slow_endpoint()
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        started.wait(timeout=2)
        for t in threads:
            t.join(timeout=5)

        assert len(calls) == 1, f"expected exactly one real computation, got {len(calls)}"
        assert len(results) == 8
        assert all(r == {"call_number": 1} for r in results)


class TestGenerationRaceDuringCompute:
    """A commit that lands while the leader is still computing must never
    get that leader's pre-write result cached under the post-write
    generation (#917 review finding 11, introduced by finding 8's
    generation-stamp redesign: the old version-in-key design was immune,
    since a pre-write result stored under the pre-write key was simply
    never looked up again once the generation moved on).

    Reproduction shape, matching the review: request 1 starts computing
    (reads the pre-write generation), an external commit lands, request 2
    arrives (reads the post-write generation, becomes a single-flight
    follower of request 1 since nothing is cached yet), request 1 finishes
    and attempts its store. Without the fix, that store succeeds --
    unconditionally -- so request 2 (waking as a follower) and any later
    request both get the stale pre-write value from the cache. With the
    fix, the store is skipped (the generation moved since request 1 read
    it), so request 2 falls back to computing its own fresh result, and a
    later request 3 gets a fresh (correct) cache entry from its own
    computation."""

    def test_write_during_leader_compute_is_not_served_stale(self, cache):
        state = {"value": "BEFORE"}
        handler_calls = []
        leader_started = threading.Event()

        @cache.cached()
        def endpoint():
            handler_calls.append(1)
            # Read first, same as a real aggregate query snapshotting its
            # data at the start -- then (only the leader, first call) take
            # a while to actually finish, long enough for the external
            # write and request 2 below to both land before it returns.
            value = state["value"]
            if len(handler_calls) == 1:
                leader_started.set()
                time.sleep(0.3)
            return {"value": value}

        results = {}

        def req1():
            results["req1"] = endpoint()

        t1 = threading.Thread(target=req1)
        t1.start()
        leader_started.wait(timeout=5)

        # External write lands while request 1 (the leader) is still
        # "computing" (sleeping) -- this is what advances the generation
        # before request 1's store runs.
        state["value"] = "AFTER"
        _commit_external_write(cache.crm_db_paths[0])

        def req2():
            results["req2"] = endpoint()

        t2 = threading.Thread(target=req2)
        t2.start()
        # Give request 2 time to read the (now advanced) generation and
        # register as a single-flight follower of request 1 before request
        # 1 finishes and attempts its store.
        time.sleep(0.1)

        t1.join(timeout=5)
        t2.join(timeout=5)

        # Request 1 correctly reflects the (pre-write) data it actually read.
        assert results["req1"] == {"value": "BEFORE"}
        # Request 2, woken as a follower, must never receive request 1's
        # stale result -- it must fall back to computing its own, fresh one.
        assert results["req2"] == {"value": "AFTER"}, (
            f"follower must not be served the leader's pre-write result: {results['req2']}"
        )

        # Request 3, well after everything has settled and no more writes
        # have happened, must also see the post-write value -- proving
        # nothing stale was left cached under the new generation either.
        req3 = endpoint()
        assert req3 == {"value": "AFTER"}, (
            f"a later request must never see a stale cached entry: {req3}"
        )


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
