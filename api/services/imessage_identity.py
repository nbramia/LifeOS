"""Identity of iMessage interactions, independent of chat.db rowids.

A chat.db ROWID differs between Macs and changes when the database is rebuilt,
and the exported timestamp of one message can differ at sub-second level
between exports. An iMessage interaction is therefore identified by a message
key: ``(UTC timestamp floored to the whole second, direction)``.

Several real messages can share one key (attachment bursts, group sends), so
keys are counted as a multiset, and messages from different people can share a
key. Matching is two-stage (see ``match_messages``): a message first claims an
unused interaction of the same person at its key, then any still-unmatched
message claims a leftover interaction of any person at that key (a relinked
message), and what remains is new.

Shared by ``scripts/sync_imessage_interactions.py`` and
``scripts/repair_imessage_duplicates.py`` so both apply the same rules.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Optional

OUTGOING_MARK = "→"
INCOMING_MARK = "←"

OUTGOING = "out"
INCOMING = "in"

# (whole seconds since the Unix epoch, direction)
MessageKey = tuple[int, str]

# Interaction ids written with the chat.db rowid embedded: ``imessage_<rowid>``.
_LEGACY_ID_RE = re.compile(r"^imessage_(\d+)$")

_PERSON_LOOKUP_CHUNK = 500


def parse_timestamp(value) -> Optional[datetime]:
    """Parse a stored ISO timestamp; timezone-naive values are read as UTC."""
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def make_key(ts: datetime, direction: str) -> MessageKey:
    """Key for an aware datetime: whole UTC second plus direction."""
    return int(ts.replace(microsecond=0).timestamp()), direction


def direction_for_is_from_me(is_from_me) -> str:
    return OUTGOING if is_from_me else INCOMING


def direction_from_title(title: Optional[str]) -> Optional[str]:
    """Direction encoded in an interaction title prefix, or None if absent."""
    if not title:
        return None
    mark = title.lstrip()[:1]
    if mark == OUTGOING_MARK:
        return OUTGOING
    if mark == INCOMING_MARK:
        return INCOMING
    return None


def message_key(timestamp, is_from_me) -> Optional[MessageKey]:
    """Key of a message-store row, or None when its timestamp is unusable."""
    ts = parse_timestamp(timestamp)
    if ts is None:
        return None
    return make_key(ts, direction_for_is_from_me(is_from_me))


def interaction_key(timestamp, title) -> Optional[MessageKey]:
    """Key of an iMessage interaction row, or None when it cannot be derived."""
    direction = direction_from_title(title)
    ts = parse_timestamp(timestamp)
    if direction is None or ts is None:
        return None
    return make_key(ts, direction)


def source_id_for(key: MessageKey, ordinal: int) -> str:
    """Rowid-free source_id for the ``ordinal``-th interaction at ``key``."""
    seconds, direction = key
    digest = hashlib.sha1(f"{seconds}|{direction}|{ordinal}".encode()).hexdigest()
    return f"imessage_k_{digest[:20]}"


def claim_source_id(key: MessageKey, taken: set[str]) -> str:
    """Return the lowest-ordinal source_id for ``key`` not in ``taken``.

    The id is added to ``taken``, so repeated calls never return the same id
    and the UNIQUE(source_type, source_id) index cannot drop a new row.
    """
    ordinal = 0
    while True:
        candidate = source_id_for(key, ordinal)
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        ordinal += 1


def legacy_rowid(source_id: Optional[str]) -> Optional[int]:
    """Rowid embedded in a legacy ``imessage_<rowid>`` id, else None.

    Only the one-time repair script uses this, to look up the message a
    legacy id once pointed at; the sync never interprets ids.
    """
    match = _LEGACY_ID_RE.match(source_id or "")
    return int(match.group(1)) if match else None


NO_TEXT = "[No text content]"

# Titles whose normalized text is at least this similar count as the same message.
TITLE_SIMILARITY = 0.8


def title_text(title: Optional[str]) -> str:
    """Message text of an interaction title: the title without its direction mark."""
    title = (title or "").strip()
    if title[:1] in (OUTGOING_MARK, INCOMING_MARK):
        title = title[1:]
    return title.strip()


def _normalize_text(text: str) -> str:
    """NFKC, casefolded, with punctuation and whitespace removed."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        ch for ch in folded
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def title_match_tier(candidate_title: Optional[str], partner_title: Optional[str]) -> Optional[int]:
    """How well ``partner_title`` stands in for ``candidate_title``.

    0 when both have the same direction and the same text (equal after
    normalization); 1 when the text is similar (difflib ratio >=
    ``TITLE_SIMILARITY``) or the candidate has no text; None otherwise. A
    candidate with text is never matched to a no-text partner.
    """
    direction = direction_from_title(candidate_title)
    if direction is None or direction != direction_from_title(partner_title):
        return None
    candidate = title_text(candidate_title)
    partner = title_text(partner_title)
    if candidate == NO_TEXT:
        return 0 if partner == NO_TEXT else 1
    if partner == NO_TEXT:
        return None
    norm_candidate = _normalize_text(candidate)
    norm_partner = _normalize_text(partner)
    if not norm_candidate or not norm_partner:
        # Punctuation-only text: compare as written.
        return 0 if candidate.casefold() == partner.casefold() else None
    if norm_candidate == norm_partner:
        return 0
    ratio = difflib.SequenceMatcher(None, norm_candidate, norm_partner).ratio()
    return 1 if ratio >= TITLE_SIMILARITY else None


def load_existing(conn: sqlite3.Connection, person_store) -> tuple[Counter, set[str]]:
    """Existing iMessage interactions: counts per (key, person) and all source_ids.

    Each row's person is the surviving person after any merge, so rows under
    a merged id match messages of the survivor. Counts depend only on each
    row's timestamp, title and person, never on the source_id format.
    """
    rows = conn.execute(
        "SELECT source_id, timestamp, title, person_id FROM interactions "
        "WHERE source_type = 'imessage'"
    ).fetchall()
    people = resolve_people(person_store, (row[3] for row in rows))
    counts: Counter = Counter()
    source_ids: set[str] = set()
    for source_id, timestamp, title, person_id in rows:
        if source_id:
            source_ids.add(source_id)
        key = interaction_key(timestamp, title)
        if key is not None:
            person = people.get(person_id)
            counts[(key, person.id if person else person_id)] += 1
    return counts, source_ids


def match_messages(items: list[tuple[MessageKey, str]], existing: Counter) -> list[bool]:
    """Match messages to existing interactions; True where already synced.

    ``items`` are ``(key, surviving person id)`` per message and ``existing``
    counts interactions per ``(key, surviving person id)``; it is consumed.
    Stage one matches each message to an unused interaction of the same person
    at its key. Stage two lets the still-unmatched messages take leftover
    interactions at that key from any person, so a message relinked to another
    person does not duplicate. Unmatched messages are new.
    """
    matched = [False] * len(items)
    for index, (key, person_id) in enumerate(items):
        if existing[(key, person_id)] > 0:
            existing[(key, person_id)] -= 1
            matched[index] = True

    leftover: Counter = Counter()
    for (key, _person_id), count in existing.items():
        if count > 0:
            leftover[key] += count
    for index, (key, _person_id) in enumerate(items):
        if not matched[index] and leftover[key] > 0:
            leftover[key] -= 1
            matched[index] = True
    return matched


def resolve_people(person_store, person_ids: Iterable[str]) -> dict:
    """Surviving PersonEntity per original id, in a few batched queries.

    Ids merged into another person map to the survivor, so callers decide on
    the survivor's ``hidden`` flag and write the survivor's id. Ids with no
    surviving entity are absent.
    """
    ids = sorted({i for i in person_ids if i})
    resolved: dict = {}
    for start in range(0, len(ids), _PERSON_LOOKUP_CHUNK):
        resolved.update(person_store.get_by_ids(ids[start:start + _PERSON_LOOKUP_CHUNK]))
    return resolved
