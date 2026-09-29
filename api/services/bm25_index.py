"""
BM25 Index for LifeOS.

Keyword-based search using SQLite FTS5 to complement vector search.
Finds exact matches for names, IDs, and codes that vector search may miss.

## Key Design Decisions

- **OR semantics**: AND fails when no chunk has all terms
- **Query sanitization**: Strips FTS5 special chars (', ", ?, .)
- **Stop word removal**: Filters "what", "is", "the", etc.
- **BM25 scores are negative**: Lower = better match

## Usage

    from api.services.bm25_index import get_bm25_index
    results = get_bm25_index().search("Alex phone", limit=20)
"""
import sqlite3
import logging
import unicodedata
from pathlib import Path
from typing import Collection, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def get_bm25_db_path() -> str:
    """Get the path to the BM25 database."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "bm25_index.db")


def file_path_of_doc_id(doc_id: str) -> str:
    """File path of a chunk id: ``{path}_{chunk}`` or ``{path}::summary``."""
    if doc_id.endswith("::summary"):
        return doc_id[: -len("::summary")]
    head, sep, tail = doc_id.rpartition("_")
    return head if sep and tail.isdigit() else doc_id


class BM25Index:
    """
    SQLite FTS5-backed BM25 keyword index.

    Provides fast keyword search to complement vector similarity.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize BM25 index.

        Args:
            db_path: Path to SQLite database (default from settings)
        """
        self.db_path = db_path or get_bm25_db_path()
        self._init_db()

    def _init_db(self):
        """Create FTS5 table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Create FTS5 virtual table for full-text search
            # Using porter tokenizer for stemming
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    doc_id,
                    content,
                    file_name,
                    people,
                    tokenize='porter unicode61'
                )
            """)
            # Sidecar date table. FTS5 virtual tables can't gain a column
            # without a destructive rebuild, so doc dates live in a plain table
            # keyed by doc_id. Created idempotently; populated incrementally as
            # documents are (re)indexed, so it self-heals on the nightly sync
            # without forcing a full reindex.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doc_dates (
                    doc_id TEXT PRIMARY KEY,
                    modified_date TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def add_document(
        self,
        doc_id: str,
        content: str,
        file_name: str,
        people: Optional[list[str]] = None,
        modified_date: Optional[str] = None
    ):
        """
        Add or update a document in the index.

        Args:
            doc_id: Unique document identifier
            content: Document text content
            file_name: Source file name
            people: List of people mentioned
            modified_date: Document date (YYYY-MM-DD), used for recency ranking
                and date-range filtering
        """
        people_str = " ".join(people) if people else ""

        conn = sqlite3.connect(self.db_path)
        try:
            # Delete existing entry if present (for updates)
            conn.execute(
                "DELETE FROM chunks_fts WHERE doc_id = ?",
                (doc_id,)
            )
            # Insert new entry
            conn.execute(
                "INSERT INTO chunks_fts (doc_id, content, file_name, people) VALUES (?, ?, ?, ?)",
                (doc_id, content, file_name, people_str)
            )
            if modified_date:
                conn.execute(
                    "INSERT OR REPLACE INTO doc_dates (doc_id, modified_date) VALUES (?, ?)",
                    (doc_id, modified_date)
                )
            else:
                conn.execute("DELETE FROM doc_dates WHERE doc_id = ?", (doc_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_document(self, doc_id: str):
        """
        Remove a document from the index.

        Args:
            doc_id: Document identifier to remove
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM chunks_fts WHERE doc_id = ?",
                (doc_id,)
            )
            conn.execute("DELETE FROM doc_dates WHERE doc_id = ?", (doc_id,))
            conn.commit()
        finally:
            conn.close()

    def get_summaries(self, file_paths: list[str]) -> dict[str, str]:
        """Return the stored one-line summary text for each indexed path that has one.

        Keys are the given paths; the "Document summary for <name>: " prefix
        written at index time is stripped.
        """
        if not file_paths:
            return {}
        by_id = {f"{p}::summary": p for p in file_paths}
        conn = sqlite3.connect(self.db_path)
        try:
            found: dict[str, str] = {}
            ids = list(by_id)
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                marks = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT doc_id, content FROM chunks_fts WHERE doc_id IN ({marks})", batch
                ).fetchall()
                for doc_id, content in rows:
                    text = content.split(": ", 1)[1] if content.startswith("Document summary for ") and ": " in content else content
                    found[by_id[doc_id]] = text
            return found
        finally:
            conn.close()

    def delete_by_path(self, file_path: str) -> int:
        """Delete every chunk indexed for ``file_path`` (chunks + summary).

        Chunk ``doc_id`` values are ``{path}_{i}``; the summary uses
        ``{path}::summary``. ``delete_document`` only matches an exact id, so
        callers that wanted to drop "all chunks for this file" — like the
        indexer's re-index path — silently leaked stale rows whenever the
        chunk count shrank or the path changed (e.g. macOS → Linux). This
        helper clears everything for the given path in one query.

        Args:
            file_path: Absolute file path used as the doc_id prefix.

        Returns:
            Number of rows removed (chunks + summary).
        """
        conn = sqlite3.connect(self.db_path)
        try:
            # FTS5 doesn't support ESCAPE on LIKE, so escape special chars
            # by enumerating the two known suffix patterns explicitly. The
            # summary uses '::summary' and chunks use '_<int>'.
            id_match = """
                doc_id = ?
                   OR doc_id = ?
                   OR doc_id GLOB ?
            """
            id_params = (
                file_path,                       # legacy: path with no suffix
                f"{file_path}::summary",         # summary chunk
                f"{file_path}_*",                # numbered chunks
            )
            cursor = conn.execute(
                f"DELETE FROM chunks_fts WHERE {id_match}", id_params
            )
            deleted = cursor.rowcount
            conn.execute(f"DELETE FROM doc_dates WHERE {id_match}", id_params)
            conn.commit()
            return deleted
        finally:
            conn.close()

    _STOP_WORDS = frozenset({
        'a', 'an', 'the', 'is', 'are', 'was', 'were', 'what', 'when', 'where',
        'who', 'which', 'how', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
        'for', 'of', 'with', 'by',
    })

    _JOINERS = frozenset("'\u2019-")

    @staticmethod
    def _extract_terms(query: str) -> list[str]:
        """
        Split text into terms (NFC-normalized).

        A term is a run of letters, digits and combining marks that may contain
        apostrophes (``'``, U+2019) and hyphens between them, so ``name's`` and
        ``follow-up`` stay single terms. Leading and trailing joiners are
        separators.
        """
        terms: list[str] = []
        current: list[str] = []

        def flush() -> None:
            while current and current[-1] in BM25Index._JOINERS:
                current.pop()
            if current:
                terms.append("".join(current))
            current.clear()

        for ch in unicodedata.normalize("NFC", query) + " ":
            if ch.isalnum() or unicodedata.category(ch).startswith("M"):
                current.append(ch)
            elif ch in BM25Index._JOINERS and current:
                current.append(ch)
            else:
                flush()
        return terms

    def _sanitize_query(self, query: str, use_or: bool = False) -> str:
        """
        Turn arbitrary text into a valid FTS5 MATCH expression.

        Every term from ``_extract_terms`` (after NFC
        normalization) becomes a double-quoted term, so no
        character in the input (punctuation, symbols, emoji, a leading ``-``
        or ``^``, ``col:`` filters) and no uppercase ``AND``/``OR``/``NOT``/
        ``NEAR`` word can act as FTS5 syntax. Quoted terms still pass through
        the porter tokenizer, so stemming applies.

        Args:
            query: Raw query string
            use_or: If True, join terms with OR (any term matches) after
                   removing stop words. If False, terms are implicitly ANDed.

        Returns:
            FTS5 expression, or an empty string when the query has no terms.
        """
        terms = self._extract_terms(query)
        if use_or:
            informative = [t for t in terms if t.lower() not in self._STOP_WORDS]
            terms = informative or terms
        quoted = [f'"{t}"' for t in terms]
        return (" OR " if use_or else " ").join(quoted)

    def _match(
        self,
        conn: sqlite3.Connection,
        match_expr: str,
        limit: int,
        match_mode: str,
        file_paths: Optional[Collection[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> list[dict]:
        """Run one FTS5 MATCH expression and tag each row with its match mode.

        ``file_paths`` and the ``date_from``/``date_to`` window (inclusive
        ``YYYY-MM-DD``; undated chunks pass) restrict the rows before the
        ``LIMIT`` applies, so a selective restriction still returns ``limit``
        rows when that many exist.
        """
        restrict = ""
        params: list = [match_expr]
        if file_paths is not None:
            allowed = file_paths if isinstance(file_paths, (set, frozenset)) else set(file_paths)
            conn.create_function(
                "in_allowed_files", 1,
                lambda doc_id: 1 if file_path_of_doc_id(doc_id) in allowed else 0,
                deterministic=True,
            )
            restrict = "AND in_allowed_files(chunks_fts.doc_id)"
        if date_from:
            restrict += " AND (COALESCE(doc_dates.modified_date, '') = '' OR substr(doc_dates.modified_date, 1, 10) >= ?)"
            params.append(date_from)
        if date_to:
            restrict += " AND (COALESCE(doc_dates.modified_date, '') = '' OR substr(doc_dates.modified_date, 1, 10) <= ?)"
            params.append(date_to)
        params.append(limit)
        cursor = conn.execute(
            f"""
            SELECT chunks_fts.doc_id, chunks_fts.content,
                   chunks_fts.file_name, chunks_fts.people,
                   bm25(chunks_fts) as score, doc_dates.modified_date
            FROM chunks_fts
            LEFT JOIN doc_dates ON doc_dates.doc_id = chunks_fts.doc_id
            WHERE chunks_fts MATCH ? {restrict}
            ORDER BY score
            LIMIT ?
            """,
            params,
        )
        return [
            {
                "doc_id": row[0],
                "content": row[1],
                "file_name": row[2],
                "people": row[3].split(",") if row[3] else [],
                "bm25_score": row[4],  # BM25 scores are negative, lower is better
                "modified_date": row[5] or "",
                "match_mode": match_mode,
            }
            for row in cursor.fetchall()
        ]

    def paths_under(self, prefix: str) -> set[str]:
        """Absolute file paths of indexed chunks whose path starts with ``prefix``."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT doc_id FROM chunks_fts WHERE substr(doc_id, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        finally:
            conn.close()
        return {file_path_of_doc_id(r[0]) for r in rows}

    def paths_with_people(self, names: list[str]) -> set[str]:
        """Candidate file paths whose people column contains any of ``names``' terms.

        The column is a space-joined, stemmed FTS text, so this is a superset
        of the exact matches; callers confirm whole person values against the
        vector store's per-chunk ``people`` lists."""
        phrases = []
        for name in names:
            terms = self._extract_terms(name)
            if terms:
                phrases.append('"' + " ".join(terms) + '"')
        if not phrases:
            return set()
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT doc_id FROM chunks_fts WHERE chunks_fts MATCH ?",
                ("people : (" + " OR ".join(phrases) + ")",),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"BM25 people lookup error: {e}")
            return set()
        finally:
            conn.close()
        return {file_path_of_doc_id(r[0]) for r in rows}

    def search(
        self,
        query: str,
        limit: int = 20,
        file_paths: Optional[Collection[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> list[dict]:
        """
        Search the index using BM25.

        Runs a strict query (every term must match) first, then fills any
        remaining slots up to ``limit`` with any-term matches (stop words
        removed) not already returned. The any-term query is skipped when the
        strict one already fills ``limit``. Each result carries ``match_mode``
        of ``"and"`` or ``"or"``; strict rows come first.

        Args:
            query: Search query string
            limit: Maximum number of results
            file_paths: When given, only chunks of these files (absolute
                paths) are candidates; an empty collection matches nothing.
            date_from: Inclusive lower bound (YYYY-MM-DD) on doc date, applied
                before ``limit``; undated chunks pass.
            date_to: Inclusive upper bound, same rules.

        Returns:
            List of matching documents with doc_id and BM25 score
        """
        strict_query = self._sanitize_query(query)
        if not strict_query:
            return []
        restrict = {
            k: v for k, v in
            (("file_paths", file_paths), ("date_from", date_from), ("date_to", date_to))
            if v is not None
        }

        conn = sqlite3.connect(self.db_path)
        try:
            results = self._match(conn, strict_query, limit, "and", **restrict)
            if len(results) >= limit:
                return results
            lenient_query = self._sanitize_query(query, use_or=True)
            if lenient_query == strict_query:
                return results
            seen = {r["doc_id"] for r in results}
            # Over-fetch by the strict count so its rows can't crowd out the fill.
            for row in self._match(conn, lenient_query, limit + len(results), "or", **restrict):
                if row["doc_id"] not in seen:
                    seen.add(row["doc_id"])
                    results.append(row)
                    if len(results) >= limit:
                        break
            return results
        except sqlite3.OperationalError as e:
            logger.warning(f"BM25 search error for query '{query}': {e}")
            return []
        finally:
            conn.close()

    def bulk_add(self, documents: list[dict]):
        """
        Add multiple documents efficiently.

        Args:
            documents: List of dicts with doc_id, content, file_name, people
        """
        conn = sqlite3.connect(self.db_path)
        try:
            for doc in documents:
                people_str = " ".join(doc.get("people", [])) if doc.get("people") else ""
                conn.execute(
                    "DELETE FROM chunks_fts WHERE doc_id = ?",
                    (doc["doc_id"],)
                )
                conn.execute(
                    "INSERT INTO chunks_fts (doc_id, content, file_name, people) VALUES (?, ?, ?, ?)",
                    (doc["doc_id"], doc["content"], doc["file_name"], people_str)
                )
                modified_date = doc.get("modified_date")
                if modified_date:
                    conn.execute(
                        "INSERT OR REPLACE INTO doc_dates (doc_id, modified_date) VALUES (?, ?)",
                        (doc["doc_id"], modified_date)
                    )
                else:
                    conn.execute("DELETE FROM doc_dates WHERE doc_id = ?", (doc["doc_id"],))
            conn.commit()
        finally:
            conn.close()

    def clear(self):
        """Clear all documents from the index."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM doc_dates")
            conn.commit()
        finally:
            conn.close()

    def count(self) -> int:
        """Get total number of documents in index."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM chunks_fts")
            return cursor.fetchone()[0]
        finally:
            conn.close()


# Singleton instance
_bm25_instance: Optional[BM25Index] = None


def get_bm25_index() -> BM25Index:
    """Get the singleton BM25Index instance."""
    global _bm25_instance
    if _bm25_instance is None:
        _bm25_instance = BM25Index()
    return _bm25_instance


def reset_bm25_index() -> None:
    """
    Reset the BM25 index singleton.

    For testing only - allows tests to start with fresh state.
    """
    global _bm25_instance
    _bm25_instance = None
