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
from typing import Iterable


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

# Shared lifecycle vocabulary.  These are intentionally internal storage
# values: the public task status/checkbox vocabulary remains unchanged.
WAIT_OPERATOR = "operator"
WAIT_PROVIDER = "provider"
WAIT_DEPENDENCY = "dependency"
WAIT_TYPES = frozenset({WAIT_OPERATOR, WAIT_PROVIDER, WAIT_DEPENDENCY})

OCCURRENCE_PENDING = "pending"
OCCURRENCE_CLAIMED = "claimed"
OCCURRENCE_DISPATCHED = "dispatched"
OCCURRENCE_FAILED = "failed"
OCCURRENCE_STATES = frozenset({
    OCCURRENCE_PENDING, OCCURRENCE_CLAIMED, OCCURRENCE_DISPATCHED,
    OCCURRENCE_FAILED,
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
    # the worker can choose a tier explicitly. NULL omits --model and leaves
    # the configured/native Claude CLI default authoritative.
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
    # (#851) Board-assignment fields. `host` is a name from
    # `settings.agent_hosts` ("" / None = the API host); `model` and
    # `effort` are the board's own picker values, threaded into the
    # executor's argv at spawn time. `conversation_id` is set only for
    # routing="hermes" sessions (the Hermes conversation this card opened).
    # `remote_pgid` is the process-group id a remote-spawned subprocess
    # echoed back on its first stdout line — used by the operator kill
    # endpoint to reach it over ssh (see `remote_spawn.py`).
    host: str | None = None
    model: str | None = None
    effort: str | None = None
    conversation_id: str | None = None
    remote_pgid: int | None = None
    # The model Hermes ITSELF reported for its most recent turn —
    # observed from the turn's own `usage` event (`_HermesTurnPersister.
    # reported_model`), not declared. Explicitly distinct from `model`
    # above: `model` is the board's operator-chosen picker value, which
    # `HermesExecutor` never reads or writes. Written only by
    # `HermesExecutor.execute` for the session it is executing (see
    # `set_hermes_model`), so a completed session's value can never be
    # rewritten by a later, unrelated turn — unlike the process-wide "last
    # observed" value `model_readout.py` keeps for `/api/health`, which any
    # Hermes turn on any surface can overwrite (see `agents.py`'s
    # `_model_label_for_routing`, which reads this column instead for a
    # per-session badge).
    hermes_model: str | None = None
    # Canonical execution inputs and the immutable pre-dispatch snapshot.
    # The snapshot is reused on retry/resume rather than re-reading defaults.
    execution_request: dict | None = None
    execution_spec: dict | None = None
    # Lifecycle identity.  A session may be deliberately reopened for a new
    # attempt; attempts and executor turns retain their own immutable ids.
    attempt_id: str | None = None
    attempt_number: int = 0
    turn_id: str | None = None
    turn_number: int = 0
    persona_id: str | None = None


@dataclass(frozen=True)
class Occurrence:
    """Durable schedule-fire identity and handoff claim state."""

    occurrence_key: str
    schedule_id: str
    scheduled_for: str
    state: str = OCCURRENCE_PENDING
    task_id: str | None = None
    session_id: str | None = None
    lease_generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    created_at: int = 0
    updated_at: int = 0


# Engine -> storage id prefix for the `cli_sessions` table (#849). Matches
# the `cc:` / `cx:` convention `CCWezTermStore` and the transcript-scan
# snapshot already use, so a cli_sessions row and its transcript-derived
# counterpart share one id in the /agents snapshot union.
CLI_ENGINE_PREFIXES = {"claude_code": "cc", "codex": "cx"}

# Lifecycle events the hook script posts. Anything else is a caller bug —
# the route rejects it with 422 rather than silently no-op'ing.
CLI_SESSION_EVENTS = frozenset({
    "session_start", "user_prompt_submit", "stop", "session_end",
})

CLI_STATUS_IDLE = "idle"
CLI_STATUS_RUNNING = "running"
CLI_STATUS_ENDED = "ended"

# Prompt previews are truncated to this many characters before storage
# (issue #849 acceptance criteria).
CLI_PROMPT_PREVIEW_MAX = 200


@dataclass
class CliSession:
    """Mirrors one row in the `cli_sessions` table — a Claude Code or Codex
    CLI session registered by `scripts/lifeos-agent-hook.sh`, from any
    machine on the tailnet. Status is event-driven (set by the hook's
    lifecycle posts), unlike the local transcript scan's file-age guess.
    """

    session_id: str  # cc:<uuid> or cx:<uuid> — shares the snapshot id space
    engine: str  # "claude_code" | "codex"
    host: str
    status: str  # idle | running | ended
    started_at: int
    last_event_at: int
    cwd: str | None = None
    transcript_path: str | None = None
    branch: str | None = None
    model: str | None = None
    prompt_preview: str | None = None
    task_id: str | None = None
    pane_id: int | None = None
    wezterm_pid: int | None = None
    ended_at: int | None = None


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
    claude_code_model         TEXT,  -- Claude tier for routing="claude_code"; NULL = omit --model
    bot                       TEXT,  -- Telegram bot that owns this session's notices; NULL = primary (#348)
    unpriced                  INTEGER NOT NULL DEFAULT 0,  -- sticky: any record_spend call priced an unknown model (#669)
    host                      TEXT,  -- board-assigned host name (#851); NULL/"" = the API host
    model                     TEXT,  -- board-assigned model id (#851), passed to the executor's --model flag
    effort                    TEXT,  -- board-assigned effort level (#851): low|medium|high|max
    conversation_id           TEXT,  -- Hermes conversation id (#851, routing='hermes' only)
    remote_pgid               INTEGER,  -- process-group id echoed by a remote-spawned subprocess (#851)
    hermes_model              TEXT,  -- model Hermes itself reported for this session's own turn (#892); NULL = no turn yet
    execution_request_json    TEXT,
    execution_spec_json       TEXT,
    attempt_id                TEXT,
    attempt_number             INTEGER NOT NULL DEFAULT 0,
    turn_id                    TEXT,
    turn_number                INTEGER NOT NULL DEFAULT 0,
    persona_id                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_yield ON sessions(status) WHERE status = 'yielded';

-- Immutable execution-attempt and executor-turn history.  The current ids
-- are mirrored on sessions for cheap reads; these tables retain prior retry
-- and resume identities for idempotent usage transport.
CREATE TABLE IF NOT EXISTS execution_attempts (
    session_id     TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (session_id, attempt_id),
    UNIQUE (session_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS execution_turns (
    session_id     TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    turn_id        TEXT NOT NULL,
    turn_number    INTEGER NOT NULL,
    operation      TEXT NOT NULL,
    started_at     INTEGER NOT NULL,
    PRIMARY KEY (session_id, attempt_id, turn_id),
    UNIQUE (session_id, attempt_id, turn_number)
);
CREATE INDEX IF NOT EXISTS idx_execution_turns_attempt
    ON execution_turns(session_id, attempt_id, turn_number);

-- Scheduler fires are separate from schedule definition/history.  The
-- occurrence key is deterministic and is the recovery/idempotency boundary
-- between Markdown and SQLite.
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    occurrence_key    TEXT PRIMARY KEY,
    schedule_id       TEXT NOT NULL,
    scheduled_for     TEXT NOT NULL,
    state             TEXT NOT NULL,
    task_id           TEXT,
    session_id        TEXT,
    lease_generation  INTEGER NOT NULL DEFAULT 0,
    lease_owner       TEXT,
    lease_expires_at  INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedule_occurrences_schedule
    ON schedule_occurrences(schedule_id, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_schedule_occurrences_recovery
    ON schedule_occurrences(state, lease_expires_at);

-- Machine/operator/dependency waits and explicit human-card links.  A wait is
-- tied to an immutable execution attempt, so a stale resolution cannot wake a
-- replacement attempt.
CREATE TABLE IF NOT EXISTS lifecycle_waits (
    wait_id          TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    attempt_id       TEXT NOT NULL,
    wait_type        TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    card_id          TEXT,
    dependencies_json TEXT,
    state            TEXT NOT NULL DEFAULT 'open',
    created_at       INTEGER NOT NULL,
    resolved_at      INTEGER,
    wake_enqueued    INTEGER NOT NULL DEFAULT 0,
    wake_consumed    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, attempt_id, wait_type)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_waits_card
    ON lifecycle_waits(card_id, state);
CREATE INDEX IF NOT EXISTS idx_lifecycle_waits_ready
    ON lifecycle_waits(state, wait_type, wake_enqueued);

-- Cross-store projection marker.  The row is inserted before a Markdown
-- update and acknowledged only after the TaskManager write succeeds.
CREATE TABLE IF NOT EXISTS lifecycle_projections (
    event_id          TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    session_id        TEXT,
    attempt_id        TEXT,
    expected_version  TEXT,
    target_status     TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    error             TEXT,
    created_at        INTEGER NOT NULL,
    applied_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_projections_pending
    ON lifecycle_projections(state, created_at);

-- Exact cancellation fences.  A FAILED status alone is not enough to tell a
-- cancellation apart from an ordinary failure (the latter may race a clean
-- executor return and retain the historical success behavior).  The guard is
-- scoped to the immutable attempt/turn identity so a reopened execution is
-- never affected by an old cancellation.
CREATE TABLE IF NOT EXISTS cancellation_guards (
    session_id TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    turn_id    TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, attempt_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_cancellation_guards_task
    ON cancellation_guards(task_id, attempt_id, turn_id);

CREATE TABLE IF NOT EXISTS execution_overrides (
    scope       TEXT NOT NULL CHECK(scope IN ('session', 'lineage')),
    scope_id    TEXT NOT NULL,
    override_json TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id)
);

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
    bot               TEXT,
    attempt_id        TEXT,
    turn_id           TEXT
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
    tool_calls_since_message         INTEGER NOT NULL DEFAULT 0,
    -- Provider totals are cumulative across Managed turns. Keep the
    -- provider/session high-water mark and the current turn baseline so a
    -- resumed turn books only its own delta.
    usage_snapshot_json              TEXT
);

-- Cross-machine Claude Code / Codex CLI sessions (#849). One row per
-- session, registered by scripts/lifeos-agent-hook.sh from any host on the
-- tailnet via POST /api/agents/cli-sessions/events. `session_id` is the
-- cc:/cx:-prefixed id (see CLI_ENGINE_PREFIXES) so it shares the id space
-- the /agents snapshot union keys transcript-derived rows on.
CREATE TABLE IF NOT EXISTS cli_sessions (
    session_id       TEXT PRIMARY KEY,
    engine           TEXT NOT NULL,
    host             TEXT NOT NULL,
    cwd              TEXT,
    transcript_path  TEXT,
    branch           TEXT,
    model            TEXT,
    status           TEXT NOT NULL,
    prompt_preview   TEXT,
    task_id          TEXT,
    pane_id          INTEGER,
    wezterm_pid      INTEGER,
    started_at       INTEGER NOT NULL,
    last_event_at    INTEGER NOT NULL,
    ended_at         INTEGER
);
"""


def _now() -> int:
    return int(time.time())


def new_session_id() -> str:
    """Generate an internal session_id. Independent of any platform id."""
    return f"sess_{uuid.uuid4().hex[:16]}"


def new_attempt_id() -> str:
    return f"attempt_{uuid.uuid4().hex[:20]}"


def new_turn_id() -> str:
    return f"turn_{uuid.uuid4().hex[:20]}"


class SessionStore:
    """Thin SQLite wrapper. Each method opens a short-lived connection so the
    store is safe to use from multiple threads or processes — SQLite's own
    locking serializes writers."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._status_projector = None
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
            if "attempt_id" not in pq_cols:
                conn.execute("ALTER TABLE pending_questions ADD COLUMN attempt_id TEXT")
            if "turn_id" not in pq_cols:
                conn.execute("ALTER TABLE pending_questions ADD COLUMN turn_id TEXT")
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
            # Old rows stay NULL → omit --model and use the CLI default.
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
            if "usage_snapshot_json" not in mc_cols:
                conn.execute("ALTER TABLE managed_cursor ADD COLUMN usage_snapshot_json TEXT")
            # Idempotent migration block for card assignment (#851): host,
            # model, effort, conversation_id, remote_pgid. Old rows stay
            # NULL — "no assignment recorded" for a pre-#851 session.
            sess_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "host" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN host TEXT")
            if "model" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
            if "effort" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN effort TEXT")
            if "conversation_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN conversation_id TEXT")
            if "remote_pgid" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN remote_pgid INTEGER")
            # Idempotent migration for the per-session Hermes-reported model.
            # Old rows stay NULL — "no turn observed yet" for a session
            # created without this column, same as a genuinely turn-less
            # one.
            if "hermes_model" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN hermes_model TEXT")
            if "execution_request_json" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN execution_request_json TEXT")
            if "execution_spec_json" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN execution_spec_json TEXT")
            # Lifecycle identity is additive and nullable for legacy rows.
            # ``ensure_attempt`` backfills an id on first dispatch, while
            # keeping old sessions readable before they run again.
            if "attempt_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN attempt_id TEXT")
            if "attempt_number" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 0"
                )
            if "turn_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN turn_id TEXT")
            if "turn_number" not in sess_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN turn_number INTEGER NOT NULL DEFAULT 0"
                )
            if "persona_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN persona_id TEXT")
            wait_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_waits)")}
            if "wake_consumed" not in wait_cols:
                conn.execute(
                    "ALTER TABLE lifecycle_waits ADD COLUMN wake_consumed INTEGER NOT NULL DEFAULT 0"
                )
            # Older databases may have been opened before the lifecycle table
            # was included in _SCHEMA; create it idempotently here as well.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    session_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (session_id, attempt_id),
                    UNIQUE (session_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS execution_turns (
                    session_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    PRIMARY KEY (session_id, attempt_id, turn_id),
                    UNIQUE (session_id, attempt_id, turn_number)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_turns_attempt
                    ON execution_turns(session_id, attempt_id, turn_number);
                """
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
        host: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        execution_request: dict | None = None,
        execution_spec: dict | None = None,
        persona_id: str | None = None,
    ) -> Session:
        """Insert a new session row. Raises sqlite3.IntegrityError if `task_id`
        already has a row — the caller should treat that as a lost-race signal.

        For root sessions (no parent), `root_session_id` defaults to the new
        session's own id. Children inherit the parent's `root_session_id` so
        lineage queries can find an entire family with one indexed lookup.

        `origin="operator"` marks a root-spawned session (no #agent task) so
        the worker's spawned-session dispatch claims it (#235).

        `host`/`model`/`effort` (#851) are the board-assignment fields
        extracted from the task's `[key:: value]` fields — see
        `assignment.extract_assignment()`. All three default to None
        ("no assignment" — the routing/executor default applies).
        """
        sid = session_id or new_session_id()
        attempt_id = new_attempt_id()
        root_sid = root_session_id or sid
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    task_id, session_id, status, routing, budget_json,
                    started_at, last_activity_at,
                    expected_output, parent_session_id,
                    root_session_id, spawn_depth, origin, claude_code_model, bot,
                    host, model, effort, execution_request_json, execution_spec_json,
                    attempt_id, attempt_number, turn_id, turn_number, persona_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, sid, status, routing,
                    json.dumps(budget) if budget else None,
                    now, now,
                    expected_output, parent_session_id,
                    root_sid, spawn_depth, origin, claude_code_model, bot,
                    host, model, effort,
                    json.dumps(execution_request) if execution_request is not None else None,
                    json.dumps(execution_spec) if execution_spec is not None else None,
                    attempt_id, 1, None, 0, persona_id,
                ),
            )
            conn.execute(
                "INSERT INTO execution_attempts(session_id, attempt_id, attempt_number, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, attempt_id, 1, now),
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
            host=host,
            model=model,
            effort=effort,
            execution_request=execution_request,
            execution_spec=execution_spec,
            attempt_id=attempt_id,
            attempt_number=1,
            turn_id=None,
            turn_number=0,
            persona_id=persona_id,
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

    # ------------------------------------------------------------------
    # Schedule occurrence ledger
    # ------------------------------------------------------------------

    @staticmethod
    def occurrence_key_for(schedule_id: str, scheduled_for: str) -> str:
        """Return the stable key for one scheduled fire."""
        if not schedule_id or not scheduled_for:
            raise ValueError("schedule_id and scheduled_for are required")
        return f"{schedule_id}:{scheduled_for}"

    def ensure_occurrence(
        self, schedule_id: str, scheduled_for: str,
        *, occurrence_key: str | None = None,
    ) -> Occurrence:
        key = occurrence_key or self.occurrence_key_for(schedule_id, scheduled_for)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schedule_occurrences "
                "(occurrence_key, schedule_id, scheduled_for, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, schedule_id, scheduled_for, OCCURRENCE_PENDING, now, now),
            )
            row = conn.execute(
                "SELECT * FROM schedule_occurrences WHERE occurrence_key = ?", (key,)
            ).fetchone()
        return self._row_to_occurrence(row)

    def get_occurrence(self, occurrence_key: str) -> Occurrence | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM schedule_occurrences WHERE occurrence_key = ?",
                (occurrence_key,),
            ).fetchone()
        return self._row_to_occurrence(row) if row else None

    def claim_occurrence(
        self, occurrence_key: str, owner: str, *, lease_seconds: int = 120,
    ) -> Occurrence | None:
        """Atomically claim a pending/expired occurrence.

        A loser receives ``None``. Returning an existing claimed row made it
        impossible for callers to distinguish a winner from a concurrent
        loser, allowing both schedulers to create a task before either linked
        it.
        """
        if not owner:
            raise ValueError("occurrence claim owner is required")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM schedule_occurrences WHERE occurrence_key = ?",
                (occurrence_key,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            claimable = row["state"] == OCCURRENCE_PENDING or (
                row["state"] == OCCURRENCE_CLAIMED
                and (row["lease_expires_at"] or 0) <= now
            )
            if claimable:
                generation = int(row["lease_generation"] or 0) + 1
                conn.execute(
                    "UPDATE schedule_occurrences SET state = ?, lease_generation = ?, "
                    "lease_owner = ?, lease_expires_at = ?, updated_at = ? "
                    "WHERE occurrence_key = ?",
                    (OCCURRENCE_CLAIMED, generation, owner,
                     now + max(1, int(lease_seconds)), now, occurrence_key),
                )
                row = conn.execute(
                    "SELECT * FROM schedule_occurrences WHERE occurrence_key = ?",
                    (occurrence_key,),
                ).fetchone()
            else:
                row = None
            conn.commit()
        return self._row_to_occurrence(row) if row is not None else None

    def renew_occurrence(
        self, occurrence_key: str, *, owner: str, lease_generation: int,
        lease_seconds: int = 120,
    ) -> bool:
        """Extend one claim without allowing an older generation to revive it."""
        if not owner or lease_generation <= 0:
            return False
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE schedule_occurrences SET lease_expires_at = ?, updated_at = ? "
                "WHERE occurrence_key = ? AND state = ? AND lease_owner = ? "
                "AND lease_generation = ? AND lease_expires_at > ?",
                (now + max(1, int(lease_seconds)), now, occurrence_key,
                 OCCURRENCE_CLAIMED, owner, int(lease_generation), now),
            )
        return cur.rowcount == 1

    def link_occurrence(
        self, occurrence_key: str, *, task_id: str, session_id: str | None = None,
        owner: str | None = None, lease_generation: int | None = None,
        state: str = OCCURRENCE_DISPATCHED,
    ) -> bool:
        if state not in OCCURRENCE_STATES:
            raise ValueError(f"invalid occurrence state: {state}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            where = "occurrence_key = ?"
            params: list[object] = [occurrence_key]
            if owner is not None:
                where += " AND lease_owner = ?"
                params.append(owner)
            if lease_generation is not None:
                where += " AND lease_generation = ? AND lease_expires_at > ?"
                params.extend((int(lease_generation), _now()))
            cur = conn.execute(
                f"UPDATE schedule_occurrences SET task_id = COALESCE(task_id, ?), "
                f"session_id = COALESCE(session_id, ?), state = ?, lease_owner = NULL, "
                f"lease_expires_at = NULL, updated_at = ? WHERE {where}",
                (task_id, session_id, state, _now(), *params),
            )
            conn.commit()
        return cur.rowcount == 1

    def link_occurrence_session(self, task_id: str, session_id: str) -> bool:
        """Attach the real worker session identity after task claim."""
        if not task_id or not session_id:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE schedule_occurrences SET session_id = COALESCE(session_id, ?), "
                "updated_at = ? WHERE task_id = ?",
                (session_id, _now(), task_id),
            )
        return cur.rowcount == 1

    def list_recoverable_occurrences(self) -> list[Occurrence]:
        now = _now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_occurrences WHERE state IN (?, ?) "
                "AND (state = ? OR lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at ASC",
                (OCCURRENCE_PENDING, OCCURRENCE_CLAIMED, OCCURRENCE_PENDING, now),
            ).fetchall()
        return [self._row_to_occurrence(row) for row in rows]

    @staticmethod
    def _row_to_occurrence(row: sqlite3.Row) -> Occurrence:
        return Occurrence(
            occurrence_key=row["occurrence_key"], schedule_id=row["schedule_id"],
            scheduled_for=row["scheduled_for"], state=row["state"],
            task_id=row["task_id"], session_id=row["session_id"],
            lease_generation=int(row["lease_generation"] or 0),
            lease_owner=row["lease_owner"], lease_expires_at=row["lease_expires_at"],
            created_at=int(row["created_at"] or 0), updated_at=int(row["updated_at"] or 0),
        )

    # ------------------------------------------------------------------
    # Lifecycle projection markers and typed waits
    # ------------------------------------------------------------------

    def begin_projection(self, event_id: str, *, task_id: str, session_id: str | None,
                         attempt_id: str | None, expected_version: str | None,
                         target_status: str, payload: dict) -> bool:
        """Record a projection before touching Markdown.

        Returns False for an already-applied event, making repeated executor
        callbacks harmless. Pending/conflicted rows return True for retry.
        """
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM lifecycle_projections WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO lifecycle_projections "
                    "(event_id, task_id, session_id, attempt_id, expected_version, "
                    "target_status, payload_json, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (event_id, task_id, session_id, attempt_id, expected_version,
                     target_status, json.dumps(payload), now),
                )
                conn.commit()
                return True
            conn.commit()
            return row["state"] != "applied"

    def acknowledge_projection(self, event_id: str, *, error: str | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lifecycle_projections SET state = ?, error = ?, applied_at = ? "
                "WHERE event_id = ? AND state != 'applied'",
                ("applied" if error is None else "conflicted", error,
                 _now() if error is None else None, event_id),
            )
        return cur.rowcount == 1

    def list_pending_projections(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lifecycle_projections WHERE state != 'applied' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def projection_applied(self, event_id: str) -> bool:
        """Return whether a lifecycle marker crossed its durable CAS boundary."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM lifecycle_projections WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return bool(row and row["state"] == "applied")

    def record_wait(
        self, *, task_id: str, session_id: str, attempt_id: str,
        wait_type: str, reason: str = "", card_id: str | None = None,
        dependencies: list[str] | None = None, wait_id: str | None = None,
    ) -> str:
        if wait_type not in WAIT_TYPES:
            raise ValueError(f"invalid wait type: {wait_type}")
        wait_id = wait_id or uuid.uuid4().hex
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lifecycle_waits "
                "(wait_id, task_id, session_id, attempt_id, wait_type, reason, card_id, dependencies_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, attempt_id, wait_type) DO UPDATE SET "
                "reason=excluded.reason, card_id=COALESCE(excluded.card_id, lifecycle_waits.card_id), "
                "dependencies_json=excluded.dependencies_json",
                (wait_id, task_id, session_id, attempt_id, wait_type, reason, card_id,
                 json.dumps(dependencies or []), now),
            )
        return wait_id

    def list_open_waits(self, *, card_id: str | None = None,
                        session_id: str | None = None) -> list[dict]:
        clauses = ["state = 'open'"]
        params: list[object] = []
        if card_id is not None:
            clauses.append("card_id = ?")
            params.append(card_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lifecycle_waits WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at", tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue_wait_wakeup(self, wait_id: str) -> bool:
        """Durably enqueue one resolved operator wait for worker replay.

        The queue marker and message are committed together. A crash after
        this method returns leaves both the message and the unconsumed wait,
        allowing startup recovery to finish the board/status projection.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            wait = conn.execute(
                "SELECT * FROM lifecycle_waits WHERE wait_id = ?", (wait_id,)
            ).fetchone()
            if wait is None or wait["state"] != "resolved" or wait["wait_type"] != WAIT_OPERATOR:
                conn.rollback()
                return False
            if wait["wake_enqueued"]:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO pending_messages(session_id, sender_id, content, created_at) "
                "VALUES (?, 'human_queue', ?, ?)",
                (wait["session_id"], f"Human queue wakeup:{wait_id}", _now()),
            )
            conn.execute(
                "UPDATE lifecycle_waits SET wake_enqueued = 1 WHERE wait_id = ?",
                (wait_id,),
            )
            conn.commit()
            return True

    def mark_wait_resolved(self, wait_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lifecycle_waits SET state = 'resolved', resolved_at = ? "
                "WHERE wait_id = ? AND state = 'open'", (_now(), wait_id),
            )
        return cur.rowcount == 1

    def claim_wait_wakeup(self, wait_id: str) -> bool:
        """At-most-once wakeup claim; duplicate card resolution is a no-op."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lifecycle_waits SET wake_enqueued = 1 WHERE wait_id = ? "
                "AND state = 'resolved' AND wake_enqueued = 0", (wait_id,),
            )
        return cur.rowcount == 1

    def dependencies_satisfied(self, dependencies: list[str] | None) -> bool:
        """Return whether all explicit wait/card dependencies are resolved."""
        deps = [str(value) for value in (dependencies or []) if value]
        if not deps:
            return True
        with self._connect() as conn:
            for dependency in deps:
                row = conn.execute(
                    "SELECT 1 FROM lifecycle_waits WHERE state = 'open' AND "
                    "(wait_id = ? OR card_id = ?) LIMIT 1",
                    (dependency, dependency),
                ).fetchone()
                if row is not None:
                    return False
        return True

    def list_resolved_waits(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lifecycle_waits WHERE state = 'resolved' "
                "AND wake_consumed = 0 ORDER BY resolved_at, created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_wait_wakeup(self, wait_id: str) -> bool:
        """Acknowledge a wake only after task and session projection succeeds."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lifecycle_waits SET wake_consumed = 1 "
                "WHERE wait_id = ? AND state = 'resolved' AND wake_enqueued = 1",
                (wait_id,),
            )
        return cur.rowcount == 1

    def discard_wait_wakeup(self, wait_id: str) -> bool:
        """Consume a stale or non-dispatchable wait without creating a wake message."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lifecycle_waits SET wake_enqueued = 1, wake_consumed = 1 "
                "WHERE wait_id = ? AND state = 'resolved' AND wake_consumed = 0",
                (wait_id,),
            )
        return cur.rowcount == 1

    def begin_executor_turn(
        self, task_id: str, operation: str, *, session: Session | None = None,
    ) -> Session:
        """Atomically persist the next executor turn and return its snapshot.

        This is called immediately before an engine side effect.  A resume is
        a new turn in the same attempt; ``begin_new_execution`` creates a new
        attempt when the operator deliberately reopens a terminal session.
        """
        if not operation:
            raise ValueError("executor turn operation is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                if session is not None and (
                    row["attempt_id"] != getattr(session, "attempt_id", None)
                    or row["turn_id"] != getattr(session, "turn_id", None)
                ):
                    raise RuntimeError("stale session attempt/turn")
                if row["status"] in TERMINAL_STATUSES:
                    raise RuntimeError("terminal session cannot begin executor turn")
                sid = row["session_id"]
                attempt_id = row["attempt_id"] or new_attempt_id()
                attempt_number = int(row["attempt_number"] or 0)
                if attempt_number <= 0:
                    attempt_number = conn.execute(
                        "SELECT COALESCE(MAX(attempt_number), 0) FROM execution_attempts "
                        "WHERE session_id = ?", (sid,)
                    ).fetchone()[0] + 1
                conn.execute(
                    "INSERT OR IGNORE INTO execution_attempts "
                    "(session_id, attempt_id, attempt_number, created_at) VALUES (?, ?, ?, ?)",
                    (sid, attempt_id, attempt_number, _now()),
                )
                turn_number = conn.execute(
                    "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM execution_turns "
                    "WHERE session_id = ? AND attempt_id = ?",
                    (sid, attempt_id),
                ).fetchone()[0]
                turn_id = new_turn_id()
                now = _now()
                conn.execute(
                    "UPDATE sessions SET attempt_id = ?, attempt_number = ?, "
                    "turn_id = ?, turn_number = ?, last_activity_at = ? WHERE task_id = ?",
                    (attempt_id, attempt_number, turn_id, turn_number, now, task_id),
                )
                conn.execute(
                    "INSERT INTO execution_turns "
                    "(session_id, attempt_id, turn_id, turn_number, operation, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, attempt_id, turn_id, turn_number, operation, now),
                )
                row = conn.execute(
                    "SELECT * FROM sessions WHERE task_id = ?", (task_id,)
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = self._row_to_session(row)
        # Callers occasionally carry an intentionally enriched in-memory
        # snapshot (for example a synthetic host assignment in tests) that
        # has not yet been persisted. Preserve that context while the
        # lifecycle ids always come from the atomic row update above.
        if session is not None:
            for name in (
                "routing", "budget", "expected_output", "parent_session_id",
                "managed_agent_session_id", "preset_class", "root_session_id",
                "spawn_depth", "yield_waiting_for", "origin",
                "claude_code_session_id", "claude_code_model", "bot", "host",
                "model", "effort", "conversation_id", "remote_pgid",
                "hermes_model", "execution_request", "execution_spec",
                "persona_id",
            ):
                value = getattr(session, name, None)
                if value is not None:
                    setattr(result, name, value)
        return result

    def mark_executor_turn_running(
        self, task_id: str, attempt_id: str, turn_id: str,
    ) -> bool:
        """Atomically start one allocated executor turn.

        Turn allocation and the engine side effect are deliberately separate:
        cancellation can arrive in between them.  Only a still-live turn may
        cross into ``running``; a failed/cancelled attempt therefore cannot be
        resurrected by a late executor callback.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET status = ?, last_activity_at = ? "
                "WHERE task_id = ? AND attempt_id = ? AND turn_id = ? "
                "AND status IN (?, ?, ?)",
                (
                    STATUS_RUNNING, _now(), task_id, attempt_id, turn_id,
                    STATUS_CLAIMED, STATUS_RUNNING, STATUS_YIELDED,
                ),
            )
        return cur.rowcount == 1

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        attempt_id: str | None = None,
        turn_id: str | None = None,
        wait_type: str | None = None,
        wait_reason: str = "",
    ) -> bool:
        """Persist status, optionally only for the current execution turn.

        Executor callbacks can finish after a newer attempt has started.  A
        task-id-only update from that old callback must not overwrite the new
        attempt's state, so identity-bearing callers get an atomic compare.
        The bool return is intentionally additive; legacy callers can ignore
        it while new lifecycle code can distinguish an ignored late write.
        """
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        # A cancellation is a terminal decision for this exact execution.
        # Keep FAILED writes idempotent (the cancellation path itself writes
        # FAILED), while every other status must observe the durable fence.
        # Without this guard, a late inter-agent YIELDED/BLOCKED write could
        # resurrect a turn that cancellation already fenced as FAILED.
        if status != STATUS_FAILED:
            where.append(
                "NOT EXISTS ("
                "SELECT 1 FROM cancellation_guards g "
                "WHERE g.session_id = sessions.session_id "
                "AND g.attempt_id = sessions.attempt_id "
                "AND g.turn_id = COALESCE(sessions.turn_id, '')"
                ")"
            )
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET status = ?, last_activity_at = ? "
                f"WHERE {' AND '.join(where)}",
                (status, _now(), *params),
            )
        applied = cur.rowcount == 1
        projector = self._status_projector
        if applied and projector is not None and status in TERMINAL_STATUSES | {STATUS_BLOCKED}:
            projector(
                task_id, status, attempt_id=attempt_id, turn_id=turn_id,
                wait_type=wait_type, wait_reason=wait_reason,
            )
        return applied

    def set_status_projector(self, callback) -> None:
        """Install the process-local callback for worker status projection."""
        self._status_projector = callback

    def mark_cancelled(
        self,
        task_id: str,
        *,
        attempt_id: str,
        turn_id: str | None = None,
        reason: str = "",
    ) -> bool:
        """Atomically fence one exact execution turn as cancelled.

        Cancellation is persisted separately from ``FAILED`` because an
        ordinary failure may legitimately race a clean executor return.  A
        caller must provide the immutable attempt id; ``turn_id`` is optional
        only for a queued pre-turn cancellation.  The status flip and guard
        insert share one transaction, so a late executor can never observe
        one without the other.
        """
        if not attempt_id:
            return False
        turn_key = turn_id or ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id, attempt_id, turn_id, status "
                    "FROM sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["attempt_id"] != attempt_id:
                    conn.rollback()
                    return False
                if turn_id is None and row["turn_id"] is not None:
                    conn.rollback()
                    return False
                if turn_id is not None and row["turn_id"] != turn_id:
                    conn.rollback()
                    return False
                conn.execute(
                    "INSERT OR IGNORE INTO cancellation_guards "
                    "(session_id, task_id, attempt_id, turn_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row["session_id"], task_id, attempt_id, turn_key, reason, _now()),
                )
                conn.execute(
                    "UPDATE sessions SET status = ?, last_activity_at = ? "
                    "WHERE task_id = ? AND attempt_id = ? "
                    "AND ((? IS NULL AND turn_id IS NULL) OR turn_id = ?) "
                    "AND status NOT IN (?, ?, ?)",
                    (
                        STATUS_FAILED, _now(), task_id, attempt_id,
                        turn_id, turn_id, *TERMINAL_STATUSES,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def is_cancelled(
        self,
        task_id: str,
        attempt_id: str | None,
        turn_id: str | None,
    ) -> bool:
        """Return whether the exact current attempt/turn was cancelled."""
        if not attempt_id:
            return False
        turn_key = turn_id or ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM cancellation_guards g "
                "JOIN sessions s ON s.session_id = g.session_id "
                "WHERE g.task_id = ? AND g.attempt_id = ? AND g.turn_id = ? "
                "AND s.attempt_id = ? AND (s.turn_id = ? OR (? = '' AND s.turn_id IS NULL))",
                (task_id, attempt_id, turn_key, attempt_id, turn_id, turn_key),
            ).fetchone()
        return row is not None

    def is_current_turn(
        self, task_id: str, attempt_id: str | None, turn_id: str | None,
    ) -> bool:
        """Return whether a callback still owns the persisted attempt/turn.

        ``turn_id`` is intentionally allowed to be ``None`` here.  Queued
        dispatches capture a session before the executor allocates its first
        turn, and must still reject that snapshot after a reopen/new attempt.
        """
        if not attempt_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_id, turn_id FROM sessions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return bool(
            row
            and row["attempt_id"] == attempt_id
            and row["turn_id"] == turn_id
        )

    def _identity_matches(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        attempt_id: str | None,
        turn_id: str | None,
    ) -> bool:
        """Check an optional identity against the row on an existing writer.

        Identity-bearing writes use this while holding the connection's write
        transaction.  A caller that only knows an attempt can guard the
        attempt; a caller with both immutable ids guards the exact turn.
        """
        if attempt_id is None and turn_id is None:
            return True
        row = conn.execute(
            "SELECT attempt_id, turn_id FROM sessions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        if attempt_id is not None and row["attempt_id"] != attempt_id:
            return False
        return turn_id is None or row["turn_id"] == turn_id

    def _guarded_update(
        self,
        task_id: str,
        sql: str,
        values: tuple[object, ...],
        *,
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Run one session/cursor write only while its identity is current."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                    conn.rollback()
                    return False
                if attempt_id is not None:
                    cancelled = conn.execute(
                        "SELECT 1 FROM cancellation_guards g "
                        "JOIN sessions s ON s.session_id = g.session_id "
                        "WHERE s.task_id = ? AND g.attempt_id = s.attempt_id "
                        "AND g.turn_id = COALESCE(s.turn_id, '')",
                        (task_id,),
                    ).fetchone()
                    if cancelled:
                        conn.rollback()
                        return False
                conn.execute(sql, (*values, task_id))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

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

    def set_claude_code_session_id(
        self, task_id: str, claude_code_session_id: str, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        """Persist the CLI's session UUID for a routing='claude_code' or
        routing='codex' session. Called by ClaudeCodeExecutor / CodexExecutor
        as soon as the subprocess emits its init event, so resume after a
        worker restart can pass `-r <uuid>` (or `codex resume <uuid>`).
        """
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET claude_code_session_id = ?, last_activity_at = ? WHERE {' AND '.join(where)}",
                (claude_code_session_id, _now(), *params),
            )
        return cur.rowcount == 1

    def set_routing_and_budget(
        self,
        task_id: str,
        routing: str | None,
        budget: dict | None,
        expected_output: str | None = None,
        preset_class: str | None = None,
        *,
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Update routing, budget, expected_output, and preset_class after preflight."""
        return self._guarded_update(
            task_id,
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
            ),
            attempt_id=attempt_id,
            turn_id=turn_id,
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
        *,
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
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
        return self._guarded_update(
            task_id,
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
            ),
            attempt_id=attempt_id,
            turn_id=turn_id,
        )

    def set_assignment(
        self,
        task_id: str,
        *,
        host: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        persona_id: str | None = None,
    ) -> None:
        """Record the board-assignment fields (#851) onto a session row.
        Called once at dispatch time, right after routing/budget are set,
        so the executor reads `session.host`/`.model`/`.effort` the same
        way it already reads `session.claude_code_model`."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET host = ?, model = ?, effort = ?, persona_id = ?, last_activity_at = ?
                WHERE task_id = ?
                """,
                (host, model, effort, persona_id, _now(), task_id),
            )

    def set_execution_snapshot(
        self, task_id: str, *, request: dict | None, spec: dict
    ) -> dict:
        """Persist the canonical request and resolved spec atomically.

        Dispatchers call this once before any executor side effect. A retry or
        restart reads the stored spec and never recomputes changed defaults.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET execution_request_json = ?, "
                "execution_spec_json = ?, routing = ?, host = ?, model = ?, "
                "effort = ?, budget_json = ?, last_activity_at = ? "
                "WHERE task_id = ? AND execution_spec_json IS NULL",
                (
                    json.dumps(request) if request is not None else None,
                    json.dumps(spec),
                    spec["executor"],
                    spec.get("host"),
                    spec.get("model_id"),
                    spec.get("effort"),
                    json.dumps(spec.get("budget")) if spec.get("budget") else None,
                    _now(),
                    task_id,
                ),
            )
            row = conn.execute(
                "SELECT execution_spec_json FROM sessions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        # A concurrent resolver may win; every caller must dispatch the same
        # persisted winner, never its stale in-memory candidate.
        return json.loads(row["execution_spec_json"])

    def begin_new_execution(
        self, task_id: str, *, request: dict | None = None
    ) -> Session:
        """Clear a terminal execution snapshot for a deliberate new turn.

        This is intentionally not a generic overwrite operation. Active
        retries/resumes retain their snapshot; only a terminal session can be
        reopened as a distinct execution.
        """
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id, status FROM sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["status"] not in TERMINAL_STATUSES:
                    raise ValueError("only a terminal session can begin a new execution")
                sid = row["session_id"]
                next_number = conn.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM execution_attempts "
                    "WHERE session_id = ?", (sid,),
                ).fetchone()[0]
                attempt_id = new_attempt_id()
                now = _now()
                conn.execute(
                    f"UPDATE sessions SET status = ?, execution_request_json = ?, "
                    f"execution_spec_json = NULL, attempt_id = ?, attempt_number = ?, "
                    f"turn_id = NULL, turn_number = 0, last_activity_at = ? "
                    f"WHERE task_id = ? AND status IN ({placeholders})",
                    (
                        STATUS_CLAIMED,
                        json.dumps(request) if request is not None else None,
                        attempt_id, next_number, now, task_id, *TERMINAL_STATUSES,
                    ),
                )
                conn.execute(
                    "INSERT INTO execution_attempts "
                    "(session_id, attempt_id, attempt_number, created_at) VALUES (?, ?, ?, ?)",
                    (sid, attempt_id, next_number, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        refreshed = self.get(task_id)
        if refreshed is None:  # pragma: no cover - row was just updated
            raise KeyError(task_id)
        return refreshed

    def set_execution_override(self, override: "object") -> None:
        """Upsert one bounded session/lineage override without global state."""
        payload = override.to_dict()
        if payload.get("scope") not in {"session", "lineage"} or not payload.get("scope_id"):
            raise ValueError("override requires a bounded session or lineage scope")
        if override.expires_at is not None and override.expires_at <= override.created_at:
            raise ValueError("override expiry must be after creation")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO execution_overrides(scope, scope_id, override_json) "
                "VALUES (?, ?, ?) ON CONFLICT(scope, scope_id) DO UPDATE SET "
                "override_json = excluded.override_json",
                (payload["scope"], payload["scope_id"], json.dumps(payload)),
            )

    def get_execution_override(
        self, *, session_id: str, root_session_id: str | None,
        now: "object | None" = None,
    ) -> "object | None":
        """Return the most specific persisted override for future resolution."""
        from datetime import datetime, timezone

        from api.services.agent_worker.execution import TemporaryOverride

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT override_json FROM execution_overrides "
                "WHERE (scope = 'session' AND scope_id = ?) "
                "OR (scope = 'lineage' AND scope_id = ?) "
                "ORDER BY CASE scope WHEN 'session' THEN 0 ELSE 1 END",
                (session_id, root_session_id or session_id),
            ).fetchall()
        now = now or datetime.now(timezone.utc)
        for row in rows:
            override = TemporaryOverride.from_dict(json.loads(row[0]))
            try:
                if override.expires_at is None or override.expires_at > now:
                    return override
            except TypeError:
                continue
        return None

    def clear_execution_override(self, *, scope: str, scope_id: str) -> None:
        if scope not in {"session", "lineage"}:
            raise ValueError("scope must be session or lineage")
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM execution_overrides WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            )

    def set_conversation_id(
        self, task_id: str, conversation_id: str, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        """Attach a Hermes conversation id (#851, routing='hermes') to a
        session — mirrors `set_managed_session_id`."""
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET conversation_id = ?, last_activity_at = ? WHERE {' AND '.join(where)}",
                (conversation_id, _now(), *params),
            )
        return cur.rowcount == 1

    def set_hermes_model(
        self, task_id: str, model: str, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        """Record the model Hermes itself reported for a turn THIS session
        ran — mirrors `set_conversation_id` exactly. Called only by
        `HermesExecutor.execute` for the session it just executed, which is
        what keeps this honest: no other writer can attribute a turn to the
        wrong session, and a completed session's value can never be
        rewritten by a later, unrelated turn — unlike the process-wide
        "last observed" value `model_readout.record_hermes_chat_turn_model`
        keeps for `/api/health`, which any Hermes turn on any surface can
        overwrite. Ignores a falsy model rather than clobbering a real
        prior observation with nothing, same rule
        `record_hermes_chat_turn_model` follows. `.strip()`s before that
        guard so a whitespace-only model (e.g. a malformed upstream
        `usage` event's `model` field) degrades to the same "no turn
        observed" outcome as an empty one, instead of writing a value that
        renders as `Hermes ·    ` — no length cap, matching
        `usage_store`/`/api/health`, which take this same string verbatim."""
        if isinstance(model, str):
            model = model.strip()
        if not model:
            return False
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET hermes_model = ?, last_activity_at = ? WHERE {' AND '.join(where)}",
                (model, _now(), *params),
            )
        return cur.rowcount == 1

    def set_remote_pgid(
        self, task_id: str, pgid: int, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        """Record the process-group id a remote-spawned subprocess echoed
        back on its first stdout line (#851) — read by the operator kill
        endpoint to reach it over ssh (see `remote_spawn.py`)."""
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET remote_pgid = ?, last_activity_at = ? WHERE {' AND '.join(where)}",
                (pgid, _now(), *params),
            )
        return cur.rowcount == 1

    def set_managed_session_id(
        self, task_id: str, managed_id: str, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        """Attach a remote Managed Agents session_id to a local session."""
        where = ["task_id = ?"]
        params: list[object] = [task_id]
        if attempt_id is not None:
            where.append("attempt_id = ?")
            params.append(attempt_id)
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET managed_agent_session_id = ?, last_activity_at = ? WHERE {' AND '.join(where)}",
                (managed_id, _now(), *params),
            )
        return cur.rowcount == 1

    def reset_managed_cursor(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Reset provider cursor state for a task before a fresh session.

        Without this, deleting a session row and re-claiming the same task_id
        (e.g., operator re-arming a task after manual cleanup) would leak the
        prior session's `last_event_id` into the new session's poll cursor —
        triggering a 400 on the events endpoint because the new session has
        never seen that id. Keep accrued session-hour dollars: they belong to
        the LifeOS session lifetime, not to one provider cursor.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                    conn.rollback()
                    return False
                conn.execute(
                    "UPDATE managed_cursor SET last_event_id = NULL, final_text = NULL, "
                    "tool_loop_signature = NULL, tool_loop_count = 0, "
                    "tool_calls_since_message = 0, usage_snapshot_json = NULL "
                    "WHERE task_id = ?",
                    (task_id,),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def add_session_hour_overhead(
        self, task_id: str, dollars: float, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Add Managed Agents session-hour overhead to the dollar counter."""
        if dollars <= 0:
            return False
        return self._guarded_update(
            task_id,
            "UPDATE sessions SET total_dollars = total_dollars + ?, last_activity_at = ? "
            "WHERE task_id = ?",
            (float(dollars), _now()),
            attempt_id=attempt_id,
            turn_id=turn_id,
        )

    # Managed Agents cursor (defined in _SCHEMA so no schema-on-write needed).
    def set_managed_last_event_id(
        self, task_id: str, event_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO managed_cursor (task_id, last_event_id) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET last_event_id = excluded.last_event_id",
                (task_id, event_id),
            )
            conn.commit()
            return True

    def get_managed_last_event_id(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> str | None:
        if attempt_id is not None and not self.is_current_turn(task_id, attempt_id, turn_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_event_id FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return row["last_event_id"] if row else None

    def get_accrued_session_hour_dollars(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> float:
        """Dollars already booked into total_dollars for session-hour overhead.

        Used by the managed executor to compute the incremental session-hour
        delta to add on each poll, avoiding double-counting.
        """
        if attempt_id is not None and not self.is_current_turn(task_id, attempt_id, turn_id):
            return 0.0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT accrued_session_hour_dollars FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set_accrued_session_hour_dollars(
        self, task_id: str, dollars: float, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO managed_cursor (task_id, accrued_session_hour_dollars) "
                "VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET accrued_session_hour_dollars = excluded.accrued_session_hour_dollars",
                (task_id, float(dollars)),
            )
            conn.commit()
            return True

    def get_managed_usage_snapshot(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict:
        """Return the provider-scoped cumulative usage cursor for a turn."""
        if attempt_id is not None and not self.is_current_turn(task_id, attempt_id, turn_id):
            return {}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT usage_snapshot_json FROM managed_cursor WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_managed_usage_snapshot(
        self, task_id: str, snapshot: dict, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Persist one provider high-water mark and current-turn baseline."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO managed_cursor (task_id, usage_snapshot_json) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET usage_snapshot_json = excluded.usage_snapshot_json",
                (task_id, json.dumps(snapshot, separators=(",", ":"))),
            )
            conn.commit()
            return True

    def set_managed_final_text(
        self, task_id: str, final_text: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Cache the latest agent.message text seen on this managed session.

        Called from the executor's poll loop whenever the driver returns a
        non-None `final_text`. Because `get_session_state` advances a cursor
        and only returns events since the last call, the terminal poll batch
        may contain only `session.status_idle` with no text content — the
        actual final answer lived in a previous batch. Persisting it here
        guarantees the finalize step always has the agent's last message.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO managed_cursor (task_id, final_text) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET final_text = excluded.final_text",
                (task_id, final_text),
            )
            conn.commit()
            return True

    def get_managed_final_text(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> str | None:
        if attempt_id is not None and not self.is_current_turn(task_id, attempt_id, turn_id):
            return None
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

    def get_runaway_state(
        self, task_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict:
        """Return the persisted runaway counters for `task_id`.

        Defaults to a clean state (signature=None, both counts 0) when no
        managed_cursor row exists yet. Callers that have never seen events
        for this session can treat the result as a virgin starting point.
        """
        if attempt_id is not None and not self.is_current_turn(task_id, attempt_id, turn_id):
            return {"tool_loop_signature": None, "tool_loop_count": 0, "tool_calls_since_message": 0}
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
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Persist runaway counters. Upsert into managed_cursor so a fresh
        session (no prior cursor row) gets a row with the counters set."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._identity_matches(conn, task_id, attempt_id, turn_id):
                conn.rollback()
                return False
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
            conn.commit()
            return True

    def record_active_seconds(
        self, task_id: str, seconds: float, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Add to a session's cumulative active-execution seconds.

        Active seconds exclude sleep time — this is the duration of LLM calls
        + tool dispatch, used by the wall-clock budget check. A session that
        spends 8 hours sleeping but only 5 minutes actually running has
        total_active_seconds ≈ 300, not 28800.
        """
        if seconds <= 0:
            return False
        return self._guarded_update(
            task_id,
            "UPDATE sessions SET total_active_seconds = total_active_seconds + ?, "
            "last_activity_at = ? WHERE task_id = ?",
            (float(seconds), _now()),
            attempt_id=attempt_id,
            turn_id=turn_id,
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
        *,
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> int | None:
        """Append one message; return its 0-based turn_index.

        Uses a single INSERT that computes the next turn_index inside the
        statement, so concurrent appends from sibling worker processes
        (Issue E) can't pick the same index — SQLite serializes the write.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if attempt_id is not None:
                row = conn.execute(
                    "SELECT task_id, attempt_id, turn_id FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or row["attempt_id"] != attempt_id or (
                    turn_id is not None and row["turn_id"] != turn_id
                ):
                    conn.rollback()
                    return None
                # A cancelled turn remains the same persisted attempt/turn
                # until it is reopened.  Reject writes from an executor that
                # is still unwinding after cancellation, even when the task
                # has not yet been reopened.
                effective_turn_id = turn_id if turn_id is not None else row["turn_id"]
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellation_guards "
                    "WHERE session_id = ? AND attempt_id = ? AND turn_id = ?",
                    (session_id, attempt_id, effective_turn_id or ""),
                ).fetchone()
                if cancelled:
                    conn.rollback()
                    return None
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
            conn.commit()
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

    def clear_messages(
        self, session_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Remove all stored messages for a session.

        Used by the local executor when resuming a session that was blocked
        at preflight before being seeded — the worker pre-injected the
        operator's clarification answer, but no system / task message
        exists. The executor clears, re-seeds with system+task, then
        re-appends the answer so the conversation arrives in the right
        order.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if attempt_id is not None:
                row = conn.execute(
                    "SELECT task_id, attempt_id, turn_id FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or row["attempt_id"] != attempt_id or (
                    turn_id is not None and row["turn_id"] != turn_id
                ):
                    conn.rollback()
                    return False
                effective_turn_id = turn_id if turn_id is not None else row["turn_id"]
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellation_guards "
                    "WHERE session_id = ? AND attempt_id = ? AND turn_id = ?",
                    (session_id, attempt_id, effective_turn_id or ""),
                ).fetchone()
                if cancelled:
                    conn.rollback()
                    return False
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # Sleeps (yield / wake)
    # ------------------------------------------------------------------

    def add_sleep(
        self, session_id: str, wake_at: int, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if attempt_id is not None:
                    row = conn.execute(
                        "SELECT task_id, attempt_id, turn_id FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if row is None or row["attempt_id"] != attempt_id or (
                        turn_id is not None and row["turn_id"] != turn_id
                    ):
                        conn.rollback()
                        return False
                    effective_turn_id = turn_id if turn_id is not None else row["turn_id"]
                    cancelled = conn.execute(
                        "SELECT 1 FROM cancellation_guards "
                        "WHERE session_id = ? AND attempt_id = ? AND turn_id = ?",
                        (session_id, attempt_id, effective_turn_id or ""),
                    ).fetchone()
                    if cancelled:
                        conn.rollback()
                        return False
                conn.execute(
                    "INSERT OR REPLACE INTO sleeps (session_id, wake_at) VALUES (?, ?)",
                    (session_id, int(wake_at)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

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

    def set_yield_waiting_for(
        self, task_id: str, children: list[str] | None, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> bool:
        return self._guarded_update(
            task_id,
            "UPDATE sessions SET yield_waiting_for = ?, last_activity_at = ? "
            "WHERE task_id = ?",
            (json.dumps(children) if children else None, _now()),
            attempt_id=attempt_id,
            turn_id=turn_id,
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

    def enqueue_message(
        self, session_id: str, sender_id: str, content: str, *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if attempt_id is not None:
                row = conn.execute(
                    "SELECT task_id FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or not self._identity_matches(
                    conn, row["task_id"], attempt_id, turn_id,
                ):
                    conn.rollback()
                    return 0
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellation_guards g "
                    "JOIN sessions s ON s.session_id = g.session_id "
                    "WHERE s.task_id = ? AND g.attempt_id = s.attempt_id "
                    "AND g.turn_id = COALESCE(s.turn_id, '')",
                    (row["task_id"],),
                ).fetchone()
                if cancelled:
                    conn.rollback()
                    return 0
            cur = conn.execute(
                "INSERT INTO pending_messages (session_id, sender_id, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, sender_id, content, _now()),
            )
        return cur.lastrowid

    def drain_pending_messages(
        self, session_id: str, *, attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict]:
        """Return + mark-delivered all pending messages for `session_id`."""
        with self._connect() as conn:
            if attempt_id is not None:
                row = conn.execute(
                    "SELECT task_id FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or not self._identity_matches(
                    conn, row["task_id"], attempt_id, turn_id,
                ):
                    return []
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
        attempt_id: str | None = None,
        turn_id: str | None = None,
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
            row = conn.execute(
                "SELECT task_id, attempt_id, turn_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                if row["task_id"] != task_id:
                    return 0
                # Questions are immutable observations of the execution that
                # asked them. Fill omitted ids from the current row so every
                # new question is fenced against a later reopen; callers may
                # still explicitly provide ids when registering an older
                # captured event.
                attempt_id = row["attempt_id"] if attempt_id is None else attempt_id
                turn_id = row["turn_id"] if turn_id is None else turn_id
                if not self._identity_matches(conn, row["task_id"], attempt_id, turn_id):
                    return 0
            elif attempt_id is not None or turn_id is not None:
                # An identity-bearing question cannot be attached to a
                # missing session; retain the old unbound-row compatibility
                # only for legacy callers that supplied no identity.
                return 0
            cur = conn.execute(
                """
                INSERT INTO pending_questions (
                    session_id, task_id, question, sent_message_id, sent_at,
                    kind, sent_message_ids, bot, attempt_id, turn_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, task_id, question, int(sent_message_id), _now(),
                    kind, json.dumps([int(i) for i in ids]), bot, attempt_id, turn_id,
                ),
            )
        return cur.lastrowid

    def add_reply_anchors(
        self,
        session_id: str,
        task_id: str,
        message_ids: list[int],
        bot: str | None = None,
        attempt_id: str | None = None,
        turn_id: str | None = None,
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
            if attempt_id is not None and not self._identity_matches(
                conn, task_id, attempt_id, turn_id,
            ):
                return
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
                        kind, sent_message_ids, bot, attempt_id, turn_id
                    ) VALUES (?, ?, ?, ?, ?, 'status_anchor', ?, ?, ?, ?)
                    """,
                    (session_id, task_id, "(session reply anchors)", ids[0],
                    _now(), json.dumps(ids), bot, attempt_id, turn_id),
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

    def deposit_answer_by_id(self, question_id: int, answer: str) -> bool:
        """Record an answer for an open question by its own row id (#850).

        Board-drawer sibling of `deposit_answer` (keyed by Telegram message
        id) and `deposit_answer_by_session_id` (keyed by session): the
        `/agents` board addresses one `pending_questions` row directly by the
        id `list_open_questions` returned it under. Sets exactly the columns
        `deposit_answer` sets, so `worker.py::_process_clarification_answers`
        consumes it unchanged on its next tick — the same path a Telegram
        reply takes. Returns True if a matching open (unanswered, not timed
        out) question existed and was updated; False otherwise.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM pending_questions "
                "WHERE id = ? AND answered_at IS NULL AND timed_out = 0 "
                "AND kind != 'status_anchor'",
                (int(question_id),),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE pending_questions "
                "SET answer = ?, answered_at = ? WHERE id = ?",
                (answer, _now(), row["id"]),
            )
        return True

    def list_open_questions(self) -> list[dict]:
        """List unanswered, unprocessed, not-timed-out questions (#850).

        Powers `GET /api/agents/pending-questions` — the board's "waiting on
        an answer" list. Scoped to `kind IN ('clarification', 'goal_approval')`
        — the two kinds `worker.py::_process_clarification_answers` treats as
        real questions awaiting a reply. `status_anchor` rows are routing
        plumbing, and `followup` rows are completion notices (see
        `notify_task_completed`), not questions — a Review card should not
        render a fake pending-question badge for one. Oldest first, so the
        board's list is stable as new questions arrive.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_questions "
                "WHERE answered_at IS NULL AND processed = 0 AND timed_out = 0 "
                "AND kind IN ('clarification', 'goal_approval') "
                "ORDER BY id ASC",
            ).fetchall()
        return [dict(r) for r in rows]

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

    # ------------------------------------------------------------------
    # Cross-machine CLI sessions (#849)
    # ------------------------------------------------------------------

    def record_cli_session_event(
        self,
        *,
        engine: str,
        event: str,
        session_id: str,
        host: str,
        cwd: str | None = None,
        transcript_path: str | None = None,
        branch: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        task_id: str | None = None,
        pane_id: int | None = None,
        wezterm_pid: int | None = None,
    ) -> CliSession:
        """Apply one hook lifecycle event to the `cli_sessions` table.

        Status machine: `session_start` -> `idle`; `user_prompt_submit` ->
        `running` (and stores `prompt` truncated to CLI_PROMPT_PREVIEW_MAX
        chars); `stop` -> `idle`; `session_end` -> `ended`. The row is
        created on the first event seen for a session_id regardless of
        which event that is — a hook installed mid-session, or one whose
        session_start post was lost, still registers the session on its
        next event rather than silently vanishing.

        Fields present on the event (cwd, branch, model, task_id, pane_id,
        wezterm_pid) overwrite the stored value; omitted (`None`) fields
        leave whatever an earlier event already captured. `ended_at` is set
        on `session_end` and cleared on `session_start` (a resumed session
        sends session_start again, which un-ends it); `stop` and
        `user_prompt_submit` leave it as-is.

        Caller (the route) validates `engine` is a key of
        CLI_ENGINE_PREFIXES and `event` is in CLI_SESSION_EVENTS — this
        method assumes both are already valid.
        """
        storage_id = f"{CLI_ENGINE_PREFIXES[engine]}:{session_id}"
        now = _now()

        if event == "session_start":
            status = CLI_STATUS_IDLE
        elif event == "user_prompt_submit":
            status = CLI_STATUS_RUNNING
        elif event == "stop":
            status = CLI_STATUS_IDLE
        else:  # session_end — validated by the caller
            status = CLI_STATUS_ENDED

        prompt_preview = (
            prompt[:CLI_PROMPT_PREVIEW_MAX]
            if event == "user_prompt_submit" and prompt is not None
            else None
        )

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM cli_sessions WHERE session_id = ?", (storage_id,)
            ).fetchone()

            if event == "session_end":
                ended_at = now
            elif event == "session_start":
                ended_at = None
            else:
                ended_at = existing["ended_at"] if existing is not None else None

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cli_sessions (
                        session_id, engine, host, cwd, transcript_path,
                        branch, model, status, prompt_preview, task_id,
                        pane_id, wezterm_pid, started_at, last_event_at,
                        ended_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_id, engine, host, cwd, transcript_path,
                        branch, model, status, prompt_preview, task_id,
                        pane_id, wezterm_pid, now, now, ended_at,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE cli_sessions SET
                        engine = ?,
                        host = ?,
                        cwd = COALESCE(?, cwd),
                        transcript_path = COALESCE(?, transcript_path),
                        branch = COALESCE(?, branch),
                        model = COALESCE(?, model),
                        status = ?,
                        prompt_preview = COALESCE(?, prompt_preview),
                        task_id = COALESCE(?, task_id),
                        pane_id = COALESCE(?, pane_id),
                        wezterm_pid = COALESCE(?, wezterm_pid),
                        last_event_at = ?,
                        ended_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        engine, host, cwd, transcript_path, branch, model,
                        status, prompt_preview, task_id, pane_id,
                        wezterm_pid, now, ended_at, storage_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM cli_sessions WHERE session_id = ?", (storage_id,)
            ).fetchone()
        return self._row_to_cli_session(row)

    def get_cli_session(self, session_id: str) -> CliSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cli_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_cli_session(row) if row else None

    def list_cli_sessions(self, limit: int = 500) -> list[CliSession]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cli_sessions ORDER BY last_event_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_cli_session(r) for r in rows]

    def list_cli_sessions_for_task(self, task_id: str) -> list[CliSession]:
        """Every `cli_sessions` row linked to `task_id`, newest first.

        Unlike the main `sessions` table, `cli_sessions.task_id` isn't a
        primary key (a card can be opened, closed, and reopened, each a
        distinct row) — so this is a direct, unbounded query, not a lookup.
        A live cc:/cx: CLI session can't be found via `get()` (that's the
        `sessions` table, keyed by task_id) or via the 200-row
        `list_sessions()` snapshot window (`cli_sessions` isn't in that
        table at all) — this is how a caller (Cancel, the board's claim
        check) finds out one exists at all.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cli_sessions WHERE task_id = ? ORDER BY last_event_at DESC",
                (task_id,),
            ).fetchall()
        return [self._row_to_cli_session(r) for r in rows]

    def has_live_session(
        self,
        task_id: str,
        status: str | None = None,
        tags: Iterable[str] | None = None,
    ) -> bool:
        """True if a non-terminal `sessions` row or a non-`ended`
        `cli_sessions` row actually backs `task_id` — as opposed to a task
        whose `status` merely reads `in_progress` with nothing behind it (a
        vault edit or an API status write can set that with no session at
        all). The board's claim rule keys its status-derived claim on this,
        not on status alone (`api.services.agent_board.is_claimed`).

        When `status` and `tags` are both given, short-circuits to `False`
        without querying anything when `agent_board.status_claim_possible`
        says the answer can't matter for the claim check anyway — the
        common case, so a policy read over many cards doesn't pay for a
        lookup per card.
        """
        if status is not None or tags is not None:
            from api.services import agent_board

            if not agent_board.status_claim_possible(status or "", tags or []):
                return False
        target = self.get(task_id)
        if target is not None and target.status not in TERMINAL_STATUSES:
            return True
        for cli in self.list_cli_sessions_for_task(task_id):
            if cli.status != CLI_STATUS_ENDED:
                return True
        return False

    @staticmethod
    def _row_to_cli_session(row: sqlite3.Row) -> CliSession:
        return CliSession(
            session_id=row["session_id"],
            engine=row["engine"],
            host=row["host"],
            status=row["status"],
            started_at=row["started_at"],
            last_event_at=row["last_event_at"],
            cwd=row["cwd"],
            transcript_path=row["transcript_path"],
            branch=row["branch"],
            model=row["model"],
            prompt_preview=row["prompt_preview"],
            task_id=row["task_id"],
            pane_id=row["pane_id"],
            wezterm_pid=row["wezterm_pid"],
            ended_at=row["ended_at"],
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
            host=(row["host"] if "host" in row.keys() else None),
            model=(row["model"] if "model" in row.keys() else None),
            effort=(row["effort"] if "effort" in row.keys() else None),
            conversation_id=(row["conversation_id"] if "conversation_id" in row.keys() else None),
            remote_pgid=(row["remote_pgid"] if "remote_pgid" in row.keys() else None),
            hermes_model=(row["hermes_model"] if "hermes_model" in row.keys() else None),
            execution_request=(
                json.loads(row["execution_request_json"])
                if "execution_request_json" in row.keys() and row["execution_request_json"]
                else None
            ),
            execution_spec=(
                json.loads(row["execution_spec_json"])
                if "execution_spec_json" in row.keys() and row["execution_spec_json"]
                else None
            ),
            attempt_id=(row["attempt_id"] if "attempt_id" in row.keys() else None),
            attempt_number=(
                row["attempt_number"] if "attempt_number" in row.keys() else 0
            ),
            turn_id=(row["turn_id"] if "turn_id" in row.keys() else None),
            turn_number=(row["turn_number"] if "turn_number" in row.keys() else 0),
            persona_id=(row["persona_id"] if "persona_id" in row.keys() else None),
        )
