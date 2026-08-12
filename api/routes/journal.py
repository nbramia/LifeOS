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
# start bound). Unknown window values fall back to "month", matching the
# no-400-on-bad-input convention used by the CRM period params.
_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90}
_DEFAULT_WINDOW = "all-time"

# Defensive cap on chain depth. Real data never exceeds 3 levels; this just
# guards against a malformed/cyclic frontmatter chain looping forever.
_MAX_CHAIN_DEPTH = 8


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
    entry_count: int
    wheel: list[EmotionNode]


def _next_link(fm: dict, value: str) -> str | None:
    """Look up the frontmatter value that follows `value` in the chain.

    Tries `<slug>_feelings` (the common case) then `<slug>_feeling`
    (singular — observed for `Bad` and at least one other branch in the real
    vault). Multi-word values (e.g. "Not sure") are slugged by lowercasing
    and replacing spaces with underscores.
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
    """
    level1 = fm.get("feeling")
    if not isinstance(level1, str) or not level1.strip():
        return []
    chain = [level1.strip()]
    seen = {chain[0].lower()}
    while len(chain) < _MAX_CHAIN_DEPTH:
        nxt = _next_link(fm, chain[-1])
        if nxt is None:
            break
        if nxt.lower() in seen:
            break  # defensive: don't loop on cyclic data
        seen.add(nxt.lower())
        chain.append(nxt)
    return chain


def _entry_date(fm: dict, path: Path) -> date | None:
    """Prefer the frontmatter `date:` field; fall back to the filename stem
    (journal files are named `YYYY-MM-DD.md`)."""
    raw = fm.get("date")
    if raw is not None:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def window_bounds(window: str, today: date) -> tuple[date | None, date]:
    """Resolve a window name to a [start, end] date range (inclusive).
    None start means all-time (no lower bound)."""
    if window == "all-time":
        return None, today
    days = _WINDOW_DAYS.get(window, _WINDOW_DAYS["month"])
    return today - timedelta(days=days - 1), today


def iter_journal_chains(vault_path: Path, window: str, today: date | None = None):
    """Yield the parsed emotion chain for every journal entry in the window.

    Entries with no `feeling` value, unparseable dates, or unreadable/
    malformed files are silently skipped (matches `extract_frontmatter`'s
    never-raise convention elsewhere in the codebase).
    """
    if today is None:
        today = date.today()
    start, end = window_bounds(window, today)
    journal_dir = Path(vault_path) / JOURNAL_SUBDIR
    if not journal_dir.is_dir():
        return
    for path in sorted(journal_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            post = frontmatter.loads(content)
        except Exception:
            continue
        fm = dict(post.metadata)
        entry_date = _entry_date(fm, path)
        if entry_date is None or entry_date > end:
            continue
        if start is not None and entry_date < start:
            continue
        chain = parse_emotion_chain(fm)
        if chain:
            yield chain


def build_wheel(chains: list[list[str]]) -> list[EmotionNode]:
    """Fold parsed chains into a nested value/count/children tree.

    Each node is keyed by its position in the tree, not just its value, so
    the same string (e.g. "Not sure") appearing as a root value and as a
    leaf under a different branch produces two distinct nodes rather than
    being merged.
    """
    root: dict[str, dict] = {}

    def insert(node: dict[str, dict], chain: list[str]) -> None:
        if not chain:
            return
        head, rest = chain[0], chain[1:]
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
    today = date.today()
    start, end = window_bounds(window, today)

    chains = list(iter_journal_chains(settings.vault_path, window, today))
    wheel = build_wheel(chains)

    return JournalEmotionsResponse(
        window=window,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat(),
        entry_count=len(chains),
        wheel=wheel,
    )
