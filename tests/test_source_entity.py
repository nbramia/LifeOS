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


class TestLinkMethod:
    """Tests for link_method provenance tracking."""

    def test_link_method_in_dataclass(self):
        """link_method field defaults to None."""
        entity = SourceEntity(source_type="gmail", source_id="msg1")
        assert entity.link_method is None

    def test_link_method_set_on_creation(self):
        """link_method can be set at creation time."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        assert entity.link_method == "email_exact"

    def test_link_method_persisted_on_add(self, store):
        """link_method is stored and retrieved via add/get."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="name_fuzzy",
        )
        store.add(entity)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "name_fuzzy"

    def test_link_method_persisted_on_update(self, store):
        """link_method is preserved through update."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        store.add(entity)

        entity.link_method = "phone_exact"
        store.update(entity)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "phone_exact"

    def test_link_to_person_with_method(self, store):
        """link_to_person sets link_method when provided."""
        entity = SourceEntity(source_type="gmail", source_id="msg1")
        store.add(entity)

        store.link_to_person(
            entity.id, "person1", confidence=0.9, method="email_exact",
        )

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "email_exact"

    def test_link_to_person_preserves_existing_method(self, store):
        """link_to_person without method keeps existing link_method (COALESCE)."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="name_fuzzy",
        )
        store.add(entity)

        store.link_to_person(entity.id, "person1", confidence=0.95)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "name_fuzzy"

    def test_link_method_in_to_dict(self):
        """link_method appears in to_dict output."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        d = entity.to_dict()
        assert d["link_method"] == "email_exact"

    def test_link_method_roundtrip_from_dict(self):
        """link_method survives to_dict → from_dict roundtrip."""
        original = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="phone_exact",
        )
        restored = SourceEntity.from_dict(original.to_dict())
        assert restored.link_method == "phone_exact"

    def test_get_low_confidence_filter_by_link_method(self, store):
        """get_low_confidence can filter by link_method."""
        for i, method in enumerate(["email_exact", "name_fuzzy", "name_fuzzy"]):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
                link_confidence=0.7,
                link_method=method,
            )
            store.add(entity, validate_person=False)

        all_low = store.get_low_confidence(max_confidence=0.85)
        assert len(all_low) == 3

        fuzzy_only = store.get_low_confidence(max_confidence=0.85, link_method="name_fuzzy")
        assert len(fuzzy_only) == 2

        exact_only = store.get_low_confidence(max_confidence=0.85, link_method="email_exact")
        assert len(exact_only) == 1

    def test_count_low_confidence_filter_by_link_method(self, store):
        """count_low_confidence can filter by link_method."""
        for i, method in enumerate(["email_exact", "name_fuzzy"]):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
                link_confidence=0.6,
                link_method=method,
            )
            store.add(entity, validate_person=False)

        assert store.count_low_confidence(max_confidence=0.85) == 2
        assert store.count_low_confidence(max_confidence=0.85, link_method="name_fuzzy") == 1
        assert store.count_low_confidence(max_confidence=0.85, link_method="nonexistent") == 0


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

    def test_create_phone_source_entity(self):
        """Phone factory uses the canonical ``phone_{e164}`` source_id and
        sets observed_phone (issue #228 — single definition of the format).
        """
        from api.services.source_entity import create_phone_source_entity

        entity = create_phone_source_entity(
            phone="+15551234567",
            observed_name="Mom",
        )

        assert entity.source_type == "phone"
        assert entity.source_id == "phone_+15551234567"
        assert entity.observed_phone == "+15551234567"
        assert entity.observed_name == "Mom"
        # observed_at defaults to "now" when not supplied.
        assert entity.observed_at is not None
        # Email is irrelevant here.
        assert entity.observed_email is None

    def test_create_phone_source_entity_minimal(self):
        """Only ``phone`` is required; everything else takes safe defaults."""
        from api.services.source_entity import create_phone_source_entity

        entity = create_phone_source_entity(phone="+15558675309")
        assert entity.source_id == "phone_+15558675309"
        assert entity.observed_name is None
        assert entity.metadata == {}

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
