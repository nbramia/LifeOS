"""
Journal trend views: the strip, the unexplored wheel, and the scalar stack
(issue follow-up to #212).

The original emotion wheel (`api/routes/journal.py`) answers "what did I
feel most often" for a single window. Operator feedback on that view was
that it says nothing about *trajectory* — and with 32 entries spanning
2026-01-27 to 2026-06-22 in three bursts (Jan-Feb, April, June) rather than
a steady cadence, "trajectory" mostly means "how sparse and clustered is
this, actually" before anything else. These views are the response; see
docs/specs/product/journal-analytics.md for the full writeup, including
options considered and rejected.

A fourth view — felt-vs-recorded connection, which cross-referenced
`connection_<name>` self-reports against `data/interactions.db` via
`api.services.entity_resolver` — was removed after operator feedback that
it wasn't useful (see the spec's Removed section). Removing it also
dropped this module's only reason to import the entity resolver or touch
the interactions database, which shrinks this module's privacy surface —
a real gain, not just a line-count one.

This is a separate module from `journal.py` rather than an extension of it
because the unexplored wheel pulls in a data source the original wheel
never needed — `data/gsheet_sync.db` — and `journal.py` at 372 lines
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
- `api.routes.journal.parse_emotion_chain` — reused a second time in this
  module (beyond the strip) to derive the unexplored wheel's primary-emotion
  grouping from real observed chains; see `_derive_branch_groups`.

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
from itertools import combinations
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from api.routes.journal import (
    _DEFAULT_WINDOW,
    _canonical_window,
    _is_plausible_label,
    _iter_valid_journal_files,
    collect_window,
    parse_emotion_chain,
    window_bounds,
)
from api.services.gsheet_sync import get_gsheet_sync_db_path
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/journal", tags=["journal"])

# The four self-reported scalar fields the daily journal logs on (per
# measured real-vault coverage) all 32 entries. Fixed by the feature
# request itself ("mood, stress, sleep, body as aligned sparklines") —
# unlike the emotion vocabulary in view B, there's no "the form might add a
# fifth scalar" concern driving this toward a derived list.
_SCALAR_FIELDS = ("mood", "stress", "sleep", "body")

# Every distinct unordered pair of scalar fields — 4 choose 2 = 6. Order
# within each pair matches `_SCALAR_FIELDS`'s order, so "mood vs. stress"
# is always `["mood", "stress"]`, never `["stress", "mood"]`.
_SCALAR_PAIRS = list(combinations(_SCALAR_FIELDS, 2))

# Column names in the Google Sheet backing the journal that hold a
# follow-up emotion branch look like "<Branch> feelings" or, for a couple
# of irregular branches (see journal.py's module docstring), "<Branch>
# feeling" — optionally with a trailing "?" (Google Forms often keeps the
# question mark in the column header). Matches case-insensitively and
# captures everything before the suffix as the branch name.
_FEELINGS_SUFFIX_RE = re.compile(r"\s+feelings?\s*\??\s*$", re.IGNORECASE)

# "Not sure" is excluded entirely from the unexplored wheel — from the
# derived branch list, the legend, the wheel's wedges, and every count —
# per explicit operator feedback: it's a non-answer to "what did you
# feel", not an emotion, so grouping it under some primary family (or
# giving it one of its own) would misrepresent it either way. It can
# appear both as a root `feeling:` value and as a leaf several steps into
# an otherwise-real chain (e.g. Sad -> ... -> "Not sure"); both positions
# are filtered, case-insensitively, everywhere this module touches it.
_EXCLUDED_VALUE = "not sure"

# The primary emotions the unexplored wheel groups every other branch
# under. Sourced from operator-supplied ground truth, not derived, because
# it can't be derived from anything in the data: `Angry`, `Bad`,
# `Disgusted`, `Happy`, and `Sad` are the root `feeling:` values actually
# observed across the real vault's entries; `Fearful` and `Surprised` never
# appear as a root but do have their own "<Primary> feelings" follow-up
# column in the form, so they're primaries the operator has simply never
# selected as a top-level feeling yet. "Not sure" is deliberately excluded
# (see `_EXCLUDED_VALUE`) even though it's also an observed root value.
_KNOWN_PRIMARIES = ("Angry", "Bad", "Disgusted", "Fearful", "Happy", "Sad", "Surprised")

# Bucket for a taxonomy branch that is neither a known primary itself nor
# reachable from one via any chain actually observed in the journal. A
# branch lands here rather than under a guessed primary — see
# `_derive_branch_groups`'s docstring for why a visible "don't know" bucket
# beats a silently wrong parent.
_UNPLACED_GROUP = "Unplaced"

# Wheel/legend group ordering: primaries in the fixed order above, then
# Unplaced last — never sorted by size or alphabetically, so the layout is
# stable across requests regardless of which groups happen to be biggest.
_GROUP_ORDER = (*_KNOWN_PRIMARIES, _UNPLACED_GROUP)


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
    group: str  # one of _KNOWN_PRIMARIES, or _UNPLACED_GROUP


class JournalTaxonomyResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    emotion_entries: int
    taxonomy_source: str  # "form" or "used-only"
    group_order: list[str]
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


def _derive_branch_groups(vault_path, today: date) -> dict[str, str]:
    """Map each branch label (lowercased) to the primary emotion it belongs
    to, derived from real parent -> child links actually observed in the
    journal — never from the form's column order.

    An earlier pass at this grouping assumed the form's `"<Node> feelings"`
    columns are laid out depth-first, so column order alone would reveal
    the tree. That assumption is false: in form order, `Playful` (index 29)
    comes immediately before `Happy` (index 30), even though `Playful` is
    one of `Happy`'s children in the standard wheel this form derives
    from. Order reflects however the form's author arranged the questions,
    not the tree structure, so it is never used here.

    What *is* reliable: every parsed emotion chain (`parse_emotion_chain`)
    is a real parent -> child path an entry actually recorded, e.g.
    `["Sad", "Muted", "Not sure"]` means "Muted" followed "Sad" and
    "Not sure" followed "Muted" in that one entry. This function walks
    every chain from every journal entry ever written — deliberately not
    window-scoped, since which primary a branch belongs to is a structural
    property of the form, not something that should shift depending on
    which time window happens to be selected — and, for every non-root
    value in a chain, casts a vote for the chain's root (`chain[0]`) as
    that value's primary. A branch's group is whichever root has the most
    votes; ties break alphabetically for determinism.

    "Not sure" chains are never used as evidence in either direction: a
    chain rooted at "Not sure" casts no votes (skipped outright), and
    "Not sure" appearing anywhere inside another chain is never itself
    recorded as a branch to be grouped (see `_EXCLUDED_VALUE` — it is
    excluded from the wheel entirely, so it needs no group). A root that
    isn't one of `_KNOWN_PRIMARIES` at all (which should not happen with a
    trustworthy chain, but a hand-edited entry could produce one) also
    casts no votes, rather than inventing an eighth primary on the fly.

    Returns only entries this function found votes for. A branch with no
    votes at all — never observed as a non-root value in any real chain —
    is absent from the returned dict; the caller (`get_journal_taxonomy`)
    treats that as `_UNPLACED_GROUP` rather than guessing. A branch that is
    itself one of `_KNOWN_PRIMARIES` doesn't need a vote to find its group
    at all — the caller checks that case first — but if one somehow
    received contrary votes anyway (real data quirk, not expected), those
    votes are still returned here; the caller's primary-name check takes
    precedence over anything in this dict.
    """
    primary_lower = {p.lower() for p in _KNOWN_PRIMARIES}
    votes: dict[str, Counter[str]] = {}
    for _, fm in _iter_valid_journal_files(vault_path, "all-time", today):
        chain = parse_emotion_chain(fm)
        if not chain:
            continue
        root = chain[0]
        if root.lower() == _EXCLUDED_VALUE or root.lower() not in primary_lower:
            continue  # "Not sure" root, or a root that isn't a known primary: no evidence to cast
        for value in chain[1:]:
            if value.lower() == _EXCLUDED_VALUE:
                continue
            votes.setdefault(value.lower(), Counter())[root] += 1

    groups: dict[str, str] = {}
    for label_lower, counter in votes.items():
        best_root, _count = min(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        groups[label_lower] = best_root
    return groups


def _group_for_label(label: str, branch_groups: dict[str, str]) -> str:
    """A branch's group: itself, if it's one of the known primaries;
    otherwise whatever `_derive_branch_groups` voted for it; otherwise the
    honest "don't know" bucket rather than a guessed parent."""
    lower = label.lower()
    for primary in _KNOWN_PRIMARIES:
        if lower == primary.lower():
            return primary
    return branch_groups.get(lower, _UNPLACED_GROUP)


@router.get("/taxonomy", response_model=JournalTaxonomyResponse)
async def get_journal_taxonomy(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalTaxonomyResponse:
    """The full form taxonomy (derived, not hardcoded — see
    `_derive_taxonomy_labels`), each branch marked used/unused with a
    frequency count over the window and grouped by primary emotion (see
    `_derive_branch_groups`). `extra_used` holds values that were actually
    logged but don't match any derived branch (the "Unrecognized" bucket
    from the disclosure policy, or taxonomy drift if the form changed since
    the sheet was last synced) — these are never dropped, just not folded
    into the known-branch list.

    "Not sure" is excluded entirely — from `branches`, from `extra_used`,
    and from every count — per explicit operator feedback that it's a
    non-answer, not an emotion. It is filtered case-insensitively wherever
    it could appear: as a derived taxonomy label, as a raw logged value,
    and (for grouping purposes) as either a chain's root or one of its
    non-root values.

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
        for value in chain:
            if value.lower() == _EXCLUDED_VALUE:
                continue
            value_counts[value] += 1

    taxonomy_labels = [
        label for label in _derive_taxonomy_labels(get_gsheet_sync_db_path())
        if label.lower() != _EXCLUDED_VALUE
    ]
    taxonomy_source = "form" if taxonomy_labels else "used-only"
    matched_lower: set[str] = set()
    branch_groups = _derive_branch_groups(settings.vault_path, today)
    group_rank = {g: i for i, g in enumerate(_GROUP_ORDER)}

    branches: list[TaxonomyBranch] = []
    for label in taxonomy_labels:
        key = label.lower()
        count = sum(c for v, c in value_counts.items() if v.lower() == key)
        if count > 0:
            matched_lower.add(key)
        branches.append(TaxonomyBranch(label=label, used=count > 0, count=count, group=_group_for_label(label, branch_groups)))
    branches.sort(key=lambda b: (group_rank.get(b.group, len(_GROUP_ORDER)), -b.count, b.label))

    extra_used = [
        TaxonomyBranch(label=value, used=True, count=count, group=_group_for_label(value, branch_groups))
        for value, count in value_counts.items()
        if value.lower() not in matched_lower and value.lower() not in {lbl.lower() for lbl in taxonomy_labels}
    ]
    extra_used.sort(key=lambda b: (group_rank.get(b.group, len(_GROUP_ORDER)), -b.count, b.label))

    start, end = window_bounds(window, today)
    return JournalTaxonomyResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=total_entries,
        emotion_entries=emotion_entries,
        taxonomy_source=taxonomy_source,
        group_order=list(_GROUP_ORDER),
        branches=branches,
        extra_used=extra_used,
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
    pair: list[str]
    n: int
    r: float | None = None
    caveat: str


class JournalScalarsResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    series: list[ScalarSeries]
    correlations: list[ScalarCorrelation]


def _numeric(value) -> float | None:
    """A frontmatter scalar as a float, or None if it isn't numeric.
    Excludes bool explicitly — `isinstance(True, int)` is True in Python,
    and a stray boolean here would otherwise silently become 1.0/0.0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pair_correlation(a: str, b: str, values_by_field: dict[str, dict[date, float]]) -> ScalarCorrelation:
    """The Pearson `r` (stdlib `statistics.correlation`) for one scalar
    pair over every entry-day where both fields are present and numeric,
    alongside `n` and a caveat. `r` is `None` (never a misleadingly-precise
    number) whenever it can't honestly be computed:

    - `n < 2`: not enough paired data at all.
    - `n >= 2` but zero variance in either field: `statistics.correlation`
      raises `StatisticsError`, and the caveat says so specifically rather
      than reusing the generic "not enough data" message — those are two
      different reasons a reader needs to distinguish.

    Deliberately does *not* embed the "the form doesn't label which
    direction is better" disclaimer here — that caveat is identical for
    every one of the six pairs (every self-reported scalar field has the
    same scale-direction ambiguity), so the frontend states it once, above
    the whole grid, rather than repeating it verbatim in all six footnotes.
    This function's `caveat` field is reserved for the pair-specific
    "not enough data" / "zero variance" cases, which *do* differ per pair.
    """
    paired_dates = sorted(set(values_by_field[a]) & set(values_by_field[b]))
    n = len(paired_dates)
    if n < 2:
        return ScalarCorrelation(
            pair=[a, b], n=n, r=None,
            caveat=f"Not enough paired {a}/{b} entries in this window (n={n}) to compute a correlation.",
        )
    a_vals = [values_by_field[a][d] for d in paired_dates]
    b_vals = [values_by_field[b][d] for d in paired_dates]
    try:
        r = statistics.correlation(a_vals, b_vals)
    except statistics.StatisticsError:
        return ScalarCorrelation(
            pair=[a, b], n=n, r=None,
            caveat=f"Correlation is undefined for this window (n={n}) — {a} or {b} had no variance across these entries.",
        )
    return ScalarCorrelation(pair=[a, b], n=n, r=r, caveat=f"co-movement only, over {n} paired entries.")


@router.get("/scalars", response_model=JournalScalarsResponse)
async def get_journal_scalars(
    window: str = Query(default=_DEFAULT_WINDOW, description="day, week, month, quarter, or all-time"),
) -> JournalScalarsResponse:
    """`mood`, `stress`, `sleep`, `body` as one point per entry-day, on the
    same calendar grid as the strip (`_grid_span`) so the two views line up
    visually, plus a `ScalarCorrelation` for every one of the six distinct
    pairs among those four fields (`_SCALAR_PAIRS`). Each is computed and
    returned directly rather than only implied by a rendered trend line —
    see the module docstring and docs/specs/product/journal-analytics.md
    for why a fitted line is deliberately not part of this feature at all,
    and for why the frontend draws every pair as a scatter rather than
    leading on the `r` values alone.
    """
    window = _canonical_window(window)
    today = date.today()
    entries = _collect_entries(settings.vault_path, window, today)
    start, end = _grid_span(window, today, entries)

    series: list[ScalarSeries] = []
    values_by_field: dict[str, dict[date, float]] = {field: {} for field in _SCALAR_FIELDS}
    for field_name in _SCALAR_FIELDS:
        points: list[ScalarPoint] = []
        for entry_date, fm in entries:
            value = _numeric(fm.get(field_name))
            if value is None:
                continue
            points.append(ScalarPoint(date=entry_date.isoformat(), value=value))
            values_by_field[field_name][entry_date] = value
        series.append(ScalarSeries(field=field_name, points=points))

    correlations = [_pair_correlation(a, b, values_by_field) for a, b in _SCALAR_PAIRS]

    return JournalScalarsResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=len(entries),
        series=series,
        correlations=correlations,
    )
