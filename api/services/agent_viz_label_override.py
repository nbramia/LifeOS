"""Durable per-session manual label overrides for the /agents UI.

A session node's name is normally auto-derived (AI `short_label` →
task description → model badge). This module lets the operator pin a
*manual* label that overrides all of those. Stored in SQLite so it
survives restarts; cached in-process so the snapshot path (every ~2s,
up to 200 sessions) does dict lookups, not per-session disk reads.

The override wins over both the AI summary label and the derived task
label everywhere the node name is shown. Clearing it (an empty label)
reverts the node to auto-naming.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Generous cap — node labels render at ~0.95rem and word-break, so a long
# manual label degrades gracefully, but we still bound stored size.
_MAX_LABEL_LEN = 120

_DB_PATH: str | None = None
_DB_LOCK = threading.Lock()

# In-process mirror of session_id → label. None until first load; kept in
# sync on every write so reads never touch disk.
_cache: dict[str, str] | None = None


def _resolve_db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from config.settings import settings
            data_dir = Path(settings.chroma_path).parent
        except Exception:  # noqa: BLE001
            data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = str(data_dir / "agent_viz_label_overrides.db")
    return _DB_PATH


def _init_db() -> None:
    with sqlite3.connect(_resolve_db_path()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_viz_label_override (
                session_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        out: dict[str, str] = {}
        try:
            with sqlite3.connect(_resolve_db_path()) as conn:
                for sid, label in conn.execute(
                    "SELECT session_id, label FROM agent_viz_label_override"
                ):
                    out[sid] = label
        except sqlite3.Error as exc:
            logger.warning("label override load failed: %s", exc)
        _cache = out
    return _cache


def get_override(session_id: str) -> str | None:
    """Return the manual label for a session, or None if none is set."""
    return _load().get(session_id)


def set_override(session_id: str, label: str) -> str:
    """Pin a manual label. An empty/blank label clears the override instead.

    Returns the stored label ("" if cleared). Raises on a disk write failure
    so the caller can surface it — a silently-dropped rename is worse than an
    error toast.
    """
    label = (label or "").strip().replace("\n", " ")
    if len(label) > _MAX_LABEL_LEN:
        label = label[: _MAX_LABEL_LEN - 1] + "…"
    if not label:
        clear_override(session_id)
        return ""
    with _DB_LOCK, sqlite3.connect(_resolve_db_path()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_viz_label_override "
            "(session_id, label, created_at) VALUES (?, ?, ?)",
            (session_id, label, int(time.time())),
        )
        conn.commit()
    _load()[session_id] = label
    return label


def clear_override(session_id: str) -> None:
    """Drop the manual label for a session (revert to auto-naming)."""
    try:
        with _DB_LOCK, sqlite3.connect(_resolve_db_path()) as conn:
            conn.execute(
                "DELETE FROM agent_viz_label_override WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("label override delete failed for %s: %s", session_id, exc)
    _load().pop(session_id, None)


def reset_cache() -> None:
    """Drop the in-process cache (tests re-point _DB_PATH then reload)."""
    global _cache
    _cache = None


# Ensure the schema exists on import — cheap, and avoids a first-write race.
_init_db()
