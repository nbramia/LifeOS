"""
Usage tracking store for LifeOS.

Tracks API usage costs over time for analytics and budgeting.
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from dataclasses import dataclass

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """A single usage record."""
    id: int
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    conversation_id: Optional[str] = None


class UsageStore:
    """
    SQLite-based usage tracking store.

    Tracks token usage and costs per API call.
    """

    def __init__(self, db_path: str = None):
        """Initialize the usage store."""
        if db_path is None:
            db_path = str(Path(settings.chroma_path).parent / "usage.db")

        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize the database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    conversation_id TEXT,
                    unpriced INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_timestamp
                ON usage(timestamp)
            """)
            # `unpriced` (#613) distinguishes a row whose upstream backend
            # sent no `cost_usd` at all (an external-backend turn from a
            # model the calculator doesn't price) from a row that reported
            # a real cost of zero (a free local model). Both otherwise land
            # in this table with cost_usd=0.0 (see
            # `_HermesTurnPersister._handle_usage` in hermes_proxy.py —
            # cost is still recorded verbatim, never invented), so without
            # this flag the two are indistinguishable once persisted. Added
            # via ALTER rather than in CREATE TABLE above so it lands on a
            # pre-existing usage.db too; a row written before this column
            # existed defaults to 0 (treated as priced) — that history is
            # unrecoverable, not retroactively fixed.
            try:
                conn.execute("ALTER TABLE usage ADD COLUMN unpriced INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists (fresh db, or a prior run already migrated it)
            conn.commit()

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        conversation_id: str = None,
        unpriced: bool = False,
    ) -> int:
        """
        Record a usage entry.

        Args:
            model: Model name used
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD. A negative value is clamped to 0.0 and
                logged loudly (#657) -- see below.
            conversation_id: Optional conversation ID
            unpriced: True when the caller has no real cost for this turn
                (an external backend that sent no `cost_usd`) — `cost_usd`
                is still stored as given (verbatim, never invented) but
                marked so a later reader can tell it apart from a turn that
                genuinely cost zero (#613).

        Returns:
            ID of the created record
        """
        # A negative cost is never legitimate -- money spent can't be less
        # than zero -- and it silently shrinks every SUM(cost_usd) it feeds
        # (GET /api/admin/usage, session-cost totals). #657 traced one
        # historical cause (a cache-token accounting bug in a long-gone
        # version of agent_loop.py that subtracted cache tokens from an
        # already-non-cached input_tokens count), but this guard is a
        # backstop against *any* upstream miscalculation, not just that one.
        # Clamp rather than reject so the call still succeeds and the
        # (accurate) token counts are still recorded -- but log loudly so a
        # recurrence is visible instead of silently absorbed.
        if cost_usd < 0:
            logger.error(
                "record_usage: negative cost_usd=%r for model=%s "
                "(input_tokens=%d, output_tokens=%d, conversation_id=%s) -- "
                "clamping to 0.0. Cost cannot be negative; this indicates a "
                "bug in whatever computed it.",
                cost_usd, model, input_tokens, output_tokens, conversation_id,
            )
            cost_usd = 0.0

        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage (timestamp, model, input_tokens, output_tokens, cost_usd, conversation_id, unpriced)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now, model, input_tokens, output_tokens, cost_usd, conversation_id, int(unpriced))
            )
            conn.commit()
            return cursor.lastrowid

    def get_conversation_usage(self, conversation_id: Optional[str]) -> dict:
        """
        Session-to-date usage for one conversation (#610, extended by #613):
        the sum of every turn already recorded under `conversation_id`, for
        a caller that wants to report "what has this conversation cost so
        far" without recomputing anything.

        Deliberately excludes the turn currently being built: this reads
        only rows a prior call to `record_usage()` already wrote, and the
        in-flight turn's row (if any) isn't written until its own stream
        finishes.

        Returns all-zero, `is_lower_bound=False`, for a conversation with no
        recorded usage (including `conversation_id=None`, e.g. the first
        turn of a brand-new conversation) -- present and zero rather than
        raising, since "no usage yet" is a normal state, not an error.

        Returns:
            Dict with cost_usd, input_tokens, output_tokens (verbatim
            sums), turn_count (how many recorded turns the sum covers),
            and is_lower_bound (True if any summed turn was `unpriced`,
            i.e. some contributing row's cost is unknown rather than
            genuinely zero -- see the `unpriced` column, #613. Rows written
            before that column existed default to priced/`0`, since that
            history can't be reclassified; a sum spanning any such row is
            still a floor even when this flag reads False).
        """
        if not conversation_id:
            return {
                "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                "turn_count": 0, "is_lower_bound": False,
            }

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT SUM(cost_usd), SUM(input_tokens), SUM(output_tokens), COUNT(*), SUM(unpriced)
                FROM usage WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()

        cost, input_tokens, output_tokens, turn_count, unpriced_count = row
        return {
            "cost_usd": cost or 0.0,
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "turn_count": turn_count or 0,
            "is_lower_bound": bool(unpriced_count),
        }

    def get_usage_stats(
        self,
        days: int = None,
        start_date: datetime = None,
        end_date: datetime = None
    ) -> dict:
        """
        Get usage statistics for a time period.

        Args:
            days: Number of days to look back (alternative to start/end dates)
            start_date: Start of period
            end_date: End of period

        Returns:
            Dict with total_cost, total_input_tokens, total_output_tokens, request_count
        """
        if days is not None:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

        query = "SELECT SUM(cost_usd), SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM usage"
        params = []

        if start_date and end_date:
            query += " WHERE timestamp >= ? AND timestamp <= ?"
            params = [start_date.isoformat(), end_date.isoformat()]
        elif start_date:
            query += " WHERE timestamp >= ?"
            params = [start_date.isoformat()]
        elif end_date:
            query += " WHERE timestamp <= ?"
            params = [end_date.isoformat()]

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()

            return {
                "total_cost": row[0] or 0.0,
                "total_input_tokens": row[1] or 0,
                "total_output_tokens": row[2] or 0,
                "request_count": row[3] or 0
            }

    def get_daily_costs(
        self,
        days: int = 30,
        start_date: datetime = None
    ) -> list[dict]:
        """
        Get daily cost breakdown.

        Args:
            days: Number of days to return
            start_date: Start date (defaults to `days` ago)

        Returns:
            List of dicts with date and cost
        """
        if start_date is None:
            start_date = datetime.now() - timedelta(days=days)

        query = """
            SELECT
                DATE(timestamp) as date,
                SUM(cost_usd) as cost,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(*) as requests
            FROM usage
            WHERE timestamp >= ?
            GROUP BY DATE(timestamp)
            ORDER BY date ASC
        """

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, [start_date.isoformat()]).fetchall()

            return [
                {
                    "date": row[0],
                    "cost": row[1] or 0.0,
                    "input_tokens": row[2] or 0,
                    "output_tokens": row[3] or 0,
                    "requests": row[4] or 0
                }
                for row in rows
            ]

    def get_summary(self) -> dict:
        """
        Get a complete usage summary.

        Returns:
            Dict with stats for 24h, 7d, 30d, and all time
        """
        return {
            "last_24h": self.get_usage_stats(days=1),
            "last_7d": self.get_usage_stats(days=7),
            "last_30d": self.get_usage_stats(days=30),
            "all_time": self.get_usage_stats(),
            "daily_breakdown": self.get_daily_costs(days=30)
        }


# Singleton instance
_usage_store: Optional[UsageStore] = None


def get_usage_store() -> UsageStore:
    """Get or create UsageStore singleton."""
    global _usage_store
    if _usage_store is None:
        _usage_store = UsageStore()
    return _usage_store
