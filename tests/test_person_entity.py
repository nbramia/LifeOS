"""
Tests for PersonEntity model and PersonEntityStore.
"""
import json
import pytest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from api.services.person_entity import (
    PersonEntity,
    PersonEntityStore,
    get_person_entity_store,
)


class TestPersonEntity:
    """Tests for PersonEntity dataclass."""

    def test_create_basic_entity(self):
        """Test creating a basic PersonEntity."""
        entity = PersonEntity(
            canonical_name="Alex Johnson",
            emails=["alex@work.example.com"],
            category="work",
        )

        assert entity.canonical_name == "Alex Johnson"
        assert entity.display_name == "Alex Johnson"  # Auto-set from canonical
        assert entity.emails == ["alex@work.example.com"]
        assert entity.category == "work"
        assert entity.id  # UUID should be auto-generated
        assert entity.confidence_score == 1.0

    def test_primary_email(self):
        """Test primary_email property."""
        entity = PersonEntity(
            canonical_name="Test",
            emails=["first@test.com", "second@test.com"],
        )
        assert entity.primary_email == "first@test.com"

        # Empty emails list
        entity_no_email = PersonEntity(canonical_name="No Email")
        assert entity_no_email.primary_email is None

    def test_has_email(self):
        """Test has_email method (case-insensitive)."""
        entity = PersonEntity(
            canonical_name="Test",
            emails=["Test@Example.com"],
        )

        assert entity.has_email("test@example.com")
        assert entity.has_email("TEST@EXAMPLE.COM")
        assert entity.has_email("Test@Example.com")
        assert not entity.has_email("other@example.com")

    def test_add_email(self):
        """Test add_email method."""
        entity = PersonEntity(canonical_name="Test", emails=["first@test.com"])

        # Add new email
        assert entity.add_email("second@test.com") is True
        assert len(entity.emails) == 2
        assert "second@test.com" in entity.emails

        # Try to add duplicate (case-insensitive)
        assert entity.add_email("FIRST@TEST.COM") is False
        assert len(entity.emails) == 2

        # Empty email
        assert entity.add_email("") is False

    def test_has_phone(self):
        """Test has_phone method."""
        entity = PersonEntity(
            canonical_name="Test",
            phone_numbers=["+19012295017"],
        )

        assert entity.has_phone("+19012295017")
        assert not entity.has_phone("+15555555555")

    def test_add_phone(self):
        """Test add_phone method."""
        entity = PersonEntity(canonical_name="Test", phone_numbers=[])

        # Add first phone - should also set as primary
        assert entity.add_phone("+19012295017") is True
        assert len(entity.phone_numbers) == 1
        assert entity.phone_primary == "+19012295017"

        # Add second phone
        assert entity.add_phone("+15551234567") is True
        assert len(entity.phone_numbers) == 2
        assert entity.phone_primary == "+19012295017"  # Still first one

        # Try to add duplicate
        assert entity.add_phone("+19012295017") is False
        assert len(entity.phone_numbers) == 2

        # Empty phone
        assert entity.add_phone("") is False

    def test_merge_entities(self):
        """Test merging two PersonEntity objects."""
        entity1 = PersonEntity(
            canonical_name="Sarah Chen",
            emails=["sarah@work.example.com"],
            company="Example Corp",
            sources=["linkedin"],
            first_seen=datetime(2024, 1, 1),
            last_seen=datetime(2024, 6, 1),
            meeting_count=5,
            email_count=10,
            aliases=["Sarah"],
            phone_numbers=["+19012295017"],
            phone_primary="+19012295017",
            confidence_score=0.9,
        )

        entity2 = PersonEntity(
            canonical_name="Sarah Chen",
            emails=["sarah.chen@example.com"],  # Different email
            position="Engineer",
            sources=["gmail", "calendar"],
            first_seen=datetime(2023, 6, 1),  # Earlier
            last_seen=datetime(2024, 12, 1),  # Later
            meeting_count=3,
            email_count=5,
            aliases=["S. Chen"],
            phone_numbers=["+15551234567"],  # Different phone
            phone_primary="+15551234567",
            confidence_score=0.8,
        )

        merged = entity1.merge(entity2)

        # Should keep original ID
        assert merged.id == entity1.id

        # Should combine emails
        assert len(merged.emails) == 2
        assert "sarah@work.example.com" in merged.emails
        assert "sarah.chen@example.com" in merged.emails

        # Should combine sources
        assert set(merged.sources) == {"linkedin", "gmail", "calendar"}

        # Should take earliest first_seen
        assert merged.first_seen == datetime(2023, 6, 1)

        # Should take latest last_seen
        assert merged.last_seen == datetime(2024, 12, 1)

        # Should sum counts
        assert merged.meeting_count == 8
        assert merged.email_count == 15

        # Should combine aliases
        assert "Sarah" in merged.aliases
        assert "S. Chen" in merged.aliases

        # Should combine phone numbers
        assert len(merged.phone_numbers) == 2
        assert "+19012295017" in merged.phone_numbers
        assert "+15551234567" in merged.phone_numbers

        # Should preserve first entity's phone_primary
        assert merged.phone_primary == "+19012295017"

        # Should take first non-None values
        assert merged.company == "Example Corp"
        assert merged.position == "Engineer"

        # Confidence should be reduced
        assert merged.confidence_score < 0.9

    def test_to_dict_and_from_dict(self):
        """Test JSON serialization roundtrip."""
        original = PersonEntity(
            id="test-uuid",
            canonical_name="Test Person",
            display_name="Test (Company)",
            emails=["test@example.com"],
            company="Test Corp",
            position="Developer",
            linkedin_url="https://linkedin.com/in/test",
            category="work",
            vault_contexts=["Work/Test/"],
            sources=["linkedin", "gmail"],
            first_seen=datetime(2024, 1, 15, 10, 30, 0),
            last_seen=datetime(2024, 12, 1, 14, 0, 0),
            meeting_count=10,
            email_count=25,
            mention_count=5,
            related_notes=["note1.md", "note2.md"],
            aliases=["Test", "TP"],
            confidence_score=0.85,
        )

        # Convert to dict
        data = original.to_dict()

        # Verify datetime serialization
        assert isinstance(data["first_seen"], str)
        assert isinstance(data["last_seen"], str)

        # Convert back
        restored = PersonEntity.from_dict(data)

        # Verify all fields match
        assert restored.id == original.id
        assert restored.canonical_name == original.canonical_name
        assert restored.display_name == original.display_name
        assert restored.emails == original.emails
        assert restored.company == original.company
        assert restored.position == original.position
        assert restored.linkedin_url == original.linkedin_url
        assert restored.category == original.category
        assert restored.vault_contexts == original.vault_contexts
        assert restored.sources == original.sources
        # Datetimes are normalized to UTC-aware after serialization roundtrip
        # So we compare the timestamp values (year, month, day, hour, minute, second)
        assert restored.first_seen.replace(tzinfo=None) == original.first_seen.replace(tzinfo=None)
        assert restored.last_seen.replace(tzinfo=None) == original.last_seen.replace(tzinfo=None)
        # Restored datetimes should be timezone-aware
        assert restored.first_seen.tzinfo is not None
        assert restored.last_seen.tzinfo is not None
        assert restored.meeting_count == original.meeting_count
        assert restored.email_count == original.email_count
        assert restored.mention_count == original.mention_count
        assert restored.related_notes == original.related_notes
        assert restored.aliases == original.aliases
        assert restored.confidence_score == original.confidence_score

    def test_from_person_record(self):
        """Test migration from PersonRecord."""
        # Import PersonRecord
        from api.services.people_aggregator import PersonRecord

        record = PersonRecord(
            canonical_name="Alex",
            email="alex@work.example.com",
            sources=["linkedin", "gmail"],
            first_seen=datetime(2024, 1, 1),
            last_seen=datetime(2024, 12, 1),
            company="Example Corp",
            position="CEO",
            linkedin_url="https://linkedin.com/in/alex",
            meeting_count=20,
            email_count=50,
            mention_count=100,
            related_notes=["meeting1.md"],
            aliases=["Alex Johnson"],
            category="work",
        )

        entity = PersonEntity.from_person_record(record)

        # Verify migration
        assert entity.canonical_name == "Alex"
        assert entity.emails == ["alex@work.example.com"]
        assert entity.sources == ["linkedin", "gmail"]
        assert entity.company == "Example Corp"
        assert entity.position == "CEO"
        assert entity.meeting_count == 20
        assert entity.email_count == 50
        assert entity.mention_count == 100
        assert entity.category == "work"
        assert entity.confidence_score == 1.0  # Full confidence for migration

    def test_to_person_record(self):
        """Test backward compatibility conversion to PersonRecord."""
        entity = PersonEntity(
            canonical_name="Test",
            emails=["primary@test.com", "secondary@test.com"],
            company="Test Corp",
            sources=["gmail"],
            meeting_count=5,
        )

        record = entity.to_person_record()

        assert record.canonical_name == "Test"
        assert record.email == "primary@test.com"  # First email becomes single email
        assert record.company == "Test Corp"
        assert record.sources == ["gmail"]
        assert record.meeting_count == 5


class TestPersonEntityStore:
    """Tests for PersonEntityStore."""

    @pytest.fixture
    def temp_store(self):
        """Create a temporary store for testing."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            store = PersonEntityStore(f.name)
            # Clear blocklist to ensure test isolation from production data
            store._blocklist.clear()
            yield store
            # Cleanup
            Path(f.name).unlink(missing_ok=True)

    def test_add_and_get_by_id(self, temp_store):
        """Test adding an entity and retrieving by ID."""
        entity = PersonEntity(
            canonical_name="Test Person",
            emails=["test@example.com"],
        )

        temp_store.add(entity)

        retrieved = temp_store.get_by_id(entity.id)
        assert retrieved is not None
        assert retrieved.canonical_name == "Test Person"

    def test_get_by_email(self, temp_store):
        """Test retrieving entity by email (case-insensitive)."""
        entity = PersonEntity(
            canonical_name="Email Test",
            emails=["Test@Example.com"],
        )

        temp_store.add(entity)

        # Case-insensitive lookup
        assert temp_store.get_by_email("test@example.com") is not None
        assert temp_store.get_by_email("TEST@EXAMPLE.COM") is not None
        assert temp_store.get_by_email("other@example.com") is None

    def test_get_by_phone(self, temp_store):
        """Test retrieving entity by phone number."""
        entity = PersonEntity(
            canonical_name="Phone Test",
            emails=["phone@example.com"],
            phone_numbers=["+19012295017", "+15551234567"],
        )

        temp_store.add(entity)

        # Phone lookup
        assert temp_store.get_by_phone("+19012295017") is not None
        assert temp_store.get_by_phone("+15551234567") is not None
        assert temp_store.get_by_phone("+15555555555") is None

    def test_get_by_name(self, temp_store):
        """Test retrieving entity by name or alias."""
        entity = PersonEntity(
            canonical_name="John Doe",
            emails=["john@test.com"],
            aliases=["Johnny", "J. Doe"],
        )

        temp_store.add(entity)

        # By canonical name
        assert temp_store.get_by_name("John Doe") is not None
        assert temp_store.get_by_name("john doe") is not None  # Case-insensitive

        # By alias
        assert temp_store.get_by_name("Johnny") is not None
        assert temp_store.get_by_name("J. Doe") is not None

        # Unknown name
        assert temp_store.get_by_name("Unknown") is None

    def test_search(self, temp_store):
        """Test searching entities."""
        entities = [
            PersonEntity(
                canonical_name="Alice Smith",
                emails=["alice@company.com"],
            ),
            PersonEntity(
                canonical_name="Bob Johnson",
                emails=["bob@company.com"],
            ),
            PersonEntity(
                canonical_name="Alice Jones",
                emails=["alice.jones@other.com"],
            ),
        ]

        for entity in entities:
            temp_store.add(entity)

        # Search by name
        results = temp_store.search("Alice")
        assert len(results) == 2

        # Search by email domain
        results = temp_store.search("company.com")
        assert len(results) == 2

        # Search with limit
        results = temp_store.search("Alice", limit=1)
        assert len(results) == 1

    def test_update(self, temp_store):
        """Test updating an entity."""
        entity = PersonEntity(
            canonical_name="Original Name",
            emails=["original@test.com"],
        )

        temp_store.add(entity)

        # Update the entity
        entity.canonical_name = "Updated Name"
        entity.add_email("new@test.com")
        temp_store.update(entity)

        # Retrieve and verify
        retrieved = temp_store.get_by_id(entity.id)
        assert retrieved.canonical_name == "Updated Name"
        assert len(retrieved.emails) == 2

        # Old index should be updated
        assert temp_store.get_by_name("Original Name") is None
        assert temp_store.get_by_name("Updated Name") is not None

    def test_delete(self, temp_store):
        """Test deleting an entity."""
        entity = PersonEntity(
            canonical_name="To Delete",
            emails=["delete@test.com"],
        )

        temp_store.add(entity)
        assert temp_store.get_by_id(entity.id) is not None

        # Delete
        result = temp_store.delete(entity.id)
        assert result is True
        assert temp_store.get_by_id(entity.id) is None
        assert temp_store.get_by_email("delete@test.com") is None

        # Delete non-existent
        result = temp_store.delete("fake-id")
        assert result is False

    def test_persistence(self):
        """Test that entities persist across store instances."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            storage_path = f.name

        try:
            # Create store and add entity
            store1 = PersonEntityStore(storage_path)
            entity = PersonEntity(
                canonical_name="Persistent Person",
                emails=["persist@test.com"],
                company="Test Corp",
            )
            store1.add(entity)
            store1.save()

            # Create new store instance from same file
            store2 = PersonEntityStore(storage_path)

            # Should load the entity
            retrieved = store2.get_by_email("persist@test.com")
            assert retrieved is not None
            assert retrieved.canonical_name == "Persistent Person"
            assert retrieved.company == "Test Corp"
        finally:
            Path(storage_path).unlink(missing_ok=True)

    def test_statistics(self, temp_store):
        """Test getting store statistics."""
        entities = [
            PersonEntity(
                canonical_name="Work Person",
                emails=["work@company.com"],
                phone_numbers=["+19012295017"],
                sources=["linkedin", "gmail"],
                category="work",
            ),
            PersonEntity(
                canonical_name="Family Person",
                emails=["family@home.com", "alt@home.com"],
                phone_numbers=["+15551234567", "+15559876543"],
                sources=["gmail"],
                category="family",
            ),
        ]

        for entity in entities:
            temp_store.add(entity)

        stats = temp_store.get_statistics()

        assert stats["total_entities"] == 2
        assert stats["by_source"]["linkedin"] == 1
        assert stats["by_source"]["gmail"] == 2
        assert stats["by_category"]["work"] == 1
        assert stats["by_category"]["family"] == 1
        assert stats["total_emails_indexed"] == 3
        assert stats["total_phones_indexed"] == 3

    def test_get_all(self, temp_store):
        """Test getting all entities."""
        entities = [
            PersonEntity(canonical_name=f"Person {i}", emails=[f"p{i}@test.com"])
            for i in range(5)
        ]

        for entity in entities:
            temp_store.add(entity)

        all_entities = temp_store.get_all()
        assert len(all_entities) == 5

    def test_count(self, temp_store):
        """Test counting entities."""
        assert temp_store.count() == 0

        temp_store.add(PersonEntity(canonical_name="One", emails=["one@test.com"]))
        assert temp_store.count() == 1

        temp_store.add(PersonEntity(canonical_name="Two", emails=["two@test.com"]))
        assert temp_store.count() == 2


class TestTimezoneHandling:
    """Tests for timezone-aware datetime handling."""

    def test_from_dict_naive_datetime_becomes_aware(self):
        """Test that naive datetime strings become timezone-aware."""
        data = {
            "id": "test-id",
            "canonical_name": "Test Person",
            "emails": ["test@test.com"],
            "first_seen": "2024-01-15T10:30:00",  # No timezone
            "last_seen": "2024-06-01T14:00:00",   # No timezone
        }
        entity = PersonEntity.from_dict(data)
        assert entity.first_seen.tzinfo is not None
        assert entity.last_seen.tzinfo is not None

    def test_from_dict_aware_datetime_preserved(self):
        """Test that timezone-aware datetime strings are preserved."""
        data = {
            "id": "test-id",
            "canonical_name": "Test Person",
            "emails": ["test@test.com"],
            "first_seen": "2024-01-15T10:30:00+00:00",
            "last_seen": "2024-06-01T14:00:00-05:00",
        }
        entity = PersonEntity.from_dict(data)
        assert entity.first_seen.tzinfo is not None
        assert entity.last_seen.tzinfo is not None

    def test_merge_mixed_timezone_datetimes(self):
        """Test merging entities with mixed timezone awareness."""
        from datetime import datetime, timezone

        entity1 = PersonEntity(
            id="id1",
            canonical_name="Test",
            emails=["test1@test.com"],
            first_seen=datetime(2024, 1, 1),  # Naive
            last_seen=datetime(2024, 6, 1),   # Naive
        )
        entity2 = PersonEntity(
            id="id2",
            canonical_name="Test",
            emails=["test2@test.com"],
            first_seen=datetime(2023, 6, 1, tzinfo=timezone.utc),  # Aware, earlier
            last_seen=datetime(2024, 12, 1, tzinfo=timezone.utc),  # Aware, later
        )
        # Should NOT raise TypeError
        merged = entity1.merge(entity2)
        assert merged.first_seen.year == 2023  # Earlier
        assert merged.last_seen.month == 12  # Later
