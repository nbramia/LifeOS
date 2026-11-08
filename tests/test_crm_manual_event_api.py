"""API tests for POST /api/crm/people/{id}/interactions (manual events).

Runs the real route and a real InteractionStore against a temporary database;
only the person store is faked, so nothing touches production CRM data.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import crm
from api.services.interaction_store import InteractionStore

pytestmark = pytest.mark.unit

START = "2026-11-07T11:00:00-06:00"


_PEOPLE = {"ana": "Ana Example", "ben": "Ben Example"}
_MERGED = {"old-ana": "ana"}


class _FakePersonStore:
    def get_canonical_id(self, person_id):
        return _MERGED.get(person_id, person_id)

    def get_by_id(self, person_id):
        name = _PEOPLE.get(person_id)
        return SimpleNamespace(id=person_id, canonical_name=name) if name else None


@pytest.fixture
def store(tmp_path, monkeypatch):
    interaction_store = InteractionStore(str(tmp_path / "interactions.db"), strict=False)
    person_store = _FakePersonStore()
    monkeypatch.setattr(crm, "get_person_entity_store", lambda: person_store)
    monkeypatch.setattr(crm, "get_interaction_store", lambda: interaction_store)
    monkeypatch.setattr("api.services.person_entity.get_person_entity_store", lambda: person_store)
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda ids: None)
    return interaction_store


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(crm.router)
    return TestClient(app)


def _post(client, person_id="ana", **overrides):
    return client.post(
        f"/api/crm/people/{person_id}/interactions",
        json={"title": "Coffee with Ana!", "timestamp": START, **overrides},
    )


def _row(store, person_id, slug="coffee_with_ana", date="2026-11-07"):
    return store.get_by_source("calendar", f"manual_{slug}_{date}:{person_id}")


def test_minimal_event_follows_manual_convention(client, store):
    response = _post(client)

    assert response.status_code == 201
    assert response.json() == {"created": 1, "skipped": 0, "person_ids": ["ana"]}
    row = _row(store, "ana")
    assert row.person_id == "ana"
    assert row.source_type == "calendar"
    assert row.source_account == "manual"
    assert row.title == "Coffee with Ana!"
    assert row.timestamp.isoformat() == START
    assert row.snippet is None
    assert row.attendee_count == 1


def test_all_fields_land_in_the_row(client, store):
    response = _post(
        client,
        end_time="2026-11-07T12:30:00-06:00",
        location="Corner Cafe",
        notes="Talked about the move",
        source_account="partiful",
    )

    assert response.status_code == 201
    row = _row(store, "ana")
    assert row.source_account == "partiful"
    assert row.snippet == "Talked about the move · Corner Cafe · Until 12:30 PM"


def test_end_time_on_a_later_day_names_the_day(client, store):
    _post(client, end_time="2026-11-08T01:00:00-06:00")
    assert _row(store, "ana").snippet == "Until Nov 8, 1:00 AM"


def test_source_id_date_is_the_events_local_date(client, store):
    # 23:30 in Chicago is already the next day in UTC.
    _post(client, timestamp="2026-11-07T23:30:00-06:00")
    assert _row(store, "ana", date="2026-11-07") is not None


def test_each_crm_attendee_gets_a_row_naming_the_others(client, store):
    response = _post(client, attendee_person_ids=["ben"], attendee_names=["Cal Outsider"])

    assert response.json()["created"] == 2
    ana, ben = _row(store, "ana"), _row(store, "ben")
    assert ana.snippet == "With Ben Example, Cal Outsider"
    assert ben.snippet == "With Ana Example, Cal Outsider"
    assert ana.attendee_count == ben.attendee_count == 3


def test_merged_attendee_id_is_not_counted_twice(client, store):
    response = _post(client, attendee_person_ids=["old-ana"])

    assert response.json() == {"created": 1, "skipped": 0, "person_ids": ["ana"]}
    assert _row(store, "ana").attendee_count == 1


def test_resubmitting_is_a_conflict_and_writes_nothing(client, store):
    assert _post(client).status_code == 201
    assert _post(client).status_code == 409
    assert len(store.get_for_person("ana", days_back=100000, limit=10)) == 1


def test_adding_an_attendee_on_resubmit_only_creates_their_row(client, store):
    _post(client)
    response = _post(client, attendee_person_ids=["ben"])

    assert response.status_code == 201
    assert response.json()["created"] == 1
    assert response.json()["skipped"] == 1


def test_timeline_returns_the_recorded_event(client):
    _post(client, timestamp=(datetime.now(UTC) - timedelta(days=1)).isoformat(), location="Corner Cafe")

    items = client.get("/api/crm/people/ana/timeline").json()["items"]

    assert [item["title"] for item in items] == ["Coffee with Ana!"]
    assert items[0]["snippet"] == "Corner Cafe"
    assert items[0]["source_type"] == "calendar"


def test_stats_are_refreshed_for_everyone_involved(client, monkeypatch):
    refreshed = []
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", refreshed.extend)

    _post(client, attendee_person_ids=["ben"])

    assert refreshed == ["ana", "ben"]


def test_stats_failure_does_not_fail_a_committed_save(client, store, monkeypatch):
    def fail(ids):
        raise RuntimeError("stats unavailable")
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", fail)

    assert _post(client).status_code == 201
    assert _row(store, "ana") is not None


@pytest.mark.parametrize(
    "person_id,overrides,status,message",
    [
        ("nobody", {}, 404, "not found"),
        ("ana", {"attendee_person_ids": ["nobody"]}, 422, "not found"),
        ("ana", {"title": "   "}, 422, "Title is required"),
        ("ana", {"end_time": START}, 422, "End time"),
        ("ana", {"timestamp": "1999-01-01T00:00:00-06:00"}, 422, "too old"),
        ("ana", {"timestamp": "2026-11-07T11:00:00"}, 422, None),  # No UTC offset
    ],
)
def test_invalid_requests_are_rejected_without_writing(client, store, person_id, overrides, status, message):
    response = _post(client, person_id=person_id, **overrides)

    assert response.status_code == status
    if message:
        assert message in response.json()["detail"]
    assert store.get_for_person("ana", days_back=100000, limit=10) == []


def test_unknown_attendee_blocks_the_whole_event(client, store):
    _post(client, attendee_person_ids=["ben", "nobody"])
    assert _row(store, "ana") is None
    assert _row(store, "ben") is None
