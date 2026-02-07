#!/usr/bin/env python3
"""
Fix LinkedIn data based on user review in docs/archive/linkedin_Matches.csv.

Clears linkedin_url, company, and position for entities marked as "no" (incorrect match).
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services.person_entity import PersonEntityStore

CSV_PATH = str(PROJECT_ROOT / 'docs/archive/linkedin_Matches.csv')

def main():
    store = PersonEntityStore()

    # Read CSV and find "no" matches
    bad_matches = []
    with open(CSV_PATH, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            match = row.get('Match', '').strip().lower()
            if match == 'no':
                # Extract name - remove leading number and period
                name = row.get('Name', '').strip()
                if name.startswith(tuple('0123456789')):
                    # Remove "1. " prefix
                    parts = name.split('. ', 1)
                    if len(parts) > 1:
                        name = parts[1]
                bad_matches.append({
                    'name': name,
                    'handle': row.get('Handle', '').strip(),
                    'company': row.get('Organization', '').strip(),
                })

    print(f"Found {len(bad_matches)} incorrect LinkedIn matches to fix:\n")

    fixed = 0
    not_found = []

    for match in bad_matches:
        name = match['name']
        entity = store.get_by_name(name)

        # Try without suffix if not found
        if not entity and ',' in name:
            name_without_suffix = name.split(',')[0].strip()
            entity = store.get_by_name(name_without_suffix)

        if not entity:
            not_found.append(match['name'])
            continue

        if not entity.linkedin_url:
            print(f"SKIP: {match['name']} - no LinkedIn URL set")
            continue

        # Verify the URL contains the expected handle
        if match['handle'].lower() not in entity.linkedin_url.lower():
            print(f"SKIP: {match['name']} - URL doesn't match expected handle '{match['handle']}'")
            print(f"       Current URL: {entity.linkedin_url}")
            continue

        print(f"FIXING: {match['name']}")
        print(f"  Clearing: {entity.linkedin_url}")
        if entity.company:
            print(f"  Clearing company: {entity.company}")
        if entity.position:
            print(f"  Clearing position: {entity.position}")

        entity.linkedin_url = None
        entity.company = None
        entity.position = None

        store.update(entity)
        fixed += 1

    if fixed > 0:
        store.save()
        print(f"\n✓ Fixed {fixed} entities")
    else:
        print("\nNo changes made")

    if not_found:
        print(f"\n⚠ Could not find {len(not_found)} entities:")
        for name in not_found:
            print(f"  - {name}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
