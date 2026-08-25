"""
Sync Health Monitoring Service.

Tracks sync status for all data sources, stores errors, and provides
health check APIs. Sources must sync at least daily or be flagged as stale.
"""
import sqlite3
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Sync health database path
SYNC_HEALTH_DB_PATH = Path(__file__).parent.parent.parent / "data" / "sync_health.db"

# Daily-cadence freshness threshold (24h). Other frequencies derive from this.
SYNC_STALE_HOURS = 24

# Maximum age before a sync is considered stale, by frequency. Weekly and
# monthly sources are expected to be silent most days, so flagging them
# every day under the 24h daily threshold produces false positives.
STALE_HOURS_BY_FREQUENCY = {
    "daily": SYNC_STALE_HOURS,
    "weekly": 8 * 24,    # 7d + 1d grace
    "monthly": 35 * 24,  # 31d + 4d grace
    "unknown": SYNC_STALE_HOURS,
}


def _is_source_disabled(source: str) -> bool:
    """Return True if the source is intentionally disabled in current settings.

    Disabled sources should never be flagged as stale, since they aren't
    expected to run. Examples: work2 accounts when work_email_domain_2 is unset,
    phone on Linux (macOS-only via FDA cron).
    """
    try:
        from config.settings import settings
    except Exception:
        return False

    # Phone is macOS-only (runs via separate FDA cron); on Linux it never runs.
    if source == "phone":
        import sys
        if sys.platform != "darwin":
            return True

    if source == "gmail_work2":
        if not getattr(settings, "sync_work2_gmail", False):
            return True
        if not getattr(settings, "work_email_domain_2", ""):
            return True

    if source == "calendar_work2":
        if not getattr(settings, "sync_work2_calendar", False):
            return True
        if not getattr(settings, "work_email_domain_2", ""):
            return True

    if source == "gmail_work":
        if not getattr(settings, "sync_work_gmail", False):
            return True
        if not getattr(settings, "work_email_domain", ""):
            return True

    if source == "calendar_work":
        if not getattr(settings, "sync_work_calendar", False):
            return True
        if not getattr(settings, "work_email_domain", ""):
            return True

    if source in ("slack", "link_slack") and not getattr(settings, "sync_slack", False):
        return True

    # Personal Google has no on/off toggle (it's the default account), so
    # "disabled" means its OAuth credentials file is absent — same signal
    # run_all_syncs.get_disabled_work_sources() uses to skip the source
    # before ever invoking the script. Without this, an unconfigured
    # install would show gmail_personal/calendar_personal as permanently
    # "never run" in the health summary instead of quietly excluded, since
    # no sync_runs row is ever written for a source that's pre-skipped —
    # issue #687.
    if source in ("gmail_personal", "calendar_personal"):
        from pathlib import Path
        credentials_path = Path(__file__).parent.parent.parent / "config" / "credentials-personal.json"
        if not credentials_path.exists():
            return True

    return False

# =============================================================================
# All data sources that should sync regularly
# Organized by phase to match run_all_syncs.py
# =============================================================================
SYNC_SOURCES = {
    # === Phase 1: Data Collection ===
    "gmail_personal": {
        "description": "Personal Gmail emails (sent + received + CC)",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "gmail_work": {
        "description": "Work Gmail emails (sent + received + CC)",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "gmail_work2": {
        "description": "Second work Gmail emails (sent + received + CC)",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "calendar_personal": {
        "description": "Personal Google Calendar events",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "calendar_work": {
        "description": "Work Google Calendar events",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "calendar_work2": {
        "description": "Second work Google Calendar events",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "linkedin": {
        "description": "LinkedIn connections from CSV export",
        "script": "scripts/sync_linkedin.py",
        "frequency": "daily",
        "phase": 1,
    },
    "contacts": {
        "description": "Apple Contacts via CSV export",
        "script": "scripts/sync_contacts_csv.py",
        "frequency": "weekly",
        "phase": 1,
    },
    "phone": {
        "description": "Phone call history from CallHistoryDB",
        "script": "scripts/sync_phone_calls.py",
        "frequency": "daily",
        "phase": 1,
    },
    "imessage": {
        "description": "iMessage/SMS conversations",
        "script": "scripts/sync_imessage_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "slack": {
        "description": "Slack users and DM messages",
        "script": "scripts/sync_slack.py",
        "frequency": "daily",
        "phase": 1,
    },
    "apple_import": {
        "description": "Import Apple ecosystem data (contacts, iMessage, phone) from Mac exports",
        "script": "scripts/apple_data_import.py",
        "frequency": "daily",
        "phase": 1,
    },

    # === Phase 2: Entity Processing ===
    "link_slack": {
        "description": "Link Slack entities to people by email",
        "script": "scripts/link_slack_entities.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["slack"],
    },
    "link_imessage": {
        "description": "Link iMessage handles to people by phone",
        "script": "scripts/link_imessage_entities.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["imessage"],
    },
    "link_source_entities": {
        "description": "Retroactively link unlinked source entities to people",
        "script": "scripts/link_source_entities.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["gmail_personal", "gmail_work", "calendar_personal", "calendar_work", "contacts", "linkedin"],
    },
    "photos": {
        "description": "Sync Apple Photos face recognition to CRM",
        "script": "scripts/sync_photos.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["contacts"],
    },

    # === Phase 2b: Stale ID Cleanup ===
    "repoint_stale_ids": {
        "description": "Re-point interactions with stale merged person IDs to canonical IDs",
        "script": "scripts/sync_repoint_stale_ids.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["link_imessage", "link_source_entities"],
    },

    # === Phase 3: Relationship Building ===
    "person_stats_full": {
        "description": "Full refresh of all PersonEntity counts and timestamps",
        "script": "scripts/sync_person_stats.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["photos", "link_source_entities"],
    },
    "relationship_discovery": {
        "description": "Discover relationships and populate edge weights",
        "script": "scripts/sync_relationship_discovery.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["gmail_personal", "gmail_work", "calendar_personal", "calendar_work", "imessage", "slack", "link_slack", "link_imessage", "phone"],
    },
    "strengths": {
        "description": "Recalculate relationship strengths for all people",
        "script": "scripts/sync_strengths.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["relationship_discovery"],
    },
    "push_birthdays": {
        "description": "Push LifeOS birthdays to Apple Contacts",
        "script": "scripts/push_birthdays_to_contacts.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["contacts"],  # Run after contacts are synced
    },

    # === Phase 4: Vector Store Indexing ===
    "vault_reindex": {
        "description": "Reindex vault notes to ChromaDB and BM25",
        "script": "scripts/sync_vault_reindex.py",
        "frequency": "daily",
        "phase": 4,
        "depends_on": ["strengths"],  # Run after all CRM processing
    },
    "crm_vectorstore": {
        "description": "Index CRM people to vector store for semantic search",
        "script": "scripts/sync_crm_to_vectorstore.py",
        "frequency": "daily",
        "phase": 4,
        "depends_on": ["strengths"],  # Run after relationship metrics computed
    },

    # === Phase 5: Content Sync ===
    "google_docs": {
        "description": "Sync Google Docs to vault as markdown",
        "script": "scripts/sync_google_docs.py",
        "frequency": "daily",
        "phase": 5,
    },
    "google_sheets": {
        "description": "Sync Google Sheets to vault as markdown",
        "script": "scripts/sync_google_sheets.py",
        "frequency": "daily",
        "phase": 5,
    },

    # === Phase 5b: Financial Data ===
    "monarch_money": {
        "description": "Monarch Money financial data (monthly summary to vault)",
        "script": "scripts/sync_monarch_money.py",
        "frequency": "monthly",
        "phase": 5,
    },

    # === Phase 6: Post-Sync Cleanup ===
    "entity_cleanup": {
        "description": "Post-sync cleanup (non-human detection, duplicate queue)",
        "script": "scripts/sync_entity_cleanup.py",
        "frequency": "daily",
        "phase": 6,
        "depends_on": ["crm_vectorstore"],
    },

    # === Phase 7: Consistency Verification ===
    "consistency_verify": {
        "description": "Cross-store data consistency verification and auto-fix",
        "script": "scripts/sync_consistency_verify.py",
        "frequency": "daily",
        "phase": 7,
        "depends_on": ["entity_cleanup"],
    },
}


class SyncStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"
    SKIPPED = "skipped"


@dataclass
class SyncResult:
    """Result of a sync operation."""
    source: str
    status: SyncStatus
    started_at: datetime
    completed_at: Optional[datetime]
    records_processed: int
    records_created: int
    records_updated: int
    errors: int
    error_message: Optional[str]
    duration_seconds: Optional[float]


@dataclass
class SyncHealth:
    """Health status for a sync source."""
    source: str
    description: str
    last_sync: Optional[datetime]
    last_status: Optional[SyncStatus]
    last_error: Optional[str]
    is_stale: bool
    hours_since_sync: Optional[float]
    expected_frequency: str
    is_disabled: bool = False


def get_sync_health_db() -> sqlite3.Connection:
    """Get connection to sync health database."""
    SYNC_HEALTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SYNC_HEALTH_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    """Initialize sync health schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            records_processed INTEGER DEFAULT 0,
            records_created INTEGER DEFAULT 0,
            records_updated INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            error_message TEXT,
            duration_seconds REAL
        );

        CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON sync_runs(source);
        CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at DESC);

        CREATE TABLE IF NOT EXISTS sync_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            error_type TEXT,
            error_message TEXT NOT NULL,
            stack_trace TEXT,
            context TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sync_errors_source ON sync_errors(source);
        CREATE INDEX IF NOT EXISTS idx_sync_errors_timestamp ON sync_errors(timestamp DESC);
    """)
    conn.commit()

    # Migration: add categorized stats columns if missing
    cursor = conn.execute("PRAGMA table_info(sync_runs)")
    columns = {row[1] for row in cursor.fetchall()}
    migrations = []
    if "people_created" not in columns:
        migrations.append("ALTER TABLE sync_runs ADD COLUMN people_created INTEGER DEFAULT 0")
    if "people_updated" not in columns:
        migrations.append("ALTER TABLE sync_runs ADD COLUMN people_updated INTEGER DEFAULT 0")
    if "interactions_created" not in columns:
        migrations.append("ALTER TABLE sync_runs ADD COLUMN interactions_created INTEGER DEFAULT 0")
    if "source_entities_created" not in columns:
        migrations.append("ALTER TABLE sync_runs ADD COLUMN source_entities_created INTEGER DEFAULT 0")
    if "trigger_source" not in columns:
        migrations.append("ALTER TABLE sync_runs ADD COLUMN trigger_source TEXT DEFAULT 'unknown'")
    if "attempt_count" not in columns:
        # Issue #541: within-run retry for transient (connectivity/rate-limit)
        # failures. 1 = succeeded or failed on the first try (no retry
        # attempted); >1 = a retry was needed before the final status. This
        # single column is enough to derive all three states the health
        # record must distinguish: first-time success (status=success,
        # attempt_count=1), success-after-retry (status=success,
        # attempt_count>1), and gave-up (status=failed, attempt_count>1 means
        # retries were exhausted; attempt_count=1 means the failure was
        # classified non-transient and never retried).
        # DEFAULT 1 makes every pre-existing row (from before this column
        # existed) read as "no retry" rather than NULL, so historical rows
        # stay queryable without special-casing NULL everywhere.
        migrations.append("ALTER TABLE sync_runs ADD COLUMN attempt_count INTEGER DEFAULT 1")

    for sql in migrations:
        conn.execute(sql)
    if migrations:
        conn.commit()
        logger.info(f"Migrated sync_runs table: added {len(migrations)} columns")


def emit_sync_stats(stats: dict) -> None:
    """Print the canonical ``SYNC_STATS:{json}`` line consumed by run_all_syncs.

    The orchestrator's ``_parse_sync_output`` treats this as authoritative,
    bypassing brittle regex inference. Each top-level sync script should call
    this once with its final tallies. Only int-valued keys are read by the
    parser; nested dicts and strings are ignored for forwards-compat.

    The output is intentionally single-line — the parser regex
    ``SYNC_STATS:(\\{[^\\n]*\\})`` will not match across newlines, so the
    JSON payload must not contain literal newlines (``json.dumps`` doesn't
    add any). Don't pretty-print the dict here.
    """
    import json as _json
    # Keep on its own line so the parser regex matches cleanly even when the
    # surrounding output has interleaved logging.
    print("SYNC_STATS:" + _json.dumps(stats), flush=True)


def reap_orphan_sync_runs(max_age_hours: int = 8) -> int:
    """Mark stale ``status='running'`` rows as failed.

    A row can stay in the running state forever if the sync process was killed
    without unwinding (SIGKILL, kernel OOM, lost ssh, system reboot). Without
    cleanup, the dashboard shows phantom in-progress syncs and the staleness
    check uses the wrong "last completed" timestamp.

    Args:
        max_age_hours: rows older than this are reaped. Default 8h covers even
            the longest vault reindex (4h) with a generous safety margin.

    Returns:
        Number of orphan rows reaped.
    """
    conn = get_sync_health_db()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        cursor = conn.execute(
            """
            UPDATE sync_runs
            SET status = ?,
                completed_at = ?,
                error_message = COALESCE(error_message, 'Reaped orphan: process died without unwinding'),
                errors = MAX(errors, 1)
            WHERE status = ?
              AND started_at < ?
            """,
            (
                SyncStatus.FAILED.value,
                datetime.now(timezone.utc).isoformat(),
                SyncStatus.RUNNING.value,
                cutoff,
            ),
        )
        reaped = cursor.rowcount
        conn.commit()
        if reaped:
            logger.warning(f"Reaped {reaped} orphan sync_runs row(s) older than {max_age_hours}h")
        return reaped
    finally:
        conn.close()


def record_sync_start(source: str) -> int:
    """
    Record the start of a sync operation.

    Returns:
        Run ID for updating completion status
    """
    conn = get_sync_health_db()
    cursor = conn.execute(
        """
        INSERT INTO sync_runs (source, status, started_at)
        VALUES (?, ?, ?)
        """,
        (source, SyncStatus.RUNNING.value, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    run_id = cursor.lastrowid
    conn.close()
    logger.info(f"Started sync for {source} (run_id={run_id})")
    return run_id


def record_sync_complete(
    run_id: int,
    status: SyncStatus,
    records_processed: int = 0,
    records_created: int = 0,
    records_updated: int = 0,
    errors: int = 0,
    error_message: Optional[str] = None,
    people_created: int = 0,
    people_updated: int = 0,
    interactions_created: int = 0,
    source_entities_created: int = 0,
    attempt_count: int = 1,
    duration_seconds: Optional[float] = None,
):
    """Record completion of a sync operation.

    ``attempt_count`` is the total number of attempts the orchestrator made
    before reaching this final status (1 = no retry needed; >1 = one or more
    transient-failure retries happened first — see issue #541). One row per
    logical run still gets one update, keeping the row *count* history
    detectors see unchanged by retries.

    ``duration_seconds``, when given, is used verbatim instead of being
    derived from ``started_at``. The retry loop passes the elapsed time of
    only the attempt that produced this final outcome — not the earlier
    failed attempts or the backoff sleeps between them — because
    ``get_typical_duration_seconds``/``_detect_duration_collapse`` assume
    this column means "how long a normal run takes". Deriving it from
    ``started_at`` (set once, at the first attempt) would let a retried
    run's failed-attempt-plus-backoff time inflate that baseline upward
    every time a retry happens, making the collapse detector progressively
    less sensitive given how often retries are expected to fire (issue #541
    adversarial review). When omitted (e.g. a caller outside the retry
    loop), duration still falls back to the ``started_at``-derived value.
    """
    conn = get_sync_health_db()

    if duration_seconds is not None:
        duration = duration_seconds
    else:
        # Get start time to calculate duration
        row = conn.execute(
            "SELECT started_at FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()

        duration = None
        if row:
            started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            duration = (datetime.now(timezone.utc) - started).total_seconds()

    conn.execute(
        """
        UPDATE sync_runs SET
            status = ?,
            completed_at = ?,
            records_processed = ?,
            records_created = ?,
            records_updated = ?,
            errors = ?,
            error_message = ?,
            duration_seconds = ?,
            people_created = ?,
            people_updated = ?,
            interactions_created = ?,
            source_entities_created = ?,
            attempt_count = ?
        WHERE id = ?
        """,
        (
            status.value,
            datetime.now(timezone.utc).isoformat(),
            records_processed,
            records_created,
            records_updated,
            errors,
            error_message,
            duration,
            people_created,
            people_updated,
            interactions_created,
            source_entities_created,
            attempt_count,
            run_id,
        )
    )
    conn.commit()
    conn.close()

    if status == SyncStatus.FAILED:
        logger.error(f"Sync failed for run_id={run_id}: {error_message}")
    else:
        logger.info(f"Sync completed for run_id={run_id}: {status.value}")


def record_sync_error(
    source: str,
    error_message: str,
    error_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    context: Optional[str] = None,
):
    """Record a sync error for later analysis."""
    conn = get_sync_health_db()
    conn.execute(
        """
        INSERT INTO sync_errors (source, timestamp, error_type, error_message, stack_trace, context)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            datetime.now(timezone.utc).isoformat(),
            error_type,
            error_message,
            stack_trace,
            context,
        )
    )
    conn.commit()
    conn.close()
    logger.error(f"Recorded sync error for {source}: {error_message}")


def get_typical_duration_seconds(
    source: str,
    n: int = 5,
    min_duration_seconds: float = 2.0,
) -> Optional[float]:
    """Median duration of the last ``n`` eligible successful runs for ``source``.

    Used by run_all_syncs to detect duration collapse: a sync that historically
    takes minutes suddenly completing in a fraction of a second is the
    signature of a silent no-op (e.g. credentials missing from the child env —
    issue #438). Runs shorter than ``min_duration_seconds`` are excluded from
    the history because they are exactly the pathology being hunted; letting
    them into the median would make consecutive silent no-ops look "typical"
    after a few days.

    Returns None when there is no eligible history.
    """
    conn = get_sync_health_db()
    try:
        rows = conn.execute(
            """
            SELECT duration_seconds FROM sync_runs
            WHERE source = ?
              AND status = ?
              AND duration_seconds IS NOT NULL
              AND duration_seconds >= ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (source, SyncStatus.SUCCESS.value, min_duration_seconds, n),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None
    return float(statistics.median(row["duration_seconds"] for row in rows))


def _row_yield(row) -> int:
    """Total records a run produced. The stats columns overlap by design
    (``records_created`` already absorbs people/interactions/source-entities via
    the max() rollup in run_all_syncs), so take the largest signal rather than
    summing — summing would double-count and inflate the median."""
    return max(
        row["records_created"] or 0,
        row["records_updated"] or 0,
        row["interactions_created"] or 0,
        row["records_processed"] or 0,
    )


def get_typical_yield(source: str, n: int = 10) -> Optional[float]:
    """Median records produced across the last ``n`` *productive* runs of ``source``.

    Complements :func:`get_typical_duration_seconds`. Duration collapse only
    catches sources that *used* to be slow; a source that always finishes
    instantly (and always produces nothing) is invisible to it. Yield measures
    the thing we actually care about — did this run do anything.

    Zero-yield runs are excluded from the history for the same reason
    :func:`get_typical_duration_seconds` excludes instant runs: they are the
    pathology being hunted. Including them lets a few days of silent no-ops
    redefine "typical" as zero, after which nothing can ever look wrong —
    exactly how `entity_cleanup` went unnoticed from Feb 2026.

    Returns None when the source has no productive history to compare against.
    """
    conn = get_sync_health_db()
    try:
        rows = conn.execute(
            """
            SELECT records_created, records_updated, interactions_created,
                   records_processed
            FROM sync_runs
            WHERE source = ? AND status = ?
              AND (COALESCE(records_created, 0) > 0
                   OR COALESCE(records_updated, 0) > 0
                   OR COALESCE(interactions_created, 0) > 0
                   OR COALESCE(records_processed, 0) > 0)
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (source, SyncStatus.SUCCESS.value, n),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None
    return float(statistics.median(_row_yield(row) for row in rows))


def get_consecutive_zero_yield_runs(source: str, limit: int = 50) -> int:
    """How many of the most recent successful runs produced nothing, in a row.

    A single empty run is normal (a quiet night with no new mail). A *streak*
    of them from a source that normally produces records is a silent failure.
    """
    conn = get_sync_health_db()
    try:
        rows = conn.execute(
            """
            SELECT records_created, records_updated, interactions_created,
                   records_processed
            FROM sync_runs
            WHERE source = ? AND status = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (source, SyncStatus.SUCCESS.value, limit),
        ).fetchall()
    finally:
        conn.close()

    streak = 0
    for row in rows:
        if _row_yield(row) > 0:
            break
        streak += 1
    return streak


def get_repeated_yield_streak(source: str, value: float, limit: int = 50) -> int:
    """How many of the most recent successful runs, in a row, produced
    exactly ``value`` records.

    Complements :func:`get_consecutive_zero_yield_runs`, which only catches
    a source going silent (yield drops to zero). A source re-importing the
    same byte-identical stale upstream file every night can instead report
    the *same non-zero* count forever — e.g. issue #646, where a dead Mac
    Mini export agent left ten nights reporting an identical "1294 created"
    while nothing had actually changed. A long streak of an identical
    non-zero count is the signature of that: real nightly variation almost
    never lands on the exact same number twice in a row.
    """
    conn = get_sync_health_db()
    try:
        rows = conn.execute(
            """
            SELECT records_created, records_updated, interactions_created,
                   records_processed
            FROM sync_runs
            WHERE source = ? AND status = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (source, SyncStatus.SUCCESS.value, limit),
        ).fetchall()
    finally:
        conn.close()

    streak = 0
    for row in rows:
        if _row_yield(row) != value:
            break
        streak += 1
    return streak


def get_yield_history(source: str) -> dict:
    """Lifetime yield stats for ``source``: run count and best run ever.

    Used to spot a source that has *never* produced anything across many runs —
    the signature of a dead or misconfigured source that no per-run check can
    see, because every run looks exactly like the last one.
    """
    conn = get_sync_health_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS runs,
                   MAX(MAX(COALESCE(records_created, 0),
                           COALESCE(records_updated, 0),
                           COALESCE(interactions_created, 0),
                           COALESCE(records_processed, 0))) AS best,
                   AVG(COALESCE(duration_seconds, 0)) AS avg_duration
            FROM sync_runs
            WHERE source = ? AND status = ?
            """,
            (source, SyncStatus.SUCCESS.value),
        ).fetchone()
    finally:
        conn.close()

    return {
        "runs": (row["runs"] if row else 0) or 0,
        "best_yield": (row["best"] if row else 0) or 0,
        "avg_duration_seconds": float((row["avg_duration"] if row else 0) or 0.0),
    }


def get_sync_health(source: str) -> SyncHealth:
    """Get health status for a specific source."""
    source_info = SYNC_SOURCES.get(source, {
        "description": f"Unknown source: {source}",
        "frequency": "unknown",
    })

    conn = get_sync_health_db()
    row = conn.execute(
        """
        SELECT source, status, completed_at, error_message
        FROM sync_runs
        WHERE source = ? AND status != 'running'
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (source,)
    ).fetchone()
    conn.close()

    last_sync = None
    last_status = None
    last_error = None
    hours_since = None
    is_stale = True

    frequency = source_info.get("frequency", "unknown")
    disabled = _is_source_disabled(source)
    stale_threshold = STALE_HOURS_BY_FREQUENCY.get(frequency, SYNC_STALE_HOURS)

    if disabled:
        # Don't flag a deliberately disabled source as stale — it isn't expected to run.
        is_stale = False
    elif row:
        if row["completed_at"]:
            last_sync = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - last_sync).total_seconds() / 3600
            is_stale = hours_since > stale_threshold
        last_status = SyncStatus(row["status"])
        last_error = row["error_message"]

    return SyncHealth(
        source=source,
        description=source_info["description"],
        last_sync=last_sync,
        last_status=last_status,
        last_error=last_error,
        is_stale=is_stale,
        hours_since_sync=hours_since,
        expected_frequency=frequency,
        is_disabled=disabled,
    )


def get_all_sync_health() -> list[SyncHealth]:
    """Get health status for all sync sources."""
    return [get_sync_health(source) for source in SYNC_SOURCES.keys()]


def get_stale_syncs() -> list[SyncHealth]:
    """Get list of syncs that are stale (>24 hours old)."""
    all_health = get_all_sync_health()
    return [h for h in all_health if h.is_stale]


def get_failed_syncs(hours: int = 24) -> list[dict]:
    """Get list of failed syncs in the last N hours."""
    conn = get_sync_health_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    rows = conn.execute(
        """
        SELECT source, status, started_at, completed_at, error_message, errors
        FROM sync_runs
        WHERE status = 'failed' AND started_at > ?
        ORDER BY started_at DESC
        """,
        (cutoff,)
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_recent_errors(source: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Get recent sync errors."""
    conn = get_sync_health_db()

    if source:
        rows = conn.execute(
            """
            SELECT * FROM sync_errors
            WHERE source = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (source, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM sync_errors
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_sync_summary() -> dict:
    """Get summary of sync health across all sources.

    Disabled sources (work2 without config, phone on Linux, etc.) are
    excluded from staleness/never-run buckets — they aren't expected to run.
    """
    all_health = get_all_sync_health()
    enabled = [h for h in all_health if not h.is_disabled]

    healthy = [h for h in enabled if not h.is_stale and h.last_status == SyncStatus.SUCCESS]
    stale = [h for h in enabled if h.is_stale and h.last_sync is not None]
    failed = [h for h in enabled if h.last_status == SyncStatus.FAILED]
    never_run = [h for h in enabled if h.last_sync is None]
    disabled = [h for h in all_health if h.is_disabled]

    return {
        "total_sources": len(SYNC_SOURCES),
        "enabled_sources": len(enabled),
        "healthy": len(healthy),
        "stale": len(stale),
        "failed": len(failed),
        "never_run": len(never_run),
        "disabled": len(disabled),
        "stale_sources": [h.source for h in stale],
        "failed_sources": [h.source for h in failed],
        "never_run_sources": [h.source for h in never_run],
        "disabled_sources": [h.source for h in disabled],
        "all_healthy": len(stale) == 0 and len(failed) == 0 and len(never_run) == 0,
    }


def detect_silent_source_entity_drift(
    interactions_db: Optional[str] = None,
    crm_db: Optional[str] = None,
    interactions_lookback_days: int = 7,
    source_entity_stale_days: int = 30,
) -> list[dict]:
    """Return per-source warnings when interactions are flowing but source_entities aren't.

    Diagnoses the silent regression pattern from issue #199 §2: a source still
    persists new interactions every night, but stops producing new
    ``source_entities`` rows. Without this detector, ``sync_runs`` happily
    records ``success`` and the dashboard reads "healthy" while entity
    resolution rots in the background.

    The check is intentionally lenient: we look at MAX(created_at) and only
    warn when the gap exceeds ``source_entity_stale_days``, so a source that
    legitimately hasn't observed any new handles (e.g. you didn't message a
    new phone number this week) won't trip an alert. The intent is to catch
    the months-long drift, not normal quiet days.

    Args:
        interactions_db: Path to interactions.db. Defaults to data/interactions.db.
        crm_db: Path to crm.db. Defaults to data/crm.db.
        interactions_lookback_days: Only consider sources with interactions
            newer than this. Avoids false positives for genuinely-defunct sources.
        source_entity_stale_days: Warn when the newest source_entity is older
            than this. 30 days is generous — most user behaviour produces at
            least one new entity per month per active source.

    Returns:
        List of warnings, each ``{"source": str, "last_interaction": iso8601,
        "last_source_entity": iso8601 | None, "gap_days": float}``.
        Empty list when everything looks consistent.
    """
    import sqlite3
    from pathlib import Path

    project_root = Path(__file__).parent.parent.parent
    interactions_path = Path(interactions_db) if interactions_db else project_root / "data" / "interactions.db"
    crm_path = Path(crm_db) if crm_db else project_root / "data" / "crm.db"

    if not interactions_path.exists() or not crm_path.exists():
        return []  # Fresh install or test environment — nothing to compare

    cutoff = (datetime.now(timezone.utc) - timedelta(days=interactions_lookback_days)).isoformat()
    warnings: list[dict] = []

    try:
        # Both DBs run in WAL mode (see InteractionStore / SourceEntityStore
        # init). Read connections see a consistent snapshot at open time;
        # concurrent writes by an ongoing sync don't block us or skew rows.
        i_conn = sqlite3.connect(str(interactions_path))
        c_conn = sqlite3.connect(str(crm_path))
        try:
            # Sources with any interactions in the lookback window
            active = i_conn.execute(
                """
                SELECT source_type, MAX(created_at) AS latest
                FROM interactions
                WHERE created_at > ?
                GROUP BY source_type
                """,
                (cutoff,),
            ).fetchall()

            for source_type, latest_interaction in active:
                if not source_type or not latest_interaction:
                    continue
                row = c_conn.execute(
                    "SELECT MAX(created_at) FROM source_entities WHERE source_type = ?",
                    (source_type,),
                ).fetchone()
                latest_se = row[0] if row else None

                if latest_se is None:
                    warnings.append({
                        "source": source_type,
                        "last_interaction": latest_interaction,
                        "last_source_entity": None,
                        "gap_days": None,
                    })
                    continue

                try:
                    se_dt = datetime.fromisoformat(latest_se.replace("Z", "+00:00"))
                    if se_dt.tzinfo is None:
                        se_dt = se_dt.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                gap_days = (datetime.now(timezone.utc) - se_dt).total_seconds() / 86400.0
                if gap_days > source_entity_stale_days:
                    warnings.append({
                        "source": source_type,
                        "last_interaction": latest_interaction,
                        "last_source_entity": latest_se,
                        "gap_days": round(gap_days, 1),
                    })
        finally:
            i_conn.close()
            c_conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"Source-entity drift detector skipped (db error): {e}")
        return []

    return warnings


def check_sync_health() -> tuple[bool, str]:
    """
    Check overall sync health.

    Returns:
        Tuple of (is_healthy, message)
    """
    summary = get_sync_summary()

    if summary["all_healthy"]:
        total = summary["total_sources"]
        disabled = summary["disabled"]
        if disabled:
            # Self-explanatory breakdown (issue #494 follow-up): total tracked
            # sources includes ones that are intentionally disabled (e.g.
            # macOS-only "phone" on a Linux host) and therefore don't appear
            # in the nightly run order — without the breakdown this line
            # looked like it contradicted "Total sources: N" a few lines up.
            enabled = summary["enabled_sources"]
            return True, f"All {total} tracked sources healthy ({enabled} active + {disabled} disabled)"
        return True, f"All {total} sources are healthy"

    issues = []
    if summary["stale"]:
        issues.append(f"{summary['stale']} stale: {', '.join(summary['stale_sources'])}")
    if summary["failed"]:
        issues.append(f"{summary['failed']} failed: {', '.join(summary['failed_sources'])}")
    if summary["never_run"]:
        issues.append(f"{summary['never_run']} never run: {', '.join(summary['never_run_sources'])}")

    return False, "; ".join(issues)
