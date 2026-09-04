"""
Tests for the per-route request timing middleware and summary (#877).

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

from api.services.route_timing import RouteTimingMiddleware, RouteTimingStore
from config.settings import settings

pytestmark = pytest.mark.unit


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
    """A raised exception is recorded (and logged, if slow) with status 500."""

    def test_exception_records_status_500(self, monkeypatch, caplog):
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
        from api.services.route_timing import get_route_timing_store

        rows = get_route_timing_store().summary()
        stream_row = next(r for r in rows if r["route"] == "/stream")
        assert stream_row["count"] >= 1
        assert stream_row["max_ms"] >= 50, (
            "duration should reflect waiting for the full stream, not the first chunk"
        )


class TestRouteTimingStore:
    """Unit tests for the store's rolling-window math, independent of ASGI."""

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


class TestPerfRoutesEndpoint:
    """Shape of GET /api/perf/routes."""

    def test_shape(self, monkeypatch):
        from api.routes import perf as perf_routes
        from api.services.route_timing import get_route_timing_store

        monkeypatch.setattr(settings, "slow_request_ms", 500)
        store = get_route_timing_store()
        store.reset()
        store.record("GET", "/api/example", 12.0)
        store.record("GET", "/api/example", 999.0)

        app = FastAPI()
        app.include_router(perf_routes.router)
        client = TestClient(app)

        response = client.get("/api/perf/routes")
        assert response.status_code == 200
        body = response.json()
        assert "routes" in body and "count" in body
        assert body["count"] == len(body["routes"])
        row = next(r for r in body["routes"] if r["route"] == "/api/example")
        for field in ("method", "route", "count", "p50_ms", "p95_ms", "max_ms", "slow_count", "last_slow_at"):
            assert field in row
        assert row["count"] == 2
        assert row["slow_count"] == 1

        store.reset()


class TestOverhead:
    """The middleware must add well under 1ms of overhead per request,
    measured as a median over many requests to avoid flaking on scheduling
    noise (per #877's acceptance criteria)."""

    def test_overhead_under_1ms_median(self, monkeypatch):
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

        bare_times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            bare_client.get("/trivial")
            bare_times.append(time.perf_counter() - t0)

        instrumented_times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            instrumented_client.get("/trivial")
            instrumented_times.append(time.perf_counter() - t0)

        bare_median_ms = statistics.median(bare_times) * 1000
        instrumented_median_ms = statistics.median(instrumented_times) * 1000
        overhead_ms = instrumented_median_ms - bare_median_ms

        assert overhead_ms < 1.0, (
            f"median overhead {overhead_ms:.3f}ms exceeds 1ms budget "
            f"(bare={bare_median_ms:.3f}ms, instrumented={instrumented_median_ms:.3f}ms)"
        )


class TestExclusionAllowlist:
    """Health and static routes are excluded from the summary entirely."""

    def test_health_and_static_are_excluded(self, monkeypatch):
        monkeypatch.setattr(settings, "slow_request_ms", 100_000)
        app = FastAPI()
        app.add_middleware(RouteTimingMiddleware)

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get("/static/app.js")
        def static_asset():
            return {"ignored": True}

        from api.services.route_timing import get_route_timing_store

        store = get_route_timing_store()
        store.reset()
        client = TestClient(app)
        client.get("/health")
        client.get("/static/app.js")

        assert store.summary() == []
        store.reset()
