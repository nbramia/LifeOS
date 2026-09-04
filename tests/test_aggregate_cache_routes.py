"""
Static + integration guard: the seven endpoints #876/#917 promise to cache
actually carry the `AggregateCache` wrapper, and at least one of them
demonstrably invalidates through a real `TestClient` request when an
external connection commits to a temporary `crm.db` (#917 review finding 3).

`tests/test_aggregate_cache.py` only ever decorates synthetic local
functions with `AggregateCache.cached()` directly -- deleting
`@cached_aggregate()` from every handler in `api/routes/crm.py` would leave
that file's suite fully green, so it alone doesn't lock in that the real
routes are wired up. This file does.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.routes.crm import router as crm_router

pytestmark = pytest.mark.unit

# The seven (method, path) pairs #876/#917 cache. Kept as a literal set
# (rather than re-deriving it from the router) so a change to what's cached
# is a deliberate edit here too, not just a silent pass-through.
CACHED_ROUTES = {
    ("GET", "/api/crm/birthdays/all"),
    ("GET", "/api/crm/people"),
    ("GET", "/api/crm/statistics"),
    ("GET", "/api/crm/me/timeline"),
    ("GET", "/api/crm/me/interactions"),
    ("GET", "/api/crm/family/timeline"),
    ("GET", "/api/crm/family/interactions"),
}


def _cached_route_pairs():
    for route in crm_router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None:
            continue
        for method in methods:
            yield method, path, route


class TestSevenEndpointsAreWrapped:
    """Each of the seven cached (method, path) pairs' registered endpoint is
    specifically the AggregateCache wrapper -- not merely *some* decorator
    (functools.wraps means __wrapped__ alone doesn't prove that; the
    wrapper's own code must come from aggregate_cache.py)."""

    def test_every_listed_route_carries_the_cache_wrapper(self):
        import inspect

        seen = set()
        for method, path, route in _cached_route_pairs():
            if (method, path) not in CACHED_ROUTES:
                continue
            seen.add((method, path))
            endpoint = route.endpoint
            assert getattr(endpoint, "__wrapped__", None) is not None, (
                f"{method} {path}: endpoint has no __wrapped__ (not decorated at all?)"
            )
            source_file = inspect.getsourcefile(endpoint) or ""
            assert source_file.endswith("aggregate_cache.py"), (
                f"{method} {path}: endpoint's own code is defined in {source_file}, "
                "not aggregate_cache.py -- some other decorator, not the cache wrapper"
            )

        assert seen == CACHED_ROUTES, (
            f"expected all seven cached routes to be found in the router, "
            f"missing: {CACHED_ROUTES - seen}"
        )

    def test_no_other_crm_route_carries_the_cache_wrapper(self):
        """A route NOT in CACHED_ROUTES should not accidentally be cached
        either -- catches a decorator applied to the wrong handler."""
        import inspect

        for method, path, route in _cached_route_pairs():
            if (method, path) in CACHED_ROUTES:
                continue
            endpoint = route.endpoint
            source_file = inspect.getsourcefile(endpoint) or ""
            assert not source_file.endswith("aggregate_cache.py"), (
                f"{method} {path} is cached but not in the expected list"
            )


class TestRealRouteInvalidatesThroughTestClient:
    """A real HTTP request to a real decorated route hits the cache, and an
    external commit to a temporary crm.db (substituted for the process-wide
    singleton's watched path) forces a recompute -- proven through
    TestClient, not a synthetic function (#917 review finding 3)."""

    def test_birthdays_all_recomputes_after_an_external_crm_db_commit(
        self, tmp_path, monkeypatch,
    ):
        from unittest.mock import patch

        from api.main import app
        from api.services.aggregate_cache import get_aggregate_cache

        # Substitute the singleton's watched crm.db (and stop watching
        # interactions.db by pointing it at the same harmless temp file --
        # get_all_birthdays never touches interactions data) with a fresh,
        # real, temporary database, and reset its connections so the next
        # read opens against the substituted path rather than a stale
        # handle to the real one.
        temp_crm_db = tmp_path / "crm.db"
        conn = sqlite3.connect(str(temp_crm_db))
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        cache = get_aggregate_cache()
        monkeypatch.setattr(cache, "crm_db_paths", [str(temp_crm_db)])
        monkeypatch.setattr(cache, "interactions_db_path", str(temp_crm_db))
        monkeypatch.setattr(cache, "_crm_conns", [None])
        monkeypatch.setattr(cache, "_interactions_conn", None)
        cache.clear()

        calls = []

        class _CountingStore:
            def get_all(self):
                calls.append(1)
                return []

        client = TestClient(app)
        with patch("api.routes.crm.get_person_entity_store", return_value=_CountingStore()):
            first = client.get("/api/crm/birthdays/all")
            assert first.status_code == 200
            assert len(calls) == 1

            second = client.get("/api/crm/birthdays/all")
            assert second.status_code == 200
            assert len(calls) == 1, "identical repeat request must hit the cache"

            # External commit to the substituted crm.db, from a brand-new
            # connection -- exactly what PRAGMA data_version is meant to
            # detect, over an actual HTTP round trip this time.
            write_conn = sqlite3.connect(str(temp_crm_db))
            write_conn.execute("INSERT INTO marker DEFAULT VALUES")
            write_conn.commit()
            write_conn.close()

            third = client.get("/api/crm/birthdays/all")
            assert third.status_code == 200
            assert len(calls) == 2, (
                "an external commit to the watched crm.db must force a recompute"
            )

        cache.clear()
