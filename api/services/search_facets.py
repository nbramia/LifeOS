"""Structured facets for vault search.

``SearchFacets`` narrows (or boosts) a hybrid search. ``resolve_allowed_paths``
turns the facets into the set of absolute file paths that satisfy all of them,
which both search arms use as a pre-filter before ranking.

Each facet is resolved by the cheapest store that holds it:

- ``doc_type`` / ``domain`` / ``topic`` / ``project``: the vault tag store
  (keyed by vault-relative path; mapped to the absolute paths the indexes use).
- ``note_type``: an alias for vault folders (``Work``, ``Personal``, ``LifeOS``,
  ``Granola``, ``ML`` = ``Work/ML``; ``Other`` = the rest), resolved by path.
  Values that are not vault types match stored chunk metadata.
- ``tags``: vector-store chunk metadata (per-tag ``tag:<name>`` boolean keys),
  reading chunk ids only.
- ``people``: BM25 ``people`` column for candidates, confirmed by whole-value
  comparison against the vector store's per-chunk ``people`` lists.
- ``folder``: a vault-relative directory; a path-prefix test on the sets above,
  or a prefix scan of the BM25 catalog when it is the only facet.

Within one facet a list is OR; across facets the sets are ANDed. Machine
facets match nothing when the tag store is missing or empty.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

MACHINE_FACETS = ("doc_type", "domain", "topic", "project")
_LIST_FACETS = ("note_type", "people", "tags") + MACHINE_FACETS


def _as_list(value) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [str(v).strip() for v in items if v is not None and str(v).strip()]


@dataclass
class SearchFacets:
    folder: Optional[str] = None
    note_type: Optional[list[str]] = None
    people: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    doc_type: Optional[list[str]] = None
    domain: Optional[list[str]] = None
    topic: Optional[list[str]] = None
    project: Optional[list[str]] = None
    boost: bool = False

    def __post_init__(self) -> None:
        for name in _LIST_FACETS:
            values = _as_list(getattr(self, name))
            setattr(self, name, values or None)
        self.folder = str(self.folder or "").strip() or None

    def active(self) -> list[str]:
        """Names of the facets that carry a value (never the values)."""
        return [f.name for f in fields(self) if f.name != "boost" and getattr(self, f.name)]

    def is_empty(self) -> bool:
        return not self.active()


def _folder_prefix(folder: str, vault_root: str) -> Optional[str]:
    """Absolute ``<vault>/<folder>/`` prefix, or None when it escapes the vault."""
    if os.path.isabs(folder):
        return None
    root = os.path.realpath(vault_root)
    target = os.path.realpath(os.path.join(root, folder))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target.rstrip(os.sep) + os.sep


def _tag_store_paths(facets: SearchFacets, tag_store, vault_root: str) -> set[str]:
    machine = {n: getattr(facets, n) for n in MACHINE_FACETS if getattr(facets, n)}
    if not machine:
        return set()
    try:
        if tag_store is None:
            from api.services.vault_tag_store import VaultTagStore, get_vault_tags_db_path
            if not os.path.exists(get_vault_tags_db_path()):
                return set()
            tag_store = VaultTagStore()
        rel_paths = tag_store.paths_matching(**machine)
    except Exception as e:
        logger.warning(f"Tag store unavailable for facet filter: {e}")
        return set()
    # The store keys vault-relative paths; the indexes key absolute ones.
    return {p if os.path.isabs(p) else os.path.join(vault_root, p) for p in rel_paths}


def _paths_under(prefix: str, vector_store, bm25_index) -> set[str]:
    if bm25_index is not None:
        return bm25_index.paths_under(prefix)
    return {p for p in vector_store.file_paths_matching() if p.startswith(prefix)}


def _note_type_paths(values: list[str], vector_store, bm25_index, vault_root: str) -> set[str]:
    """Paths for ``note_type`` values, resolved through vault folders.

    Vault types (``NOTE_TYPE_FOLDERS``) are aliases for a folder prefix and
    ``Other`` is every vault file outside all of them, so results never depend
    on stored chunk metadata. Any other value (e.g. a calendar or Slack type)
    matches the stored ``note_type`` metadata.
    """
    from api.services.vault_listing import NOTE_TYPE_FOLDERS

    known = {k.casefold(): k for k in NOTE_TYPE_FOLDERS}
    out: set[str] = set()
    stored: list[str] = []
    for value in values:
        name = known.get(value.casefold())
        if name is not None:
            prefix = _folder_prefix(NOTE_TYPE_FOLDERS[name], vault_root)
            out |= _paths_under(prefix, vector_store, bm25_index) if prefix else set()
        elif value.casefold() == "other":
            everything = _paths_under(os.path.join(vault_root, ""), vector_store, bm25_index)
            for folder in NOTE_TYPE_FOLDERS.values():
                prefix = _folder_prefix(folder, vault_root)
                everything -= {p for p in everything if p.startswith(prefix)}
            out |= everything
        else:
            stored.append(value)
    if stored:
        out |= vector_store.file_paths_matching({"note_type": {"$in": stored}})
    return out


def resolve_allowed_paths(
    facets: SearchFacets,
    vector_store,
    bm25_index,
    tag_store=None,
    vault_root: str | None = None,
) -> set[str]:
    """Absolute file paths satisfying every active facet (empty set: no match)."""
    from api.services.vectorstore import tag_key

    # The indexes key files by ``Path.resolve()``, so the root is resolved once
    # here and every relative key or folder is joined to the resolved root.
    vault_root = os.path.realpath(vault_root or str(settings.vault_path))
    sets: list[set[str]] = []

    if any(getattr(facets, n) for n in MACHINE_FACETS):
        sets.append(_tag_store_paths(facets, tag_store, vault_root))
    if facets.note_type:
        sets.append(_note_type_paths(facets.note_type, vector_store, bm25_index, vault_root))
    if facets.tags:
        clauses = [{tag_key(t): True} for t in facets.tags]
        sets.append(vector_store.file_paths_matching(
            clauses[0] if len(clauses) == 1 else {"$or": clauses}
        ))
    if facets.people:
        candidates = bm25_index.paths_with_people(facets.people) if bm25_index is not None else set()
        sets.append(
            vector_store.file_paths_with_people(candidates, facets.people) if candidates else set()
        )

    if facets.folder:
        prefix = _folder_prefix(facets.folder, vault_root)
        if prefix is None:
            return set()
        if sets:
            sets = [{p for p in s if p.startswith(prefix)} for s in sets]
        elif bm25_index is not None:
            sets.append(bm25_index.paths_under(prefix))
        else:
            sets.append({p for p in vector_store.file_paths_matching() if p.startswith(prefix)})

    if not sets:
        return set()
    allowed = sets[0]
    for other in sets[1:]:
        allowed = allowed & other
        if not allowed:
            break
    return allowed
