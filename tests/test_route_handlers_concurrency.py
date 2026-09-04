"""
Concurrency regression: a slow CRM request must not stall an unrelated fast
one (#868).

Before this fix, every CRM/people handler ran `async def` with no `await`,
so FastAPI ran it inline on the single event loop instead of dispatching it
to the worker threadpool. A slow request (a full people-list scan, a
relationship tone analysis) then blocked every other request on the process
until it finished — measured on production as a 3 ms request taking 3.4 s
behind four people-list calls, or 14 s behind a tone analysis.

This starts the real app with `TestClient` as a context manager, which (per
Starlette) keeps one shared portal/event loop alive for the client's
lifetime — the same single-event-loop model the production server uses,
unlike a bare `client.get()` call outside a `with` block, which gets its own
throwaway loop per request and would never reproduce the bug. It then fires
several concurrent slow requests and asserts a trivial one finishes quickly
while they're in flight.

The slow request is made deterministic by monkeypatching the person store's
`get_all()` (what `GET /api/crm/people` calls) to sleep and return an empty
list, so this does not depend on `data/crm.db` existing or having any
particular size.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

SLEEP_SECONDS = 1.5
# Generous compared to the issue's <100ms production target: this asserts
# the fast request is NOT serialized behind the slow ones (which would push
# it past SLEEP_SECONDS), not that it hits the production latency bound on
# potentially loaded CI hardware.
FAST_REQUEST_BOUND_SECONDS = 0.75


@pytest.fixture
def app_client(monkeypatch):
    from api.main import app
    from api.services.person_entity import get_person_entity_store

    store = get_person_entity_store()

    def slow_get_all(*args, **kwargs):
        # Sleep only — deliberately does not fall through to the real
        # get_all(), so this test's timing is independent of how much data
        # (if any) data/crm.db holds.
        time.sleep(SLEEP_SECONDS)
        return []

    monkeypatch.setattr(store, "get_all", slow_get_all)

    with TestClient(app) as client:
        yield client


def test_fast_config_request_not_blocked_by_concurrent_slow_people_list(app_client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        slow_futures = [
            pool.submit(app_client.get, "/api/crm/people?limit=300")
            for _ in range(4)
        ]
        # Give the slow requests time to actually start running.
        time.sleep(0.2)

        start = time.time()
        fast_response = app_client.get("/api/crm/config")
        elapsed = time.time() - start

        for future in slow_futures:
            slow_response = future.result(timeout=SLEEP_SECONDS + 5)
            assert slow_response.status_code == 200

    assert fast_response.status_code == 200
    assert elapsed < FAST_REQUEST_BOUND_SECONDS, (
        f"GET /api/crm/config took {elapsed:.2f}s with 4 slow people-list "
        f"requests in flight (bound {FAST_REQUEST_BOUND_SECONDS}s) — it "
        "looks like a handler is running inline on the event loop again"
    )
