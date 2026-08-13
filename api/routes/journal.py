"""
Journal emotion aggregation API (#212).

The daily journal (`<vault>/Personal/Journal/YYYY-MM-DD.md`) logs a
`feeling:` frontmatter field per entry, which chains into deeper fields
named after the parent value — e.g. `feeling: Angry` -> `angry_feelings:
Restless` -> `restless_feelings: Prickly` (values here are illustrative,
not real journal content). This module walks that chain across a window of
entries and returns a nested count tree for the static emotion-wheel view
(see docs/specs/product/journal-analytics.md).

Two irregularities in the real data drove the parsing approach:
- At least two observed branches use a singular `<slug>_feeling` key
  instead of the plural `<slug>_feelings` used everywhere else — more than
  the single exception assumed during scoping. Rather than hardcode which
  values are irregular, `_next_link` tries plural then singular for every
  value.
- Chains can terminate at any depth (1, 2, or 3+) and the same value (e.g.
  "Not sure") can legitimately appear at multiple positions in the wheel —
  the tree keeps those occurrences separate because they're keyed by path,
  not by value alone.

Adversarial review (post-implementation) surfaced privacy and correctness
gaps in the first pass, all addressed here:
- Frontmatter values are free text as far as the parser is concerned — the
  fixed Google Form is today's only source, but a hand-edited entry could
  put a full sentence in `feeling:` and it would otherwise become public
  API/display text. `_display_value` applies a conservative shape policy
  (short, few words, no line breaks) instead of allowlisting today's known
  vocabulary, which would silently misclassify real data the moment the
  form's options change.
- `entry_count` used to mean "entries with a feeling", silently presented
  as if it meant "journal entries" — a window with mostly feeling-less
  entries looked like a small window with complete coverage. The response
  now reports `total_entries` and `emotion_entries` separately.
- File access is now symlink-safe (#212 review FIX 3) and treats the
  `YYYY-MM-DD.md` filename as the canonical date, skipping files whose
  frontmatter `date:` disagrees (FIX 4) or whose bytes aren't valid UTF-8
  (FIX 5), and an unrecognized `window` value is normalized before both
  computing bounds and being echoed back (FIX 6).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import frontmatter
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from config.settings import settings

router = APIRouter(prefix="/api/journal", tags=["journal"])

JOURNAL_SUBDIR = Path("Personal") / "Journal"

# Window name -> trailing day count. "all-time" is handled separately (no
# start bound).
_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90}
_VALID_WINDOWS = frozenset({"day", "week", "month", "quarter", "all-time"})
_DEFAULT_WINDOW = "all-time"

# Defensive cap on chain depth. Real data never exceeds 3 levels; this just
# guards against a malformed/cyclic frontmatter chain looping forever.
_MAX_CHAIN_DEPTH = 8

# Shape policy for a value before it becomes public display text (wedge
# label / legend / tooltip / API response). Every real observed value is a
# single word, or "Not sure" (two words) — these caps are deliberately
# generous relative to that, not a tight fit, so ordinary vocabulary growth
# doesn't trip them; they exist to catch free text, not to pin the current
# taxonomy.
_MAX_VALUE_LEN = 30
_MAX_VALUE_WORDS = 3
_UNRECOGNIZED_LABEL = "Unrecognized"

# Bounds how many distinct labels (besides the shared "Unrecognized"
# bucket) can become their own wedge in one response. The shape policy
# above catches long free text, but a corrupted file could still produce
# many distinct short "plausible-looking" values; this keeps the wheel
# bounded regardless. Real vocabulary is ~23 words, so this has generous
# headroom without being effectively unbounded.
_MAX_DISTINCT_VALUES = 50


class EmotionNode(BaseModel):
    """One value in the emotion chain, with its count over the window and
    its children (the values that followed it)."""
    value: str
    count: int
    children: list["EmotionNode"] = Field(default_factory=list)


class JournalEmotionsResponse(BaseModel):
    window: str
    start_date: str | None = None
    end_date: str
    total_entries: int
    emotion_entries: int
    wheel: list[EmotionNode]


def _canonical_window(window: str) -> str:
    """Normalize an unrecognized window value to "month" — the same value
    used for both computing bounds and the `window` field in the response,
    so the two never disagree."""
    return window if window in _VALID_WINDOWS else "month"


def _is_plausible_label(value: str) -> bool:
    """Conservative shape check: short, few words, no line breaks.

    Deliberately does not allowlist the current fixed vocabulary (~6
    primary + ~17 secondary values) — that taxonomy comes from a Google
    Form and would silently start dropping real data the moment the form's
    options change. This only rejects values that look structurally like
    free text rather than a short category label.
    """
    if not value or len(value) > _MAX_VALUE_LEN:
        return False
    if "\n" in value or "\r" in value:
        return False
    if len(value.split()) > _MAX_VALUE_WORDS:
        return False
    return True


def _display_value(value: str) -> str:
    """The value as it may be shown to the user: itself if it passes the
    shape policy, else a neutral bucket that never echoes raw text back."""
    return value if _is_plausible_label(value) else _UNRECOGNIZED_LABEL


def _next_link(fm: dict, value: str) -> str | None:
    """Look up the frontmatter value that follows `value` in the chain.

    Tries `<slug>_feelings` (the common case) then `<slug>_feeling`
    (singular — observed for `Bad` and at least one other branch in the real
    vault). Multi-word values (e.g. "Not sure") are slugged by lowercasing
    and replacing spaces with underscores. Uses the raw (pre-shape-policy)
    value, since the frontmatter key is derived from the real text
    regardless of whether it will end up displayed.
    """
    slug = value.strip().lower().replace(" ", "_")
    if not slug:
        return None
    nxt = fm.get(f"{slug}_feelings")
    if nxt is None:
        nxt = fm.get(f"{slug}_feeling")
    if isinstance(nxt, str) and nxt.strip():
        return nxt.strip()
    return None


def parse_emotion_chain(fm: dict) -> list[str]:
    """Walk the feeling -> ..._feelings chain out of one entry's frontmatter.

    Returns e.g. ["Bad", "Muted", "Not sure"], or ["Happy"] if the chain
    terminates immediately, or [] if there's no `feeling` key at all.

    Traversal (which frontmatter keys to look up next) always uses the raw
    value; only the values placed in the returned chain go through the
    shape policy (`_display_value`) — a free-text value can't sensibly
    continue the chain anyway, but even if it did, it must never come back
    out as display text.
    """
    level1 = fm.get("feeling")
    if not isinstance(level1, str) or not level1.strip():
        return []
    raw_chain = [level1.strip()]
    seen = {raw_chain[0].lower()}
    while len(raw_chain) < _MAX_CHAIN_DEPTH:
        nxt = _next_link(fm, raw_chain[-1])
        if nxt is None:
            break
        if nxt.lower() in seen:
            break  # defensive: don't loop on cyclic data
        seen.add(nxt.lower())
        raw_chain.append(nxt)
    return [_display_value(v) for v in raw_chain]


def window_bounds(window: str, today: date) -> tuple[date | None, date]:
    """Resolve a window name to a [start, end] date range (inclusive).
    None start means all-time (no lower bound). Unrecognized names are
    treated as "month" (defense in depth — callers should already have
    normalized via `_canonical_window`)."""
    if window == "all-time":
        return None, today
    days = _WINDOW_DAYS.get(window, _WINDOW_DAYS["month"])
    return today - timedelta(days=days - 1), today


# No result caching: every request below re-reads and re-parses every
# *.md file directly in the journal directory. Fine at the real-world
# scale here (dozens of entries as of #212); revisit with a cache or an
# index if entry volume ever reaches the thousands.
def _iter_valid_journal_files(vault_path: Path, window: str, today: date | None = None):
    """Yield (entry_date, frontmatter_dict) for every trustworthy dated
    journal file in the window.

    "Trustworthy" means all of:
    - Named `YYYY-MM-DD.md` — the filename is the canonical date for files
      in this directory (not frontmatter), so a same-named `Index.md` or
      similar is never admitted just because it happens to carry `date:`
      and `feeling:` fields.
    - A regular file, not a symlink, and its resolved real path stays
      directly inside the real journal directory — a symlink placed in the
      journal folder pointing at, say, a therapy note elsewhere in the
      vault must not be read and aggregated just because it landed in this
      directory.
    - Readable as UTF-8 text and parseable as frontmatter (silently skips
      anything that isn't, rather than 500ing the whole request over one
      bad file).
    - If frontmatter has a `date:` field, it agrees with the filename date;
      a disagreement means the entry isn't safely attributable to either
      date, so it's skipped rather than picking one silently.
    """
    if today is None:
        today = date.today()
    start, end = window_bounds(window, today)
    journal_dir = Path(vault_path) / JOURNAL_SUBDIR
    if not journal_dir.is_dir():
        return
    real_journal_dir = journal_dir.resolve()

    for path in sorted(journal_dir.glob("*.md")):
        try:
            filename_date = date.fromisoformat(path.stem)
        except ValueError:
            continue  # doesn't match the YYYY-MM-DD.md naming contract

        if path.is_symlink():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != real_journal_dir or not resolved.is_file():
            continue

        if filename_date > end:
            continue
        if start is not None and filename_date < start:
            continue

        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            post = frontmatter.loads(content)
        except Exception:
            continue
        fm = dict(post.metadata)

        raw_fm_date = fm.get("date")
        if raw_fm_date is not None:
            try:
                fm_date = date.fromisoformat(str(raw_fm_date)[:10])
            except ValueError:
                fm_date = None
            if fm_date is not None and fm_date != filename_date:
                continue  # frontmatter disagrees with the filename; skip

        yield filename_date, fm


def iter_journal_chains(vault_path: Path, window: str, today: date | None = None):
    """Yield the parsed emotion chain for every journal entry in the window
    that has a parseable `feeling:` value. Entries without one don't
    appear here — see `collect_window` for the total-vs-emotion-bearing
    distinction the API response surfaces."""
    for _, fm in _iter_valid_journal_files(vault_path, window, today):
        chain = parse_emotion_chain(fm)
        if chain:
            yield chain


def collect_window(
    vault_path: Path, window: str, today: date | None = None
) -> tuple[list[list[str]], int, int]:
    """One pass over the journal directory for a window: parsed emotion
    chains, the total number of valid dated entries, and how many of those
    had a parseable `feeling:` chain.

    These two counts can differ — not every entry logs a feeling — so
    callers must not treat one as a proxy for the other (a window of
    mostly feeling-less entries must not look like a small window with
    complete coverage).
    """
    chains: list[list[str]] = []
    total = 0
    with_emotion = 0
    for _, fm in _iter_valid_journal_files(vault_path, window, today):
        total += 1
        chain = parse_emotion_chain(fm)
        if chain:
            with_emotion += 1
            chains.append(chain)
    return chains, total, with_emotion


def build_wheel(chains: list[list[str]], max_distinct_values: int = _MAX_DISTINCT_VALUES) -> list[EmotionNode]:
    """Fold parsed chains into a nested value/count/children tree.

    Each node is keyed by its position in the tree, not just its value, so
    the same string (e.g. "Not sure") appearing as a root value and as a
    leaf under a different branch produces two distinct nodes rather than
    being merged.

    `max_distinct_values` bounds how many distinct labels (besides the
    shared "Unrecognized" bucket) are admitted as their own wedge across
    the whole response — see the module-level constant for why.
    """
    root: dict[str, dict] = {}
    admitted: set[str] = set()

    def capped(value: str) -> str:
        if value == _UNRECOGNIZED_LABEL or value in admitted:
            return value
        if len(admitted) >= max_distinct_values:
            return _UNRECOGNIZED_LABEL
        admitted.add(value)
        return value

    def insert(node: dict[str, dict], chain: list[str]) -> None:
        if not chain:
            return
        head, rest = capped(chain[0]), chain[1:]
        entry = node.setdefault(head, {"count": 0, "children": {}})
        entry["count"] += 1
        insert(entry["children"], rest)

    for chain in chains:
        insert(root, chain)

    def to_nodes(node: dict[str, dict]) -> list[EmotionNode]:
        items = [
            EmotionNode(value=val, count=data["count"], children=to_nodes(data["children"]))
            for val, data in node.items()
        ]
        # Largest slice first; alphabetical tiebreak for determinism.
        items.sort(key=lambda n: (-n.count, n.value))
        return items

    return to_nodes(root)


@router.get("/emotions", response_model=JournalEmotionsResponse)
async def get_journal_emotions(
    window: str = Query(
        default=_DEFAULT_WINDOW,
        description="day, week, month, quarter, or all-time",
    ),
) -> JournalEmotionsResponse:
    """Aggregate the emotion-wheel chain from daily journal entries for a window."""
    window = _canonical_window(window)
    today = date.today()
    start, end = window_bounds(window, today)

    chains, total_entries, emotion_entries = collect_window(settings.vault_path, window, today)
    wheel = build_wheel(chains)

    return JournalEmotionsResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        total_entries=total_entries,
        emotion_entries=emotion_entries,
        wheel=wheel,
    )
