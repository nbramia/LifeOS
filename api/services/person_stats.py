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
from datetime import datetime, timezone
from pathlib import Path
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
        # Full refresh — read all per-(person_id, source) counts, then collapse
        # legacy (pre-merge) person_ids onto their canonical entity.id BEFORE
        # applying. Without this, the loop processes the same canonical entity
        # multiple times: get_by_id() follows the merge map and returns the
        # canonical, then _apply_counts_to_entity / _update_timestamps overwrite
        # with the *legacy* ID's (much smaller, much older) data — silently
        # corrupting both stats and last_seen on every canonical with merged
        # IDs that still carry stale interactions in the table.
        cursor = conn.execute("""
            SELECT person_id, source_type, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id, source_type
        """)

        canonical_counts: dict[str, dict[str, int]] = {}
        # Map canonical_id -> set of raw person_ids that resolve to it
        # (used by _update_timestamps to query MAX across all variants).
        canonical_ids: dict[str, set[str]] = {}

        for row in cursor:
            pid, source_type, count = row
            entity = store.get_by_id(pid)
            if not entity:
                continue
            canon = entity.id
            bucket = canonical_counts.setdefault(canon, {})
            bucket[source_type] = bucket.get(source_type, 0) + count
            canonical_ids.setdefault(canon, set()).add(pid)
            stats['total_interactions'] += count

        # Update each canonical entity exactly once, with aggregated counts.
        for canon, counts in canonical_counts.items():
            entity = store.get_by_id(canon)
            if entity:
                _apply_counts_to_entity(entity, counts)
                _update_timestamps(entity, list(canonical_ids[canon]), conn)
                store.update(entity)
                stats['updated'] += 1

        # Zero out people with no interactions (they may have had interactions
        # deleted). They may still have source_entity timestamps worth updating.
        for entity in store.get_all():
            if entity.id not in canonical_counts:
                modified = False
                if entity.email_count or entity.meeting_count or entity.message_count or entity.mention_count or entity.photo_count:
                    entity.email_count = 0
                    entity.meeting_count = 0
                    entity.message_count = 0
                    entity.mention_count = 0
                    entity.photo_count = 0
                    modified = True
                if _update_timestamps(entity, [entity.id], conn):
                    modified = True
                if modified:
                    store.update(entity)
                    stats['updated'] += 1

    else:
        # Targeted refresh — for each input, resolve to canonical and expand
        # to the canonical + every known legacy ID that merges into it. This
        # makes the refresh correct regardless of whether callers pass the
        # canonical or a legacy ID, and regardless of whether interactions
        # have been repointed yet.
        canon_to_variants: dict[str, set[str]] = {}
        for pid in person_ids:
            entity = store.get_by_id(pid)
            if not entity:
                continue
            variants = {entity.id, pid} | store.get_legacy_ids(entity.id)
            canon_to_variants.setdefault(entity.id, set()).update(variants)

        for canon, variants in canon_to_variants.items():
            placeholders = ','.join(['?'] * len(variants))
            cursor = conn.execute(f"""
                SELECT source_type, COUNT(*) as cnt
                FROM interactions
                WHERE person_id IN ({placeholders})
                GROUP BY source_type
            """, tuple(variants))
            counts = {row[0]: row[1] for row in cursor}
            stats['total_interactions'] += sum(counts.values())

            entity = store.get_by_id(canon)
            if entity:
                _apply_counts_to_entity(entity, counts)
                _update_timestamps(entity, list(variants), conn)
                store.update(entity)
                stats['updated'] += 1

    conn.close()

    if save:
        store.save()  # Uses file locking

    if stats['updated'] > 0:
        logger.info(f"Refreshed stats for {stats['updated']} people ({stats['total_interactions']} interactions)")

    return stats


def _update_timestamps(entity, person_ids, int_conn) -> bool:
    """
    Update first_seen/last_seen from interactions and source_entities.

    Args:
        entity: PersonEntity to update.
        person_ids: Either a single person_id (str) or a list of person_ids
            covering the canonical entity plus any legacy IDs that merge to it.
            Querying across all of them keeps stats correct when stale
            interactions still carry legacy IDs (repoint_stale_ids hasn't
            caught up).
        int_conn: Open connection to interactions.db.

    Returns True if entity was modified.
    """
    if isinstance(person_ids, str):
        person_ids = [person_ids]
    if not person_ids:
        return False
    placeholders = ','.join(['?'] * len(person_ids))

    # Get timestamp range from interactions across ALL person_id variants
    cursor = int_conn.execute(f"""
        SELECT MIN(timestamp), MAX(timestamp)
        FROM interactions
        WHERE person_id IN ({placeholders})
    """, tuple(person_ids))
    row = cursor.fetchone()
    interaction_first = None
    interaction_last = None
    if row and row[0]:
        interaction_first = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
    if row and row[1]:
        interaction_last = datetime.fromisoformat(row[1]).replace(tzinfo=timezone.utc)

    # Get timestamp range from source_entities across the same variants
    # (source_entities.canonical_person_id may also still carry legacy IDs
    # if a merge happened without backfilling that column).
    crm_db = Path("data/crm.db")
    source_first = None
    source_last = None
    if crm_db.exists():
        crm_conn = sqlite3.connect(str(crm_db))
        crm_cursor = crm_conn.execute(f"""
            SELECT MIN(observed_at), MAX(observed_at)
            FROM source_entities
            WHERE canonical_person_id IN ({placeholders})
        """, tuple(person_ids))
        se_row = crm_cursor.fetchone()
        crm_conn.close()
        if se_row and se_row[0]:
            source_first = datetime.fromisoformat(se_row[0]).replace(tzinfo=timezone.utc)
        if se_row and se_row[1]:
            source_last = datetime.fromisoformat(se_row[1]).replace(tzinfo=timezone.utc)

    # Compute first_seen = earliest across both sources
    if interaction_first and source_first:
        new_first = min(interaction_first, source_first)
    else:
        new_first = interaction_first or source_first

    # Compute last_seen = latest across both sources, capped at now
    now = datetime.now(timezone.utc)
    if interaction_last and source_last:
        new_last = min(max(interaction_last, source_last), now)
    elif interaction_last:
        new_last = min(interaction_last, now)
    elif source_last:
        new_last = min(source_last, now)
    else:
        new_last = None

    modified = False
    if new_first and entity.first_seen != new_first:
        entity.first_seen = new_first
        modified = True
    if new_last and entity.last_seen != new_last:
        entity.last_seen = new_last
        modified = True

    return modified


def _apply_counts_to_entity(entity, counts: dict[str, int]) -> None:
    """
    Apply interaction counts to a PersonEntity.

    Maps source_type to the appropriate count field:
    - gmail -> email_count
    - calendar -> meeting_count
    - vault, granola -> mention_count
    - imessage, whatsapp, phone, slack -> message_count
    - photos -> photo_count

    Note: slack_message_count is managed separately by slack_sync.py
    (raw message counts, not daily interaction summaries).
    """
    entity.email_count = counts.get('gmail', 0)
    entity.meeting_count = counts.get('calendar', 0)
    entity.mention_count = counts.get('vault', 0) + counts.get('granola', 0)
    entity.message_count = (
        counts.get('imessage', 0) +
        counts.get('whatsapp', 0) +
        counts.get('phone', 0) +
        counts.get('slack', 0)
    )
    entity.photo_count = counts.get('photos', 0)

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

    # Group raw interaction person_ids by their canonical entity, so the
    # comparison mirrors what refresh_person_stats writes. Without this,
    # canonicals with merged legacy IDs would appear as "discrepancies" and
    # fix=True would overwrite correct aggregated counts with canonical-only
    # under-counts.
    raw_ids: set[str] = set()
    cursor = conn.execute("SELECT DISTINCT person_id FROM interactions")
    for (pid,) in cursor:
        raw_ids.add(pid)
    canonical_to_raws: dict[str, set[str]] = {}
    for pid in raw_ids:
        entity = store.get_by_id(pid)
        if entity:
            canonical_to_raws.setdefault(entity.id, set()).add(pid)

    discrepancies = {}

    for entity in store.get_all():
        variant_ids = list(canonical_to_raws.get(entity.id, {entity.id}))
        placeholders = ','.join(['?'] * len(variant_ids))
        cursor = conn.execute(f"""
            SELECT source_type, COUNT(*) as cnt
            FROM interactions
            WHERE person_id IN ({placeholders})
            GROUP BY source_type
        """, tuple(variant_ids))

        counts = {row[0]: row[1] for row in cursor}

        computed_email = counts.get('gmail', 0)
        computed_meeting = counts.get('calendar', 0)
        computed_mention = counts.get('vault', 0) + counts.get('granola', 0)
        computed_message = (
            counts.get('imessage', 0) +
            counts.get('whatsapp', 0) +
            counts.get('phone', 0) +
            counts.get('slack', 0)
        )
        computed_photo = counts.get('photos', 0)

        if (entity.email_count != computed_email or
            entity.meeting_count != computed_meeting or
            entity.mention_count != computed_mention or
            entity.message_count != computed_message or
            entity.photo_count != computed_photo):

            discrepancies[entity.id] = {
                'name': entity.canonical_name,
                'cached': {
                    'email': entity.email_count,
                    'meeting': entity.meeting_count,
                    'mention': entity.mention_count,
                    'message': entity.message_count,
                    'photo': entity.photo_count,
                },
                'computed': {
                    'email': computed_email,
                    'meeting': computed_meeting,
                    'mention': computed_mention,
                    'message': computed_message,
                    'photo': computed_photo,
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
