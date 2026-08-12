"""
Journal trend views: the strip, the unexplored wheel, felt-vs-recorded
connection, and the scalar stack (issue follow-up to #212).

The original emotion wheel (`api/routes/journal.py`) answers "what did I
feel most often" for a single window. Operator feedback on that view was
that it says nothing about *trajectory* — and with 32 entries spanning
2026-01-27 to 2026-06-22 in three bursts (Jan-Feb, April, June) rather than
a steady cadence, "trajectory" mostly means "how sparse and clustered is
this, actually" before anything else. These four views are the response;
see docs/specs/product/journal-analytics.md for the full writeup, including
options considered and rejected.

This is a separate module from `journal.py` rather than an extension of it
because each view pulls in a data source the wheel never needed —
`data/gsheet_sync.db` (view B), `data/interactions.db` plus
`api.services.entity_resolver` (view C) — and `journal.py` at 372 lines
before this file existed was already a full unit on its own. Splitting
keeps the wheel's import surface untouched.

Deliberately reuses rather than reimplements, per the issue's explicit
instruction:
- `api.routes.journal.window_bounds` / `_canonical_window` — the same
  day/week/month/quarter/all-time semantics as the wheel.
- `api.routes.journal._iter_valid_journal_files` / `collect_window` — the
  same symlink-safe, filename-is-canonical, UTF-8-safe file walk.
- `api.routes.journal._is_plausible_label` / `_display_value` — the same
  value-disclosure shape policy, applied here to the taxonomy labels
  extracted from the Google Sheet's column names (view B) as a defense-in
  depth measure, even though that source is form structure rather than
  journal free text.

None of these are re-exported as a public API of this module; the imports
below are intentionally of the underscore-prefixed originals, because
duplicating the logic under a second name would be exactly the
reinvention the issue asked to avoid.

`resonant_moment` (free-text, the most sensitive field in the file) and
`one_word` are never read here, matching the wheel's existing boundary.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.routes.journal import (
    _DEFAULT_WINDOW,
    _canonical_window,
    _is_plausible_label,
    _iter_valid_journal_files,
    collect_window,
    parse_emotion_chain,
    window_bounds,
)
from api.services.entity_resolver import get_entity_resolver
from api.services.gsheet_sync import get_gsheet_sync_db_path
from api.services.interaction_store import get_interaction_db_path
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/journal", tags=["journal"])

# The four self-reported scalar fields the daily journal logs on (per
# measured real-vault coverage) all 32 entries. Fixed by the feature
# request itself ("mood, stress, sleep, body as aligned sparklines") —
# unlike the emotion vocabulary in view B, there's no "the form might add a
# fifth scalar" concern driving this toward a derived list.
_SCALAR_FIELDS = ("mood", "stress", "sleep", "body")

# Below this confidence, a name resolution is disclosed as low-confidence
# rather than presented as a plain match — same spirit as the emotion
# wheel's value-disclosure policy, applied to person resolution instead of
# free text.
_LOW_CONFIDENCE_THRESHOLD = 0.5

# Column names in the Google Sheet backing the journal that hold a
# follow-up emotion branch look like "<Branch> feelings" or, for a couple
# of irregular branches (see journal.py's module docstring), "<Branch>
# feeling" — optionally with a trailing "?" (Google Forms often keeps the
# question mark in the column header). Matches case-insensitively and
# captures everything before the suffix as the branch name.
_FEELINGS_SUFFIX_RE = re.compile(r"\s+feelings?\s*\??\s*$", re.IGNORECASE)

_TRUTHY = {"yes", "y", "true", "1"}
_FALSY = {"no", "n", "false", "0"}


# ---------------------------------------------------------------------------
# Shared window/grid helpers
# ---------------------------------------------------------------------------

def _collect_entries(vault_path, window: str, today: date) -> list[tuple[date, dict]]:
    """All valid journal entries in the window, oldest first."""
    return sorted(_iter_valid_journal_files(vault_path, window, today), key=lambda pair: pair[0])


def _grid_span(window: str, today: date, entries: list[tuple[date, dict]]) -> tuple[date | None, date]:
    """Concrete [start, end] for calendar-grid views (the strip, the scalar
    stack) — both need every day in the range, not just the days with data.

    `window_bounds("all-time")` returns `start=None` ("no lower bound") by
    design for the wheel's aggregation, which never needs a finite range.
    A calendar grid can't render an unbounded range, so all-time here means
    "from the earliest entry actually in view" instead — the tightest
    honest span, not an arbitrary long window. Day/week/month/quarter keep
    `window_bounds`'s fixed bounds unchanged: a short window's grid must
    not shrink just because its entries (if any) cluster near one end.
    """
    start, end = window_bounds(window, today)
    if start is None:
        start = entries[0][0] if entries else None
    return start, end


# ---------------------------------------------------------------------------
# View A: the strip
# ---------------------------------------------------------------------------

class StripDay(BaseModel):
    date: str
    has_entry: bool
    primary_emotion: str | None = None


class JournalStripResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    emotion_entries: int
    days: list[StripDay]


@router.get("/strip", response_model=JournalStripResponse)
async def get_journal_strip(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalStripResponse:
    """One cell per calendar day across the window, colored by that day's
    primary (level-1) emotion. Days with no journal file at all render as
    `has_entry=False` — an explicit gap, never skipped or interpolated —
    and days with a file but no parseable `feeling:` render as
    `has_entry=True, primary_emotion=None`, a third, distinct state from
    either "no journal that day" or "logged a feeling"."""
    window = _canonical_window(window)
    today = date.today()
    entries = _collect_entries(settings.vault_path, window, today)
    start, end = _grid_span(window, today, entries)
    by_date = dict(entries)

    days: list[StripDay] = []
    emotion_entries = 0
    if start is not None:
        cur = start
        while cur <= end:
            fm = by_date.get(cur)
            if fm is None:
                days.append(StripDay(date=cur.isoformat(), has_entry=False))
            else:
                chain = parse_emotion_chain(fm)
                if chain:
                    emotion_entries += 1
                days.append(StripDay(date=cur.isoformat(), has_entry=True, primary_emotion=chain[0] if chain else None))
            cur += timedelta(days=1)

    return JournalStripResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=len(entries),
        emotion_entries=emotion_entries,
        days=days,
    )


# ---------------------------------------------------------------------------
# View B: the unexplored wheel
# ---------------------------------------------------------------------------

class TaxonomyBranch(BaseModel):
    label: str
    used: bool
    count: int


class JournalTaxonomyResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    emotion_entries: int
    taxonomy_source: str  # "form" or "used-only"
    branches: list[TaxonomyBranch]
    extra_used: list[TaxonomyBranch]


def _derive_taxonomy_labels(db_path: str) -> list[str]:
    """Read `data/gsheet_sync.db` -> `synced_rows.raw_data` for column names
    that look like an emotion-branch follow-up question, and return the
    deduped branch labels (suffix stripped, original casing kept).

    Returns `[]` whenever the source can't be trusted to be complete or
    even present — missing file, missing/corrupt table, or a file that
    exists but the table is simply empty. Callers must treat `[]` as
    "taxonomy unavailable" and degrade to a used-only view, not as
    "the form has zero branches".

    Deliberately does not hardcode the ~48 branch words a manual
    inspection of one real vault's sheet found — that list is sourced from
    a Google Form Nathan configured for his own journal, would silently go
    stale the moment the form changes, and would be exactly the kind of
    personal-value hardcoding this project's own contribution guidelines
    rule out for a codebase other people run against their own forms.
    Every row ever synced is scanned (not just the latest), so a branch
    the form used to have but later removed still shows up as "available,
    unused" rather than disappearing.

    Opens the database read-only (`mode=ro`) so a missing file is a clean
    "not found" instead of sqlite3 silently creating an empty one — this
    endpoint must never have the side effect of creating
    `data/gsheet_sync.db` on a machine that has never configured the
    Google Sheets sync at all.
    """
    path = Path(db_path)
    if not path.exists():
        return []

    labels: dict[str, str] = {}  # lowercase -> display label (first-seen casing)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        try:
            cursor = conn.execute("SELECT raw_data FROM synced_rows")
        except sqlite3.OperationalError:
            return []  # table missing/corrupt
        for (raw_json,) in cursor.fetchall():
            if not raw_json:
                continue
            try:
                row = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            for key in row:
                if not isinstance(key, str):
                    continue
                match = _FEELINGS_SUFFIX_RE.search(key)
                if not match:
                    continue
                branch = key[: match.start()].strip()
                if not branch or not _is_plausible_label(branch):
                    continue  # same shape policy as journal.py, defense in depth
                lower = branch.lower()
                if lower not in labels:
                    labels[lower] = branch
    finally:
        conn.close()
    return sorted(labels.values())


@router.get("/taxonomy", response_model=JournalTaxonomyResponse)
async def get_journal_taxonomy(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalTaxonomyResponse:
    """The full form taxonomy (derived, not hardcoded — see
    `_derive_taxonomy_labels`), each branch marked used/unused with a
    frequency count over the window. `extra_used` holds values that were
    actually logged but don't match any derived branch (a stray value like
    "Not sure" that isn't itself a branch name, the "Unrecognized" bucket
    from the disclosure policy, or taxonomy drift if the form changed since
    the sheet was last synced) — these are never dropped, just not folded
    into the known-branch list.

    When the sheet database is absent, empty, or has no matching columns,
    `taxonomy_source` is `"used-only"` and `branches` is empty; every
    observed value appears in `extra_used` instead, which is exactly the
    old wheel's flat frequency list — a real degraded mode, not a failure.
    """
    window = _canonical_window(window)
    today = date.today()
    chains, total_entries, emotion_entries = collect_window(settings.vault_path, window, today)

    value_counts: Counter[str] = Counter()
    for chain in chains:
        value_counts.update(chain)

    taxonomy_labels = _derive_taxonomy_labels(get_gsheet_sync_db_path())
    taxonomy_source = "form" if taxonomy_labels else "used-only"
    matched_lower: set[str] = set()

    branches: list[TaxonomyBranch] = []
    for label in taxonomy_labels:
        key = label.lower()
        count = sum(c for v, c in value_counts.items() if v.lower() == key)
        if count > 0:
            matched_lower.add(key)
        branches.append(TaxonomyBranch(label=label, used=count > 0, count=count))
    branches.sort(key=lambda b: (-b.count, b.label))

    extra_used = [
        TaxonomyBranch(label=value, used=True, count=count)
        for value, count in value_counts.items()
        if value.lower() not in matched_lower and value.lower() not in {lbl.lower() for lbl in taxonomy_labels}
    ]
    extra_used.sort(key=lambda b: (-b.count, b.label))

    start, end = window_bounds(window, today)
    return JournalTaxonomyResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=total_entries,
        emotion_entries=emotion_entries,
        taxonomy_source=taxonomy_source,
        branches=branches,
        extra_used=extra_used,
    )


# ---------------------------------------------------------------------------
# View C: felt vs. recorded connection
# ---------------------------------------------------------------------------

class ConnectionResolution(BaseModel):
    status: str  # "resolved" | "low_confidence" | "ambiguous" | "unresolved"
    canonical_name: str | None = None
    confidence: float | None = None
    note: str


class ConnectionDay(BaseModel):
    date: str
    self_reported: bool
    interaction_count: int | None = None  # None only when resolution failed


class ConnectionField(BaseModel):
    field: str  # raw frontmatter key, e.g. "connection_taylor"
    queried_name: str  # what was handed to the entity resolver
    resolution: ConnectionResolution
    field_entries: int
    unparseable_entries: int
    days: list[ConnectionDay]


class JournalConnectionsResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    fields: list[ConnectionField]


def _parse_bool_field(value) -> bool | None:
    """Best-effort boolean parse for a `connection_<name>` value. Accepts a
    real YAML boolean (the expected case — PyYAML/`frontmatter` already
    parses `true`/`false`/`yes`/`no` literals as Python `bool`), a small
    set of common string/int spellings, and nothing else. Returns `None`
    for anything unparseable so the caller can skip that day rather than
    guess.

    No real journal data was available while building this (the project's
    own rule is to never read the real journal in code or tests), so this
    intentionally covers more than just the one form-emitted shape rather
    than assuming a single exact type.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUTHY:
            return True
        if v in _FALSY:
            return False
    return None


def _interaction_counts_by_day(person_id: str, start: date, end: date, db_path: str) -> dict[str, int]:
    """Count-only lookup of interactions for one person, grouped by
    calendar day, over `[start, end]`. Never reads title/snippet/source
    content — the query itself only ever selects `timestamp`, so there is
    nothing to leak even in memory, matching the "counts only, never
    content" privacy constraint for this view.

    Uses substring comparison on the ISO timestamp text for both the date
    grouping and the range filter, matching the existing convention in
    `InteractionStore.get_all_in_range`'s `specific_date` branch — accepted
    imprecision near a timezone-offset midnight boundary, not something
    this view introduces.

    Returns `{}` without connecting if the database file doesn't exist —
    same "never create data/interactions.db as a side effect of a read"
    posture as `_derive_taxonomy_labels`.
    """
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        try:
            cursor = conn.execute(
                "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) FROM interactions "
                "WHERE person_id = ? AND substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) <= ? "
                "GROUP BY day",
                (person_id, start.isoformat(), end.isoformat()),
            )
        except sqlite3.OperationalError:
            return {}
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _resolve_connection_name(name: str) -> tuple[ConnectionResolution, str | None]:
    """Resolve a `connection_<name>` slug to a person, disclosing
    ambiguity and low confidence rather than silently picking a match.

    Returns the disclosure-facing `ConnectionResolution` alongside the
    resolved person's id (or `None`), so the caller can fetch interaction
    counts without re-running resolution a second time.

    `resolve_by_name` collapses two different ambiguity outcomes into the
    same `None` return (fully ambiguous with no dominant candidate vs.
    genuinely no match) — both are reported here as "unresolved" since the
    caller can't tell them apart from the return value alone, and neither
    can safely be attached to a specific person's interaction history.
    """
    resolver = get_entity_resolver()
    result = resolver.resolve_by_name(name, create_if_missing=False)
    if result is None:
        return (
            ConnectionResolution(
                status="unresolved",
                note=f"No confident match for '{name}' — interaction counts are not available for this field.",
            ),
            None,
        )
    if result.disambiguation_applied:
        return (
            ConnectionResolution(
                status="ambiguous",
                canonical_name=result.entity.canonical_name,
                confidence=result.confidence,
                note=(
                    f"'{name}' matched multiple similarly-scored people; showing counts for the closest "
                    f"guess ({result.entity.canonical_name}). Treat this comparison cautiously."
                ),
            ),
            result.entity.id,
        )
    if result.confidence < _LOW_CONFIDENCE_THRESHOLD:
        return (
            ConnectionResolution(
                status="low_confidence",
                canonical_name=result.entity.canonical_name,
                confidence=result.confidence,
                note=f"Low-confidence match for '{name}' ({result.entity.canonical_name}); treat counts cautiously.",
            ),
            result.entity.id,
        )
    return (
        ConnectionResolution(
            status="resolved",
            canonical_name=result.entity.canonical_name,
            confidence=result.confidence,
            note=f"'{name}' resolved to {result.entity.canonical_name}.",
        ),
        result.entity.id,
    )


@router.get("/connections", response_model=JournalConnectionsResponse)
async def get_journal_connections(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalConnectionsResponse:
    """For every `connection_<name>` field seen in the window, the
    self-reported boolean per day alongside the actual interaction count
    with that person from `data/interactions.db`.

    In-person connection leaves no digital trace, so a day marked
    "connected" with zero recorded interactions is not an error in either
    direction — it's information about *channel*, not about accuracy. This
    view (and the page that renders it) must never label a divergence as
    wrong. The more actionable direction is the reverse: a day *not*
    marked as connecting that has a nonzero interaction count, meaning a
    real exchange happened through a channel the self-report didn't
    credit.
    """
    window = _canonical_window(window)
    today = date.today()
    entries = _collect_entries(settings.vault_path, window, today)
    start, end = window_bounds(window, today)

    slugs: set[str] = set()
    for _, fm in entries:
        for key in fm:
            if isinstance(key, str) and key.startswith("connection_"):
                slugs.add(key)

    fields: list[ConnectionField] = []
    for field_key in sorted(slugs):
        name_slug = field_key[len("connection_"):]
        queried_name = name_slug.replace("_", " ").strip().title() or name_slug
        resolution, person_id = _resolve_connection_name(queried_name)

        counts_by_day: dict[str, int] = {}
        if person_id is not None:
            grid_start, _ = _grid_span(window, today, entries)
            counts_by_day = _interaction_counts_by_day(
                person_id, grid_start or today, end, get_interaction_db_path()
            )

        days: list[ConnectionDay] = []
        unparseable = 0
        for entry_date, fm in entries:
            if field_key not in fm:
                continue
            parsed = _parse_bool_field(fm.get(field_key))
            if parsed is None:
                unparseable += 1
                continue
            count = None if resolution.status == "unresolved" else counts_by_day.get(entry_date.isoformat(), 0)
            days.append(ConnectionDay(date=entry_date.isoformat(), self_reported=parsed, interaction_count=count))

        fields.append(
            ConnectionField(
                field=field_key,
                queried_name=queried_name,
                resolution=resolution,
                field_entries=len(days),
                unparseable_entries=unparseable,
                days=days,
            )
        )

    return JournalConnectionsResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=len(entries),
        fields=fields,
    )


# ---------------------------------------------------------------------------
# View D: the scalar stack
# ---------------------------------------------------------------------------

class ScalarPoint(BaseModel):
    date: str
    value: float


class ScalarSeries(BaseModel):
    field: str
    points: list[ScalarPoint]


class ScalarCorrelation(BaseModel):
    pair: list[str] = Field(default_factory=lambda: ["mood", "stress"])
    n: int
    r: float | None = None
    caveat: str


class JournalScalarsResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    series: list[ScalarSeries]
    correlation: ScalarCorrelation


def _numeric(value) -> float | None:
    """A frontmatter scalar as a float, or None if it isn't numeric.
    Excludes bool explicitly — `isinstance(True, int)` is True in Python,
    and a stray boolean here would otherwise silently become 1.0/0.0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@router.get("/scalars", response_model=JournalScalarsResponse)
async def get_journal_scalars(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalScalarsResponse:
    """`mood`, `stress`, `sleep`, `body` as one point per entry-day, on the
    same calendar grid as the strip (`_grid_span`) so the two views line up
    visually. The mood/stress correlation is computed and returned
    directly rather than only implied by a rendered trend line — see the
    module docstring and docs/specs/product/journal-analytics.md for why a
    fitted line is deliberately not part of this feature at all.
    """
    window = _canonical_window(window)
    today = date.today()
    entries = _collect_entries(settings.vault_path, window, today)
    start, end = _grid_span(window, today, entries)

    series: list[ScalarSeries] = []
    mood_by_date: dict[date, float] = {}
    stress_by_date: dict[date, float] = {}
    for field_name in _SCALAR_FIELDS:
        points: list[ScalarPoint] = []
        for entry_date, fm in entries:
            value = _numeric(fm.get(field_name))
            if value is None:
                continue
            points.append(ScalarPoint(date=entry_date.isoformat(), value=value))
            if field_name == "mood":
                mood_by_date[entry_date] = value
            elif field_name == "stress":
                stress_by_date[entry_date] = value
        series.append(ScalarSeries(field=field_name, points=points))

    paired_dates = sorted(set(mood_by_date) & set(stress_by_date))
    n = len(paired_dates)
    caveat = (
        "The journal form does not label which direction of the mood or stress scale is "
        "‘better’ — a positive correlation may mean mood holds up (or improves) under "
        "load, or it may mean one of the two fields is effectively reverse-coded relative to the "
        "other. This number describes co-movement only, not which interpretation is correct."
    )
    r: float | None = None
    if n < 2:
        caveat = f"Not enough paired mood/stress entries in this window (n={n}) to compute a correlation."
    else:
        moods = [mood_by_date[d] for d in paired_dates]
        stresses = [stress_by_date[d] for d in paired_dates]
        try:
            r = statistics.correlation(moods, stresses)
        except statistics.StatisticsError:
            r = None
            caveat = (
                f"Correlation is undefined for this window (n={n}) — mood or stress had no "
                "variance across these entries."
            )

    return JournalScalarsResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=len(entries),
        series=series,
        correlation=ScalarCorrelation(n=n, r=r, caveat=caveat),
    )
