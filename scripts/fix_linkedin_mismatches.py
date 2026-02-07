#!/usr/bin/env python3
"""
Fix LinkedIn data that was incorrectly matched to person entities.

These are cases where the LinkedIn username clearly doesn't match the
person's canonical name - typically caused by the old token_set_ratio
algorithm being too permissive.

This script clears linkedin_url, company, and position fields for
identified bad matches.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import PersonEntityStore

# List of people with confirmed bad LinkedIn data
# Format: (canonical_name, expected_linkedin_username_fragment, reason)
BAD_MATCHES = [
    ("Madeline Eden", "denismikush", "Completely different name"),
    ("Tamara Miller", "liliomere", "Completely different name"),
    ("Samuel Miller", "amitpatel2008", "Completely different name - Amit Patel"),
    ("Sarah Long, CLC, CSC", "val-sanin", "Completely different name"),
    # Add more as needed
]

def main():
    store = PersonEntityStore()
    fixed = 0
    errors = 0

    for canonical_name, bad_username, reason in BAD_MATCHES:
        entity = store.get_by_name(canonical_name)

        if not entity:
            # Try without suffix
            parts = canonical_name.split(',')
            entity = store.get_by_name(parts[0].strip())

        if not entity:
            print(f"SKIP: Could not find '{canonical_name}'")
            continue

        if not entity.linkedin_url:
            print(f"SKIP: {canonical_name} - no LinkedIn URL set")
            continue

        if bad_username.lower() not in entity.linkedin_url.lower():
            print(f"SKIP: {canonical_name} - LinkedIn URL doesn't contain '{bad_username}'")
            print(f"       Current URL: {entity.linkedin_url}")
            continue

        print(f"FIXING: {canonical_name}")
        print(f"  Reason: {reason}")
        print(f"  Old LinkedIn: {entity.linkedin_url}")
        print(f"  Old Company: {entity.company}")
        print(f"  Old Position: {entity.position}")

        # Clear the bad data
        entity.linkedin_url = None
        entity.company = None
        entity.position = None

        store.update(entity)
        fixed += 1
        print(f"  --> Cleared!")
        print()

    if fixed > 0:
        store.save()
        print(f"\nSaved {fixed} fixes to database.")
    else:
        print("\nNo changes made.")

    return 0 if errors == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
