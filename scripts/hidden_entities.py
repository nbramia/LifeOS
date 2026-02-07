#!/usr/bin/env python3
"""
Export and import hidden entities for backup/restore.

Usage:
    python scripts/hidden_entities.py export          # Save current hidden entities to file
    python scripts/hidden_entities.py import          # Re-hide entities from backup file
    python scripts/hidden_entities.py list            # Show current hidden entities
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import get_person_entity_store

BACKUP_FILE = Path("data/hidden_entities_backup.json")


def export_hidden():
    """Export current hidden entities to backup file."""
    store = get_person_entity_store()
    all_entities = store.get_all(include_hidden=True, include_merged=True)

    hidden = [e for e in all_entities if e.hidden]

    backup_data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(hidden),
        "entities": [
            {
                "id": e.id,
                "canonical_name": e.canonical_name,
                "emails": e.emails,
                "hidden_reason": e.hidden_reason,
                "hidden_at": e.hidden_at.isoformat() if e.hidden_at else None,
            }
            for e in hidden
        ]
    }

    BACKUP_FILE.write_text(json.dumps(backup_data, indent=2))
    print(f"Exported {len(hidden)} hidden entities to {BACKUP_FILE}")


def import_hidden():
    """Re-hide entities from backup file."""
    if not BACKUP_FILE.exists():
        print(f"No backup file found at {BACKUP_FILE}")
        print("Run 'python scripts/hidden_entities.py export' first")
        return

    backup_data = json.loads(BACKUP_FILE.read_text())
    store = get_person_entity_store()

    restored = 0
    skipped = 0
    not_found = 0

    for entry in backup_data["entities"]:
        entity = store.get_by_id(entry["id"])
        if not entity:
            print(f"  Not found: {entry['canonical_name']} ({entry['id'][:8]}...)")
            not_found += 1
            continue

        if entity.hidden:
            skipped += 1
            continue

        # Re-hide with original reason
        reason = entry.get("hidden_reason", "restored from backup")
        store.hide_person(entry["id"], reason)
        print(f"  Hidden: {entity.canonical_name}")
        restored += 1

    print(f"\nRestored {restored} hidden entities")
    print(f"Skipped {skipped} (already hidden)")
    print(f"Not found: {not_found}")


def list_hidden():
    """List current hidden entities."""
    store = get_person_entity_store()
    all_entities = store.get_all(include_hidden=True, include_merged=True)

    hidden = [e for e in all_entities if e.hidden]
    hidden.sort(key=lambda x: x.hidden_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    print(f"Currently hidden: {len(hidden)} entities\n")
    for e in hidden:
        hidden_at = e.hidden_at.strftime("%Y-%m-%d %H:%M") if e.hidden_at else "unknown"
        print(f"  {hidden_at} | {e.canonical_name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "export":
        export_hidden()
    elif command == "import":
        import_hidden()
    elif command == "list":
        list_hidden()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)
