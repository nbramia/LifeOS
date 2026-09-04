"""
Concurrency regression: a slow CRM request must not stall an unrelated fast
one (#868).

Before this fix, every CRM/people handler ran `async def` with no `await`,
so FastAPI ran it inline on the single event loop instead of dispatching it
to the worker threadpool. A slow request (a full people-list scan, a
relationship tone analysis) then blocked every other request on the process
until it finished — measured on production as a 3 ms request taking 3.4 s
behind four people-list calls, or 14.1 s behind a tone analysis. The issue's
acceptance criteria give both scenarios an explicit bound: `GET
/api/crm/config` must complete in under 100 ms while either is in flight.

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
to sleep and return an empty result, so this does not depend on `data/`
existing or having any particular size:
- people-list scenario: `person_entity_store.get_all()` (what `GET
  /api/crm/people` calls).
- tone-analysis scenario: `interaction_store.get_for_person()` (the first
  blocking call in `analyze_relationship_tone_detailed`).
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.crm import router as crm_router

pytestmark = pytest.mark.unit

SLEEP_SECONDS = 1.5
# The issue's own acceptance bound. Measured non-flaky even under deliberate
# full-CPU saturation (idle: low-single-digit ms; 32 busy-loops on a 32-core
# box: still under 10ms) — a 100x margin under contention, so 100ms asserts
# the criterion instead of just approximating it.
FAST_REQUEST_BOUND_SECONDS = 0.1


def _router_only_app() -> FastAPI:
    """A minimal app carrying only the CRM router — no lifespan, no other
    routers, no side effects. Enough to reproduce the event-loop-sharing bug
    without starting Telegram/scheduler/job-queue/watchers."""
    app = FastAPI()
    app.include_router(crm_router)
    return app


def _assert_fast_request_not_blocked(client, *, slow_request):
    """Fire 4 concurrent slow requests via `slow_request(client)`, then
    assert `GET /api/crm/config` completes well under the slow requests'
    sleep while they're in flight."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        slow_futures = [pool.submit(slow_request, client) for _ in range(4)]
        # Give the slow requests time to actually start running.
        time.sleep(0.2)

        start = time.time()
        fast_response = client.get("/api/crm/config")
        elapsed = time.time() - start

        for future in slow_futures:
            slow_response = future.result(timeout=SLEEP_SECONDS + 5)
            assert slow_response.status_code == 200

    assert fast_response.status_code == 200
    assert elapsed < FAST_REQUEST_BOUND_SECONDS, (
        f"GET /api/crm/config took {elapsed:.3f}s with slow requests in "
        f"flight (bound {FAST_REQUEST_BOUND_SECONDS}s) — it looks like a "
        "handler is running inline on the event loop again"
    )


def test_fast_config_request_not_blocked_by_concurrent_slow_people_list(monkeypatch):
    """Acceptance criterion 1: 4 concurrent `GET /api/crm/people?limit=300`
    in flight must not push `GET /api/crm/config` past 100ms."""
    from api.services.person_entity import get_person_entity_store

    store = get_person_entity_store()

    def slow_get_all(*args, **kwargs):
        # Sleep only — deliberately does not fall through to the real
        # get_all(), so this test's timing is independent of how much data
        # (if any) data/crm.db holds.
        time.sleep(SLEEP_SECONDS)
        return []

    monkeypatch.setattr(store, "get_all", slow_get_all)

    with TestClient(_router_only_app()) as client:
        _assert_fast_request_not_blocked(
            client,
            slow_request=lambda c: c.get("/api/crm/people?limit=300"),
        )


def test_fast_config_request_not_blocked_by_concurrent_tone_analysis(monkeypatch):
    """Acceptance criterion 2: a `POST /api/crm/relationship/tone-analysis-detailed`
    in flight must not push `GET /api/crm/config` past 100ms."""
    from api.services.interaction_store import get_interaction_store

    interaction_store = get_interaction_store()

    def slow_get_for_person(*args, **kwargs):
        # Sleep only, same reasoning as above — tone analysis's first
        # blocking call is interaction_store.get_for_person().
        time.sleep(SLEEP_SECONDS)
        return []

    monkeypatch.setattr(interaction_store, "get_for_person", slow_get_for_person)

    with TestClient(_router_only_app()) as client:
        _assert_fast_request_not_blocked(
            client,
            slow_request=lambda c: c.post("/api/crm/relationship/tone-analysis-detailed"),
        )
