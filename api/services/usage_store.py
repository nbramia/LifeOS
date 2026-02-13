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
                    conversation_id TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_timestamp
                ON usage(timestamp)
            """)
            conn.commit()

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        conversation_id: str = None
    ) -> int:
        """
        Record a usage entry.

        Args:
            model: Model name used
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD
            conversation_id: Optional conversation ID

        Returns:
            ID of the created record
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage (timestamp, model, input_tokens, output_tokens, cost_usd, conversation_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, model, input_tokens, output_tokens, cost_usd, conversation_id)
            )
            conn.commit()
            return cursor.lastrowid

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
