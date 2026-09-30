"""Generated Vault Map pages.

For every top-level vault folder, `Wiki/Vault Map/<folder>.md` lists the note
count, how many notes changed in the last 30 days, and the most recent notes
with their one-line summaries. When the tag store holds tags, `Domains/<domain>.md`
lists a domain's topics with counts and `Topics/<parent>--<child>.md` lists a
populated topic's most recent notes (membership matches the topic search facet).
`Wiki/Vault Map/index.md` links the domain pages, then the folder pages. Pages use the wiki's frontmatter keys plus the `source: lifeos-index`
ownership marker, are deterministic (stable ordering, dates only), and are only
rewritten when the rendered bytes differ.
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

INDEX_FOLDER = "Wiki/Vault Map"
LEGACY_INDEX_FOLDER = "LifeOS/Index"
OWNER = "lifeos-index"
WIKI_INDEX_LINK = "Part of [[Wiki/index|Wiki Index]] → Vault map."
RECENT_DAYS = 30
MAX_RECENT = 30
MAX_SUMMARY_CHARS = 200

SummaryLookup = Callable[[list[str]], dict[str, str]]


def _one_line(text: str) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    if len(line) > MAX_SUMMARY_CHARS:
        line = line[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return line


def _frontmatter(latest: str | None) -> list[str]:
    """Frontmatter whose `date` is the newest note date on the page (none when empty)."""
    lines = ["---", "type: index", f"source: {OWNER}"]
    if latest:
        lines.append(f"date: {latest}")
    return lines + ["generated: true", "---"]


def _label(name: str) -> str:
    return name.replace("_", " ")


def topic_page_name(topic: str) -> str:
    """Page name for a `parent/child` topic: `parent--child`."""
    return topic.replace("/", "--")


def render_map_index(
    rows: list[tuple[str, int, int, str | None]],
    domains: list[tuple[str, int, str | None]] | None = None,
) -> str:
    """Render `Wiki/Vault Map/index.md`; `rows` are `(folder, notes, recent, latest_date)`.

    `domains` are `(domain, notes, latest_date)`; None means no tags are stored.
    """
    latest = max(
        [r[3] for r in rows if r[3]] + [d[2] for d in domains or [] if d[2]],
        default=None,
    )
    lines = _frontmatter(latest) + [
        "# Vault map",
        "",
        WIKI_INDEX_LINK,
        "",
        "Generated list of the domain and per-folder index pages, rebuilt during the nightly reindex. Do not edit.",
        "",
        "## By domain",
        "",
    ]
    if domains is None:
        lines.append("_No tags stored yet; domain and topic pages appear once notes are tagged._")
    else:
        for name, total, _ in domains:
            lines.append(f"- [[{INDEX_FOLDER}/Domains/{name}|{_label(name)}]] — {total} notes")
    lines += ["", "## By folder", ""]
    for name, total, recent, _ in sorted(rows):
        lines.append(f"- [[{INDEX_FOLDER}/{name}|{name}]] — {total} notes, {recent} changed in {RECENT_DAYS} days")
    if not rows:
        lines.append("_No folders yet._")
    return "\n".join(lines) + "\n"


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
    lines = _frontmatter(max((e["modified_date"] for e in ordered), default=None)) + [
        f"# {folder_name} index",
        "",
        WIKI_INDEX_LINK,
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


def _member_lines(notes: list[dict], summaries: dict[str, str]) -> list[str]:
    lines = []
    for n in notes:
        link = n["relative_path"][:-3] if n["relative_path"].endswith(".md") else n["relative_path"]
        title = n["name"][:-3] if n["name"].endswith(".md") else n["name"]
        line = f"- [[{link}|{title}]] ({n['modified_date']})"
        if summaries.get(n["relative_path"]):
            line += f" — {_one_line(summaries[n['relative_path']])}"
        lines.append(line)
    return lines


def render_topic_page(topic: str, total: int, notes: list[dict], summaries: dict[str, str]) -> str:
    """Render a topic page; `notes` are the (already capped) newest members."""
    parent, _, child = topic.partition("/")
    lines = _frontmatter(max((n["modified_date"] for n in notes), default=None)) + [
        f"# {_label(parent)} / {_label(child)}",
        "",
        WIKI_INDEX_LINK,
        "",
        f"Generated list of notes on `{topic}`, rebuilt during the nightly reindex. Do not edit.",
        "",
        f"Total: {total}",
        "",
        "## Most recent notes",
        "",
    ]
    return "\n".join(lines + _member_lines(notes, summaries)) + "\n"


def render_domain_page(
    domain: str, topics: list[tuple[str, int]], domain_only: int, latest: str | None
) -> str:
    """Render a domain page; `topics` are `(parent/child, count)`."""
    lines = _frontmatter(latest) + [
        f"# {_label(domain)}",
        "",
        WIKI_INDEX_LINK,
        "",
        f"Generated list of the `{domain}` topics, rebuilt during the nightly reindex. Do not edit.",
        "",
        "## Topics",
        "",
    ]
    for topic, count in topics:
        label = _label(topic.partition("/")[2])
        lines.append(f"- [[{INDEX_FOLDER}/Topics/{topic_page_name(topic)}|{label}]] — {count} notes")
    if not topics:
        lines.append("_No populated topics._")
    lines += ["", f"Notes classified only at the domain level: {domain_only}"]
    return "\n".join(lines) + "\n"


def _tag_membership(notes_by_path: dict[str, dict], tag_store, taxonomy):
    """Domain and topic membership from the tag store, or None when no tags exist.

    Returns `{domain: (domain members, {topic: members})}`; members are existing,
    non-restricted notes, newest first. Topic membership is
    `VaultTagStore.paths_matching(topic=...)`, the matching the search facet uses.
    """
    try:
        if tag_store is None:
            from api.services.vault_tag_store import VaultTagStore, get_vault_tags_db_path

            if not os.path.exists(get_vault_tags_db_path()):
                return None
            tag_store = VaultTagStore()
        if not tag_store.paths_matching():
            return None
        if taxonomy is None:
            from api.services.vault_taxonomy import load_taxonomy

            taxonomy = load_taxonomy()
        restricted = set(tag_store.paths_matching(sensitivity="restricted"))
        order = {p: i for i, p in enumerate(notes_by_path)}

        def members(**facet) -> list[str]:
            found = {p for p in tag_store.paths_matching(**facet) if p in order and p not in restricted}
            return sorted(found, key=order.__getitem__)

        out: dict[str, tuple[list[str], dict[str, list[str]]]] = {}
        for domain in taxonomy.domains:
            topics = {}
            for topic in sorted(t for t in taxonomy.topics if t.startswith(domain + "/")):
                found = members(topic=[topic])
                if found:
                    topics[topic] = found
            out[domain] = (members(domain=[domain]), topics)
        return out
    except Exception as e:
        logger.warning("Tag store unavailable for domain/topic pages: %s", e)
        return None


def _bm25_summary_lookup(paths: list[str]) -> dict[str, str]:
    from api.services.bm25_index import get_bm25_index

    return get_bm25_index().get_summaries(paths)


def write_index_pages(
    vault_root: Path,
    today: date | None = None,
    summary_lookup: SummaryLookup | None = None,
    tag_store=None,
    taxonomy=None,
) -> dict:
    """Write `Wiki/Vault Map/<top-level-folder>.md` for each top-level folder,
    plus `Wiki/Vault Map/index.md` listing them.

    Notes inside the index folder are not counted or listed. Summaries come
    from the keyword index; if it is unavailable pages fall back to titles
    only. Pages for folders absent from the vault are removed when they carry the
    generated-page frontmatter. A file whose name collides with a page but which
    lacks that frontmatter is left untouched and counted in `skipped_collision`. If the index folder resolves outside the vault
    nothing is written. Returns `{written, unchanged, skipped_collision,
    folders, changed, removed, removed_legacy}`, where `changed` and `removed` are absolute file paths.
    Generated pages left in the legacy `LifeOS/Index` folder are deleted (and
    listed in `removed`); other files there are never touched.

    Domain and topic pages come from the tag store (`tag_store`, default the
    on-disk store; `taxonomy`, default the loaded one). With no stored tags the
    map index says so and no domain or topic pages are written; owned ones left
    over are removed. Restricted notes appear on folder pages only.
    """
    root = vault_root.resolve()
    today = today or date.today()
    lookup = summary_lookup or _bm25_summary_lookup
    index_dir = (root / INDEX_FOLDER).resolve()
    try:
        index_dir.relative_to(root)
    except ValueError:
        logger.warning("Index folder resolves outside the vault; skipping index pages")
        return {"written": 0, "unchanged": 0, "skipped_collision": 0, "folders": 0, "changed": [], "removed": [], "removed_legacy": 0, "domain_pages": 0, "topic_pages": 0}
    index_dir.mkdir(parents=True, exist_ok=True)
    index_prefix = INDEX_FOLDER + "/"

    written = unchanged = skipped_collision = 0
    changed: list[str] = []
    rows: list[tuple[str, int, int, str | None]] = []
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
        cutoff = today - timedelta(days=RECENT_DAYS)
        rows.append((
            folder.name,
            len(entries),
            sum(1 for e in entries if date.fromisoformat(e["modified_date"]) >= cutoff),
            max((e["modified_date"] for e in entries), default=None),
        ))
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
        status = _write_owned(target, content.encode("utf-8"))
        if status == "unchanged":
            unchanged += 1
        elif status == "collision":
            skipped_collision += 1
        else:
            written += 1
            changed.append(str(target))
    all_notes = [n for n in scan_notes(root, root) if not n["relative_path"].startswith(index_prefix)]
    notes_by_path = {
        n["relative_path"]: {
            "relative_path": n["relative_path"],
            "name": n["name"],
            "modified_date": mtime_date(n["mtime"]).isoformat(),
            "path": n["path"],
        }
        for n in all_notes
    }
    order = {p: i for i, p in enumerate(notes_by_path)}
    membership = _tag_membership(notes_by_path, tag_store, taxonomy)
    pages: dict[str, dict[str, str]] = {"Domains": {}, "Topics": {}}
    domain_rows: list[tuple[str, int, str | None]] | None = None
    if membership is not None:
        domain_rows = []
        for domain, (dom_paths, topics) in membership.items():
            if not dom_paths and not topics:
                continue
            in_topics = {p for paths in topics.values() for p in paths}
            union = set(dom_paths) | in_topics
            latest = max((notes_by_path[p]["modified_date"] for p in union), default=None)
            domain_rows.append((domain, len(dom_paths), latest))
            pages["Domains"][domain] = render_domain_page(
                domain,
                [(t, len(ps)) for t, ps in topics.items()],
                len([p for p in dom_paths if p not in in_topics]),
                latest,
            )
            for topic, paths in topics.items():
                top = [notes_by_path[p] for p in sorted(paths, key=order.__getitem__)[:MAX_RECENT]]
                real = {str(n["path"].resolve()): n["relative_path"] for n in top}
                try:
                    found = lookup(list(real))
                except Exception as e:
                    logger.warning("Index summaries unavailable for %s, using titles only: %s", topic, e)
                    found = {}
                summaries = {real[p]: t for p, t in found.items() if p in real}
                pages["Topics"][topic_page_name(topic)] = render_topic_page(topic, len(paths), top, summaries)

    def emit(target: Path, content: str) -> None:
        nonlocal written, unchanged, skipped_collision
        status = _write_owned(target, content.encode("utf-8"))
        if status == "unchanged":
            unchanged += 1
        elif status == "collision":
            skipped_collision += 1
        else:
            written += 1
            changed.append(str(target))

    emit(index_dir / "index.md", render_map_index(rows, domain_rows))
    removed: list[str] = []
    for sub, sub_pages in pages.items():
        sub_dir = index_dir / sub
        if sub_dir.is_symlink() or (sub_dir.exists() and not sub_dir.is_dir()):
            logger.warning("Vault Map %s folder is not a plain directory; skipping", sub)
            continue
        if sub_pages:
            sub_dir.mkdir(exist_ok=True)
            for name, content in sub_pages.items():
                emit(sub_dir / f"{name}.md", content)
        if sub_dir.is_dir():
            removed += _remove_stale_pages(sub_dir, set(sub_pages))
            try:
                sub_dir.rmdir()
            except OSError:
                pass
    removed += _remove_stale_pages(index_dir, {f.name for f in folders} | {"index"})
    legacy = _remove_legacy_pages(root)
    removed += legacy
    return {
        "written": written,
        "unchanged": unchanged,
        "skipped_collision": skipped_collision,
        "folders": len(folders),
        "changed": changed,
        "removed": removed,
        "removed_legacy": len(legacy),
        "domain_pages": len(pages["Domains"]),
        "topic_pages": len(pages["Topics"]),
    }


def _write_owned(target: Path, data: bytes) -> str:
    """Write `data` unless identical or the file is not ours; returns the outcome."""
    if target.exists():
        existing = target.read_bytes()
        if existing == data:
            return "unchanged"
        meta, _ = extract_frontmatter(existing.decode("utf-8", errors="replace"))
        if meta.get("source") != OWNER:
            logger.warning("Index page %s collides with a non-generated file; skipping", target.name)
            return "collision"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return "written"


def _remove_legacy_pages(root: Path) -> list[str]:
    """Delete generated pages from the legacy index folder, then the folder if empty."""
    legacy_dir = root / LEGACY_INDEX_FOLDER
    if legacy_dir.is_symlink() or not legacy_dir.is_dir():
        return []
    removed = []
    for page in sorted(legacy_dir.glob("*.md")):
        if page.is_symlink() or not page.is_file():
            continue
        try:
            meta, _ = extract_frontmatter(page.read_text(encoding="utf-8"))
            if meta.get("source") != OWNER:
                continue
            page.unlink()
        except OSError:
            continue
        removed.append(str(page))
    try:
        legacy_dir.rmdir()
    except OSError:
        pass
    return removed


def _remove_stale_pages(index_dir: Path, folder_names: set[str]) -> list[str]:
    """Delete generated pages whose folder is gone; never touches other files."""
    removed = []
    for page in sorted(index_dir.glob("*.md")):
        if page.stem in folder_names or page.is_symlink() or not page.is_file():
            continue
        try:
            meta, _ = extract_frontmatter(page.read_text(encoding="utf-8"))
            if meta.get("source") != OWNER:
                continue
            page.unlink()
        except OSError:
            continue
        removed.append(str(page))
    return removed
