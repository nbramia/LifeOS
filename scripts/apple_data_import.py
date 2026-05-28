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
import re
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
    """Check the import manifest for freshness and per-source errors.

    Logs warnings/errors based on data age:
    - >48h: WARNING (picked up by nightly health batch)
    - >7d:  CRITICAL-level log (triggers immediate alert if server is running)

    Also walks manifest["results"] and logs CRITICAL for any source the Mac
    Mini export marked with status == "error". The caller (main) uses the
    returned manifest to decide exit status.
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

    # Walk per-source results and log CRITICAL for any export-side errors.
    # The CRITICAL log level is the existing alerting path — it routes through
    # email/Telegram when the server is running.
    results = manifest.get("results") or {}
    if isinstance(results, dict):
        for source_name, source_result in results.items():
            if not isinstance(source_result, dict):
                continue
            if source_result.get("status") == "error":
                reason = source_result.get("reason") or source_result.get("error") or "unknown"
                logger.critical(
                    f"Mac Mini export failed for {source_name}: {reason}. "
                    f"Check wacli/tooling on the Mac Mini."
                )

    return manifest


def _manifest_source_errored(manifest: dict | None, source: str) -> bool:
    """Return True if the manifest marks ``source`` as status=error."""
    if not manifest:
        return False
    results = manifest.get("results") or {}
    if not isinstance(results, dict):
        return False
    source_result = results.get(source)
    if not isinstance(source_result, dict):
        return False
    return source_result.get("status") == "error"


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
        from api.services.phone_utils import normalize_phone
        phones = [
            normalize_phone(p["value"]) or p["value"]
            for p in contact.get("phones", []) if p.get("value")
        ]

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
                person.add_phone(phone)  # normalizes to E.164
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


_PHONE_RE = re.compile(r"\+\d{10,15}")


def _extract_phone_from_title(title: str) -> str | None:
    """Extract E.164 phone number from a call title string."""
    m = _PHONE_RE.search(title)
    return m.group(0) if m else None


def import_phone_calls(dry_run: bool = False) -> dict:
    """Import phone call history from the Mac Mini's JSON export.

    Mirrors the source_entity behaviour of ``scripts/sync_phone_calls.py``
    (the macOS-native version). For each call whose phone number resolves
    to an existing PersonEntity, we ensure a phone-keyed SourceEntity
    exists (``phone_{e164}``), update its ``observed_at`` to the most
    recent call we've seen for that number in this batch, link it to the
    person if it isn't already, then create the Interaction.

    Scope: only resolvable callers get source_entities. Callers we've never
    associated with a Person (spam, robocalls, first-time inbound) stay in
    ``unresolved``. The entity-resolver has no phone-only create-if-missing
    branch (see ``api/services/entity_resolver.py:881``) and adding one
    here would mint a junk PersonEntity per spam call. Filed as a separate
    follow-up.

    The source_entity update happens BEFORE the "interaction already
    exists?" check so that re-imports still close the issue #199 §2 drift
    gap when a Person row was created out-of-band between runs.

    Manual SourceEntity→Person links (``link_status='manual'``) are never
    overwritten by the auto resolver — a human's curated link beats a
    nightly's best guess.
    """
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
    from api.services.entity_resolver import get_entity_resolver
    from api.services.source_entity import (
        SourceEntity,
        get_source_entity_store,
        LINK_STATUS_AUTO,
    )

    store = get_interaction_store()
    resolver = get_entity_resolver()
    se_store = get_source_entity_store()

    # Per-phone cache for this run so we don't hit the DB again for every
    # call from the same number (typical export has 100s of calls across
    # dozens of unique numbers).
    seen_phones: set[str] = set()

    # Hoisted once per run — used as the fallback observed_at when a call
    # has no timestamp; recomputing in the hot loop just adds noise.
    now = datetime.now(timezone.utc)

    imported = 0
    skipped = 0
    unresolved = 0
    source_entities_created = 0
    source_entities_updated = 0

    for call in calls:
        source_id = call.get("source_id", "")
        if not source_id:
            skipped += 1
            continue

        source_type = call.get("source_type", "phone")

        timestamp = None
        if call.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(call["timestamp"])
            except (ValueError, TypeError):
                skipped += 1
                continue

        title = call.get("title", "")
        phone = _extract_phone_from_title(title)
        if not phone:
            unresolved += 1
            continue

        # Resolve to PersonEntity by phone. Note: the resolver has no
        # "create from phone alone" branch (see entity_resolver.py:881 —
        # only email and name create-if-missing paths exist), so callers
        # whose phone we've never linked to a known Person are deliberately
        # left in ``unresolved``. Adding a phone-only create branch is a
        # separate, broader change (filed as a follow-up); doing it here
        # would auto-create a junk Person for every spam call.
        result = resolver.resolve(phone=phone, create_if_missing=False)
        person = result.entity if result else None
        if not person:
            unresolved += 1
            continue

        # Ensure a phone-keyed source_entity exists and is linked.
        # This happens BEFORE the "existing interaction?" check so that
        # already-imported calls still contribute to source_entity
        # accumulation when the resolver / entity store grows new rows.
        # ``seen_phones`` dedups within this run; per-call freshness is
        # preserved by computing the merged observed_at across all calls
        # seen for the same phone before the first DB write.
        if phone not in seen_phones:
            seen_phones.add(phone)
            se_source_id = f"phone_{phone}"
            # The most-recent valid timestamp among any call for this phone
            # in this batch — prevents observed_at from ratcheting backwards
            # when the export isn't strictly chronological. Malformed
            # timestamps inside other calls are skipped silently.
            same_phone_timestamps: list[datetime] = []
            for c in calls:
                if (
                    c.get("timestamp")
                    and _extract_phone_from_title(c.get("title", "")) == phone
                ):
                    try:
                        same_phone_timestamps.append(
                            datetime.fromisoformat(c["timestamp"])
                        )
                    except (ValueError, TypeError):
                        continue
            this_phone_max_ts = max(same_phone_timestamps, default=timestamp or now)
            existing_se = se_store.get_by_source("phone", se_source_id)
            if existing_se:
                # Preserve fields we don't have authoritative data for in
                # this importer (observed_name comes from Address Book on
                # the Mac-side path; here we only know the number).
                if existing_se.observed_at is None or this_phone_max_ts > existing_se.observed_at:
                    existing_se.observed_at = this_phone_max_ts
                existing_se.observed_phone = phone
                se_store.update(existing_se)
                source_entities_updated += 1
                se = existing_se
            else:
                se = SourceEntity(
                    source_type="phone",
                    source_id=se_source_id,
                    observed_name=None,
                    observed_phone=phone,
                    metadata={},
                    observed_at=this_phone_max_ts,
                )
                se = se_store.add(se)
                source_entities_created += 1
            # Only re-link from auto-quality data: never clobber a manual
            # link (link_status == "manual") because that represents a
            # human's judgement that this number belongs to a specific
            # person, possibly across a merge or rename.
            if (
                se.canonical_person_id != person.id
                and (se.link_status or "auto") != "manual"
            ):
                se_store.link_to_person(
                    se.id, person.id,
                    confidence=0.95, status=LINK_STATUS_AUTO,
                )

        # Skip calls already in the interaction store.
        existing = store.get_by_source(source_type, source_id)
        if existing:
            skipped += 1
            continue

        interaction = Interaction(
            id=call.get("id") or str(uuid.uuid4()),
            person_id=person.id,
            timestamp=timestamp,
            source_type=source_type,
            title=title,
            snippet=call.get("snippet"),
            source_link=call.get("source_link", ""),
            source_id=source_id,
        )
        store.add_if_not_exists(interaction)
        imported += 1

    logger.info(
        f"Phone calls: {imported} imported, {skipped} skipped (existing/invalid), "
        f"{unresolved} unresolved (no phone in title), "
        f"{source_entities_created} source_entities created, "
        f"{source_entities_updated} updated"
    )
    return {
        "status": "ok",
        "imported": imported,
        "skipped": skipped,
        "unresolved": unresolved,
        "source_entities_created": source_entities_created,
        "source_entities_updated": source_entities_updated,
    }


def import_photos_faces(dry_run: bool = False) -> dict:
    """Import Photos face recognition data from Mac Mini export.

    Matches Photos people to PersonEntity records using contact UUIDs
    (via previously imported contacts), then creates SourceEntity and
    Interaction records — the same records that sync_photos.py would
    create if the Photos library were available locally.
    """
    photos_path = IMPORT_DIR / "photos_faces.json"
    if not photos_path.exists():
        return {"status": "skipped", "reason": "photos_faces.json not found"}

    with open(photos_path) as f:
        data = json.load(f)

    people = data.get("people", [])
    faces = data.get("face_appearances", [])
    logger.info(
        f"Found {len(people)} people, {len(faces)} face appearances "
        f"(exported {data.get('exported_at', '?')})"
    )

    if dry_run:
        return {"status": "dry_run", "people": len(people), "faces": len(faces)}

    from api.services.source_entity import get_source_entity_store, SourceEntity
    from api.services.interaction_store import get_interaction_store, Interaction
    from api.services.person_entity import get_person_entity_store

    se_store = get_source_entity_store()
    ia_store = get_interaction_store()
    pe_store = get_person_entity_store()

    # Build contact UUID → PersonEntity ID mapping
    uuid_to_person: dict[str, str | None] = {}
    for person_data in people:
        contact_uuid = person_data.get("contact_uuid")
        if not contact_uuid:
            continue

        # Look up the imported contact source entity by UUID
        source_id = f"contacts:{contact_uuid}"
        contact_se = se_store.get_by_source("contacts", source_id)
        if contact_se and contact_se.canonical_person_id:
            uuid_to_person[contact_uuid] = contact_se.canonical_person_id
            continue

        # Fallback: the contact may be stored with the raw UUID as source_id
        # (different import batches may use different formats)
        contact_se = se_store.get_by_source("contacts", contact_uuid)
        if contact_se and contact_se.canonical_person_id:
            uuid_to_person[contact_uuid] = contact_se.canonical_person_id
            continue

        # Final fallback: match by name
        pe = pe_store.get_by_name(person_data.get("full_name", ""))
        if pe:
            uuid_to_person[contact_uuid] = pe.id
        else:
            uuid_to_person[contact_uuid] = None

    matched = sum(1 for v in uuid_to_person.values() if v)
    logger.info(f"Matched {matched}/{len(people)} Photos people to PersonEntity records")

    # Build person_pk → person_id lookup for face appearances
    pk_to_person: dict[int, str] = {}
    for person_data in people:
        contact_uuid = person_data.get("contact_uuid")
        person_id = uuid_to_person.get(contact_uuid) if contact_uuid else None
        if person_id:
            pk_to_person[person_data["photos_pk"]] = person_id

    # Import face appearances
    sources_created = 0
    interactions_created = 0
    skipped = 0

    for face in faces:
        person_id = pk_to_person.get(face.get("person_pk"))
        if not person_id:
            skipped += 1
            continue

        asset_uuid = face.get("asset_uuid")
        if not asset_uuid:
            skipped += 1
            continue

        source_id = f"{asset_uuid}:{face['person_pk']}"

        # Skip if already exists
        existing = se_store.get_by_source("photos", source_id)
        if existing:
            skipped += 1
            continue

        timestamp = None
        if face.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(face["timestamp"])
            except (ValueError, TypeError):
                timestamp = datetime.now(timezone.utc)

        # Create SourceEntity
        se = SourceEntity(
            source_type="photos",
            source_id=source_id,
            observed_name=next(
                (p["full_name"] for p in people if p["photos_pk"] == face["person_pk"]),
                "",
            ),
            canonical_person_id=person_id,
            link_confidence=0.95,
            link_method="contact_uuid",
            observed_at=timestamp or datetime.now(timezone.utc),
            metadata={
                "photos_person_pk": face["person_pk"],
                "asset_uuid": asset_uuid,
                "latitude": face.get("latitude"),
                "longitude": face.get("longitude"),
            },
        )
        se_store.add(se)
        sources_created += 1

        # Create Interaction
        interaction = Interaction(
            id=str(uuid.uuid4()),
            person_id=person_id,
            timestamp=timestamp or datetime.now(timezone.utc),
            source_type="photos",
            title="Photo",
            source_link=f"photos://asset/{asset_uuid}",
            source_id=source_id,
        )
        ia_store.add_if_not_exists(interaction)
        interactions_created += 1

    logger.info(
        f"Photos: {sources_created} sources, {interactions_created} interactions created, "
        f"{skipped} skipped"
    )
    return {
        "status": "ok",
        "people_matched": matched,
        "sources_created": sources_created,
        "interactions_created": interactions_created,
        "skipped": skipped,
    }


def import_whatsapp(dry_run: bool = False, manifest: dict | None = None) -> dict:
    """Import WhatsApp data exported from the Mac Mini.

    Reads data/apple-imports/whatsapp.json (produced by export_whatsapp on
    the Mac) and dispatches to api.services.whatsapp for the actual entity
    resolution and Interaction creation.

    Status semantics:
    - "skipped": nothing to do (file absent and manifest didn't attempt an
      export, e.g. a single-source --source contacts run on the Mac).
    - "error": the Mac-side export failed. If a stale whatsapp.json exists
      from a previous successful run we still import it (data is better than
      nothing) but propagate the error so the operator is alerted and the
      run is recorded as FAILED.
    """
    whatsapp_path = IMPORT_DIR / "whatsapp.json"
    manifest_errored = _manifest_source_errored(manifest, "whatsapp")

    if not whatsapp_path.exists():
        if manifest_errored:
            reason = "whatsapp.json not found and Mac export marked whatsapp as error"
            return {"status": "error", "reason": reason}
        return {"status": "skipped", "reason": "whatsapp.json not found"}

    with open(whatsapp_path) as f:
        data = json.load(f)

    contacts = data.get("contacts") or []
    messages = data.get("messages") or []
    group_participants = data.get("group_participants") or []
    lid_contacts = data.get("lid_contacts") or []
    lid_phones = data.get("lid_phones") or {}

    logger.info(
        f"Found WhatsApp export: {len(contacts)} contacts, {len(messages)} messages "
        f"(exported {data.get('exported_at', '?')})"
    )

    if dry_run:
        base = {
            "contacts": len(contacts),
            "messages": len(messages),
        }
        if manifest_errored:
            return {
                "status": "error",
                "reason": "Mac export marked whatsapp as error (stale file used for dry-run counts)",
                **base,
            }
        return {"status": "dry_run", **base}

    from api.services.whatsapp import (
        process_whatsapp_contacts,
        process_whatsapp_messages,
    )

    contact_stats = process_whatsapp_contacts(contacts, dry_run=dry_run)
    logger.info(
        f"WhatsApp contacts: {contact_stats['source_entities_created']} created, "
        f"{contact_stats['source_entities_updated']} updated, "
        f"{contact_stats['persons_linked']} linked, "
        f"{contact_stats['skipped']} skipped"
    )

    message_stats = process_whatsapp_messages(
        messages=messages,
        group_participants=group_participants,
        lid_contacts=lid_contacts,
        lid_phones=lid_phones,
        dry_run=dry_run,
    )
    logger.info(
        f"WhatsApp messages: {message_stats['interactions_created']} created, "
        f"{message_stats['interactions_skipped']} skipped, "
        f"{message_stats['skipped_large_group']} large-group skips, "
        f"{message_stats['outgoing_group_created']} outgoing-group fan-outs"
    )

    if manifest_errored:
        return {
            "status": "error",
            "reason": "Mac export marked whatsapp as error; imported stale whatsapp.json",
            "contacts": contact_stats,
            "messages": message_stats,
        }

    return {
        "status": "ok",
        "contacts": contact_stats,
        "messages": message_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="Import Apple ecosystem data from Mac Mini exports")
    parser.add_argument("--execute", action="store_true", help="Actually import")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--source", choices=["contacts", "imessage", "phone", "photos", "whatsapp"], help="Import single source")
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

    # Some sources need the manifest to distinguish "nothing exported" from
    # "export attempted and failed". Keep the uniform (dry_run,) signature for
    # the rest so the dispatch stays simple.
    def _import_whatsapp_wrapper(dry_run: bool = False) -> dict:
        return import_whatsapp(dry_run=dry_run, manifest=manifest)

    sources = {
        "contacts": import_contacts,
        "imessage": import_imessage,
        "phone": import_phone_calls,
        "photos": import_photos_faces,
        "whatsapp": _import_whatsapp_wrapper,
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

    # Also surface per-source errors recorded in the manifest for sources we
    # didn't attempt this run (e.g. --source contacts will skip whatsapp, but
    # if the manifest says whatsapp errored on the Mac side we still want to
    # propagate that). Only sources the caller selected are merged so that
    # --source contacts doesn't spuriously fail on an unrelated phone error.
    if manifest:
        manifest_results = manifest.get("results") or {}
        for name in sources:
            if name in results:
                continue
            manifest_entry = manifest_results.get(name) if isinstance(manifest_results, dict) else None
            if isinstance(manifest_entry, dict) and manifest_entry.get("status") == "error":
                results[name] = {
                    "status": "error",
                    "reason": manifest_entry.get("reason")
                    or manifest_entry.get("error")
                    or "Mac export reported error",
                }

    print(json.dumps({"results": results}, indent=2))

    # Emit canonical sync stats for the orchestrator. Aggregates across all
    # sub-imports so run_all_syncs.py records the true row deltas instead of
    # inferring zero from output that doesn't match its regex patterns.
    #
    # Each import_* function uses a different result-dict shape (historical),
    # so the dispatch is explicit per source. An if/elif chain — not unguarded
    # accumulators — keeps an `import_contacts` that grows new keys later
    # (e.g. someone adds "sources_created") from being double-counted.
    from api.services.sync_health import emit_sync_stats
    aggregate = {
        "interactions_created": 0,
        "source_entities_created": 0,
        "people_created": 0,
        "people_updated": 0,
    }
    for name, result in results.items():
        if not isinstance(result, dict):
            continue
        if name == "contacts":
            # import_contacts → {"created", "updated", "linked"}
            aggregate["source_entities_created"] += int(result.get("created", 0) or 0)
            aggregate["people_updated"] += int(result.get("linked", 0) or 0)
        elif name == "imessage":
            # import_imessage just copies the db; the actual source_entity /
            # interaction creation happens later in sync_imessage_interactions
            # (which emits its own SYNC_STATS line). Nothing to count here.
            pass
        elif name == "phone":
            # import_phone_calls → {"imported", "source_entities_created",
            # "source_entities_updated", ...}. The orchestrator's
            # SYNC_STATS schema has no "source_entities_updated" field —
            # update counts surface only in the importer's log line. Worth
            # adding to the schema if we ever start tracking
            # "things changed without growing the table".
            aggregate["interactions_created"] += int(result.get("imported", 0) or 0)
            aggregate["source_entities_created"] += int(result.get("source_entities_created", 0) or 0)
        elif name == "photos":
            # import_photos_faces → {"sources_created", "interactions_created"}
            aggregate["source_entities_created"] += int(result.get("sources_created", 0) or 0)
            aggregate["interactions_created"] += int(result.get("interactions_created", 0) or 0)
        elif name == "whatsapp":
            # import_whatsapp → {"contacts": {...}, "messages": {...}}
            contacts = result.get("contacts")
            if isinstance(contacts, dict):
                aggregate["source_entities_created"] += int(contacts.get("source_entities_created", 0) or 0)
                aggregate["people_updated"] += int(contacts.get("persons_linked", 0) or 0)
            messages = result.get("messages")
            if isinstance(messages, dict):
                aggregate["interactions_created"] += int(messages.get("interactions_created", 0) or 0)
    emit_sync_stats(aggregate)

    # Non-zero exit on any per-source error so run_all_syncs.run_sync marks
    # apple_import as FAILED and sync_health records it. "skipped" is fine —
    # it means nothing to do.
    errored_sources = [
        name
        for name, result in results.items()
        if isinstance(result, dict) and result.get("status") == "error"
    ]
    if errored_sources:
        logger.error(
            f"Apple import finished with errors in: {', '.join(sorted(errored_sources))}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
