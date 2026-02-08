"""
Conversation Context - Tracks context across follow-up queries.

This module provides a lightweight context tracker for maintaining
continuity in multi-turn conversations. Used by:
- Chat routes for expanding follow-up queries
- Query router for maintaining person/topic context

Key features:
- Tracks last person mentioned for "what else?" queries
- Remembers which sources were queried for follow-up routing
- Maintains topic context for coherent conversations
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """
    Context accumulated during a conversation for follow-up handling.

    This is NOT persisted - it's computed from conversation history
    when needed for follow-up expansion.
    """
    # Person context
    last_person_name: Optional[str] = None
    last_person_id: Optional[str] = None
    last_person_email: Optional[str] = None

    # Source context
    last_sources: list[str] = field(default_factory=list)
    last_fetch_depth: str = "normal"

    # Topic context
    last_topics: list[str] = field(default_factory=list)

    # Reminder context (for edit/delete follow-ups)
    last_reminder_id: Optional[str] = None
    last_reminder_name: Optional[str] = None

    # Timing
    last_query_time: Optional[datetime] = None

    def has_person_context(self) -> bool:
        """Check if we have person context for follow-up."""
        return bool(self.last_person_name or self.last_person_id)

    def has_reminder_context(self) -> bool:
        """Check if we have reminder context for follow-up."""
        return bool(self.last_reminder_id or self.last_reminder_name)

    def is_stale(self, max_minutes: int = 30) -> bool:
        """Check if context is too old to be relevant."""
        if not self.last_query_time:
            return True
        now = datetime.now(timezone.utc)
        last = self.last_query_time
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_minutes = (now - last).total_seconds() / 60
        return age_minutes > max_minutes

    def to_dict(self) -> dict:
        """Convert to dict for logging/debugging."""
        return {
            "last_person_name": self.last_person_name,
            "last_person_id": self.last_person_id,
            "last_sources": self.last_sources,
            "last_fetch_depth": self.last_fetch_depth,
            "last_topics": self.last_topics,
            "last_reminder_id": self.last_reminder_id,
            "last_reminder_name": self.last_reminder_name,
            "is_stale": self.is_stale(),
        }


def extract_context_from_history(
    messages: list,
    max_lookback: int = 6,
) -> ConversationContext:
    """
    Extract conversation context from message history.

    Scans recent messages to find:
    - Last person mentioned (from routing metadata)
    - Last sources queried
    - Topics discussed

    Args:
        messages: List of Message objects from conversation store
        max_lookback: How many messages to scan (default: 6 = 3 exchanges)

    Returns:
        ConversationContext with extracted information
    """
    context = ConversationContext()

    if not messages:
        return context

    # Look at recent messages (most recent first)
    recent = messages[-max_lookback:] if len(messages) > max_lookback else messages

    for msg in reversed(recent):
        # Extract person context from routing metadata
        if hasattr(msg, 'routing') and msg.routing:
            routing = msg.routing
            # Check for person in routing
            if not context.last_person_name and routing.get("person"):
                context.last_person_name = routing["person"]
                logger.debug(f"Found person context: {context.last_person_name}")

            # Get last sources
            if not context.last_sources and routing.get("sources"):
                context.last_sources = routing["sources"]

            # Get fetch depth
            if routing.get("fetch_depth"):
                context.last_fetch_depth = routing["fetch_depth"]

            # Check for reminder in routing (from reminder creation)
            if not context.last_reminder_id and routing.get("created_reminder"):
                reminder_info = routing["created_reminder"]
                context.last_reminder_id = reminder_info.get("id")
                context.last_reminder_name = reminder_info.get("name")
                logger.debug(f"Found reminder context: {context.last_reminder_name}")

        # Extract person from content (as fallback)
        if not context.last_person_name and hasattr(msg, 'content') and msg.content:
            # Look for "about [Name]" or "with [Name]" patterns
            import re
            patterns = [
                r"about\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
                r"with\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
                r"for\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            ]
            for pattern in patterns:
                match = re.search(pattern, msg.content)
                if match:
                    potential_name = match.group(1)
                    # Skip common non-names
                    if potential_name.lower() not in {"the", "my", "your", "our"}:
                        context.last_person_name = potential_name
                        break

        # Track query timestamp
        if hasattr(msg, 'created_at') and msg.created_at:
            if not context.last_query_time:
                context.last_query_time = msg.created_at

    return context


def expand_followup_with_context(
    query: str,
    context: ConversationContext,
) -> str:
    """
    Expand a follow-up query using conversation context.

    Handles patterns like:
    - "what else?" → "what else about [last_person]?"
    - "tell me more" → "tell me more about [last_person]"
    - "their email?" → "[last_person]'s email?"

    Args:
        query: The user's follow-up query
        context: ConversationContext from previous messages

    Returns:
        Expanded query (or original if no expansion needed)
    """
    if not context.has_person_context() or context.is_stale():
        return query

    query_lower = query.lower().strip()
    person = context.last_person_name

    # Patterns that need person context
    followup_patterns = [
        ("what else", f"what else about {person}"),
        ("tell me more", f"tell me more about {person}"),
        ("anything else", f"anything else about {person}"),
        ("more context", f"more context about {person}"),
        ("their email", f"{person}'s email"),
        ("their phone", f"{person}'s phone"),
        ("their company", f"what company does {person} work at"),
        ("when did we", f"when did I and {person}"),
        ("last time", f"last time I spoke with {person}"),
    ]

    for pattern, replacement in followup_patterns:
        if query_lower.startswith(pattern):
            logger.info(f"Expanded followup: '{query}' -> '{replacement}'")
            return replacement

    # Pronoun resolution
    pronoun_replacements = [
        ("they ", f"{person} "),
        ("them ", f"{person} "),
        ("their ", f"{person}'s "),
        ("he ", f"{person} "),
        ("she ", f"{person} "),
        ("him ", f"{person} "),
        ("her ", f"{person} "),
    ]

    expanded = query
    for pronoun, replacement in pronoun_replacements:
        if pronoun in query_lower:
            # Case-insensitive replacement while preserving case of rest
            import re
            expanded = re.sub(
                re.escape(pronoun),
                replacement,
                expanded,
                flags=re.IGNORECASE,
                count=1  # Only first occurrence
            )
            if expanded != query:
                logger.info(f"Pronoun resolution: '{query}' -> '{expanded}'")
                break

    return expanded
