#!/usr/bin/env python3
"""
Import the gmcli archive (Google Messages: SMS, MMS and RCS) into LifeOS.

Reads a snapshot copy of gmcli's SQLite archive (never session.json) and:
  1. links each text-message counterpart to a PersonEntity by phone number, then
     by exact name, and creates a person only for a named contact you have
     actually exchanged messages with;
  2. records contact and message-handle SourceEntity rows;
  3. writes one Interaction per message. The source_type is "imessage", the
     existing text-message channel, so weights, stats and facts apply. The
     source_id is "gmessages_<message_id>", which keeps these rows separable.

Dry run unless --execute. Re-running is safe: messages are deduplicated by
source_id. Outgoing messages in group chats are skipped, because they have no
single counterpart.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from api.services.entity_resolver import get_entity_resolver
from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import get_person_entity_store
from api.services.phone_utils import normalize_phone
from api.services.source_entity import (
    LINK_STATUS_AUTO,
    create_contacts_source_entity,
    create_imessage_source_entity,
    get_source_entity_store,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Resolver match types that are safe to link without review. Fuzzy and
# ambiguous name matches are skipped and reported instead.
ACCEPT_MATCHES = {"phone_exact", "email_exact", "name_exact", "structured", "link_override"}


def find_store_dir(arg: str | None) -> Path:
    """Locate the gmcli store directory (the one holding gmcli.db)."""
    if arg:
        return Path(arg)
    env = os.environ.get("LIFEOS_GMCLI_STORE")
    if env:
        return Path(env)
    for hit in sorted(Path("/mnt/c/Users").glob("*/.local/state/gmcli")):
        if (hit / "gmcli.db").exists():
            return hit
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gmcli"


def snapshot_archive(store_dir: Path, dest: Path) -> Path:
    """Copy gmcli.db (and its WAL files) into dest. Never copies session.json."""
    src = store_dir / "gmcli.db"
    if not src.exists():
        raise FileNotFoundError(f"No gmcli.db in {store_dir}")
    for suffix in ("", "-wal", "-shm"):
        f = store_dir / f"gmcli.db{suffix}"
        if f.exists():
            shutil.copy2(f, dest / f.name)
    return dest / "gmcli.db"


def e164_of(value: str | None) -> str | None:
    """Full phone numbers only. Short codes and blanks return None."""
    if not value or not value.startswith("+"):
        return None
    return normalize_phone(value) or None


def valid_name(name: str | None) -> bool:
    return bool(name) and len(name.strip()) >= 2 and any(c.isalpha() for c in name)


def backup_databases(data_dir: Path) -> Path:
    """Consistent backup of the CRM and interaction databases before writing."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = data_dir / "backups" / f"pre-gmcli-import-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("crm.db", "interactions.db"):
        src_path = data_dir / name
        if not src_path.exists():
            continue
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dest / name)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    return dest


def load_archive(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    contacts = [dict(r) for r in conn.execute(
        "SELECT participant_id, name, e164, is_me FROM contacts")]
    convs = [dict(r) for r in conn.execute(
        "SELECT conversation_id, is_group, participants_json FROM conversations")]
    msgs = [dict(r) for r in conn.execute(
        "SELECT message_id, conversation_id, sender_id, body, timestamp_ms, "
        "is_from_me, mime_type FROM messages ORDER BY timestamp_ms")]
    conn.close()
    return {"contacts": contacts, "convs": convs, "msgs": msgs}


def build_directory(archive: dict) -> dict:
    """Map participant ids and conversations to phone numbers and names."""
    my_numbers: set[str] = set()
    my_ids: set[str] = set()
    pid_e164: dict[str, str] = {}
    name_by_e164: dict[str, str] = {}

    def note(pid, e164, name, is_me):
        e = e164_of(e164)
        if is_me and e:
            my_numbers.add(e)
        if pid and e:
            pid_e164[str(pid)] = e
        if e and valid_name(name) and not is_me:
            name_by_e164.setdefault(e, name.strip())

    for c in archive["contacts"]:
        note(c["participant_id"], c["e164"], c["name"], c["is_me"])
    parsed = {}
    for cv in archive["convs"]:
        try:
            plist = json.loads(cv["participants_json"] or "[]")
        except json.JSONDecodeError:
            plist = []
        parsed[cv["conversation_id"]] = plist
        for p in plist:
            note(p.get("id"), p.get("e164"), p.get("name"), p.get("is_me"))

    # Participant ids that resolve to my own number are "me" in every thread.
    for pid, e in pid_e164.items():
        if e in my_numbers:
            my_ids.add(pid)

    conv_info = {}
    for cv in archive["convs"]:
        cps = {
            e164_of(p.get("e164")) for p in parsed[cv["conversation_id"]]
            if not p.get("is_me") and e164_of(p.get("e164"))
        } - my_numbers
        is_group = bool(cv["is_group"]) or len(cps) > 1
        conv_info[cv["conversation_id"]] = {
            "group": is_group,
            "counterpart": next(iter(cps)) if len(cps) == 1 and not is_group else None,
        }
    return {"my_ids": my_ids, "pid_e164": pid_e164, "name_by_e164": name_by_e164,
            "conv": conv_info}


def attribute_messages(archive: dict, directory: dict, stats: dict) -> list[tuple]:
    """Return (message, outgoing, e164) for every message that has a counterpart."""
    out = []
    for m in archive["msgs"]:
        info = directory["conv"].get(m["conversation_id"], {})
        outgoing = bool(m["is_from_me"]) or str(m["sender_id"]) in directory["my_ids"]
        if info.get("group"):
            if outgoing:
                stats["group_outgoing_skipped"] += 1
                continue
            e164 = directory["pid_e164"].get(str(m["sender_id"]))
        else:
            e164 = info.get("counterpart")
        if not e164:
            stats["no_phone_counterpart"] += 1
            continue
        out.append((m, outgoing, e164))
    return out


def message_title_and_snippet(m: dict, outgoing: bool) -> tuple[str, str | None]:
    text = (m["body"] or "").strip()
    if not text:
        text = f"[{m['mime_type']}]" if m["mime_type"] else ""
    preview = text if len(text) <= 100 else text[:97] + "..."
    if not preview:
        preview = "[No text content]"
    return f"{'→' if outgoing else '←'} {preview}", (text[:200] or None)


def import_gmcli(store_dir: Path, dry_run: bool = True, min_messages: int = 1,
                 limit: int | None = None, backup: bool = True) -> dict:
    stats = Counter()
    plan = defaultdict(list)

    with tempfile.TemporaryDirectory(prefix="lifeos-gmcli-") as tmp:
        archive = load_archive(snapshot_archive(store_dir, Path(tmp)))
    logger.info(
        f"Archive snapshot: {len(archive['convs'])} conversations, "
        f"{len(archive['msgs'])} messages, {len(archive['contacts'])} contacts"
    )
    stats["messages_in_archive"] = len(archive["msgs"])

    directory = build_directory(archive)
    attributed = attribute_messages(archive, directory, stats)
    if limit:
        attributed = attributed[-limit:]

    per_number = Counter(e for _, _, e in attributed)
    last_seen: dict[str, datetime] = {}
    for m, _, e in attributed:
        ts = datetime.fromtimestamp(m["timestamp_ms"] / 1000, tz=timezone.utc)
        if e not in last_seen or ts > last_seen[e]:
            last_seen[e] = ts

    person_store = get_person_entity_store()
    source_store = get_source_entity_store()
    resolver = get_entity_resolver()

    if not dry_run and backup:
        data_dir = Path(get_interaction_db_path()).parent
        logger.info(f"Backed up CRM databases to {backup_databases(data_dir)}")

    def keep_unlinked(e164: str, name: str | None, count: int) -> None:
        """Store a number we could not link as an unlinked source entity, so the
        nightly link_source_entities.py (or a manual link) can attach it later."""
        if dry_run:
            stats["unlinked_numbers_would_keep"] += 1
            return
        entity = create_imessage_source_entity(
            handle=e164, display_name=name if valid_name(name) else None,
            observed_at=last_seen.get(e164),
            metadata={"service": "google_messages", "message_count": count},
        )
        source_store.add_or_update(entity)
        stats["unlinked_numbers_kept"] += 1

    # Resolve each distinct phone number to a person once.
    person_for: dict[str, object] = {}
    method_for: dict[str, str] = {}
    for e164, count in per_number.most_common():
        name = directory["name_by_e164"].get(e164)
        person = person_store.get_by_phone(e164)
        method = "phone_exact" if person else None

        # A person linked to this number's source entity (by link_source_entities.py
        # or by hand) counts even if the number is not on their record yet.
        if not person:
            prior = source_store.get_by_source("imessage", e164)
            if prior and prior.canonical_person_id:
                person = person_store.get_by_id(prior.canonical_person_id)
                method = "source_link" if person else None

        if not person and name:
            res = resolver.resolve(name=name, phone=e164, create_if_missing=False)
            if res and res.entity:
                if res.match_type in ACCEPT_MATCHES:
                    person, method = res.entity, res.match_type
                else:
                    stats["ambiguous_skipped"] += 1
                    plan["ambiguous"].append(
                        f"{name} ~ {res.entity.canonical_name} ({res.match_type}, {count} msgs)")
                    keep_unlinked(e164, name, count)
                    continue

        if not person and valid_name(name) and count >= min_messages:
            if dry_run:
                stats["would_create_people"] += 1
                plan["would_create"].append(f"{name} ({count} msgs)")
                continue
            res = resolver.resolve(name=name, phone=e164, create_if_missing=True)
            if res and res.entity:
                person = res.entity
                method = "created" if res.is_new else res.match_type
                if res.is_new:
                    stats["people_created"] += 1

        if not person:
            stats["numbers_without_person"] += 1
            if not valid_name(name):
                plan["unnamed"].append(f"{e164} ({count} msgs)")
            keep_unlinked(e164, name, count)
            continue

        stats["numbers_linked"] += 1
        stats[f"link_{method}"] += 1
        person_for[e164], method_for[e164] = person, method
        plan["linked"].append(
            f"{name or e164} -> {person.canonical_name} ({method}, {count} msgs)")

        if dry_run:
            continue

        changed = False
        if e164 not in person.phone_numbers:
            person.phone_numbers.append(e164)
            if not person.phone_primary:
                person.phone_primary = e164
            changed = True
        if "imessage" not in person.sources:
            person.sources.append("imessage")
            changed = True
        if changed:
            person_store.update(person)
            stats["people_updated"] += 1

        confidence = 1.0 if method in ("phone_exact", "created") else 0.9
        if valid_name(name):
            contact, _ = source_store.add_or_update(create_contacts_source_entity(
                contact_id=f"gmcli_{e164}", name=name, phone=e164,
                metadata={"origin": "google_messages"},
            ))
            source_store.link_to_person(
                contact.id, person.id, confidence=confidence,
                status=LINK_STATUS_AUTO, method="gmessages_contact",
            )
        handle = create_imessage_source_entity(
            handle=e164, display_name=person.canonical_name,
            observed_at=last_seen.get(e164), metadata={"service": "google_messages"},
        )
        handle.canonical_person_id = person.id
        handle.link_confidence = confidence
        handle.link_method = "gmessages_handle"
        handle.linked_at = datetime.now(timezone.utc)
        source_store.add_or_update(handle)
        stats["source_entities_written"] += 2 if valid_name(name) else 1

        new_count = source_store.count_for_person(person.id)
        if person.source_entity_count != new_count:
            person.source_entity_count = new_count
            person_store.update(person)
            stats["source_counts_refreshed"] += 1

    if not dry_run:
        person_store.save()

    # Interactions
    iconn = sqlite3.connect(get_interaction_db_path())
    existing = {r[0] for r in iconn.execute(
        "SELECT source_id FROM interactions "
        "WHERE source_type = 'imessage' AND source_id LIKE 'gmessages_%'")}
    logger.info(f"Found {len(existing)} existing Google Messages interactions")

    batch, affected = [], set()
    for m, outgoing, e164 in attributed:
        stats["messages_checked"] += 1
        person = person_for.get(e164)
        if not person:
            stats["messages_without_person"] += 1
            continue
        source_id = f"gmessages_{m['message_id']}"
        if source_id in existing:
            stats["already_exists"] += 1
            continue
        ts = datetime.fromtimestamp(m["timestamp_ms"] / 1000, tz=timezone.utc)
        if ts.year < 2000:
            stats["errors"] += 1
            continue
        title, snippet = message_title_and_snippet(m, outgoing)
        batch.append((str(uuid.uuid4()), person.id, ts.isoformat(), "imessage",
                      title, snippet, "", source_id,
                      datetime.now(timezone.utc).isoformat()))
        affected.add(person.id)
        stats["interactions_to_insert" if dry_run else "interactions_inserted"] += 1

    if batch and not dry_run:
        iconn.executemany(
            "INSERT OR IGNORE INTO interactions (id, person_id, timestamp, source_type, "
            "title, snippet, source_link, source_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
        iconn.commit()
    iconn.close()

    if not dry_run and affected:
        from api.services.person_stats import refresh_person_stats
        logger.info(f"Refreshing stats for {len(affected)} people...")
        refresh_person_stats(list(affected))

    logger.info("\n=== Google Messages (gmcli) Import Summary ===")
    for key, value in sorted(stats.items()):
        logger.info(f"{key}: {value}")
    for label, key in (("Linked", "linked"), ("Would create", "would_create"),
                       ("Ambiguous, skipped", "ambiguous"), ("Unnamed numbers", "unnamed")):
        if plan[key]:
            logger.info(f"\n{label} ({len(plan[key])}):")
            for line in plan[key][:25]:
                logger.info(f"  {line}")
    if dry_run:
        logger.info("\nDRY RUN - no changes made")

    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({
        "interactions_created": stats["interactions_inserted"],
        "people_created": stats["people_created"],
        "people_updated": stats["people_updated"],
        "errors": stats["errors"],
    })
    return dict(stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import the gmcli archive into LifeOS")
    parser.add_argument("--store", help="gmcli store directory (default: auto-detect)")
    parser.add_argument("--execute", action="store_true", help="Actually apply changes")
    parser.add_argument("--min-messages", type=int, default=1,
                        help="Messages needed before creating a person for a named contact")
    parser.add_argument("--limit", type=int, help="Only the most recent N messages")
    parser.add_argument("--no-backup", action="store_true", help="Skip the pre-import DB backup")
    args = parser.parse_args()

    import_gmcli(
        find_store_dir(args.store),
        dry_run=not args.execute,
        min_messages=args.min_messages,
        limit=args.limit,
        backup=not args.no_backup,
    )
