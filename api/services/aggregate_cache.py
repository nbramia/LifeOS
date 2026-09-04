"""
Short-lived response cache for the CRM's heaviest read aggregates (#876).

`/me/interactions`, `/me/timeline`, `/family/interactions`, `/family/timeline`,
`/birthdays/all`, `/statistics`, and the default-parameter `/people` list all
recompute from scratch on every request even when nothing in crm.db or
interactions.db has changed since the last call -- #871/#880 made each of
those individually fast, but a dashboard switch or a page refresh still pays
the full cost every time. `AggregateCache.cached()` memoizes a route
handler's return value for a short TTL, keyed on its resolved query
parameters plus both databases' `PRAGMA data_version` counters -- the same
invalidation signal `PersonEntityStore`'s own `get_all()` cache uses (see
api/services/person_entity.py) -- so a write to either database, from this
process or any other, makes the next request for an affected key recompute
rather than serve stale data. The TTL is a backstop bound on memory/entry
lifetime, not the primary invalidation path: a data_version change makes a
key's next lookup a miss immediately, regardless of TTL.

Structured as a class (rather than bare module globals) so a test can
construct an isolated `AggregateCache` pointed at temporary databases, the
same way `tests/test_person_entity.py` builds a `PersonEntityStore(db_path)`
instead of fighting the process-wide singleton -- see `get_aggregate_cache()`
below for the singleton `api/routes/crm.py` actually decorates its handlers
with.
"""
import functools
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

from fastapi.encoders import jsonable_encoder

from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import PersonEntityStore

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
MAX_ENTRIES = 200
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MB


class AggregateCache:
    """A bounded, TTL-backstopped, data_version-keyed cache for read-only
    route handlers.

    Each instance owns two persistent, pragma-only SQLite connections (one
    per database) used solely to read `PRAGMA data_version` cheaply -- the
    same justification as `PersonEntityStore._get_data_version_connection()`.
    Every call site of those connections is reached only while holding
    `self._lock`, so cross-thread use is serialized, which is what makes
    `check_same_thread=False` safe here despite handlers running on the
    FastAPI worker threadpool.
    """

    def __init__(
        self,
        crm_db_path: Optional[str] = None,
        interactions_db_path: Optional[str] = None,
        max_entries: int = MAX_ENTRIES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ):
        self.crm_db_path = crm_db_path or str(PersonEntityStore.CRM_DB_PATH)
        self.interactions_db_path = interactions_db_path or get_interaction_db_path()
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes

        self._lock = threading.Lock()
        self._crm_conn: Optional[sqlite3.Connection] = None
        self._interactions_conn: Optional[sqlite3.Connection] = None
        # key -> (expires_at monotonic, jsonable value, size in bytes). Dict
        # order doubles as LRU order (most-recently-used at the end).
        self._cache: "OrderedDict[tuple, tuple[float, object, int]]" = OrderedDict()
        self._total_bytes = 0

    def _get_crm_connection(self) -> sqlite3.Connection:
        if self._crm_conn is None:
            self._crm_conn = sqlite3.connect(
                self.crm_db_path,
                check_same_thread=False,  # threadpool-safe: pragma-only, guarded by lock
            )
        return self._crm_conn

    def _get_interactions_connection(self) -> sqlite3.Connection:
        if self._interactions_conn is None:
            self._interactions_conn = sqlite3.connect(
                self.interactions_db_path,
                check_same_thread=False,  # threadpool-safe: pragma-only, guarded by lock
            )
        return self._interactions_conn

    def _data_versions_locked(self) -> tuple:
        """Read both PRAGMA data_version counters. Caller must hold `_lock`."""
        crm_version = self._get_crm_connection().execute("PRAGMA data_version").fetchone()[0]
        interactions_version = self._get_interactions_connection().execute(
            "PRAGMA data_version").fetchone()[0]
        return int(crm_version), int(interactions_version)

    def _evict_locked(self) -> None:
        """Drop the least-recently-used entries while over either bound.
        Caller must hold `_lock`."""
        while self._cache and (len(self._cache) > self.max_entries or
                                self._total_bytes > self.max_total_bytes):
            _, (_, _, size) = self._cache.popitem(last=False)
            self._total_bytes -= size

    def stats(self) -> dict:
        """Entry count and total cached bytes -- for tests and diagnostics."""
        with self._lock:
            return {"entries": len(self._cache), "total_bytes": self._total_bytes}

    def clear(self) -> None:
        """Drop every cached entry. Test-only escape hatch (production
        invalidation is via data_version, never a manual clear)."""
        with self._lock:
            self._cache.clear()
            self._total_bytes = 0

    def cached(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Callable:
        """Memoize a function's return value for `ttl_seconds`, keyed on its
        resolved keyword arguments plus both databases' current
        data_version.

        Intended for a FastAPI route handler, applied directly under
        `@router.get(...)` (i.e. as the innermost decorator) so FastAPI still
        sees the original function's signature: `functools.wraps` sets
        `__wrapped__`, which Python's `inspect.signature()` follows by
        default -- that is what lets FastAPI keep resolving `Query()`
        parameters from the wrapped function normally.

        Only a successful return is ever cached: an exception (including a
        raised HTTPException for a non-200 response) propagates before this
        reaches the cache-store step, so an error response is never cached.
        """
        def decorator(func: Callable) -> Callable:
            identity = f"{func.__module__}.{func.__qualname__}"

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                # FastAPI always calls path operation functions with
                # resolved Query()/Path() values as keyword arguments, so
                # this alone covers "every query parameter" without
                # inspecting the request directly.
                param_key = tuple(sorted(kwargs.items()))

                with self._lock:
                    crm_version, interactions_version = self._data_versions_locked()
                    key = (identity, param_key, crm_version, interactions_version)
                    cached = self._cache.get(key)
                    if cached is not None:
                        expires_at, value, size = cached
                        if expires_at > time.monotonic():
                            self._cache.move_to_end(key)
                            return value
                        # Expired -- drop it now rather than waiting for a
                        # bounds-triggered eviction that might never come.
                        del self._cache[key]
                        self._total_bytes -= size

                # Compute outside the lock: the wrapped handler can take
                # tens/hundreds of ms, and holding the lock here would
                # serialize every cached CRM aggregate request behind
                # whichever one is currently a cache miss.
                result = func(*args, **kwargs)

                # The cached (and returned) value is `result` itself, not a
                # jsonable_encoder()'d copy: some existing CRM unit tests
                # call a decorated handler directly (bypassing FastAPI) and
                # expect the same Pydantic model FastAPI would otherwise
                # serialize later, with normal attribute access -- e.g.
                # `result.people`, not `result["people"]`. jsonable_encoder
                # is used only to measure the byte size for the cache's
                # bounds; it never replaces what's actually stored/returned.
                size = len(json.dumps(jsonable_encoder(result)).encode("utf-8"))

                with self._lock:
                    # A concurrent miss for the same key may have already
                    # stored an entry while this call was computing outside
                    # the lock -- last writer wins, which is fine, since
                    # both computed from the same data_version pair and
                    # therefore the same underlying data.
                    if key in self._cache:
                        self._total_bytes -= self._cache[key][2]
                    self._cache[key] = (time.monotonic() + ttl_seconds, result, size)
                    self._cache.move_to_end(key)
                    self._total_bytes += size
                    self._evict_locked()

                return result

            return wrapper

        return decorator


_aggregate_cache: Optional[AggregateCache] = None


def get_aggregate_cache() -> AggregateCache:
    """Process-wide singleton, matching the `get_*()` accessor pattern used
    throughout `api/services/` (e.g. `get_person_entity_store()`)."""
    global _aggregate_cache
    if _aggregate_cache is None:
        _aggregate_cache = AggregateCache()
    return _aggregate_cache


def cached_aggregate(ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Callable:
    """Decorator applied to CRM route handlers, backed by the process-wide
    `AggregateCache` singleton. See `AggregateCache.cached()` for the actual
    caching behavior.

    Called once per decorated function, at import time -- the returned
    wrapper closes over whichever `AggregateCache` instance
    `get_aggregate_cache()` returns at that moment, so `reset_aggregate_cache()`
    below clears that instance's entries in place rather than swapping in a
    new one (which the already-decorated handlers would never see).
    """
    return get_aggregate_cache().cached(ttl_seconds)


def reset_aggregate_cache() -> None:
    """Clear every cached entry on the process-wide singleton.

    For testing only: several existing CRM unit tests call a decorated route
    handler (e.g. `get_me_interactions`) directly with its store dependency
    mocked (`patch('api.routes.crm.get_person_entity_store', ...)`) -- the
    mock never touches the real crm.db/interactions.db files, so this
    cache's data_version-keyed entries from an earlier test with the same
    parameters would otherwise still be a "hit" and mask the new mock's
    return value entirely. Wired into
    `tests.reset_singletons.reset_lightweight_singletons()`, which the
    autouse `reset_singletons_after_test` fixture in `tests/conftest.py`
    already runs after every test.

    Deliberately does not reassign the module-level `_aggregate_cache`
    singleton to a fresh instance: `cached_aggregate()` binds each decorated
    route handler to whatever instance existed at import time, so a new
    instance here would never be seen by those closures -- only clearing
    the existing one in place reaches them.
    """
    get_aggregate_cache().clear()
