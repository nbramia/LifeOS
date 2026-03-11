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
import uuid
import sqlite3
import plistlib
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

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


def _parse_abcdp_labeled(field_dict: dict | None) -> list[dict]:
    """Parse an AddressBook labeled-value dict (Email, Phone, etc.) into [{label, value}]."""
    if not field_dict or not isinstance(field_dict, dict):
        return []
    values = field_dict.get("values") or []
    labels = field_dict.get("labels") or []
    result = []
    for i, val in enumerate(values):
        if not val:
            continue
        label = labels[i] if i < len(labels) else ""
        # Strip Apple label markers: _$!<Mobile>!$_ → Mobile
        if label.startswith("_$!<") and label.endswith(">!$_"):
            label = label[4:-4]
        result.append({"label": label or "other", "value": str(val)})
    return result


def _parse_abcdp_contact(plist_data: dict, identifier: str) -> dict | None:
    """Parse a single .abcdp plist into the contacts.json format."""
    first = plist_data.get("First", "")
    last = plist_data.get("Last", "")
    organization = plist_data.get("Organization", "")

    # Skip contacts with no useful name
    if not first and not last and not organization:
        return None

    full_name = " ".join(p for p in [first, last] if p)
    if not full_name:
        full_name = organization

    emails = _parse_abcdp_labeled(plist_data.get("Email"))
    phones = _parse_abcdp_labeled(plist_data.get("Phone"))

    birthday = None
    bd = plist_data.get("Birthday")
    if isinstance(bd, datetime):
        birthday = bd.isoformat()
    elif hasattr(bd, "isoformat"):
        birthday = bd.isoformat()

    return {
        "identifier": identifier,
        "given_name": first,
        "family_name": last,
        "full_name": full_name,
        "nickname": plist_data.get("Nickname", ""),
        "organization": organization,
        "job_title": plist_data.get("JobTitle", ""),
        "department": plist_data.get("Department", ""),
        "emails": emails,
        "phones": phones,
        "addresses": [],  # Postal addresses have complex plist structure; omit for now
        "social_profiles": [],
        "note": "",  # Notes may contain sensitive data; omit from export
        "image_available": False,
        "birthday": birthday,
    }


def export_contacts(dry_run: bool = False) -> dict:
    """Export Apple Contacts by reading .abcdp plist files directly.

    Reads from ~/Library/Application Support/AddressBook/Sources/*/Metadata/
    which requires Full Disk Access but NOT the Contacts TCC permission.
    This works over SSH and in cron — no per-app Contacts grant needed.
    """
    addressbook_dir = Path.home() / "Library" / "Application Support" / "AddressBook"
    if not addressbook_dir.exists():
        logger.warning(f"AddressBook directory not found: {addressbook_dir}")
        return {"status": "skipped", "reason": "AddressBook not found"}

    # Glob all .abcdp person files across all sources
    abcdp_files = list(addressbook_dir.glob("Sources/*/Metadata/*:ABPerson.abcdp"))
    logger.info(f"Found {len(abcdp_files)} .abcdp contact files")

    if not abcdp_files:
        return {"status": "ok", "count": 0, "path": ""}

    if dry_run:
        return {"status": "dry_run", "count": len(abcdp_files)}

    # Parse each file; deduplicate by identifier (UUID from filename)
    seen_ids: set[str] = set()
    export = []
    errors = 0
    for path in abcdp_files:
        try:
            # Filename format: <UUID>:ABPerson.abcdp
            identifier = path.name.split(":")[0]
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)

            with open(path, "rb") as f:
                plist_data = plistlib.load(f)

            contact = _parse_abcdp_contact(plist_data, identifier)
            if contact:
                export.append(contact)
        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning(f"Error parsing {path.name}: {e}")

    if errors:
        logger.warning(f"Skipped {errors} contacts due to parse errors")

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
    """Export iMessage database.

    Calls IMessageStore.export_from_source() to populate data/imessage.db
    from Apple's Messages.db (requires FDA), then copies it to exports.
    """
    from api.services.imessage import get_imessage_store

    imessage_db = PROJECT_ROOT / "data" / "imessage.db"

    # Populate imessage.db from Apple's Messages database
    try:
        store = get_imessage_store()
        export_stats = store.export_from_source()
        logger.info(f"Exported {export_stats.get('messages_exported', 0)} new messages from Messages.app")
    except FileNotFoundError:
        logger.warning("Apple Messages database not found — skipping iMessage export")
        return {"status": "skipped", "reason": "Messages.db not found"}
    except PermissionError:
        logger.warning("Cannot access Messages.db — grant Full Disk Access to Terminal")
        return {"status": "skipped", "reason": "Full Disk Access required"}

    if not imessage_db.exists():
        logger.warning("imessage.db not found after export")
        return {"status": "skipped", "reason": "imessage.db not found after export"}

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

    Reads directly from Apple's CallHistoryDB (requires FDA).
    Produces the same JSON format that apple_data_import.import_phone_calls() expects.
    """
    # macOS Core Data epoch: 2001-01-01 00:00:00 UTC
    CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

    CALL_TYPE_NAMES = {
        1: "Phone",
        8: "FaceTime Audio",
        16: "FaceTime Video",
    }

    # Must use "phone" to match sync_phone_calls.py and relationship_discovery.py
    SOURCE_TYPE_MAP = {
        1: "phone",
        8: "phone",
        16: "phone",
    }

    def normalize_phone(phone: str) -> str:
        if not phone:
            return ""
        if "@" in phone:
            return ""
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 10:
            return f"+1{digits}"
        elif len(digits) == 11 and digits.startswith('1'):
            return f"+{digits}"
        elif len(digits) > 10:
            return f"+{digits}"
        return ""

    def format_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

    callhistory_path = Path.home() / "Library/Application Support/CallHistoryDB/CallHistory.storedata"
    if not callhistory_path.exists():
        logger.warning(f"CallHistoryDB not found at {callhistory_path}")
        return {"status": "skipped", "reason": "CallHistoryDB not found"}

    try:
        conn = sqlite3.connect(f"file:{callhistory_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        if "unable to open" in str(e):
            logger.warning("Cannot access CallHistoryDB — grant Full Disk Access to Terminal")
            return {"status": "skipped", "reason": "Full Disk Access required"}
        raise

    query = """
        SELECT
            ZUNIQUE_ID,
            ZDATE,
            ZDURATION,
            ZADDRESS,
            ZNAME,
            ZORIGINATED,
            ZANSWERED,
            ZCALLTYPE
        FROM ZCALLRECORD
        ORDER BY ZDATE DESC
    """

    cursor = conn.execute(query)
    calls = cursor.fetchall()
    conn.close()

    logger.info(f"Found {len(calls)} calls in CallHistoryDB")

    if dry_run:
        return {"status": "dry_run", "count": len(calls)}

    export = []
    errors = 0
    for row in calls:
        try:
            unique_id, zdate, duration, address, name, originated, answered, call_type = row

            if zdate is None:
                continue

            phone = normalize_phone(address)
            if not phone:
                continue

            timestamp = CORE_DATA_EPOCH + timedelta(seconds=zdate)

            direction = "Outgoing" if originated else "Incoming"
            status = "answered" if answered else "missed"
            call_type_name = CALL_TYPE_NAMES.get(call_type, "Call")
            contact_name = name or phone
            source_type = SOURCE_TYPE_MAP.get(call_type, "phone")

            if duration and duration > 0:
                title = f"{direction} {call_type_name} with {contact_name} ({format_duration(duration)})"
            else:
                title = f"{direction} {call_type_name} ({status}) - {contact_name}"

            export.append({
                "id": str(uuid.uuid4()),
                "source_id": unique_id,
                "source_type": source_type,
                "person_id": "",
                "timestamp": timestamp.isoformat(),
                "title": title,
                "snippet": None,
                "source_link": "",
            })
        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning(f"Skipping call row: {e}")

    if errors:
        logger.warning(f"Skipped {errors} calls due to errors")

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
