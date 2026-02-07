"""Tests for Signal chat export parser."""
import pytest
from datetime import datetime, timezone

from api.services.signal_import import (
    parse_signal_export,
    SignalContact,
    SignalConversationStats,
    create_signal_source_entity,
    SOURCE_SIGNAL,
    _parse_timestamp,
)


class TestParseSignalExport:
    """Tests for parse_signal_export function."""

    def test_parse_basic_export(self):
        """Test parsing basic Signal export."""
        data = {
            "conversations": [
                {
                    "id": "conv1",
                    "uuid": "uuid-123",
                    "name": "John Doe",
                    "e164": "+15550100",
                    "type": "private",
                },
                {
                    "id": "conv2",
                    "uuid": "uuid-456",
                    "name": "Jane Smith",
                    "e164": "+15550200",
                    "type": "private",
                },
            ],
            "messages": [
                {
                    "conversationId": "conv1",
                    "type": "outgoing",
                    "sent_at": 1704067200000,  # 2024-01-01 00:00:00 UTC
                },
                {
                    "conversationId": "conv1",
                    "type": "incoming",
                    "received_at": 1704070800000,  # 2024-01-01 01:00:00 UTC
                },
                {
                    "conversationId": "conv2",
                    "type": "outgoing",
                    "sent_at": 1704153600000,  # 2024-01-02 00:00:00 UTC
                },
            ],
        }

        export = parse_signal_export(data)

        assert len(export.contacts) == 2
        assert export.total_messages == 3
        assert "conv1" in export.conversation_stats
        assert export.conversation_stats["conv1"].message_count == 2
        assert export.conversation_stats["conv1"].sent_count == 1
        assert export.conversation_stats["conv1"].received_count == 1

    def test_parse_group_conversation(self):
        """Test parsing group conversations."""
        data = {
            "conversations": [
                {
                    "id": "group1",
                    "name": "Family Group",
                    "type": "group",
                    "members": ["member1", "member2", "member3"],
                },
            ],
            "messages": [],
        }

        export = parse_signal_export(data)

        assert len(export.contacts) == 1
        contact = export.contacts["group1"]
        assert contact.is_group is True
        assert len(contact.group_members) == 3

    def test_skip_system_conversations(self):
        """Test that system conversations are skipped."""
        data = {
            "conversations": [
                {"id": "system", "type": "system"},
                {"id": "conv1", "name": "John", "type": "private"},
            ],
            "messages": [],
        }

        export = parse_signal_export(data)

        assert len(export.contacts) == 1
        assert "system" not in export.contacts

    def test_timestamp_tracking(self):
        """Test message timestamp tracking."""
        data = {
            "conversations": [
                {"id": "conv1", "name": "John", "type": "private"},
            ],
            "messages": [
                {"conversationId": "conv1", "type": "outgoing", "sent_at": 1704067200000},
                {"conversationId": "conv1", "type": "outgoing", "sent_at": 1704153600000},
                {"conversationId": "conv1", "type": "outgoing", "sent_at": 1704240000000},
            ],
        }

        export = parse_signal_export(data)

        stats = export.conversation_stats["conv1"]
        assert stats.first_message is not None
        assert stats.last_message is not None
        assert stats.first_message < stats.last_message

    def test_empty_export(self):
        """Test handling empty export."""
        data = {"conversations": [], "messages": []}

        export = parse_signal_export(data)

        assert len(export.contacts) == 0
        assert export.total_messages == 0


class TestParseTimestamp:
    """Tests for _parse_timestamp helper."""

    def test_valid_timestamp(self):
        """Test parsing valid timestamp."""
        ts = 1704067200000  # 2024-01-01 00:00:00 UTC
        dt = _parse_timestamp(ts)

        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 1

    def test_none_timestamp(self):
        """Test handling None/0 timestamp."""
        assert _parse_timestamp(0) is None
        assert _parse_timestamp(None) is None


class TestSignalContact:
    """Tests for SignalContact dataclass."""

    def test_display_name_with_name(self):
        """Test display name with name set."""
        contact = SignalContact(
            id="conv1",
            name="John Doe",
            profile_name="Johnny",
        )

        assert contact.display_name == "John Doe"

    def test_display_name_profile_fallback(self):
        """Test display name falls back to profile_name."""
        contact = SignalContact(
            id="conv1",
            profile_name="Johnny",
        )

        assert contact.display_name == "Johnny"

    def test_display_name_phone_fallback(self):
        """Test display name falls back to phone."""
        contact = SignalContact(
            id="conv1",
            phone="+1-555-0100",
        )

        assert contact.display_name == "+1-555-0100"

    def test_display_name_id_fallback(self):
        """Test display name falls back to id."""
        contact = SignalContact(id="conv1")

        assert contact.display_name == "conv1"

    def test_to_dict(self):
        """Test serialization to dict."""
        contact = SignalContact(
            id="conv1",
            uuid="uuid-123",
            name="John Doe",
            phone="+1-555-0100",
            is_group=False,
        )

        data = contact.to_dict()
        assert data["id"] == "conv1"
        assert data["uuid"] == "uuid-123"
        assert data["name"] == "John Doe"
        assert data["phone"] == "+1-555-0100"


class TestCreateSignalSourceEntity:
    """Tests for create_signal_source_entity function."""

    def test_basic_contact(self):
        """Test creating entity from basic contact."""
        contact = SignalContact(
            id="conv1",
            uuid="uuid-123",
            name="John Doe",
            phone="+1-555-0100",
        )
        stats = SignalConversationStats(
            contact_id="conv1",
            message_count=50,
            sent_count=25,
            received_count=25,
            first_message=datetime(2024, 1, 1, tzinfo=timezone.utc),
            last_message=datetime(2024, 12, 1, tzinfo=timezone.utc),
        )

        entity = create_signal_source_entity(contact, stats)

        assert entity.source_type == SOURCE_SIGNAL
        assert entity.observed_name == "John Doe"
        assert entity.observed_phone == "+1-555-0100"
        assert "signal:uuid-123" in entity.source_id

    def test_metadata_fields(self):
        """Test that metadata includes all fields."""
        contact = SignalContact(
            id="conv1",
            uuid="uuid-123",
            name="John Doe",
            profile_name="Johnny",
            color="blue",
        )
        stats = SignalConversationStats(
            contact_id="conv1",
            message_count=100,
            sent_count=40,
            received_count=60,
        )

        entity = create_signal_source_entity(contact, stats)

        assert entity.metadata["uuid"] == "uuid-123"
        assert entity.metadata["profile_name"] == "Johnny"
        assert entity.metadata["color"] == "blue"
        assert entity.metadata["message_count"] == 100
        assert entity.metadata["sent_count"] == 40
        assert entity.metadata["received_count"] == 60

    def test_without_stats(self):
        """Test creating entity without stats."""
        contact = SignalContact(
            id="conv1",
            name="John Doe",
        )

        entity = create_signal_source_entity(contact)

        assert entity.metadata["message_count"] == 0

    def test_group_contact(self):
        """Test creating entity from group contact."""
        contact = SignalContact(
            id="group1",
            name="Family Group",
            is_group=True,
            group_members=["m1", "m2", "m3"],
        )

        entity = create_signal_source_entity(contact)

        assert entity.metadata["is_group"] is True
