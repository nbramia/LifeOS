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

from api.services.chunker import extract_frontmatter
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
    modified_date}` (ISO date string), already ordered newest first; the
    first 30 are listed. `summaries` maps relative_path to a one-line summary.
    """
    ordered = list(entries)
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
    only. Pages whose folder no longer exists are removed when they carry the
    generated-page frontmatter. A file whose name collides with a page but which
    lacks that frontmatter is left untouched and counted in `skipped_collision`. If the index folder resolves outside the vault
    nothing is written. Returns `{written, unchanged, skipped_collision,
    folders, changed, removed}`, where `changed` and `removed` are absolute file paths.
    """
    root = vault_root.resolve()
    today = today or date.today()
    lookup = summary_lookup or _bm25_summary_lookup
    index_dir = (root / INDEX_FOLDER).resolve()
    try:
        index_dir.relative_to(root)
    except ValueError:
        logger.warning("Index folder resolves outside the vault; skipping index pages")
        return {"written": 0, "unchanged": 0, "skipped_collision": 0, "folders": 0, "changed": [], "removed": []}
    index_dir.mkdir(parents=True, exist_ok=True)
    index_prefix = INDEX_FOLDER + "/"

    written = unchanged = skipped_collision = 0
    changed: list[str] = []
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
        if target.exists():
            existing = target.read_bytes()
            if existing == data:
                unchanged += 1
                continue
            meta, _ = extract_frontmatter(existing.decode("utf-8", errors="replace"))
            if meta.get("source") != "lifeos-index":
                logger.warning("Index page %s collides with a non-generated file; skipping", target.name)
                skipped_collision += 1
                continue
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        written += 1
        changed.append(str(target))
    removed = _remove_stale_pages(index_dir, {f.name for f in folders})
    return {
        "written": written,
        "unchanged": unchanged,
        "skipped_collision": skipped_collision,
        "folders": len(folders),
        "changed": changed,
        "removed": removed,
    }


def _remove_stale_pages(index_dir: Path, folder_names: set[str]) -> list[str]:
    """Delete generated pages whose folder is gone; never touches other files."""
    removed = []
    for page in sorted(index_dir.glob("*.md")):
        if page.stem in folder_names or page.is_symlink() or not page.is_file():
            continue
        try:
            meta, _ = extract_frontmatter(page.read_text(encoding="utf-8"))
            if meta.get("source") != "lifeos-index":
                continue
            page.unlink()
        except OSError:
            continue
        removed.append(str(page))
    return removed
