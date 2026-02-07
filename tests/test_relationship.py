"""Tests for Relationship and RelationshipStore."""
import tempfile
import pytest
from datetime import datetime, timezone

from api.services.relationship import (
    Relationship,
    RelationshipStore,
    TYPE_COWORKER,
    TYPE_FRIEND,
    TYPE_INFERRED,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield f.name


@pytest.fixture
def store(temp_db):
    """Create a RelationshipStore with temp database."""
    return RelationshipStore(db_path=temp_db)


class TestRelationship:
    """Tests for Relationship dataclass."""

    def test_create_relationship(self):
        """Test basic relationship creation."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            relationship_type=TYPE_COWORKER,
            shared_events_count=5,
        )

        assert rel.person_a_id == "person1"
        assert rel.person_b_id == "person2"
        assert rel.relationship_type == TYPE_COWORKER
        assert rel.shared_events_count == 5

    def test_normalize_person_ids(self):
        """Test that person IDs are normalized (a < b)."""
        rel = Relationship(
            person_a_id="zzz",
            person_b_id="aaa",
        )

        # Should be swapped
        assert rel.person_a_id == "aaa"
        assert rel.person_b_id == "zzz"

    def test_involves(self):
        """Test involves() method."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
        )

        assert rel.involves("person1")
        assert rel.involves("person2")
        assert not rel.involves("person3")

    def test_other_person(self):
        """Test other_person() method."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
        )

        assert rel.other_person("person1") == "person2"
        assert rel.other_person("person2") == "person1"
        assert rel.other_person("person3") is None

    def test_total_shared_interactions(self):
        """Test total shared interactions property."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            shared_events_count=5,
            shared_threads_count=3,
        )

        assert rel.total_shared_interactions == 8

    def test_to_dict_from_dict(self):
        """Test serialization roundtrip."""
        now = datetime.now(timezone.utc)
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            relationship_type=TYPE_FRIEND,
            shared_contexts=["friends", "gym"],
            shared_events_count=10,
            first_seen_together=now,
        )

        data = rel.to_dict()
        restored = Relationship.from_dict(data)

        assert restored.person_a_id == rel.person_a_id
        assert restored.person_b_id == rel.person_b_id
        assert restored.relationship_type == rel.relationship_type
        assert restored.shared_contexts == rel.shared_contexts
        assert restored.shared_events_count == rel.shared_events_count


class TestRelationshipStore:
    """Tests for RelationshipStore."""

    def test_add_relationship(self, store):
        """Test adding a relationship."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            relationship_type=TYPE_COWORKER,
        )

        added = store.add(rel)
        assert added.id == rel.id

        retrieved = store.get_by_id(rel.id)
        assert retrieved is not None
        assert retrieved.relationship_type == TYPE_COWORKER

    def test_get_between(self, store):
        """Test getting relationship between two people."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
        )
        store.add(rel)

        # Should work regardless of order
        retrieved1 = store.get_between("person1", "person2")
        assert retrieved1 is not None

        retrieved2 = store.get_between("person2", "person1")
        assert retrieved2 is not None
        assert retrieved1.id == retrieved2.id

    def test_get_for_person(self, store):
        """Test getting all relationships for a person."""
        # person1 knows person2 and person3
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person2",
        ))
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person3",
        ))
        # person4 knows person5
        store.add(Relationship(
            person_a_id="person4",
            person_b_id="person5",
        ))

        rels = store.get_for_person("person1")
        assert len(rels) == 2

        rels = store.get_for_person("person2")
        assert len(rels) == 1

    def test_get_for_person_by_type(self, store):
        """Test filtering relationships by type."""
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person2",
            relationship_type=TYPE_COWORKER,
        ))
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person3",
            relationship_type=TYPE_FRIEND,
        ))

        rels = store.get_for_person("person1", relationship_type=TYPE_COWORKER)
        assert len(rels) == 1
        assert rels[0].person_b_id in ("person2", "person1")

    def test_get_connections(self, store):
        """Test getting connected person IDs."""
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person2",
        ))
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person3",
        ))

        connections = store.get_connections("person1")
        assert set(connections) == {"person2", "person3"}

    def test_increment_shared_event(self, store):
        """Test incrementing shared events."""
        # First increment creates the relationship
        rel = store.increment_shared_event("person1", "person2")
        assert rel.shared_events_count == 1

        # Second increment updates it
        rel = store.increment_shared_event("person1", "person2")
        assert rel.shared_events_count == 2

    def test_increment_shared_thread(self, store):
        """Test incrementing shared threads."""
        rel = store.increment_shared_thread("person1", "person2")
        assert rel.shared_threads_count == 1

        rel = store.increment_shared_thread("person1", "person2")
        assert rel.shared_threads_count == 2

    def test_increment_with_context(self, store):
        """Test incrementing with context."""
        rel = store.increment_shared_event(
            "person1", "person2",
            context="Work/ML/"
        )
        assert "Work/ML/" in rel.shared_contexts

        # Adding same context again shouldn't duplicate
        rel = store.increment_shared_event(
            "person1", "person2",
            context="Work/ML/"
        )
        assert rel.shared_contexts.count("Work/ML/") == 1

        # Adding different context
        rel = store.increment_shared_event(
            "person1", "person2",
            context="Personal/"
        )
        assert "Personal/" in rel.shared_contexts

    def test_add_or_update(self, store):
        """Test add_or_update merges relationships."""
        # First add
        rel1 = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            shared_contexts=["context1"],
            shared_events_count=5,
        )
        added, created = store.add_or_update(rel1)
        assert created

        # Update with new context
        rel2 = Relationship(
            person_a_id="person1",
            person_b_id="person2",
            shared_contexts=["context2"],
            shared_events_count=3,
        )
        updated, created = store.add_or_update(rel2)
        assert not created
        assert set(updated.shared_contexts) == {"context1", "context2"}

    def test_delete(self, store):
        """Test deleting a relationship."""
        rel = Relationship(
            person_a_id="person1",
            person_b_id="person2",
        )
        store.add(rel)

        success = store.delete(rel.id)
        assert success

        retrieved = store.get_by_id(rel.id)
        assert retrieved is None

    def test_delete_for_person(self, store):
        """Test deleting all relationships for a person."""
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person2",
        ))
        store.add(Relationship(
            person_a_id="person1",
            person_b_id="person3",
        ))
        store.add(Relationship(
            person_a_id="person4",
            person_b_id="person5",
        ))

        deleted = store.delete_for_person("person1")
        assert deleted == 2

        assert store.count() == 1

    def test_statistics(self, store):
        """Test getting statistics."""
        store.add(Relationship(
            person_a_id="p1",
            person_b_id="p2",
            relationship_type=TYPE_COWORKER,
            shared_events_count=5,
            shared_threads_count=3,
        ))
        store.add(Relationship(
            person_a_id="p3",
            person_b_id="p4",
            relationship_type=TYPE_FRIEND,
            shared_events_count=10,
        ))

        stats = store.get_statistics()
        assert stats["total_relationships"] == 2
        assert stats["by_type"][TYPE_COWORKER] == 1
        assert stats["by_type"][TYPE_FRIEND] == 1
        assert stats["avg_shared_interactions"] > 0
