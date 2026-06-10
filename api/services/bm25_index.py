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
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def get_bm25_db_path() -> str:
    """Get the path to the BM25 database."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "bm25_index.db")


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

    def _sanitize_query(self, query: str, use_or: bool = True) -> str:
        """
        Sanitize query for FTS5 MATCH syntax.

        FTS5 has special characters that cause syntax errors:
        - Quotes, apostrophes, parentheses need removal
        - Reserved words (AND, OR, NOT, NEAR) are handled by FTS5

        Args:
            query: Raw query string
            use_or: If True, join terms with OR (any term matches).
                   If False, use default AND (all terms must match).

        Returns:
            Sanitized query safe for FTS5
        """
        import re
        # Remove characters that break FTS5 syntax
        # Periods in filenames (like .md), question marks, etc cause issues
        # Keep alphanumeric, spaces, hyphens, underscores
        sanitized = re.sub(r"['\"\(\)\[\]\{\}\*\^\~\.\:\;\?\!]", " ", query)
        # Collapse multiple spaces
        sanitized = re.sub(r"\s+", " ", sanitized).strip()

        if use_or and sanitized:
            # Join terms with OR for more lenient matching
            # Filter out common stop words that add noise
            stop_words = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'what',
                         'when', 'where', 'who', 'which', 'how', 'and', 'or',
                         'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
            terms = [t for t in sanitized.split() if t.lower() not in stop_words]
            if terms:
                sanitized = " OR ".join(terms)

        return sanitized

    def search(
        self,
        query: str,
        limit: int = 20
    ) -> list[dict]:
        """
        Search the index using BM25.

        Args:
            query: Search query string
            limit: Maximum number of results

        Returns:
            List of matching documents with doc_id and BM25 score
        """
        if not query.strip():
            return []

        # Sanitize query for FTS5 syntax
        sanitized_query = self._sanitize_query(query)
        if not sanitized_query:
            return []

        conn = sqlite3.connect(self.db_path)
        try:
            # FTS5 MATCH query with BM25 ranking
            # Search across content, file_name, and people
            # Return all columns for full result data
            cursor = conn.execute(
                """
                SELECT chunks_fts.doc_id, chunks_fts.content,
                       chunks_fts.file_name, chunks_fts.people,
                       bm25(chunks_fts) as score, doc_dates.modified_date
                FROM chunks_fts
                LEFT JOIN doc_dates ON doc_dates.doc_id = chunks_fts.doc_id
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (sanitized_query, limit)
            )

            results = []
            for row in cursor.fetchall():
                results.append({
                    "doc_id": row[0],
                    "content": row[1],
                    "file_name": row[2],
                    "people": row[3].split(",") if row[3] else [],
                    "bm25_score": row[4],  # Note: BM25 scores are negative, lower is better
                    "modified_date": row[5] or ""
                })

            return results

        except sqlite3.OperationalError as e:
            # Handle invalid FTS query syntax
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
