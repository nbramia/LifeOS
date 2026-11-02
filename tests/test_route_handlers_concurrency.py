"""
A slow CRM request must not stall an unrelated fast one.

Every CRM/people handler with no `await` runs as a plain `def`, so FastAPI
dispatches it to the worker threadpool instead of running it inline on the
single event loop. A slow request (a full people-list scan, a relationship
tone analysis) must not block other requests on the process: `GET
/api/crm/config` must complete while either is still in flight.

The proof is ordering, not elapsed time: each slow handler is held on a
`threading.Event` until the fast request has returned, so a fast request that
finishes first can only have been served while the slow handlers were blocked
off the event loop. A wall-clock bound would also measure interpreter pauses
(a full garbage collection stops every thread) and host load.

This builds a router-only app (`FastAPI()` + `include_router(crm_router)`)
rather than the real `api.main.app`, and enters `TestClient` as a context
manager against *that* — a bare `client.get()` outside a `with` block gets
its own throwaway event loop per request and would never reproduce the bug,
but the real app's lifespan starts Telegram listeners, the scheduler, the
job-queue worker, and file watchers, and writes Dashboard files into the
configured vault — side effects a "unit" test must not have, and ones that
would collide with an already-running production server's own listeners on
a real deployment. A router-only app keeps the one thing the bug actually
needs (a single shared event loop dispatching `def` handlers to the
threadpool) without booting anything else.

Both slow requests are made deterministic by monkeypatching a store method
to block on the gate and return an empty result, so this does not depend on
`data/` existing or having any particular size:
- people-list scenario: `person_entity_store.get_all()` (what `GET
  /api/crm/people` calls).
- tone-analysis scenario:
  `interaction_store.get_monthly_interaction_counts_in_range()` (the first
  blocking call in `analyze_relationship_tone_detailed`).
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.crm import router as crm_router

pytestmark = pytest.mark.unit

SLOW_REQUESTS = 4
# Generous: neither bound is a latency assertion. A handler running inline on
# the event loop never lets the fast request finish while it is held, so any
# finite wait separates the two cases.
GATE_WAIT_SECONDS = 30
FAST_REQUEST_WAIT_SECONDS = 15


class _SlowGate:
    """Holds slow handlers inside their patched call until `release` is set.

    `entered` is set once `expected_entries` handlers are blocked in `block()`;
    `exited` counts handlers that have returned from it."""

    def __init__(self, expected_entries: int = SLOW_REQUESTS):
        self._expected = expected_entries
        self._lock = threading.Lock()
        self._entries = 0
        self.exited = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def block(self):
        with self._lock:
            self._entries += 1
            if self._entries >= self._expected:
                self.entered.set()
        self.release.wait(timeout=GATE_WAIT_SECONDS * 2)
        with self._lock:
            self.exited += 1


def _router_only_app() -> FastAPI:
    """A minimal app carrying only the CRM router — no lifespan, no other
    routers, no side effects. Enough to reproduce the event-loop-sharing bug
    without starting Telegram/scheduler/job-queue/watchers."""
    app = FastAPI()
    app.include_router(crm_router)
    return app


def _assert_fast_request_not_blocked(client, gate, *, slow_request):
    """Fire `SLOW_REQUESTS` concurrent slow requests via `slow_request(client)`,
    wait until the gate's expected handlers are blocked on it, then
    assert `GET /api/crm/config` completes while every one of them is still
    held. The gate is released only afterwards (and always, on failure)."""
    with ThreadPoolExecutor(max_workers=SLOW_REQUESTS + 1) as pool:
        try:
            slow_futures = [pool.submit(slow_request, client) for _ in range(SLOW_REQUESTS)]
            assert gate.entered.wait(timeout=GATE_WAIT_SECONDS), (
                "the slow handlers never all reached the gate -- a handler is "
                "running inline on the event loop and starving the others"
            )

            fast_future = pool.submit(client.get, "/api/crm/config")
            try:
                fast_response = fast_future.result(timeout=FAST_REQUEST_WAIT_SECONDS)
            except FutureTimeoutError:
                pytest.fail(
                    "GET /api/crm/config did not complete while slow requests "
                    "were held -- a handler is running inline on the event loop"
                )
            assert gate.exited == 0, "a slow handler finished before the fast request did"
        finally:
            gate.release.set()

        for future in slow_futures:
            assert future.result(timeout=GATE_WAIT_SECONDS).status_code == 200

    assert fast_response.status_code == 200


def test_fast_config_request_not_blocked_by_concurrent_slow_people_list(monkeypatch):
    """4 concurrent `GET /api/crm/people?limit=300` held in flight must not
    stop `GET /api/crm/config` from completing."""
    from api.services.person_entity import get_person_entity_store

    store = get_person_entity_store()

    gate = _SlowGate()

    def slow_get_all(*args, **kwargs):
        # Blocks only — deliberately does not fall through to the real
        # get_all(), so this test is independent of how much data (if any)
        # data/crm.db holds.
        gate.block()
        return []

    monkeypatch.setattr(store, "get_all", slow_get_all)

    with TestClient(_router_only_app()) as client:
        _assert_fast_request_not_blocked(
            client,
            gate,
            slow_request=lambda c: c.get("/api/crm/people?limit=300"),
        )


def test_fast_config_request_not_blocked_by_concurrent_tone_analysis(monkeypatch):
    """`POST /api/crm/relationship/tone-analysis-detailed` requests held in
    flight must not stop `GET /api/crm/config` from completing."""
    from api.services.interaction_store import get_interaction_store

    interaction_store = get_interaction_store()

    gate = _SlowGate()

    def slow_monthly_counts(*args, **kwargs):
        # Blocks only, same reasoning as above — tone analysis's first
        # blocking call is the per-month interaction count query.
        gate.block()
        return {}

    monkeypatch.setattr(interaction_store, "get_monthly_interaction_counts_in_range", slow_monthly_counts)

    with TestClient(_router_only_app()) as client:
        _assert_fast_request_not_blocked(
            client,
            gate,
            slow_request=lambda c: c.post("/api/crm/relationship/tone-analysis-detailed"),
        )
