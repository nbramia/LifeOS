#!/usr/bin/env python3
"""
Fix vault interaction matches based on user review in docs/archive/vault_matches.csv.

Handles three types of corrections:
1. Simple reassignment: "Ben" -> "Ben Calvin"
2. Context-dependent: "Dan" -> "Dan Porter" if BlueLabs folder, "Dan McSwain" if Murm folder
3. Unlink: "No match" or "None" - remove person_id from interaction
"""

import csv
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services.person_entity import PersonEntityStore
from api.services.interaction_store import get_interaction_db_path

CSV_PATH = str(PROJECT_ROOT / 'docs/archive/vault_matches.csv')


def parse_correction(correction_text: str) -> dict:
    """
    Parse the correction text into actionable rules.

    Returns dict with:
        - type: 'simple', 'context', 'none'
        - target: person name for simple reassignment
        - rules: list of {folder_pattern, target} for context-dependent
    """
    if not correction_text or correction_text.strip() == '':
        return {'type': 'skip'}

    text = correction_text.strip()

    # Check for "No match", "None", or similar
    if text.lower() in ('no match', 'none', 'unmatched', 'unmatched if not'):
        return {'type': 'none'}

    # Check for context-dependent patterns like "X if in Y folder, Z if in W folder"
    # Pattern: "Name1 if in the Folder1 folder. Name2 if in the Folder2 folder"
    if ' if ' in text.lower():
        rules = []
        # Split on periods or commas followed by space and capital letter
        parts = re.split(r'[.,]\s+(?=[A-Z])', text)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Match patterns like "Dan Porter if in the BlueLabs folder"
            match = re.match(r"(.+?)\s+if\s+(?:it's\s+)?in\s+(?:the\s+)?(\w+)\s+folder", part, re.IGNORECASE)
            if match:
                target_name = match.group(1).strip()
                folder_pattern = match.group(2).strip()

                # Handle "Otherwise, no match" or "Unmatched if not"
                if 'no match' in target_name.lower() or 'unmatched' in target_name.lower():
                    rules.append({'folder': None, 'target': None, 'is_default': True})
                else:
                    rules.append({'folder': folder_pattern.lower(), 'target': target_name})
            elif 'otherwise' in part.lower() and 'no match' in part.lower():
                rules.append({'folder': None, 'target': None, 'is_default': True})

        if rules:
            return {'type': 'context', 'rules': rules}

    # Check for simple name with parenthetical note (ignore the note)
    # e.g., "Ben Calvin" or "Hayley Currier" or "Kevin Perez (there is a person...)"
    if '(' in text:
        text = text.split('(')[0].strip()

    # Simple reassignment
    if text:
        return {'type': 'simple', 'target': text}

    return {'type': 'skip'}


def find_person_id(store: PersonEntityStore, name: str) -> str | None:
    """Find person ID by name."""
    entity = store.get_by_name(name)
    if entity:
        return entity.id

    # Try without suffix
    if ',' in name:
        entity = store.get_by_name(name.split(',')[0].strip())
        if entity:
            return entity.id

    return None


def main():
    store = PersonEntityStore()
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    # Read CSV
    corrections = []
    with open(CSV_PATH, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            source_name = row.get('Name', '').strip()
            correction = row.get('Correct match', '').strip()

            if not source_name or not correction:
                continue

            parsed = parse_correction(correction)
            if parsed['type'] != 'skip':
                corrections.append({
                    'source_name': source_name,
                    'correction': parsed,
                    'original_text': correction,
                })

    print(f"Processing {len(corrections)} vault match corrections...\n")

    stats = {'fixed': 0, 'unlinked': 0, 'skipped': 0, 'errors': []}
    affected_person_ids: set[str] = set()  # Track for stats refresh

    for corr in corrections:
        source_name = corr['source_name']
        parsed = corr['correction']

        # Find source entity to get its current person_id
        source_entity = store.get_by_name(source_name)
        if not source_entity:
            # Try without suffix
            if ',' in source_name:
                source_entity = store.get_by_name(source_name.split(',')[0].strip())

        if not source_entity:
            print(f"SKIP: Could not find source entity '{source_name}'")
            stats['skipped'] += 1
            continue

        source_id = source_entity.id

        # Get all vault interactions for this entity
        cursor = conn.execute("""
            SELECT id, source_id, title FROM interactions
            WHERE person_id = ? AND source_type IN ('vault', 'granola')
        """, (source_id,))
        interactions = cursor.fetchall()

        if not interactions:
            print(f"SKIP: No vault interactions for '{source_name}'")
            stats['skipped'] += 1
            continue

        print(f"\n{source_name}: {len(interactions)} vault interactions")
        print(f"  Correction: {corr['original_text'][:60]}...")

        if parsed['type'] == 'simple':
            # Simple reassignment - move all interactions to target person
            target_name = parsed['target']
            target_id = find_person_id(store, target_name)

            if not target_id:
                print(f"  ERROR: Could not find target '{target_name}'")
                stats['errors'].append(f"{source_name} -> {target_name}")
                continue

            if target_id == source_id:
                print(f"  SKIP: Already correct (same person)")
                stats['skipped'] += 1
                continue

            conn.execute("""
                UPDATE interactions SET person_id = ?
                WHERE person_id = ? AND source_type IN ('vault', 'granola')
            """, (target_id, source_id))
            affected_person_ids.add(source_id)
            affected_person_ids.add(target_id)
            print(f"  -> Reassigned {len(interactions)} to '{target_name}'")
            stats['fixed'] += len(interactions)

        elif parsed['type'] == 'context':
            # Context-dependent - check each interaction's path
            rules = parsed['rules']
            fixed_count = 0
            unlinked_count = 0

            for int_id, source_path, title in interactions:
                path_lower = source_path.lower() if source_path else ''
                matched = False

                for rule in rules:
                    if rule.get('is_default'):
                        continue  # Handle default last

                    folder = rule['folder']
                    if folder and folder in path_lower:
                        target_name = rule['target']
                        target_id = find_person_id(store, target_name)

                        if target_id and target_id != source_id:
                            conn.execute("UPDATE interactions SET person_id = ? WHERE id = ?",
                                       (target_id, int_id))
                            affected_person_ids.add(source_id)
                            affected_person_ids.add(target_id)
                            fixed_count += 1
                        matched = True
                        break

                # Apply default rule if no match
                if not matched:
                    has_default = any(r.get('is_default') for r in rules)
                    if has_default:
                        # Delete the incorrectly linked interaction
                        conn.execute("DELETE FROM interactions WHERE id = ?", (int_id,))
                        affected_person_ids.add(source_id)
                        unlinked_count += 1

            if fixed_count:
                print(f"  -> Reassigned {fixed_count} based on folder context")
                stats['fixed'] += fixed_count
            if unlinked_count:
                print(f"  -> Unlinked {unlinked_count} (no matching folder)")
                stats['unlinked'] += unlinked_count

        elif parsed['type'] == 'none':
            # Delete all incorrectly linked interactions
            conn.execute("""
                DELETE FROM interactions
                WHERE person_id = ? AND source_type IN ('vault', 'granola')
            """, (source_id,))
            affected_person_ids.add(source_id)
            print(f"  -> Deleted {len(interactions)} (no match)")
            stats['unlinked'] += len(interactions)

    conn.commit()
    conn.close()

    # Refresh PersonEntity stats for all affected people
    if affected_person_ids:
        from api.services.person_stats import refresh_person_stats
        print(f"\nRefreshing stats for {len(affected_person_ids)} affected people...")
        refresh_person_stats(list(affected_person_ids))

    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Fixed (reassigned): {stats['fixed']}")
    print(f"  Unlinked: {stats['unlinked']}")
    print(f"  Skipped: {stats['skipped']}")
    if stats['errors']:
        print(f"  Errors: {len(stats['errors'])}")
        for err in stats['errors']:
            print(f"    - {err}")

    return 0 if not stats['errors'] else 1


if __name__ == "__main__":
    sys.exit(main())
