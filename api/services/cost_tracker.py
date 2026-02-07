"""
API Cost Tracking for LifeOS (P6.2).

Tracks token usage and calculates costs for Claude API calls.
"""
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Model pricing (per million tokens)
MODEL_PRICING = {
    "haiku": {"input": 0.25, "output": 1.25},
    "sonnet": {"input": 3.0, "output": 15.0},
    "opus": {"input": 15.0, "output": 75.0},
}

# Default database path
DEFAULT_DB_PATH = Path.home() / ".lifeos" / "cost_tracking.db"


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate the cost for a Claude API call.

    Args:
        model: Model name (haiku, sonnet, opus, or full name)
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens

    Returns:
        Cost in USD
    """
    # Normalize model name
    model_lower = model.lower()
    if "haiku" in model_lower:
        pricing = MODEL_PRICING["haiku"]
    elif "opus" in model_lower:
        pricing = MODEL_PRICING["opus"]
    else:
        # Default to sonnet for any other model
        pricing = MODEL_PRICING["sonnet"]

    # Calculate cost (price is per million tokens)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost


@dataclass
class UsageRecord:
    """Record of a single API usage."""
    id: str
    conversation_id: Optional[str]
    message_id: Optional[str]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: datetime

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CostTracker:
    """
    Service for tracking API usage and costs.

    Stores usage data in SQLite for historical analysis.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize cost tracker.

        Args:
            db_path: Path to SQLite database (default: ~/.lifeos/cost_tracking.db)
        """
        self.db_path = db_path or str(DEFAULT_DB_PATH)

        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_usage (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    message_id TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_usd REAL,
                    created_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_conversation
                ON api_usage(conversation_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_created
                ON api_usage(created_at)
            """)
            conn.commit()

    def record_usage(
        self,
        conversation_id: Optional[str],
        message_id: Optional[str],
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> UsageRecord:
        """
        Record API usage.

        Args:
            conversation_id: ID of the conversation
            message_id: ID of the message
            model: Model used
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            UsageRecord with calculated cost
        """
        record = UsageRecord(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            message_id=message_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=calculate_cost(model, input_tokens, output_tokens),
            created_at=datetime.now()
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO api_usage
                (id, conversation_id, message_id, model, input_tokens, output_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.id,
                record.conversation_id,
                record.message_id,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.cost_usd,
                record.created_at
            ))
            conn.commit()

        logger.debug(f"Recorded usage: {record.model} - {record.input_tokens}in/{record.output_tokens}out - ${record.cost_usd:.4f}")

        return record

    def get_conversation_total(self, conversation_id: str) -> float:
        """
        Get total cost for a conversation.

        Args:
            conversation_id: ID of the conversation

        Returns:
            Total cost in USD
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0)
                FROM api_usage
                WHERE conversation_id = ?
            """, (conversation_id,))
            result = cursor.fetchone()
            return result[0] if result else 0.0

    def get_session_total(self, hours: int = 24) -> float:
        """
        Get total cost for the current session (last N hours).

        Args:
            hours: Number of hours to include (default 24)

        Returns:
            Total cost in USD
        """
        since = datetime.now() - timedelta(hours=hours)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0)
                FROM api_usage
                WHERE created_at >= ?
            """, (since,))
            result = cursor.fetchone()
            return result[0] if result else 0.0

    def get_usage_by_conversation(self, conversation_id: str) -> list[UsageRecord]:
        """
        Get all usage records for a conversation.

        Args:
            conversation_id: ID of the conversation

        Returns:
            List of UsageRecords
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM api_usage
                WHERE conversation_id = ?
                ORDER BY created_at DESC
            """, (conversation_id,))
            rows = cursor.fetchall()

        return [self._row_to_record(row) for row in rows]

    def get_usage_by_date_range(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> list[UsageRecord]:
        """
        Get usage records within a date range.

        Args:
            start_date: Start of date range
            end_date: End of date range

        Returns:
            List of UsageRecords
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM api_usage
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at DESC
            """, (start_date, end_date))
            rows = cursor.fetchall()

        return [self._row_to_record(row) for row in rows]

    def get_total_usage(self) -> dict:
        """
        Get total usage statistics.

        Returns:
            Dictionary with total tokens and cost
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT
                    COALESCE(SUM(input_tokens), 0) as total_input,
                    COALESCE(SUM(output_tokens), 0) as total_output,
                    COALESCE(SUM(cost_usd), 0) as total_cost,
                    COUNT(*) as total_requests
                FROM api_usage
            """)
            row = cursor.fetchone()
            return {
                "total_input_tokens": row[0],
                "total_output_tokens": row[1],
                "total_cost_usd": row[2],
                "total_requests": row[3]
            }

    def _row_to_record(self, row) -> UsageRecord:
        """Convert database row to UsageRecord."""
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return UsageRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
            created_at=created_at
        )


def format_usage_event(
    record: UsageRecord,
    conversation_total: float = None,
    session_total: float = None
) -> dict:
    """
    Format usage record for SSE event.

    Args:
        record: UsageRecord to format
        conversation_total: Total cost for conversation
        session_total: Total cost for session

    Returns:
        Dictionary suitable for SSE event
    """
    event = {
        "type": "usage",
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cost_usd": record.cost_usd,
        "model": record.model,
    }

    if conversation_total is not None:
        event["conversation_total"] = conversation_total
    if session_total is not None:
        event["session_total"] = session_total

    return event


# Singleton instance
_cost_tracker: Optional[CostTracker] = None


def get_cost_tracker() -> CostTracker:
    """Get or create CostTracker singleton."""
    global _cost_tracker
    if _cost_tracker is None:
        _cost_tracker = CostTracker()
    return _cost_tracker
