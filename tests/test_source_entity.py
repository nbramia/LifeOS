"""Tests for SourceEntity and SourceEntityStore."""
import tempfile
import pytest
from datetime import datetime, timezone

from api.services.source_entity import (
    SourceEntity,
    SourceEntityStore,
    LINK_STATUS_AUTO,
    LINK_STATUS_CONFIRMED,
    LINK_STATUS_REJECTED,
    create_gmail_source_entity,
    create_calendar_source_entity,
    create_imessage_source_entity,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield f.name


@pytest.fixture
def store(temp_db):
    """Create a SourceEntityStore with temp database."""
    return SourceEntityStore(db_path=temp_db)


class TestSourceEntity:
    """Tests for SourceEntity dataclass."""

    def test_create_source_entity(self):
        """Test basic source entity creation."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
        )

        assert entity.source_type == "gmail"
        assert entity.source_id == "msg123"
        assert entity.observed_name == "John Doe"
        assert entity.observed_email == "john@example.com"
        assert entity.canonical_person_id is None
        assert entity.link_status == LINK_STATUS_AUTO

    def test_source_badge(self):
        """Test source badge emoji lookup."""
        gmail = SourceEntity(source_type="gmail")
        assert gmail.source_badge == "📧"

        calendar = SourceEntity(source_type="calendar")
        assert calendar.source_badge == "📅"

        slack = SourceEntity(source_type="slack")
        assert slack.source_badge == "💬"

    def test_is_linked(self):
        """Test is_linked property."""
        unlinked = SourceEntity(source_type="gmail")
        assert not unlinked.is_linked

        linked = SourceEntity(
            source_type="gmail",
            canonical_person_id="person123",
            link_status=LINK_STATUS_CONFIRMED,
        )
        assert linked.is_linked

        rejected = SourceEntity(
            source_type="gmail",
            canonical_person_id="person123",
            link_status=LINK_STATUS_REJECTED,
        )
        assert not rejected.is_linked

    def test_to_dict_from_dict(self):
        """Test serialization roundtrip."""
        now = datetime.now(timezone.utc)
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
            observed_at=now,
            metadata={"thread_id": "thread456"},
        )

        data = entity.to_dict()
        restored = SourceEntity.from_dict(data)

        assert restored.source_type == entity.source_type
        assert restored.source_id == entity.source_id
        assert restored.observed_name == entity.observed_name
        assert restored.observed_email == entity.observed_email
        assert restored.metadata == entity.metadata


class TestSourceEntityStore:
    """Tests for SourceEntityStore."""

    def test_add_entity(self, store):
        """Test adding a source entity."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
        )

        added = store.add(entity)
        assert added.id == entity.id

        # Verify it's stored
        retrieved = store.get_by_id(entity.id)
        assert retrieved is not None
        assert retrieved.observed_name == "John Doe"

    def test_get_by_source(self, store):
        """Test getting entity by source type and ID."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_email="john@example.com",
        )
        store.add(entity)

        retrieved = store.get_by_source("gmail", "msg123")
        assert retrieved is not None
        assert retrieved.observed_email == "john@example.com"

        # Non-existent
        not_found = store.get_by_source("gmail", "nonexistent")
        assert not_found is None

    def test_link_to_person(self, store):
        """Test linking a source entity to a canonical person."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
        )
        store.add(entity)

        success = store.link_to_person(
            entity.id,
            "person456",
            confidence=0.95,
            status=LINK_STATUS_CONFIRMED,
        )
        assert success

        retrieved = store.get_by_id(entity.id)
        assert retrieved.canonical_person_id == "person456"
        assert retrieved.link_confidence == 0.95
        assert retrieved.link_status == LINK_STATUS_CONFIRMED

    def test_unlink(self, store):
        """Test unlinking a source entity."""
        # Note: validate_person=False bypasses person existence check for unit testing
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            canonical_person_id="person456",
            link_confidence=1.0,
            link_status=LINK_STATUS_CONFIRMED,
        )
        store.add(entity, validate_person=False)

        success = store.unlink(entity.id)
        assert success

        retrieved = store.get_by_id(entity.id)
        assert retrieved.canonical_person_id is None
        assert retrieved.link_confidence == 0.0

    def test_get_for_person(self, store):
        """Test getting all entities for a canonical person."""
        # Add entities for different people
        # Note: validate_person=False bypasses person existence check for unit testing
        for i in range(3):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
            )
            store.add(entity, validate_person=False)

        entity = SourceEntity(
            source_type="gmail",
            source_id="msg99",
            canonical_person_id="person2",
        )
        store.add(entity, validate_person=False)

        # Get for person1
        entities = store.get_for_person("person1")
        assert len(entities) == 3

        # Get for person2
        entities = store.get_for_person("person2")
        assert len(entities) == 1

    def test_get_unlinked(self, store):
        """Test getting unlinked entities."""
        # Add linked and unlinked entities
        # Note: validate_person=False bypasses person existence check for unit testing
        linked = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            canonical_person_id="person1",
        )
        store.add(linked, validate_person=False)

        unlinked = SourceEntity(
            source_type="gmail",
            source_id="msg2",
        )
        store.add(unlinked)

        entities = store.get_unlinked()
        assert len(entities) == 1
        assert entities[0].source_id == "msg2"

    def test_get_by_email(self, store):
        """Test getting entities by observed email."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            observed_email="John@Example.com",
        )
        store.add(entity)

        # Case-insensitive
        entities = store.get_by_email("john@example.com")
        assert len(entities) == 1

    def test_statistics(self, store):
        """Test getting statistics."""
        # Add some entities
        # Note: validate_person=False bypasses person existence check for unit testing
        for i in range(3):
            store.add(SourceEntity(
                source_type="gmail",
                source_id=f"gmail{i}",
                canonical_person_id="person1" if i < 2 else None,
            ), validate_person=False)

        store.add(SourceEntity(
            source_type="calendar",
            source_id="cal1",
            canonical_person_id="person1",
        ), validate_person=False)

        stats = store.get_statistics()
        assert stats["total_entities"] == 4
        assert stats["linked_entities"] == 3
        assert stats["unlinked_entities"] == 1
        assert stats["by_source"]["gmail"] == 3
        assert stats["by_source"]["calendar"] == 1


class TestFactoryFunctions:
    """Tests for source entity factory functions."""

    def test_create_gmail_source_entity(self):
        """Test Gmail source entity factory."""
        entity = create_gmail_source_entity(
            message_id="msg123",
            sender_email="John@Example.com",
            sender_name="John Doe",
        )

        assert entity.source_type == "gmail"
        assert entity.source_id == "msg123"
        assert entity.observed_email == "john@example.com"  # Lowercased
        assert entity.observed_name == "John Doe"

    def test_create_calendar_source_entity(self):
        """Test Calendar source entity factory."""
        entity = create_calendar_source_entity(
            event_id="event123",
            attendee_email="Jane@Example.com",
            attendee_name="Jane Smith",
        )

        assert entity.source_type == "calendar"
        assert entity.source_id == "event123:Jane@Example.com"
        assert entity.observed_email == "jane@example.com"

    def test_create_imessage_source_entity_phone(self):
        """Test iMessage source entity with phone number."""
        entity = create_imessage_source_entity(
            handle="+15551234567",
            display_name="Mom",
        )

        assert entity.source_type == "imessage"
        assert entity.observed_phone == "+15551234567"
        assert entity.observed_email is None
        assert entity.observed_name == "Mom"

    def test_create_imessage_source_entity_email(self):
        """Test iMessage source entity with email."""
        entity = create_imessage_source_entity(
            handle="john@example.com",
        )

        assert entity.source_type == "imessage"
        assert entity.observed_email == "john@example.com"
        assert entity.observed_phone is None
