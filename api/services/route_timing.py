"""
Per-route request timing.

Complements `api/services/perf_trace.py` (which records LLM/chat-turn spans
for a conversation) with an always-on summary of every HTTP request: how
long it took, whether it counted as slow, and how large the response was.
This is a live "what's slow right now" signal -- process-local, bounded, and
reset on restart, not a persisted history.

Usage:
    from api.services.route_timing import RouteTimingMiddleware, get_route_timing_store

    app.add_middleware(RouteTimingMiddleware)  # registered outermost (api/main.py)
    get_route_timing_store().summary()         # -> list of per-route stat dicts
    get_route_timing_store().stream_summary()  # -> list of per-route stream stat dicts
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
#
# Matched as a full path segment, not a bare prefix (same convention as
# `_in_gzip_scope` in api/main.py) -- so a future `/healthz-admin` or
# `/staticmaps` route isn't silently swept into the exclusion.
_EXCLUDED_ROUTES = ("/health", "/static")

_RouteKey = Tuple[str, str]  # (method, route_template)

_SSE_CONTENT_TYPE = "text/event-stream"


def _is_excluded(path: str) -> bool:
    return any(path == route or path.startswith(route + "/") for route in _EXCLUDED_ROUTES)


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

    Long-lived streaming responses (SSE) are tracked separately in
    `stream_summary()` -- see `record_stream()` for why: a duration that
    equals "how long the browser tab was open" is not a latency signal and
    must never dominate this summary or the slow-request log.

    Handlers run in FastAPI's threadpool, so recording must be safe
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
        self._stream_counts: Dict[_RouteKey, int] = defaultdict(int)
        self._stream_bytes: Dict[_RouteKey, int] = defaultdict(int)

    def record(self, method: str, route: str, duration_ms: float) -> None:
        """Record one completed (non-streaming) request's duration."""
        key = (method, route)
        is_slow = duration_ms >= settings.slow_request_ms
        with self._lock:
            self._windows[key].append(duration_ms)
            if is_slow:
                self._last_slow_at[key] = datetime.now(timezone.utc).isoformat()

    def record_stream(self, method: str, route: str, bytes_sent: int) -> None:
        """Record one completed streaming (`text/event-stream`) response.

        No duration is recorded: an SSE connection's "duration" is however
        long the client kept it open (a browser tab, an agent transcript
        viewer), not a latency measurement, so it would otherwise dominate
        `summary()`'s p95-sorted table and fire a false slow-request
        warning on every disconnect. Only count and total bytes are kept.
        """
        key = (method, route)
        with self._lock:
            self._stream_counts[key] += 1
            self._stream_bytes[key] += max(bytes_sent, 0)

    def summary(self) -> List[dict]:
        """Per-route stats, one row per (method, route) with samples in its
        window. Sorted by p95 descending so the slowest routes sort first.

        Never includes streaming responses -- see `stream_summary()`.
        """
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

    def stream_summary(self) -> List[dict]:
        """Per-route stats for streaming (`text/event-stream`) responses:
        count and total bytes only -- no duration, no percentiles, no
        slow_count. Sorted by bytes descending."""
        with self._lock:
            counts = dict(self._stream_counts)
            byte_totals = {key: total for key, total in self._stream_bytes.items() if key in counts}

        rows = [
            {"method": method, "route": route, "count": count, "bytes": byte_totals.get((method, route), 0)}
            for (method, route), count in counts.items()
            if count
        ]
        rows.sort(key=lambda r: r["bytes"], reverse=True)
        return rows

    def reset(self) -> None:
        """Clear all recorded state. Test-only -- not exposed via any route."""
        with self._lock:
            self._windows.clear()
            self._last_slow_at.clear()
            self._stream_counts.clear()
            self._stream_bytes.clear()


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


def _header_value(headers: List[Tuple[bytes, bytes]], name: bytes) -> Optional[bytes]:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class RouteTimingMiddleware:
    """Pure-ASGI middleware timing every HTTP request.

    Records duration, status, and response size into `RouteTimingStore`,
    keyed by (method, route template) read from `scope["route"]` once
    routing has resolved it -- never the raw path or query string, so a
    person id embedded in a URL never reaches a log line or the in-memory
    summary. Falls back to `"<unmatched>"` for a 404 (routing never set
    `scope["route"]`).

    A `text/event-stream` response (SSE) is recorded separately, via
    `RouteTimingStore.record_stream()` -- count and bytes only, never a
    duration, and never subject to the slow-request log. An SSE connection's
    lifetime is however long the client kept it open, not a latency
    measurement; timing it as a normal request would make a page-open
    artifact dominate the summary and fire a false slow-request warning on
    every disconnect.

    Logs one WARNING per non-streaming request slower than
    `settings.slow_request_ms`, with the method, route template, status,
    duration, and response bytes -- never the raw path. A request that
    raises after its response already started (e.g. a stream that dies
    mid-flight) logs the status actually sent to the client plus
    `aborted=true`, rather than the misleading `status=500` a client never
    saw.

    A pure-ASGI implementation (rather than `BaseHTTPMiddleware`, which
    buffers the whole response to hand back a `Response` object): every
    chunk is passed straight through and the middleware waits for the
    stream's final chunk before recording. A non-SSE streaming response is
    timed to that final chunk; an SSE response is recorded by count and
    bytes only (see `record_stream()` above), never a duration.

    Registered outermost among this app's own middleware (added *last*, in
    `api/main.py`, after `CORSMiddleware` and the scoped gzip middleware --
    each `add_middleware` call wraps around everything added before it) so
    its timing and byte count cover the full response, including gzip
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
        response_started = False
        is_stream = False
        content_length_hint: Optional[bytes] = None

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code, bytes_sent, response_started, is_stream, content_length_hint
            message_type = message["type"]
            if message_type == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = message.get("headers") or []
                content_type = _header_value(headers, b"content-type")
                if content_type is not None:
                    is_stream = (
                        content_type.split(b";", 1)[0].strip().lower()
                        == _SSE_CONTENT_TYPE.encode("ascii")
                    )
                content_length_hint = _header_value(headers, b"content-length")
            elif message_type == "http.response.body":
                bytes_sent += len(message.get("body") or b"")
            elif message_type == "http.response.pathsend":
                # Zero-copy file response (FileResponse when the ASGI server
                # advertises the extension) -- no body message is ever sent,
                # so there is nothing to count bytes from directly. FileResponse
                # always sets Content-Length from the file's stat result on the
                # preceding http.response.start, so use that instead of
                # stat'ing the file again (which would be a blocking syscall
                # on the event loop). If it's somehow absent, byte count for
                # this response reads as 0 -- logged once at debug level
                # rather than guessed.
                if content_length_hint is not None:
                    try:
                        bytes_sent += int(content_length_hint)
                    except (TypeError, ValueError):
                        logger.debug(
                            "route_timing: unparseable Content-Length for pathsend response"
                        )
                else:
                    logger.debug(
                        "route_timing: pathsend response with no Content-Length header; "
                        "byte count for this response will read as 0"
                    )
            await send(message)

        exc_occurred = False
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            exc_occurred = True
            if not response_started:
                status_code = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            method = scope.get("method", "")
            route = scope.get("route")
            route_template = getattr(route, "path", None) or "<unmatched>"

            if is_stream:
                store.record_stream(method, route_template, bytes_sent)
            else:
                store.record(method, route_template, duration_ms)
                if duration_ms >= settings.slow_request_ms:
                    if exc_occurred and response_started:
                        logger.warning(
                            "slow request: method=%s route=%s status=%s duration_ms=%.1f "
                            "bytes=%d aborted=true",
                            method,
                            route_template,
                            status_code,
                            duration_ms,
                            bytes_sent,
                        )
                    else:
                        logger.warning(
                            "slow request: method=%s route=%s status=%s duration_ms=%.1f bytes=%d",
                            method,
                            route_template,
                            status_code,
                            duration_ms,
                            bytes_sent,
                        )

