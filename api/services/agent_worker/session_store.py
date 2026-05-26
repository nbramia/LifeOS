"""SQLite-backed session store for the agent worker.

One row per agent session (which maps 1:1 to a claimed task in this issue).
Schema is deliberately permissive — later issues add columns and tables
(`messages`, `pending_questions`, `sleeps`, lineage fields). The store
intentionally exposes thin CRUD; orchestration lives in `worker.py`.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DB_PATH = Path("data/agent_sessions.db")


# Status vocabulary used across the agent worker. Kept here so other modules
# can import the constants instead of stringly-typed values.
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_YIELDED = "yielded"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_BLOCKED = "blocked"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_BUDGET_EXCEEDED,
})


@dataclass
class Session:
    """Mirrors one row in the `sessions` table.

    Only fields needed by Issue B are populated; later issues fill in routing,
    budget, token counts, etc. Stored timestamps are unix epoch seconds (int).
    """

    task_id: str
    session_id: str
    status: str
    started_at: int
    last_activity_at: int
    routing: str | None = None
    budget: dict | None = None
    expected_output: str | None = None
    parent_session_id: str | None = None
    managed_agent_session_id: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_dollars: float = 0.0
    total_active_seconds: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    task_id                   TEXT PRIMARY KEY,
    session_id                TEXT UNIQUE NOT NULL,
    status                    TEXT NOT NULL,
    routing                   TEXT,
    budget_json               TEXT,
    started_at                INTEGER NOT NULL,
    last_activity_at          INTEGER NOT NULL,
    total_input_tokens        INTEGER NOT NULL DEFAULT 0,
    total_output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_dollars             REAL    NOT NULL DEFAULT 0.0,
    total_active_seconds      REAL    NOT NULL DEFAULT 0.0,
    expected_output           TEXT,
    parent_session_id         TEXT,
    managed_agent_session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS daily_spend (
    date           TEXT PRIMARY KEY,
    total_dollars  REAL NOT NULL DEFAULT 0.0
);

-- Conversation log for local-path sessions. Managed sessions store messages
-- in the JSONL transcript instead (their authoritative state lives on the
-- Anthropic side).
CREATE TABLE IF NOT EXISTS messages (
    session_id    TEXT NOT NULL,
    turn_index    INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content_json  TEXT NOT NULL,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

-- Sleep wake-ups. A session with a row here is "yielded": the worker's main
-- loop scans this table and resumes the session when wake_at <= now().
CREATE TABLE IF NOT EXISTS sleeps (
    session_id    TEXT PRIMARY KEY,
    wake_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sleeps_wake ON sleeps(wake_at);
"""


def _now() -> int:
    return int(time.time())


def new_session_id() -> str:
    """Generate an internal session_id. Independent of any platform id."""
    return f"sess_{uuid.uuid4().hex[:16]}"


class SessionStore:
    """Thin SQLite wrapper. Each method opens a short-lived connection so the
    store is safe to use from multiple threads or processes — SQLite's own
    locking serializes writers."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL improves concurrent reader/writer behavior; safe to re-set.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        task_id: str,
        session_id: str | None = None,
        status: str = STATUS_CLAIMED,
        routing: str | None = None,
        budget: dict | None = None,
        expected_output: str | None = None,
        parent_session_id: str | None = None,
    ) -> Session:
        """Insert a new session row. Raises sqlite3.IntegrityError if `task_id`
        already has a row — the caller should treat that as a lost-race signal.
        """
        sid = session_id or new_session_id()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    task_id, session_id, status, routing, budget_json,
                    started_at, last_activity_at,
                    expected_output, parent_session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, sid, status, routing,
                    json.dumps(budget) if budget else None,
                    now, now,
                    expected_output, parent_session_id,
                ),
            )
        return Session(
            task_id=task_id,
            session_id=sid,
            status=status,
            started_at=now,
            last_activity_at=now,
            routing=routing,
            budget=budget,
            expected_output=expected_output,
            parent_session_id=parent_session_id,
        )

    def get(self, task_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def get_by_session_id(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def update_status(self, task_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, last_activity_at = ?
                WHERE task_id = ?
                """,
                (status, _now(), task_id),
            )

    def list_by_status(self, status: str) -> list[Session]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY started_at ASC",
                (status,),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def list_non_terminal(self) -> list[Session]:
        """Return sessions that the worker may still need to act on after restart."""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE status NOT IN ({placeholders})",
                tuple(TERMINAL_STATUSES),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def set_routing_and_budget(
        self,
        task_id: str,
        routing: str | None,
        budget: dict | None,
        expected_output: str | None = None,
    ) -> None:
        """Update routing decision and budget after the preflight call."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET routing = ?, budget_json = ?, expected_output = ?, last_activity_at = ?
                WHERE task_id = ?
                """,
                (
                    routing,
                    json.dumps(budget) if budget else None,
                    expected_output,
                    _now(),
                    task_id,
                ),
            )

    def record_spend(
        self,
        task_id: str,
        tokens_in: int,
        tokens_out: int,
        dollars: float,
    ) -> None:
        """Add to a session's cumulative token + dollar counters."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET total_input_tokens  = total_input_tokens  + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_dollars       = total_dollars       + ?,
                    last_activity_at    = ?
                WHERE task_id = ?
                """,
                (tokens_in, tokens_out, dollars, _now(), task_id),
            )

    def record_active_seconds(self, task_id: str, seconds: float) -> None:
        """Add to a session's cumulative active-execution seconds.

        Active seconds exclude sleep time — this is the duration of LLM calls
        + tool dispatch, used by the wall-clock budget check. A session that
        spends 8 hours sleeping but only 5 minutes actually running has
        total_active_seconds ≈ 300, not 28800.
        """
        if seconds <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET total_active_seconds = total_active_seconds + ?, "
                "last_activity_at = ? WHERE task_id = ?",
                (float(seconds), _now(), task_id),
            )

    # ------------------------------------------------------------------
    # Messages (local-path conversation log)
    # ------------------------------------------------------------------

    def append_message(
        self,
        session_id: str,
        role: str,
        content: dict | list | str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> int:
        """Append one message; return its 0-based turn_index.

        Uses a single INSERT that computes the next turn_index inside the
        statement, so concurrent appends from sibling worker processes
        (Issue E) can't pick the same index — SQLite serializes the write.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    session_id, turn_index, role, content_json,
                    tokens_in, tokens_out, created_at
                )
                SELECT ?, COALESCE(MAX(turn_index), -1) + 1, ?, ?, ?, ?, ?
                FROM messages WHERE session_id = ?
                """,
                (
                    session_id, role, json.dumps(content),
                    tokens_in, tokens_out, _now(),
                    session_id,
                ),
            )
            row = conn.execute(
                "SELECT MAX(turn_index) AS i FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["i"])

    def get_messages(self, session_id: str) -> list[dict]:
        """Return all messages in order as {role, content} dicts ready for the LLM."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content_json FROM messages "
                "WHERE session_id = ? ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]

    # ------------------------------------------------------------------
    # Sleeps (yield / wake)
    # ------------------------------------------------------------------

    def add_sleep(self, session_id: str, wake_at: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sleeps (session_id, wake_at) VALUES (?, ?)",
                (session_id, int(wake_at)),
            )

    def remove_sleep(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sleeps WHERE session_id = ?", (session_id,))

    def due_sleeps(self, now_ts: int | None = None) -> list[str]:
        """Return session_ids whose wake time has arrived."""
        ts = now_ts if now_ts is not None else _now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sleeps WHERE wake_at <= ? ORDER BY wake_at ASC",
                (ts,),
            ).fetchall()
        return [r["session_id"] for r in rows]

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            task_id=row["task_id"],
            session_id=row["session_id"],
            status=row["status"],
            routing=row["routing"],
            budget=json.loads(row["budget_json"]) if row["budget_json"] else None,
            started_at=row["started_at"],
            last_activity_at=row["last_activity_at"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_dollars=row["total_dollars"],
            total_active_seconds=(
                row["total_active_seconds"]
                if "total_active_seconds" in row.keys()
                else 0.0
            ),
            expected_output=row["expected_output"],
            parent_session_id=row["parent_session_id"],
            managed_agent_session_id=row["managed_agent_session_id"],
        )
