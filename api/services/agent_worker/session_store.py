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


# Anchored to the repo root (this file's own location), NOT the caller's
# cwd (#640 review). A bare relative `Path("data/agent_sessions.db")`
# resolves against whatever process opens it — fine for the API and worker,
# which both run from the repo root, but Hermes runs `mcp_server.py` as a
# stdio child from ITS OWN cwd (`~/.hermes`), so a caller relying on this
# default would silently create and read an empty sibling DB there instead
# of the real one, defeating the whole point of a shared caller_session_id.
# Same fix, same reason, as `load_dotenv()` in `api/main.py` (#598) and
# `job_queue.py`'s `_DEFAULT_DB_PATH` — anchor to `__file__`, not cwd. Only
# the DEFAULT changes: a caller that passes its own (even relative) db_path
# explicitly still resolves that path against its own cwd, unchanged.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "agent_sessions.db"


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
    # Preset class for per-session tool filtering (#139 §3). When set,
    # ManagedExecutor.start() calls driver.update_session() with the
    # class's filtered tool list between create and the first user
    # message — scoping cache_creation to the smaller tool set.
    preset_class: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # Prompt-cache buckets — kept separate from total_input_tokens because
    # they're billed at different rates (cache_creation = 1.25× input,
    # cache_read = 0.10× input). On cache-heavy presets cache_creation on the
    # first turn often dwarfs uncached input.
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_dollars: float = 0.0
    total_active_seconds: float = 0.0
    root_session_id: str | None = None
    spawn_depth: int = 0
    yield_waiting_for: list[str] | None = None
    # Provenance of the session. NULL/"agent" = claimed from an #agent vault
    # task; "operator" = root-spawned on demand from Telegram/chat with no
    # backing task. The worker's spawned-session dispatch picks up operator
    # sessions even though they have no parent (#235).
    origin: str | None = None
    # Claude Code CLI session UUID, captured from the subprocess's init
    # stream-json event. Set only for routing="claude_code" sessions and used
    # by ClaudeCodeExecutor.resume() to invoke `claude -r <id>` so the CLI
    # picks up its prior in-process state across worker restarts.
    # NB: codex sessions reuse this column too (CodexExecutor stores the
    # codex thread id here); routing disambiguates.
    claude_code_session_id: str | None = None
    # Claude tier the Claude Code CLI runs for routing="claude_code" sessions
    # ("haiku" / "sonnet" / "opus"). Set by lifeos_agent_spawn's `tier` arg so
    # the worker can escalate simple delegated work to a cheaper model. NULL
    # falls back to the CLI default ("opus").
    claude_code_model: str | None = None
    # Telegram bot identity that owns this session's operator-facing messages.
    # NULL = primary bot (the default for every legacy / non-doctor session).
    # An orchestration bot (e.g. "doctor") tags its spawned sessions so the
    # worker routes [NOTIFY]/[CLARIFY]/completion notices back to that bot, not
    # the primary. See api/services/telegram.py and config/telegram_bots.json.
    bot: str | None = None
    # True once any `record_spend` call for this session priced a turn from
    # an unrecognized model (pricing.is_known_model() == False). Sticky for
    # the life of the session -- unlike total_dollars, which only ever
    # accrues real priced spend, this flag exists so a reader can tell
    # "$0.00 total" apart from "some turns couldn't be priced" (#669, same
    # motivation as usage_store's per-row `unpriced` column from #613/#661,
    # adapted here to an accumulating total rather than one row per turn).
    unpriced: bool = False


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
    total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    total_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    total_dollars             REAL    NOT NULL DEFAULT 0.0,
    total_active_seconds      REAL    NOT NULL DEFAULT 0.0,
    expected_output           TEXT,
    parent_session_id         TEXT,
    root_session_id           TEXT,
    spawn_depth               INTEGER NOT NULL DEFAULT 0,
    yield_waiting_for         TEXT,  -- JSON array of session_ids the agent is waiting on
    managed_agent_session_id  TEXT,
    preset_class              TEXT,
    origin                    TEXT,  -- NULL/"agent" = #agent task; "operator" = root-spawned (#235)
    claude_code_session_id    TEXT,  -- Claude Code (or Codex) CLI session UUID for routing="claude_code"/"codex"
    claude_code_model         TEXT,  -- Claude tier for routing="claude_code" (haiku/sonnet/opus); NULL = CLI default (opus)
    bot                       TEXT,  -- Telegram bot that owns this session's notices; NULL = primary (#348)
    unpriced                  INTEGER NOT NULL DEFAULT 0  -- sticky: any record_spend call priced an unknown model (#669)
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

-- Open clarification questions sent to the operator via Telegram (Issue F)
-- AND completion-message follow-ups (operator replies to a finished task's
-- Telegram message to continue the thread, like "now turn this into a .md").
-- The listener hook in telegram.py matches incoming reply_to_message ids
-- against `sent_message_id` and deposits the answer. Worker.tick scans for
-- answered+unprocessed rows to resume sessions, and for timed-out rows to
-- send a follow-up nudge.
--
-- `kind` distinguishes the two flows:
--   "clarification" — agent asked a question mid-task, session is BLOCKED,
--                     the answer unblocks and resumes the executor.
--   "followup"      — task already completed, operator replies on the
--                     completion message to continue. Resume reopens the
--                     COMPLETED session and appends the reply as a new
--                     user turn so the agent retains full context.
CREATE TABLE IF NOT EXISTS pending_questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    question          TEXT NOT NULL,
    sent_message_id   INTEGER NOT NULL,
    sent_at           INTEGER NOT NULL,
    answer            TEXT,
    answered_at       INTEGER,
    processed         INTEGER NOT NULL DEFAULT 0,
    timed_out         INTEGER NOT NULL DEFAULT 0,
    kind              TEXT NOT NULL DEFAULT 'clarification',
    -- JSON array of every Telegram chunk id for this notification. Long
    -- completions split across multiple 4096-char messages; a reply can land
    -- on any chunk, so `deposit_answer` matches membership in this list (not
    -- just the first chunk in `sent_message_id`). NULL for legacy rows, which
    -- still match via `sent_message_id`.
    sent_message_ids  TEXT,
    -- Telegram bot that sent this question. NULL = primary. Reply matching is
    -- scoped by bot so a doctor-bot reply can't collide with a primary-bot
    -- question that happens to share a numeric message id (#348).
    bot               TEXT
);
CREATE INDEX IF NOT EXISTS idx_pq_message_id ON pending_questions(sent_message_id);
CREATE INDEX IF NOT EXISTS idx_pq_open ON pending_questions(answered_at, processed, timed_out);

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
--
-- `final_text` caches the most recent agent.message text seen across polls.
-- Required because `get_session_state` uses an event cursor: if the final
-- agent.message arrives in poll N-1 and only `session.status_idle` arrives in
-- poll N, the latter's response has no text to extract. The executor writes
-- this on every poll where `state.final_text` is non-None, and reads it at
-- finalize so the Telegram completion summary is never empty when the agent
-- actually produced output.
CREATE TABLE IF NOT EXISTS managed_cursor (
    task_id                          TEXT PRIMARY KEY,
    last_event_id                    TEXT,
    accrued_session_hour_dollars     REAL NOT NULL DEFAULT 0.0,
    final_text                       TEXT,
    -- Runaway detection counters (#139 Section 5). Persisted so cross-poll
    -- signals survive worker restarts mid-session.
    -- `tool_loop_signature` is the (tool_name, sorted_args_json) of the most
    -- recent tool call; `tool_loop_count` is how many consecutive times that
    -- exact signature has fired with no intervening *different* tool. A
    -- different tool resets the count. `tool_calls_since_message` increments
    -- on each tool_use and resets to 0 on each agent.message.
    tool_loop_signature              TEXT,
    tool_loop_count                  INTEGER NOT NULL DEFAULT 0,
    tool_calls_since_message         INTEGER NOT NULL DEFAULT 0
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
            # Idempotent migration for `managed_cursor.final_text` — added
            # after the table was first introduced. SQLite has no "ADD COLUMN
            # IF NOT EXISTS", so probe via PRAGMA first.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(managed_cursor)")}
            if "final_text" not in cols:
                conn.execute("ALTER TABLE managed_cursor ADD COLUMN final_text TEXT")
            # Idempotent migration for `pending_questions.kind` — distinguishes
            # mid-task clarifications from completion-message follow-ups.
            pq_cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_questions)")}
            if "kind" not in pq_cols:
                conn.execute(
                    "ALTER TABLE pending_questions ADD COLUMN kind TEXT "
                    "NOT NULL DEFAULT 'clarification'"
                )
            # Idempotent migration for `pending_questions.sent_message_ids` —
            # the full chunk-id list so a reply to any chunk of a split
            # notification matches. Legacy rows stay NULL and match on
            # `sent_message_id`.
            if "sent_message_ids" not in pq_cols:
                conn.execute(
                    "ALTER TABLE pending_questions ADD COLUMN sent_message_ids TEXT"
                )
            # Idempotent migration for `pending_questions.bot` (#348) — scopes
            # reply matching to the sending bot. Legacy rows stay NULL = primary.
            if "bot" not in pq_cols:
                conn.execute("ALTER TABLE pending_questions ADD COLUMN bot TEXT")
            # Idempotent migration for the prompt-cache token buckets on
            # `sessions`. Old rows stay at zero — we don't backfill historical
            # sessions, the raw event payloads in transcripts still have the
            # data if anyone ever wants to recompute.
            sess_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "total_cache_creation_tokens" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN total_cache_creation_tokens "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "total_cache_read_tokens" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN total_cache_read_tokens "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # Idempotent migration for the per-session preset_class column
            # (#139 §3 worker wiring). Old rows stay NULL → fullstack.
            if "preset_class" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN preset_class TEXT"
                )
            # Idempotent migration for the per-session origin column (#235).
            # Old rows stay NULL (treated as "agent").
            if "origin" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN origin TEXT")
            # Idempotent migration for the Claude Code CLI session UUID.
            # Set for routing="claude_code" (Claude Code) and "codex" sessions;
            # NULL for everything else.
            if "bot" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN bot TEXT")
            if "claude_code_session_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN claude_code_session_id TEXT")
                # Migrate data from the legacy column if it existed (pre-rename
                # databases). The legacy column is dropped below.
                if "code_session_id" in sess_cols:
                    conn.execute(
                        "UPDATE sessions SET claude_code_session_id = code_session_id "
                        "WHERE claude_code_session_id IS NULL AND code_session_id IS NOT NULL"
                    )
            # Idempotent migration for the per-session Claude Code tier (#349).
            # Old rows stay NULL → the CLI default ("opus").
            if "claude_code_model" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN claude_code_model TEXT")
            # Idempotent migration for the sticky `unpriced` flag (#669). Old
            # rows default to 0 (not retroactively flagged) -- a pre-existing
            # row's cost is what it is; we don't reclassify history.
            if "unpriced" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN unpriced INTEGER NOT NULL DEFAULT 0"
                )
            # Migrate legacy routing tag: 'code' was the pre-rename name for
            # what is now 'claude_code'. Idempotent — only flips rows that
            # still carry the old value.
            conn.execute(
                "UPDATE sessions SET routing = 'claude_code' WHERE routing = 'code'"
            )
            # Drop the legacy code_session_id column after data has been
            # migrated to claude_code_session_id. Idempotent — only fires if
            # the column still exists. Requires SQLite 3.35+ (Linux/macOS
            # builds since 2021 all qualify).
            sess_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "code_session_id" in sess_cols:
                conn.execute("ALTER TABLE sessions DROP COLUMN code_session_id")
            # Idempotent cleanup of the legacy `code_followup` pending_question
            # kind. The retired in-memory ClaudeOrchestrator used these rows
            # to register a Claude Code completion; they're now unresumable.
            # Mark any leftovers processed so the worker's clarification-answer
            # loop doesn't try to drain them and the timeout sweeper doesn't
            # keep nudging the operator.
            conn.execute(
                "UPDATE pending_questions SET processed = 1, timed_out = 1 "
                "WHERE kind = 'code_followup' AND processed = 0"
            )
            # Idempotent migrations for the runaway detection counters on
            # managed_cursor (#139 Section 5).
            mc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(managed_cursor)")}
            if "tool_loop_signature" not in mc_cols:
                conn.execute("ALTER TABLE managed_cursor ADD COLUMN tool_loop_signature TEXT")
            if "tool_loop_count" not in mc_cols:
                conn.execute(
                    "ALTER TABLE managed_cursor ADD COLUMN tool_loop_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "tool_calls_since_message" not in mc_cols:
                conn.execute(
                    "ALTER TABLE managed_cursor ADD COLUMN tool_calls_since_message "
                    "INTEGER NOT NULL DEFAULT 0"
                )

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
        origin: str | None = None,
        claude_code_model: str | None = None,
        bot: str | None = None,
    ) -> Session:
        """Insert a new session row. Raises sqlite3.IntegrityError if `task_id`
        already has a row — the caller should treat that as a lost-race signal.

        For root sessions (no parent), `root_session_id` defaults to the new
        session's own id. Children inherit the parent's `root_session_id` so
        lineage queries can find an entire family with one indexed lookup.

        `origin="operator"` marks a root-spawned session (no #agent task) so
        the worker's spawned-session dispatch claims it (#235).
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
                    root_session_id, spawn_depth, origin, claude_code_model, bot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, sid, status, routing,
                    json.dumps(budget) if budget else None,
                    now, now,
                    expected_output, parent_session_id,
                    root_sid, spawn_depth, origin, claude_code_model, bot,
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
            origin=origin,
            claude_code_model=claude_code_model,
            bot=bot,
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

    def set_claude_code_session_id(self, task_id: str, claude_code_session_id: str) -> None:
        """Persist the CLI's session UUID for a routing='claude_code' or
        routing='codex' session. Called by ClaudeCodeExecutor / CodexExecutor
        as soon as the subprocess emits its init event, so resume after a
        worker restart can pass `-r <uuid>` (or `codex resume <uuid>`).
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET claude_code_session_id = ?, last_activity_at = ?
                WHERE task_id = ?
                """,
                (claude_code_session_id, _now(), task_id),
            )

    def set_routing_and_budget(
        self,
        task_id: str,
        routing: str | None,
        budget: dict | None,
        expected_output: str | None = None,
        preset_class: str | None = None,
    ) -> None:
        """Update routing, budget, expected_output, and preset_class after preflight."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET routing = ?, budget_json = ?, expected_output = ?,
                    preset_class = ?, last_activity_at = ?
                WHERE task_id = ?
                """,
                (
                    routing,
                    json.dumps(budget) if budget else None,
                    expected_output,
                    preset_class,
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
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        unpriced: bool = False,
    ) -> None:
        """Add to a session's cumulative token + dollar counters.

        Cache buckets default to zero so the local executor (which only
        sees plain input/output) doesn't need to update; managed sessions
        always pass all four buckets.

        `unpriced` (#669) marks that `dollars` for this call came from an
        unrecognized model rather than a real rate -- a **record** caller
        (the local executor, Claude Code ingest) should pass `dollars=0.0`
        and `unpriced=True` in that case rather than inventing a number. An
        **estimate** caller (the managed-executor budget-kill check) should
        keep passing a fallback-rate `dollars` and leave `unpriced` False --
        that conservative estimate is intentional, not a bug. Sticky: once
        True for a session, later priced calls don't clear it.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET total_input_tokens          = total_input_tokens          + ?,
                    total_output_tokens         = total_output_tokens         + ?,
                    total_cache_creation_tokens = total_cache_creation_tokens + ?,
                    total_cache_read_tokens     = total_cache_read_tokens     + ?,
                    total_dollars               = total_dollars               + ?,
                    unpriced                    = unpriced OR ?,
                    last_activity_at            = ?
                WHERE task_id = ?
                """,
                (
                    tokens_in,
                    tokens_out,
                    cache_creation_tokens,
                    cache_read_tokens,
                    dollars,
                    int(unpriced),
                    _now(),
                    task_id,
                ),
            )

    def set_managed_session_id(self, task_id: str, managed_id: str) -> None:
        """Attach a remote Managed Agents session_id to a local session."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET managed_agent_session_id = ?, last_activity_at = ? "
                "WHERE task_id = ?",
                (managed_id, _now(), task_id),
            )

    def reset_managed_cursor(self, task_id: str) -> None:
        """Drop any managed-cursor state for a task before a fresh session.

        Without this, deleting a session row and re-claiming the same task_id
        (e.g., operator re-arming a task after manual cleanup) would leak the
        prior session's `last_event_id` into the new session's poll cursor —
        triggering a 400 on the events endpoint because the new session has
        never seen that id.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM managed_cursor WHERE task_id = ?", (task_id,))

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

    def set_managed_final_text(self, task_id: str, final_text: str) -> None:
        """Cache the latest agent.message text seen on this managed session.

        Called from the executor's poll loop whenever the driver returns a
        non-None `final_text`. Because `get_session_state` advances a cursor
        and only returns events since the last call, the terminal poll batch
        may contain only `session.status_idle` with no text content — the
        actual final answer lived in a previous batch. Persisting it here
        guarantees the finalize step always has the agent's last message.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO managed_cursor (task_id, final_text) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET final_text = excluded.final_text",
                (task_id, final_text),
            )

    def get_managed_final_text(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT final_text FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return row["final_text"]

    # ------------------------------------------------------------------
    # Runaway detection state (#139 Section 5)
    # ------------------------------------------------------------------

    def get_runaway_state(self, task_id: str) -> dict:
        """Return the persisted runaway counters for `task_id`.

        Defaults to a clean state (signature=None, both counts 0) when no
        managed_cursor row exists yet. Callers that have never seen events
        for this session can treat the result as a virgin starting point.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tool_loop_signature, tool_loop_count, tool_calls_since_message "
                "FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return {
                "tool_loop_signature": None,
                "tool_loop_count": 0,
                "tool_calls_since_message": 0,
            }
        return {
            "tool_loop_signature": row["tool_loop_signature"],
            "tool_loop_count": int(row["tool_loop_count"] or 0),
            "tool_calls_since_message": int(row["tool_calls_since_message"] or 0),
        }

    def set_runaway_state(
        self,
        task_id: str,
        *,
        tool_loop_signature: str | None,
        tool_loop_count: int,
        tool_calls_since_message: int,
    ) -> None:
        """Persist runaway counters. Upsert into managed_cursor so a fresh
        session (no prior cursor row) gets a row with the counters set."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_cursor (
                    task_id, tool_loop_signature, tool_loop_count,
                    tool_calls_since_message
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    tool_loop_signature      = excluded.tool_loop_signature,
                    tool_loop_count          = excluded.tool_loop_count,
                    tool_calls_since_message = excluded.tool_calls_since_message
                """,
                (task_id, tool_loop_signature, tool_loop_count, tool_calls_since_message),
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

    def clear_messages(self, session_id: str) -> None:
        """Remove all stored messages for a session.

        Used by the local executor when resuming a session that was blocked
        at preflight before being seeded — the worker pre-injected the
        operator's clarification answer, but no system / task message
        exists. The executor clears, re-seeds with system+task, then
        re-appends the answer so the conversation arrives in the right
        order.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,),
            )

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

    # ------------------------------------------------------------------
    # Pending clarification questions (Issue F)
    # ------------------------------------------------------------------

    def create_pending_question(
        self,
        session_id: str,
        task_id: str,
        question: str,
        sent_message_id: int,
        kind: str = "clarification",
        sent_message_ids: list[int] | None = None,
        bot: str | None = None,
    ) -> int:
        """Record a pending question / follow-up keyed by Telegram message id.

        `sent_message_id` is the first (matchable) chunk id; `sent_message_ids`
        is the full chunk list for a split notification (defaults to just the
        first chunk). `deposit_answer` matches a reply to any chunk in the list.
        `bot` is the Telegram bot that sent the message (NULL = primary); reply
        matching is scoped by it so bots can't collide on numeric ids (#348).
        """
        ids = sent_message_ids or [int(sent_message_id)]
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_questions (
                    session_id, task_id, question, sent_message_id, sent_at,
                    kind, sent_message_ids, bot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, task_id, question, int(sent_message_id), _now(),
                    kind, json.dumps([int(i) for i in ids]), bot,
                ),
            )
        return cur.lastrowid

    def add_reply_anchors(
        self,
        session_id: str,
        task_id: str,
        message_ids: list[int],
        bot: str | None = None,
    ) -> None:
        """Register operator-facing message ids as reply anchors for a session.

        Every message a session sends to Telegram (streamed [NOTIFY] bodies,
        heartbeats, acks) registers here so a threaded reply to ANY of them can
        be routed back into the session as a context note. One always-open
        ``kind='status_anchor'`` row per session accumulates the ids in its
        ``sent_message_ids`` JSON list — reusing the pending_questions matching
        machinery without new schema. The row is excluded from deposit_answer,
        the web open-question lookup, and the timeout sweep: it is a routing
        index, not a question.
        """
        if not message_ids:
            return
        ids = [int(i) for i in message_ids]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, sent_message_ids FROM pending_questions "
                "WHERE session_id = ? AND kind = 'status_anchor' LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO pending_questions (
                        session_id, task_id, question, sent_message_id, sent_at,
                        kind, sent_message_ids, bot
                    ) VALUES (?, ?, ?, ?, ?, 'status_anchor', ?, ?)
                    """,
                    (session_id, task_id, "(session reply anchors)", ids[0],
                     _now(), json.dumps(ids), bot),
                )
            else:
                existing = json.loads(row["sent_message_ids"] or "[]")
                merged = existing + [i for i in ids if i not in existing]
                conn.execute(
                    "UPDATE pending_questions SET sent_message_ids = ? WHERE id = ?",
                    (json.dumps(merged), row["id"]),
                )

    def has_pending_messages(self, session_id: str) -> bool:
        """True when undelivered pending messages exist for `session_id`."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM pending_messages WHERE session_id = ? AND delivered = 0 LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def register_completion_followup(
        self,
        session_id: str,
        task_id: str,
        sent_message_ids: list[int],
        label: str = "",
    ) -> int:
        """Register a terminal-notification's Telegram msg_id(s) so an operator
        reply to any chunk reopens the session as a follow-up turn.

        Used for COMPLETED, FAILED, and BUDGET_EXCEEDED notifications — every
        terminal state is replyable. The row goes into `pending_questions` with
        kind='followup'; `label` (the task description) is stored in `question`
        so the resume path can show a `↪ continuing "<task>"` prefix. The worker
        tick branches on `kind` when processing.
        """
        if not sent_message_ids:
            raise ValueError("register_completion_followup requires at least one message id")
        return self.create_pending_question(
            session_id=session_id,
            task_id=task_id,
            question=label,
            sent_message_id=sent_message_ids[0],
            kind="followup",
            sent_message_ids=sent_message_ids,
        )

    @staticmethod
    def _bot_scope_clause(bot: str | None) -> tuple[str, list]:
        """SQL fragment + params that scope a pending_questions lookup to `bot`.

        ``bot=None`` → no scoping (legacy behavior). ``bot="primary"`` matches
        both explicit 'primary' rows and legacy NULL-bot rows; any other name
        matches only its own rows (#348).
        """
        if bot is None:
            return "", []
        return " AND (bot = ? OR (bot IS NULL AND ? = 'primary'))", [bot, bot]

    def deposit_answer(self, sent_message_id: int, answer: str, bot: str | None = None) -> bool:
        """Record an answer for an open question, keyed by Telegram message_id.

        Matches a reply landing on any chunk of a split notification: the id is
        checked against both the primary `sent_message_id` and membership in the
        `sent_message_ids` JSON list. When `bot` is given, the match is scoped to
        that bot (see :meth:`_bot_scope_clause`). Returns True if a matching open
        question was found and updated; False otherwise (so the listener can fall
        through to the chat pipeline).
        """
        bot_clause, bot_params = self._bot_scope_clause(bot)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM pending_questions "
                "WHERE answered_at IS NULL AND timed_out = 0 "
                "AND kind != 'status_anchor' "
                "AND (sent_message_id = ? OR (sent_message_ids IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM json_each(sent_message_ids) WHERE value = ?)))"
                + bot_clause +
                " ORDER BY id ASC LIMIT 1",
                (int(sent_message_id), int(sent_message_id), *bot_params),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE pending_questions "
                "SET answer = ?, answered_at = ? WHERE id = ?",
                (answer, _now(), row["id"]),
            )
        return True

    def get_open_question_by_session_id(self, session_id: str) -> dict | None:
        """Return the open (unanswered, not-timed-out) question for `session_id`,
        or None.

        The session-keyed sibling of `get_open_question_by_message_id`: a
        web/voice surface has no Telegram `sent_message_id` to match a reply
        against, but it does know which agent session its conversation spawned
        (#403). Returns the oldest open question so the caller can inspect its
        `kind` (clarification / goal_approval / followup) before depositing an
        answer onto it.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE session_id = ? AND answered_at IS NULL AND timed_out = 0 "
                "AND kind != 'status_anchor' "
                "ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def deposit_answer_by_session_id(self, session_id: str, answer: str) -> bool:
        """Record an answer for `session_id`'s open question, keyed by session.

        The session-keyed sibling of `deposit_answer` (#403). A web/voice
        surface deposits onto the *existing* open `pending_questions` row for the
        session rather than creating a new one, so the row's `kind` is preserved
        and the worker's existing tick resumes it through the right path
        (`_resume_goal` for goal_approval, `_resume_as_followup` / clarification
        otherwise). Returns True if a matching open question was found and
        updated; False otherwise (no open question, or already answered).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM pending_questions "
                "WHERE session_id = ? AND answered_at IS NULL AND timed_out = 0 "
                "ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE pending_questions "
                "SET answer = ?, answered_at = ? WHERE id = ?",
                (answer, _now(), row["id"]),
            )
        return True

    def delete_session(self, session_id: str) -> None:
        """Hard-delete a session and its queued messages/questions/turns.

        Used to clean up an operator spawn that couldn't be routed (preflight
        returned `ask` but the calling surface has no clarification flow), so it
        doesn't linger as a permanently-blocked thread.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def enqueue_web_followup(self, session_id: str, task_id: str, answer: str) -> int:
        """Queue a follow-up turn from a non-Telegram surface (web /chat, #236).

        Inserts a pre-answered `kind='followup'` row so the worker's
        `_process_clarification_answers` tick picks it up and reopens the
        session via `_resume_as_followup` — the same path a Telegram reply
        takes. There's no Telegram message to match, so `sent_message_id` is a
        sentinel 0 (web follow-ups are created already-answered, so they never
        participate in reply-id matching via `deposit_answer`).
        """
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_questions (
                    session_id, task_id, question, sent_message_id, sent_at,
                    kind, answer, answered_at
                ) VALUES (?, ?, '', 0, ?, 'followup', ?, ?)
                """,
                (session_id, task_id, now, answer, now),
            )
        return cur.lastrowid

    def get_recent_resumable_followup(self, within_seconds: int) -> dict | None:
        """Return the most recent open follow-up whose notification was sent
        within `within_seconds`, or None — a "is there a recently-finished,
        still-open agent thread?" query.
        """
        cutoff = _now() - int(within_seconds)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE kind = 'followup' AND answered_at IS NULL "
                "AND processed = 0 AND timed_out = 0 AND sent_at >= ? "
                "ORDER BY sent_at DESC, id DESC LIMIT 1",
                (cutoff,),
            ).fetchone()
        return dict(row) if row else None

    def get_question_by_message_id(self, sent_message_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions WHERE sent_message_id = ?",
                (int(sent_message_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_open_question(
        self, bot: str | None = None, kind: str | None = None
    ) -> dict | None:
        """The most recent open (unanswered, not-timed-out) question, optionally
        filtered by owning bot and kind. Used to route a bare affirmative sent
        as a plain message to the goal gate it almost certainly answers (#453)
        instead of spawning a context-free session."""
        bot_clause, bot_params = self._bot_scope_clause(bot)
        kind_clause = " AND kind = ?" if kind else ""
        params: list = [*bot_params, *([kind] if kind else [])]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE answered_at IS NULL AND timed_out = 0 "
                "AND kind != 'status_anchor'"
                + bot_clause + kind_clause +
                " ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return dict(row) if row else None

    def get_open_question_by_message_id(
        self, sent_message_id: int, bot: str | None = None,
        include_answered: bool = False,
    ) -> dict | None:
        """Return the open (unanswered, not-timed-out) question a reply to
        `sent_message_id` matches — on any chunk — or None.

        Read-only sibling of `deposit_answer`: lets a caller inspect the matched
        row's `kind` before recording an answer (e.g. so the Telegram listener
        can recognize a ``routing='code'`` follow-up). When `bot` is given, the
        match is scoped to that bot (see :meth:`_bot_scope_clause`). With
        ``include_answered=True`` the answered_at filter is dropped, so a
        caller can recognize a reply landing on an ALREADY-answered question
        (check ``answered_at`` on the returned row) instead of treating it as
        unrelated.
        """
        bot_clause, bot_params = self._bot_scope_clause(bot)
        answered_clause = "" if include_answered else "answered_at IS NULL AND "
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE " + answered_clause + "timed_out = 0 "
                "AND (sent_message_id = ? OR (sent_message_ids IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM json_each(sent_message_ids) WHERE value = ?)))"
                + bot_clause +
                " ORDER BY id ASC LIMIT 1",
                (int(sent_message_id), int(sent_message_id), *bot_params),
            ).fetchone()
        return dict(row) if row else None

    def list_answered_unprocessed_questions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE answered_at IS NOT NULL AND processed = 0",
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_question_processed(self, question_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_questions SET processed = 1 WHERE id = ?",
                (int(question_id),),
            )

    def list_timed_out_questions(self, before_ts: int) -> list[dict]:
        """Open questions sent before `before_ts` that haven't been answered
        or already nudged."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE answered_at IS NULL AND timed_out = 0 AND sent_at < ? "
                "AND kind != 'status_anchor'",
                (int(before_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_question_timed_out(self, question_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_questions SET timed_out = 1 WHERE id = ?",
                (int(question_id),),
            )

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
            total_cache_creation_tokens=(
                row["total_cache_creation_tokens"]
                if "total_cache_creation_tokens" in row.keys()
                else 0
            ),
            total_cache_read_tokens=(
                row["total_cache_read_tokens"]
                if "total_cache_read_tokens" in row.keys()
                else 0
            ),
            total_dollars=row["total_dollars"],
            total_active_seconds=(
                row["total_active_seconds"]
                if "total_active_seconds" in row.keys()
                else 0.0
            ),
            expected_output=row["expected_output"],
            parent_session_id=row["parent_session_id"],
            managed_agent_session_id=row["managed_agent_session_id"],
            preset_class=(
                row["preset_class"]
                if "preset_class" in row.keys()
                else None
            ),
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
            origin=(row["origin"] if "origin" in row.keys() else None),
            claude_code_session_id=(
                row["claude_code_session_id"] if "claude_code_session_id" in row.keys() else None
            ),
            claude_code_model=(
                row["claude_code_model"] if "claude_code_model" in row.keys() else None
            ),
            bot=(row["bot"] if "bot" in row.keys() else None),
            unpriced=(bool(row["unpriced"]) if "unpriced" in row.keys() else False),
        )
