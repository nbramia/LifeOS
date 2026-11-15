"""In-person interactions from the Pendant feed.

Reads `GET {pendant_feed_url}/interactions?from=&to=` (a text-free view of the
wearer's recorded conversations) and reconciles one `in_person` interaction
per person per local day, with provider `pendant` and source_id
`pendant:<YYYY-MM-DD>:<person_id>`.

Counting, per conversation and person:

- A speaker counts when labelled by the user, a meeting note or a calendar
  event, or when voiceprint-matched with a score at or above the feed's
  `match_threshold` plus `LIFEOS_PENDANT_MATCH_SCORE_MARGIN`. The wearer, the
  LifeOS owner, labels of a kind other than `person` (an empty kind is a
  person label), speakers without a person id,
  hidden people and `media` conversations never count. Person ids resolve
  through the merge map to their survivor.
- A conversation drops out for a person who already has a row covering it: a
  `calendar` row within 2 minutes of the start of the event the feed matched,
  a `phone` row starting within the conversation (5 minutes either side), or a
  `meeting` row from a note the feed linked.
- Conversations a person holds only through voiceprint matches count when
  that person spoke at least `LIFEOS_PENDANT_MATCH_MIN_SPEECH_SECONDS` across
  the day's remaining conversations.

A day's row is timed at the start of its last counted conversation, titled
with the conversation count and total minutes, links to the day's Pendant
note, and carries as `attendee_count` the number of counted people in its
largest counted conversation. Rows never carry conversation titles, text or
names.

Each run rewrites the window's rows: it upserts the rows that should exist
and deletes the window's other `in_person`/`pendant` rows. Days whose feed
request fails are left untouched.
"""
from __future__ import annotations

import logging
import posixpath
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from api.services import pendant_feed
from api.services.interaction_store import Interaction, build_obsidian_link
from config.settings import settings

logger = logging.getLogger(__name__)

SOURCE_TYPE = "in_person"
PROVIDER = "pendant"
SOURCE_ID_PREFIX = "pendant:"
MAX_CHUNK_DAYS = 31
CALENDAR_START_TOLERANCE = timedelta(minutes=2)
CALL_OVERLAP_TOLERANCE = timedelta(minutes=5)
FIRM_LABELS = frozenset({"user", "meeting", "calendar"})
MATCH_LABEL = "match"
# The feed leaves label_kind empty on person labels as well as naming them "person".
PERSON_LABEL_KINDS = frozenset({None, "person"})
CALL_SOURCE_TYPES = ("phone",)

# Counter keys reported by reconcile().
COUNTER_KEYS = (
    "conversations", "media_skipped",
    "speakers_wearer", "speakers_unlabeled", "speakers_no_person",
    "match_below_floor", "match_below_seconds",
    "dropped_calendar", "dropped_call", "dropped_meeting",
)

Fetch = Callable[[date, date], dict]


@dataclass
class Rules:
    margin: float
    min_speech_seconds: float
    note_path_template: str

    @classmethod
    def from_settings(cls) -> "Rules":
        return cls(
            margin=settings.pendant_match_score_margin,
            min_speech_seconds=settings.pendant_match_min_speech_seconds,
            note_path_template=settings.pendant_note_path_template,
        )


@dataclass
class _Conv:
    start_ms: int
    end_ms: int
    firm: bool
    speech_s: float
    size: int


@dataclass
class _Covered:
    """Rows already recording a person's time, keyed by canonical person id."""

    calendar: dict[str, list[datetime]] = field(default_factory=lambda: defaultdict(list))
    calls: dict[str, list[datetime]] = field(default_factory=lambda: defaultdict(list))
    meetings: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class ReconcileResult:
    counters: Counter
    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: int = 0
    days_reconciled: int = 0
    rows: int = 0
    people: int = 0
    affected_person_ids: set[str] = field(default_factory=set)
    error_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- feed


def fetch_interactions(start: date, end: date) -> dict:
    """One feed request covering `start`..`end` inclusive (at most 31 days)."""
    return pendant_feed.get_json(
        "interactions", {"from": start.isoformat(), "to": end.isoformat()}, timeout=30
    )


def fetch_first_day() -> Optional[date]:
    """The earliest day the feed holds (from `GET /days`), or None when empty."""
    data = pendant_feed.get_json("days")
    days = data.get("days")
    if not isinstance(days, list):
        raise pendant_feed.PendantFeedError("bad_json")
    parsed = []
    for entry in days:
        try:
            parsed.append(date.fromisoformat(entry["date"]))
        except (TypeError, KeyError, ValueError):
            raise pendant_feed.PendantFeedError("bad_json") from None
    return min(parsed) if parsed else None


def day_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split `start`..`end` inclusive into ranges of at most 31 days."""
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=MAX_CHUNK_DAYS - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def window_days(days: int, today: Optional[date] = None) -> tuple[date, date]:
    """The `days` local days ending today."""
    today = today or date.today()
    return today - timedelta(days=max(days, 1) - 1), today


# ---------------------------------------------------------------- helpers


def _ms_to_dt(ms) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _note_matches(source_id: str, note: str) -> bool:
    """A meeting row's stored path refers to the vault-relative `note`."""
    stored = source_id.split("#dup-", 1)[0].replace("\\", "/")
    note = note.replace("\\", "/").lstrip("/")
    if not note:
        return False
    if stored == note or stored.endswith("/" + note):
        return True
    return posixpath.basename(stored) == posixpath.basename(note)


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class _Resolver:
    """Feed entity ids to canonical, countable person ids (memoized)."""

    def __init__(self, person_store, owner_id: str):
        self._store = person_store
        self._owner = person_store.get_canonical_id(owner_id) if owner_id else None
        self._cache: dict[str, tuple[Optional[str], str]] = {}

    def canonical(self, person_id: str) -> str:
        return self._store.get_canonical_id(person_id)

    def resolve(self, entity_id: str) -> tuple[Optional[str], str]:
        """(person id, "") when countable, else (None, counter key)."""
        if entity_id not in self._cache:
            canon = self._store.get_canonical_id(entity_id)
            if self._owner and canon == self._owner:
                result = (None, "speakers_wearer")
            else:
                entity = self._store.get_by_id(canon)
                if entity is None or entity.hidden:
                    result = (None, "speakers_no_person")
                else:
                    result = (entity.id, "")
            self._cache[entity_id] = result
        return self._cache[entity_id]


def _load_covered(conn: sqlite3.Connection, resolver: _Resolver, start: datetime, end: datetime) -> _Covered:
    covered = _Covered()
    placeholders = ",".join("?" * (1 + len(CALL_SOURCE_TYPES)))
    for pid, source_type, ts in conn.execute(
        f"""
        SELECT person_id, source_type, timestamp FROM interactions
        WHERE source_type IN ({placeholders})
          AND julianday(timestamp) BETWEEN julianday(?) AND julianday(?)
        """,
        ("calendar", *CALL_SOURCE_TYPES, start.isoformat(), end.isoformat()),
    ):
        parsed = _parse_ts(ts)
        if parsed is None or not pid:
            continue
        target = covered.calendar if source_type == "calendar" else covered.calls
        target[resolver.canonical(pid)].append(parsed)
    for pid, source_id in conn.execute(
        "SELECT person_id, source_id FROM interactions WHERE source_type = 'meeting' AND source_id IS NOT NULL"
    ):
        if pid:
            covered.meetings[resolver.canonical(pid)].append(source_id)
    return covered


def _drop_reason(conv: dict, pid: str, covered: _Covered) -> Optional[str]:
    event = conv.get("calendar_event")
    if isinstance(event, dict) and _number(event.get("start_ms")) is not None:
        event_start = _ms_to_dt(event["start_ms"])
        if any(abs(ts - event_start) <= CALENDAR_START_TOLERANCE for ts in covered.calendar.get(pid, ())):
            return "dropped_calendar"
    start, end = _ms_to_dt(conv["start_ms"]), _ms_to_dt(conv["end_ms"])
    if any(start - CALL_OVERLAP_TOLERANCE <= ts <= end + CALL_OVERLAP_TOLERANCE for ts in covered.calls.get(pid, ())):
        return "dropped_call"
    notes = [n for n in conv.get("meeting_notes") or () if isinstance(n, str)]
    if notes and any(_note_matches(sid, n) for sid in covered.meetings.get(pid, ()) for n in notes):
        return "dropped_meeting"
    return None


def _speaker_people(conv: dict, floor: float, resolver: _Resolver, counters: Counter) -> dict[str, dict]:
    """Countable people in one conversation: pid -> {firm, speech_s}."""
    people: dict[str, dict] = {}
    for speaker in conv.get("speakers") or ():
        if not isinstance(speaker, dict):
            continue
        if speaker.get("wearer"):
            counters["speakers_wearer"] += 1
            continue
        labeled_by = speaker.get("labeled_by")
        if speaker.get("label_kind") not in PERSON_LABEL_KINDS or labeled_by not in FIRM_LABELS | {MATCH_LABEL}:
            counters["speakers_unlabeled"] += 1
            continue
        entity_id = speaker.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            counters["speakers_no_person"] += 1
            continue
        pid, reason = resolver.resolve(entity_id)
        if pid is None:
            counters[reason] += 1
            continue
        if labeled_by == MATCH_LABEL:
            score = _number(speaker.get("score"))
            if score is None or score < floor:
                counters["match_below_floor"] += 1
                continue
        entry = people.setdefault(pid, {"firm": False, "speech_s": 0.0})
        entry["firm"] = entry["firm"] or labeled_by in FIRM_LABELS
        entry["speech_s"] += max(_number(speaker.get("speech_s")) or 0.0, 0.0)
    return people


def _title(conversations: int, minutes: int) -> str:
    noun = "conversation" if conversations == 1 else "conversations"
    return f"In person · {conversations} {noun} · {minutes} min"


def desired_rows(
    conversations: Iterable[dict],
    match_threshold: float,
    days: set[str],
    covered: _Covered,
    resolver: _Resolver,
    rules: Rules,
    counters: Counter,
) -> dict[str, dict]:
    """The rows that should exist for `days`, keyed by source_id."""
    floor = round(match_threshold + rules.margin, 6)  # 0.55 + 0.05 is not 0.6 in floats
    candidates: dict[tuple[str, str], list[_Conv]] = defaultdict(list)
    for conv in conversations:
        if not isinstance(conv, dict) or conv.get("day") not in days:
            continue
        if _number(conv.get("start_ms")) is None or _number(conv.get("end_ms")) is None:
            continue
        counters["conversations"] += 1
        if conv.get("kind") == "media":
            counters["media_skipped"] += 1
            continue
        people = _speaker_people(conv, floor, resolver, counters)
        for pid, info in people.items():
            reason = _drop_reason(conv, pid, covered)
            if reason:
                counters[reason] += 1
                continue
            candidates[(conv["day"], pid)].append(
                _Conv(int(conv["start_ms"]), int(conv["end_ms"]), info["firm"], info["speech_s"], len(people))
            )

    rows: dict[str, dict] = {}
    for (day, pid), convs in candidates.items():
        counted = [c for c in convs if c.firm]
        matched = [c for c in convs if not c.firm]
        if matched:
            if sum(c.speech_s for c in convs) >= rules.min_speech_seconds:
                counted.extend(matched)
            else:
                counters["match_below_seconds"] += len(matched)
        if not counted:
            continue
        minutes = round(sum(max(c.end_ms - c.start_ms, 0) for c in counted) / 60000)
        source_id = f"{SOURCE_ID_PREFIX}{day}:{pid}"
        rows[source_id] = {
            "person_id": pid,
            "timestamp": _ms_to_dt(max(c.start_ms for c in counted)).isoformat(),
            "title": _title(len(counted), minutes),
            "source_link": build_obsidian_link(rules.note_path_template.format(day=day)),
            "attendee_count": max(max(c.size for c in counted), 1),
        }
    return rows


# ---------------------------------------------------------------- reconcile


_ROW_FIELDS = ("person_id", "timestamp", "title", "source_link", "attendee_count")


def _existing_rows(conn: sqlite3.Connection, days: set[str]) -> dict[str, dict]:
    existing = {}
    for row in conn.execute(
        """
        SELECT id, source_id, person_id, timestamp, title, source_link, attendee_count, snippet
        FROM interactions
        WHERE source_type = ? AND provider = ? AND source_id LIKE ?
        """,
        (SOURCE_TYPE, PROVIDER, SOURCE_ID_PREFIX + "%"),
    ):
        day = row[1][len(SOURCE_ID_PREFIX):].split(":", 1)[0]
        if day in days:
            existing[row[1]] = dict(zip(("id", "source_id", *_ROW_FIELDS, "snippet"), row))
    return existing


def reconcile(
    start: date,
    end: date,
    *,
    db_path: str,
    person_store,
    rules: Optional[Rules] = None,
    owner_id: Optional[str] = None,
    fetch: Optional[Fetch] = None,
    dry_run: bool = False,
) -> ReconcileResult:
    """Recompute and write the in-person rows for `start`..`end` inclusive."""
    rules = rules or Rules.from_settings()
    fetch = fetch or fetch_interactions
    resolver = _Resolver(person_store, settings.my_person_id if owner_id is None else owner_id)
    result = ReconcileResult(counters=Counter({k: 0 for k in COUNTER_KEYS}))

    fetched: list[tuple[set[str], list, float]] = []
    for chunk_start, chunk_end in day_chunks(start, end):
        try:
            data = fetch(chunk_start, chunk_end)
            conversations = data.get("conversations")
            threshold = _number(data.get("match_threshold"))
            if not isinstance(conversations, list) or threshold is None:
                raise pendant_feed.PendantFeedError("bad_json")
        except (pendant_feed.PendantFeedError, AttributeError) as e:
            reason = str(e) if isinstance(e, pendant_feed.PendantFeedError) else "bad_json"
            logger.error("Pendant feed request failed for %s..%s: %s", chunk_start, chunk_end, reason)
            result.errors += 1
            result.error_reasons.append(reason)
            continue
        days = {(chunk_start + timedelta(days=i)).isoformat() for i in range((chunk_end - chunk_start).days + 1)}
        fetched.append((days, conversations, threshold))

    if not fetched:
        return result

    all_days = set().union(*(d for d, _, _ in fetched))
    span_start = datetime.combine(min(date.fromisoformat(d) for d in all_days), datetime.min.time(), timezone.utc)
    span_end = datetime.combine(max(date.fromisoformat(d) for d in all_days), datetime.min.time(), timezone.utc)

    conn = sqlite3.connect(db_path)
    try:
        covered = _load_covered(conn, resolver, span_start - timedelta(days=2), span_end + timedelta(days=3))
        desired: dict[str, dict] = {}
        for days, conversations, threshold in fetched:
            desired.update(desired_rows(conversations, threshold, days, covered, resolver, rules, result.counters))
        existing = _existing_rows(conn, all_days)

        now_iso = datetime.now(timezone.utc).isoformat()
        inserts, updates = [], []
        for source_id, row in desired.items():
            Interaction(
                id="", person_id=row["person_id"], timestamp=datetime.fromisoformat(row["timestamp"]),
                source_type=SOURCE_TYPE, title=row["title"],
            ).validate()
            old = existing.get(source_id)
            if old is None:
                inserts.append((
                    str(uuid.uuid4()), row["person_id"], row["timestamp"], SOURCE_TYPE, row["title"],
                    None, row["source_link"], source_id, now_iso, None, row["attendee_count"], PROVIDER,
                ))
            elif any(old[k] != row[k] for k in _ROW_FIELDS) or old["snippet"] is not None:
                updates.append((
                    row["person_id"], row["timestamp"], row["title"], row["source_link"],
                    row["attendee_count"], old["id"],
                ))
                result.affected_person_ids.add(old["person_id"])
            else:
                continue
            result.affected_person_ids.add(row["person_id"])
        stale = [old for sid, old in existing.items() if sid not in desired]
        result.affected_person_ids.update(old["person_id"] for old in stale)

        if not dry_run:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO interactions
                    (id, person_id, timestamp, source_type, title, snippet, source_link, source_id,
                     created_at, source_account, attendee_count, provider)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    inserts,
                )
                conn.executemany(
                    """
                    UPDATE interactions
                    SET person_id = ?, timestamp = ?, title = ?, source_link = ?,
                        attendee_count = ?, snippet = NULL
                    WHERE id = ?
                    """,
                    updates,
                )
                conn.executemany("DELETE FROM interactions WHERE id = ?", [(old["id"],) for old in stale])
    finally:
        conn.close()

    result.created, result.updated, result.deleted = len(inserts), len(updates), len(stale)
    result.days_reconciled = len(all_days)
    result.rows = len(desired)
    result.people = len({row["person_id"] for row in desired.values()})
    return result
