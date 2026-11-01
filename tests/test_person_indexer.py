"""Tests for how relationship strength shapes the people index."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.services import person_indexer
from api.services.person_entity import PersonEntity
from api.services.person_indexer import generate_person_document, sync_people_to_vectorstore

pytestmark = pytest.mark.unit


def _summary(strength: float, interactions: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        relationship_strength=strength,
        total_interactions_90d=interactions,
        channels=[],
        primary_channel=None,
        contact_on_record=False,
        days_since_contact=0,
        has_facts=False,
    )


class TestStrengthWording:
    @pytest.mark.parametrize(
        ("strength", "wording"),
        [
            (100, "Very strong relationship"),
            (65, "Very strong relationship"),
            (64.9, "Strong relationship"),
            (40, "Strong relationship"),
            (39.9, "Moderate relationship"),
            (18, "Moderate relationship"),
            (17.9, "Light relationship"),
            (11, "Light relationship"),
            (10.9, "Distant relationship"),
            (0, "Distant relationship"),
        ],
    )
    def test_bands(self, strength, wording):
        person = PersonEntity(id="p1", canonical_name="Robin Example")
        doc = generate_person_document(person, _summary(strength))
        assert f"{wording} (strength: {strength}/100)" in doc


class TestEligibility:
    @pytest.fixture
    def run_sync(self, monkeypatch):
        def run(strength, interactions):
            person = PersonEntity(id="p1", canonical_name="Robin Example")
            index = MagicMock(return_value=True)
            monkeypatch.setattr(person_indexer, "VectorStore", MagicMock())
            monkeypatch.setattr(
                person_indexer, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: [person])
            )
            monkeypatch.setattr(
                person_indexer, "get_relationship_summary", lambda person_id: _summary(strength, interactions)
            )
            monkeypatch.setattr(person_indexer, "index_person_to_vectorstore", index)
            sync_people_to_vectorstore()
            return index.called

        return run

    def test_at_the_strength_cutoff(self, run_sync):
        assert run_sync(9, 0) is True

    def test_below_the_strength_cutoff(self, run_sync):
        assert run_sync(8.9, 2) is False

    def test_recent_activity_qualifies_on_its_own(self, run_sync):
        assert run_sync(0, 3) is True
