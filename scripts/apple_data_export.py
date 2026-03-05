#!/usr/bin/env python3
"""
Apple Data Export — runs on Mac Mini to export Apple ecosystem data.

Exports iMessage, phone calls, and contacts to portable files that the
Linux server can import. This is the "Apple Data Agent" side of the bridge.

Usage:
    python scripts/apple_data_export.py --execute           # Full export
    python scripts/apple_data_export.py --execute --source imessage  # Single source
    python scripts/apple_data_export.py --dry-run           # Preview only

Exported to: data/apple-exports/
    contacts.json       — Apple Contacts data
    imessage.db         — Copy of the local iMessage cache
    phone_calls.json    — Phone/FaceTime call history

The Linux server picks these up from data/apple-imports/ (via rsync).
"""
import sys
import json
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

EXPORT_DIR = PROJECT_ROOT / "data" / "apple-exports"


def export_contacts(dry_run: bool = False) -> dict:
    """Export Apple Contacts to JSON."""
    try:
        from api.services.apple_contacts import get_contacts_reader
    except ImportError:
        logger.warning("pyobjc not available — skipping contacts export")
        return {"status": "skipped", "reason": "pyobjc not available"}

    reader = get_contacts_reader()
    if reader is None:
        logger.warning("Contacts reader not available — skipping")
        return {"status": "skipped", "reason": "Contacts framework not available"}

    contacts = reader.fetch_all()
    logger.info(f"Found {len(contacts)} contacts")

    if dry_run:
        return {"status": "dry_run", "count": len(contacts)}

    # Serialize contacts to JSON
    export = []
    for c in contacts:
        export.append({
            "identifier": c.identifier,
            "given_name": c.given_name,
            "family_name": c.family_name,
            "full_name": c.full_name,
            "nickname": c.nickname,
            "organization": c.organization,
            "job_title": c.job_title,
            "department": c.department,
            "emails": c.emails,
            "phones": c.phones,
            "addresses": c.addresses,
            "social_profiles": c.social_profiles,
            "note": c.note,
            "image_available": c.image_available,
            "birthday": c.birthday.isoformat() if c.birthday else None,
        })

    out_path = EXPORT_DIR / "contacts.json"
    with open(out_path, "w") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(export),
            "contacts": export,
        }, f, indent=2)

    logger.info(f"Exported {len(export)} contacts to {out_path}")
    return {"status": "ok", "count": len(export), "path": str(out_path)}


def export_imessage(dry_run: bool = False) -> dict:
    """Export iMessage database (copy the local cache)."""
    imessage_db = PROJECT_ROOT / "data" / "imessage.db"
    if not imessage_db.exists():
        logger.warning("imessage.db not found — run FDA sync first")
        return {"status": "skipped", "reason": "imessage.db not found"}

    if dry_run:
        size_mb = imessage_db.stat().st_size / (1024 * 1024)
        return {"status": "dry_run", "size_mb": round(size_mb, 1)}

    # Copy the database file (it's already in portable SQLite format)
    out_path = EXPORT_DIR / "imessage.db"
    shutil.copy2(str(imessage_db), str(out_path))

    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info(f"Exported imessage.db ({size_mb:.1f} MB) to {out_path}")
    return {"status": "ok", "size_mb": round(size_mb, 1), "path": str(out_path)}


def export_phone_calls(dry_run: bool = False) -> dict:
    """Export phone call history to JSON.

    Reads from the interaction store (phone calls were already synced by FDA sync).
    """
    try:
        from api.services.interaction_store import get_interaction_store
    except ImportError:
        logger.warning("interaction_store not available — skipping phone export")
        return {"status": "skipped", "reason": "import error"}

    store = get_interaction_store()
    # Get all phone/facetime interactions
    calls = store.search(source_type="phone_call", limit=50000)
    calls += store.search(source_type="facetime_audio", limit=50000)
    calls += store.search(source_type="facetime_video", limit=50000)

    logger.info(f"Found {len(calls)} phone/FaceTime interactions")

    if dry_run:
        return {"status": "dry_run", "count": len(calls)}

    export = []
    for call in calls:
        export.append({
            "source_id": call.source_id,
            "source_type": call.source_type,
            "person_entity_id": call.person_entity_id,
            "timestamp": call.timestamp.isoformat() if call.timestamp else None,
            "title": call.title,
            "content": call.content,
            "metadata": call.metadata if hasattr(call, 'metadata') else {},
        })

    out_path = EXPORT_DIR / "phone_calls.json"
    with open(out_path, "w") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(export),
            "calls": export,
        }, f, indent=2)

    logger.info(f"Exported {len(export)} calls to {out_path}")
    return {"status": "ok", "count": len(export), "path": str(out_path)}


def main():
    parser = argparse.ArgumentParser(description="Export Apple ecosystem data")
    parser.add_argument("--execute", action="store_true", help="Actually export (not dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be exported")
    parser.add_argument("--source", choices=["contacts", "imessage", "phone"], help="Export single source")
    args = parser.parse_args()

    if not args.execute and not args.dry_run:
        print("Use --execute to export or --dry-run to preview.")
        sys.exit(1)

    dry_run = not args.execute

    # Create export directory
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    if sys.platform != "darwin":
        logger.error("This script must run on macOS (Apple data sources not available)")
        sys.exit(1)

    sources = {
        "contacts": export_contacts,
        "imessage": export_imessage,
        "phone": export_phone_calls,
    }

    if args.source:
        sources = {args.source: sources[args.source]}

    results = {}
    for name, func in sources.items():
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Exporting {name}...")
        try:
            results[name] = func(dry_run=dry_run)
        except Exception as e:
            logger.error(f"Failed to export {name}: {e}")
            results[name] = {"status": "error", "error": str(e)}

    # Write manifest
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "hostname": __import__("socket").gethostname(),
        "results": results,
    }

    if not dry_run:
        manifest_path = EXPORT_DIR / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        logger.info(f"Manifest written to {manifest_path}")

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
