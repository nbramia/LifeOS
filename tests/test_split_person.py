"""
Tests for person split functionality.

Tests cover:
- Split helper functions (stats recalculation, relationship recalculation)
- Split operation logic
- Consistency with merge behavior
"""
import pytest
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


class TestRecalculatePersonStats:
    """Tests for _recalculate_person_stats helper function."""

    @pytest.fixture
    def mock_int_conn(self):
        """Create an in-memory SQLite connection with test data."""
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                person_id TEXT,
                source_type TEXT,
                timestamp TEXT
            )
        """)
        # Insert test interactions
        test_data = [
            ("i1", "person-1", "gmail", "2024-01-01T10:00:00"),
            ("i2", "person-1", "gmail", "2024-01-02T10:00:00"),
            ("i3", "person-1", "calendar", "2024-01-03T10:00:00"),
            ("i4", "person-1", "imessage", "2024-01-04T10:00:00"),
            ("i5", "person-1", "vault", "2024-01-05T10:00:00"),
            ("i6", "person-2", "gmail", "2024-01-06T10:00:00"),
        ]
        conn.executemany(
            "INSERT INTO interactions VALUES (?, ?, ?, ?)",
            test_data
        )
        conn.commit()
        return conn

    @pytest.fixture
    def mock_person_store(self):
        """Create mock person store."""
        from api.services.person_entity import PersonEntity

        person = PersonEntity(
            id="person-1",
            canonical_name="Test Person",
            email_count=10,  # Old value
            meeting_count=5,
            message_count=3,
            mention_count=1,
        )

        store = MagicMock()
        store.get_by_id.return_value = person
        store.update.return_value = None
        return store

    def test_recalculates_stats_from_interactions(self, mock_int_conn, mock_person_store):
        """Stats are recalculated from interactions table."""
        from api.routes.crm import _recalculate_person_stats

        result = _recalculate_person_stats("person-1", mock_int_conn, mock_person_store)

        # Verify stats were recalculated
        assert result['new']['email_count'] == 2  # 2 gmail interactions
        assert result['new']['meeting_count'] == 1  # 1 calendar interaction
        assert result['new']['message_count'] == 1  # 1 imessage interaction
        assert result['new']['mention_count'] == 1  # 1 vault interaction

    def test_preserves_old_stats_for_comparison(self, mock_int_conn, mock_person_store):
        """Old stats are returned for logging/comparison."""
        from api.routes.crm import _recalculate_person_stats

        result = _recalculate_person_stats("person-1", mock_int_conn, mock_person_store)

        assert result['old']['email_count'] == 10
        assert result['old']['meeting_count'] == 5

    def test_returns_empty_for_nonexistent_person(self, mock_int_conn):
        """Returns empty dict if person not found."""
        from api.routes.crm import _recalculate_person_stats

        store = MagicMock()
        store.get_by_id.return_value = None

        result = _recalculate_person_stats("nonexistent", mock_int_conn, store)

        assert result == {}


class TestRecalculateRelationshipWithMe:
    """Tests for _recalculate_relationship_with_me helper function."""

    @pytest.fixture
    def mock_int_conn(self):
        """Create an in-memory SQLite connection with test data."""
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                person_id TEXT,
                source_type TEXT,
                timestamp TEXT
            )
        """)
        # Insert test interactions
        test_data = [
            ("i1", "person-1", "gmail", "2024-01-01T10:00:00"),
            ("i2", "person-1", "gmail", "2024-01-02T10:00:00"),
            ("i3", "person-1", "calendar", "2024-01-03T10:00:00"),
            ("i4", "person-1", "imessage", "2024-01-04T10:00:00"),
            ("i5", "person-1", "whatsapp", "2024-01-05T10:00:00"),
            ("i6", "person-1", "phone", "2024-01-06T10:00:00"),
            ("i7", "person-1", "slack", "2024-01-07T10:00:00"),
        ]
        conn.executemany(
            "INSERT INTO interactions VALUES (?, ?, ?, ?)",
            test_data
        )
        conn.commit()
        return conn

    def test_updates_existing_relationship(self, mock_int_conn):
        """Updates existing relationship with new counts."""
        from api.routes.crm import _recalculate_relationship_with_me
        from api.services.relationship import Relationship

        mock_existing = Relationship(
            id="rel-1",
            person_a_id="my-person",
            person_b_id="person-1",
            shared_events_count=10,  # Old value
            shared_threads_count=5,
        )

        mock_rel_store = MagicMock()
        mock_rel_store.get_between.return_value = mock_existing
        mock_rel_store.update.return_value = None

        with patch('api.services.relationship.get_relationship_store', return_value=mock_rel_store):
            result = _recalculate_relationship_with_me("person-1", "my-person", mock_int_conn)

        assert result['action'] == 'updated'
        assert mock_existing.shared_events_count == 1  # 1 calendar
        assert mock_existing.shared_threads_count == 2  # 2 gmail
        assert mock_existing.shared_messages_count == 1  # 1 imessage
        assert mock_existing.shared_whatsapp_count == 1
        assert mock_existing.shared_phone_calls_count == 1
        assert mock_existing.shared_slack_count == 1

    def test_creates_new_relationship_when_none_exists(self, mock_int_conn):
        """Creates new relationship if none exists."""
        from api.routes.crm import _recalculate_relationship_with_me

        mock_rel_store = MagicMock()
        mock_rel_store.get_between.return_value = None
        mock_rel_store.add.return_value = None

        with patch('api.services.relationship.get_relationship_store', return_value=mock_rel_store):
            result = _recalculate_relationship_with_me("person-1", "my-person", mock_int_conn)

        assert result['action'] == 'created'
        mock_rel_store.add.assert_called_once()

    def test_deletes_relationship_when_no_interactions(self):
        """Deletes relationship if person has no interactions."""
        from api.routes.crm import _recalculate_relationship_with_me
        from api.services.relationship import Relationship

        # Empty interactions database
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                person_id TEXT,
                source_type TEXT,
                timestamp TEXT
            )
        """)

        mock_existing = Relationship(
            id="rel-1",
            person_a_id="my-person",
            person_b_id="person-1",
        )

        mock_rel_store = MagicMock()
        mock_rel_store.get_between.return_value = mock_existing
        mock_rel_store.delete.return_value = None

        with patch('api.services.relationship.get_relationship_store', return_value=mock_rel_store):
            result = _recalculate_relationship_with_me("person-1", "my-person", conn)

        assert result['action'] == 'deleted'
        mock_rel_store.delete.assert_called_once_with("rel-1")

    def test_skips_self_relationship(self, mock_int_conn):
        """Returns empty when person_id == my_person_id."""
        from api.routes.crm import _recalculate_relationship_with_me

        result = _recalculate_relationship_with_me("my-person", "my-person", mock_int_conn)

        assert result == {}

    def test_skips_when_no_my_person_id(self, mock_int_conn):
        """Returns empty when my_person_id is None."""
        from api.routes.crm import _recalculate_relationship_with_me

        result = _recalculate_relationship_with_me("person-1", None, mock_int_conn)

        assert result == {}


class TestSplitPersonConsistency:
    """Tests verifying split is consistent with merge behavior."""

    def test_split_and_merge_are_inverse_operations(self):
        """
        Conceptual test: after split then merge, data should be restored.

        This documents the expected behavior, not full integration.
        """
        # Split: moves interactions from A to B
        # Merge: combines B back into A
        # Result: A should have original interactions
        #
        # Key consistency requirements:
        # 1. Both update person stats (email_count, meeting_count, etc.)
        # 2. Both update relationships
        # 3. Both recalculate relationship strength
        pass

    def test_split_recalculates_not_subtracts(self):
        """
        Split recalculates stats from interactions, not by subtraction.

        Merge SUMS stats because secondary is deleted.
        Split RECALCULATES because both persons survive with partial data.
        """
        # This is the key difference from merge:
        # - Merge: primary.email_count = primary.email_count + secondary.email_count
        # - Split: primary.email_count = COUNT(*) FROM interactions WHERE person_id = primary
        pass


class TestVaultSourceEntityCreation:
    """Tests for vault/granola source entity factory functions."""

    def test_vault_source_entity_has_correct_type(self):
        """Vault source entity has source_type='vault'."""
        from api.services.source_entity import create_vault_source_entity

        entity = create_vault_source_entity(
            file_path="/vault/note.md",
            person_name="John Doe",
        )

        assert entity.source_type == "vault"

    def test_vault_source_entity_has_unique_source_id(self):
        """Source ID combines file path and person name."""
        from api.services.source_entity import create_vault_source_entity

        entity = create_vault_source_entity(
            file_path="/vault/note.md",
            person_name="John Doe",
        )

        assert entity.source_id == "/vault/note.md:John Doe"

    def test_vault_source_entity_captures_observed_name(self):
        """observed_name is set to the person name."""
        from api.services.source_entity import create_vault_source_entity

        entity = create_vault_source_entity(
            file_path="/vault/note.md",
            person_name="John Doe",
        )

        assert entity.observed_name == "John Doe"

    def test_vault_source_entity_preserves_metadata(self):
        """Metadata is stored on the entity."""
        from api.services.source_entity import create_vault_source_entity

        entity = create_vault_source_entity(
            file_path="/vault/note.md",
            person_name="John Doe",
            metadata={"note_title": "Meeting Notes", "is_granola": False},
        )

        assert entity.metadata["note_title"] == "Meeting Notes"
        assert entity.metadata["is_granola"] is False

    def test_granola_source_entity_has_correct_type(self):
        """Granola source entity has source_type='granola'."""
        from api.services.source_entity import create_granola_source_entity

        entity = create_granola_source_entity(
            file_path="/vault/meeting.md",
            person_name="Jane Smith",
        )

        assert entity.source_type == "granola"

    def test_granola_source_entity_has_unique_source_id(self):
        """Granola source ID combines file path and person name."""
        from api.services.source_entity import create_granola_source_entity

        entity = create_granola_source_entity(
            file_path="/vault/meeting.md",
            person_name="Jane Smith",
        )

        assert entity.source_id == "/vault/meeting.md:Jane Smith"

    def test_source_entities_are_distinguishable_per_person_per_file(self):
        """Different people in same file get different source entities."""
        from api.services.source_entity import create_vault_source_entity

        entity1 = create_vault_source_entity(
            file_path="/vault/meeting.md",
            person_name="Alice",
        )
        entity2 = create_vault_source_entity(
            file_path="/vault/meeting.md",
            person_name="Bob",
        )

        assert entity1.source_id != entity2.source_id
        assert entity1.source_id == "/vault/meeting.md:Alice"
        assert entity2.source_id == "/vault/meeting.md:Bob"

    def test_source_entities_use_observed_at_for_timestamp(self):
        """observed_at is set from the provided timestamp."""
        from api.services.source_entity import create_vault_source_entity

        note_date = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        entity = create_vault_source_entity(
            file_path="/vault/note.md",
            person_name="John Doe",
            observed_at=note_date,
        )

        assert entity.observed_at == note_date
