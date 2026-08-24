"""
Idempotency store for the journal ring ingestion endpoint (#660).

A capture device (e.g. the Pebble Index ring) may retry a delivery on a flaky
connection; this store lets `POST /api/journal/ingest` recognize a retried
delivery and log it exactly once. It persists only a *derived* dedupe key
(a sha256 hash of device/timestamp/text, or a device-supplied id) — never the
fragment text itself — so this store can't become another place personal
content leaks.

Mirrors `api/services/fitness_store.py`'s db-path and singleton pattern.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings


def get_journal_ingest_db_path() -> str:
    """Path to the journal-ingest dedupe database (alongside the other LifeOS
    SQLite stores)."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "journal_ingest.db")


class JournalIngestStore:
    """SQLite-backed record of dedupe keys already processed."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_journal_ingest_db_path()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS processed_ingests ("
                "dedupe_key TEXT PRIMARY KEY, "
                "processed_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def was_processed(self, dedupe_key: str) -> bool:
        """True if this dedupe key has already been recorded as processed."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_ingests WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def mark_processed(self, dedupe_key: str) -> None:
        """Record a dedupe key as processed. Call only after the capture
        pipeline has succeeded, so a delivery that failed mid-flight can still
        be retried rather than being silently swallowed as a "duplicate"."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO processed_ingests (dedupe_key, processed_at) "
                "VALUES (?, ?)",
                (dedupe_key, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()


_store_instance: Optional[JournalIngestStore] = None


def get_journal_ingest_store() -> JournalIngestStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = JournalIngestStore()
    return _store_instance
