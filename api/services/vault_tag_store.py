"""Rebuildable store for machine-derived vault tags.

`data/vault_tags.db` (next to, but separate from, ``bm25_index.db``, so a forced
BM25 rebuild never drops tags) holds one row per vault file in table
``vault_tags``. A row is current while the file's content hash and the
taxonomy ``vocab_version`` both match; modification time plays no part.
Raw probabilities live in ``topics_json`` so thresholds can change without
re-inference.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable

from config.settings import settings


@dataclass
class TagRecord:
    file_path: str
    content_sha256: str
    vocab_version: str
    model: str = ""
    tagged_at: str = ""
    doc_type: str | None = None
    doc_type_conf: float | None = None
    domain: str | None = None
    domain_conf: float | None = None
    topic: str | None = None
    topic_conf: float | None = None
    topics_json: str = "{}"
    project: str | None = None
    project_conf: float | None = None
    actionability: float | None = None
    has_decision: float | None = None
    sensitivity: str = "private"
    backend: str = "code"  # "jev" | "code"


_COLUMNS = [f.name for f in fields(TagRecord)]
_FILTERABLE = {"doc_type", "domain", "topic", "project", "sensitivity", "backend"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_tags (
    file_path TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    vocab_version TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    tagged_at TEXT NOT NULL DEFAULT '',
    doc_type TEXT, doc_type_conf REAL,
    domain TEXT, domain_conf REAL,
    topic TEXT, topic_conf REAL,
    topics_json TEXT NOT NULL DEFAULT '{}',
    project TEXT, project_conf REAL,
    actionability REAL,
    has_decision REAL,
    sensitivity TEXT NOT NULL DEFAULT 'private',
    backend TEXT NOT NULL DEFAULT 'code'
);
CREATE INDEX IF NOT EXISTS idx_vault_tags_doc_type ON vault_tags(doc_type);
CREATE INDEX IF NOT EXISTS idx_vault_tags_domain ON vault_tags(domain);
CREATE INDEX IF NOT EXISTS idx_vault_tags_topic ON vault_tags(topic);
CREATE INDEX IF NOT EXISTS idx_vault_tags_project ON vault_tags(project);
"""


def get_vault_tags_db_path() -> str:
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "vault_tags.db")


class VaultTagStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_vault_tags_db_path()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, record: TagRecord) -> None:
        cols = ", ".join(_COLUMNS)
        marks = ", ".join(f":{c}" for c in _COLUMNS)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO vault_tags ({cols}) VALUES ({marks})", asdict(record)
                )
        finally:
            conn.close()

    def get(self, path: str) -> TagRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM vault_tags WHERE file_path = ?", (path,)).fetchone()
        finally:
            conn.close()
        return TagRecord(**{c: row[c] for c in _COLUMNS}) if row else None

    def needs_tagging(self, path: str, sha: str, vocab_version: str) -> bool:
        """True unless the stored row matches both content hash and vocab version."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT content_sha256, vocab_version FROM vault_tags WHERE file_path = ?", (path,)
            ).fetchone()
        finally:
            conn.close()
        return row is None or row["content_sha256"] != sha or row["vocab_version"] != vocab_version

    def paths_matching(self, **facets: str | Iterable[str]) -> list[str]:
        """Paths whose row matches every given facet (AND across facets).

        A facet value may be a string or a list of strings (OR within the
        facet). A ``topic`` value matches that exact topic or, given a
        parent, every ``parent/child`` topic under it.
        """
        unknown = set(facets) - _FILTERABLE
        if unknown:
            raise ValueError(f"unsupported facet(s): {', '.join(sorted(unknown))}")
        clauses: list[str] = []
        params: list[str] = []
        for name, raw in facets.items():
            values = [raw] if isinstance(raw, str) else list(raw)
            if not values:
                return []
            ors = []
            for v in values:
                ors.append(f"{name} = ?")
                params.append(v)
                if name == "topic":
                    ors.append("topic LIKE ?")
                    params.append(v + "/%")
            clauses.append("(" + " OR ".join(ors) + ")")
        where = " AND ".join(clauses) or "1"
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT file_path FROM vault_tags WHERE {where} ORDER BY file_path", params
            ).fetchall()
        finally:
            conn.close()
        return [r["file_path"] for r in rows]

    def delete_missing(self, existing_paths: Iterable[str]) -> int:
        """Delete rows whose path is not in ``existing_paths``; returns the count."""
        keep = set(existing_paths)
        conn = self._connect()
        try:
            with conn:
                stored = [r["file_path"] for r in conn.execute("SELECT file_path FROM vault_tags")]
                gone = [p for p in stored if p not in keep]
                conn.executemany("DELETE FROM vault_tags WHERE file_path = ?", [(p,) for p in gone])
        finally:
            conn.close()
        return len(gone)
