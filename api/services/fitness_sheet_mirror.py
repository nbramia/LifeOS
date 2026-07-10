"""
Google Sheet mirror for the fitness workout log (issue #321).

The SQLite fitness store (#320) is the source of truth; this mirrors it into a
Google Sheet so the log is viewable/editable on a phone. Opt-in via
LIFEOS_FITNESS_SHEET_ID — a no-op when unset, so a fresh clone is unaffected.

Strategy: full rebuild of the data tabs on each sync. Dedup is by construction
(the sheet is overwritten, never appended), so re-running can never duplicate
rows. A content hash short-circuits no-op rewrites. The data tabs are
mirror-managed — don't hand-edit them; the store is authoritative.

Writes are triggered in the background after a log/update/metric so they never
block the bot's reply, serialized via a dirty/running guard.
"""
import hashlib
import json
import logging
import threading

from config.settings import settings

logger = logging.getLogger(__name__)

SESSIONS_TAB = "Sessions"
SETS_TAB = "Sets"
METRICS_TAB = "Metrics"

_SESSIONS_HEADER = ["id", "date", "kind", "title", "source", "notes", "created_at"]
_SETS_HEADER = ["session_id", "date", "exercise", "set_index", "reps", "weight", "unit", "rpe", "time", "notes"]
_METRICS_HEADER = ["date", "metric_type", "value", "unit"]

_state_lock = threading.Lock()
_running = False
_dirty = False
_last_hash: str | None = None


def mirror_enabled() -> bool:
    return bool(settings.fitness_sheet_id)


def _cell(v):
    """Sheets cell value — empty string for None."""
    return "" if v is None else v


def _time_cell(seconds) -> str:
    """Duration as M:SS (or H:MM:SS) for the sheet's time column."""
    from api.services.fitness_store import format_duration
    return format_duration(seconds)


def build_tabs(store) -> dict[str, list[list]]:
    """Build the full tab contents (header + rows) from the store."""
    sessions = store.list_sessions(limit=100000)
    sess_rows = [_SESSIONS_HEADER]
    set_rows = [_SETS_HEADER]
    for s in sessions:
        sess_rows.append([s.id, s.date, s.kind, s.title, s.source, s.notes, s.created_at])
        for st in s.sets:
            set_rows.append([
                s.id, s.date, st.exercise, st.set_index,
                _cell(st.reps), _cell(st.weight), st.unit, _cell(st.rpe),
                _time_cell(st.duration_seconds), st.notes,
            ])
    # Manually reported metrics only (body weight etc.) — device imports like
    # intraday Apple Health samples would flood the sheet.
    metric_rows = [_METRICS_HEADER]
    for m in store.list_manual_metrics():
        metric_rows.append([m.start_at[:10], m.metric_type, m.value, m.unit])
    return {SESSIONS_TAB: sess_rows, SETS_TAB: set_rows, METRICS_TAB: metric_rows}


def _hash_tabs(tabs: dict) -> str:
    return hashlib.sha256(json.dumps(tabs, sort_keys=True, default=str).encode()).hexdigest()


def sync(force: bool = False) -> bool:
    """Rebuild the sheet from the store. Returns True if a write happened."""
    if not mirror_enabled():
        return False
    from api.services.fitness_store import get_fitness_store
    from api.services.sheets import get_sheets_service

    tabs = build_tabs(get_fitness_store())
    digest = _hash_tabs(tabs)
    global _last_hash
    if not force and digest == _last_hash:
        return False

    sheet_id = settings.fitness_sheet_id
    try:
        svc = get_sheets_service()
        svc.ensure_sheets(sheet_id, list(tabs.keys()))
        for title, rows in tabs.items():
            svc.clear_values(sheet_id, f"{title}!A:Z")
            svc.update_values(sheet_id, f"{title}!A1", rows)
        _last_hash = digest
        logger.info(f"Fitness sheet mirror: wrote {len(tabs[SETS_TAB]) - 1} set rows")
        return True
    except Exception as e:
        # 403 here usually means the OAuth token still has spreadsheets.readonly
        # — re-run the Google auth flow to grant read+write. Never crash the bot.
        logger.error(f"Fitness sheet mirror failed (re-auth Google if this is a 403): {e}")
        return False


def trigger_mirror() -> None:
    """Request a background mirror. Debounced + serialized; non-blocking."""
    if not mirror_enabled():
        return
    global _running, _dirty
    with _state_lock:
        _dirty = True
        if _running:
            return
        _running = True
    threading.Thread(target=_drain, daemon=True, name="FitnessSheetMirror").start()


def _drain() -> None:
    global _running, _dirty
    try:
        while True:
            with _state_lock:
                if not _dirty:
                    _running = False
                    return
                _dirty = False
            sync()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Fitness sheet mirror drain crashed: {e}")
        with _state_lock:
            _running = False
