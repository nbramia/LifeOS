"""
Concurrent merges must serialize (#868 review finding 3).

Converting the CRM/people/photos handlers from `async def` with no `await`
to plain `def` (#868) restored fairness by dispatching them to the worker
threadpool instead of running them start-to-finish inline on the event
loop — but that inline run was also, incidentally, every mutating
handler's only serialization against every other request. `merge_people`
delegates to `scripts/merge_people.merge_people`, which keeps a single
global merge-intent log and does a read-modify-write of the merged-ids
file; two concurrent merges interleaving there can corrupt that
bookkeeping. `api/routes/crm.py` now holds `_mutation_lock` (a module-level
`threading.Lock`) across `merge_people` and the other write handlers named
in the review (split, hide, review-queue confirm/reject, the sync-trigger
POSTs) to restore that serialization explicitly.

This test proves the lock actually works: it patches the merge function
`merge_people` calls (`scripts.merge_people.merge_people`, imported locally
inside the handler on every call) with a fake that records whether it was
ever entered while another call was still inside it, fires two concurrent
`POST /api/crm/people/merge` requests, and asserts no overlap was ever
observed.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.crm import router as crm_router

pytestmark = pytest.mark.unit


def _router_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(crm_router)
    return app


def test_concurrent_merges_serialize(monkeypatch):
    fake_person_store = MagicMock()
    fake_person_store.get_by_id.return_value = MagicMock()  # any id "exists"
    monkeypatch.setattr(
        "api.routes.crm.get_person_entity_store", lambda: fake_person_store
    )
    monkeypatch.setattr(
        "api.routes.crm.get_source_entity_store", lambda: MagicMock()
    )

    currently_running = threading.Event()
    overlap_detected = threading.Event()

    def fake_do_merge(primary_id, secondary_id, dry_run=False):
        if currently_running.is_set():
            overlap_detected.set()
        currently_running.set()
        try:
            # Long enough that a second, unserialized call would overlap
            # this one; short enough to keep the test fast.
            time.sleep(0.2)
        finally:
            currently_running.clear()
        return {}

    monkeypatch.setattr(
        "scripts.merge_people.merge_people", fake_do_merge
    )

    def do_request(client, n):
        return client.post(
            "/api/crm/people/merge",
            json={"primary_id": "primary", "secondary_ids": [f"secondary-{n}"]},
        )

    with TestClient(_router_only_app()) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_request, client, n) for n in range(2)]
            responses = [f.result(timeout=10) for f in futures]

    for response in responses:
        assert response.status_code == 200, response.text

    assert not overlap_detected.is_set(), (
        "two concurrent merge requests ran scripts.merge_people.merge_people "
        "at the same time — _mutation_lock in api/routes/crm.py is not "
        "serializing them"
    )
