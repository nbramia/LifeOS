"""
Person stats refresh - keeps PersonEntity counts in sync with InteractionStore.

This module provides the ONLY correct way to update PersonEntity counts.
All sync scripts MUST call refresh_person_stats() after modifying interactions.

Usage:
    from api.services.person_stats import refresh_person_stats

    # At end of sync script:
    affected_person_ids = {'uuid1', 'uuid2', ...}
    refresh_person_stats(list(affected_person_ids))
"""
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def refresh_person_stats(person_ids: Optional[list[str]] = None, save: bool = True) -> dict:
    """
    Recompute PersonEntity counts from InteractionStore.

    This is the ONLY correct way to update PersonEntity counts. It queries
    the source of truth (InteractionStore) and updates the cached counts.

    Args:
        person_ids: Specific people to refresh. If None, refreshes ALL people.
        save: Whether to persist changes to disk. Set False for batch operations.

    Returns:
        Dict with stats: {updated: int, total_interactions: int}
    """
    from api.services.person_entity import get_person_entity_store
    from api.services.interaction_store import get_interaction_db_path

    store = get_person_entity_store()
    conn = sqlite3.connect(get_interaction_db_path())

    stats = {'updated': 0, 'total_interactions': 0}

    if person_ids is None:
        # Full refresh - get counts for all people in one query
        cursor = conn.execute("""
            SELECT person_id, source_type, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id, source_type
        """)

        # Build per-person counts
        person_counts: dict[str, dict[str, int]] = {}
        for row in cursor:
            pid, source_type, count = row
            if pid not in person_counts:
                person_counts[pid] = {}
            person_counts[pid][source_type] = count
            stats['total_interactions'] += count

        # Update people with interactions
        for person_id, counts in person_counts.items():
            entity = store.get_by_id(person_id)
            if entity:
                _apply_counts_to_entity(entity, counts)
                store.update(entity)
                stats['updated'] += 1

        # Zero out people with no interactions (they may have had interactions deleted)
        for entity in store.get_all():
            if entity.id not in person_counts:
                if entity.email_count or entity.meeting_count or entity.message_count or entity.mention_count:
                    entity.email_count = 0
                    entity.meeting_count = 0
                    entity.message_count = 0
                    entity.mention_count = 0
                    store.update(entity)
                    stats['updated'] += 1

    else:
        # Targeted refresh - only specified people
        for person_id in person_ids:
            cursor = conn.execute("""
                SELECT source_type, COUNT(*) as cnt
                FROM interactions
                WHERE person_id = ?
                GROUP BY source_type
            """, (person_id,))

            counts = {row[0]: row[1] for row in cursor}
            stats['total_interactions'] += sum(counts.values())

            entity = store.get_by_id(person_id)
            if entity:
                _apply_counts_to_entity(entity, counts)
                store.update(entity)
                stats['updated'] += 1

    conn.close()

    if save:
        store.save()  # Uses file locking

    if stats['updated'] > 0:
        logger.info(f"Refreshed stats for {stats['updated']} people ({stats['total_interactions']} interactions)")

    return stats


def _apply_counts_to_entity(entity, counts: dict[str, int]) -> None:
    """
    Apply interaction counts to a PersonEntity.

    Maps source_type to the appropriate count field:
    - gmail -> email_count
    - calendar -> meeting_count
    - vault, granola -> mention_count
    - imessage, whatsapp, sms, slack -> message_count
    """
    entity.email_count = counts.get('gmail', 0)
    entity.meeting_count = counts.get('calendar', 0)
    entity.mention_count = counts.get('vault', 0) + counts.get('granola', 0)
    entity.message_count = (
        counts.get('imessage', 0) +
        counts.get('whatsapp', 0) +
        counts.get('sms', 0) +
        counts.get('slack', 0)
    )

    # Update sources list to include any source types with interactions
    interaction_sources = set(counts.keys())
    existing_sources = set(entity.sources or [])
    entity.sources = list(existing_sources | interaction_sources)


def verify_person_stats(fix: bool = False) -> dict:
    """
    Verify PersonEntity counts match InteractionStore.

    Used as a safety net to catch any discrepancies that slipped through.

    Args:
        fix: If True, fix any discrepancies found.

    Returns:
        Dict mapping person_id to discrepancy details. Empty dict if all consistent.
    """
    from api.services.person_entity import get_person_entity_store
    from api.services.interaction_store import get_interaction_db_path

    store = get_person_entity_store()
    conn = sqlite3.connect(get_interaction_db_path())

    discrepancies = {}

    for entity in store.get_all():
        cursor = conn.execute("""
            SELECT source_type, COUNT(*) as cnt
            FROM interactions
            WHERE person_id = ?
            GROUP BY source_type
        """, (entity.id,))

        counts = {row[0]: row[1] for row in cursor}

        computed_email = counts.get('gmail', 0)
        computed_meeting = counts.get('calendar', 0)
        computed_mention = counts.get('vault', 0) + counts.get('granola', 0)
        computed_message = (
            counts.get('imessage', 0) +
            counts.get('whatsapp', 0) +
            counts.get('sms', 0) +
            counts.get('slack', 0)
        )

        if (entity.email_count != computed_email or
            entity.meeting_count != computed_meeting or
            entity.mention_count != computed_mention or
            entity.message_count != computed_message):

            discrepancies[entity.id] = {
                'name': entity.canonical_name,
                'cached': {
                    'email': entity.email_count,
                    'meeting': entity.meeting_count,
                    'mention': entity.mention_count,
                    'message': entity.message_count,
                },
                'computed': {
                    'email': computed_email,
                    'meeting': computed_meeting,
                    'mention': computed_mention,
                    'message': computed_message,
                },
            }

            if fix:
                _apply_counts_to_entity(entity, counts)
                store.update(entity)

    conn.close()

    if fix and discrepancies:
        store.save()
        logger.info(f"Fixed {len(discrepancies)} discrepancies")

    return discrepancies
