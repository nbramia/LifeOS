"""Controlled vocabulary for vault tagging.

``config/vault_taxonomy.yaml`` (committed, generic) defines the closed option
sets machine tagging chooses from: document types, domains, ``parent/child``
topics, and the kinds of live lists project names come from. An optional
git-ignored ``config/vault_taxonomy.local.yaml`` is merged over it at load:
list entries are added, topics are replaced by name, and a ``remove`` mapping
(facet -> values) deletes entries.

``Taxonomy.vocab_version`` is the first 12 hex characters of the SHA-256 of the
merged content in canonical form (sorted keys and lists), so formatting and
ordering changes keep the version while any vocabulary change alters it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
DEFAULT_TAXONOMY_PATH = _CONFIG_DIR / "vault_taxonomy.yaml"
DEFAULT_OVERRIDE_PATH = _CONFIG_DIR / "vault_taxonomy.local.yaml"

_LIST_FACETS = ("doc_types", "domains", "project_sources")


class TaxonomyError(ValueError):
    """The taxonomy file (or its override) is malformed."""


@dataclass(frozen=True)
class Taxonomy:
    version: str
    doc_types: tuple[str, ...]
    domains: tuple[str, ...]
    topics: dict[str, str]  # "parent/child" -> one-line description
    project_sources: tuple[str, ...]
    vocab_version: str


_cache: dict[tuple[str, str | None], Taxonomy] = {}


def reset_taxonomy_cache() -> None:
    """Drop cached taxonomies (for tests and after editing the files)."""
    _cache.clear()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise TaxonomyError(f"cannot read taxonomy file {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TaxonomyError(f"{path}: top level must be a mapping")
    return data


def _string_list(raw: Any, facet: str, source: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise TaxonomyError(f"{source}: '{facet}' must be a list of non-empty strings")
    values = [v.strip() for v in raw]
    dupes = sorted({v for v in values if values.count(v) > 1})
    if dupes:
        raise TaxonomyError(f"{source}: duplicate values in '{facet}': {', '.join(dupes)}")
    return values


def _topic_entries(raw: Any, source: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise TaxonomyError(f"{source}: 'topics' must be a list of {{name, description}}")
    topics: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise TaxonomyError(f"{source}: each topic needs a string 'name'")
        name = item["name"].strip()
        if name in topics:
            raise TaxonomyError(f"{source}: duplicate values in 'topics': {name}")
        topics[name] = str(item.get("description") or "").strip()
    return topics


def _parse_layer(data: dict[str, Any], source: str) -> dict[str, Any]:
    layer: dict[str, Any] = {
        facet: _string_list(data.get(facet), facet, source) for facet in _LIST_FACETS
    }
    layer["topics"] = _topic_entries(data.get("topics"), source)
    remove = data.get("remove") or {}
    if not isinstance(remove, dict):
        raise TaxonomyError(f"{source}: 'remove' must map facet names to value lists")
    unknown = set(remove) - set(_LIST_FACETS) - {"topics"}
    if unknown:
        raise TaxonomyError(f"{source}: unknown facet in 'remove': {', '.join(sorted(unknown))}")
    layer["remove"] = {
        facet: _string_list(values, f"remove.{facet}", source) for facet, values in remove.items()
    }
    layer["version"] = str(data.get("version", "")).strip()
    return layer


def _validate_topics(topics: dict[str, str], domains: list[str]) -> None:
    for name in topics:
        parent, sep, child = name.partition("/")
        if not sep or not parent or not child or "/" in child:
            raise TaxonomyError(f"topic '{name}' must have the form parent/child")
        if parent not in domains:
            raise TaxonomyError(f"topic '{name}': parent '{parent}' is not a declared domain")


def _canonical(merged: dict[str, Any]) -> str:
    body = {
        "version": merged["version"],
        **{facet: sorted(merged[facet]) for facet in _LIST_FACETS},
        "topics": merged["topics"],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_taxonomy(
    path: str | Path | None = None,
    override_path: str | Path | None = None,
) -> Taxonomy:
    """Load the taxonomy, merging the operator override when one exists.

    With the default ``path`` the override defaults to
    ``config/vault_taxonomy.local.yaml`` (skipped when absent). With an
    explicit ``path`` and no ``override_path`` no override is applied.
    Results are cached per (path, override_path); call ``reset_taxonomy_cache``
    to reload.
    """
    explicit_path = path is not None
    base_path = Path(path) if explicit_path else DEFAULT_TAXONOMY_PATH
    if override_path is not None:
        local_path: Path | None = Path(override_path)
    else:
        local_path = None if explicit_path else DEFAULT_OVERRIDE_PATH
    if local_path is not None and not local_path.exists():
        local_path = None

    key = (str(base_path), str(local_path) if local_path else None)
    if key in _cache:
        return _cache[key]

    merged = _parse_layer(_read_yaml(base_path), str(base_path))
    if local_path is not None:
        layer = _parse_layer(_read_yaml(local_path), str(local_path))
        for facet in _LIST_FACETS:
            merged[facet] += [v for v in layer[facet] if v not in merged[facet]]
        merged["topics"].update(layer["topics"])
        if layer["version"]:
            merged["version"] = layer["version"]
        for facet, values in layer["remove"].items():
            drop = set(values)
            if facet == "topics":
                merged["topics"] = {k: v for k, v in merged["topics"].items() if k not in drop}
            else:
                merged[facet] = [v for v in merged[facet] if v not in drop]

    if not merged["version"]:
        raise TaxonomyError(f"{base_path}: 'version' is required")
    _validate_topics(merged["topics"], merged["domains"])

    taxonomy = Taxonomy(
        version=merged["version"],
        doc_types=tuple(merged["doc_types"]),
        domains=tuple(merged["domains"]),
        topics=dict(merged["topics"]),
        project_sources=tuple(merged["project_sources"]),
        vocab_version=hashlib.sha256(_canonical(merged).encode("utf-8")).hexdigest()[:12],
    )
    _cache[key] = taxonomy
    return taxonomy
