"""
WhatsApp chat export parser for LifeOS CRM.

Parses WhatsApp .txt export files to extract contacts and messages.
Creates SourceEntity records for each participant.

Export format (Android/iOS may vary slightly):
[date, time] sender: message
or
date, time - sender: message
"""
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, BinaryIO
from collections import defaultdict

from api.services.source_entity import SourceEntity, SourceEntityStore

logger = logging.getLogger(__name__)

# Source type constant
SOURCE_WHATSAPP = "whatsapp"

# Common WhatsApp message patterns
# Pattern 1: [DD/MM/YYYY, HH:MM:SS] Sender: Message
PATTERN_BRACKETS = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\]\s*([^:]+):\s*(.*)$",
    re.IGNORECASE,
)

# Pattern 2: DD/MM/YYYY, HH:MM - Sender: Message
PATTERN_DASH = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\s*-\s*([^:]+):\s*(.*)$",
    re.IGNORECASE,
)

# Pattern 3: MM/DD/YY, HH:MM AM/PM - Sender: Message (US format)
PATTERN_US = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\d{1,2}:\d{2}(?:\s*[AP]M)?)\s*-\s*([^:]+):\s*(.*)$",
    re.IGNORECASE,
)

# System messages to skip
SYSTEM_PATTERNS = [
    re.compile(r"^Messages and calls are end-to-end encrypted", re.IGNORECASE),
    re.compile(r"^You created group", re.IGNORECASE),
    re.compile(r"^.* added .*", re.IGNORECASE),
    re.compile(r"^.* left$", re.IGNORECASE),
    re.compile(r"^.* removed .*", re.IGNORECASE),
    re.compile(r"^.* changed the subject", re.IGNORECASE),
    re.compile(r"^.* changed this group's icon", re.IGNORECASE),
    re.compile(r"^.* changed the group description", re.IGNORECASE),
    re.compile(r"^\<Media omitted\>$", re.IGNORECASE),
    re.compile(r"^This message was deleted$", re.IGNORECASE),
    re.compile(r"^Missed voice call$", re.IGNORECASE),
    re.compile(r"^Missed video call$", re.IGNORECASE),
]


@dataclass
class WhatsAppMessage:
    """Represents a parsed WhatsApp message."""
    timestamp: datetime
    sender: str
    text: str
    is_system: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "sender": self.sender,
            "text": self.text,
            "is_system": self.is_system,
        }


@dataclass
class WhatsAppParticipant:
    """Aggregated data for a chat participant."""
    name: str
    message_count: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    phone: Optional[str] = None  # If sender is phone number

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "message_count": self.message_count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "phone": self.phone,
        }


@dataclass
class WhatsAppChatExport:
    """Represents a parsed WhatsApp chat export."""
    filename: str
    participants: dict[str, WhatsAppParticipant] = field(default_factory=dict)
    message_count: int = 0
    first_message: Optional[datetime] = None
    last_message: Optional[datetime] = None
    is_group: bool = False

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "participants": {k: v.to_dict() for k, v in self.participants.items()},
            "message_count": self.message_count,
            "first_message": self.first_message.isoformat() if self.first_message else None,
            "last_message": self.last_message.isoformat() if self.last_message else None,
            "is_group": self.is_group,
        }


def _is_phone_number(text: str) -> bool:
    """Check if text looks like a phone number."""
    # Remove common phone number characters
    cleaned = re.sub(r"[\s\-\(\)\+]", "", text)
    return cleaned.isdigit() and len(cleaned) >= 7


def _parse_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse date and time strings to datetime."""
    # Normalize separators
    date_str = date_str.replace("-", "/")
    time_str = time_str.strip()

    # Common date formats
    date_formats = [
        "%d/%m/%Y",
        "%d/%m/%y",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
    ]

    # Common time formats
    time_formats = [
        "%H:%M:%S",
        "%H:%M",
        "%I:%M:%S %p",
        "%I:%M %p",
        "%I:%M%p",
    ]

    for df in date_formats:
        for tf in time_formats:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", f"{df} {tf}")
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    logger.debug(f"Could not parse datetime: {date_str} {time_str}")
    return None


def _is_system_message(text: str) -> bool:
    """Check if message is a system message."""
    for pattern in SYSTEM_PATTERNS:
        if pattern.match(text):
            return True
    return False


def parse_whatsapp_export(
    content: str,
    filename: str = "chat.txt",
) -> WhatsAppChatExport:
    """
    Parse WhatsApp chat export file content.

    Args:
        content: Raw text content of the export file
        filename: Original filename for reference

    Returns:
        WhatsAppChatExport with parsed data
    """
    export = WhatsAppChatExport(filename=filename)
    participants: dict[str, WhatsAppParticipant] = {}
    current_message = None

    lines = content.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Try each pattern
        match = None
        for pattern in [PATTERN_BRACKETS, PATTERN_DASH, PATTERN_US]:
            match = pattern.match(line)
            if match:
                break

        if match:
            date_str, time_str, sender, text = match.groups()
            sender = sender.strip()
            text = text.strip()

            timestamp = _parse_datetime(date_str, time_str)
            if not timestamp:
                continue

            is_system = _is_system_message(text)

            if not is_system:
                # Track participant
                if sender not in participants:
                    phone = sender if _is_phone_number(sender) else None
                    participants[sender] = WhatsAppParticipant(
                        name=sender,
                        phone=phone,
                        first_seen=timestamp,
                    )

                participants[sender].message_count += 1
                participants[sender].last_seen = timestamp

                # Track overall chat stats
                export.message_count += 1
                if export.first_message is None or timestamp < export.first_message:
                    export.first_message = timestamp
                if export.last_message is None or timestamp > export.last_message:
                    export.last_message = timestamp

        else:
            # Continuation of previous message (multiline)
            # In WhatsApp exports, lines without timestamp are continuation
            pass

    export.participants = participants
    export.is_group = len(participants) > 2

    return export


def parse_whatsapp_file(
    file: BinaryIO,
    filename: str = "chat.txt",
) -> WhatsAppChatExport:
    """
    Parse WhatsApp export from file object.

    Args:
        file: File-like object with chat export
        filename: Original filename

    Returns:
        WhatsAppChatExport with parsed data
    """
    content = file.read()
    if isinstance(content, bytes):
        # Try UTF-8 first, fall back to latin-1
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            content = content.decode("latin-1")

    return parse_whatsapp_export(content, filename)


def create_whatsapp_source_entity(
    participant: WhatsAppParticipant,
    chat_name: Optional[str] = None,
) -> SourceEntity:
    """
    Create a SourceEntity from a WhatsApp participant.

    Args:
        participant: WhatsAppParticipant object
        chat_name: Name of the chat (for context)

    Returns:
        SourceEntity ready for storage
    """
    source_id = participant.phone or participant.name

    return SourceEntity(
        source_type=SOURCE_WHATSAPP,
        source_id=f"whatsapp:{source_id}",
        observed_name=participant.name if not _is_phone_number(participant.name) else None,
        observed_email=None,
        observed_phone=participant.phone,
        metadata={
            "message_count": participant.message_count,
            "first_seen": participant.first_seen.isoformat() if participant.first_seen else None,
            "last_seen": participant.last_seen.isoformat() if participant.last_seen else None,
            "chat_name": chat_name,
        },
        observed_at=participant.last_seen or datetime.now(timezone.utc),
    )


def import_whatsapp_export(
    content: str,
    filename: str,
    entity_store: SourceEntityStore,
) -> dict:
    """
    Import WhatsApp export to SourceEntity store.

    Args:
        content: Raw export file content
        filename: Original filename
        entity_store: SourceEntityStore to save entities

    Returns:
        Import statistics
    """
    stats = {
        "filename": filename,
        "total_messages": 0,
        "participants": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
    }

    export = parse_whatsapp_export(content, filename)
    stats["total_messages"] = export.message_count
    stats["participants"] = len(export.participants)
    stats["is_group"] = export.is_group

    # Derive chat name from filename
    chat_name = filename.replace("WhatsApp Chat with ", "").replace(".txt", "")

    for participant in export.participants.values():
        # Skip if participant is just a phone number with few messages
        if participant.phone and participant.message_count < 2:
            stats["skipped"] += 1
            continue

        source_entity = create_whatsapp_source_entity(participant, chat_name)

        # Check if entity already exists
        existing = entity_store.get_by_source(SOURCE_WHATSAPP, source_entity.source_id)
        if existing:
            # Update with new data
            if source_entity.observed_name:
                existing.observed_name = source_entity.observed_name
            if source_entity.observed_phone:
                existing.observed_phone = source_entity.observed_phone
            # Merge metadata
            existing.metadata["message_count"] = (
                existing.metadata.get("message_count", 0) + participant.message_count
            )
            if participant.last_seen:
                if not existing.metadata.get("last_seen") or participant.last_seen.isoformat() > existing.metadata["last_seen"]:
                    existing.metadata["last_seen"] = participant.last_seen.isoformat()
                    existing.observed_at = participant.last_seen
            entity_store.update(existing)
            stats["updated"] += 1
        else:
            entity_store.add(source_entity)
            stats["created"] += 1

    return stats
