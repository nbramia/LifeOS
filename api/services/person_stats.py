"""
Person stats refresh - keeps PersonEntity counts in sync with InteractionStore.

This module provides the ONLY correct way to update PersonEntity counts.
All sync scripts MUST call refresh_person_stats() after modifying interactions.

Usage:
    from api.services.person_stats import refresh_person_stats

    # At end of sync script:
    affected_person_ids = {'uuid1', 'uuid2', ...}
    refresh_person_stats(list(affected_person_ids))

last_seen has one definition and one writer: sync_last_seen() below stores the
most recent real interaction with each person, and nothing else writes the
column. Refreshing stats runs it, and consistency verification checks it nightly.
"""
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from api.services.person_entity import PersonEntityStore

logger = logging.getLogger(__name__)


def refresh_person_stats(person_ids: Optional[list[str]] = None, save: bool = True) -> dict:
    """
    Recompute PersonEntity counts and last_seen from InteractionStore.

    This is the ONLY correct way to update PersonEntity counts. It queries
    the source of truth (InteractionStore) and updates the cached counts.

    Args:
        person_ids: Specific people to refresh. If None, refreshes ALL people.
        save: Whether to persist changes to disk. Set False for batch operations.

    Returns:
        Dict with stats: {updated: int, total_interactions: int,
        last_seen_changed: int}
    """
    from api.services.person_entity import get_person_entity_store
    from api.services.interaction_store import get_interaction_db_path

    store = get_person_entity_store()
    conn = sqlite3.connect(get_interaction_db_path())

    stats = {'updated': 0, 'total_interactions': 0}

    if person_ids is None:
        # Full refresh — read all per-(person_id, source) counts, then collapse
        # legacy (pre-merge) person_ids onto their canonical entity.id BEFORE
        # applying, so each canonical entity is written exactly once with the
        # totals across every id that resolves to it. get_by_id() follows the
        # merge map, so applying per raw id would overwrite a canonical's totals
        # with whichever legacy id came last.
        cursor = conn.execute("""
            SELECT person_id, source_type, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id, source_type
        """)

        canonical_counts: dict[str, dict[str, int]] = {}
        # Map canonical_id -> set of raw person_ids that resolve to it
        # (used by _update_first_seen to query MIN across all variants).
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
                _update_first_seen(entity, list(canonical_ids[canon]), conn)
                store.update(entity)
                stats['updated'] += 1

        # Zero out people with no interactions (they may have had interactions
        # deleted). They may still have source_entity timestamps worth updating
        # for first_seen.
        for cached_entity in store.get_all():
            if cached_entity.id not in canonical_counts:
                entity = store.get_by_id(cached_entity.id)
                if not entity:
                    continue
                modified = False
                if entity.email_count or entity.meeting_count or entity.message_count or entity.mention_count or entity.photo_count:
                    entity.email_count = 0
                    entity.meeting_count = 0
                    entity.message_count = 0
                    entity.mention_count = 0
                    entity.photo_count = 0
                    modified = True
                if _update_first_seen(entity, [entity.id], conn):
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
                _update_first_seen(entity, list(variants), conn)
                store.update(entity)
                stats['updated'] += 1

    conn.close()

    stats['last_seen_changed'] = sync_last_seen(person_ids)['mismatched']

    if save:
        store.save()  # Uses file locking

    if stats['updated'] > 0:
        logger.info(f"Refreshed stats for {stats['updated']} people ({stats['total_interactions']} interactions)")

    return stats


def sync_last_seen(person_ids: Optional[Iterable[str]] = None, *, dry_run: bool = False) -> dict:
    """
    Make person_entities.last_seen agree with the interactions table.

    This is the only code that writes last_seen. The value is the most recent
    real interaction with the person, as get_last_real_interactions() defines it.
    Source entities and contact imports play no part, and someone with no real
    interaction is NULL. A merged id's rows count for the surviving person, so a
    merged-away row is NULL too.

    Only rows that disagree are written, so a run that finds nothing to do leaves
    the database untouched.

    Args:
        person_ids: People to check, by any id that resolves to them. None checks
            everyone with one aggregate query.
        dry_run: Report disagreements without writing them.

    Returns:
        Dict with stats: {checked: int, mismatched: int, cleared: int}, where
        cleared is how many of the mismatched rows have no value to hold.
    """
    from api.services.person_entity import get_person_entity_store
    from api.services.interaction_store import get_interaction_db_path, get_last_real_interactions

    store = get_person_entity_store()

    targets: Optional[set[str]] = None
    if person_ids is not None:
        targets = set()
        for pid in person_ids:
            canonical = store.get_canonical_id(pid)
            targets |= {canonical, pid} | store.get_legacy_ids(canonical)

    int_conn = sqlite3.connect(get_interaction_db_path(), timeout=60.0)
    try:
        latest_by_raw_id = get_last_real_interactions(int_conn, targets)
    finally:
        int_conn.close()

    latest: dict[str, datetime] = {}
    for pid, last in latest_by_raw_id.items():
        canonical = store.get_canonical_id(pid)
        if canonical not in latest or last > latest[canonical]:
            latest[canonical] = last

    conn = store._get_connection()
    try:
        rows = [
            (row['id'], row['last_seen'])
            for row in conn.execute("SELECT id, last_seen FROM person_entities")
            if targets is None or row['id'] in targets
        ]
        changes = []
        cleared = 0
        for pid, stored in rows:
            last = latest.get(pid)
            value = last.isoformat() if last else None
            if stored != value:
                changes.append((value, pid))
                cleared += value is None

        if changes and not dry_run:
            conn.executemany("UPDATE person_entities SET last_seen = ? WHERE id = ?", changes)
            conn.commit()
            store.save()
    finally:
        conn.close()

    return {'checked': len(rows), 'mismatched': len(changes), 'cleared': cleared}


def _update_first_seen(entity, person_ids, int_conn) -> bool:
    """
    Update first_seen from interactions and source_entities.

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

    # Earliest interaction across ALL person_id variants
    cursor = int_conn.execute(f"""
        SELECT MIN(timestamp)
        FROM interactions
        WHERE person_id IN ({placeholders})
    """, tuple(person_ids))
    row = cursor.fetchone()
    interaction_first = None
    if row and row[0]:
        interaction_first = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)

    # Earliest source_entity observation across the same variants
    # (source_entities.canonical_person_id may also still carry legacy IDs
    # if a merge happened without backfilling that column).
    # Same physical database as PersonEntityStore's default — reuse its anchored
    # constant rather than a second cwd-relative literal for the same file.
    crm_db = Path(PersonEntityStore.CRM_DB_PATH)
    source_first = None
    if crm_db.exists():
        crm_conn = sqlite3.connect(str(crm_db))
        crm_cursor = crm_conn.execute(f"""
            SELECT MIN(observed_at)
            FROM source_entities
            WHERE canonical_person_id IN ({placeholders})
        """, tuple(person_ids))
        se_row = crm_cursor.fetchone()
        crm_conn.close()
        if se_row and se_row[0]:
            source_first = datetime.fromisoformat(se_row[0]).replace(tzinfo=timezone.utc)

    # Compute first_seen = earliest across both sources
    if interaction_first and source_first:
        new_first = min(interaction_first, source_first)
    else:
        new_first = interaction_first or source_first

    if new_first and entity.first_seen != new_first:
        entity.first_seen = new_first
        return True
    return False


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
                entity = store.get_by_id(entity.id)
                if not entity:
                    continue
                _apply_counts_to_entity(entity, counts)
                store.update(entity)

    conn.close()

    if fix and discrepancies:
        store.save()
        logger.info(f"Fixed {len(discrepancies)} discrepancies")

    return discrepancies
