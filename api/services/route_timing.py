"""
Per-route request timing (#877).

Complements `api/services/perf_trace.py` (which records LLM/chat-turn spans
for a conversation) with an always-on summary of every HTTP request: how
long it took, whether it counted as slow, and how large the response was.
This is a live "what's slow right now" signal -- process-local, bounded, and
reset on restart, not a persisted history (#733 covers persisted rollups
that this can feed).

Usage:
    from api.services.route_timing import RouteTimingMiddleware, get_route_timing_store

    app.add_middleware(RouteTimingMiddleware)  # registered outermost (api/main.py)
    get_route_timing_store().summary()         # -> list of per-route stat dicts
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, MutableMapping, Optional, Tuple

from config.settings import settings

logger = logging.getLogger(__name__)

_WINDOW_SIZE = 200

# Routes too trivial or too frequent to matter for a slow-route summary:
# health checks (polled constantly by out-of-band monitors) and the static
# asset mount (JS/CSS/images -- many requests per page load, none of them
# meaningfully "slow"). Everything else -- every /api/* call and every page
# route (/crm, /me, /family, ...) -- is timed and recorded, since page loads
# are exactly what an operator wants visible in the summary.
_EXCLUDED_PREFIXES = ("/health", "/static")

_RouteKey = Tuple[str, str]  # (method, route_template)


def _is_excluded(path: str) -> bool:
    return path.startswith(_EXCLUDED_PREFIXES)


def _percentile(sorted_values: List[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list of values."""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


class RouteTimingStore:
    """Thread-safe, bounded rolling window of request durations per route.

    Keyed by (method, route_template) -- never the raw path or query string,
    so a person id embedded in a URL (e.g. `/api/crm/people/{person_id}`)
    never appears here, only its route template.

    `count`, `p50_ms`, `p95_ms`, and `max_ms` are computed over each route's
    current window (the most recent `window_size` samples). `slow_count` is
    also computed over that window, evaluated against the *current*
    `settings.slow_request_ms` rather than whatever the threshold was when
    each sample was recorded. `last_slow_at` is tracked independently of the
    window, so a slow request remains visible in the summary even after
    enough fast requests have rolled it out of the window.

    Handlers run in FastAPI's threadpool (#868), so recording must be safe
    under concurrent calls from multiple threads; a single lock around the
    small dict/deque mutations is cheap enough not to matter for overhead.
    """

    def __init__(self, window_size: int = _WINDOW_SIZE) -> None:
        self._window_size = window_size
        self._lock = threading.Lock()
        self._windows: Dict[_RouteKey, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        self._last_slow_at: Dict[_RouteKey, str] = {}

    def record(self, method: str, route: str, duration_ms: float) -> None:
        """Record one completed request's duration."""
        key = (method, route)
        is_slow = duration_ms >= settings.slow_request_ms
        with self._lock:
            self._windows[key].append(duration_ms)
            if is_slow:
                self._last_slow_at[key] = datetime.now(timezone.utc).isoformat()

    def summary(self) -> List[dict]:
        """Per-route stats, one row per (method, route) with samples in its
        window. Sorted by p95 descending so the slowest routes sort first."""
        threshold = settings.slow_request_ms
        with self._lock:
            snapshot = {
                key: list(durations)
                for key, durations in self._windows.items()
                if durations
            }
            last_slow = dict(self._last_slow_at)

        rows = []
        for (method, route), durations in snapshot.items():
            sorted_durations = sorted(durations)
            rows.append(
                {
                    "method": method,
                    "route": route,
                    "count": len(sorted_durations),
                    "p50_ms": round(_percentile(sorted_durations, 0.50), 1),
                    "p95_ms": round(_percentile(sorted_durations, 0.95), 1),
                    "max_ms": round(sorted_durations[-1], 1),
                    "slow_count": sum(1 for d in durations if d >= threshold),
                    "last_slow_at": last_slow.get((method, route)),
                }
            )
        rows.sort(key=lambda r: r["p95_ms"], reverse=True)
        return rows

    def reset(self) -> None:
        """Clear all recorded state. Test-only -- not exposed via any route."""
        with self._lock:
            self._windows.clear()
            self._last_slow_at.clear()


_store: Optional[RouteTimingStore] = None
_store_lock = threading.Lock()


def get_route_timing_store() -> RouteTimingStore:
    """Process-wide singleton, created lazily and thread-safely."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = RouteTimingStore()
    return _store


class RouteTimingMiddleware:
    """Pure-ASGI middleware timing every HTTP request (#877).

    Records duration, status, and response size into `RouteTimingStore`,
    keyed by (method, route template) read from `scope["route"]` once
    routing has resolved it -- never the raw path or query string, so a
    person id embedded in a URL never reaches a log line or the in-memory
    summary. Falls back to `"<unmatched>"` for a 404 (routing never set
    `scope["route"]`).

    Logs one WARNING per request slower than `settings.slow_request_ms`,
    with the method, route template, status, duration, and response bytes
    -- never the raw path.

    A pure-ASGI implementation (rather than `BaseHTTPMiddleware`, which
    buffers the whole response to hand back a `Response` object) so a
    streaming response (SSE chat) is timed to completion via its final body
    chunk, and passes every chunk through untouched and unbuffered.

    Registered outermost among this app's own middleware (added first, in
    `api/main.py`, before `CORSMiddleware` and the scoped gzip middleware)
    so its timing and byte count cover the full response, including gzip
    compression performed by a middleware nested inside it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or _is_excluded(scope["path"]):
            await self.app(scope, receive, send)
            return

        store = get_route_timing_store()
        start = time.perf_counter()
        status_code = 500  # default if the request raises before any response starts
        bytes_sent = 0

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code, bytes_sent
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                bytes_sent += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            method = scope.get("method", "")
            route = scope.get("route")
            route_template = getattr(route, "path", None) or "<unmatched>"
            store.record(method, route_template, duration_ms)
            if duration_ms >= settings.slow_request_ms:
                logger.warning(
                    "slow request: method=%s route=%s status=%s duration_ms=%.1f bytes=%d",
                    method,
                    route_template,
                    status_code,
                    duration_ms,
                    bytes_sent,
                )
