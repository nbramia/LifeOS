"""
Apple Health ingest core (issue #333).

Shared by both delivery paths: the file importer (`scripts/apple_data_import.py`,
nightly `apple_import` step) and the authenticated `POST /api/fitness/health/ingest`
endpoint. Both hand a parsed payload dict to `ingest_health()`.

Writes into the self-data fitness store (ADR-013): Apple workouts →
workout_sessions(source=apple_health), metrics → health_metrics(source=apple_health).
Idempotent — workouts dedupe on the HKWorkout uuid, metrics on (type, start).
"""
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

# Apple HKWorkoutActivityType (or a friendly label) → our session kind.
_HEALTH_KIND_MAP = {
    "running": "cardio", "walking": "cardio", "cycling": "cardio",
    "swimming": "cardio", "rowing": "cardio", "elliptical": "cardio",
    "hiking": "cardio", "highintensityintervaltraining": "cardio",
    "functionalstrengthtraining": "strength", "traditionalstrengthtraining": "strength",
    "strengthtraining": "strength", "weighttraining": "strength", "weightlifting": "strength",
    "yoga": "mobility", "coretraining": "mobility", "flexibility": "mobility",
}


def _to_utc_iso(ts: str | None) -> str:
    """Normalize an ISO8601 timestamp (offset or trailing Z) to UTC ISO.

    Returns "" for missing or unparseable input so callers can treat it as "no
    usable timestamp" — important for metric dedup, which needs a stable key.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def _local_date(utc_iso: str) -> str | None:
    """Local calendar date (YYYY-MM-DD) for a UTC ISO timestamp, in the configured
    timezone — so an evening workout buckets on the right day and matches manual
    logging's notion of 'today'. None if not parseable."""
    if not utc_iso:
        return None
    try:
        return datetime.fromisoformat(utc_iso).astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    except (ValueError, TypeError):
        return None


def _health_kind(workout_type: str | None) -> str:
    if not workout_type:
        return "cardio"
    key = re.sub(r"[^a-z]", "", workout_type.lower()).replace("hkworkoutactivitytype", "")
    return _HEALTH_KIND_MAP.get(key, "cardio")


def _workout_summary(w: dict) -> str:
    """One-line human summary for an Apple workout, e.g. '10.0 km · 55 min · 145 bpm'."""
    parts = []
    dist = w.get("distance_m")
    if dist:
        parts.append(f"{dist / 1000:.1f} km")
    dur = w.get("duration_s")
    if dur:
        parts.append(f"{int(dur // 60)} min")
    energy = w.get("energy_kcal")
    if energy:
        parts.append(f"{int(energy)} kcal")
    hr = w.get("avg_hr")
    if hr:
        parts.append(f"{int(hr)} bpm")
    return " · ".join(parts)


def ingest_health(data: dict, dry_run: bool = False) -> dict:
    """Upsert a parsed Apple Health payload into the fitness store.

    `data` is `{"workouts": [...], "metrics": [...]}`. Idempotent and safe to
    re-run; returns created/skipped counts.
    """
    from api.services.fitness_store import get_fitness_store
    store = get_fitness_store()

    w_created = w_skipped = m_created = m_skipped = 0

    # Preload existing keys once, then dedup in-memory and insert in a single
    # transaction per table. A multi-year backfill can be 100k+ rows; the old
    # per-row connect+commit approach took minutes and timed out the request.
    existing_refs = store.existing_workout_refs()
    seen_refs: set[str] = set()
    session_rows: list[dict] = []
    for w in data.get("workouts") or []:
        ref = (w.get("uuid") or "").strip()
        if not ref:
            continue
        if ref in existing_refs or ref in seen_refs:
            w_skipped += 1
            continue
        seen_refs.add(ref)
        w_created += 1
        if dry_run:
            continue
        start = _to_utc_iso(w.get("start"))
        session_rows.append({
            "date": _local_date(start),   # local calendar day; None → store uses today
            "kind": _health_kind(w.get("type")),
            "source": "apple_health",
            "title": w.get("type", "") or "Workout",
            "notes": _workout_summary(w),
            "raw_ref": ref,
        })

    existing_metric_keys = store.existing_metric_keys()
    seen_keys: set[tuple[str, str]] = set()
    metric_rows: list[dict] = []
    for m in data.get("metrics") or []:
        mtype = (m.get("type") or "").strip()
        value = m.get("value")
        if not mtype or value is None:
            continue
        start = _to_utc_iso(m.get("start"))
        if not start:
            # Without a stable timestamp the metric can't be deduped (it would
            # duplicate on every re-import), so skip it.
            m_skipped += 1
            continue
        key = (mtype, start)
        if key in existing_metric_keys or key in seen_keys:
            m_skipped += 1
            continue
        if dry_run:
            seen_keys.add(key)
            m_created += 1
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        seen_keys.add(key)
        m_created += 1
        metric_rows.append({
            "metric_type": mtype, "value": value, "unit": m.get("unit", ""),
            "start_at": start, "end_at": _to_utc_iso(m.get("end")), "source": "apple_health",
        })

    if not dry_run:
        store.bulk_insert_sessions(session_rows)
        store.bulk_insert_metrics(metric_rows)

    logger.info(
        f"Apple Health: workouts +{w_created} (skip {w_skipped}), "
        f"metrics +{m_created} (skip {m_skipped})"
    )
    return {
        "status": "ok",
        "workouts_created": w_created, "workouts_skipped": w_skipped,
        "metrics_created": m_created, "metrics_skipped": m_skipped,
    }
