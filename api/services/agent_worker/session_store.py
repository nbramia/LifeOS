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
    root_session_id: str | None = None
    spawn_depth: int = 0
    yield_waiting_for: list[str] | None = None


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
    root_session_id           TEXT,
    spawn_depth               INTEGER NOT NULL DEFAULT 0,
    yield_waiting_for         TEXT,  -- JSON array of session_ids the agent is waiting on
    managed_agent_session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_yield ON sessions(status) WHERE status = 'yielded';

-- Inter-agent messages queued for delivery to a peer/child/parent session.
-- Used by `lifeos_agent_send` for sessions that aren't actively running.
-- For yielded sessions, these are injected when the session resumes.
CREATE TABLE IF NOT EXISTS pending_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    sender_id    TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_msgs_session ON pending_messages(session_id, delivered);

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

-- Cursor + bookkeeping for Managed Agents sessions. One row per task_id with
-- the last event id we've ingested + the cumulative session-hour dollars
-- already booked into the sessions row's total_dollars. Issue D.
CREATE TABLE IF NOT EXISTS managed_cursor (
    task_id                          TEXT PRIMARY KEY,
    last_event_id                    TEXT,
    accrued_session_hour_dollars     REAL NOT NULL DEFAULT 0.0
);
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
        root_session_id: str | None = None,
        spawn_depth: int = 0,
    ) -> Session:
        """Insert a new session row. Raises sqlite3.IntegrityError if `task_id`
        already has a row — the caller should treat that as a lost-race signal.

        For root sessions (no parent), `root_session_id` defaults to the new
        session's own id. Children inherit the parent's `root_session_id` so
        lineage queries can find an entire family with one indexed lookup.
        """
        sid = session_id or new_session_id()
        root_sid = root_session_id or sid
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    task_id, session_id, status, routing, budget_json,
                    started_at, last_activity_at,
                    expected_output, parent_session_id,
                    root_session_id, spawn_depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, sid, status, routing,
                    json.dumps(budget) if budget else None,
                    now, now,
                    expected_output, parent_session_id,
                    root_sid, spawn_depth,
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
            root_session_id=root_sid,
            spawn_depth=spawn_depth,
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

    def list_active_managed(self) -> list[Session]:
        """Sessions with a remote Managed Agents id that haven't terminated."""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions "
                f"WHERE managed_agent_session_id IS NOT NULL "
                f"AND status NOT IN ({placeholders})",
                tuple(TERMINAL_STATUSES),
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

    def set_managed_session_id(self, task_id: str, managed_id: str) -> None:
        """Attach a remote Managed Agents session_id to a local session."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET managed_agent_session_id = ?, last_activity_at = ? "
                "WHERE task_id = ?",
                (managed_id, _now(), task_id),
            )

    def add_session_hour_overhead(self, task_id: str, dollars: float) -> None:
        """Add Managed Agents session-hour overhead to the dollar counter."""
        if dollars <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET total_dollars = total_dollars + ?, last_activity_at = ? "
                "WHERE task_id = ?",
                (float(dollars), _now(), task_id),
            )

    # Managed Agents cursor (defined in _SCHEMA so no schema-on-write needed).
    def set_managed_last_event_id(self, task_id: str, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO managed_cursor (task_id, last_event_id) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET last_event_id = excluded.last_event_id",
                (task_id, event_id),
            )

    def get_managed_last_event_id(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_event_id FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return row["last_event_id"] if row else None

    def get_accrued_session_hour_dollars(self, task_id: str) -> float:
        """Dollars already booked into total_dollars for session-hour overhead.

        Used by the managed executor to compute the incremental session-hour
        delta to add on each poll, avoiding double-counting.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT accrued_session_hour_dollars FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set_accrued_session_hour_dollars(self, task_id: str, dollars: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO managed_cursor (task_id, accrued_session_hour_dollars) "
                "VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET accrued_session_hour_dollars = excluded.accrued_session_hour_dollars",
                (task_id, float(dollars)),
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

    # ------------------------------------------------------------------
    # Lineage / yield / pending messages (Issue E)
    # ------------------------------------------------------------------

    def list_by_session_ids(self, session_ids: list[str]) -> list[Session]:
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE session_id IN ({placeholders})",
                tuple(session_ids),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def count_active_by_routing(self, routing: str) -> int:
        """Sessions with the given `routing` that aren't terminal yet."""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM sessions "
                f"WHERE routing = ? AND status NOT IN ({placeholders})",
                (routing, *TERMINAL_STATUSES),
            ).fetchone()
        return int(row["c"])

    def count_descendants(self, root_session_id: str) -> int:
        """Count of sessions sharing the given root, excluding the root itself."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions "
                "WHERE root_session_id = ? AND session_id != ?",
                (root_session_id, root_session_id),
            ).fetchone()
        return int(row["c"])

    def lineage_total_dollars(self, root_session_id: str) -> float:
        """Aggregate spend across a session and all its descendants."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_dollars), 0) AS s FROM sessions "
                "WHERE root_session_id = ?",
                (root_session_id,),
            ).fetchone()
        return float(row["s"])

    def list_descendants(self, root_session_id: str) -> list[Session]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions "
                "WHERE root_session_id = ? AND session_id != ? ",
                (root_session_id, root_session_id),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def list_yielded_waiting_on_children(self) -> list[Session]:
        """Yielded sessions where the resume condition is children-done."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions "
                "WHERE status = ? AND yield_waiting_for IS NOT NULL",
                (STATUS_YIELDED,),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def set_yield_waiting_for(self, task_id: str, children: list[str] | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET yield_waiting_for = ?, last_activity_at = ? "
                "WHERE task_id = ?",
                (json.dumps(children) if children else None, _now(), task_id),
            )

    def list_sessions(
        self,
        status: str | None = None,
        routing: str | None = None,
        parent_session_id: str | None = None,
        limit: int = 200,
    ) -> list[Session]:
        """Filtered listing for the `lifeos_agent_sessions_list` tool."""
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if routing:
            conditions.append("routing = ?")
            params.append(routing)
        if parent_session_id:
            conditions.append("parent_session_id = ?")
            params.append(parent_session_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY started_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    # ------------------------------------------------------------------
    # Pending messages (Issue E send-to-yielded)
    # ------------------------------------------------------------------

    def enqueue_message(self, session_id: str, sender_id: str, content: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pending_messages (session_id, sender_id, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, sender_id, content, _now()),
            )
        return cur.lastrowid

    def drain_pending_messages(self, session_id: str) -> list[dict]:
        """Return + mark-delivered all pending messages for `session_id`."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, sender_id, content, created_at FROM pending_messages "
                "WHERE session_id = ? AND delivered = 0 ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE pending_messages SET delivered = 1 WHERE session_id = ? AND delivered = 0",
                    (session_id,),
                )
        return [
            {"id": r["id"], "sender_id": r["sender_id"], "content": r["content"], "created_at": r["created_at"]}
            for r in rows
        ]

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
            root_session_id=(
                row["root_session_id"] if "root_session_id" in row.keys() else None
            ),
            spawn_depth=(
                row["spawn_depth"] if "spawn_depth" in row.keys() else 0
            ),
            yield_waiting_for=(
                json.loads(row["yield_waiting_for"])
                if "yield_waiting_for" in row.keys() and row["yield_waiting_for"]
                else None
            ),
        )
