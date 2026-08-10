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
import subprocess
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


def _read_abcdp_contacts(addressbook_dir: Path) -> list[dict]:
    """Read contacts from legacy per-person .abcdp plist files, if any exist.

    On current macOS this glob matches nothing (contacts moved to the
    AddressBook-v22.abcddb SQLite databases — see issue #514) but older
    macOS versions still write one .abcdp file per contact under
    Sources/*/Metadata/, so this path is kept as a fallback.
    """
    abcdp_files = list(addressbook_dir.glob("Sources/*/Metadata/*:ABPerson.abcdp"))
    logger.info(f"Found {len(abcdp_files)} .abcdp contact files")
    if not abcdp_files:
        return []

    seen_ids: set[str] = set()
    contacts = []
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
                contacts.append(contact)
        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning(f"Error parsing {path.name}: {e}")

    if errors:
        logger.warning(f"Skipped {errors} .abcdp contacts due to parse errors")

    return contacts


def _strip_abcddb_label(label: str | None) -> str:
    """Strip AddressBook's plist-style label wrapper: _$!<Mobile>!$_ -> Mobile."""
    label = label or ""
    if label.startswith("_$!<") and label.endswith(">!$_"):
        label = label[4:-4]
    return label or "other"


def _find_abcddb_paths(addressbook_dir: Path) -> list[Path]:
    """List every AddressBook-v22.abcddb: the root DB plus one per account source.

    The root DB (directly under AddressBook/) is typically empty — each
    account source (iCloud, Exchange, "On My Mac", ...) keeps its own copy
    under Sources/<uuid>/. Both are read the same way.
    """
    candidates = [addressbook_dir / "AddressBook-v22.abcddb"]
    candidates += sorted(addressbook_dir.glob("Sources/*/AddressBook-v22.abcddb"))
    return [p for p in candidates if p.exists()]


def _fetch_abcddb_contacts(db_path: Path) -> list[dict]:
    """Read contacts (with phones/emails) from one AddressBook-v22.abcddb file.

    ZABCDRECORD is a shared table for several Core Data entity subtypes
    (contacts, groups, containers, ...); the contact rows are the ones whose
    Z_ENT matches the 'ABCDContact' entity registered in Z_PRIMARYKEY. That
    id isn't a stable literal across macOS versions, so it's looked up by
    name rather than hardcoded. Phone numbers and email addresses live in
    separate tables keyed by ZOWNER = ZABCDRECORD.Z_PK.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as e:
        logger.warning(f"Cannot open {db_path}: {e}")
        return []

    try:
        ent_row = conn.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ABCDContact'"
        ).fetchone()
        if not ent_row:
            return []
        contact_ent = ent_row["Z_ENT"]

        records = conn.execute(
            """
            SELECT Z_PK, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION,
                   ZNICKNAME, ZJOBTITLE, ZDEPARTMENT, ZBIRTHDAY
            FROM ZABCDRECORD
            WHERE Z_ENT = ?
            ORDER BY Z_PK
            """,
            (contact_ent,),
        ).fetchall()

        phones_by_owner: dict[int, list[dict]] = {}
        for row in conn.execute("SELECT ZOWNER, ZLABEL, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
            if not row["ZFULLNUMBER"]:
                continue
            phones_by_owner.setdefault(row["ZOWNER"], []).append(
                {"label": _strip_abcddb_label(row["ZLABEL"]), "value": row["ZFULLNUMBER"]}
            )

        emails_by_owner: dict[int, list[dict]] = {}
        for row in conn.execute("SELECT ZOWNER, ZLABEL, ZADDRESS FROM ZABCDEMAILADDRESS"):
            if not row["ZADDRESS"]:
                continue
            emails_by_owner.setdefault(row["ZOWNER"], []).append(
                {"label": _strip_abcddb_label(row["ZLABEL"]), "value": row["ZADDRESS"]}
            )
    except sqlite3.DatabaseError as e:
        logger.warning(f"Error reading {db_path}: {e}")
        return []
    finally:
        conn.close()

    # Core Data reference date: seconds since 2001-01-01, same epoch used
    # elsewhere in this file (e.g. export_phone_calls' CORE_DATA_EPOCH).
    core_data_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)

    contacts = []
    for r in records:
        first = r["ZFIRSTNAME"] or ""
        last = r["ZLASTNAME"] or ""
        organization = r["ZORGANIZATION"] or ""
        if not first and not last and not organization:
            continue

        full_name = " ".join(p for p in [first, last] if p) or organization

        birthday = None
        bd = r["ZBIRTHDAY"]
        if bd is not None:
            try:
                birthday = (core_data_epoch + timedelta(seconds=bd)).isoformat()
            except (OverflowError, OSError, ValueError):
                birthday = None

        contacts.append({
            "identifier": f"abcddb:{r['ZUNIQUEID'] or r['Z_PK']}",
            "given_name": first,
            "family_name": last,
            "full_name": full_name,
            "nickname": r["ZNICKNAME"] or "",
            "organization": organization,
            "job_title": r["ZJOBTITLE"] or "",
            "department": r["ZDEPARTMENT"] or "",
            "emails": emails_by_owner.get(r["Z_PK"], []),
            "phones": phones_by_owner.get(r["Z_PK"], []),
            "addresses": [],  # Postal addresses have complex plist structure; omit for now
            "social_profiles": [],
            "note": "",  # Notes may contain sensitive data; omit from export
            "image_available": False,
            "birthday": birthday,
        })

    return contacts


def _read_abcddb_contacts(addressbook_dir: Path) -> list[dict]:
    """Read contacts from every AddressBook-v22.abcddb (root + each account source)."""
    contacts: list[dict] = []
    for db_path in _find_abcddb_paths(addressbook_dir):
        contacts.extend(_fetch_abcddb_contacts(db_path))
    return contacts


def _normalize_contact_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().casefold())


def _dedupe_contacts(contacts: list[dict]) -> list[dict]:
    """Merge contacts that appear in more than one account source.

    The same real contact is commonly synced into several account sources
    (iCloud, Exchange, "On My Mac", ...) and shows up as a separate row in
    each. The schema has no reliable cross-source join key — ZLINKID doesn't
    span sources and ZUNIQUEID is generated per-source — so contacts are
    grouped by normalized full name (falling back to organization for
    company-only cards, which have no personal name). That's the field most
    likely to be identical across a synced copy of the same real contact,
    and it's simple: no fuzzy phone/email matching heuristics to get wrong.
    Contacts with neither a usable name nor organization can't be grouped
    and pass through unmerged.

    Merging unions emails/phones by value and backfills any other field
    that's empty on the first-seen copy, so no data from either source is
    lost.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    passthrough: list[dict] = []

    for contact in contacts:
        key = _normalize_contact_key(contact.get("full_name") or contact.get("organization") or "")
        if not key:
            passthrough.append(contact)
            continue

        if key not in merged:
            target = dict(contact)
            target["emails"] = list(contact.get("emails") or [])
            target["phones"] = list(contact.get("phones") or [])
            merged[key] = target
            order.append(key)
            continue

        target = merged[key]
        seen_emails = {e["value"].lower() for e in target["emails"]}
        for e in contact.get("emails") or []:
            if e["value"].lower() not in seen_emails:
                target["emails"].append(e)
                seen_emails.add(e["value"].lower())

        seen_phones = {p["value"] for p in target["phones"]}
        for p in contact.get("phones") or []:
            if p["value"] not in seen_phones:
                target["phones"].append(p)
                seen_phones.add(p["value"])

        for field in ("organization", "job_title", "department", "nickname", "birthday", "given_name", "family_name"):
            if not target.get(field) and contact.get(field):
                target[field] = contact[field]

    return [merged[k] for k in order] + passthrough


def export_contacts(dry_run: bool = False) -> dict:
    """Export Apple Contacts by reading AddressBook's local files directly.

    Contacts currently live in AddressBook-v22.abcddb SQLite databases (the
    root DB plus one per account source under Sources/*/) — see issue #514.
    Older macOS instead wrote one .abcdp plist per contact under
    Sources/*/Metadata/, which is kept as a fallback for that case.

    Either way this reads files directly rather than going through the
    Contacts framework, which needs only Full Disk Access — not the
    per-app Contacts TCC grant — so it works over SSH and in cron.
    """
    addressbook_dir = Path.home() / "Library" / "Application Support" / "AddressBook"
    if not addressbook_dir.exists():
        logger.warning(f"AddressBook directory not found: {addressbook_dir}")
        return {"status": "skipped", "reason": "AddressBook not found"}

    contacts = _read_abcddb_contacts(addressbook_dir)
    source_method = "abcddb"
    if not contacts:
        contacts = _read_abcdp_contacts(addressbook_dir)
        source_method = "abcdp"

    contacts = _dedupe_contacts(contacts)
    logger.info(f"Found {len(contacts)} contacts via {source_method} (after cross-source dedup)")

    if not contacts:
        return {
            "status": "error",
            "count": 0,
            "path": "",
            "reason": "no contacts found via abcddb or abcdp",
        }

    if dry_run:
        return {"status": "dry_run", "count": len(contacts)}

    out_path = EXPORT_DIR / "contacts.json"
    with open(out_path, "w") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(contacts),
            "contacts": contacts,
        }, f, indent=2)

    logger.info(f"Exported {len(contacts)} contacts to {out_path}")
    return {"status": "ok", "count": len(contacts), "path": str(out_path)}


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


def export_photos_faces(dry_run: bool = False) -> dict:
    """Export Photos face recognition data as JSON.

    Reads directly from Apple's Photos.sqlite database (requires the Photos
    library to be mounted). Exports named people and their face appearances
    so the Linux server can create SourceEntity/Interaction records without
    needing access to the Photos library.
    """
    # Seconds from Unix epoch to Apple epoch (2001-01-01)
    APPLE_EPOCH_OFFSET = 978307200

    # Try common Photos library locations
    photos_db = None
    candidates = [
        Path.home() / "Pictures" / "Photos Library.photoslibrary" / "database" / "Photos.sqlite",
        Path("/Volumes/NVMe External Storage/Photos Library.photoslibrary/database/Photos.sqlite"),
    ]
    for candidate in candidates:
        if candidate.exists():
            photos_db = candidate
            break

    if not photos_db:
        logger.warning("Photos.sqlite not found at any known location")
        return {"status": "skipped", "reason": "Photos.sqlite not found"}

    logger.info(f"Reading Photos database: {photos_db}")

    try:
        conn = sqlite3.connect(f"file:{photos_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as e:
        logger.warning(f"Cannot open Photos database: {e}")
        return {"status": "skipped", "reason": str(e)}

    # Get named people with contact links
    people_rows = conn.execute("""
        SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT, ZPERSONURI
        FROM ZPERSON
        WHERE ZFULLNAME IS NOT NULL
          AND ZFACECOUNT > 0
          AND ZPERSONURI IS NOT NULL
        ORDER BY ZFACECOUNT DESC
    """).fetchall()

    logger.info(f"Found {len(people_rows)} Photos people linked to contacts")

    if dry_run:
        conn.close()
        return {"status": "dry_run", "people": len(people_rows)}

    people_export = []
    faces_export = []
    errors = 0

    for person in people_rows:
        contact_uri = person["ZPERSONURI"]
        contact_uuid = contact_uri.replace(":ABPerson", "") if ":ABPerson" in contact_uri else None

        people_export.append({
            "photos_pk": person["Z_PK"],
            "full_name": person["ZFULLNAME"],
            "display_name": person["ZDISPLAYNAME"],
            "face_count": person["ZFACECOUNT"] or 0,
            "contact_uuid": contact_uuid,
            "contact_uri": contact_uri,
        })

        # Get face appearances for this person
        try:
            photos = conn.execute("""
                SELECT
                    a.ZUUID,
                    a.ZDATECREATED,
                    a.ZLATITUDE,
                    a.ZLONGITUDE
                FROM ZASSET a
                JOIN ZDETECTEDFACE f ON f.ZASSETFORFACE = a.Z_PK
                WHERE f.ZPERSONFORFACE = ?
                ORDER BY a.ZDATECREATED DESC
                LIMIT 5000
            """, (person["Z_PK"],)).fetchall()

            for photo in photos:
                ts = None
                if photo["ZDATECREATED"] is not None:
                    unix_ts = photo["ZDATECREATED"] + APPLE_EPOCH_OFFSET
                    ts = datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()

                faces_export.append({
                    "person_pk": person["Z_PK"],
                    "asset_uuid": photo["ZUUID"],
                    "timestamp": ts,
                    "latitude": photo["ZLATITUDE"],
                    "longitude": photo["ZLONGITUDE"],
                })
        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning(f"Error reading photos for {person['ZFULLNAME']}: {e}")

    conn.close()

    out_path = EXPORT_DIR / "photos_faces.json"
    with open(out_path, "w") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "people_count": len(people_export),
            "face_appearances_count": len(faces_export),
            "people": people_export,
            "face_appearances": faces_export,
        }, f)  # No indent — this file can be large

    logger.info(
        f"Exported {len(people_export)} people, {len(faces_export)} face appearances to {out_path}"
    )
    return {
        "status": "ok",
        "people": len(people_export),
        "faces": len(faces_export),
        "path": str(out_path),
    }


def export_whatsapp(dry_run: bool = False) -> dict:
    """Export WhatsApp data via wacli for import on Linux.

    Routes through the wacli CLI (steipete/tap/wacli) which is macOS-only
    and reads the WhatsApp Desktop app's local SQLite database. The Mac Mini
    is the canonical source — Linux can't run wacli, so this export bridges
    them via a JSON file the Linux importer can consume.
    """
    import subprocess

    # Step 1: Have wacli refresh its local database from WhatsApp Desktop.
    # Best effort — if this fails we still export whatever's already on disk.
    try:
        result = subprocess.run(
            ["wacli", "sync", "--once"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            logger.info("wacli sync completed")
        else:
            logger.warning(f"wacli sync returned non-zero: {result.stderr.strip()[:200]}")
    except FileNotFoundError:
        logger.error("wacli not found. Install with: brew install steipete/tap/wacli")
        return {"status": "error", "reason": "wacli not installed"}
    except subprocess.TimeoutExpired:
        logger.warning("wacli sync timed out after 3 minutes — continuing with existing data")

    # Step 2: Verify wacli databases exist
    wacli_db = Path.home() / ".wacli" / "wacli.db"
    session_db = Path.home() / ".wacli" / "session.db"
    if not wacli_db.exists():
        logger.error(f"wacli database not found at {wacli_db}")
        return {"status": "error", "reason": "wacli.db not found"}

    # Step 3: Pull the contact list via the wacli command (richer than raw rows)
    try:
        result = subprocess.run(
            ["wacli", "--json", "contacts", "search", ".", "--limit", "10000"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("wacli contacts search timed out")
        return {"status": "error", "reason": "wacli contacts timeout"}

    contacts: list[dict] = []
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict) and "data" in parsed:
                contacts = parsed.get("data") or []
            elif isinstance(parsed, list):
                contacts = parsed
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse wacli contacts JSON: {e}")
    else:
        logger.warning(f"wacli contacts search failed: {result.stderr.strip()[:200]}")

    if dry_run:
        return {
            "status": "dry_run",
            "contacts": len(contacts),
            "wacli_db_mb": round(wacli_db.stat().st_size / (1024 * 1024), 1),
        }

    # Step 4: Dump messages, group_participants, lid contacts from wacli.db
    conn = sqlite3.connect(f"file:{wacli_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    messages = [
        dict(row)
        for row in conn.execute("""
            SELECT
                msg_id, chat_jid, chat_name, sender_jid, sender_name,
                ts, from_me, text, display_text, media_type
            FROM messages
            ORDER BY ts DESC
        """)
    ]

    group_participants = [
        dict(row)
        for row in conn.execute("SELECT group_jid, user_jid FROM group_participants")
    ]

    lid_contacts = [
        dict(row)
        for row in conn.execute("""
            SELECT jid, push_name FROM contacts
            WHERE jid LIKE '%@lid' AND push_name IS NOT NULL AND push_name != ''
        """)
    ]
    conn.close()

    # Step 5: Dump the LID-to-phone map from session.db (if present)
    lid_phones: dict[str, str] = {}
    if session_db.exists():
        try:
            sconn = sqlite3.connect(f"file:{session_db}?mode=ro", uri=True)
            for row in sconn.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                if row[0] and row[1]:
                    lid_phones[row[0]] = row[1]
            sconn.close()
        except sqlite3.OperationalError as e:
            logger.warning(f"Could not read whatsmeow_lid_map: {e}")

    # Step 6: Write JSON
    out_path = EXPORT_DIR / "whatsapp.json"
    with open(out_path, "w") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "contacts": contacts,
            "messages": messages,
            "group_participants": group_participants,
            "lid_contacts": lid_contacts,
            "lid_phones": lid_phones,
        }, f)  # No indent — file can be large

    logger.info(
        f"Exported WhatsApp: {len(contacts)} contacts, {len(messages)} messages, "
        f"{len(group_participants)} group memberships, {len(lid_phones)} LID phones to {out_path}"
    )
    return {
        "status": "ok",
        "contacts": len(contacts),
        "messages": len(messages),
        "group_participants": len(group_participants),
        "lid_phones": len(lid_phones),
        "path": str(out_path),
    }


def _finalize_result(result: dict) -> dict:
    """Guard against a source reporting "ok" with no actual output.

    Observed for `contacts` (issue #505): when no .abcdp files were found,
    export_contacts returned {"status": "ok", "count": 0, "path": ""} —
    indistinguishable from a healthy empty result. A source that claims "ok"
    but produced neither a count nor an output path didn't actually export
    anything, so treat that combination as an error. This makes the
    manifest accurate for the Linux side's _manifest_source_errored() check.
    """
    if (
        result.get("status") == "ok"
        and result.get("count") == 0
        and result.get("path") == ""
    ):
        result = dict(result)
        result["status"] = "error"
        result["reason"] = "reported ok with zero count and no output path"
    return result


def _get_agent_sha() -> str | None:
    """Return the git HEAD SHA of this checkout, or None if it can't be determined.

    Recorded in the manifest so the Linux side (issue #509) can tell which
    revision of the export pipeline produced a given export without SSHing to
    the Mac Mini — e.g. to notice the agent's self-update silently stopped
    working. Best-effort: a shallow clone, missing git binary, or non-repo
    checkout must never break the export itself.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def main():
    parser = argparse.ArgumentParser(description="Export Apple ecosystem data")
    parser.add_argument("--execute", action="store_true", help="Actually export (not dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be exported")
    parser.add_argument("--source", choices=["contacts", "imessage", "phone", "photos", "whatsapp"], help="Export single source")
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
        "photos": export_photos_faces,
        "whatsapp": export_whatsapp,
    }

    if args.source:
        sources = {args.source: sources[args.source]}

    results = {}
    for name, func in sources.items():
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Exporting {name}...")
        try:
            results[name] = _finalize_result(func(dry_run=dry_run))
        except Exception as e:
            logger.error(f"Failed to export {name}: {e}")
            results[name] = {"status": "error", "error": str(e)}

    # Write manifest
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "hostname": __import__("socket").gethostname(),
        "agent_sha": _get_agent_sha(),
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
