"""Identity of iMessage interactions, independent of chat.db rowids.

A chat.db ROWID differs between Macs and changes when the database is rebuilt,
and the exported timestamp of one message can differ at sub-second level
between exports. An iMessage interaction is therefore identified by a message
key: ``(UTC timestamp floored to the whole second, direction)``.

Several real messages can share one key (attachment bursts, group sends), so
keys are counted as a multiset. A message is "already synced" when an unused
interaction exists at its key; otherwise it is new.

Shared by ``scripts/sync_imessage_interactions.py`` and
``scripts/repair_imessage_duplicates.py`` so both apply the same rules.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
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


def load_existing(conn: sqlite3.Connection) -> tuple[Counter, set[str]]:
    """Existing iMessage interactions: per-key counts and all source_ids.

    Counts depend only on each row's timestamp and title, never on the
    source_id format.
    """
    counts: Counter = Counter()
    source_ids: set[str] = set()
    for source_id, timestamp, title in conn.execute(
        "SELECT source_id, timestamp, title FROM interactions "
        "WHERE source_type = 'imessage'"
    ):
        if source_id:
            source_ids.add(source_id)
        key = interaction_key(timestamp, title)
        if key is not None:
            counts[key] += 1
    return counts, source_ids


def consume(counts: Counter, key: MessageKey) -> bool:
    """Consume one existing interaction at ``key``.

    True when one was available (the message is already synced); False when
    the message is new.
    """
    if counts[key] > 0:
        counts[key] -= 1
        return True
    return False


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
