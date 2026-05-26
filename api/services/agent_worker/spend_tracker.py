"""Daily $-cap enforcement for the agent worker.

A cumulative dollar counter per local date prevents runaway spend across all
agent tasks. The worker calls `can_start_task(estimated_dollars)` before
claiming; on completion it calls `record(actual_dollars)`. Issue B never
spends real money (no-op dispatcher) but the tracker is exercised by tests
and ready for Issue C/D.
"""
from __future__ import annotations

import sqlite3
from datetime import date as date_cls
from pathlib import Path

from api.services.agent_worker.session_store import DEFAULT_DB_PATH


class SpendTracker:
    """Daily spend ledger backed by the same SQLite file as `session_store`.

    Sharing the DB keeps deployment simple (one file) and lets a future
    "global cap reached → pause new claims" check join across sessions if
    needed.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, daily_cap_dollars: float = 100.0):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_cap_dollars = daily_cap_dollars
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        # session_store also creates this table; be idempotent so import order
        # doesn't matter.
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_spend (
                    date TEXT PRIMARY KEY,
                    total_dollars REAL NOT NULL DEFAULT 0.0
                );
                """
            )

    @staticmethod
    def _today_key(today: date_cls | None = None) -> str:
        return (today or date_cls.today()).isoformat()

    def today_total(self, today: date_cls | None = None) -> float:
        key = self._today_key(today)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT total_dollars FROM daily_spend WHERE date = ?", (key,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def can_start_task(self, estimated_dollars: float, today: date_cls | None = None) -> bool:
        """Return True iff `today_total + estimated_dollars <= daily_cap_dollars`.

        Reasoning: budgets are inclusive — the cap is a ceiling the worker is
        willing to *reach*, not exceed. A task with estimate exactly equal to
        the remaining budget is allowed.

        Special case: `daily_cap_dollars <= 0` is the operator's "pause"
        signal. We refuse all claims unconditionally in that case so a fresh
        clone setting `LIFEOS_AGENT_DAILY_CAP_DOLLARS=0` actually pauses
        instead of allowing zero-dollar tasks through.
        """
        if estimated_dollars < 0:
            raise ValueError("estimated_dollars must be non-negative")
        if self.daily_cap_dollars <= 0:
            return False
        return self.today_total(today) + estimated_dollars <= self.daily_cap_dollars

    def record(self, dollars: float, today: date_cls | None = None) -> float:
        """Add `dollars` to today's bucket. Returns the new total."""
        if dollars < 0:
            raise ValueError("dollars must be non-negative")
        if dollars == 0:
            # No-op: don't create a daily_spend row just to accumulate zero.
            return self.today_total(today)
        key = self._today_key(today)
        with self._connect() as conn:
            # UPSERT: insert or accumulate atomically.
            conn.execute(
                """
                INSERT INTO daily_spend (date, total_dollars)
                VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_dollars = total_dollars + excluded.total_dollars
                """,
                (key, dollars),
            )
            row = conn.execute(
                "SELECT total_dollars FROM daily_spend WHERE date = ?", (key,)
            ).fetchone()
        return float(row[0])
