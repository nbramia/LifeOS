"""Vault structure listing: folder/glob browsing with path safety.

Shared by the `list_vault` orchestrator tool, `GET /api/vault/list`, and the
generated index pages. Only reads file metadata and frontmatter; never the
vector store.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from api.services.chunker import extract_frontmatter, normalize_tags

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_GLOB = "**/*.md"


class VaultListError(ValueError):
    """The requested path or glob cannot be listed."""


# Vault-relative folder that each vault note type names. ``Other`` is every
# note outside all of them.
NOTE_TYPE_FOLDERS = {
    "ML": "Work/ML",
    "Granola": "Granola",
    "Personal": "Personal",
    "Work": "Work",
    "LifeOS": "LifeOS",
}


def infer_note_type(rel_path: Path | str) -> str:
    """Infer a note type from a vault-relative path (never an absolute one)."""
    parts = [p.casefold() for p in Path(str(rel_path).replace("\\", "/")).parts]
    if parts[:2] == ["work", "ml"]:
        return "ML"
    for name in ("Granola", "Personal", "Work", "LifeOS"):
        if parts and parts[0] == name.casefold():
            return name
    return "Other"


def resolve_vault_dir(vault_root: Path, rel_path: str) -> tuple[Path, Path]:
    """Resolve `rel_path` under the vault root, returning (resolved_root, resolved_dir).

    Both sides are resolved so a symlinked vault root still contains its own
    children. Raises VaultListError on escapes, absolute paths, or a missing folder.
    """
    rel = (rel_path or "").strip()
    if rel.startswith(("/", "~")) or Path(rel).is_absolute():
        raise VaultListError("path must be vault-relative")
    try:
        root = vault_root.resolve()
        target = (root / rel).resolve()
        target.relative_to(root)
        is_dir = target.is_dir()
    except ValueError:
        raise VaultListError("path resolves outside the vault")
    except OSError:
        raise VaultListError("cannot read folder")
    if not is_dir:
        raise VaultListError(f"folder '{rel or '.'}' not found in vault")
    return root, target


def is_hidden(rel_parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") for part in rel_parts)


def scan_notes(root: Path, folder: Path, glob: str = DEFAULT_GLOB) -> list[dict]:
    """Return `{path, relative_path, name, mtime}` for matching files, newest first.

    Hidden path components are excluded and files whose real location is
    outside the vault (symlink escapes) are dropped. Ties on mtime break on
    relative path so the order is stable.
    """
    found = []
    try:
        matches = list(folder.glob(glob))
    except (ValueError, NotImplementedError) as exc:
        raise VaultListError(f"invalid glob: {exc}")
    except OSError:
        raise VaultListError("cannot read folder")
    for p in matches:
        try:
            if not p.is_file():
                continue
            p.resolve().relative_to(root)
            rel = p.relative_to(root)
            if is_hidden(rel.parts):
                continue
            mtime = p.stat().st_mtime
        except (OSError, ValueError):
            continue
        found.append({"path": p, "relative_path": rel.as_posix(), "name": p.name, "mtime": mtime})
    found.sort(key=lambda e: (-e["mtime"], e["relative_path"]))
    return found


def mtime_date(mtime: float) -> date:
    return datetime.fromtimestamp(mtime).date()


def _tags_for(path: Path) -> list[str]:
    try:
        meta, _ = extract_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    return normalize_tags(meta.get("tags"))


def list_vault_entries(
    vault_root: Path,
    path: str = "",
    glob: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """List notes under `path` (recursively, or matching `glob`), newest first.

    Returns `{path, total, offset, limit, folders, entries}` where each entry is
    `{name, relative_path, modified_date, note_type, tags}`. Raises VaultListError.
    """
    root, folder = resolve_vault_dir(vault_root, path)
    if not 1 <= limit <= MAX_LIMIT:
        raise VaultListError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise VaultListError("offset must be 0 or greater")
    notes = scan_notes(root, folder, glob or DEFAULT_GLOB)
    page = notes[offset:offset + limit]
    entries = [
        {
            "name": n["name"],
            "relative_path": n["relative_path"],
            "modified_date": mtime_date(n["mtime"]).isoformat(),
            "note_type": infer_note_type(n["relative_path"]),
            "tags": _tags_for(n["path"]),
        }
        for n in page
    ]
    try:
        folders = sorted(
            d.name for d in folder.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        raise VaultListError("cannot read folder")
    return {
        "path": folder.relative_to(root).as_posix() if folder != root else "",
        "total": len(notes),
        "offset": offset,
        "limit": limit,
        "folders": folders,
        "entries": entries,
    }
