#!/usr/bin/env python3
"""
Apple Data Import — runs on the Linux server to import Apple ecosystem data.

Reads exported data from data/apple-imports/ (synced from Mac Mini via rsync)
and integrates it into the LifeOS data stores.

Usage:
    python scripts/apple_data_import.py --execute
    python scripts/apple_data_import.py --dry-run
    python scripts/apple_data_import.py --execute --source contacts

Import source: data/apple-imports/
    contacts.json       — Apple Contacts data
    imessage.db         — iMessage database
    phone_calls.json    — Phone/FaceTime call history
    manifest.json       — Export metadata
"""
import sys
import json
import shutil
import logging
import argparse
import uuid
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

IMPORT_DIR = PROJECT_ROOT / "data" / "apple-imports"


STALENESS_WARNING_HOURS = 48
STALENESS_CRITICAL_HOURS = 168  # 7 days


def check_manifest() -> dict | None:
    """Check the import manifest for freshness.

    Logs warnings/errors based on data age:
    - >48h: WARNING (picked up by nightly health batch)
    - >7d:  CRITICAL-level log (triggers immediate alert if server is running)
    """
    manifest_path = IMPORT_DIR / "manifest.json"
    if not manifest_path.exists():
        logger.warning("No manifest.json found in apple-imports/")
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    exported_at_str = manifest.get("exported_at", "")
    logger.info(f"Import data exported at: {exported_at_str} from {manifest.get('hostname', 'unknown')}")

    if exported_at_str:
        try:
            exported_at = datetime.fromisoformat(exported_at_str)
            if exported_at.tzinfo is None:
                exported_at = exported_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - exported_at
            age_hours = age.total_seconds() / 3600

            if age_hours > STALENESS_CRITICAL_HOURS:
                logger.critical(
                    f"Apple import data is {age.days} days old (exported {exported_at_str}). "
                    f"Check Mac Mini cron and rsync pipeline."
                )
            elif age_hours > STALENESS_WARNING_HOURS:
                logger.warning(
                    f"Apple import data is {age_hours:.0f}h old (exported {exported_at_str}). "
                    f"Expected refresh within {STALENESS_WARNING_HOURS}h."
                )
            else:
                logger.info(f"Apple import data is {age_hours:.0f}h old — fresh")
        except (ValueError, TypeError) as e:
            logger.warning(f"Cannot parse manifest exported_at: {e}")

    return manifest


def import_contacts(dry_run: bool = False) -> dict:
    """Import contacts from JSON export."""
    contacts_path = IMPORT_DIR / "contacts.json"
    if not contacts_path.exists():
        return {"status": "skipped", "reason": "contacts.json not found"}

    with open(contacts_path) as f:
        data = json.load(f)

    contacts = data.get("contacts", [])
    logger.info(f"Found {len(contacts)} contacts to import (exported {data.get('exported_at', '?')})")

    if dry_run:
        return {"status": "dry_run", "count": len(contacts)}

    from api.services.source_entity import (
        get_source_entity_store, SourceEntity, LINK_STATUS_AUTO,
    )
    from api.services.entity_resolver import get_entity_resolver
    from api.services.person_entity import get_person_entity_store

    se_store = get_source_entity_store()
    resolver = get_entity_resolver()
    pe_store = get_person_entity_store()

    created = 0
    updated = 0
    linked = 0

    for contact in contacts:
        identifier = contact.get("identifier", "")
        full_name = contact.get("full_name", "").strip()
        if not full_name:
            continue

        source_id = f"contacts:{identifier}"
        emails = [e["value"].lower() for e in contact.get("emails", []) if e.get("value")]
        phones = [p["value"] for p in contact.get("phones", []) if p.get("value")]

        # Create/update source entity using actual SourceEntity dataclass
        existing = se_store.get_by_source("contacts", source_id)

        entity = SourceEntity(
            source_type="contacts",
            source_id=source_id,
            observed_name=full_name,
            observed_email=emails[0] if emails else None,
            observed_phone=phones[0] if phones else None,
            metadata={"raw": contact, "emails": emails, "phones": phones},
        )

        if existing:
            entity.id = existing.id
            se_store.update(entity)
            updated += 1
        else:
            se_store.add(entity)
            created += 1

        # Resolve to person entity
        person = None
        for email in emails:
            person = resolver.resolve_by_email(email)
            if person:
                break
        if not person:
            for phone in phones:
                person = resolver.resolve_by_phone(phone)
                if person:
                    break

        if person:
            # Update person entity with contact data
            if contact.get("organization"):
                person.company = contact["organization"]
            if contact.get("job_title"):
                person.position = contact["job_title"]
            # Merge emails/phones (don't overwrite existing)
            for email in emails:
                if email not in person.emails:
                    person.emails.append(email)
            for phone in phones:
                if phone not in person.phone_numbers:
                    person.phone_numbers.append(phone)
            if contact.get("birthday"):
                try:
                    bday = datetime.fromisoformat(contact["birthday"])
                    person.birthday = bday.strftime("%m-%d")
                except (ValueError, TypeError):
                    pass

            pe_store.update(person)

            # Link source entity to person
            se_store.link_to_person(entity.id, person.id, confidence=0.95, status=LINK_STATUS_AUTO)
            linked += 1

    logger.info(f"Contacts: {created} created, {updated} updated, {linked} linked")
    return {"status": "ok", "created": created, "updated": updated, "linked": linked}


def import_imessage(dry_run: bool = False) -> dict:
    """Import iMessage database from export."""
    imessage_path = IMPORT_DIR / "imessage.db"
    if not imessage_path.exists():
        return {"status": "skipped", "reason": "imessage.db not found"}

    size_mb = imessage_path.stat().st_size / (1024 * 1024)
    logger.info(f"Found imessage.db ({size_mb:.1f} MB)")

    if dry_run:
        return {"status": "dry_run", "size_mb": round(size_mb, 1)}

    # Copy to the standard location where sync scripts expect it.
    # The iMessage linking and interaction sync scripts in the normal pipeline
    # (link_imessage, imessage in SYNC_ORDER) will process this database.
    dest = PROJECT_ROOT / "data" / "imessage.db"
    shutil.copy2(str(imessage_path), str(dest))
    logger.info(f"Copied imessage.db to {dest}")
    logger.info("iMessage data will be processed by link_imessage in the sync pipeline")

    return {"status": "ok", "size_mb": round(size_mb, 1)}


def import_phone_calls(dry_run: bool = False) -> dict:
    """Import phone call history from JSON export."""
    calls_path = IMPORT_DIR / "phone_calls.json"
    if not calls_path.exists():
        return {"status": "skipped", "reason": "phone_calls.json not found"}

    with open(calls_path) as f:
        data = json.load(f)

    calls = data.get("calls", [])
    logger.info(f"Found {len(calls)} calls to import")

    if dry_run:
        return {"status": "dry_run", "count": len(calls)}

    from api.services.interaction_store import get_interaction_store, Interaction

    store = get_interaction_store()
    imported = 0
    skipped = 0

    for call in calls:
        source_id = call.get("source_id", "")
        if not source_id:
            skipped += 1
            continue

        source_type = call.get("source_type", "phone_call")

        # Check if already exists
        existing = store.get_by_source(source_type, source_id)
        if existing:
            skipped += 1
            continue

        timestamp = None
        if call.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(call["timestamp"])
            except (ValueError, TypeError):
                skipped += 1
                continue

        interaction = Interaction(
            id=call.get("id") or str(uuid.uuid4()),
            person_id=call.get("person_id", ""),
            timestamp=timestamp,
            source_type=source_type,
            title=call.get("title", ""),
            snippet=call.get("snippet"),
            source_link=call.get("source_link", ""),
            source_id=source_id,
        )
        store.add_if_not_exists(interaction)
        imported += 1

    logger.info(f"Phone calls: {imported} imported, {skipped} skipped (existing/invalid)")
    return {"status": "ok", "imported": imported, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description="Import Apple ecosystem data from Mac Mini exports")
    parser.add_argument("--execute", action="store_true", help="Actually import")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--source", choices=["contacts", "imessage", "phone"], help="Import single source")
    args = parser.parse_args()

    if not args.execute and not args.dry_run:
        print("Use --execute to import or --dry-run to preview.")
        sys.exit(1)

    dry_run = not args.execute

    if not IMPORT_DIR.exists():
        logger.error(f"Import directory not found: {IMPORT_DIR}")
        logger.info("Run the Apple Data Agent on Mac Mini first, then rsync to this machine.")
        sys.exit(1)

    manifest = check_manifest()
    if not manifest and not dry_run:
        logger.warning("No manifest found — data may be stale or incomplete")

    sources = {
        "contacts": import_contacts,
        "imessage": import_imessage,
        "phone": import_phone_calls,
    }

    if args.source:
        sources = {args.source: sources[args.source]}

    results = {}
    for name, func in sources.items():
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Importing {name}...")
        try:
            results[name] = func(dry_run=dry_run)
        except Exception as e:
            logger.error(f"Failed to import {name}: {e}")
            results[name] = {"status": "error", "error": str(e)}

    print(json.dumps({"results": results}, indent=2))


if __name__ == "__main__":
    main()
