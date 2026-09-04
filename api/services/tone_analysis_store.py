"""
Tone Analysis Store for LifeOS CRM.

Persists per-person, per-month relationship tone analysis results so the
Relationship page can read them instantly instead of recomputing an LLM
call on every page load. Mirrors the storage style of
`api/services/relationship_insights.py`: a small SQLite-backed store, one
fresh connection per call (never cached across calls/threads -- see
`tests/test_route_handlers_no_shared_connections.py`), created lazily on
first use via `CREATE TABLE IF NOT EXISTS`.

Each row is one person's tone result for one calendar month (`period_key`,
`YYYY-MM`). The `result` column holds the month's scores as a JSON blob
(see `api/routes/crm.py`'s `analyze_relationship_tone_detailed` for the
shape). `interaction_count` is the number of interactions that went into
computing the result -- the caller uses it, together with `updated_at`, to
decide whether a stored month is still fresh.

Rows for a merged-away or deleted person don't orphan (#910): when two
people are merged, `scripts/merge_people.py` deletes the absorbed
person's rows here in the same crm.db transaction it clears
`person_facts` in -- deliberately *not* re-keyed onto the survivor (a
re-keyed row could collide with a `period_key` the survivor already has,
since the primary key here is `(person_id, period_key)`) and deliberately
leaving the survivor's own rows untouched (unlike `person_facts`, a
survivor's stale row self-heals via the ordinary interaction-count
freshness check in `api/routes/crm.py`'s
`analyze_relationship_tone_detailed` the next time tone analysis runs for
it, so there's nothing here that needs forcing). `scripts/cleanup_orphaned_records.py`
separately reports and removes any row whose person id no longer exists
at all, guarded for installs where this table hasn't been created yet.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from api.utils.datetime_utils import make_aware as _make_aware
from api.utils.db_paths import get_crm_db_path

logger = logging.getLogger(__name__)


@dataclass
class ToneAnalysisResult:
    """A stored tone analysis result for one person, for one month."""
    person_id: str = ""
    period_key: str = ""  # YYYY-MM
    interaction_count: int = 0
    result: dict = field(default_factory=dict)
    model: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "ToneAnalysisResult":
        """Create from a SQLite row.

        Column order: 0: person_id, 1: period_key, 2: interaction_count,
        3: result, 4: model, 5: created_at, 6: updated_at
        """
        try:
            result = json.loads(row[3]) if row[3] else {}
        except (TypeError, ValueError):
            result = {}
        return cls(
            person_id=row[0],
            period_key=row[1],
            interaction_count=row[2] or 0,
            result=result,
            model=row[4] or "",
            created_at=_make_aware(datetime.fromisoformat(row[5])) if row[5] else None,
            updated_at=_make_aware(datetime.fromisoformat(row[6])) if row[6] else None,
        )


class ToneAnalysisStore:
    """SQLite-backed storage for relationship tone analysis results."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_crm_db_path()
        self._init_db()

    def _init_db(self):
        """Create the tone_analysis_results table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tone_analysis_results (
                    person_id TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    interaction_count INTEGER DEFAULT 0,
                    result TEXT NOT NULL,
                    model TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (person_id, period_key)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tone_analysis_person
                ON tone_analysis_results(person_id)
            """)

            conn.commit()
            logger.info(f"Initialized tone_analysis_results table in {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def get_for_person(self, person_id: str) -> list[ToneAnalysisResult]:
        """Get all stored monthly tone results for a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT person_id, period_key, interaction_count, result, model, created_at, updated_at
                FROM tone_analysis_results
                WHERE person_id = ?
                ORDER BY period_key ASC
            """, (person_id,))
            return [ToneAnalysisResult.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_month(self, person_id: str, period_key: str) -> Optional[ToneAnalysisResult]:
        """Get a single person/month tone result, if stored."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT person_id, period_key, interaction_count, result, model, created_at, updated_at
                FROM tone_analysis_results
                WHERE person_id = ? AND period_key = ?
            """, (person_id, period_key))
            row = cursor.fetchone()
            return ToneAnalysisResult.from_row(row) if row else None
        finally:
            conn.close()

    def upsert(
        self,
        person_id: str,
        period_key: str,
        interaction_count: int,
        result: dict,
        model: str = "",
    ) -> ToneAnalysisResult:
        """Insert or replace a person/month tone result.

        Idempotent: calling this twice with the same person_id + period_key
        replaces the row rather than creating a duplicate (enforced by the
        PRIMARY KEY on (person_id, period_key)).
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO tone_analysis_results
                    (person_id, period_key, interaction_count, result, model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id, period_key) DO UPDATE SET
                    interaction_count = excluded.interaction_count,
                    result = excluded.result,
                    model = excluded.model,
                    updated_at = excluded.updated_at
            """, (
                person_id,
                period_key,
                interaction_count,
                json.dumps(result),
                model,
                now,
                now,
            ))
            conn.commit()
        finally:
            conn.close()
        return ToneAnalysisResult(
            person_id=person_id,
            period_key=period_key,
            interaction_count=interaction_count,
            result=result,
            model=model,
            updated_at=_make_aware(datetime.fromisoformat(now)),
        )



# Singleton instance
_tone_analysis_store: Optional[ToneAnalysisStore] = None


def get_tone_analysis_store(db_path: Optional[str] = None) -> ToneAnalysisStore:
    """Get or create the singleton ToneAnalysisStore."""
    global _tone_analysis_store
    if _tone_analysis_store is None:
        _tone_analysis_store = ToneAnalysisStore(db_path)
    return _tone_analysis_store
