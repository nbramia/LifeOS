"""
WhatsApp data processing for LifeOS CRM.

Pure functions for extracting people and interactions from WhatsApp data
exported by wacli on the Mac Mini. The export step (running wacli, reading
its SQLite databases) lives in scripts/apple_data_export.py because it
requires a macOS host with WhatsApp Desktop and the wacli CLI installed.

This module operates on parsed dicts/lists, so it can be unit-tested
without mocking subprocess or sqlite.

Source data format (produced by scripts.apple_data_export.export_whatsapp):

    {
        "exported_at": "<iso8601>",
        "contacts": [{"JID": ..., "Phone": ..., "Name": ..., "Alias": ...}],
        "messages": [{"msg_id": ..., "chat_jid": ..., "sender_jid": ...,
                      "ts": ..., "from_me": 0|1, "text": ..., ...}],
        "group_participants": [{"group_jid": ..., "user_jid": ...}],
        "lid_contacts": [{"jid": "...@lid", "push_name": ...}],
        "lid_phones": {"<bare-lid>": "<raw-phone>"},
    }
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from api.services.entity_resolver import get_entity_resolver
from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import get_person_entity_store
from api.services.source_entity import (
    LINK_STATUS_AUTO,
    SourceEntity,
    get_source_entity_store,
)

logger = logging.getLogger(__name__)

SOURCE_WHATSAPP = "whatsapp"

# Skip group threads with more than this many participants. Large groups
# (broadcast lists, mass-marketing groups) generate noisy interactions
# that aren't useful as relationship signal.
LARGE_GROUP_THRESHOLD = 20


# ---------------------------------------------------------------------------
# Pure helpers — JID parsing, phone normalization
# ---------------------------------------------------------------------------

def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format.

    Returns empty string for invalid input. WhatsApp processing returns
    "" rather than None so callers can use truthiness checks; the canonical
    normalizer in api.services.phone_utils returns None.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) > 10:
        return f"+{digits}"
    return ""


def is_group_jid(jid: str) -> bool:
    """Return True if the JID is a WhatsApp group chat."""
    if not jid:
        return False
    return "@g.us" in jid


def extract_phone_from_jid(jid: str) -> str:
    """Extract a normalized E.164 phone from a WhatsApp JID.

    JID formats:
        {phone}@s.whatsapp.net  → individuals
        {groupid}@g.us           → groups (returns "")
        {lid}@lid                → linked devices, not real phones (returns "")
    """
    if not jid or is_group_jid(jid):
        return ""
    if "@lid" in jid:
        return ""
    phone_part = jid.split("@")[0] if "@" in jid else jid
    return normalize_phone(phone_part)


def resolve_lid_phone(lid_jid: str, lid_phones: dict[str, str]) -> str:
    """Look up phone number for a LID JID via the whatsmeow lid_map.

    LID JIDs may include a `:N` device suffix that must be stripped before
    looking up in the map. The map values are raw phone strings that need
    normalization.
    """
    if not lid_jid or "@lid" not in lid_jid:
        return ""
    bare = lid_jid.split("@")[0].split(":")[0]
    raw = lid_phones.get(bare, "")
    return normalize_phone(raw) if raw else ""


def parse_message_timestamp(ts) -> datetime:
    """Parse a wacli message timestamp into a tz-aware UTC datetime.

    wacli stores timestamps as either ISO strings or unix epoch seconds.
    Falls back to "now" on parse failure to keep the import flowing.
    """
    try:
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Contact processing
# ---------------------------------------------------------------------------

def _upsert_contact_source(
    source_store,
    person_store,
    resolver,
    stats: dict,
    source_id: str,
    existing_source: Optional[SourceEntity],
    observed_name: str,
    observed_phone: str,
    metadata: dict,
    dry_run: bool,
) -> bool:
    """Create/update a WhatsApp SourceEntity and resolve+link it to a PersonEntity.

    Shared by the classic-contact and LID-contact branches of
    process_whatsapp_contacts so both paths get identical resolve/link/
    person-update behavior and stats counting. Returns True if a new
    SourceEntity was created (False on update), so callers can layer their
    own creation counters (e.g. lid_entities_created) on top.
    """
    source_entity = SourceEntity(
        source_type=SOURCE_WHATSAPP,
        source_id=source_id,
        observed_name=observed_name,
        observed_phone=observed_phone,
        metadata=metadata,
        observed_at=datetime.now(timezone.utc),
    )

    created = False
    if existing_source:
        if not dry_run:
            existing_source.observed_name = source_entity.observed_name
            existing_source.observed_phone = source_entity.observed_phone
            existing_source.metadata = source_entity.metadata
            existing_source.observed_at = datetime.now(timezone.utc)
            source_store.update(existing_source)
        stats["source_entities_updated"] += 1
        source_entity = existing_source
    else:
        if not dry_run:
            source_entity = source_store.add(source_entity)
        stats["source_entities_created"] += 1
        created = True

    result = resolver.resolve(
        name=observed_name,
        phone=observed_phone,
        create_if_missing=True,
    )

    if result and result.entity:
        person = result.entity
        person_updated = False

        if not existing_source or existing_source.canonical_person_id != person.id:
            if not dry_run:
                source_store.link_to_person(
                    source_entity.id,
                    person.id,
                    confidence=0.95,
                    status=LINK_STATUS_AUTO,
                )
            stats["persons_linked"] += 1

        if observed_phone and observed_phone not in person.phone_numbers:
            person.phone_numbers.append(observed_phone)
            if not person.phone_primary:
                person.phone_primary = observed_phone
            person_updated = True

        if SOURCE_WHATSAPP not in person.sources:
            person.sources.append(SOURCE_WHATSAPP)
            person_updated = True

        if not dry_run:
            new_count = source_store.count_for_person(person.id)
            if person.source_entity_count != new_count:
                person.source_entity_count = new_count
                person_updated = True

        if person_updated:
            if not dry_run:
                person_store.update(person)
            stats["persons_updated"] += 1

        if result.is_new:
            stats["persons_created"] += 1

    return created


def process_whatsapp_contacts(
    contacts: list[dict],
    lid_contacts: Optional[list[dict]] = None,
    lid_phones: Optional[dict[str, str]] = None,
    dry_run: bool = False,
) -> dict:
    """Create/update SourceEntity and PersonEntity records from WhatsApp contacts.

    Also processes `lid_contacts` (WhatsApp's LID privacy migration means new
    contacts arrive here instead of in `contacts` — see module docstring).
    Each LID entry needs a non-empty push_name AND a phone resolvable via
    `lid_phones`; entries missing either are skipped, mirroring the nameless-
    number filter on the classic branch below. Before creating a new
    LID-keyed entity, checks whether a classic `whatsapp_{phone-jid}` entity
    already exists for the same phone and updates that instead — avoids
    creating a duplicate person for someone who's already a classic contact.

    Args:
        contacts: List of contact dicts with keys JID, Phone, Name, Alias.
        lid_contacts: List of {jid, push_name} for @lid contacts.
        lid_phones: Map of bare LID → raw phone (from whatsmeow lid_map).
        dry_run: If True, count what would happen without writing.

    Returns:
        Stats dict.
    """
    lid_contacts = lid_contacts or []
    lid_phones = lid_phones or {}

    stats = {
        "contacts_read": len(contacts),
        "source_entities_created": 0,
        "source_entities_updated": 0,
        "persons_linked": 0,
        "persons_created": 0,
        "persons_updated": 0,
        "skipped": 0,
        "errors": 0,
        "lid_contacts_read": len(lid_contacts),
        "lid_entities_created": 0,
        "lid_merged_into_classic": 0,
        "lid_skipped": 0,
    }

    source_store = get_source_entity_store()
    person_store = get_person_entity_store()
    resolver = get_entity_resolver()

    for contact in contacts:
        try:
            jid = contact.get("JID", "")
            phone_raw = contact.get("Phone", "")
            name = (contact.get("Name") or "").strip()
            alias = (contact.get("Alias") or "").strip()

            phone = normalize_phone(phone_raw)
            if not phone:
                stats["skipped"] += 1
                continue

            display_name = name or alias
            if not display_name or display_name == phone_raw:
                stats["skipped"] += 1
                continue

            source_id = f"whatsapp_{jid}"
            existing_source = source_store.get_by_source(SOURCE_WHATSAPP, source_id)

            _upsert_contact_source(
                source_store,
                person_store,
                resolver,
                stats,
                source_id=source_id,
                existing_source=existing_source,
                observed_name=display_name,
                observed_phone=phone,
                metadata={
                    "jid": jid,
                    "alias": alias,
                    "raw_phone": phone_raw,
                },
                dry_run=dry_run,
            )

        except Exception as e:
            logger.error(f"Error processing contact: {e}")
            stats["errors"] += 1

    for entry in lid_contacts:
        try:
            lid_jid = entry.get("jid", "")
            push_name = (entry.get("push_name") or "").strip()
            if not push_name:
                stats["lid_skipped"] += 1
                continue

            phone = resolve_lid_phone(lid_jid, lid_phones)
            if not phone:
                stats["lid_skipped"] += 1
                continue

            # Dedup: a classic {phone}@s.whatsapp.net entity for the same
            # phone already exists → update it, don't create a LID duplicate.
            classic_jid = f"{phone.lstrip('+')}@s.whatsapp.net"
            classic_source_id = f"whatsapp_{classic_jid}"
            existing_classic = source_store.get_by_source(SOURCE_WHATSAPP, classic_source_id)

            if existing_classic:
                source_id = classic_source_id
                existing_source = existing_classic
                stats["lid_merged_into_classic"] += 1
            else:
                source_id = f"whatsapp_{lid_jid}"
                existing_source = source_store.get_by_source(SOURCE_WHATSAPP, source_id)

            bare_lid = lid_jid.split("@")[0].split(":")[0] if lid_jid else ""

            created = _upsert_contact_source(
                source_store,
                person_store,
                resolver,
                stats,
                source_id=source_id,
                existing_source=existing_source,
                observed_name=push_name,
                observed_phone=phone,
                metadata={
                    "jid": lid_jid,
                    "lid": True,
                    "raw_phone": lid_phones.get(bare_lid, ""),
                },
                dry_run=dry_run,
            )

            if created and not existing_classic:
                stats["lid_entities_created"] += 1

        except Exception as e:
            logger.error(f"Error processing LID contact: {e}")
            stats["errors"] += 1

    if not dry_run:
        person_store.save()

    return stats


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def process_whatsapp_messages(
    messages: list[dict],
    group_participants: list[dict],
    lid_contacts: list[dict],
    lid_phones: dict[str, str],
    dry_run: bool = False,
    my_person_id: Optional[str] = None,
) -> dict:
    """Create Interaction records from WhatsApp messages.

    Args:
        messages: List of message dicts with keys msg_id, chat_jid, chat_name,
            sender_jid, sender_name, ts, from_me, text, display_text, media_type.
        group_participants: List of {group_jid, user_jid} pairs.
        lid_contacts: List of {jid, push_name} for @lid contacts.
        lid_phones: Map of bare LID → raw phone (from whatsmeow lid_map).
        dry_run: If True, count without writing.
        my_person_id: PersonEntity ID for the user (skipped to avoid
            self-referential interactions for outgoing group messages).

    Returns:
        Stats dict.
    """
    import sqlite3

    if my_person_id is None:
        from config.settings import settings
        my_person_id = settings.my_person_id

    stats = {
        "messages_read": len(messages),
        "interactions_created": 0,
        "interactions_skipped": 0,
        "skipped_lid": 0,
        "resolved_lid": 0,
        "resolved_lid_phone": 0,
        "skipped_large_group": 0,
        "outgoing_group_created": 0,
        "persons_not_found": 0,
        "errors": 0,
    }

    # Build lookups
    group_sizes: dict[str, int] = {}
    group_members: dict[str, list[str]] = {}
    for row in group_participants:
        gjid = row["group_jid"]
        ujid = row["user_jid"]
        group_sizes[gjid] = group_sizes.get(gjid, 0) + 1
        group_members.setdefault(gjid, []).append(ujid)

    lid_names: dict[str, str] = {
        c["jid"]: c["push_name"]
        for c in lid_contacts
        if c.get("jid") and c.get("push_name")
    }

    resolver = get_entity_resolver()
    interaction_db = get_interaction_db_path()

    affected_person_ids: set[str] = set()
    int_conn = sqlite3.connect(interaction_db)
    try:
        int_cursor = int_conn.cursor()

        # Get existing WhatsApp interactions to avoid duplicates
        int_cursor.execute(
            "SELECT source_id FROM interactions WHERE source_type = ?",
            (SOURCE_WHATSAPP,),
        )
        existing_ids = {row[0] for row in int_cursor.fetchall()}
        logger.info(f"Found {len(existing_ids)} existing WhatsApp interactions")

        batch: list[tuple] = []
        batch_size = 500

        def flush_batch():
            nonlocal batch
            if batch and not dry_run:
                int_cursor.executemany(
                    """
                    INSERT OR IGNORE INTO interactions
                        (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                int_conn.commit()
            batch = []

        for msg in messages:
            try:
                msg_id = msg["msg_id"]
                source_id = f"whatsapp_{msg_id}"
                if source_id in existing_ids:
                    stats["interactions_skipped"] += 1
                    continue

                sender_jid = msg.get("sender_jid") or ""
                sender_name = msg.get("sender_name") or ""
                chat_jid = msg.get("chat_jid") or ""
                chat_name = msg.get("chat_name") or ""
                from_me = bool(msg.get("from_me"))
                text = msg.get("display_text") or msg.get("text") or ""
                is_group = is_group_jid(chat_jid)

                if is_group and group_sizes.get(chat_jid, 0) > LARGE_GROUP_THRESHOLD:
                    stats["skipped_large_group"] += 1
                    continue

                ts = parse_message_timestamp(msg.get("ts"))
                snippet = text[:500] if text else ""

                # Outgoing group: fan out to each non-self participant
                if from_me and is_group:
                    for participant_jid in group_members.get(chat_jid, []):
                        participant_source_id = f"whatsapp_{msg_id}:{participant_jid}"
                        if participant_source_id in existing_ids:
                            continue

                        # phone stays None when we only have a push_name — passing None
                        # to the resolver is the explicit "no phone" signal. Matches the
                        # 1:1 path below.
                        p_phone: Optional[str] = None
                        p_name: Optional[str] = None
                        if "@lid" in participant_jid:
                            lid_phone = resolve_lid_phone(participant_jid, lid_phones)
                            if lid_phone:
                                p_phone = lid_phone
                                stats["resolved_lid_phone"] += 1
                            p_name = lid_names.get(participant_jid)
                            if not p_phone and not p_name:
                                continue
                        else:
                            extracted = extract_phone_from_jid(participant_jid)
                            if not extracted:
                                continue
                            p_phone = extracted

                        result = resolver.resolve(
                            name=p_name,
                            phone=p_phone,
                            create_if_missing=False,
                        )
                        if not result or not result.entity:
                            continue

                        person_id = result.entity.id
                        if person_id == my_person_id:
                            continue
                        affected_person_ids.add(person_id)
                        p_display = p_name or p_phone
                        title = f"WhatsApp → {chat_name} ({p_display})"

                        batch.append((
                            str(uuid.uuid4()),
                            person_id,
                            ts.isoformat(),
                            SOURCE_WHATSAPP,
                            title,
                            snippet,
                            None,
                            participant_source_id,
                            datetime.now(timezone.utc).isoformat(),
                        ))
                        stats["interactions_created"] += 1
                        stats["outgoing_group_created"] += 1
                    continue

                # 1:1 message — outgoing uses chat_jid (recipient), incoming uses sender_jid
                target_jid = chat_jid if from_me else sender_jid
                target_name = (chat_name or sender_name) if from_me else sender_name

                # phone may legitimately be None when we resolve a LID by push_name
                # only — passing None to the resolver is the explicit "no phone" signal.
                phone: Optional[str] = None
                if target_jid and "@lid" in target_jid:
                    lid_phone = resolve_lid_phone(target_jid, lid_phones)
                    if lid_phone:
                        phone = lid_phone
                        push_name = lid_names.get(target_jid)
                        if push_name:
                            target_name = push_name
                        stats["resolved_lid_phone"] += 1
                    else:
                        push_name = lid_names.get(target_jid)
                        if push_name:
                            target_name = push_name
                            stats["resolved_lid"] += 1
                        else:
                            stats["skipped_lid"] += 1
                            continue
                else:
                    phone = extract_phone_from_jid(target_jid)
                    if not phone:
                        stats["interactions_skipped"] += 1
                        continue

                result = resolver.resolve(
                    name=target_name or None,
                    phone=phone,
                    create_if_missing=True,
                )
                if not result or not result.entity:
                    stats["persons_not_found"] += 1
                    continue

                person_id = result.entity.id
                affected_person_ids.add(person_id)

                direction = "→" if from_me else "←"
                title = f"WhatsApp {direction} {target_name or phone}"

                batch.append((
                    str(uuid.uuid4()),
                    person_id,
                    ts.isoformat(),
                    SOURCE_WHATSAPP,
                    title,
                    snippet,
                    None,
                    source_id,
                    datetime.now(timezone.utc).isoformat(),
                ))
                stats["interactions_created"] += 1

                if len(batch) >= batch_size:
                    flush_batch()
                    logger.info(
                        f"Inserted batch — {stats['interactions_created']} interactions so far"
                    )

            except Exception as e:
                logger.error(f"Error processing message {msg.get('msg_id', '?')}: {e}")
                stats["errors"] += 1

        flush_batch()
    finally:
        int_conn.close()

    if not dry_run and affected_person_ids:
        from api.services.person_stats import refresh_person_stats
        logger.info(f"Refreshing stats for {len(affected_person_ids)} affected people...")
        refresh_person_stats(list(affected_person_ids))

    return stats
