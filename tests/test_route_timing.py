"""
Tests for the per-route request timing middleware and summary.

Deliberately built against a *tiny* FastAPI app rather than `from api.main
import app` -- importing the real app pulls in chromadb/sentence-transformers
and is marked `slow` elsewhere in this suite (see test_calendar_api.py).
`RouteTimingMiddleware` and `RouteTimingStore` are self-contained enough not
to need it.
"""
import asyncio
import statistics
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from api.services.route_timing import RouteTimingMiddleware, RouteTimingStore, get_route_timing_store
from config.settings import settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_route_timing_store():
    """Every test in this file that builds a `RouteTimingMiddleware`-backed
    app records into the process-wide singleton (`get_route_timing_store()`)
    regardless of which FastAPI app instance it attaches to. Resetting
    before *and* after each test keeps assertions against that singleton
    order-independent, rather than relying on file/class ordering or
    scattered inline `store.reset()` calls."""
    store = get_route_timing_store()
    store.reset()
    yield
    store.reset()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RouteTimingMiddleware)

    @app.get("/fast")
    def fast():
        return {"ok": True}

    @app.get("/slow")
    def slow():
        time.sleep(0.05)
        return {"ok": True}

    @app.get("/items/{item_id}")
    def get_item(item_id: str):
        time.sleep(0.05)
        return {"item_id": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("synthetic failure for #877 tests")

    @app.get("/stream")
    def stream():
        async def gen():
            yield b"chunk-1-"
            await asyncio.sleep(0.03)
            yield b"chunk-2-"
            await asyncio.sleep(0.03)
            yield b"chunk-3"

        return StreamingResponse(gen(), media_type="text/plain")

    @app.get("/sse")
    def sse():
        async def gen():
            yield b"data: one\n\n"
            await asyncio.sleep(0.03)
            yield b"data: two\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/sse-charset")
    def sse_charset():
        # A content-type with parameters (as the real chat endpoint sends:
        # "text/event-stream; charset=utf-8") must still be recognized.
        async def gen():
            yield b"data: hi\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")

    @app.get("/sse-midstream-failure")
    def sse_midstream_failure():
        async def gen():
            yield b"data: partial\n\n"
            raise RuntimeError("synthetic mid-stream failure for #877 tests")

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/midstream-failure")
    def midstream_failure():
        async def gen():
            yield b"partial-body"
            raise RuntimeError("synthetic mid-stream failure for #877 tests")

        return StreamingResponse(gen(), media_type="text/plain")

    return app


class TestSlowRequestLogging:
    """Slow requests log one WARNING with the route template, not the raw path."""

    def test_slow_route_logs_once_with_template(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "slow_request_ms", 20)
        app = _build_app()
        client = TestClient(app)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            response = client.get("/items/synthetic-person-id-999")

        assert response.status_code == 200
        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert len(warnings) == 1, "slow route should log exactly one warning"
        message = warnings[0].getMessage()
        assert "/items/{item_id}" in message
        assert "synthetic-person-id-999" not in message, (
            "the raw path/id must never appear in the slow-request log line"
        )
        assert "GET" in message
        assert "status=200" in message

    def test_fast_route_does_not_log(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "slow_request_ms", 1000)
        app = _build_app()
        client = TestClient(app)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            response = client.get("/fast")

        assert response.status_code == 200
        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert warnings == []


class TestExceptionPath:
    """A raised exception is recorded (and logged, if slow) with status 500
    when nothing was sent yet, or the real sent status plus `aborted=true`
    when the response had already started."""

    def test_exception_before_response_records_status_500(self, monkeypatch, caplog):
        # Threshold of 0 makes every request "slow" so the log path (which
        # is what carries the status) always fires, deterministically.
        monkeypatch.setattr(settings, "slow_request_ms", 0)
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            response = client.get("/boom")

        assert response.status_code == 500
        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "/boom" in message
        assert "status=500" in message
        assert "aborted=true" not in message, (
            "no response had started yet -- this isn't a mid-stream abort"
        )

    def test_exception_after_response_started_logs_real_status_and_aborted(
        self, monkeypatch, caplog
    ):
        """A generator that raises after its first chunk has already sent a
        200 -- the client saw 200, so the log must say 200, not 500, and
        must mark the response as aborted."""
        monkeypatch.setattr(settings, "slow_request_ms", 0)
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            response = client.get("/midstream-failure")

        # httpx/TestClient reports whatever status line was actually sent.
        assert response.status_code == 200
        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "/midstream-failure" in message
        assert "status=200" in message, "must log the status actually sent, not a synthetic 500"
        assert "aborted=true" in message


class TestStreaming:
    """A streaming (SSE-like) response passes through untouched and is timed
    to completion rather than when the first chunk is sent."""

    def test_stream_passes_through_and_times_to_completion(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 100_000)  # don't log/care
        app = _build_app()
        client = TestClient(app)

        response = client.get("/stream")

        assert response.status_code == 200
        assert response.text == "chunk-1-chunk-2-chunk-3"

        # The route recorded a duration consistent with waiting for both
        # `asyncio.sleep(0.03)` calls to complete, not just the first chunk.
        # (media_type="text/plain" here, not SSE, so this goes through the
        # normal duration-based summary() -- see TestSSEExclusion for the
        # text/event-stream case.)
        rows = get_route_timing_store().summary()
        stream_row = next(r for r in rows if r["route"] == "/stream")
        assert stream_row["count"] >= 1
        assert stream_row["max_ms"] >= 50, (
            "duration should reflect waiting for the full stream, not the first chunk"
        )


class TestSSEExclusion:
    """A `text/event-stream` response is tracked separately from every other
    route: it never appears in `summary()`, never counts toward
    `slow_count`, and never fires the slow-request log -- an SSE
    connection's duration is however long the client kept it open, not a
    latency measurement."""

    def test_sse_response_excluded_from_routes_summary_and_log(self, monkeypatch, caplog):
        # A near-zero threshold would make an ordinary route log on every
        # request; an SSE route must still never log, proving the exclusion
        # isn't just "happens not to be slow enough".
        monkeypatch.setattr(settings, "slow_request_ms", 0)
        app = _build_app()
        client = TestClient(app)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            response = client.get("/sse")

        assert response.status_code == 200
        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert warnings == [], "an SSE response must never trigger the slow-request log"

        store = get_route_timing_store()
        assert all(r["route"] != "/sse" for r in store.summary()), (
            "an SSE response must never appear in the duration-based routes summary"
        )

        stream_rows = store.stream_summary()
        row = next(r for r in stream_rows if r["route"] == "/sse")
        assert row["method"] == "GET"
        assert row["count"] == 1
        assert row["bytes"] == len(b"data: one\n\ndata: two\n\n")
        assert set(row.keys()) == {"method", "route", "count", "bytes"}, (
            "stream rows carry no duration/percentile/slow_count fields"
        )

    def test_content_type_with_charset_param_is_still_recognized_as_sse(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 0)
        app = _build_app()
        client = TestClient(app)

        response = client.get("/sse-charset")
        assert response.status_code == 200

        store = get_route_timing_store()
        assert any(r["route"] == "/sse-charset" for r in store.stream_summary())
        assert all(r["route"] != "/sse-charset" for r in store.summary())

    def test_sse_midstream_failure_still_recorded_as_stream_not_logged(self, monkeypatch, caplog):
        """Even if an SSE generator dies mid-stream, it stays a stream
        record, not a duration/slow-log entry -- content-type, not success,
        decides which bucket a response lands in."""
        monkeypatch.setattr(settings, "slow_request_ms", 0)
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level("WARNING", logger="api.services.route_timing"):
            client.get("/sse-midstream-failure")

        warnings = [r for r in caplog.records if r.name == "api.services.route_timing"]
        assert warnings == []

        store = get_route_timing_store()
        assert any(r["route"] == "/sse-midstream-failure" for r in store.stream_summary())
        assert all(r["route"] != "/sse-midstream-failure" for r in store.summary())


class _FakeRoute:
    def __init__(self, path: str) -> None:
        self.path = path


class TestPathsend:
    """`http.response.pathsend` (zero-copy file responses, e.g. `FileResponse`
    when the ASGI server advertises the extension) is accounted for from the
    preceding `http.response.start`'s Content-Length header, without
    stat'ing the file again on the event loop.

    Driven directly against the middleware with a fake inner ASGI app,
    since neither `TestClient`'s transport nor uvicorn on this host
    advertises `http.response.pathsend` -- there is no way to exercise this
    message type through a real request in this test environment."""

    async def test_pathsend_bytes_come_from_content_length_header(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "slow_request_ms", 0)  # force the log line

        async def fake_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-length", b"12345"),
                        (b"content-type", b"text/html; charset=utf-8"),
                    ],
                }
            )
            await send({"type": "http.response.pathsend", "path": "/tmp/does-not-need-to-exist.html"})

        scope = {"type": "http", "method": "GET", "path": "/crm", "route": _FakeRoute("/crm")}

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        middleware = RouteTimingMiddleware(fake_app)
        logger_name = "api.services.route_timing"
        with caplog.at_level("WARNING", logger=logger_name):
            await middleware(scope, receive, send)

        warnings = [r for r in caplog.records if r.name == logger_name]
        assert len(warnings) == 1
        assert "bytes=12345" in warnings[0].getMessage()

    async def test_pathsend_without_content_length_reads_as_zero_bytes(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "slow_request_ms", 0)

        async def fake_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.pathsend", "path": "/tmp/does-not-need-to-exist.html"})

        scope = {"type": "http", "method": "GET", "path": "/crm", "route": _FakeRoute("/crm")}

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        middleware = RouteTimingMiddleware(fake_app)
        logger_name = "api.services.route_timing"
        with caplog.at_level("DEBUG", logger=logger_name):
            await middleware(scope, receive, send)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "bytes=0" in warnings[0].getMessage()
        debug_notes = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any("Content-Length" in r.getMessage() for r in debug_notes)


class TestRouteTimingStore:
    """Unit tests for the store's rolling-window math, independent of ASGI.

    Uses its own `RouteTimingStore()` instances throughout, not the process
    singleton -- these don't need the autouse fixture's reset."""

    def test_summary_shape_and_percentiles(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 500)
        store = RouteTimingStore(window_size=200)
        for d in [10, 20, 30, 40, 5000]:  # last sample is "slow"
            store.record("GET", "/api/example", d)

        rows = store.summary()
        assert len(rows) == 1
        row = rows[0]
        assert row["method"] == "GET"
        assert row["route"] == "/api/example"
        assert row["count"] == 5
        assert row["max_ms"] == 5000
        assert row["slow_count"] == 1
        assert row["last_slow_at"] is not None
        assert 0 < row["p50_ms"] <= row["p95_ms"] <= row["max_ms"]

    def test_window_is_bounded(self):
        store = RouteTimingStore(window_size=10)
        for i in range(100):
            store.record("GET", "/api/many", float(i))

        row = store.summary()[0]
        assert row["count"] == 10

    def test_routes_with_no_samples_are_omitted(self):
        store = RouteTimingStore()
        assert store.summary() == []

    def test_thread_safety_concurrent_recording(self):
        """Many threads recording concurrently must not corrupt state or raise."""
        store = RouteTimingStore(window_size=200)
        threads_count = 20
        records_per_thread = 10

        def worker(offset: int):
            for i in range(records_per_thread):
                store.record("GET", "/api/concurrent", float(offset + i))

        threads = [
            threading.Thread(target=worker, args=(t * records_per_thread,))
            for t in range(threads_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = store.summary()
        assert len(rows) == 1
        row = rows[0]
        # window_size=200 == threads_count * records_per_thread, so nothing
        # should have rolled out, and every recorded value should be intact.
        assert row["count"] == threads_count * records_per_thread
        assert row["max_ms"] == float(threads_count * records_per_thread - 1)

    def test_stream_summary_shape_and_totals(self):
        store = RouteTimingStore()
        store.record_stream("GET", "/api/agents/stream", 1000)
        store.record_stream("GET", "/api/agents/stream", 500)
        store.record_stream("GET", "/api/other/stream", 42)

        rows = store.stream_summary()
        assert len(rows) == 2
        by_route = {r["route"]: r for r in rows}
        assert by_route["/api/agents/stream"]["count"] == 2
        assert by_route["/api/agents/stream"]["bytes"] == 1500
        assert by_route["/api/other/stream"]["count"] == 1
        assert by_route["/api/other/stream"]["bytes"] == 42
        # Sorted by bytes descending.
        assert rows[0]["route"] == "/api/agents/stream"

    def test_reset_clears_stream_state_too(self):
        store = RouteTimingStore()
        store.record_stream("GET", "/api/agents/stream", 1000)
        store.reset()
        assert store.stream_summary() == []


class TestPerfRoutesEndpoint:
    """Shape of GET /api/perf/routes."""

    def test_shape(self, monkeypatch):
        from api.routes import perf as perf_routes

        monkeypatch.setattr(settings, "slow_request_ms", 500)
        store = get_route_timing_store()
        store.record("GET", "/api/example", 12.0)
        store.record("GET", "/api/example", 999.0)
        store.record_stream("GET", "/api/agents/stream", 4096)

        app = FastAPI()
        app.include_router(perf_routes.router)
        client = TestClient(app)

        response = client.get("/api/perf/routes")
        assert response.status_code == 200
        body = response.json()
        assert "routes" in body and "count" in body
        assert "streams" in body and "stream_count" in body
        assert body["count"] == len(body["routes"])
        assert body["stream_count"] == len(body["streams"])

        row = next(r for r in body["routes"] if r["route"] == "/api/example")
        for field in ("method", "route", "count", "p50_ms", "p95_ms", "max_ms", "slow_count", "last_slow_at"):
            assert field in row
        assert row["count"] == 2
        assert row["slow_count"] == 1
        assert all(r["route"] != "/api/agents/stream" for r in body["routes"])

        stream_row = next(r for r in body["streams"] if r["route"] == "/api/agents/stream")
        for field in ("method", "route", "count", "bytes"):
            assert field in stream_row
        assert stream_row["count"] == 1
        assert stream_row["bytes"] == 4096


class TestOverhead:
    """The middleware must add well under 1ms of overhead per request.

    Bare and instrumented requests are interleaved *within* each iteration
    and measured as the median of the paired per-iteration deltas, rather
    than as two sequential phases -- a sequential measurement puts any
    change in host load between the phases entirely into the delta, which
    fails ~50% of the time on a busy shared host even though the
    middleware's actual overhead is well under budget."""

    def test_overhead_under_1ms_median_paired(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 100_000)  # never log

        bare_app = FastAPI()

        @bare_app.get("/trivial")
        def _bare():
            return {"ok": True}

        instrumented_app = FastAPI()
        instrumented_app.add_middleware(RouteTimingMiddleware)

        @instrumented_app.get("/trivial")
        def _instrumented():
            return {"ok": True}

        bare_client = TestClient(bare_app)
        instrumented_client = TestClient(instrumented_app)

        iterations = 200
        warmup = 20

        for _ in range(warmup):
            bare_client.get("/trivial")
            instrumented_client.get("/trivial")

        deltas_ms = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            bare_client.get("/trivial")
            t1 = time.perf_counter()
            instrumented_client.get("/trivial")
            t2 = time.perf_counter()

            bare_ms = (t1 - t0) * 1000
            instrumented_ms = (t2 - t1) * 1000
            deltas_ms.append(instrumented_ms - bare_ms)

        median_delta_ms = statistics.median(deltas_ms)

        assert median_delta_ms < 1.0, (
            f"median paired overhead {median_delta_ms:.3f}ms exceeds 1ms budget"
        )


class TestExclusionAllowlist:
    """Health and static routes are excluded from the summary entirely, and
    only as a full path segment -- a route that merely starts with the same
    characters (e.g. `/healthz-admin`) is not swept in (matching the
    `_in_gzip_scope` convention in api/main.py)."""

    def test_health_and_static_are_excluded(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 100_000)
        app = FastAPI()
        app.add_middleware(RouteTimingMiddleware)

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get("/health/full")
        def health_full():
            return {"status": "ok"}

        @app.get("/static/app.js")
        def static_asset():
            return {"ignored": True}

        client = TestClient(app)
        client.get("/health")
        client.get("/health/full")
        client.get("/static/app.js")

        assert get_route_timing_store().summary() == []

    def test_lookalike_routes_are_not_excluded(self, monkeypatch):
        """A route that merely starts with "/health" or "/static" as a
        substring, rather than as a full path segment, must still be
        recorded."""
        monkeypatch.setattr(settings, "slow_request_ms", 100_000)
        app = FastAPI()
        app.add_middleware(RouteTimingMiddleware)

        @app.get("/healthz-admin")
        def healthz_admin():
            return {"ignored": False}

        @app.get("/staticmaps")
        def staticmaps():
            return {"ignored": False}

        client = TestClient(app)
        client.get("/healthz-admin")
        client.get("/staticmaps")

        recorded_routes = {r["route"] for r in get_route_timing_store().summary()}
        assert "/healthz-admin" in recorded_routes
        assert "/staticmaps" in recorded_routes
