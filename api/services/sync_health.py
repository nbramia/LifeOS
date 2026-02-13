"""
Sync Health Monitoring Service.

Tracks sync status for all data sources, stores errors, and provides
health check APIs. Sources must sync at least daily or be flagged as stale.
"""
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Sync health database path
SYNC_HEALTH_DB_PATH = Path(__file__).parent.parent.parent / "data" / "sync_health.db"

# Maximum age before a sync is considered stale (24 hours)
SYNC_STALE_HOURS = 24

# =============================================================================
# All data sources that should sync regularly
# Organized by phase to match run_all_syncs.py
# =============================================================================
SYNC_SOURCES = {
    # === Phase 1: Data Collection ===
    "gmail": {
        "description": "Gmail emails (sent + received + CC) via Google API",
        "script": "scripts/sync_gmail_calendar_interactions.py",
        "frequency": "daily",
        "phase": 1,
    },
    "calendar": {
        "description": "Google Calendar events",
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
    "whatsapp": {
        "description": "WhatsApp contacts and messages via wacli",
        "script": "scripts/sync_whatsapp.py",
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
        "depends_on": ["gmail", "calendar", "contacts", "linkedin"],
    },
    "photos": {
        "description": "Sync Apple Photos face recognition to CRM",
        "script": "scripts/sync_photos.py",
        "frequency": "daily",
        "phase": 2,
        "depends_on": ["contacts"],
    },

    # === Phase 3: Relationship Building ===
    "relationship_discovery": {
        "description": "Discover relationships and populate edge weights",
        "script": "scripts/sync_relationship_discovery.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["gmail", "calendar", "imessage", "whatsapp", "slack", "link_slack", "link_imessage", "phone"],
    },
    "person_stats": {
        "description": "Update person interaction counts",
        "script": "scripts/sync_person_stats.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["relationship_discovery"],
    },
    "strengths": {
        "description": "Recalculate relationship strengths for all people",
        "script": "scripts/sync_strengths.py",
        "frequency": "daily",
        "phase": 3,
        "depends_on": ["person_stats"],
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


def get_sync_health_db() -> sqlite3.Connection:
    """Get connection to sync health database."""
    SYNC_HEALTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SYNC_HEALTH_DB_PATH))
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

    for sql in migrations:
        conn.execute(sql)
    if migrations:
        conn.commit()
        logger.info(f"Migrated sync_runs table: added {len(migrations)} columns")


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
):
    """Record completion of a sync operation."""
    conn = get_sync_health_db()

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
            source_entities_created = ?
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

    if row:
        if row["completed_at"]:
            last_sync = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - last_sync).total_seconds() / 3600
            is_stale = hours_since > SYNC_STALE_HOURS
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
        expected_frequency=source_info.get("frequency", "unknown"),
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
    """Get summary of sync health across all sources."""
    all_health = get_all_sync_health()

    healthy = [h for h in all_health if not h.is_stale and h.last_status == SyncStatus.SUCCESS]
    stale = [h for h in all_health if h.is_stale]
    failed = [h for h in all_health if h.last_status == SyncStatus.FAILED]
    never_run = [h for h in all_health if h.last_sync is None]

    return {
        "total_sources": len(SYNC_SOURCES),
        "healthy": len(healthy),
        "stale": len(stale),
        "failed": len(failed),
        "never_run": len(never_run),
        "stale_sources": [h.source for h in stale],
        "failed_sources": [h.source for h in failed],
        "never_run_sources": [h.source for h in never_run],
        "all_healthy": len(stale) == 0 and len(failed) == 0,
    }


def check_sync_health() -> tuple[bool, str]:
    """
    Check overall sync health.

    Returns:
        Tuple of (is_healthy, message)
    """
    summary = get_sync_summary()

    if summary["all_healthy"]:
        return True, f"All {summary['total_sources']} sources are healthy"

    issues = []
    if summary["stale"]:
        issues.append(f"{summary['stale']} stale: {', '.join(summary['stale_sources'])}")
    if summary["failed"]:
        issues.append(f"{summary['failed']} failed: {', '.join(summary['failed_sources'])}")
    if summary["never_run"]:
        issues.append(f"{summary['never_run']} never run: {', '.join(summary['never_run_sources'])}")

    return False, "; ".join(issues)
