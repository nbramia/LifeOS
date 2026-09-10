"""
Tests for pair strength migration.

These tests validate that the migration from edge_weight to pair_strength
maintains reasonable behavior and doesn't break the system.

Run with: pytest tests/test_pair_strength_migration.py -v
"""
import sqlite3
from pathlib import Path

import pytest
from datetime import datetime, timedelta, timezone

# Mark all tests as unit tests
pytestmark = pytest.mark.unit


@pytest.fixture
def worker_relationship_db(tmp_path_factory):
    """Return a unique synthetic CRM path for this test invocation.

    xdist workers get separate basetemp trees, and each test gets a fresh
    child, so relationship stores cannot accidentally share a process-wide
    singleton or a real workspace database.
    """
    return tmp_path_factory.mktemp("pair-strength-crm") / "crm.db"


@pytest.fixture(autouse=True)
def isolate_relationship_and_person_stores(monkeypatch, worker_relationship_db):
    """Point both singleton stores at an isolated synthetic database."""
    import api.services.person_entity as person_entity
    from api.services.person_entity import PersonEntity, get_person_entity_store
    from api.services.relationship import Relationship, get_relationship_store, reset_relationship_store
    from config.settings import settings

    monkeypatch.setattr(
        "api.services.relationship.get_crm_db_path",
        lambda: str(worker_relationship_db),
    )
    monkeypatch.setattr(
        person_entity.PersonEntityStore,
        "CRM_DB_PATH",
        worker_relationship_db,
    )

    def reset_person_store():
        old_store = person_entity._entity_store
        if old_store is not None and old_store._data_version_conn is not None:
            old_store._data_version_conn.close()
        person_entity._entity_store = None

    reset_relationship_store()
    reset_person_store()
    monkeypatch.setattr(settings, "my_person_id", "synthetic-owner", raising=False)

    person_store = get_person_entity_store()
    for person, strength in (
        (PersonEntity(id="synthetic-owner", canonical_name="Synthetic Owner"), 0),
        (PersonEntity(id="synthetic-contact", canonical_name="Synthetic Contact"), 83),
        (PersonEntity(id="synthetic-peer", canonical_name="Synthetic Peer"), 37),
    ):
        person.relationship_strength = strength
        person_store.add(person)

    now = datetime.now(timezone.utc)
    relationship_store = get_relationship_store()
    relationship_store.add(Relationship(
        id="synthetic-owner-contact",
        person_a_id="synthetic-owner",
        person_b_id="synthetic-contact",
        shared_events_count=4,
        last_seen_together=now,
    ))
    relationship_store.add(Relationship(
        id="synthetic-contact-peer",
        person_a_id="synthetic-contact",
        person_b_id="synthetic-peer",
        shared_threads_count=3,
        last_seen_together=now,
    ))
    yield
    reset_relationship_store()
    reset_person_store()


def test_relationship_store_uses_unique_populated_synthetic_db(worker_relationship_db):
    """The fixture creates the schema in its own path, not cwd/data/crm.db."""
    from api.services.relationship import get_relationship_store

    store = get_relationship_store()
    assert Path(store.db_path) == worker_relationship_db
    with sqlite3.connect(worker_relationship_db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "relationships" in tables


def test_existing_synthetic_cwd_db_is_not_modified(
    tmp_path, worker_relationship_db, monkeypatch,
):
    """An existing synthetic cwd/data/crm.db remains an untouched sentinel."""
    cwd_db = tmp_path / "data" / "crm.db"
    cwd_db.parent.mkdir()
    cwd_db.write_bytes(b"synthetic-cwd-sentinel")
    monkeypatch.chdir(tmp_path)

    from api.services.relationship import get_relationship_store

    get_relationship_store().get_all_relationships()
    assert cwd_db.read_bytes() == b"synthetic-cwd-sentinel"
    assert worker_relationship_db.exists()


class TestPairStrengthBaseline:
    """Baseline tests capturing current edge_weight behavior."""

    def test_relationship_store_loads(self):
        """Relationship store can be queried without error."""
        from api.services.relationship import get_relationship_store
        store = get_relationship_store()
        all_rels = store.get_all_relationships()
        # The store must return a list-like result (open-source default: empty).
        # Populated installs are exercised by the integration tests below.
        assert isinstance(all_rels, list)

    def test_edge_weight_property_exists(self):
        """Relationship has edge_weight property."""
        from api.services.relationship import Relationship
        rel = Relationship(
            id="test",
            person_a_id="a",
            person_b_id="b",
            shared_events_count=10,
            shared_threads_count=5,
        )
        assert hasattr(rel, 'edge_weight')
        assert isinstance(rel.edge_weight, int)
        assert 0 <= rel.edge_weight <= 100

    def test_edge_weight_increases_with_interactions(self):
        """Edge weight should increase with more interactions."""
        from api.services.relationship import Relationship

        low = Relationship(id="low", person_a_id="a", person_b_id="b", shared_events_count=1)
        med = Relationship(id="med", person_a_id="a", person_b_id="b", shared_events_count=50)
        high = Relationship(id="high", person_a_id="a", person_b_id="b", shared_events_count=500)

        assert low.edge_weight < med.edge_weight < high.edge_weight


class TestPairStrengthRequirements:
    """Tests for the new pair_strength property."""

    def test_pair_strength_property_exists(self):
        """Relationship should have pair_strength property after migration."""
        from api.services.relationship import Relationship
        rel = Relationship(
            id="test",
            person_a_id="a",
            person_b_id="b",
            shared_events_count=10,
            last_seen_together=datetime.now(timezone.utc),
        )
        # This will fail until we implement pair_strength
        assert hasattr(rel, 'pair_strength'), "Relationship should have pair_strength property"

    def test_pair_strength_range(self):
        """Pair strength should be 0-100."""
        from api.services.relationship import Relationship
        rel = Relationship(
            id="test",
            person_a_id="a",
            person_b_id="b",
            shared_events_count=10,
            last_seen_together=datetime.now(timezone.utc),
        )
        strength = rel.pair_strength
        assert 0 <= strength <= 100, f"pair_strength should be 0-100, got {strength}"

    def test_pair_strength_recency_decay(self):
        """Pair strength should decay with recency."""
        from api.services.relationship import Relationship

        now = datetime.now(timezone.utc)
        recent = Relationship(
            id="recent", person_a_id="a", person_b_id="b",
            shared_events_count=50,
            last_seen_together=now - timedelta(days=7),
        )
        old = Relationship(
            id="old", person_a_id="a", person_b_id="b",
            shared_events_count=50,  # Same interaction count
            last_seen_together=now - timedelta(days=300),
        )

        assert recent.pair_strength > old.pair_strength, \
            "Recent relationship should have higher pair_strength"

    def test_pair_strength_frequency_scaling(self):
        """Pair strength should increase with frequency."""
        from api.services.relationship import Relationship

        now = datetime.now(timezone.utc)
        low = Relationship(
            id="low", person_a_id="a", person_b_id="b",
            shared_events_count=5,
            last_seen_together=now,
        )
        high = Relationship(
            id="high", person_a_id="a", person_b_id="b",
            shared_events_count=100,
            last_seen_together=now,  # Same recency
        )

        assert high.pair_strength > low.pair_strength, \
            "Higher interaction count should give higher pair_strength"

    def test_pair_strength_diversity_bonus(self):
        """Pair strength should get bonus for diverse interaction types."""
        from api.services.relationship import Relationship

        now = datetime.now(timezone.utc)
        # Single source type
        single = Relationship(
            id="single", person_a_id="a", person_b_id="b",
            shared_events_count=50,
            shared_threads_count=0,
            shared_messages_count=0,
            last_seen_together=now,
        )
        # Multiple source types with same total interactions
        diverse = Relationship(
            id="diverse", person_a_id="a", person_b_id="b",
            shared_events_count=25,
            shared_threads_count=15,
            shared_messages_count=10,
            last_seen_together=now,
        )

        assert diverse.pair_strength > single.pair_strength, \
            "Diverse sources should give higher pair_strength"

    def test_pair_strength_non_owner_reasonable_values(self):
        """Non-owner edges with low interactions should still have reasonable strength."""
        from api.services.relationship import Relationship

        now = datetime.now(timezone.utc)
        # Typical non-owner edge: few interactions but recent
        rel = Relationship(
            id="test", person_a_id="a", person_b_id="b",
            shared_events_count=3,  # Low, typical of non-owner
            last_seen_together=now - timedelta(days=30),
        )

        # Should not be abysmal - at least 20% for a recent relationship
        assert rel.pair_strength >= 20, \
            f"Recent non-owner edge should have reasonable strength, got {rel.pair_strength}"


class TestNetworkGraphIntegration:
    """Tests for network graph API using pair_strength."""

    def test_network_graph_returns_edges_with_weight(self):
        """Network graph edges should have weight field."""
        from api.services.relationship import get_relationship_store
        from api.services.person_entity import get_person_entity_store
        from config.settings import settings

        person_store = get_person_entity_store()
        # Use settings.my_person_id to get the correct owner ID
        # (get_by_name may return wrong ID if there are duplicates)
        owner = person_store.get_by_id(settings.my_person_id)
        assert owner is not None, "Synthetic owner fixture must be populated"

        rel_store = get_relationship_store()
        rels = rel_store.get_for_person(owner.id)
        assert rels, "Synthetic owner fixture must have relationships"

        # All should have edge_weight (or pair_strength after migration)
        for rel in rels[:10]:
            weight = getattr(rel, 'pair_strength', None) or rel.edge_weight
            assert 0 <= weight <= 100, f"Weight should be 0-100, got {weight}"


class TestEdgeWeightSourceLogic:
    """Tests for edge weight source selection (relationship_strength vs pair_strength)."""

    def test_owner_edge_uses_relationship_strength(self):
        """Edges involving the owner should use the other person's relationship_strength."""
        from api.services.relationship import get_relationship_store
        from api.services.person_entity import get_person_entity_store
        from config.settings import settings

        person_store = get_person_entity_store()
        rel_store = get_relationship_store()
        owner_id = settings.my_person_id

        test_person = person_store.get_by_id("synthetic-contact")
        assert test_person is not None

        # For owner edges, weight should match relationship_strength (not
        # pair_strength), using the production helper that renders graph data.
        rel = rel_store.get_between(owner_id, test_person.id)
        expected_weight = int(test_person.relationship_strength)
        from api.routes.crm import _rendered_edge_weight

        people_by_id = {person.id: person for person in person_store.get_all()}
        rendered_weight = _rendered_edge_weight(
            rel, owner_id, test_person.id, owner_id, people_by_id,
        )
        assert rendered_weight == expected_weight
        assert rendered_weight != rel.pair_strength

    def test_non_owner_edge_uses_pair_strength(self):
        """Edges not involving the owner should use pair_strength."""
        from api.services.relationship import get_relationship_store
        from config.settings import settings

        rel_store = get_relationship_store()
        owner_id = settings.my_person_id

        non_owner_rel = rel_store.get_between("synthetic-contact", "synthetic-peer")
        assert non_owner_rel is not None

        # For non-owner edges, the production renderer returns pair_strength.
        from api.routes.crm import _rendered_edge_weight
        from api.services.person_entity import get_person_entity_store

        person_store = get_person_entity_store()
        people_by_id = {person.id: person for person in person_store.get_all()}
        rendered_weight = _rendered_edge_weight(
            non_owner_rel,
            non_owner_rel.person_a_id,
            non_owner_rel.person_b_id,
            owner_id,
            people_by_id,
        )
        assert rendered_weight == non_owner_rel.pair_strength
        assert 0 <= rendered_weight <= 100
