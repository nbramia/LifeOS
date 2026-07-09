"""
Fitness store for LifeOS.

SQLite-backed log of workout sessions/sets, health metrics (e.g. morning body
weight), and a small training profile. Written by the fitness Telegram bot (via
the orchestrator's `manage_workouts` tool) and queried for history/trends.

Self-referential data — it deliberately sits OUTSIDE the person-centric
SourceEntity/PersonEntity model (the CRM is about people; this is about the
user's own training). Apple Health import (issue #323) writes into the same
`health_metrics`/`workout_sessions` tables with source="apple_health".
"""
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

# Exercise alias dictionary (lowercased alias -> canonical display name).
_ALIASES_FILE = Path("config/exercise_aliases.json")


def get_fitness_db_path() -> str:
    """Path to the fitness database (alongside the other LifeOS SQLite stores)."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "fitness.db")


def _today() -> str:
    """Today's date (YYYY-MM-DD) in the configured timezone."""
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _now_iso() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat()


def _local_day(utc_iso: str) -> Optional[str]:
    """Local calendar day (YYYY-MM-DD) for a stored UTC ISO timestamp, in the
    configured timezone — so daily rollups match the session day-bucketing."""
    if not utc_iso:
        return None
    try:
        return datetime.fromisoformat(utc_iso).astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    except (ValueError, TypeError):
        return None


@dataclass
class WorkoutSet:
    exercise: str
    set_index: int
    reps: Optional[int] = None
    weight: Optional[float] = None
    unit: str = ""           # lb/kg for weighted sets; steps/m/… for counted work
    rpe: Optional[float] = None
    duration_seconds: Optional[int] = None
    notes: str = ""


@dataclass
class WorkoutSession:
    id: str
    date: str
    kind: str = ""           # strength | cardio | mobility | sport | other
    source: str = "manual"   # manual | apple_health
    title: str = ""
    notes: str = ""
    raw_ref: str = ""
    created_at: str = ""
    updated_at: str = ""
    sets: list[WorkoutSet] = field(default_factory=list)


@dataclass
class HealthMetric:
    id: str
    metric_type: str
    value: float
    unit: str = ""
    start_at: str = ""
    end_at: str = ""
    source: str = "manual"


def _load_aliases() -> dict:
    try:
        data = json.loads(_ALIASES_FILE.read_text())
        return {k.lower(): v for k, v in data.get("aliases", {}).items()}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load exercise aliases: {e}")
        return {}


class FitnessStore:
    """SQLite-backed workout log, health metrics, and training profile."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_fitness_db_path()
        self._aliases = _load_aliases()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_sessions (
                    id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    started_at TEXT,
                    kind TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    title TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    raw_ref TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_sets (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    exercise TEXT NOT NULL,
                    set_index INTEGER NOT NULL,
                    reps INTEGER,
                    weight REAL,
                    unit TEXT DEFAULT '',
                    rpe REAL,
                    duration_seconds INTEGER,
                    notes TEXT DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES workout_sessions(id) ON DELETE CASCADE
                )
            """)
            # Migrations for workout_sets created before these columns existed.
            set_cols = {r[1] for r in conn.execute("PRAGMA table_info(workout_sets)")}
            if "duration_seconds" not in set_cols:
                conn.execute("ALTER TABLE workout_sets ADD COLUMN duration_seconds INTEGER")
            if "weight_unit" in set_cols:
                conn.execute("ALTER TABLE workout_sets RENAME COLUMN weight_unit TO unit")
                # The old column defaulted to 'lb' unconditionally; a unit is
                # meaningless without a weight, so clear it on weightless rows.
                conn.execute("UPDATE workout_sets SET unit = '' WHERE weight IS NULL AND unit = 'lb'")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_metrics (
                    id TEXT PRIMARY KEY,
                    metric_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT DEFAULT '',
                    start_at TEXT NOT NULL,
                    end_at TEXT,
                    source TEXT DEFAULT 'manual'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS training_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_date ON workout_sessions(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sets_exercise ON workout_sets(exercise, session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sets_session ON workout_sets(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_type ON health_metrics(metric_type, start_at)")
            conn.commit()
        finally:
            conn.close()

    # -- normalization --

    def normalize_exercise(self, name: str) -> str:
        """Map an exercise alias to its canonical name; title-case unknowns."""
        if not name:
            return name
        key = name.strip().lower()
        if key in self._aliases:
            return self._aliases[key]
        return name.strip().title()

    # -- sessions --

    def add_session(
        self,
        sets: list[dict],
        date: Optional[str] = None,
        kind: str = "",
        source: str = "manual",
        title: str = "",
        notes: str = "",
        raw_ref: str = "",
    ) -> WorkoutSession:
        """Create a session and its sets.

        Each entry in `sets` may carry `count` (number of identical sets, default
        1), which is expanded into that many `workout_sets` rows with incrementing
        `set_index`. `exercise` is normalized via the alias dictionary.
        """
        session_id = uuid.uuid4().hex[:12]
        date = date or _today()
        now = _now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO workout_sessions (id, date, kind, source, title, notes, raw_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, date, kind, source, title, notes, raw_ref, now, now),
            )
            self._insert_sets(conn, session_id, sets)
            conn.commit()
        finally:
            conn.close()
        return self.get_session(session_id)

    def _insert_sets(self, conn, session_id: str, sets: list[dict], start_index: int = 1):
        idx = start_index
        for s in sets or []:
            exercise = self.normalize_exercise(s.get("exercise", ""))
            count = int(s.get("count", 1) or 1)
            count = max(1, count)
            # lb/kg only make sense with a weight; counted work may carry its
            # own unit ('steps', 'm'). Accept the legacy 'weight_unit' key.
            unit = s.get("unit") or s.get("weight_unit") or ""
            if not unit and s.get("weight") is not None:
                unit = "lb"
            for _ in range(count):
                conn.execute(
                    "INSERT INTO workout_sets (id, session_id, exercise, set_index, reps, weight, unit, rpe, duration_seconds, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex[:12], session_id, exercise, idx,
                        s.get("reps"), s.get("weight"), unit,
                        s.get("rpe"), s.get("duration_seconds"), s.get("notes", ""),
                    ),
                )
                idx += 1

    def get_session(self, session_id: str) -> Optional[WorkoutSession]:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id, date, kind, source, title, notes, raw_ref, created_at, updated_at "
                "FROM workout_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            sets = self._fetch_sets(conn, session_id)
        finally:
            conn.close()
        return WorkoutSession(
            id=row[0], date=row[1], kind=row[2], source=row[3], title=row[4],
            notes=row[5], raw_ref=row[6], created_at=row[7], updated_at=row[8], sets=sets,
        )

    def _fetch_sets(self, conn, session_id: str) -> list[WorkoutSet]:
        rows = conn.execute(
            "SELECT exercise, set_index, reps, weight, unit, rpe, duration_seconds, notes "
            "FROM workout_sets WHERE session_id = ? ORDER BY set_index", (session_id,)
        ).fetchall()
        return [
            WorkoutSet(exercise=r[0], set_index=r[1], reps=r[2], weight=r[3],
                       unit=r[4] or "", rpe=r[5], duration_seconds=r[6], notes=r[7] or "")
            for r in rows
        ]

    def has_workout_ref(self, raw_ref: str) -> bool:
        """Whether a session with this provenance ref (e.g. an Apple workout UUID)
        already exists — used for idempotent imports."""
        if not raw_ref:
            return False
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM workout_sessions WHERE raw_ref = ? LIMIT 1", (raw_ref,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def has_metric(self, metric_type: str, start_at: str) -> bool:
        """Whether a metric sample at this (type, start_at) already exists — used
        for idempotent imports."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM health_metrics WHERE metric_type = ? AND start_at = ? LIMIT 1",
                (metric_type, start_at),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def get_latest_session(self) -> Optional[WorkoutSession]:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM workout_sessions ORDER BY created_at DESC, date DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return self.get_session(row[0]) if row else None

    def update_session(
        self,
        session_id: Optional[str] = None,
        target: str = "latest",
        date: Optional[str] = None,
        kind: Optional[str] = None,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        sets: Optional[list[dict]] = None,
    ) -> Optional[WorkoutSession]:
        """Update a session. If no `session_id`, resolve `target` ('latest').

        When `sets` is provided it REPLACES the session's existing sets.
        """
        if not session_id:
            if target == "latest":
                latest = self.get_latest_session()
                if not latest:
                    return None
                session_id = latest.id
            else:
                return None
        if not self.get_session(session_id):
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            fields = {}
            if date is not None:
                fields["date"] = date
            if kind is not None:
                fields["kind"] = kind
            if title is not None:
                fields["title"] = title
            if notes is not None:
                fields["notes"] = notes
            if fields:
                fields["updated_at"] = _now_iso()
                cols = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(
                    f"UPDATE workout_sessions SET {cols} WHERE id = ?",
                    (*fields.values(), session_id),
                )
            if sets is not None:
                conn.execute("DELETE FROM workout_sets WHERE session_id = ?", (session_id,))
                self._insert_sets(conn, session_id, sets)
                conn.execute(
                    "UPDATE workout_sessions SET updated_at = ? WHERE id = ?",
                    (_now_iso(), session_id),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_session(session_id)

    def list_sessions(
        self, date_start: Optional[str] = None, date_end: Optional[str] = None,
        kind: Optional[str] = None, limit: int = 50,
    ) -> list[WorkoutSession]:
        clauses, params = [], []
        if date_start:
            clauses.append("date >= ?")
            params.append(date_start)
        if date_end:
            clauses.append("date <= ?")
            params.append(date_end)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT id FROM workout_sessions {where} ORDER BY date DESC, created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        return [self.get_session(r[0]) for r in rows]

    def exercise_history(self, exercise: str, limit: int = 20) -> list[dict]:
        """Recent sets for an exercise (normalized), newest first."""
        canonical = self.normalize_exercise(exercise)
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT s.date, ws.reps, ws.weight, ws.unit, ws.rpe, ws.duration_seconds, s.id "
                "FROM workout_sets ws JOIN workout_sessions s ON ws.session_id = s.id "
                "WHERE ws.exercise = ? ORDER BY s.date DESC, ws.set_index ASC LIMIT ?",
                (canonical, limit),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"date": r[0], "reps": r[1], "weight": r[2], "unit": r[3] or "", "rpe": r[4],
             "duration_seconds": r[5], "session_id": r[6]}
            for r in rows
        ]

    def volume_summary(
        self, exercise: Optional[str] = None, kind: Optional[str] = None,
        date_start: Optional[str] = None, date_end: Optional[str] = None,
    ) -> dict:
        """Aggregate volume (sets, reps, tonnage = sum(weight*reps)) over a window."""
        clauses, params = [], []
        if exercise:
            clauses.append("ws.exercise = ?")
            params.append(self.normalize_exercise(exercise))
        if kind:
            clauses.append("s.kind = ?")
            params.append(kind)
        if date_start:
            clauses.append("s.date >= ?")
            params.append(date_start)
        if date_end:
            clauses.append("s.date <= ?")
            params.append(date_end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(ws.reps),0), "
                "COALESCE(SUM(ws.weight * ws.reps),0), COUNT(DISTINCT ws.session_id) "
                f"FROM workout_sets ws JOIN workout_sessions s ON ws.session_id = s.id {where}",
                params,
            ).fetchone()
        finally:
            conn.close()
        return {"sets": row[0], "reps": row[1], "tonnage": row[2], "sessions": row[3]}

    # -- health metrics --

    def log_metric(
        self, metric_type: str, value: float, unit: str = "",
        start_at: Optional[str] = None, end_at: Optional[str] = None, source: str = "manual",
    ) -> HealthMetric:
        metric_id = uuid.uuid4().hex[:12]
        start_at = start_at or _now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO health_metrics (id, metric_type, value, unit, start_at, end_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (metric_id, metric_type, value, unit, start_at, end_at, source),
            )
            conn.commit()
        finally:
            conn.close()
        return HealthMetric(id=metric_id, metric_type=metric_type, value=value, unit=unit,
                            start_at=start_at, end_at=end_at or "", source=source)

    # -- bulk import (single-transaction; for large Apple Health backfills) --

    def existing_workout_refs(self) -> set[str]:
        """All non-empty provenance refs — one query for batch idempotent import."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT raw_ref FROM workout_sessions WHERE raw_ref != ''"
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def existing_metric_keys(self) -> set[tuple[str, str]]:
        """All (metric_type, start_at) pairs — one query for batch idempotent import."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT metric_type, start_at FROM health_metrics"
            ).fetchall()
        finally:
            conn.close()
        return {(r[0], r[1]) for r in rows}

    def bulk_insert_sessions(self, sessions: list[dict]) -> None:
        """Insert workout sessions (no sets) in a single transaction. Each dict
        needs date, kind, source, title, notes, raw_ref; id/timestamps generated.
        Caller is responsible for dedup (see existing_workout_refs)."""
        if not sessions:
            return
        now = _now_iso()
        rows = [
            (uuid.uuid4().hex[:12], s.get("date") or _today(), s.get("kind", ""),
             s.get("source", "manual"), s.get("title", ""), s.get("notes", ""),
             s.get("raw_ref", ""), now, now)
            for s in sessions
        ]
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO workout_sessions (id, date, kind, source, title, notes, raw_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def bulk_insert_metrics(self, metrics: list[dict]) -> None:
        """Insert health metrics in a single transaction. Each dict needs
        metric_type, value, unit, start_at, end_at, source; id generated.
        Caller is responsible for dedup (see existing_metric_keys)."""
        if not metrics:
            return
        rows = [
            (uuid.uuid4().hex[:12], m["metric_type"], m["value"], m.get("unit", ""),
             m["start_at"], m.get("end_at"), m.get("source", "manual"))
            for m in metrics
        ]
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO health_metrics (id, metric_type, value, unit, start_at, end_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def list_metrics(
        self, metric_type: str, start: Optional[str] = None, end: Optional[str] = None, limit: int = 100,
    ) -> list[HealthMetric]:
        clauses, params = ["metric_type = ?"], [metric_type]
        if start:
            clauses.append("start_at >= ?")
            params.append(start)
        if end:
            clauses.append("start_at <= ?")
            params.append(end)
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT id, metric_type, value, unit, start_at, end_at, source FROM health_metrics "
                f"WHERE {' AND '.join(clauses)} ORDER BY start_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        return [
            HealthMetric(id=r[0], metric_type=r[1], value=r[2], unit=r[3] or "",
                         start_at=r[4], end_at=r[5] or "", source=r[6])
            for r in rows
        ]

    def list_manual_metrics(self, limit: int = 10000) -> list[HealthMetric]:
        """Manually reported metrics (bot-logged; excludes device imports like
        intraday Apple Health samples), newest first."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, metric_type, value, unit, start_at, end_at, source FROM health_metrics "
                "WHERE source = 'manual' ORDER BY start_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            HealthMetric(id=r[0], metric_type=r[1], value=r[2], unit=r[3] or "",
                         start_at=r[4], end_at=r[5] or "", source=r[6])
            for r in rows
        ]

    def latest_metric(self, metric_type: str) -> Optional[HealthMetric]:
        rows = self.list_metrics(metric_type, limit=1)
        return rows[0] if rows else None

    def daily_metric_totals(
        self, metric_type: str, start: Optional[str] = None, end: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Daily rollup for a cumulative metric (e.g. steps, active_energy): the
        sum of all samples within each local calendar day. Apple Health stores
        these as many intraday buckets; this collapses them to one total per day.

        start/end bound the local-day range (YYYY-MM-DD, inclusive). Returns a
        newest-first list of {date, value, unit, samples}, capped at `limit` days.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT value, unit, start_at FROM health_metrics WHERE metric_type = ?",
                (metric_type,),
            ).fetchall()
        finally:
            conn.close()
        buckets: dict[str, dict] = {}
        for value, unit, start_at in rows:
            day = _local_day(start_at)
            if day is None or (start and day < start) or (end and day > end):
                continue
            b = buckets.get(day)
            if b is None:
                buckets[day] = {"date": day, "value": value, "unit": unit or "", "samples": 1}
            else:
                b["value"] += value
                b["samples"] += 1
        ordered = sorted(buckets.values(), key=lambda d: d["date"], reverse=True)
        return ordered[:limit]

    # -- training profile --

    def set_profile(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO training_profile (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_profile(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, value FROM training_profile").fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows}


# -- singleton --

_store_instance: Optional[FitnessStore] = None


def get_fitness_store() -> FitnessStore:
    """Get the singleton FitnessStore instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = FitnessStore()
    return _store_instance
