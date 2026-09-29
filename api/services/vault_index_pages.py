"""Generated per-folder index pages.

For every top-level vault folder, `LifeOS/Index/<folder>.md` lists the note
count, how many notes changed in the last 30 days, and the most recent notes
with their one-line summaries. Pages are deterministic (stable ordering,
dates only) so sync tools don't see churn, and are only rewritten when the
rendered bytes differ.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from api.services.vault_listing import is_hidden, mtime_date, scan_notes

logger = logging.getLogger(__name__)

INDEX_FOLDER = "LifeOS/Index"
RECENT_DAYS = 30
MAX_RECENT = 30
MAX_SUMMARY_CHARS = 200

SummaryLookup = Callable[[list[str]], dict[str, str]]


def _one_line(text: str) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    if len(line) > MAX_SUMMARY_CHARS:
        line = line[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return line


def render_index_page(
    folder_name: str,
    entries: list[dict],
    summaries: dict[str, str],
    *,
    today: date,
) -> str:
    """Render one index page.

    `entries` are every note in the folder as `{relative_path, name,
    modified_date}` (ISO date string); `summaries` maps relative_path to a
    one-line summary. Ordering is modified date descending, then path.
    """
    ordered = sorted(entries, key=lambda e: (-date.fromisoformat(e["modified_date"]).toordinal(), e["relative_path"]))
    cutoff = today - timedelta(days=RECENT_DAYS)
    recent_count = sum(1 for e in ordered if date.fromisoformat(e["modified_date"]) >= cutoff)
    lines = [
        "---",
        "generated: true",
        "source: lifeos-index",
        "---",
        f"# {folder_name} index",
        "",
        f"Generated index of the `{folder_name}` folder, rebuilt during the nightly reindex. Do not edit.",
        "",
        f"- Notes: {len(ordered)}",
        f"- Changed in the last {RECENT_DAYS} days: {recent_count}",
        "",
        "## Most recent notes",
        "",
    ]
    for e in ordered[:MAX_RECENT]:
        link = e["relative_path"][:-3] if e["relative_path"].endswith(".md") else e["relative_path"]
        title = e["name"][:-3] if e["name"].endswith(".md") else e["name"]
        line = f"- [[{link}|{title}]] ({e['modified_date']})"
        summary = summaries.get(e["relative_path"])
        if summary:
            line += f" — {_one_line(summary)}"
        lines.append(line)
    if not ordered:
        lines.append("_No notes yet._")
    return "\n".join(lines) + "\n"


def _bm25_summary_lookup(paths: list[str]) -> dict[str, str]:
    from api.services.bm25_index import get_bm25_index

    return get_bm25_index().get_summaries(paths)


def write_index_pages(
    vault_root: Path,
    today: date | None = None,
    summary_lookup: SummaryLookup | None = None,
) -> dict:
    """Write `LifeOS/Index/<top-level-folder>.md` for each top-level folder.

    Notes inside the index folder are not counted or listed. Summaries come
    from the keyword index; if it is unavailable pages fall back to titles
    only. Returns `{written, unchanged, folders}`.
    """
    root = vault_root.resolve()
    today = today or date.today()
    lookup = summary_lookup or _bm25_summary_lookup
    index_dir = root / INDEX_FOLDER
    index_dir.mkdir(parents=True, exist_ok=True)
    index_prefix = INDEX_FOLDER + "/"

    written = unchanged = 0
    folders = sorted(
        d for d in root.iterdir()
        if d.is_dir() and not is_hidden((d.name,))
    )
    for folder in folders:
        try:
            resolved = folder.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        notes = [
            n for n in scan_notes(root, folder)
            if not n["relative_path"].startswith(index_prefix)
        ]
        entries = [
            {
                "relative_path": n["relative_path"],
                "name": n["name"],
                "modified_date": mtime_date(n["mtime"]).isoformat(),
            }
            for n in notes
        ]
        top = notes[:MAX_RECENT]
        real_paths = {str(n["path"].resolve()): n["relative_path"] for n in top}
        try:
            found = lookup(list(real_paths))
        except Exception as e:
            logger.warning("Index summaries unavailable for %s, using titles only: %s", folder.name, e)
            found = {}
        summaries = {real_paths[p]: s for p, s in found.items() if p in real_paths}

        content = render_index_page(folder.name, entries, summaries, today=today)
        target = index_dir / f"{folder.name}.md"
        data = content.encode("utf-8")
        if target.exists() and target.read_bytes() == data:
            unchanged += 1
            continue
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        written += 1
    return {"written": written, "unchanged": unchanged, "folders": len(folders)}
