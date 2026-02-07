"""
Signal chat export parser for LifeOS CRM.

Parses Signal Desktop .json export files to extract contacts and messages.
Creates SourceEntity records for each participant.

Signal Desktop exports messages as JSON with format:
{
    "conversations": [...],
    "messages": [...],
    ...
}
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, BinaryIO

from api.services.source_entity import SourceEntity, SourceEntityStore

logger = logging.getLogger(__name__)

# Source type constant
SOURCE_SIGNAL = "signal"


@dataclass
class SignalContact:
    """Represents a Signal contact from export."""
    id: str
    uuid: Optional[str] = None
    name: Optional[str] = None
    profile_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    color: Optional[str] = None
    is_group: bool = False
    group_members: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """Get display name with fallbacks."""
        if self.name:
            return self.name
        if self.profile_name:
            return self.profile_name
        if self.phone:
            return self.phone
        return self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "uuid": self.uuid,
            "name": self.name,
            "profile_name": self.profile_name,
            "display_name": self.display_name,
            "phone": self.phone,
            "email": self.email,
            "color": self.color,
            "is_group": self.is_group,
            "group_members": self.group_members,
        }


@dataclass
class SignalConversationStats:
    """Statistics for a Signal conversation."""
    contact_id: str
    message_count: int = 0
    first_message: Optional[datetime] = None
    last_message: Optional[datetime] = None
    sent_count: int = 0
    received_count: int = 0

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "message_count": self.message_count,
            "first_message": self.first_message.isoformat() if self.first_message else None,
            "last_message": self.last_message.isoformat() if self.last_message else None,
            "sent_count": self.sent_count,
            "received_count": self.received_count,
        }


@dataclass
class SignalExport:
    """Represents a parsed Signal export."""
    contacts: dict[str, SignalContact] = field(default_factory=dict)
    conversation_stats: dict[str, SignalConversationStats] = field(default_factory=dict)
    total_messages: int = 0
    export_date: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "contacts": {k: v.to_dict() for k, v in self.contacts.items()},
            "conversation_stats": {k: v.to_dict() for k, v in self.conversation_stats.items()},
            "total_messages": self.total_messages,
            "export_date": self.export_date.isoformat() if self.export_date else None,
        }


def _parse_timestamp(ts: int) -> Optional[datetime]:
    """Parse Signal timestamp (milliseconds since epoch)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def parse_signal_export(data: dict) -> SignalExport:
    """
    Parse Signal Desktop export JSON.

    Args:
        data: Parsed JSON data from export file

    Returns:
        SignalExport with parsed data
    """
    export = SignalExport()

    # Parse conversations (contacts)
    for conv in data.get("conversations", []):
        contact_id = conv.get("id") or conv.get("e164") or conv.get("uuid")
        if not contact_id:
            continue

        # Skip system conversations
        if conv.get("type") == "system":
            continue

        is_group = conv.get("type") == "group"

        contact = SignalContact(
            id=contact_id,
            uuid=conv.get("uuid"),
            name=conv.get("name"),
            profile_name=conv.get("profileName"),
            phone=conv.get("e164"),
            email=conv.get("email"),
            color=conv.get("color"),
            is_group=is_group,
            group_members=conv.get("members", []) if is_group else [],
        )

        export.contacts[contact_id] = contact
        export.conversation_stats[contact_id] = SignalConversationStats(
            contact_id=contact_id
        )

    # Parse messages to get stats
    for msg in data.get("messages", []):
        conv_id = msg.get("conversationId")
        if not conv_id or conv_id not in export.conversation_stats:
            continue

        stats = export.conversation_stats[conv_id]
        stats.message_count += 1
        export.total_messages += 1

        # Track sent vs received
        if msg.get("type") == "outgoing":
            stats.sent_count += 1
        else:
            stats.received_count += 1

        # Track timestamps
        timestamp = _parse_timestamp(msg.get("sent_at") or msg.get("received_at"))
        if timestamp:
            if stats.first_message is None or timestamp < stats.first_message:
                stats.first_message = timestamp
            if stats.last_message is None or timestamp > stats.last_message:
                stats.last_message = timestamp

    # Export date from first message of whole export
    all_timestamps = [
        s.first_message for s in export.conversation_stats.values()
        if s.first_message
    ]
    if all_timestamps:
        export.export_date = min(all_timestamps)

    return export


def parse_signal_file(file: BinaryIO) -> SignalExport:
    """
    Parse Signal export from file object.

    Args:
        file: File-like object with JSON export

    Returns:
        SignalExport with parsed data
    """
    content = file.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8")

    data = json.loads(content)
    return parse_signal_export(data)


def create_signal_source_entity(
    contact: SignalContact,
    stats: Optional[SignalConversationStats] = None,
) -> SourceEntity:
    """
    Create a SourceEntity from a Signal contact.

    Args:
        contact: SignalContact object
        stats: Optional conversation statistics

    Returns:
        SourceEntity ready for storage
    """
    return SourceEntity(
        source_type=SOURCE_SIGNAL,
        source_id=f"signal:{contact.uuid or contact.phone or contact.id}",
        observed_name=contact.display_name if contact.display_name != contact.id else None,
        observed_email=contact.email,
        observed_phone=contact.phone,
        metadata={
            "uuid": contact.uuid,
            "profile_name": contact.profile_name,
            "color": contact.color,
            "is_group": contact.is_group,
            "message_count": stats.message_count if stats else 0,
            "sent_count": stats.sent_count if stats else 0,
            "received_count": stats.received_count if stats else 0,
            "first_message": stats.first_message.isoformat() if stats and stats.first_message else None,
            "last_message": stats.last_message.isoformat() if stats and stats.last_message else None,
        },
        observed_at=stats.last_message if stats and stats.last_message else datetime.now(timezone.utc),
    )


def import_signal_export(
    content: str,
    entity_store: SourceEntityStore,
) -> dict:
    """
    Import Signal export to SourceEntity store.

    Args:
        content: Raw JSON export content
        entity_store: SourceEntityStore to save entities

    Returns:
        Import statistics
    """
    stats = {
        "total_messages": 0,
        "contacts": 0,
        "created": 0,
        "updated": 0,
        "skipped_groups": 0,
    }

    data = json.loads(content)
    export = parse_signal_export(data)

    stats["total_messages"] = export.total_messages
    stats["contacts"] = len(export.contacts)

    for contact_id, contact in export.contacts.items():
        # Skip groups for now
        if contact.is_group:
            stats["skipped_groups"] += 1
            continue

        conv_stats = export.conversation_stats.get(contact_id)
        source_entity = create_signal_source_entity(contact, conv_stats)

        # Check if entity already exists
        existing = entity_store.get_by_source(SOURCE_SIGNAL, source_entity.source_id)
        if existing:
            # Update with new data
            if source_entity.observed_name:
                existing.observed_name = source_entity.observed_name
            if source_entity.observed_phone:
                existing.observed_phone = source_entity.observed_phone
            # Merge metadata
            existing.metadata.update(source_entity.metadata)
            entity_store.update(existing)
            stats["updated"] += 1
        else:
            entity_store.add(source_entity)
            stats["created"] += 1

    return stats
