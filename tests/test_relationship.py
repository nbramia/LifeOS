"""Tests for Relationship and RelationshipStore."""
import tempfile
import pytest
from datetime import datetime, timezone

from api.services.relationship import (
    Relationship,
    RelationshipStore,
    TYPE_COWORKER,
    TYPE_FRIEND,
)

pytestmark = pytest.mark.unit


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


class TestGetTopNeighbors:
    """Tests for RelationshipStore.get_top_neighbors()."""

    def test_orders_by_summed_shared_counts_desc(self, store):
        """Strongest neighbor (highest summed shared counts) comes first."""
        store.add(Relationship(person_a_id="center", person_b_id="weak", shared_events_count=1))
        store.add(Relationship(person_a_id="center", person_b_id="strong", shared_events_count=20))
        store.add(Relationship(person_a_id="center", person_b_id="medium", shared_messages_count=5))

        neighbors = store.get_top_neighbors("center", limit=10)

        assert [r.other_person("center") for r in neighbors] == ["strong", "medium", "weak"]

    def test_works_regardless_of_person_a_or_b_position(self, store):
        """A neighbor stored as person_a_id or person_b_id is found either way."""
        # "center" ends up as person_a_id or person_b_id depending on
        # lexicographic order - both must be found.
        store.add(Relationship(person_a_id="aaa_center", person_b_id="zzz_other", shared_events_count=3))
        store.add(Relationship(person_a_id="000_other", person_b_id="aaa_center", shared_events_count=7))

        neighbors = store.get_top_neighbors("aaa_center", limit=10)

        others = {r.other_person("aaa_center") for r in neighbors}
        assert others == {"zzz_other", "000_other"}

    def test_respects_limit(self, store):
        """Only the top `limit` neighbors are returned."""
        for i in range(5):
            store.add(Relationship(
                person_a_id="center", person_b_id=f"p{i}", shared_events_count=i + 1,
            ))

        neighbors = store.get_top_neighbors("center", limit=2)

        assert len(neighbors) == 2
        # Highest shared_events_count values are p4 (5) then p3 (4).
        assert {r.other_person("center") for r in neighbors} == {"p4", "p3"}

    def test_limit_zero_returns_empty(self, store):
        """limit=0 (or negative) returns no rows without querying."""
        store.add(Relationship(person_a_id="center", person_b_id="other", shared_events_count=1))

        assert store.get_top_neighbors("center", limit=0) == []
        assert store.get_top_neighbors("center", limit=-1) == []

    def test_no_relationships_returns_empty(self, store):
        """A person with no relationships returns an empty list."""
        assert store.get_top_neighbors("lonely", limit=10) == []

    def test_tied_scores_break_deterministically_by_id(self, store):
        """Two relationships tied on the count-sum proxy resolve to a stable
        order (by relationship id), not whatever order SQLite happens to
        return."""
        r1 = store.add(Relationship(person_a_id="center", person_b_id="tied1", shared_events_count=5))
        r2 = store.add(Relationship(person_a_id="center", person_b_id="tied2", shared_events_count=5))
        expected_order = sorted([r1.id, r2.id])

        # Repeat a few times - a non-deterministic tiebreak would be free to
        # vary across calls even against the same data.
        for _ in range(3):
            neighbors = store.get_top_neighbors("center", limit=10)
            assert [r.id for r in neighbors] == expected_order

    def test_shares_a_passed_in_connection(self, store):
        """A caller-supplied `conn` is used (and left open) instead of the
        store opening and closing its own."""
        store.add(Relationship(person_a_id="center", person_b_id="other", shared_events_count=1))

        conn = store.open_connection()
        try:
            neighbors = store.get_top_neighbors("center", limit=10, conn=conn)
            assert len(neighbors) == 1
            # Connection must still be usable - the store did not close it.
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def test_only_hydrates_up_to_limit_relationships(self, store, monkeypatch):
        """The ranking and LIMIT happen in SQL, so from_row() is called at
        most `limit` times -- not once per relationship touching the
        person, which would be an unbounded fetch on the network-graph
        endpoint's hottest path."""
        for i in range(50):
            store.add(Relationship(person_a_id="center", person_b_id=f"p{i}", shared_events_count=i))

        original_from_row = Relationship.from_row.__func__
        calls = []

        def _counting_from_row(cls, row):
            calls.append(1)
            return original_from_row(cls, row)

        monkeypatch.setattr(Relationship, "from_row", classmethod(_counting_from_row))

        neighbors = store.get_top_neighbors("center", limit=5)

        assert len(neighbors) == 5
        assert len(calls) == 5, f"expected exactly 5 Relationship.from_row() calls, got {len(calls)}"


class TestGetAllForPerson:
    """Tests for RelationshipStore.get_all_for_person()."""

    def test_returns_every_relationship_for_person_unordered(self, store):
        """All relationships touching person_id come back, regardless of
        which side of the row they're stored on."""
        store.add(Relationship(person_a_id="center", person_b_id="a", shared_events_count=1))
        store.add(Relationship(person_a_id="b", person_b_id="center", shared_events_count=2))
        store.add(Relationship(person_a_id="x", person_b_id="y", shared_events_count=99))  # unrelated

        rels = store.get_all_for_person("center")

        others = {r.other_person("center") for r in rels}
        assert others == {"a", "b"}

    def test_no_relationships_returns_empty(self, store):
        assert store.get_all_for_person("lonely") == []

    def test_shares_a_passed_in_connection(self, store):
        store.add(Relationship(person_a_id="center", person_b_id="other"))
        conn = store.open_connection()
        try:
            rels = store.get_all_for_person("center", conn=conn)
            assert len(rels) == 1
            conn.execute("SELECT 1")  # still usable
        finally:
            conn.close()


class TestGetEdgesAmong:
    """Tests for RelationshipStore.get_edges_among()."""

    def test_returns_only_edges_with_both_endpoints_in_set(self, store):
        """An edge is included only when both its endpoints are in the set."""
        store.add(Relationship(person_a_id="a", person_b_id="b"))  # both in set
        store.add(Relationship(person_a_id="a", person_b_id="outside"))  # one in set
        store.add(Relationship(person_a_id="outside1", person_b_id="outside2"))  # none in set

        edges = store.get_edges_among({"a", "b"})

        pairs = {(r.person_a_id, r.person_b_id) for r in edges}
        assert pairs == {("a", "b")}

    def test_empty_input_returns_empty(self, store):
        """An empty id set returns no edges without querying."""
        store.add(Relationship(person_a_id="a", person_b_id="b"))
        assert store.get_edges_among(set()) == []

    def test_dedupes_by_unordered_pair(self, store):
        """Each unordered pair appears at most once even though the method
        queries both person_a_id and person_b_id."""
        store.add(Relationship(person_a_id="a", person_b_id="b"))

        edges = store.get_edges_among({"a", "b"})

        assert len(edges) == 1

    def test_correct_with_more_ids_than_sqlite_variable_limit(self, store):
        """Correct results when the id set is larger than SQLite's default
        999-variable limit. The temp-table join this method uses has no
        `IN (...)` list at all, so there's no chunking to reason about --
        this just pins that a large id set still works."""
        num_people = 1200
        ids = [f"person{i}" for i in range(num_people)]

        # Chain them: person0-person1, person1-person2, ... so there are
        # num_people - 1 edges, every one of which has both endpoints in `ids`.
        for i in range(num_people - 1):
            store.add(Relationship(person_a_id=ids[i], person_b_id=ids[i + 1]))

        # Also add an edge to someone outside the set - must be excluded.
        store.add(Relationship(person_a_id=ids[0], person_b_id="outsider"))

        edges = store.get_edges_among(ids)

        assert len(edges) == num_people - 1
        pairs = {(r.person_a_id, r.person_b_id) for r in edges}
        for i in range(num_people - 1):
            a, b = sorted([ids[i], ids[i + 1]])
            assert (a, b) in pairs
        assert not any("outsider" in pair for pair in pairs)

    def test_shares_a_passed_in_connection(self, store):
        """A caller-supplied `conn` is used (and left open) instead of the
        store opening and closing its own."""
        store.add(Relationship(person_a_id="a", person_b_id="b"))

        conn = store.open_connection()
        try:
            edges = store.get_edges_among({"a", "b"}, conn=conn)
            assert len(edges) == 1
            conn.execute("SELECT 1")  # still usable
        finally:
            conn.close()

    def test_two_calls_on_same_connection_do_not_collide(self, store):
        """The temp table used internally is cleared/dropped between calls,
        so reusing one connection for consecutive calls (as the network
        endpoint does across its selection pass) doesn't leak stale rows."""
        store.add(Relationship(person_a_id="a", person_b_id="b"))
        store.add(Relationship(person_a_id="c", person_b_id="d"))

        conn = store.open_connection()
        try:
            first = store.get_edges_among({"a", "b"}, conn=conn)
            second = store.get_edges_among({"c", "d"}, conn=conn)
            assert {(r.person_a_id, r.person_b_id) for r in first} == {("a", "b")}
            assert {(r.person_a_id, r.person_b_id) for r in second} == {("c", "d")}
        finally:
            conn.close()
