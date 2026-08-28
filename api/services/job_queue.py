"""
SQLite-backed job queue for LifeOS.

Provides background job processing so long-running operations (reindex,
sync) don't block the API. Jobs run one at a time in a background thread
with status tracking, retry logic, and progress reporting.

Usage:
    queue = get_job_queue()
    job_id = queue.enqueue("reindex_vault", params={"force": True})
    status = queue.get_job(job_id)
"""
import contextlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default DB path: data/jobs.db relative to project root
_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "jobs.db")

# Job status constants
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

# Registry of job type -> handler function
_JOB_HANDLERS: dict[str, Callable] = {}


def register_job_handler(job_type: str, handler: Callable):
    """Register a handler function for a job type."""
    _JOB_HANDLERS[job_type] = handler


def is_stale_running_job(job: "Job", process_start_time: str) -> bool:
    """Whether a `RUNNING` job predates this process (#768).

    `JobQueue.start_worker()` reconciles rows like this to `FAILED` at
    startup, but a caller reading a job's status (e.g. `GET
    /api/admin/status`, `GET /api/jobs/{id}`) may run before that
    reconciliation has happened — or against a queue instance that never
    called `start_worker()` at all. This lets those callers flag the same
    condition independently, using `JobQueue.process_start_time` as the
    "is the owning process still alive" signal: ISO-8601 timestamps from
    `datetime.now(timezone.utc).isoformat()` compare correctly as strings.

    A missing `started_at` on a `RUNNING` row counts as stale too: `_claim_next`
    always sets it in the same atomic UPDATE that sets status to `RUNNING`, so
    a real, currently-executing job always has one — a `RUNNING` row without
    it cannot be this process's own live job (Codex review of #768).
    """
    return job.status == RUNNING and (job.started_at is None or job.started_at < process_start_time)


@dataclass
class Job:
    id: str
    type: str
    status: str
    params: dict
    result: dict | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    attempts: int
    max_attempts: int
    priority: int
    error: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "params": self.params,
            "result": self.result,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "priority": self.priority,
            "error": self.error,
        }


class JobQueue:
    """SQLite-backed job queue with background worker."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._last_cleanup: float = 0.0
        # This instance's construction time, used as a proxy for "this
        # process's start time" (#768): JobQueue is a per-process singleton
        # (see get_job_queue()), created once during API startup, so a job
        # row still RUNNING with a started_at before this timestamp cannot
        # have been claimed by this process — the process that claimed it
        # is gone. Exposed so callers outside this class (admin/jobs routes)
        # can flag staleness even before start_worker()'s reconciliation
        # below has had a chance to run.
        self.process_start_time: str = datetime.now(timezone.utc).isoformat()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    params TEXT NOT NULL DEFAULT '{}',
                    result TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    priority INTEGER NOT NULL DEFAULT 10,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_status
                ON jobs (status, priority, created_at)
            """)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue(
        self,
        job_type: str,
        params: dict | None = None,
        max_attempts: int = 3,
        priority: int = 10,
    ) -> str:
        """Add a job to the queue. Returns the job ID."""
        job_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs (id, type, status, params, created_at, max_attempts, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, job_type, PENDING, json.dumps(params or {}), now, max_attempts, priority),
            )
        logger.info(f"Job enqueued: {job_id} ({job_type})")
        return job_id

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """List jobs with optional filtering."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if job_type:
            query += " AND type = ?"
            params.append(job_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending job. Returns True if cancelled."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ? AND status = ?",
                (CANCELLED, job_id, PENDING),
            )
        return cur.rowcount > 0

    def _claim_next(self) -> Job | None:
        """Claim the next pending job (atomic UPDATE...RETURNING). Returns None if queue is empty."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """UPDATE jobs SET status = ?, started_at = ?, attempts = attempts + 1
                   WHERE id = (
                       SELECT id FROM jobs
                       WHERE status = ?
                       ORDER BY priority ASC, created_at ASC
                       LIMIT 1
                   )
                   RETURNING *""",
                (RUNNING, now, PENDING),
            ).fetchone()
            if not row:
                return None
        return self._row_to_job(row)

    def _mark_completed(self, job_id: str, result: dict | None = None):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, completed_at = ?, result = ? WHERE id = ?",
                (COMPLETED, now, json.dumps(result or {}), job_id),
            )

    def _mark_failed(self, job_id: str, error: str, attempts: int, max_attempts: int):
        now = datetime.now(timezone.utc).isoformat()
        # If retries remain, set back to pending; otherwise mark failed
        new_status = PENDING if attempts < max_attempts else FAILED
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                (new_status, now if new_status == FAILED else None, error, job_id),
            )
        if new_status == PENDING:
            logger.info(f"Job {job_id} will retry (attempt {attempts}/{max_attempts})")

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            type=row["type"],
            status=row["status"],
            params=json.loads(row["params"]),
            result=json.loads(row["result"]) if row["result"] else None,
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            priority=row["priority"],
            error=row["error"],
        )

    # ---- Worker ----

    def start_worker(self):
        """Start the background worker thread.

        Reconciles orphaned jobs first (#768): a job can be left `RUNNING`
        in the database by a process that was killed or restarted mid-job
        (e.g. an unrelated auto-deploy), and nothing else ever revisits a
        `RUNNING` row to notice the process that owned it is gone. Since
        this queue processes one job at a time on a single worker thread
        that hasn't started yet, any row already `RUNNING` at this point
        that predates this process cannot belong to it — it's stranded.
        """
        if self._worker_thread and self._worker_thread.is_alive():
            logger.warning("Worker already running")
            return
        self._reconcile_orphaned_jobs()
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="JobQueueWorker",
        )
        self._worker_thread.start()
        logger.info("Job queue worker started")

    def _reconcile_orphaned_jobs(self):
        """Mark every `RUNNING` job that predates this process failed/orphaned.

        Called once, before the worker thread starts, so nothing has been
        claimed by this process yet — scoped to `started_at < process_start_time`
        (or a missing `started_at`, which `_claim_next` never actually leaves
        null on a real claim) rather than every `RUNNING` row unconditionally,
        so this can never touch a job genuinely owned by a still-alive process
        sharing the same database (Codex review of #768). Retries are
        intentionally not offered here (unlike `_mark_failed`): the job's own
        process is gone, not merely a single attempt, so whether to re-enqueue
        is a separate decision.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, completed_at = ?, error = ?
                   WHERE status = ? AND (started_at IS NULL OR started_at < ?)""",
                (FAILED, now, "orphaned: process restarted while job was running",
                 RUNNING, self.process_start_time),
            )
        if cur.rowcount:
            logger.warning(
                f"Reconciled {cur.rowcount} orphaned job(s) stranded by a previous process restart"
            )

    def stop_worker(self, timeout: float = 10.0):
        """Stop the background worker."""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        logger.info("Job queue worker stopped")

    def _worker_loop(self):
        """Poll for jobs and execute them one at a time."""
        logger.info("Job queue worker loop started")
        while not self._stop_event.is_set():
            try:
                # Auto-cleanup old jobs once per day
                if time.time() - self._last_cleanup > 86400:
                    self.cleanup_old_jobs()
                    self._last_cleanup = time.time()

                job = self._claim_next()
                if job:
                    self._execute_job(job)
                else:
                    # No jobs — sleep 2 seconds before polling again
                    self._stop_event.wait(timeout=2.0)
            except Exception as e:
                logger.error(f"Worker loop error: {e}", exc_info=True)
                self._stop_event.wait(timeout=5.0)

    def _execute_job(self, job: Job):
        """Execute a single job."""
        handler = _JOB_HANDLERS.get(job.type)
        if not handler:
            self._mark_failed(job.id, f"No handler for job type: {job.type}", job.attempts, job.max_attempts)
            return

        logger.info(f"Executing job {job.id} ({job.type}), attempt {job.attempts}/{job.max_attempts}")
        try:
            result = handler(job.params)
            self._mark_completed(job.id, result)
            logger.info(f"Job {job.id} completed")
        except Exception as e:
            logger.error(f"Job {job.id} failed: {e}", exc_info=True)
            self._mark_failed(job.id, str(e), job.attempts, job.max_attempts)

    def cleanup_old_jobs(self, days: int = 30):
        """Delete completed/failed jobs older than N days."""
        cutoff = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """DELETE FROM jobs
                   WHERE status IN (?, ?, ?)
                   AND created_at < datetime(?, '-' || ? || ' days')""",
                (COMPLETED, FAILED, CANCELLED, cutoff, days),
            )
        if cur.rowcount:
            logger.info(f"Cleaned up {cur.rowcount} old jobs")


# ---- Job Handlers ----

def _handle_reindex_vault(params: dict) -> dict:
    """Reindex the vault (runs in worker thread)."""
    from api.services.indexer import IndexerService
    from api.services.service_health import enter_maintenance, exit_maintenance
    from config.settings import settings

    # Suppress alerts during reindex (heavy ChromaDB writes can cause transient errors)
    enter_maintenance(4 * 3600)
    try:
        indexer = IndexerService(vault_path=settings.vault_path)
        count = indexer.index_all()
        return {"files_indexed": count}
    finally:
        exit_maintenance()


def _handle_sync_source(params: dict) -> dict:
    """Run a sync for a specific source via subprocess."""
    import subprocess
    source = params.get("source", "")
    if not source:
        raise ValueError("Missing 'source' parameter")

    # Map source names to sync scripts.
    # WhatsApp is intentionally absent — it now ships via the Mac Mini Apple
    # Data Agent (apple_data_export.py → apple_data_import.py).
    script_map = {
        "gmail": "scripts/sync_gmail.py",
        "calendar": "scripts/sync_calendar.py",
        "linkedin": "scripts/sync_linkedin.py",
        "contacts": "scripts/sync_contacts.py",
        "slack": "scripts/sync_slack.py",
        "vault": "scripts/sync_vault.py",
    }

    script = script_map.get(source)
    if not script:
        raise ValueError(f"Unknown sync source: {source}")

    project_root = Path(__file__).parent.parent.parent
    script_path = project_root / script

    if not script_path.exists():
        raise FileNotFoundError(f"Sync script not found: {script}")

    python = Path("~/.venvs/lifeos/bin/python").expanduser()
    result = subprocess.run(
        [str(python), str(script_path)],
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max
    )

    if result.returncode != 0:
        raise RuntimeError(f"Sync failed (exit {result.returncode}): {result.stderr[:500]}")

    return {"source": source, "stdout": result.stdout[-500:]}


# Register built-in handlers
register_job_handler("reindex_vault", _handle_reindex_vault)
register_job_handler("sync_source", _handle_sync_source)


# ---- Singleton ----

_instance: JobQueue | None = None
_lock = threading.Lock()


def get_job_queue(db_path: str | None = None) -> JobQueue:
    """Get the singleton JobQueue instance."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = JobQueue(db_path=db_path)
    return _instance
