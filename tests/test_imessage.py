"""
Tests for iMessage integration.
"""
import pytest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.services.imessage import (
    IMessageStore,
    IMessageRecord,
    apple_timestamp_to_datetime,
    datetime_to_apple_timestamp,
    extract_text_from_attributed_body,
    get_imessage_store,
)


class TestAppleTimestampConversion:
    """Tests for Apple timestamp conversion functions."""

    def test_apple_timestamp_to_datetime(self):
        """Test converting Apple timestamp to datetime."""
        # Apple timestamp for 2024-01-15 12:00:00 UTC
        # Unix epoch + offset + date
        apple_ts = 726840000_000_000_000  # nanoseconds

        dt = apple_timestamp_to_datetime(apple_ts)

        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        # Exact day depends on timezone, just verify it's in January 2024
        assert dt.tzinfo == timezone.utc

    def test_apple_timestamp_zero_returns_none(self):
        """Test that zero timestamp returns None."""
        assert apple_timestamp_to_datetime(0) is None
        assert apple_timestamp_to_datetime(None) is None

    def test_datetime_to_apple_timestamp_roundtrip(self):
        """Test roundtrip conversion."""
        original = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        apple_ts = datetime_to_apple_timestamp(original)
        converted = apple_timestamp_to_datetime(apple_ts)

        # Allow 1 second tolerance for floating point
        assert abs((converted - original).total_seconds()) < 1


class TestIMessageStore:
    """Tests for IMessageStore."""

    @pytest.fixture
    def temp_store(self):
        """Create a temporary store for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = IMessageStore(f.name)
            yield store
            Path(f.name).unlink(missing_ok=True)

    def test_store_creates_schema(self, temp_store):
        """Test that store creates proper schema."""
        import sqlite3

        with sqlite3.connect(temp_store.storage_path) as conn:
            # Check tables exist
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor}

            assert "messages" in tables
            assert "sync_state" in tables

    def test_sync_state_tracking(self, temp_store):
        """Test sync state ROWID tracking."""
        # Initial state should be 0
        assert temp_store._get_last_synced_rowid() == 0

        # Update and verify
        temp_store._set_last_synced_rowid(12345)
        assert temp_store._get_last_synced_rowid() == 12345

        # Update again
        temp_store._set_last_synced_rowid(99999)
        assert temp_store._get_last_synced_rowid() == 99999

    def test_insert_and_query_messages(self, temp_store):
        """Test inserting and querying messages."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Hello!", now.isoformat(), 1, "+15551234567", "+15551234567", "iMessage"),
            (2, "Hi there", now.isoformat(), 0, "+15551234567", "+15551234567", "iMessage"),
            (3, "How are you?", now.isoformat(), 1, "+15557654321", "+15557654321", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Query by phone
        messages = temp_store.get_messages_for_phone("+15551234567")
        assert len(messages) == 2

        messages = temp_store.get_messages_for_phone("+15557654321")
        assert len(messages) == 1

    def test_statistics(self, temp_store):
        """Test statistics collection."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test 1", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
            (2, "Test 2", now.isoformat(), 0, "+15552222222", "+15552222222", "SMS"),
            (3, "Test 3", now.isoformat(), 0, "+15553333333", "+15553333333", "SMS"),
        ]
        temp_store._insert_batch(batch)

        stats = temp_store.get_statistics()

        assert stats["total_messages"] == 3
        assert stats["by_service"]["iMessage"] == 1
        assert stats["by_service"]["SMS"] == 2
        assert stats["sent"] == 1
        assert stats["received"] == 2
        assert stats["unique_contacts"] == 3

    def test_update_entity_mappings(self, temp_store):
        """Test updating entity mappings."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test 1", now.isoformat(), 1, "+15551234567", "+15551234567", "iMessage"),
            (2, "Test 2", now.isoformat(), 0, "+15551234567", "+15551234567", "iMessage"),
            (3, "Test 3", now.isoformat(), 0, "+15559876543", "+15559876543", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Update mappings
        phone_to_entity = {
            "+15551234567": "entity-123",
            "+15559876543": "entity-456",
        }
        updated = temp_store.update_entity_mappings(phone_to_entity)

        assert updated == 3

        # Verify by querying
        messages = temp_store.get_messages_for_entity("entity-123")
        assert len(messages) == 2

        messages = temp_store.get_messages_for_entity("entity-456")
        assert len(messages) == 1

    def test_search_messages(self, temp_store):
        """Test message search."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Hello world!", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
            (2, "Goodbye world!", now.isoformat(), 0, "+15551111111", "+15551111111", "iMessage"),
            (3, "Something else", now.isoformat(), 0, "+15552222222", "+15552222222", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Search for "world"
        results = temp_store.search_messages("world")
        assert len(results) == 2

        # Search with phone filter
        results = temp_store.search_messages("world", phone="+15551111111")
        assert len(results) == 2

        # Search for non-existent
        results = temp_store.search_messages("foobar")
        assert len(results) == 0

    def test_clear_data(self, temp_store):
        """Test clearing data for full resync."""
        # Insert some data
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
        ]
        temp_store._insert_batch(batch)
        temp_store._set_last_synced_rowid(1000)

        # Verify data exists
        assert temp_store.get_statistics()["total_messages"] == 1
        assert temp_store._get_last_synced_rowid() == 1000

        # Clear data
        temp_store._clear_data()

        # Verify cleared
        assert temp_store.get_statistics()["total_messages"] == 0
        assert temp_store._get_last_synced_rowid() == 0


class TestIMessageRecord:
    """Tests for IMessageRecord dataclass."""

    def test_create_record(self):
        """Test creating an IMessageRecord."""
        record = IMessageRecord(
            rowid=1,
            text="Hello!",
            timestamp=datetime.now(timezone.utc),
            is_from_me=True,
            handle="+15551234567",
            handle_normalized="+15551234567",
            service="iMessage",
        )

        assert record.rowid == 1
        assert record.text == "Hello!"
        assert record.is_from_me is True
        assert record.service == "iMessage"
        assert record.person_entity_id is None


class TestExtractTextFromAttributedBody:
    """Tests for attributedBody text extraction."""

    def test_extract_simple_text(self):
        """Test extracting text from simulated attributedBody."""
        # Simulate the format with embedded text
        blob = b"streamtyped\x00\x00\x00NSString\x00Hello world!\x00NSDictionary"
        result = extract_text_from_attributed_body(blob)
        assert result == "Hello world!"

    def test_extract_longer_text_wins(self):
        """Test that longest non-metadata string is returned."""
        blob = b"\x00NSString\x00Hi\x00This is a longer message\x00NSObject"
        result = extract_text_from_attributed_body(blob)
        assert result == "This is a longer message"

    def test_extract_none_for_empty(self):
        """Test that None is returned for empty input."""
        assert extract_text_from_attributed_body(None) is None
        assert extract_text_from_attributed_body(b"") is None

    def test_extract_filters_metadata(self):
        """Test that NS* metadata strings are filtered out."""
        blob = b"NSMutableAttributedString\x00NSString\x00Actual message"
        result = extract_text_from_attributed_body(blob)
        assert result == "Actual message"

    def test_extract_unicode_text(self):
        """Test extraction of unicode text."""
        # Unicode text with accented characters
        blob = "streamtyped\x00\x00Hello café world!\x00NSString".encode("utf-8")
        result = extract_text_from_attributed_body(blob)
        assert result == "Hello café world!"
