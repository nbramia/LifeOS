"""Daily $-cap enforcement for the agent worker.

A cumulative dollar counter per local date prevents runaway spend across all
agent tasks. The worker calls `can_start_task(estimated_dollars)` before
claiming; on completion it calls `record(actual_dollars)`. The configured
`daily_cap_dollars` can be raised for the current local date only — via
`set_cap_override` — so a "raise to $N" reply lifts today's cap without
touching the configured default, and the override reverts on its own once
the date rolls over (it's stored per-date, alongside `total_dollars`).
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import date as date_cls
from pathlib import Path
from typing import Iterator

from api.services.agent_worker.session_store import DEFAULT_DB_PATH
from api.services.sqlite_connect import connect_closing


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

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, closing it on the way out — see
        `SessionStore._connect`, which shares this database and this
        pattern."""
        with connect_closing(str(self.db_path), isolation_level=None, timeout=10.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn

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
            # Idempotent migrations for the per-day cap override and the
            # once-per-day notice marker, mirroring session_store's
            # PRAGMA table_info-guarded ADD COLUMN pattern.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_spend)")}
            if "cap_override_dollars" not in cols:
                conn.execute("ALTER TABLE daily_spend ADD COLUMN cap_override_dollars REAL")
            if "notified_cap_dollars" not in cols:
                conn.execute("ALTER TABLE daily_spend ADD COLUMN notified_cap_dollars REAL")

    @staticmethod
    def _today_key(today: date_cls | None = None) -> str:
        return (today or date_cls.today()).isoformat()

    def today_key(self, today: date_cls | None = None) -> str:
        """Public form of `_today_key`, for callers that need the same
        per-date identity used by this tracker's storage (e.g. to name a
        replyable notice for "today")."""
        return self._today_key(today)

    def today_total(self, today: date_cls | None = None) -> float:
        key = self._today_key(today)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT total_dollars FROM daily_spend WHERE date = ?", (key,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def effective_cap_dollars(self, today: date_cls | None = None) -> float:
        """The cap in force for `today`: a `set_cap_override` value for that
        date if one was recorded, else the configured `daily_cap_dollars`.

        The override lives on the same per-date `daily_spend` row as the
        running total, so it persists across a worker restart and reverts
        on its own once the date rolls over — no separate cleanup needed.
        """
        key = self._today_key(today)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cap_override_dollars FROM daily_spend WHERE date = ?", (key,)
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return self.daily_cap_dollars

    def set_cap_override(self, new_cap: float, today: date_cls | None = None) -> float:
        """Raise (or otherwise set) today's effective cap to `new_cap`.

        Scoped to the given local date only; every other date keeps using
        the configured `daily_cap_dollars`.
        """
        key = self._today_key(today)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_spend (date, total_dollars, cap_override_dollars)
                VALUES (?, 0.0, ?)
                ON CONFLICT(date) DO UPDATE SET
                    cap_override_dollars = excluded.cap_override_dollars
                """,
                (key, new_cap),
            )
        return new_cap

    def notified_cap_dollars(self, today: date_cls | None = None) -> float | None:
        """The cap value the once-per-day crossing notice was already sent
        for today, or None if no notice has gone out yet today."""
        key = self._today_key(today)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified_cap_dollars FROM daily_spend WHERE date = ?", (key,)
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def mark_cap_notified(self, cap: float, today: date_cls | None = None) -> None:
        """Record that the crossing notice for `cap` went out today, so a
        later tick that finds the same cap still in force doesn't re-notify.
        A subsequent `set_cap_override` to a different value naturally
        allows one more notice when that new cap is crossed."""
        key = self._today_key(today)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_spend (date, total_dollars, notified_cap_dollars)
                VALUES (?, 0.0, ?)
                ON CONFLICT(date) DO UPDATE SET
                    notified_cap_dollars = excluded.notified_cap_dollars
                """,
                (key, cap),
            )

    def can_start_task(self, estimated_dollars: float, today: date_cls | None = None) -> bool:
        """Return True iff `today_total + estimated_dollars <= effective_cap_dollars`.

        Reasoning: budgets are inclusive — the cap is a ceiling the worker is
        willing to *reach*, not exceed. A task with estimate exactly equal to
        the remaining budget is allowed.

        Special case: an effective cap `<= 0` is the operator's "pause"
        signal. We refuse all claims unconditionally in that case so a fresh
        clone setting `LIFEOS_AGENT_DAILY_CAP_DOLLARS=0` actually pauses
        instead of allowing zero-dollar tasks through.
        """
        if estimated_dollars < 0:
            raise ValueError("estimated_dollars must be non-negative")
        cap = self.effective_cap_dollars(today)
        if cap <= 0:
            return False
        return self.today_total(today) + estimated_dollars <= cap

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
