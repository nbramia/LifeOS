#!/usr/bin/env python3
"""
Merge duplicate person records.

This script merges two PersonEntity records into one, updating all references
(interactions, source_entities, facts) to point to the surviving record.

The merge is durable - merged IDs are tracked so entity resolution won't
recreate duplicates from future syncs.

Usage:
    python scripts/merge_people.py --primary <id> --secondary <id> [--execute]
    python scripts/merge_people.py --list-duplicates
    python scripts/merge_people.py --search "name pattern"
"""
import sys
import json
import sqlite3
import logging
import argparse
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import get_person_entity_store
from api.services.interaction_store import get_interaction_db_path
from api.services.source_entity import get_crm_db_path

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# File to track merged person IDs for durability
MERGED_IDS_FILE = Path(__file__).parent.parent / "data" / "merged_person_ids.json"
# File to track in-progress merge for crash recovery
MERGE_LOG_FILE = Path(__file__).parent.parent / "data" / "merge_log.json"


def load_merged_ids() -> dict:
    """Load the merged IDs mapping (secondary_id -> primary_id)."""
    if MERGED_IDS_FILE.exists():
        with open(MERGED_IDS_FILE) as f:
            return json.load(f)
    return {}


def save_merged_ids(merged_ids: dict):
    """Save the merged IDs mapping with atomic write.

    Uses temp file + rename to prevent corruption if process crashes mid-write.
    """
    import tempfile
    import shutil
    import os

    MERGED_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp_fd, temp_path = tempfile.mkstemp(suffix=".json", dir=MERGED_IDS_FILE.parent)
    try:
        with os.fdopen(temp_fd, "w") as f:
            json.dump(merged_ids, f, indent=2)
        shutil.move(temp_path, MERGED_IDS_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def write_merge_intent(primary_id: str, secondary_id: str):
    """Write intent log for crash recovery. Atomic write like save_merged_ids()."""
    import tempfile
    import os

    log = {
        "operation": "merge",
        "primary_id": primary_id,
        "secondary_id": secondary_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phase": "pending",
    }
    MERGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(suffix=".json", dir=MERGE_LOG_FILE.parent)
    try:
        with os.fdopen(temp_fd, "w") as f:
            json.dump(log, f, indent=2)
        import shutil
        shutil.move(temp_path, MERGE_LOG_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def update_merge_phase(phase: str):
    """Atomically update the phase field in the merge intent log."""
    import tempfile
    import os

    log = load_merge_log()
    if log is None:
        raise RuntimeError("No merge log to update")
    log["phase"] = phase
    temp_fd, temp_path = tempfile.mkstemp(suffix=".json", dir=MERGE_LOG_FILE.parent)
    try:
        with os.fdopen(temp_fd, "w") as f:
            json.dump(log, f, indent=2)
        import shutil
        shutil.move(temp_path, MERGE_LOG_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def load_merge_log() -> dict | None:
    """Load the merge intent log, or None if absent."""
    if MERGE_LOG_FILE.exists():
        with open(MERGE_LOG_FILE) as f:
            return json.load(f)
    return None


def clear_merge_log():
    """Delete the merge intent log file."""
    if MERGE_LOG_FILE.exists():
        MERGE_LOG_FILE.unlink()


def _tone_analysis_results_table_exists(conn: sqlite3.Connection) -> bool:
    """True if crm.db has a tone_analysis_results table.

    Unlike the other tables merge_people touches, this one is created
    lazily by ToneAnalysisStore on its first use
    (api/services/tone_analysis_store.py) rather than always being
    present, so a crm.db where tone analysis has never run doesn't have it
    yet (#910).
    """
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tone_analysis_results'"
    ).fetchone() is not None


def get_canonical_person_id(person_id: str) -> str:
    """
    Get the canonical (primary) person ID, following merge chain if needed.

    This is called by entity resolver to ensure we always use the primary ID.
    """
    merged_ids = load_merged_ids()

    # Follow the merge chain (in case of multiple merges)
    visited = set()
    while person_id in merged_ids and person_id not in visited:
        visited.add(person_id)
        person_id = merged_ids[person_id]

    return person_id


def search_people(pattern: str) -> list:
    """Search for people matching a pattern."""
    store = get_person_entity_store()
    people = store.get_all()

    pattern_lower = pattern.lower()
    matches = []

    for p in people:
        # Match against name, emails, phones
        if pattern_lower in p.canonical_name.lower():
            matches.append(p)
        elif any(pattern_lower in e.lower() for e in (p.emails or [])):
            matches.append(p)
        elif any(pattern_lower in ph for ph in (p.phone_numbers or [])):
            matches.append(p)

    return matches


def find_potential_duplicates() -> list:
    """Find potential duplicate person records."""
    store = get_person_entity_store()
    people = store.get_all()

    duplicates = []

    # Group by normalized name
    by_name = {}
    for p in people:
        # Normalize: lowercase, remove common suffixes
        name = p.canonical_name.lower().strip()
        for suffix in [' jr', ' sr', ' ii', ' iii']:
            name = name.replace(suffix, '')

        if name not in by_name:
            by_name[name] = []
        by_name[name].append(p)

    for name, group in by_name.items():
        if len(group) > 1:
            duplicates.append({
                'name': name,
                'people': group,
            })

    # Also check for shared emails/phones across different names
    by_email = {}
    by_phone = {}

    for p in people:
        for email in (p.emails or []):
            if email not in by_email:
                by_email[email] = []
            by_email[email].append(p)
        for phone in (p.phone_numbers or []):
            if phone not in by_phone:
                by_phone[phone] = []
            by_phone[phone].append(p)

    for email, group in by_email.items():
        if len(group) > 1:
            names = [p.canonical_name for p in group]
            if len(set(names)) > 1:  # Different names sharing email
                duplicates.append({
                    'reason': f'shared email: {email}',
                    'people': group,
                })

    for phone, group in by_phone.items():
        if len(group) > 1:
            names = [p.canonical_name for p in group]
            if len(set(names)) > 1:  # Different names sharing phone
                duplicates.append({
                    'reason': f'shared phone: {phone}',
                    'people': group,
                })

    return duplicates


def _merge_contexts_json(a_json: str | None, b_json: str | None) -> str:
    """Merge two JSON-encoded context lists, deduplicating."""
    a = json.loads(a_json) if a_json else []
    b = json.loads(b_json) if b_json else []
    merged = list(a)
    for ctx in b:
        if ctx not in merged:
            merged.append(ctx)
    return json.dumps(merged)


def _earliest(a: str | None, b: str | None) -> str | None:
    """Return the earlier of two ISO timestamp strings."""
    if not a:
        return b
    if not b:
        return a
    return a if a < b else b


def _latest(a: str | None, b: str | None) -> str | None:
    """Return the later of two ISO timestamp strings."""
    if not a:
        return b
    if not b:
        return a
    return a if a > b else b


def merge_people(primary_id: str, secondary_id: str, dry_run: bool = True) -> dict:
    """
    Merge secondary person into primary person.

    Args:
        primary_id: ID of the person to keep (survivor)
        secondary_id: ID of the person to merge and delete
        dry_run: If True, don't actually make changes

    Returns:
        Stats dict
    """
    stats = {
        'interactions_updated': 0,
        'source_entities_updated': 0,
        'facts_cleared': 0,
        'tone_rows_cleared': 0,
        'emails_merged': 0,
        'phones_merged': 0,
        'aliases_added': 0,
        'tags_merged': 0,
        'notes_merged': 0,
    }

    store = get_person_entity_store()
    primary = store.get_by_id(primary_id)
    secondary = store.get_by_id(secondary_id)

    if not primary:
        raise ValueError(f"Primary person not found: {primary_id}")
    if not secondary:
        raise ValueError(f"Secondary person not found: {secondary_id}")

    # Use the canonical IDs from the resolved entities (follows merge chains)
    # This ensures if B was already merged into C, merging A→B actually goes to C
    canonical_primary_id = primary.id
    canonical_secondary_id = secondary.id

    logger.info(f"Merging: '{secondary.canonical_name}' -> '{primary.canonical_name}'")
    logger.info(f"  Primary ID: {primary_id}" + (f" (canonical: {canonical_primary_id})" if canonical_primary_id != primary_id else ""))
    logger.info(f"  Secondary ID: {secondary_id}" + (f" (canonical: {canonical_secondary_id})" if canonical_secondary_id != secondary_id else ""))

    # 1. Merge identifying info into primary
    logger.info("\n1. Merging identifying info...")

    # Merge emails
    for email in (secondary.emails or []):
        if email and email not in (primary.emails or []):
            if primary.emails is None:
                primary.emails = []
            primary.emails.append(email)
            stats['emails_merged'] += 1
            logger.info("   + Email merged")

    # Merge phone numbers
    for phone in (secondary.phone_numbers or []):
        if phone and phone not in (primary.phone_numbers or []):
            if primary.phone_numbers is None:
                primary.phone_numbers = []
            primary.phone_numbers.append(phone)
            stats['phones_merged'] += 1
            logger.info("   + Phone merged")

    # Add secondary's name as alias
    if secondary.canonical_name and secondary.canonical_name != primary.canonical_name:
        if primary.aliases is None:
            primary.aliases = []
        if secondary.canonical_name not in primary.aliases:
            primary.aliases.append(secondary.canonical_name)
            stats['aliases_added'] += 1
            logger.info(f"   + Alias: {secondary.canonical_name}")

    # Merge secondary's aliases
    for alias in (secondary.aliases or []):
        if alias and alias not in (primary.aliases or []):
            if primary.aliases is None:
                primary.aliases = []
            primary.aliases.append(alias)
            stats['aliases_added'] += 1
            logger.info(f"   + Alias: {alias}")

    # Merge sources
    for source in (secondary.sources or []):
        if source and source not in (primary.sources or []):
            if primary.sources is None:
                primary.sources = []
            primary.sources.append(source)

    # Merge category using hierarchy: family > work > personal > unknown
    category_priority = {"family": 0, "work": 1, "personal": 2, "unknown": 3}
    primary_cat_priority = category_priority.get(primary.category, 3)
    secondary_cat_priority = category_priority.get(secondary.category, 3)
    if secondary_cat_priority < primary_cat_priority:
        logger.info(f"   ~ Category: {primary.category} -> {secondary.category}")
        primary.category = secondary.category

    # Merge tags (combine and deduplicate)
    stats['tags_merged'] = 0
    for tag in (secondary.tags or []):
        if tag and tag not in (primary.tags or []):
            if primary.tags is None:
                primary.tags = []
            primary.tags.append(tag)
            stats['tags_merged'] += 1
            logger.info(f"   + Tag: {tag}")

    # Merge notes (concatenate with separator if both have content)
    stats['notes_merged'] = 0
    if secondary.notes and secondary.notes.strip():
        if primary.notes and primary.notes.strip():
            # Both have notes - concatenate with separator
            if secondary.notes.strip() != primary.notes.strip():
                primary.notes = f"{primary.notes}\n\n---\n\n{secondary.notes}"
                stats['notes_merged'] = 1
                logger.info("   + Notes: concatenated from secondary")
        else:
            # Only secondary has notes - use them
            primary.notes = secondary.notes
            stats['notes_merged'] = 1
            logger.info("   + Notes: copied from secondary")

    if dry_run:
        # Dry run: gather stats without modifying anything
        # 2. Count interactions
        logger.info("\n2. Counting interactions...")
        interactions_db = get_interaction_db_path()
        int_conn = sqlite3.connect(interactions_db)
        ids_to_migrate = [canonical_secondary_id]
        if secondary_id != canonical_secondary_id:
            ids_to_migrate.append(secondary_id)
        total_count = 0
        for old_id in ids_to_migrate:
            count = int_conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE person_id = ?", (old_id,)
            ).fetchone()[0]
            if count > 0:
                logger.info(f"   {count} interactions to update for {old_id}")
            total_count += count
        stats['interactions_updated'] = total_count
        logger.info(f"   Total: {total_count} interactions" if total_count else "   No interactions to update")
        int_conn.close()

        # 3. Count source entities / facts
        crm_db = get_crm_db_path()
        crm_conn = sqlite3.connect(crm_db)
        stats['source_entities_updated'] = crm_conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (canonical_secondary_id,)
        ).fetchone()[0]
        logger.info(f"\n3. {stats['source_entities_updated']} source entities to update")
        stats['facts_cleared'] = crm_conn.execute(
            "SELECT COUNT(*) FROM person_facts WHERE person_id IN (?, ?)",
            (canonical_primary_id, canonical_secondary_id)
        ).fetchone()[0]
        logger.info(f"4. {stats['facts_cleared']} facts to clear")
        stats['tone_rows_cleared'] = 0
        if _tone_analysis_results_table_exists(crm_conn):
            stats['tone_rows_cleared'] = crm_conn.execute(
                "SELECT COUNT(*) FROM tone_analysis_results WHERE person_id = ?",
                (canonical_secondary_id,)
            ).fetchone()[0]
        logger.info(f"   {stats['tone_rows_cleared']} tone analysis rows to clear")

        # 4. Count relationships
        from api.services.relationship import get_relationship_store
        rel_store = get_relationship_store()
        secondary_rels = rel_store.get_for_person(canonical_secondary_id)
        stats['relationships_updated'] = 0
        stats['relationships_merged'] = 0
        stats['relationships_deleted'] = 0
        for rel in secondary_rels:
            other_id = rel.other_person(canonical_secondary_id)
            if not other_id:
                continue
            if other_id == canonical_primary_id:
                stats['relationships_deleted'] += 1
            elif rel_store.get_between(canonical_primary_id, other_id):
                stats['relationships_merged'] += 1
            else:
                stats['relationships_updated'] += 1
        logger.info(f"5. {len(secondary_rels)} relationships to process")
        crm_conn.close()

        logger.info("\n=== Merge Summary (DRY RUN) ===")
        logger.info(f"Primary: {primary.canonical_name} ({canonical_primary_id})")
        logger.info(f"Secondary: {secondary.canonical_name} ({canonical_secondary_id})")
        logger.info(f"Interactions: {stats['interactions_updated']}")
        logger.info(f"Source entities: {stats['source_entities_updated']}")
        logger.info(f"Facts: {stats['facts_cleared']}")
        logger.info(f"Tone analysis rows: {stats['tone_rows_cleared']}")
        logger.info(f"Relationships: {stats['relationships_updated']} transfer, {stats['relationships_merged']} merge, {stats['relationships_deleted']} delete")
        logger.info(f"Emails: +{stats['emails_merged']}, Phones: +{stats['phones_merged']}, Aliases: +{stats['aliases_added']}")
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")
        return stats

    # === EXECUTE PATH (crash-safe with intent log) ===

    # Update last_seen/first_seen on in-memory primary before persisting
    if secondary.last_seen:
        if primary.last_seen is None or secondary.last_seen > primary.last_seen:
            primary.last_seen = secondary.last_seen
    if secondary.first_seen:
        if primary.first_seen is None or secondary.first_seen < primary.first_seen:
            primary.first_seen = secondary.first_seen

    # Step 1: Write intent log
    logger.info("\n2. Writing intent log...")
    write_merge_intent(canonical_primary_id, canonical_secondary_id)

    # Step 2: Update merged_person_ids.json (early — prevents sync from recreating dupes)
    logger.info("\n3. Recording merge IDs...")
    merged_ids = load_merged_ids()
    merged_ids[secondary_id] = canonical_primary_id
    if canonical_secondary_id != secondary_id:
        merged_ids[canonical_secondary_id] = canonical_primary_id
    save_merged_ids(merged_ids)
    update_merge_phase("ids_written")
    logger.info(f"   Recorded: {secondary_id} -> {canonical_primary_id}")

    # Step 3: Update interactions.db
    logger.info("\n4. Updating interactions...")
    interactions_db = get_interaction_db_path()
    int_conn = sqlite3.connect(interactions_db)
    ids_to_migrate = [canonical_secondary_id]
    if secondary_id != canonical_secondary_id:
        ids_to_migrate.append(secondary_id)
    total_count = 0
    for old_id in ids_to_migrate:
        count = int_conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE person_id = ?", (old_id,)
        ).fetchone()[0]
        if count > 0:
            logger.info(f"   {count} interactions to update for {old_id}")
            int_conn.execute(
                "UPDATE interactions SET person_id = ? WHERE person_id = ?",
                (canonical_primary_id, old_id)
            )
        total_count += count
    stats['interactions_updated'] = total_count
    if total_count > 0:
        int_conn.commit()
        logger.info(f"   Total: {total_count} interactions updated")
    else:
        logger.info("   No interactions to update")
    int_conn.close()
    update_merge_phase("interactions_done")

    # Step 4: Single crm.db transaction
    # Groups: source_entities, facts, relationships, person_entities update+delete
    logger.info("\n5. Updating CRM database (single transaction)...")
    crm_db = get_crm_db_path()
    crm_conn = sqlite3.connect(crm_db)
    crm_conn.execute("BEGIN IMMEDIATE")

    try:
        # 4a. Update source entities
        cursor = crm_conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (canonical_secondary_id,)
        )
        stats['source_entities_updated'] = cursor.fetchone()[0]
        if stats['source_entities_updated'] > 0:
            crm_conn.execute(
                "UPDATE source_entities SET canonical_person_id = ? WHERE canonical_person_id = ?",
                (canonical_primary_id, canonical_secondary_id)
            )
        logger.info(f"   Source entities: {stats['source_entities_updated']} updated")

        # 4b. Clear facts
        cursor = crm_conn.execute(
            "SELECT COUNT(*) FROM person_facts WHERE person_id IN (?, ?)",
            (canonical_primary_id, canonical_secondary_id)
        )
        stats['facts_cleared'] = cursor.fetchone()[0]
        if stats['facts_cleared'] > 0:
            crm_conn.execute(
                "DELETE FROM person_facts WHERE person_id IN (?, ?)",
                (canonical_primary_id, canonical_secondary_id)
            )
        logger.info(f"   Facts cleared: {stats['facts_cleared']}")

        # 4b-2. Remove tone analysis results for the absorbed person only
        # (#910). Unlike facts (cleared for *both* ids, above), the
        # primary's own tone_analysis_results rows are deliberately left
        # alone: each row's freshness is already keyed to a stored
        # interaction_count compared against the person's *current*
        # interaction count (api/routes/crm.py's analyze_relationship_tone_detailed).
        # Step 3 above just repointed the secondary's interactions to the
        # primary, so any month where that changes the primary's count
        # self-heals the next time tone analysis runs for the primary --
        # it recomputes using the now-complete, merged interaction history
        # rather than needing this script to know that. The secondary's
        # own rows have no such self-healing (the id they're keyed to is
        # about to stop existing), so those are the ones that must be
        # deleted here to avoid orphaning -- not re-keyed onto the primary,
        # since a re-keyed row could collide with a period_key the primary
        # already has (the table's primary key is (person_id, period_key)),
        # and simply deleting it lets the primary's next tone-analysis
        # request compute a fresh score from the merged data instead of
        # carrying forward a score computed from only half of it.
        stats['tone_rows_cleared'] = 0
        if _tone_analysis_results_table_exists(crm_conn):
            cursor = crm_conn.execute(
                "SELECT COUNT(*) FROM tone_analysis_results WHERE person_id = ?",
                (canonical_secondary_id,)
            )
            stats['tone_rows_cleared'] = cursor.fetchone()[0]
            if stats['tone_rows_cleared'] > 0:
                crm_conn.execute(
                    "DELETE FROM tone_analysis_results WHERE person_id = ?",
                    (canonical_secondary_id,)
                )
        logger.info(f"   Tone analysis rows cleared: {stats['tone_rows_cleared']}")

        # 4c. Merge relationships (raw SQL within same transaction)
        stats['relationships_updated'] = 0
        stats['relationships_merged'] = 0
        stats['relationships_deleted'] = 0

        # Read all secondary relationships
        secondary_rels = crm_conn.execute(
            "SELECT * FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
            (canonical_secondary_id, canonical_secondary_id)
        ).fetchall()
        col_names = [desc[0] for desc in crm_conn.execute("SELECT * FROM relationships LIMIT 0").description]
        logger.info(f"   Relationships: {len(secondary_rels)} to process")

        for row in secondary_rels:
            rel = dict(zip(col_names, row))
            # Find the "other" person
            if rel['person_a_id'] == canonical_secondary_id:
                other_id = rel['person_b_id']
            else:
                other_id = rel['person_a_id']

            if other_id == canonical_primary_id:
                # Self-loop — delete
                crm_conn.execute("DELETE FROM relationships WHERE id = ?", (rel['id'],))
                stats['relationships_deleted'] += 1
                logger.info("   - Deleted self-loop relationship")
                continue

            # Normalize IDs for the primary-other pair
            norm_a, norm_b = (canonical_primary_id, other_id) if canonical_primary_id < other_id else (other_id, canonical_primary_id)

            existing = crm_conn.execute(
                "SELECT * FROM relationships WHERE person_a_id = ? AND person_b_id = ?",
                (norm_a, norm_b)
            ).fetchone()

            if existing:
                ex = dict(zip(col_names, existing))
                # Merge counts
                now = datetime.now(timezone.utc).isoformat()
                crm_conn.execute("""
                    UPDATE relationships SET
                        shared_events_count = ?,
                        shared_threads_count = ?,
                        shared_messages_count = ?,
                        shared_whatsapp_count = ?,
                        shared_slack_count = ?,
                        shared_phone_calls_count = ?,
                        shared_photos_count = ?,
                        shared_contexts = ?,
                        first_seen_together = ?,
                        last_seen_together = ?,
                        is_linkedin_connection = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (
                    (ex.get('shared_events_count') or 0) + (rel.get('shared_events_count') or 0),
                    (ex.get('shared_threads_count') or 0) + (rel.get('shared_threads_count') or 0),
                    (ex.get('shared_messages_count') or 0) + (rel.get('shared_messages_count') or 0),
                    (ex.get('shared_whatsapp_count') or 0) + (rel.get('shared_whatsapp_count') or 0),
                    (ex.get('shared_slack_count') or 0) + (rel.get('shared_slack_count') or 0),
                    (ex.get('shared_phone_calls_count') or 0) + (rel.get('shared_phone_calls_count') or 0),
                    (ex.get('shared_photos_count') or 0) + (rel.get('shared_photos_count') or 0),
                    _merge_contexts_json(ex.get('shared_contexts'), rel.get('shared_contexts')),
                    _earliest(ex.get('first_seen_together'), rel.get('first_seen_together')),
                    _latest(ex.get('last_seen_together'), rel.get('last_seen_together')),
                    1 if (ex.get('is_linkedin_connection') or rel.get('is_linkedin_connection')) else 0,
                    now,
                    ex['id'],
                ))
                # Delete the secondary's relationship
                crm_conn.execute("DELETE FROM relationships WHERE id = ?", (rel['id'],))
                stats['relationships_merged'] += 1
                logger.info(f"   ~ Merged relationship with {other_id}")
            else:
                # Transfer: delete old, insert new with primary ID
                new_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat()
                crm_conn.execute("DELETE FROM relationships WHERE id = ?", (rel['id'],))
                crm_conn.execute("""
                    INSERT INTO relationships
                    (id, person_a_id, person_b_id, relationship_type, shared_contexts,
                     shared_events_count, shared_threads_count, first_seen_together,
                     last_seen_together, created_at, updated_at,
                     shared_messages_count, shared_whatsapp_count, shared_slack_count,
                     is_linkedin_connection, shared_phone_calls_count, shared_photos_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    new_id, norm_a, norm_b,
                    rel.get('relationship_type'),
                    rel.get('shared_contexts'),
                    rel.get('shared_events_count') or 0,
                    rel.get('shared_threads_count') or 0,
                    rel.get('first_seen_together'),
                    rel.get('last_seen_together'),
                    rel.get('created_at') or now,
                    now,
                    rel.get('shared_messages_count') or 0,
                    rel.get('shared_whatsapp_count') or 0,
                    rel.get('shared_slack_count') or 0,
                    1 if rel.get('is_linkedin_connection') else 0,
                    rel.get('shared_phone_calls_count') or 0,
                    rel.get('shared_photos_count') or 0,
                ))
                stats['relationships_updated'] += 1
                logger.info(f"   > Transferred relationship with {other_id}")

        # 4d. INSERT OR REPLACE primary person entity
        primary_values = store._entity_to_values(primary)
        from api.services.person_entity import PersonEntityStore
        crm_conn.execute(
            f"INSERT OR REPLACE INTO person_entities ({PersonEntityStore._COLUMNS_STR}) "
            f"VALUES ({PersonEntityStore._PLACEHOLDERS})",
            primary_values
        )

        # 4e. Update lookup tables for primary
        crm_conn.execute("DELETE FROM person_emails WHERE person_id = ?", (canonical_primary_id,))
        crm_conn.execute("DELETE FROM person_phones WHERE person_id = ?", (canonical_primary_id,))
        crm_conn.execute("DELETE FROM person_names WHERE person_id = ?", (canonical_primary_id,))
        for email in (primary.emails or []):
            crm_conn.execute(
                "INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
                (email.lower(), canonical_primary_id))
        for phone in (primary.phone_numbers or []):
            if phone:
                crm_conn.execute(
                    "INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
                    (phone, canonical_primary_id))
        if primary.canonical_name:
            crm_conn.execute(
                "INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                (primary.canonical_name.lower(), canonical_primary_id))
        for alias in (primary.aliases or []):
            if alias:
                crm_conn.execute(
                    "INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                    (alias.lower(), canonical_primary_id))

        # 4f. Soft-delete secondary person entity, remove lookup tables
        now_iso = datetime.now(timezone.utc).isoformat()
        crm_conn.execute(
            "UPDATE person_entities SET hidden = 1, hidden_at = ?, hidden_reason = ? WHERE id = ?",
            (now_iso, f"merged_into:{canonical_primary_id}", canonical_secondary_id),
        )
        crm_conn.execute("DELETE FROM person_emails WHERE person_id = ?", (canonical_secondary_id,))
        crm_conn.execute("DELETE FROM person_phones WHERE person_id = ?", (canonical_secondary_id,))
        crm_conn.execute("DELETE FROM person_names WHERE person_id = ?", (canonical_secondary_id,))

        crm_conn.commit()
        logger.info("   CRM transaction committed")
    except Exception:
        crm_conn.rollback()
        crm_conn.close()
        raise
    crm_conn.close()
    update_merge_phase("crm_done")

    logger.info(f"   Soft-deleted secondary record: {secondary.canonical_name}")

    # Step 5: Post-merge cleanup (stats refresh + relationship strength)
    logger.info("\n6. Post-merge cleanup...")
    from api.services.person_stats import refresh_person_stats
    logger.info("   Refreshing stats from InteractionStore...")
    refresh_person_stats([canonical_primary_id])

    from api.services.relationship_metrics import update_strength_for_person
    primary = store.get_by_id(canonical_primary_id)
    old_strength = primary.relationship_strength if primary else None
    new_strength = update_strength_for_person(canonical_primary_id)
    if new_strength != old_strength:
        logger.info(f"   Strength: {old_strength} -> {new_strength}")
    else:
        logger.info(f"   Strength unchanged: {new_strength}")

    # Step 6: Clear intent log
    update_merge_phase("complete")
    clear_merge_log()
    logger.info("   Intent log cleared")

    # Summary
    logger.info("\n=== Merge Summary ===")
    logger.info(f"Primary: {primary.canonical_name} ({canonical_primary_id})")
    logger.info(f"Secondary: {secondary.canonical_name} ({canonical_secondary_id})")
    logger.info(f"Interactions updated: {stats['interactions_updated']}")
    logger.info(f"Source entities updated: {stats['source_entities_updated']}")
    logger.info(f"Facts cleared: {stats['facts_cleared']} (will regenerate)")
    logger.info(f"Tone analysis rows cleared: {stats['tone_rows_cleared']}")
    logger.info(f"Relationships: {stats['relationships_updated']} transferred, {stats['relationships_merged']} merged, {stats['relationships_deleted']} deleted")
    logger.info(f"Emails merged: {stats['emails_merged']}")
    logger.info(f"Phones merged: {stats['phones_merged']}")
    logger.info(f"Aliases added: {stats['aliases_added']}")

    return stats


def recover_incomplete_merge() -> bool:
    """
    Check for and recover an incomplete merge operation.

    Returns True if a recovery was performed, False if nothing to recover.
    """
    log = load_merge_log()
    if log is None:
        return False

    if log.get("phase") == "complete":
        clear_merge_log()
        return False

    primary_id = log["primary_id"]
    secondary_id = log["secondary_id"]
    logger.warning(f"Found incomplete merge: {secondary_id} -> {primary_id} (phase: {log.get('phase')})")

    store = get_person_entity_store()
    primary = store.get_by_id(primary_id)

    # IMPORTANT: Don't use get_by_id(secondary_id) — it follows the merge chain.
    # If merged_person_ids.json was already written, get_by_id(secondary) resolves
    # to the primary, causing a self-merge that deletes the primary.
    # Instead: check if secondary_id is in the merge map. If so, it's logically gone.
    merged_ids = load_merged_ids()
    secondary_is_merged = secondary_id in merged_ids

    if secondary_is_merged:
        # The secondary was already recorded as merged. Check if its physical
        # record still exists by doing a raw lookup (not following merge chain).
        secondary_result = store.get_by_id(secondary_id)
        # get_by_id follows chain, so if it returns the primary, secondary is gone
        secondary_exists = secondary_result is not None and secondary_result.id == secondary_id
    else:
        secondary_result = store.get_by_id(secondary_id)
        secondary_exists = secondary_result is not None

    if not primary:
        # Primary doesn't exist — can't recover meaningfully
        logger.error(f"Recovery failed: primary person {primary_id} not found")
        clear_merge_log()
        return False

    if not secondary_exists:
        # Secondary already deleted — merge was mostly/fully done.
        # Run post-merge cleanup and clear log.
        logger.info("Secondary already deleted — running cleanup steps only")
        try:
            from api.services.person_stats import refresh_person_stats
            refresh_person_stats([primary_id])
            from api.services.relationship_metrics import update_strength_for_person
            update_strength_for_person(primary_id)
        except Exception as e:
            logger.warning(f"Post-merge cleanup during recovery had issues: {e}")
        clear_merge_log()
        return True

    # Both exist — re-run the full merge (all steps are idempotent)
    logger.info("Re-running merge for crash recovery...")
    merge_people(primary_id, secondary_id, dry_run=False)
    return True


def main():
    parser = argparse.ArgumentParser(description='Merge duplicate person records')
    parser.add_argument('--primary', help='ID of the person to keep')
    parser.add_argument('--secondary', help='ID of the person to merge into primary')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--list-duplicates', action='store_true', help='List potential duplicates')
    parser.add_argument('--search', help='Search for people by name/email/phone')
    parser.add_argument('--recover', action='store_true', help='Recover an incomplete merge')
    args = parser.parse_args()

    if args.recover:
        if recover_incomplete_merge():
            print("Recovery completed successfully.")
        else:
            print("No incomplete merge to recover.")
        return

    if args.list_duplicates:
        duplicates = find_potential_duplicates()
        print(f"\nFound {len(duplicates)} potential duplicate groups:\n")
        for i, dup in enumerate(duplicates, 1):
            reason = dup.get('name') or dup.get('reason')
            print(f"{i}. {reason}")
            for p in dup['people']:
                total = (p.email_count or 0) + (p.message_count or 0) + (p.meeting_count or 0)
                print(f"   - {p.canonical_name} (ID: {p.id[:8]}..., interactions: {total})")
            print()
        return

    if args.search:
        matches = search_people(args.search)
        print(f"\nFound {len(matches)} matches for '{args.search}':\n")
        for p in matches:
            total = (p.email_count or 0) + (p.message_count or 0) + (p.meeting_count or 0)
            print(f"  ID: {p.id}")
            print(f"  Name: {p.canonical_name}")
            print(f"  Emails: {p.emails}")
            print(f"  Phones: {p.phone_numbers}")
            print(f"  Aliases: {p.aliases}")
            print(f"  Interactions: {total} (email={p.email_count}, msg={p.message_count}, mtg={p.meeting_count})")
            print(f"  Strength: {p.relationship_strength}")
            print()
        return

    if not args.primary or not args.secondary:
        parser.print_help()
        print("\nExamples:")
        print("  python scripts/merge_people.py --search 'Alex'")
        print("  python scripts/merge_people.py --list-duplicates")
        print("  python scripts/merge_people.py --primary abc123 --secondary def456")
        print("  python scripts/merge_people.py --primary abc123 --secondary def456 --execute")
        return

    merge_people(args.primary, args.secondary, dry_run=not args.execute)


if __name__ == '__main__':
    main()
