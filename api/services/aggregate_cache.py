"""
Short-lived response cache for the CRM's heaviest read aggregates.

`/me/interactions`, `/me/timeline`, `/family/interactions`, `/family/timeline`,
`/birthdays/all`, `/statistics`, and the default-parameter `/people` list all
recompute from scratch on every request even when nothing in crm.db or
interactions.db has changed since the last call, so a dashboard switch or a
page refresh still pays the full cost every time. `AggregateCache.cached()`
memoizes a route handler's return value for a short TTL, keyed on its
resolved query parameters, invalidated whenever either database's `PRAGMA
data_version` counter changes -- the same invalidation signal
`PersonEntityStore`'s own `get_all()` cache uses (see
api/services/person_entity.py) -- so a write to either database, from this
process or any other, drops every cached entry before the next request. The
TTL is a backstop bound on entry lifetime, not the primary invalidation path.

Structured as a class (rather than bare module globals) so a test can
construct an isolated `AggregateCache` pointed at temporary databases, the
same way `tests/test_person_entity.py` builds a `PersonEntityStore(db_path)`
instead of fighting the process-wide singleton -- see `get_aggregate_cache()`
below for the singleton `api/routes/crm.py` actually decorates its handlers
with.

Invariants (see each method's docstring for detail):
- A `PRAGMA data_version` read failure never fails the request -- it falls
  through to computing uncached and the connection is reopened next call.
- `crm.db` is resolved through both `PersonEntityStore.CRM_DB_PATH` and
  `api.utils.db_paths.get_crm_db_path()` (deduped by `os.path.realpath`),
  since the stores backing `/statistics` and `/people` use the latter and
  the two can diverge under a non-default `LIFEOS_CHROMA_PATH`. Connections
  open read-only with a `mode=ro` URI so a missing file is never silently created.
- The byte bound tracks serialized JSON size, which understates real
  retained heap by roughly 7x (measured) -- `MAX_TOTAL_BYTES` is scaled down
  by that ratio so it targets a real ~50 MB heap ceiling, not a 20 MB one.
- A cache hit returns a deep copy, and a miss stores a deep copy of what it
  returns, so a caller mutating their own result can never poison the entry
  another caller (or the same one, later) receives.
- The data_version pair is a generation stamp, not part of the key: a
  change clears every entry outright instead of leaving old-generation
  entries as unreachable dead weight inside the byte/entry bounds. An entry
  larger than the byte cap is skipped rather than flushing the cache to
  make room for it.
- Concurrent misses for the same key single-flight behind a per-key
  `threading.Event`: the first caller computes, the rest wait for it and
  reuse its result (falling back to computing themselves only if the
  leader's call raised).
- A store re-checks that the generation it read before computing is still
  current, since a commit that lands while a handler is running could
  otherwise get that handler's pre-write result cached under the post-write
  generation, served stale for the full TTL. A mismatch at store time skips
  caching -- the next request for that key simply misses and recomputes
  against the new generation.
"""
import copy
import functools
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from fastapi.encoders import jsonable_encoder

from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import PersonEntityStore
from api.utils.db_paths import get_crm_db_path

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
MAX_ENTRIES = 200

# How long a follower waits for the leader computing its key before giving
# up and computing itself. Deliberately far shorter than DEFAULT_TTL_SECONDS:
# a real handler realistically takes at most a few seconds even on the
# largest aggregate, so waiting a full 300s would just hold a threadpool
# worker thread hostage to whatever went wrong with the leader (an
# unexpected hang, not merely a slow query) far longer than any request
# should ever legitimately take.
FOLLOWER_WAIT_TIMEOUT_SECONDS = 30

# A ~7 MB serialized-JSON entry was measured to retain ~51 MB of actual
# Python-object heap once decoded/cached -- nested dicts/lists/model
# instances cost far more per byte than the compact JSON wire format they
# came from. MAX_TOTAL_BYTES tracks serialized size (cheap to compute, no
# new dependency for a real heap profiler), scaled down by that measured
# ratio so the bound it actually enforces is a real heap ceiling, not a
# serialized-bytes one.
HEAP_TO_SERIALIZED_RATIO = 7
HEAP_TARGET_BYTES = 50 * 1024 * 1024  # ~50 MB real heap ceiling
MAX_TOTAL_BYTES = HEAP_TARGET_BYTES // HEAP_TO_SERIALIZED_RATIO  # ~7.1 MB serialized


def _dedupe_paths(paths: list) -> list:
    """Distinct filesystem targets among `paths`, preserving first-seen
    order (`os.path.realpath` normalizes symlinks/`..`/relative segments;
    it does not require the path to exist)."""
    seen = {}
    for p in paths:
        seen.setdefault(os.path.realpath(p), p)
    return list(seen.values())


def _default_crm_db_paths() -> list:
    """Every path a CRM store actually reads crm.db from.

    `PersonEntityStore` uses the hardcoded `CRM_DB_PATH`; `SourceEntityStore`
    and `RelationshipStore` (which back `/statistics`'s and `/people`'s
    category computation) resolve through `get_crm_db_path()`, derived from
    `settings.chroma_path`. The two are the same file by default but diverge
    under a non-default `LIFEOS_CHROMA_PATH` -- watching only one would leave
    writes through the other invisible to this cache, bounded only by the TTL.
    """
    return _dedupe_paths([str(PersonEntityStore.CRM_DB_PATH), get_crm_db_path()])


def _open_existing_db(path: str) -> sqlite3.Connection:
    """Open `path` read-only, without ever creating it.

    Plain `sqlite3.connect(path)` silently creates a 0-byte (and therefore
    permanently `data_version`-static) file if `path` doesn't exist yet --
    exactly the wrong failure mode for a path this cache merely *watches*
    and never writes to. The `mode=ro` URI param makes SQLite raise instead
    of creating it, and -- verified against a
    WAL-mode database, which every store here uses -- a read-only
    connection still sees `PRAGMA data_version` change correctly after an
    external write, so there is no need for `mode=rw` (which would also
    have worked, but claims write access this connection never uses).
    """
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,  # threadpool-safe: pragma-only, guarded by lock
    )


class AggregateCache:
    """A bounded, TTL-backstopped, data_version-keyed cache for read-only
    route handlers.

    Each instance owns one persistent, pragma-only SQLite connection per
    watched database path, used solely to read `PRAGMA data_version`
    cheaply -- the same justification as
    `PersonEntityStore._get_data_version_connection()`. Every call site of
    those connections is reached only while holding `self._lock`, so
    cross-thread use is serialized, which is what makes
    `check_same_thread=False` safe here despite handlers running on the
    FastAPI worker threadpool.
    """

    def __init__(
        self,
        crm_db_paths: Optional[list] = None,
        interactions_db_path: Optional[str] = None,
        max_entries: int = MAX_ENTRIES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ):
        self.crm_db_paths = _dedupe_paths(
            list(crm_db_paths) if crm_db_paths is not None else _default_crm_db_paths()
        )
        self.interactions_db_path = interactions_db_path or get_interaction_db_path()
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes

        self._lock = threading.Lock()
        self._crm_conns: list = [None] * len(self.crm_db_paths)
        self._interactions_conn: Optional[sqlite3.Connection] = None
        self._version_read_failing = False  # for "log once" on the way down

        # key -> (expires_at monotonic, deep-copied value, size in bytes).
        # Dict order doubles as LRU order (most-recently-used at the end).
        # The data_version pair is NOT part of the key -- see
        # _refresh_generation_locked().
        self._cache: "OrderedDict[tuple, tuple[float, object, int]]" = OrderedDict()
        self._total_bytes = 0
        self._generation: Optional[tuple] = None

        # key -> Event, for single-flight de-duplication of concurrent
        # misses. Guarded by `_lock` like everything else here.
        self._in_flight: dict = {}

    def _get_crm_connection(self, index: int) -> sqlite3.Connection:
        if self._crm_conns[index] is None:
            self._crm_conns[index] = _open_existing_db(self.crm_db_paths[index])
        return self._crm_conns[index]

    def _get_interactions_connection(self) -> sqlite3.Connection:
        if self._interactions_conn is None:
            self._interactions_conn = _open_existing_db(self.interactions_db_path)
        return self._interactions_conn

    def _try_read_versions_locked(self) -> Optional[tuple]:
        """Read every watched database's `PRAGMA data_version`, or `None` if
        any read fails. Caller must hold `_lock`.

        A missing file, a file mid-replacement (e.g. a backup-restore
        swap), or any other `sqlite3.Error` must never turn a request that
        would otherwise succeed into a 500 -- the caller falls through to
        computing uncached on `None`. Every open
        connection is dropped on failure so the *next* call opens fresh
        (the file may appear, or be restored, in the meantime); logs once
        per outage rather than once per request.
        """
        try:
            crm_versions = tuple(
                int(self._get_crm_connection(i).execute("PRAGMA data_version").fetchone()[0])
                for i in range(len(self.crm_db_paths))
            )
            interactions_version = int(
                self._get_interactions_connection().execute("PRAGMA data_version").fetchone()[0]
            )
        except sqlite3.Error as exc:
            if not self._version_read_failing:
                logger.warning(
                    "AggregateCache: PRAGMA data_version read failed (%s); "
                    "serving every decorated endpoint uncached until it recovers", exc,
                )
                self._version_read_failing = True
            self._crm_conns = [None] * len(self.crm_db_paths)
            self._interactions_conn = None
            return None

        self._version_read_failing = False
        return (crm_versions, interactions_version)

    def _refresh_generation_locked(self, versions: tuple) -> None:
        """Drop every cached entry if `versions` differs from the last-seen
        generation. Caller must hold `_lock`.

        Keeping the version pair as a generation stamp rather than inside
        each entry's key means a write drops stale entries outright instead
        of merely making them unreachable while they still count against
        the entry/byte bounds until LRU pressure happens to reclaim them.
        """
        if self._generation is not None and self._generation != versions:
            self._cache.clear()
            self._total_bytes = 0
        self._generation = versions

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
            self._generation = None

    def cached(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
               should_cache: Optional[Callable[[dict], bool]] = None) -> Callable:
        """Memoize a function's return value for `ttl_seconds`, keyed on its
        resolved keyword arguments, invalidated whenever any watched
        database's data_version changes.

        `should_cache`, if given, is called with the resolved kwargs dict
        before anything else; a `False` return bypasses the cache entirely
        for that call (no read, no store) -- used to keep `/people` search
        text and unbounded-`limit` pages out of a long-lived in-memory key.

        Intended for a FastAPI route handler, applied directly under
        `@router.get(...)` (i.e. as the innermost decorator) so FastAPI still
        sees the original function's signature: `functools.wraps` sets
        `__wrapped__`, which Python's `inspect.signature()` follows by
        default -- that is what lets FastAPI keep resolving `Query()`
        parameters from the wrapped function normally.

        Only a successful return is ever cached: an exception (including a
        raised HTTPException for a non-200 response) propagates before this
        reaches the cache-store step, so an error response is never cached.

        The returned value is always a deep copy, on both a hit and a miss:
        a caller that mutates its own result in place can never poison the
        cache, and a caller receiving a hit can never be poisoned by a
        previous caller's mutation.
        """
        def decorator(func: Callable) -> Callable:
            identity = f"{func.__module__}.{func.__qualname__}"

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                if should_cache is not None and not should_cache(kwargs):
                    return func(*args, **kwargs)

                # FastAPI always calls path operation functions with
                # resolved Query()/Path() values as keyword arguments, so
                # this alone covers "every query parameter" without
                # inspecting the request directly.
                param_key = tuple(sorted(kwargs.items()))
                key = (identity, param_key)

                is_leader = False
                event = None
                with self._lock:
                    versions = self._try_read_versions_locked()
                    if versions is not None:
                        self._refresh_generation_locked(versions)
                        cached = self._cache.get(key)
                        if cached is not None:
                            expires_at, value, size = cached
                            if expires_at > time.monotonic():
                                self._cache.move_to_end(key)
                                return copy.deepcopy(value)
                            # Expired -- drop it now rather than waiting for
                            # a bounds-triggered eviction that might never come.
                            del self._cache[key]
                            self._total_bytes -= size

                        # Single-flight: only the first miss for this key
                        # computes; concurrent others wait for it below.
                        event = self._in_flight.get(key)
                        if event is None:
                            event = threading.Event()
                            self._in_flight[key] = event
                            is_leader = True

                if versions is None:
                    # Cache unavailable this call (see
                    # _try_read_versions_locked) -- compute uncached rather
                    # than fail the request.
                    return func(*args, **kwargs)

                if not is_leader:
                    # A concurrent call is already computing this exact key;
                    # wait for it and reuse its result instead of also
                    # computing.
                    event.wait(timeout=FOLLOWER_WAIT_TIMEOUT_SECONDS)
                    with self._lock:
                        cached = self._cache.get(key)
                        if cached is not None:
                            expires_at, value, _size = cached
                            if expires_at > time.monotonic():
                                self._cache.move_to_end(key)
                                return copy.deepcopy(value)
                    # The leader's call raised, its entry already expired,
                    # or we timed out waiting -- fall back to computing
                    # ourselves rather than waiting forever.
                    return func(*args, **kwargs)

                # Leader path: compute outside the lock (the wrapped handler
                # can take tens/hundreds of ms, and holding the lock here
                # would serialize every cached CRM aggregate request behind
                # whichever one is currently a cache miss). Followers must
                # not be woken until the result is actually visible in
                # `self._cache` -- waking them right after `func()` returns,
                # before the store below runs, is exactly what a follower's
                # own fallback ("nothing cached yet -> compute it myself")
                # is for, silently turning single-flight into "everyone
                # computes anyway" under real timing (a bug caught only by
                # measuring over real concurrent HTTP load, not the unit
                # test's synthetic slow function -- the store there was fast
                # enough to usually win the race).
                try:
                    result = func(*args, **kwargs)

                    # Deep-copy what's stored (and returned): the caller of
                    # this very call could still mutate `result` in place,
                    # but that must never reach the cached entry another
                    # caller receives later. This roughly doubles a warm
                    # hit's CPU cost relative to a shallow return (measured:
                    # a 235 KB /me/timeline page costs ~4.6ms of *added*
                    # work over real HTTP -- still far under the 20ms
                    # target, but worth knowing if a response size this
                    # decorates grows a lot further). jsonable_encoder is
                    # used only to measure a byte size for the cache's
                    # bounds; it never replaces what's actually
                    # stored/returned.
                    stored = copy.deepcopy(result)
                    size = len(json.dumps(jsonable_encoder(stored)).encode("utf-8"))

                    with self._lock:
                        # Re-check the generation against what THIS call
                        # read before computing, not whatever it is now: a
                        # commit that lands while `func()` above is running
                        # advances `self._generation` (via a later request's
                        # own version read) before this store runs, so
                        # storing unconditionally would cache a pre-write
                        # result under the post-write generation and serve
                        # it for the full TTL -- exactly the staleness this
                        # cache exists to prevent.
                        if self._generation != versions:
                            pass
                        elif size > self.max_total_bytes:
                            # Too big to ever fit -- skip caching it rather
                            # than evicting everything else to make room.
                            pass
                        else:
                            if key in self._cache:
                                self._total_bytes -= self._cache[key][2]
                            self._cache[key] = (time.monotonic() + ttl_seconds, stored, size)
                            self._cache.move_to_end(key)
                            self._total_bytes += size
                            self._evict_locked()

                    return result
                finally:
                    # Wake followers before dropping the in-flight marker:
                    # a brand-new request arriving in between would
                    # otherwise see neither a cache entry (if the store
                    # above was skipped) nor an in-flight event, and start
                    # a second, fully redundant computation rather than
                    # falling into the (already-set, so non-blocking)
                    # follower path and getting the same "compute it
                    # myself" fallback that path already has. Single-flight
                    # is best-effort, not exact, but this ordering narrows
                    # the gap.
                    event.set()
                    with self._lock:
                        self._in_flight.pop(key, None)

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


def cached_aggregate(ttl_seconds: float = DEFAULT_TTL_SECONDS,
                      should_cache: Optional[Callable[[dict], bool]] = None) -> Callable:
    """Decorator applied to CRM route handlers, backed by the process-wide
    `AggregateCache` singleton. See `AggregateCache.cached()` for the actual
    caching behavior.

    Called once per decorated function, at import time -- the returned
    wrapper closes over whichever `AggregateCache` instance
    `get_aggregate_cache()` returns at that moment, so `reset_aggregate_cache()`
    below clears that instance's entries in place rather than swapping in a
    new one (which the already-decorated handlers would never see).
    """
    return get_aggregate_cache().cached(ttl_seconds, should_cache=should_cache)


def reset_aggregate_cache() -> None:
    """Clear every cached entry on the process-wide singleton.

    For testing only: several existing CRM unit tests call a decorated route
    handler (e.g. `get_me_interactions`) directly with its store dependency
    mocked (`patch('api.routes.crm.get_person_entity_store', ...)`) -- the
    mock never touches the real crm.db/interactions.db files, so this
    cache's entries from an earlier test with the same parameters would
    otherwise still be a "hit" and mask the new mock's return value
    entirely. Wired into `tests.reset_singletons.reset_lightweight_singletons()`,
    which the autouse `reset_singletons_after_test` fixture in
    `tests/conftest.py` already runs after every test.

    Deliberately does not reassign the module-level `_aggregate_cache`
    singleton to a fresh instance: `cached_aggregate()` binds each decorated
    route handler to whatever instance existed at import time, so a new
    instance here would never be seen by those closures -- only clearing
    the existing one in place reaches them.
    """
    get_aggregate_cache().clear()
