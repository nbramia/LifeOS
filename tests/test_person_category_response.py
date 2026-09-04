"""
Regression test for PR #880 review finding 1 (issue #869).

`_person_to_detail_response()` must pass `None` (not `[]`) to
`compute_person_category()` when `include_related=False`.
`compute_person_category()` treats the two very differently: `None` means "no
source entities were supplied, do your own internal fetch"; `[]` means
"here are the source entities -- there are none, don't fetch any". The default
list path (`GET /api/crm/people`, `GET /birthdays/today`) calls with
`include_related=False` and must keep the old fallback-fetch behavior, or
work-vs-personal category computation silently regresses for anyone whose
category depends on source entities (e.g. Slack membership) rather than their
own email domain.
"""
from unittest.mock import patch

import pytest

from api.routes.crm import _person_to_detail_response
from api.services.person_entity import PersonEntity
from api.services.source_entity import SourceEntity

pytestmark = pytest.mark.unit


def _make_person(**overrides) -> PersonEntity:
    defaults = dict(
        id="person-category-regression",
        canonical_name="Category Regression Person",
        emails=["nobody@example.com"],
    )
    defaults.update(overrides)
    return PersonEntity(**defaults)


class TestPersonToDetailResponseCategorySemantics:
    def test_include_related_false_passes_none_not_empty_list(self):
        """The fast/default path must not skip compute_person_category()'s
        internal source-entity fallback fetch."""
        person = _make_person()
        with patch("api.routes.crm.compute_person_category", return_value="personal") as mock_compute, \
                patch("api.routes.crm.get_source_entity_store") as mock_get_source_store:
            _person_to_detail_response(person, include_related=False)

            mock_compute.assert_called_once()
            call_args = mock_compute.call_args.args
            assert call_args[0] is person
            assert call_args[1] is None, (
                "include_related=False must pass None to compute_person_category, "
                "not [] -- [] disables its internal source-entity fallback fetch"
            )
            mock_get_source_store.assert_not_called()

    def test_include_related_true_passes_the_fetched_source_entities(self):
        """The detail path (include_related=True) still fetches and forwards
        the real source entities, unchanged."""
        person = _make_person()
        fetched_entities = [
            SourceEntity(id="source-1", source_type="gmail", canonical_person_id=person.id),
            SourceEntity(id="source-2", source_type="calendar", canonical_person_id=person.id),
        ]
        with patch("api.routes.crm.compute_person_category", return_value="personal") as mock_compute, \
                patch("api.routes.crm.get_source_entity_store") as mock_get_source_store, \
                patch("api.routes.crm.get_relationship_store") as mock_get_rel_store, \
                patch("api.routes.crm.get_person_entity_store"):
            mock_get_source_store.return_value.get_for_person.return_value = fetched_entities
            mock_get_rel_store.return_value.get_for_person.return_value = []

            response = _person_to_detail_response(person, include_related=True)

            mock_compute.assert_called_once()
            call_args = mock_compute.call_args.args
            assert call_args[1] is fetched_entities
            # The real (non-mocked) response conversion ran over the fetched
            # entities, confirming the response actually used what
            # compute_person_category saw.
            assert [e.id for e in response.source_entities] == ["source-1", "source-2"]

    def test_none_and_empty_list_give_different_categories_when_it_matters(self):
        """End-to-end sanity check with the real compute_person_category(): a
        person who only qualifies as "work" via a Slack source entity (not
        their own email domain) must still be categorized "work" on the
        default (include_related=False) path, exactly like the
        include_related=True path."""
        person = _make_person(emails=["nobody@personal-domain.example"])
        slack_source = SourceEntity(
            id="source-slack-1",
            source_type="slack",
            source_id="slack-msg-1",
            canonical_person_id=person.id,
        )

        with patch("api.routes.crm.get_source_entity_store") as mock_crm_source_store, \
                patch("api.services.source_entity.get_source_entity_store") as mock_internal_source_store, \
                patch("api.routes.crm.get_relationship_store") as mock_get_rel_store, \
                patch("api.routes.crm.get_person_entity_store"):
            mock_crm_source_store.return_value.get_for_person.return_value = [slack_source]
            mock_internal_source_store.return_value.get_for_person.return_value = [slack_source]
            mock_get_rel_store.return_value.get_for_person.return_value = []

            # include_related=True: the real fetched list (with the slack
            # source) drives categorization directly.
            detail_response = _person_to_detail_response(person, include_related=True)
            assert detail_response.category == "work"

            # include_related=False: compute_person_category() must fall back
            # to its own internal fetch (also stubbed to return the slack
            # source above), landing on the same category as the path above.
            list_response = _person_to_detail_response(person, include_related=False)
            assert list_response.category == "work"
