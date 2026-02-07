"""Tests for WhatsApp chat export parser."""
import pytest
from datetime import datetime, timezone

from api.services.whatsapp_import import (
    parse_whatsapp_export,
    WhatsAppParticipant,
    create_whatsapp_source_entity,
    SOURCE_WHATSAPP,
    _is_phone_number,
    _parse_datetime,
)


class TestParseWhatsAppExport:
    """Tests for parse_whatsapp_export function."""

    def test_parse_bracket_format(self):
        """Test parsing bracket format messages."""
        content = """[12/1/2024, 10:30:15 AM] John Doe: Hello there!
[12/1/2024, 10:31:00 AM] Jane Smith: Hi John!
[12/1/2024, 10:32:00 AM] John Doe: How are you?"""

        export = parse_whatsapp_export(content, "test.txt")

        assert export.message_count == 3
        assert len(export.participants) == 2
        assert "John Doe" in export.participants
        assert "Jane Smith" in export.participants
        assert export.participants["John Doe"].message_count == 2
        assert export.participants["Jane Smith"].message_count == 1

    def test_parse_dash_format(self):
        """Test parsing dash format messages."""
        content = """01/12/24, 10:30 - Alice: First message
01/12/24, 10:31 - Bob: Reply to first
01/12/24, 10:32 - Alice: Another message"""

        export = parse_whatsapp_export(content, "test.txt")

        assert export.message_count == 3
        assert len(export.participants) == 2

    def test_skip_system_messages(self):
        """Test that system messages are skipped."""
        content = """[12/1/2024, 10:30:00 AM] Messages and calls are end-to-end encrypted
[12/1/2024, 10:30:15 AM] John: Hello!
[12/1/2024, 10:31:00 AM] Jane added Bob
[12/1/2024, 10:32:00 AM] Jane: Hi everyone!"""

        export = parse_whatsapp_export(content, "test.txt")

        # Only 2 real messages
        assert export.message_count == 2

    def test_phone_number_sender(self):
        """Test handling phone number as sender."""
        content = """[12/1/2024, 10:30:15 AM] +1 555-123-4567: Hello!
[12/1/2024, 10:31:00 AM] +1 555-123-4567: Another message"""

        export = parse_whatsapp_export(content, "test.txt")

        assert len(export.participants) == 1
        participant = list(export.participants.values())[0]
        assert participant.phone == "+1 555-123-4567"

    def test_is_group_detection(self):
        """Test group chat detection."""
        content = """[12/1/2024, 10:30:15 AM] Alice: Hi!
[12/1/2024, 10:31:00 AM] Bob: Hello!
[12/1/2024, 10:32:00 AM] Charlie: Hey!"""

        export = parse_whatsapp_export(content, "test.txt")

        assert export.is_group is True  # More than 2 participants

    def test_not_group(self):
        """Test 1:1 chat detection."""
        content = """[12/1/2024, 10:30:15 AM] Alice: Hi!
[12/1/2024, 10:31:00 AM] Bob: Hello!"""

        export = parse_whatsapp_export(content, "test.txt")

        assert export.is_group is False

    def test_timestamp_tracking(self):
        """Test first/last message timestamps."""
        content = """[12/1/2024, 10:30:00 AM] Alice: First
[12/2/2024, 11:00:00 AM] Alice: Middle
[12/3/2024, 12:00:00 PM] Alice: Last"""

        export = parse_whatsapp_export(content, "test.txt")

        assert export.first_message is not None
        assert export.last_message is not None
        assert export.first_message < export.last_message

    def test_empty_content(self):
        """Test handling empty content."""
        export = parse_whatsapp_export("", "test.txt")

        assert export.message_count == 0
        assert len(export.participants) == 0


class TestIsPhoneNumber:
    """Tests for _is_phone_number helper."""

    def test_valid_phone_numbers(self):
        """Test valid phone number detection."""
        assert _is_phone_number("+1 555-123-4567") is True
        assert _is_phone_number("555-123-4567") is True
        assert _is_phone_number("+44 20 7946 0958") is True
        assert _is_phone_number("(555) 123-4567") is True

    def test_invalid_phone_numbers(self):
        """Test non-phone number detection."""
        assert _is_phone_number("John Doe") is False
        assert _is_phone_number("alice@example.com") is False
        assert _is_phone_number("12345") is False  # Too short


class TestParseDateTime:
    """Tests for _parse_datetime helper."""

    def test_various_formats(self):
        """Test parsing various datetime formats."""
        # DD/MM/YYYY format
        dt = _parse_datetime("25/12/2024", "10:30")
        assert dt is not None
        assert dt.day == 25
        assert dt.month == 12

        # With AM/PM
        dt = _parse_datetime("12/25/24", "10:30 AM")
        assert dt is not None

    def test_invalid_format(self):
        """Test handling invalid formats."""
        dt = _parse_datetime("not-a-date", "not-a-time")
        assert dt is None


class TestCreateWhatsAppSourceEntity:
    """Tests for create_whatsapp_source_entity function."""

    def test_basic_participant(self):
        """Test creating entity from basic participant."""
        participant = WhatsAppParticipant(
            name="John Doe",
            message_count=50,
            first_seen=datetime(2024, 1, 1, tzinfo=timezone.utc),
            last_seen=datetime(2024, 12, 1, tzinfo=timezone.utc),
        )

        entity = create_whatsapp_source_entity(participant, "Test Chat")

        assert entity.source_type == SOURCE_WHATSAPP
        assert entity.observed_name == "John Doe"
        assert entity.metadata["message_count"] == 50
        assert entity.metadata["chat_name"] == "Test Chat"

    def test_phone_number_participant(self):
        """Test creating entity from phone number participant."""
        participant = WhatsAppParticipant(
            name="+1-555-0100",
            phone="+1-555-0100",
            message_count=10,
        )

        entity = create_whatsapp_source_entity(participant)

        assert entity.observed_phone == "+1-555-0100"
        assert entity.observed_name is None  # Phone number not used as name
        assert "whatsapp:+1-555-0100" in entity.source_id

    def test_metadata_fields(self):
        """Test that metadata includes all fields."""
        first_seen = datetime(2024, 1, 1, tzinfo=timezone.utc)
        last_seen = datetime(2024, 12, 1, tzinfo=timezone.utc)

        participant = WhatsAppParticipant(
            name="Alice",
            message_count=100,
            first_seen=first_seen,
            last_seen=last_seen,
        )

        entity = create_whatsapp_source_entity(participant)

        assert entity.metadata["first_seen"] == first_seen.isoformat()
        assert entity.metadata["last_seen"] == last_seen.isoformat()
