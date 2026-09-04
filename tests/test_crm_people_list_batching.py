"""
GET /api/crm/people (and GET /birthdays/today) must batch the per-page
source-entity fetch that compute_person_category()'s include_related=False
fallback needs, via SourceEntityStore.get_for_people_batch(), instead of
calling SourceEntityStore.get_for_person() once per person on the page.

Fetching source entities per person (even through
PersonEntityStore.get_all()'s hydration cache) would make list_people()'s
warm latency scale with the number of returned people, defeating the point
of caching get_all() -- a strength-sorted CRM page is dominated by the
highest-interaction people, many of whom have very large source-entity
histories.
"""
from unittest.mock import MagicMock, patch

import pytest

from api.routes.crm import list_people, get_todays_birthdays
from api.services.person_entity import PersonEntity

pytestmark = pytest.mark.unit


def _make_people(count: int) -> list[PersonEntity]:
    people = [
        PersonEntity(
            id=f"person-{i:03d}",
            canonical_name=f"Person {i:03d}",
            emails=[f"person{i:03d}@personal-domain.example"],
        )
        for i in range(count)
    ]
    for i, p in enumerate(people):
        p.relationship_strength = float(count - i)
    return people


class _SpySourceEntityStore:
    """Stands in for SourceEntityStore, recording how it was called."""

    def __init__(self):
        self.get_for_people_batch_calls: list[list[str]] = []
        self.get_for_person_calls: list[str] = []

    def get_for_people_batch(self, canonical_person_ids, limit_per_person=500):
        self.get_for_people_batch_calls.append(list(canonical_person_ids))
        return {}

    def get_for_person(self, canonical_person_id, source_type=None, limit=None):
        self.get_for_person_calls.append(canonical_person_id)
        return []


class TestListPeopleBatchesSourceEntityFetch:
    def test_issues_one_batch_call_not_one_per_person(self):
        people = _make_people(50)
        person_store = MagicMock()
        person_store.get_all.return_value = people
        spy_source_store = _SpySourceEntityStore()

        with patch("api.routes.crm.get_person_entity_store", return_value=person_store), \
                patch("api.routes.crm.get_source_entity_store", return_value=spy_source_store):
            result = list_people(
                q=None, category=None, source=None, dunbar_circles=None, tags=None,
                has_interactions=None, min_interactions=0, sort="strength",
                offset=0, limit=50,
            )

        assert len(result.people) == 50
        assert len(spy_source_store.get_for_people_batch_calls) == 1
        assert set(spy_source_store.get_for_people_batch_calls[0]) == {p.id for p in people}
        assert spy_source_store.get_for_person_calls == []

    def test_batch_call_only_covers_the_current_page(self):
        """Pagination (offset/limit) narrows the batch to the returned page,
        not the full result set."""
        people = _make_people(120)
        person_store = MagicMock()
        person_store.get_all.return_value = people
        spy_source_store = _SpySourceEntityStore()

        with patch("api.routes.crm.get_person_entity_store", return_value=person_store), \
                patch("api.routes.crm.get_source_entity_store", return_value=spy_source_store):
            result = list_people(
                q=None, category=None, source=None, dunbar_circles=None, tags=None,
                has_interactions=None, min_interactions=0, sort="strength",
                offset=0, limit=10,
            )

        assert len(result.people) == 10
        assert len(spy_source_store.get_for_people_batch_calls) == 1
        assert len(spy_source_store.get_for_people_batch_calls[0]) == 10


class TestBirthdaysTodayBatchesSourceEntityFetch:
    def test_issues_one_batch_call_not_one_per_person(self):
        people = _make_people(5)
        from datetime import datetime
        today_mm_dd = datetime.now().strftime("%m-%d")
        for p in people:
            p.birthday = today_mm_dd

        person_store = MagicMock()
        person_store.get_all.return_value = people
        spy_source_store = _SpySourceEntityStore()

        with patch("api.routes.crm.get_person_entity_store", return_value=person_store), \
                patch("api.routes.crm.get_source_entity_store", return_value=spy_source_store):
            result = get_todays_birthdays()

        assert result["count"] == 5
        assert len(spy_source_store.get_for_people_batch_calls) == 1
        assert set(spy_source_store.get_for_people_batch_calls[0]) == {p.id for p in people}
        assert spy_source_store.get_for_person_calls == []
