"""Rebuildable store for machine-derived vault tags.

`data/vault_tags.db` (next to, but separate from, ``bm25_index.db``, so a forced
BM25 rebuild never drops tags) holds one row per vault file in table
``vault_tags``. A row is current while the file's content hash and the
taxonomy ``vocab_version`` both match; modification time plays no part.
Raw probabilities live in ``topics_json`` so thresholds can change without
re-inference.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable, Iterator

from api.services.sqlite_connect import connect_closing
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
    doc_type_source: str | None = "code"  # "frontmatter" | "jev" | "code"; NULL (older rows) reads as "jev"


# A requested topic matches a note whose stored topic distribution ranks it
# within the top TOPIC_MATCH_TOP_K entries or gives it at least TOPIC_MATCH_MIN_P.
TOPIC_MATCH_TOP_K = 3
TOPIC_MATCH_MIN_P = 0.10

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
    backend TEXT NOT NULL DEFAULT 'code',
    doc_type_source TEXT
);
CREATE TABLE IF NOT EXISTS vault_tag_topics (
    file_path TEXT NOT NULL,
    topic TEXT NOT NULL,
    p REAL NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (file_path, topic)
);
CREATE INDEX IF NOT EXISTS idx_vault_tag_topics_topic ON vault_tag_topics(topic);
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
        with self._connect() as conn:
            fresh = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vault_tag_topics'"
            ).fetchone() is None
            conn.executescript(_SCHEMA)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(vault_tags)")}
            if "doc_type_source" not in columns:
                conn.execute("ALTER TABLE vault_tags ADD COLUMN doc_type_source TEXT")
            if fresh:
                with conn:
                    for r in conn.execute("SELECT file_path, topics_json FROM vault_tags").fetchall():
                        self._write_distribution(conn, r["file_path"], r["topics_json"])

    @staticmethod
    def _write_distribution(conn: sqlite3.Connection, file_path: str, topics_json: str) -> None:
        conn.execute("DELETE FROM vault_tag_topics WHERE file_path = ?", (file_path,))
        conn.executemany(
            "INSERT INTO vault_tag_topics (file_path, topic, p, rank) VALUES (?, ?, ?, ?)",
            _distribution_rows(file_path, topics_json),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with connect_closing(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def upsert(self, record: TagRecord) -> None:
        cols = ", ".join(_COLUMNS)
        marks = ", ".join(f":{c}" for c in _COLUMNS)
        with self._connect() as conn:
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO vault_tags ({cols}) VALUES ({marks})", asdict(record)
                )
                self._write_distribution(conn, record.file_path, record.topics_json)

    def get(self, path: str) -> TagRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM vault_tags WHERE file_path = ?", (path,)).fetchone()
        if not row:
            return None
        record = TagRecord(**{c: row[c] for c in _COLUMNS})
        record.doc_type_source = record.doc_type_source or "jev"
        return record

    def needs_tagging(self, path: str, sha: str, vocab_version: str) -> bool:
        """True unless the stored row matches both content hash and vocab version."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_sha256, vocab_version FROM vault_tags WHERE file_path = ?", (path,)
            ).fetchone()
        return row is None or row["content_sha256"] != sha or row["vocab_version"] != vocab_version

    def paths_matching(self, **facets: str | Iterable[str]) -> list[str]:
        """Paths whose row matches every given facet (AND across facets).

        A facet value may be a string or a list of strings (OR within the
        facet). A ``topic`` value matches a note when it is the note's
        primary or a secondary topic, or sits in the note's stored topic
        distribution (mirrored in ``vault_tag_topics``) within the top
        ``TOPIC_MATCH_TOP_K`` or at probability ``TOPIC_MATCH_MIN_P`` or more. A parent value also matches every
        ``parent/child`` topic under it.
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
            if name == "topic":
                clause, tparams = _topic_clause(values)
                clauses.append(clause)
                params.extend(tparams)
                continue
            clauses.append("(" + " OR ".join(f"{name} = ?" for _ in values) + ")")
            params.extend(values)
        where = " AND ".join(clauses) or "1"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT file_path FROM vault_tags WHERE {where} ORDER BY file_path", params
            ).fetchall()
        return [r["file_path"] for r in rows]

    def delete_missing(self, existing_paths: Iterable[str]) -> int:
        """Delete rows whose path is not in ``existing_paths``; returns the count."""
        keep = set(existing_paths)
        with self._connect() as conn:
            with conn:
                stored = [r["file_path"] for r in conn.execute("SELECT file_path FROM vault_tags")]
                gone = [p for p in stored if p not in keep]
                conn.executemany("DELETE FROM vault_tags WHERE file_path = ?", [(p,) for p in gone])
                conn.executemany(
                    "DELETE FROM vault_tag_topics WHERE file_path = ?", [(p,) for p in gone]
                )
        return len(gone)


def _distribution_rows(file_path: str, topics_json: str) -> list[tuple[str, str, float, int]]:
    """Side-table rows ``(file_path, topic, p, rank)`` for one note.

    Every topic with p > 0 gets its 1-based rank by descending probability;
    a secondary topic without probability mass gets rank 0, and a topic
    an operator tag mapped to (``from_tags``) gets p = 1.0 at rank 0.
    """
    try:
        data = json.loads(topics_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    dist = data.get("topic")
    dist = dist if isinstance(dist, dict) else {}
    ranked = sorted(
        ((lb, float(p)) for lb, p in dist.items()
         if isinstance(lb, str) and isinstance(p, (int, float)) and p > 0),
        key=lambda kv: -kv[1],
    )
    rows = {lb: (file_path, lb, p, r) for r, (lb, p) in enumerate(ranked, 1)}
    secondary = data.get("secondary")
    for lab in secondary if isinstance(secondary, list) else []:
        if isinstance(lab, str):
            rows[lab] = (file_path, lab, rows[lab][2] if lab in rows else 0.0, 0)
    from_tags = data.get("from_tags")
    for lab in from_tags if isinstance(from_tags, dict) else []:
        if isinstance(lab, str):
            rows[lab] = (file_path, lab, 1.0, 0)
    return list(rows.values())


def _topic_clause(requested: list[str]) -> tuple[str, list]:
    """SQL clause: primary, secondary, or distribution match for any requested topic."""
    labels = [x for t in requested for x in (t, t + "/%")]
    like = " OR ".join("(topic = ? OR topic LIKE ?)" for _ in requested)
    side = " OR ".join("(t.topic = ? OR t.topic LIKE ?)" for _ in requested)
    clause = (
        f"({like} OR EXISTS (SELECT 1 FROM vault_tag_topics t "
        f"WHERE t.file_path = vault_tags.file_path AND ({side}) "
        "AND (t.rank <= ? OR t.p >= ?)))"
    )
    return clause, labels + labels + [TOPIC_MATCH_TOP_K, TOPIC_MATCH_MIN_P]
