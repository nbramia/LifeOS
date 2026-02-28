"""
Tests for InteractionStore.
"""
import pytest
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from api.services.interaction_store import (
    Interaction,
    InteractionStore,
    get_interaction_store,
    build_obsidian_link,
    build_gmail_link,
    build_calendar_link,
    create_gmail_interaction,
    create_calendar_interaction,
    create_vault_interaction,
)


class TestInteraction:
    """Tests for Interaction dataclass."""

    def test_create_interaction(self):
        """Test creating a basic interaction."""
        interaction = Interaction(
            id="test-id",
            person_id="person-123",
            timestamp=datetime(2024, 6, 15, 10, 30),
            source_type="gmail",
            title="Re: Project Update",
            snippet="Thanks for the update...",
            source_link="https://mail.google.com/...",
            source_id="msg-abc123",
        )

        assert interaction.id == "test-id"
        assert interaction.person_id == "person-123"
        assert interaction.source_type == "gmail"
        assert interaction.title == "Re: Project Update"
        assert interaction.source_badge == "📧"

    def test_source_badges(self):
        """Test source badge property for different types."""
        badges = {
            "gmail": "📧",
            "calendar": "📅",
            "vault": "📝",
            "granola": "📝",
        }

        for source_type, expected_badge in badges.items():
            interaction = Interaction(
                id="test",
                person_id="p1",
                timestamp=datetime.now(),
                source_type=source_type,
                title="Test",
            )
            assert interaction.source_badge == expected_badge

    def test_to_dict_and_from_dict(self):
        """Test JSON serialization roundtrip."""
        original = Interaction(
            id="test-id",
            person_id="person-123",
            timestamp=datetime(2024, 6, 15, 10, 30, 0),
            source_type="calendar",
            title="1:1 Meeting",
            snippet="Discuss project roadmap",
            source_link="https://calendar.google.com/...",
            source_id="event-xyz",
            created_at=datetime(2024, 6, 15, 11, 0, 0),
        )

        data = original.to_dict()
        assert isinstance(data["timestamp"], str)
        assert isinstance(data["created_at"], str)

        restored = Interaction.from_dict(data)
        assert restored.id == original.id
        assert restored.person_id == original.person_id
        # Datetimes are normalized to UTC-aware after serialization roundtrip
        # So we compare the timestamp values (year, month, day, hour, minute, second)
        assert restored.timestamp.replace(tzinfo=None) == original.timestamp.replace(tzinfo=None)
        assert restored.created_at.replace(tzinfo=None) == original.created_at.replace(tzinfo=None)
        # Restored datetimes should be timezone-aware
        assert restored.timestamp.tzinfo is not None
        assert restored.created_at.tzinfo is not None
        assert restored.source_type == original.source_type
        assert restored.title == original.title
        assert restored.snippet == original.snippet
        assert restored.source_link == original.source_link
        assert restored.source_id == original.source_id


class TestLinkBuilders:
    """Tests for link building functions."""

    def test_build_gmail_link(self):
        """Test Gmail link generation."""
        link = build_gmail_link("abc123")
        assert "mail.google.com" in link
        assert "abc123" in link

    def test_build_calendar_link(self):
        """Test Calendar link generation."""
        link = build_calendar_link("event123")
        assert "calendar.google.com" in link
        assert "event123" in link

    def test_build_obsidian_link(self):
        """Test Obsidian URI generation."""
        link = build_obsidian_link(
            "/Users/test/Notes 2025/Work/meeting.md",
            vault_path="/Users/test/Notes 2025",
        )
        assert link.startswith("obsidian://")
        assert "vault=" in link
        assert "file=" in link


class TestInteractionStore:
    """Tests for InteractionStore."""

    @pytest.fixture
    def temp_store(self):
        """Create a temporary store for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = InteractionStore(f.name)
            yield store
            Path(f.name).unlink(missing_ok=True)

    def test_add_and_get_by_id(self, temp_store):
        """Test adding and retrieving an interaction."""
        interaction = Interaction(
            id=str(uuid.uuid4()),
            person_id="person-123",
            timestamp=datetime.now(),
            source_type="gmail",
            title="Test Email",
        )

        temp_store.add(interaction)
        retrieved = temp_store.get_by_id(interaction.id)

        assert retrieved is not None
        assert retrieved.id == interaction.id
        assert retrieved.title == "Test Email"

    def test_get_by_source(self, temp_store):
        """Test retrieving interaction by source."""
        interaction = Interaction(
            id=str(uuid.uuid4()),
            person_id="person-123",
            timestamp=datetime.now(),
            source_type="gmail",
            title="Test Email",
            source_id="msg-unique-123",
        )

        temp_store.add(interaction)
        retrieved = temp_store.get_by_source("gmail", "msg-unique-123")

        assert retrieved is not None
        assert retrieved.source_id == "msg-unique-123"

    def test_add_if_not_exists(self, temp_store):
        """Test deduplication when adding."""
        interaction1 = Interaction(
            id=str(uuid.uuid4()),
            person_id="person-123",
            timestamp=datetime.now(),
            source_type="gmail",
            title="Original",
            source_id="msg-same-id",
        )

        interaction2 = Interaction(
            id=str(uuid.uuid4()),
            person_id="person-123",
            timestamp=datetime.now(),
            source_type="gmail",
            title="Duplicate",
            source_id="msg-same-id",  # Same source_id
        )

        result1, added1 = temp_store.add_if_not_exists(interaction1)
        assert added1 is True
        assert result1.title == "Original"

        result2, added2 = temp_store.add_if_not_exists(interaction2)
        assert added2 is False
        assert result2.title == "Original"  # Returns existing

    def test_get_for_person(self, temp_store):
        """Test getting interactions for a person."""
        person_id = "person-abc"

        # Add interactions at different times
        for i, days_ago in enumerate([1, 5, 30, 500]):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now() - timedelta(days=days_ago),
                source_type="vault",
                title=f"Note {i}",
            )
            temp_store.add(interaction)

        # Default window is 3650 days (10 years), so all 4 interactions are included
        results = temp_store.get_for_person(person_id)
        assert len(results) == 4

        # Explicit 365-day window excludes 500-day-old interaction
        results = temp_store.get_for_person(person_id, days_back=365)
        assert len(results) == 3  # Excludes 500 days ago

        # Custom window
        results = temp_store.get_for_person(person_id, days_back=10)
        assert len(results) == 2  # Only 1 and 5 days ago

        # With limit
        results = temp_store.get_for_person(person_id, limit=2)
        assert len(results) == 2

        # Should be ordered by timestamp DESC (most recent first)
        assert results[0].title == "Note 0"  # 1 day ago

    def test_get_for_person_by_source_type(self, temp_store):
        """Test filtering interactions by source type."""
        person_id = "person-filter"

        # Add different source types
        for source in ["gmail", "gmail", "calendar", "vault"]:
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now(),
                source_type=source,
                title=f"Test {source}",
            )
            temp_store.add(interaction)

        # Filter by source
        gmail_results = temp_store.get_for_person(person_id, source_type="gmail")
        assert len(gmail_results) == 2

        calendar_results = temp_store.get_for_person(
            person_id, source_type="calendar"
        )
        assert len(calendar_results) == 1

    def test_get_interaction_counts(self, temp_store):
        """Test getting interaction counts by source."""
        person_id = "person-counts"

        # Add various interactions
        sources = ["gmail"] * 5 + ["calendar"] * 3 + ["vault"] * 2

        for i, source in enumerate(sources):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now() - timedelta(days=i),
                source_type=source,
                title=f"Test {i}",
            )
            temp_store.add(interaction)

        counts = temp_store.get_interaction_counts(person_id)

        assert counts.get("gmail") == 5
        assert counts.get("calendar") == 3
        assert counts.get("vault") == 2

    def test_get_last_interaction(self, temp_store):
        """Test getting most recent interaction."""
        person_id = "person-last"

        # Add interactions
        for i, days_ago in enumerate([10, 5, 1]):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now() - timedelta(days=days_ago),
                source_type="vault",
                title=f"Note {days_ago} days ago",
            )
            temp_store.add(interaction)

        last = temp_store.get_last_interaction(person_id)
        assert last is not None
        assert "1 days ago" in last.title

    def test_get_last_interaction_by_source(self, temp_store):
        """Test getting most recent interaction per source type."""
        person_id = "person-channel-recency"

        # Add interactions across different channels at different times
        # Gmail: 10 days ago, 5 days ago (most recent gmail = 5 days ago)
        # iMessage: 2 days ago (most recent imessage = 2 days ago)
        # Calendar: 30 days ago (most recent calendar = 30 days ago)
        interactions = [
            ("gmail", 10),
            ("gmail", 5),
            ("imessage", 2),
            ("calendar", 30),
        ]

        for source, days_ago in interactions:
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now() - timedelta(days=days_ago),
                source_type=source,
                title=f"{source} {days_ago} days ago",
            )
            temp_store.add(interaction)

        recency = temp_store.get_last_interaction_by_source(person_id)

        # Should have 3 keys (gmail, imessage, calendar)
        assert len(recency) == 3
        assert "gmail" in recency
        assert "imessage" in recency
        assert "calendar" in recency

        # Verify the most recent timestamp for each source
        now = datetime.now()
        gmail_days_ago = (now - recency["gmail"].replace(tzinfo=None)).days
        imessage_days_ago = (now - recency["imessage"].replace(tzinfo=None)).days
        calendar_days_ago = (now - recency["calendar"].replace(tzinfo=None)).days

        assert gmail_days_ago == 5  # Most recent gmail was 5 days ago
        assert imessage_days_ago == 2  # Most recent imessage was 2 days ago
        assert calendar_days_ago == 30  # Most recent calendar was 30 days ago

        # All returned datetimes should be timezone-aware
        for dt in recency.values():
            assert dt.tzinfo is not None

    def test_get_last_interaction_by_source_empty(self, temp_store):
        """Test channel recency for person with no interactions."""
        recency = temp_store.get_last_interaction_by_source("nonexistent-person")
        assert recency == {}

    def test_delete(self, temp_store):
        """Test deleting an interaction."""
        interaction = Interaction(
            id=str(uuid.uuid4()),
            person_id="person-123",
            timestamp=datetime.now(),
            source_type="gmail",
            title="To Delete",
        )

        temp_store.add(interaction)
        assert temp_store.get_by_id(interaction.id) is not None

        result = temp_store.delete(interaction.id)
        assert result is True
        assert temp_store.get_by_id(interaction.id) is None

        # Delete non-existent
        result = temp_store.delete("fake-id")
        assert result is False

    def test_delete_for_person(self, temp_store):
        """Test deleting all interactions for a person."""
        person_id = "person-to-delete"

        # Add multiple interactions
        for i in range(5):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now(),
                source_type="vault",
                title=f"Note {i}",
            )
            temp_store.add(interaction)

        # Also add for another person
        other = Interaction(
            id=str(uuid.uuid4()),
            person_id="other-person",
            timestamp=datetime.now(),
            source_type="vault",
            title="Other",
        )
        temp_store.add(other)

        # Delete for specific person
        deleted = temp_store.delete_for_person(person_id)
        assert deleted == 5

        # Verify other person's interactions still exist
        assert temp_store.get_by_id(other.id) is not None

    def test_statistics(self, temp_store):
        """Test getting store statistics."""
        # Add interactions for multiple people
        for i in range(10):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=f"person-{i % 3}",  # 3 unique people
                timestamp=datetime.now() - timedelta(days=i),
                source_type=["gmail", "calendar", "vault"][i % 3],
                title=f"Test {i}",
            )
            temp_store.add(interaction)

        stats = temp_store.get_statistics()

        assert stats["total_interactions"] == 10
        assert stats["unique_people"] == 3
        assert sum(stats["by_source"].values()) == 10

    def test_format_interaction_history(self, temp_store):
        """Test formatted markdown output."""
        person_id = "person-format"

        # Add some interactions
        for i, (source, title) in enumerate(
            [
                ("gmail", "Re: Budget Update"),
                ("calendar", "1:1 Meeting"),
                ("vault", "Project Notes"),
            ]
        ):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=datetime.now() - timedelta(days=i),
                source_type=source,
                title=title,
                source_link=f"https://example.com/{i}",
            )
            temp_store.add(interaction)

        formatted = temp_store.format_interaction_history(person_id)

        # Check structure
        assert "**Summary:**" in formatted
        assert "3 interactions" in formatted
        assert "📧" in formatted
        assert "📅" in formatted
        assert "📝" in formatted
        assert "Re: Budget Update" in formatted

    def test_count(self, temp_store):
        """Test counting interactions."""
        assert temp_store.count() == 0

        for i in range(3):
            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id="person-123",
                timestamp=datetime.now(),
                source_type="vault",
                title=f"Note {i}",
            )
            temp_store.add(interaction)

        assert temp_store.count() == 3


class TestInteractionFactories:
    """Tests for interaction factory functions."""

    def test_create_gmail_interaction(self):
        """Test Gmail interaction factory."""
        interaction = create_gmail_interaction(
            person_id="person-123",
            message_id="msg-abc",
            subject="Test Subject",
            timestamp=datetime(2024, 6, 15),
            snippet="Email body preview...",
        )

        assert interaction.source_type == "gmail"
        assert interaction.title == "Test Subject"
        assert "mail.google.com" in interaction.source_link
        assert interaction.source_id == "msg-abc"
        assert interaction.id  # UUID should be generated

    def test_create_calendar_interaction(self):
        """Test Calendar interaction factory."""
        interaction = create_calendar_interaction(
            person_id="person-123",
            event_id="event-xyz",
            title="Team Meeting",
            timestamp=datetime(2024, 6, 15, 14, 0),
            snippet="Discuss Q3 goals",
        )

        assert interaction.source_type == "calendar"
        assert interaction.title == "Team Meeting"
        assert "calendar.google.com" in interaction.source_link
        assert interaction.source_id == "event-xyz"

    def test_create_vault_interaction(self):
        """Test Vault interaction factory."""
        interaction = create_vault_interaction(
            person_id="person-123",
            file_path="/Users/test/vault/notes/meeting.md",
            title="Meeting Notes",
            timestamp=datetime(2024, 6, 15),
            snippet="We discussed...",
            is_granola=False,
        )

        assert interaction.source_type == "vault"
        assert interaction.title == "Meeting Notes"
        assert "obsidian://" in interaction.source_link

    def test_create_granola_interaction(self):
        """Test Granola (meeting note) interaction factory."""
        interaction = create_vault_interaction(
            person_id="person-123",
            file_path="/Users/test/vault/Granola/meeting.md",
            title="Standup",
            timestamp=datetime(2024, 6, 15),
            is_granola=True,
        )

        assert interaction.source_type == "granola"


class TestTimezoneHandling:
    """Tests for timezone-aware datetime handling."""

    def test_from_dict_naive_datetime_becomes_aware(self):
        """Test that naive datetime strings become timezone-aware."""
        data = {
            "id": "test-id",
            "person_id": "person-123",
            "timestamp": "2024-06-15T10:30:00",  # No timezone
            "source_type": "gmail",
            "title": "Test Email",
            "source_link": "https://example.com",
            "source_id": "msg-123",
            "created_at": "2024-06-15T11:00:00",  # No timezone
        }
        interaction = Interaction.from_dict(data)
        assert interaction.timestamp.tzinfo is not None
        assert interaction.created_at.tzinfo is not None

    def test_from_row_naive_datetime_becomes_aware(self):
        """Test that naive datetime from SQLite becomes timezone-aware."""
        row = (
            "id-1",
            "person-123",
            "2024-06-15T10:30:00",  # Naive timestamp
            "calendar",
            "Meeting Title",
            "Snippet text",
            "https://example.com",
            "event-123",
            "2024-06-15T11:00:00",  # Naive created_at
        )
        interaction = Interaction.from_row(row)
        assert interaction.timestamp.tzinfo is not None
        assert interaction.created_at.tzinfo is not None
