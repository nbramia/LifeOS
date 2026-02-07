#!/usr/bin/env python3
"""
Split incorrectly merged person entities.

Moves source entities and interactions from one person to another,
or creates a new person if needed.

Usage:
    # Move vault sources from person A to person B
    uv run python scripts/split_person.py --from-person "Hayley" --to-person "Hayleycurrier" --source-types vault granola

    # Move sources to a new person
    uv run python scripts/split_person.py --from-person "Hayley" --to-person NEW --new-name "Hayley Currier" --source-types vault

    # Dry run (default)
    uv run python scripts/split_person.py --from-person "Hayley" --to-person "Hayleycurrier" --source-types vault

    # Execute
    uv run python scripts/split_person.py --from-person "Hayley" --to-person "Hayleycurrier" --source-types vault --execute
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import sqlite3
import logging
from datetime import datetime, timezone

from api.services.person_entity import get_person_entity_store, PersonEntity
from api.services.source_entity import get_source_entity_store
from api.services.interaction_store import get_interaction_db_path
from api.services.link_override import get_link_override_store, LinkOverride

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def find_person_by_name(name: str) -> PersonEntity | None:
    """Find a person by name (case-insensitive partial match)."""
    store = get_person_entity_store()
    for person in store.get_all():
        if name.lower() in person.canonical_name.lower():
            return person
    return None


def get_source_entities_for_person(person_id: str, source_types: list[str] | None = None) -> list[dict]:
    """Get source entities linked to a person, optionally filtered by type."""
    crm_db = Path(__file__).parent.parent / "data" / "crm.db"
    conn = sqlite3.connect(crm_db)
    conn.row_factory = sqlite3.Row

    if source_types:
        placeholders = ','.join('?' * len(source_types))
        query = f"""
            SELECT id, source_type, source_id, observed_name, observed_email, observed_phone
            FROM source_entities
            WHERE canonical_person_id = ?
            AND source_type IN ({placeholders})
        """
        cursor = conn.execute(query, [person_id] + source_types)
    else:
        cursor = conn.execute("""
            SELECT id, source_type, source_id, observed_name, observed_email, observed_phone
            FROM source_entities
            WHERE canonical_person_id = ?
        """, (person_id,))

    results = [dict(row) for row in cursor]
    conn.close()
    return results


def move_source_entities(from_person_id: str, to_person_id: str, source_types: list[str], dry_run: bool = True) -> int:
    """Move source entities from one person to another."""
    crm_db = Path(__file__).parent.parent / "data" / "crm.db"
    conn = sqlite3.connect(crm_db)

    placeholders = ','.join('?' * len(source_types))

    if dry_run:
        cursor = conn.execute(f"""
            SELECT COUNT(*) FROM source_entities
            WHERE canonical_person_id = ?
            AND source_type IN ({placeholders})
        """, [from_person_id] + source_types)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    cursor = conn.execute(f"""
        UPDATE source_entities
        SET canonical_person_id = ?, linked_at = ?
        WHERE canonical_person_id = ?
        AND source_type IN ({placeholders})
    """, [to_person_id, datetime.now(timezone.utc).isoformat(), from_person_id] + source_types)

    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def move_interactions(from_person_id: str, to_person_id: str, source_types: list[str], dry_run: bool = True) -> int:
    """Move interactions from one person to another based on source types."""
    int_db = get_interaction_db_path()
    conn = sqlite3.connect(int_db)

    placeholders = ','.join('?' * len(source_types))

    if dry_run:
        cursor = conn.execute(f"""
            SELECT COUNT(*) FROM interactions
            WHERE person_id = ?
            AND source_type IN ({placeholders})
        """, [from_person_id] + source_types)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    cursor = conn.execute(f"""
        UPDATE interactions
        SET person_id = ?
        WHERE person_id = ?
        AND source_type IN ({placeholders})
    """, [to_person_id, from_person_id] + source_types)

    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def update_person_sources(person_id: str):
    """Update the sources list on a PersonEntity based on its source entities."""
    crm_db = Path(__file__).parent.parent / "data" / "crm.db"
    conn = sqlite3.connect(crm_db)

    cursor = conn.execute("""
        SELECT DISTINCT source_type FROM source_entities
        WHERE canonical_person_id = ?
    """, (person_id,))
    sources = [row[0] for row in cursor]
    conn.close()

    store = get_person_entity_store()
    person = store.get_by_id(person_id)
    if person:
        person.sources = sources
        store.update(person)
        store.save()

    return sources


def create_link_overrides(
    from_person_id: str,
    to_person_id: str,
    source_entities: list[dict],
    dry_run: bool = True
) -> int:
    """
    Create link override rules to ensure future sources are linked correctly.

    Analyzes the source entities being moved and creates rules based on
    common patterns (name + source_type + context).
    """
    if dry_run:
        return 0

    override_store = get_link_override_store()
    created = 0

    # Group by observed_name and source_type to find patterns
    patterns = {}
    for se in source_entities:
        name = se.get('observed_name', '')
        source_type = se.get('source_type', '')
        source_id = se.get('source_id', '')

        if not name:
            continue

        key = (name.lower(), source_type)
        if key not in patterns:
            patterns[key] = {
                'name': name,
                'source_type': source_type,
                'contexts': set(),
            }

        # Extract context from source_id (for vault sources)
        if source_type in ('vault', 'granola') and source_id:
            if 'Work/ML' in source_id:
                patterns[key]['contexts'].add('Work/ML/')
            elif 'Work/' in source_id:
                patterns[key]['contexts'].add('Work/')
            elif 'Personal/' in source_id:
                patterns[key]['contexts'].add('Personal/')

    # Create overrides for each pattern
    for (name_lower, source_type), pattern in patterns.items():
        # If we have specific context patterns, create targeted rules
        if pattern['contexts']:
            for context in pattern['contexts']:
                override = LinkOverride(
                    id=None,
                    name_pattern=pattern['name'],
                    source_type=source_type,
                    context_pattern=context,
                    preferred_person_id=to_person_id,
                    rejected_person_id=from_person_id,
                    reason=f"Split from person {from_person_id[:8]}",
                )
                override_store.add(override)
                created += 1
                logger.info(f"Created override: '{pattern['name']}' + {source_type} + '{context}' -> {to_person_id[:8]}")
        else:
            # General rule for this name + source_type
            override = LinkOverride(
                id=None,
                name_pattern=pattern['name'],
                source_type=source_type,
                context_pattern=None,
                preferred_person_id=to_person_id,
                rejected_person_id=from_person_id,
                reason=f"Split from person {from_person_id[:8]}",
            )
            override_store.add(override)
            created += 1
            logger.info(f"Created override: '{pattern['name']}' + {source_type} -> {to_person_id[:8]}")

    return created


def split_person(from_name: str, to_name: str, source_types: list[str],
                 new_person_name: str = None, create_overrides: bool = True,
                 dry_run: bool = True) -> dict:
    """
    Split source entities from one person to another.

    Args:
        from_name: Name of person to take sources from
        to_name: Name of person to give sources to, or "NEW" to create new
        source_types: List of source types to move (e.g., ["vault", "granola"])
        new_person_name: Name for new person if to_name is "NEW"
        dry_run: If True, don't make changes

    Returns:
        Stats dict
    """
    stats = {
        'source_entities_moved': 0,
        'interactions_moved': 0,
        'from_person': None,
        'to_person': None,
    }

    # Find source person
    from_person = find_person_by_name(from_name)
    if not from_person:
        logger.error(f"Could not find person matching '{from_name}'")
        return stats

    stats['from_person'] = {'id': from_person.id, 'name': from_person.canonical_name}
    logger.info(f"From person: {from_person.canonical_name} ({from_person.id[:8]})")

    # Find or create target person
    if to_name.upper() == "NEW":
        if not new_person_name:
            logger.error("Must specify --new-name when using --to-person NEW")
            return stats

        if dry_run:
            logger.info(f"Would create new person: {new_person_name}")
            stats['to_person'] = {'id': 'NEW', 'name': new_person_name}
        else:
            import uuid
            store = get_person_entity_store()
            to_person = PersonEntity(
                id=str(uuid.uuid4()),
                canonical_name=new_person_name,
                display_name=new_person_name,
                sources=[],
            )
            store.add(to_person)
            store.save()
            stats['to_person'] = {'id': to_person.id, 'name': to_person.canonical_name}
            logger.info(f"Created new person: {to_person.canonical_name} ({to_person.id})")

            # Verify the person was saved correctly
            verify_person = store.get_by_id(to_person.id)
            if not verify_person:
                logger.error(f"CRITICAL: Person {to_person.id} was not saved correctly!")
                raise RuntimeError(f"Failed to save new person: {to_person.canonical_name}")
    else:
        to_person = find_person_by_name(to_name)
        if not to_person:
            logger.error(f"Could not find person matching '{to_name}'")
            return stats
        stats['to_person'] = {'id': to_person.id, 'name': to_person.canonical_name}
        logger.info(f"To person: {to_person.canonical_name} ({to_person.id[:8]})")

    # Get source entities to be moved
    source_entities = get_source_entities_for_person(from_person.id, source_types)
    logger.info(f"\nSource entities to move ({len(source_entities)}):")
    for se in source_entities[:10]:
        logger.info(f"  {se['source_type']:12} | {se['observed_name']:30} | {se['source_id'][:50] if se['source_id'] else 'N/A'}")
    if len(source_entities) > 10:
        logger.info(f"  ... and {len(source_entities) - 10} more")

    # Move source entities
    to_id = stats['to_person']['id']
    if to_id != 'NEW':
        count = move_source_entities(from_person.id, to_id, source_types, dry_run)
        stats['source_entities_moved'] = count
        if dry_run:
            logger.info(f"\nWould move {count} source entities")
        else:
            logger.info(f"\nMoved {count} source entities")

    # Move interactions
    if to_id != 'NEW':
        count = move_interactions(from_person.id, to_id, source_types, dry_run)
        stats['interactions_moved'] = count
        if dry_run:
            logger.info(f"Would move {count} interactions")
        else:
            logger.info(f"Moved {count} interactions")

    # Update sources lists on both persons
    if not dry_run and to_id != 'NEW':
        from_sources = update_person_sources(from_person.id)
        to_sources = update_person_sources(to_id)
        logger.info(f"\nUpdated {from_person.canonical_name} sources: {from_sources}")
        logger.info(f"Updated {stats['to_person']['name']} sources: {to_sources}")

        # Refresh stats from InteractionStore for both affected people
        from api.services.person_stats import refresh_person_stats
        logger.info("Refreshing stats from InteractionStore...")
        refresh_person_stats([from_person.id, to_id])

        # Recalculate relationship strength for both affected people
        # (also updates is_peripheral_contact; dunbar_circle requires full recalc)
        from api.services.relationship_metrics import update_strength_for_person
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()

        # Reload from_person to get fresh stats for logging
        from_person = person_store.get_by_id(from_person.id)

        for person_id, name in [(from_person.id, from_person.canonical_name), (to_id, to_id)]:
            if person_id and person_id != 'NEW':
                old_person = person_store.get_by_id(person_id)
                old_strength = old_person.relationship_strength if old_person else None
                new_strength = update_strength_for_person(person_id)
                if new_strength is not None and new_strength != old_strength:
                    logger.info(f"   {name} strength: {old_strength} -> {new_strength}")

        person_store.save()

    # Create link overrides for durability
    if not dry_run and to_id != 'NEW' and create_overrides:
        override_count = create_link_overrides(
            from_person.id,
            to_id,
            source_entities,
            dry_run=False,
        )
        stats['overrides_created'] = override_count
        if override_count > 0:
            logger.info(f"\nCreated {override_count} link override rules for future sources")

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


def main():
    parser = argparse.ArgumentParser(description='Split incorrectly merged person entities')
    parser.add_argument('--from-person', required=True, help='Name of person to take sources from')
    parser.add_argument('--to-person', required=True, help='Name of person to give sources to, or NEW')
    parser.add_argument('--source-types', nargs='+', required=True, help='Source types to move (e.g., vault granola)')
    parser.add_argument('--new-name', help='Name for new person if --to-person is NEW')
    parser.add_argument('--no-overrides', action='store_true', help='Skip creating link override rules')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')

    args = parser.parse_args()

    split_person(
        from_name=args.from_person,
        to_name=args.to_person,
        source_types=args.source_types,
        new_person_name=args.new_name,
        create_overrides=not args.no_overrides,
        dry_run=not args.execute,
    )


if __name__ == '__main__':
    main()
