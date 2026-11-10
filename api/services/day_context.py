"""Day-context job: assembles Dailies suggestions and writes them to Home.

Every 30 minutes, for the Dailies logical day (America/New_York, before 03:00
counts as the previous day) and the day before, it gathers

- sleep hours from the fitness store (the night he woke up from),
- Pendant day signals over HTTP (`GET /pendant/feed/day-signals?day=`),

and upserts one row per day into Home's `home_day_context` table as the
role `home_day_context_writer` (`LIFEOS_DAY_CONTEXT_DATABASE_URL`). The
subject comes from the Dailies reader connection
(`LIFEOS_DAILIES_DATABASE_URL`) and must be exactly one distinct value. Rows
older than 14 days are deleted.

An unset `LIFEOS_DAY_CONTEXT_DATABASE_URL` disables the job. A missing source
leaves that field null or empty. Failures log a fixed category only, never
sleep, signal or entry content, and never propagate to the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
TICK_SECONDS = 30 * 60
RETENTION_DAYS = 14
DAY_STARTS_AT_HOUR = 3
MIN_CONFIDENCE = 0.8
SLEEP_SOURCE = "apple_watch"

_task: "asyncio.Task[None] | None" = None

UPSERT_SQL = """
INSERT INTO home_day_context (subject, entry_date, sleep_hours, sleep_source, feelings, resonant, words, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (subject, entry_date) DO UPDATE SET
  sleep_hours = EXCLUDED.sleep_hours, sleep_source = EXCLUDED.sleep_source,
  feelings = EXCLUDED.feelings, resonant = EXCLUDED.resonant,
  words = EXCLUDED.words, updated_at = now()
"""
PRUNE_SQL = "DELETE FROM home_day_context WHERE entry_date < %s"


def logical_day(now: Optional[datetime] = None) -> date:
    """The Dailies day: New York calendar day, before 03:00 the previous one."""
    local = (now or datetime.now(NY)).astimezone(NY)
    return (local - timedelta(hours=DAY_STARTS_AT_HOUR)).date()


def target_days(now: Optional[datetime] = None) -> list[date]:
    today = logical_day(now)
    return [today, today - timedelta(days=1)]


# ---------------------------------------------------------------- sources


def fetch_subjects(dsn: str) -> list[str]:
    """Distinct subjects in `home_dailies` (read-only transaction)."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        conn.read_only = True
        return [r[0] for r in conn.execute("SELECT DISTINCT subject FROM home_dailies").fetchall()]


def sleep_hours_for(day: date, store=None) -> Optional[float]:
    """Hours slept in the night ending on the morning of `day`.

    HealthBridge keys a `sleep_hours` metric on the wake-up date
    (`start_at` = `YYYY-MM-DDT00:00:00+00:00`).
    """
    if store is None:
        from api.services.fitness_store import get_fitness_store
        store = get_fitness_store()
    iso = day.isoformat()
    metrics = store.list_metrics("sleep_hours", start=iso, end=f"{iso}T23:59:59", limit=1)
    if not metrics:
        return None
    try:
        hours = round(float(metrics[0].value), 2)
    except (TypeError, ValueError):
        return None
    return hours if 0 <= hours <= 24 else None


def _feed_token() -> str:
    if settings.pendant_feed_token:
        return settings.pendant_feed_token
    try:
        return Path(settings.pendant_feed_token_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def fetch_pendant_signals(day: date) -> Optional[dict]:
    """The Pendant feed's day signals, or None on 404, error or bad JSON."""
    url = f"{settings.pendant_feed_url.rstrip('/')}/day-signals?{urllib.parse.urlencode({'day': day.isoformat()})}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_feed_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- assembly


def _feelings(signals: dict) -> list[dict]:
    moment = signals.get("emotional_moment")
    if not isinstance(moment, dict):
        return []
    confidence = moment.get("confidence")
    path = moment.get("path")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
        return []
    if not isinstance(path, list) or not 1 <= len(path) <= 3:
        return []
    if not all(isinstance(n, str) and n.strip() for n in path):
        return []
    reason = moment.get("reason")
    reason = reason.strip()[:120] if isinstance(reason, str) else ""
    return [{"path": [n.strip() for n in path], "reason": reason}]


def _words(signals: dict) -> list[str]:
    words = signals.get("words")
    if not isinstance(words, list):
        return []
    clean = [w.strip() for w in words if isinstance(w, str) and w.strip() and len(w.strip()) <= 40]
    return clean[:3]


def build_row(day: date, sleep: Optional[float], signals: Optional[dict]) -> Optional[dict]:
    """One `home_day_context` row, or None when no source has anything."""
    signals = signals or {}
    resonant = signals.get("resonant")
    resonant = resonant.strip()[:280] if isinstance(resonant, str) and resonant.strip() else None
    row = {
        "entry_date": day,
        "sleep_hours": sleep,
        "sleep_source": SLEEP_SOURCE if sleep is not None else None,
        "feelings": _feelings(signals),
        "resonant": resonant,
        "words": _words(signals),
    }
    if row["sleep_hours"] is None and not (row["feelings"] or row["resonant"] or row["words"]):
        return None
    return row


# ---------------------------------------------------------------- writer


def write_rows(dsn: str, subject: str, rows: list[dict], cutoff: date) -> None:
    """Upsert the rows and prune older ones in one transaction."""
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        for row in rows:
            conn.execute(UPSERT_SQL, (
                subject, row["entry_date"], row["sleep_hours"], row["sleep_source"],
                Jsonb(row["feelings"]), row["resonant"], Jsonb(row["words"]),
            ))
        conn.execute(PRUNE_SQL, (cutoff,))


# ---------------------------------------------------------------- job


def run_once(
    *,
    now: Optional[datetime] = None,
    fetch_subjects_fn: Optional[Callable[[str], list[str]]] = None,
    sleep_fn: Optional[Callable[[date], Optional[float]]] = None,
    signals_fn: Optional[Callable[[date], Optional[dict]]] = None,
    write_fn: Optional[Callable[[str, str, list[dict], date], None]] = None,
) -> dict:
    """One pass. Returns {"status", "reason"?, "written"}; never raises."""
    def skipped(reason: str) -> dict:
        logger.warning("Day context skipped: %s", reason)
        return {"status": "skipped", "reason": reason, "written": 0}

    target_dsn = settings.day_context_database_url
    if not target_dsn:
        return {"status": "disabled", "written": 0}
    reader_dsn = settings.dailies_database_url
    if not reader_dsn:
        return skipped("dailies_db_not_configured")
    try:
        subjects = {s for s in (fetch_subjects_fn or fetch_subjects)(reader_dsn) if s}
    except Exception:
        return skipped("subject_lookup_failed")
    if len(subjects) != 1:
        return skipped("subject_not_unique")
    subject = next(iter(subjects))

    rows = []
    days = target_days(now)
    for day in days:
        try:
            sleep = (sleep_fn or sleep_hours_for)(day)
        except Exception:
            logger.warning("Day context: sleep_source_failed")
            sleep = None
        try:
            signals = (signals_fn or fetch_pendant_signals)(day)
        except Exception:
            logger.warning("Day context: pendant_source_failed")
            signals = None
        row = build_row(day, sleep, signals)
        if row:
            rows.append(row)

    cutoff = days[0] - timedelta(days=RETENTION_DAYS)
    try:
        (write_fn or write_rows)(target_dsn, subject, rows, cutoff)
    except Exception:
        return skipped("write_failed")
    logger.info("Day context: wrote %d row(s)", len(rows))
    return {"status": "ok", "written": len(rows)}


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must never die
            logger.warning("Day context: tick_failed")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Launch the background loop; a no-op when the writer URL is unset."""
    global _task
    if not settings.day_context_database_url:
        logger.info("Day context job disabled (LIFEOS_DAY_CONTEXT_DATABASE_URL unset)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop(), name="day_context")
    logger.info("Day context job started (every %d min)", TICK_SECONDS // 60)


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
