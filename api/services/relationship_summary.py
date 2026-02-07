"""
Relationship Summary - Encapsulates relationship context for routing decisions.

This module provides a computed-on-demand summary of a person's relationship data,
combining channel activity, recency, and relationship strength into a single
coherent view. Used by:
- MCP tools (lifeos_people_search) for agent context
- Query router for smart source selection
- Chat routes for auto-searching active channels

Key design principle: NO new storage. Everything is computed fresh from
existing InteractionStore and PersonEntity data.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Threshold for considering a channel "recently active"
RECENT_ACTIVITY_DAYS = 7


@dataclass
class ChannelActivity:
    """Activity summary for a single communication channel."""
    source_type: str
    count_90d: int  # Interactions in last 90 days
    last_interaction: Optional[datetime]
    is_recent: bool  # Had activity within RECENT_ACTIVITY_DAYS

    @property
    def days_since_last(self) -> Optional[int]:
        """Days since last interaction on this channel, or None if never."""
        if not self.last_interaction:
            return None
        now = datetime.now(timezone.utc)
        last = self.last_interaction
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).days


@dataclass
class RelationshipSummary:
    """
    Complete relationship context for a person.

    Computed on-demand from InteractionStore + PersonEntity data.
    No data is stored - this is a read-only view.
    """
    person_id: str
    person_name: str
    relationship_strength: float  # 0-100 scale

    # Channel breakdown
    channels: list[ChannelActivity] = field(default_factory=list)
    active_channels: list[str] = field(default_factory=list)  # Channels with recent activity
    primary_channel: Optional[str] = None  # Most frequent channel (by 90d count)

    # Quick stats
    total_interactions_90d: int = 0
    last_interaction: Optional[datetime] = None
    days_since_contact: int = 999  # 999 = never contacted

    # Optional extras
    facts_count: int = 0
    has_facts: bool = False

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "person_id": self.person_id,
            "person_name": self.person_name,
            "relationship_strength": self.relationship_strength,
            "channels": [
                {
                    "source_type": ch.source_type,
                    "count_90d": ch.count_90d,
                    "last_interaction": ch.last_interaction.isoformat() if ch.last_interaction else None,
                    "is_recent": ch.is_recent,
                    "days_since_last": ch.days_since_last,
                }
                for ch in self.channels
            ],
            "active_channels": self.active_channels,
            "primary_channel": self.primary_channel,
            "total_interactions_90d": self.total_interactions_90d,
            "last_interaction": self.last_interaction.isoformat() if self.last_interaction else None,
            "days_since_contact": self.days_since_contact,
            "facts_count": self.facts_count,
            "has_facts": self.has_facts,
        }


def get_relationship_summary(person_id: str) -> Optional[RelationshipSummary]:
    """
    Build complete relationship context for routing decisions.

    Fetches data from InteractionStore and PersonEntity stores to build
    a comprehensive view of the relationship. Includes:
    - Which channels are used
    - Which channels are recently active (last 7 days)
    - Primary communication channel
    - Overall relationship strength

    Args:
        person_id: PersonEntity ID

    Returns:
        RelationshipSummary or None if person not found
    """
    from api.services.person_entity import get_person_entity_store
    from api.services.interaction_store import get_interaction_store
    from api.services.person_facts import get_person_fact_store

    person_store = get_person_entity_store()
    interaction_store = get_interaction_store()

    # Get person entity
    person = person_store.get_by_id(person_id)
    if not person:
        logger.debug(f"Person not found: {person_id}")
        return None

    # Get channel-specific data
    counts_90d = interaction_store.get_interaction_counts(person_id, days_back=90)
    recency_by_source = interaction_store.get_last_interaction_by_source(person_id)

    now = datetime.now(timezone.utc)
    channels = []
    active = []

    # Build channel activity list
    # Combine keys from both counts and recency (in case one has data the other doesn't)
    all_sources = set(counts_90d.keys()) | set(recency_by_source.keys())

    for source_type in all_sources:
        count = counts_90d.get(source_type, 0)
        last = recency_by_source.get(source_type)

        # Determine if this channel has recent activity
        is_recent = False
        if last:
            last_aware = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            days_ago = (now - last_aware).days
            is_recent = days_ago <= RECENT_ACTIVITY_DAYS

        channels.append(ChannelActivity(
            source_type=source_type,
            count_90d=count,
            last_interaction=last,
            is_recent=is_recent,
        ))

        if is_recent:
            active.append(source_type)

    # Sort channels by 90-day count (descending) to find primary
    channels.sort(key=lambda c: c.count_90d, reverse=True)
    primary = channels[0].source_type if channels else None

    # Calculate days since contact
    days_since = 999
    if person.last_seen:
        last_seen = person.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        days_since = (now - last_seen).days

    # Get facts count (optional, doesn't fail if store unavailable)
    facts_count = 0
    try:
        fact_store = get_person_fact_store()
        facts = fact_store.get_for_person(person_id)
        facts_count = len(facts)
    except Exception as e:
        logger.debug(f"Could not get facts for {person_id}: {e}")

    return RelationshipSummary(
        person_id=person_id,
        person_name=person.display_name or person.canonical_name,
        relationship_strength=person.relationship_strength,
        channels=channels,
        active_channels=active,
        primary_channel=primary,
        total_interactions_90d=sum(counts_90d.values()),
        last_interaction=person.last_seen,
        days_since_contact=days_since,
        facts_count=facts_count,
        has_facts=facts_count > 0,
    )


def format_relationship_context(summary: RelationshipSummary) -> str:
    """
    Format relationship summary as human-readable context for prompts.

    Used by query router and chat routes to inject relationship context
    into LLM prompts.

    Args:
        summary: RelationshipSummary to format

    Returns:
        Formatted markdown string
    """
    lines = [
        f"## Relationship Context: {summary.person_name}",
        f"- **Strength**: {summary.relationship_strength}/100",
        f"- **Days since contact**: {summary.days_since_contact}",
    ]

    if summary.active_channels:
        lines.append(f"- **Active channels** (last 7 days): {', '.join(summary.active_channels)}")
    else:
        lines.append("- **Active channels**: None recently")

    if summary.primary_channel:
        lines.append(f"- **Primary channel**: {summary.primary_channel}")

    if summary.channels:
        lines.append("\n### Channel Activity (90 days)")
        for ch in summary.channels:
            status = "active" if ch.is_recent else "dormant"
            lines.append(f"- {ch.source_type}: {ch.count_90d} interactions ({status})")

    if summary.has_facts:
        lines.append(f"\n_({summary.facts_count} extracted facts available)_")

    return "\n".join(lines)
