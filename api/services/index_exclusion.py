"""Vault paths kept out of the index and out of search, decided from the path alone.

The globs come from ``LIFEOS_VAULT_INDEX_EXCLUDE_GLOBS``. They are vault-relative,
case-insensitive, and use ``*`` (within a folder), ``**`` (across folders) and ``?``.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional

from api.services.vault_listing import vault_relative_path
from config.settings import settings


def _glob_to_regex(glob: str) -> re.Pattern:
    glob = glob.strip().replace("\\", "/")
    while glob.startswith(("./", "/")):
        glob = glob[2:] if glob.startswith("./") else glob[1:]
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


@lru_cache(maxsize=16)
def _compile(globs: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(_glob_to_regex(g) for g in globs)


def matches_exclude_globs(rel_path: str, globs: Iterable[str]) -> bool:
    """Whether a vault-relative POSIX path (or chunk id tail) matches any glob."""
    patterns = _compile(tuple(globs))
    rel = str(rel_path).replace("\\", "/")
    return any(p.fullmatch(rel) for p in patterns)


def index_exclude_matcher() -> Optional[Callable[[str], bool]]:
    """
    A test for whether a note path or chunk id falls under an excluded glob.

    Returns:
        The test, or None when ``LIFEOS_VAULT_INDEX_EXCLUDE_GLOBS`` is empty
    """
    globs = tuple(settings.vault_index_exclude_globs)
    if not globs:
        return None
    root = Path(settings.vault_path).resolve()

    def excluded(path: str) -> bool:
        rel = vault_relative_path(path, root)
        if rel is None:
            rel = vault_relative_path(path, Path(settings.vault_path))
        if rel is None:
            return False
        # Chunk ids (``{path}_{n}``, ``{path}::{n}``, ``{path}::summary``) match as their file.
        base = re.sub(r"(?:::(?:\d+|summary)|_\d+)$", "", rel)
        return matches_exclude_globs(rel, globs) or matches_exclude_globs(base, globs)

    return excluded
