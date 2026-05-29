"""SQLite-backed mapping from Claude Code session id → WezTerm pane id.

When the /agents `Resume` action spawns a WezTerm tab via
`wezterm cli spawn`, the resulting pane id is recorded here so the new
`Focus` action can later call `wezterm cli activate-pane --pane-id <id>`
and bring the user back to the existing tab instead of spawning a fresh
one.

Storage: `data/cc_wezterm.db`, one row per session_id. Pane ids may
become stale (user closed the tab, restarted wezterm); the focus
endpoint deletes stale rows on activate-pane failure.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path("data/cc_wezterm.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS panes (
    session_id   TEXT PRIMARY KEY,
    pane_id      INTEGER NOT NULL,
    cwd          TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    wezterm_pid  INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class PaneMapping:
    session_id: str
    pane_id: int
    cwd: str
    created_at: int
    # PID of the wezterm-gui process at the time this mapping was written.
    # Used by /focus to invalidate the cache across wezterm restarts: pane
    # ids reset when wezterm-gui restarts, so a mapping pointing at "pane 5"
    # before the restart no longer corresponds to the same terminal session
    # afterwards. wezterm_pid=0 means "unknown" — pre-migration rows or
    # writers that couldn't determine the live pid. Always treated as stale
    # at read time so the focus endpoint falls through to a fresh probe.
    wezterm_pid: int = 0


class CCWezTermStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Forward-migrate pre-#257 schemas that lack `wezterm_pid`. ALTER
        # TABLE ADD COLUMN is idempotent-safe via the PRAGMA check; SQLite
        # has no `IF NOT EXISTS` for columns.
        self._migrate_add_column("wezterm_pid", "INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()

    def _migrate_add_column(self, column: str, decl: str) -> None:
        cur = self._conn.execute("PRAGMA table_info(panes)")
        existing = {row["name"] for row in cur.fetchall()}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE panes ADD COLUMN {column} {decl}")

    def get(self, session_id: str) -> Optional[PaneMapping]:
        cur = self._conn.execute(
            "SELECT session_id, pane_id, cwd, created_at, wezterm_pid "
            "FROM panes WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return PaneMapping(
            session_id=row["session_id"],
            pane_id=int(row["pane_id"]),
            cwd=row["cwd"],
            created_at=int(row["created_at"]),
            wezterm_pid=int(row["wezterm_pid"]),
        )

    def upsert(
        self,
        session_id: str,
        pane_id: int,
        cwd: str,
        wezterm_pid: int = 0,
    ) -> PaneMapping:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO panes(session_id, pane_id, cwd, created_at, wezterm_pid)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                pane_id     = excluded.pane_id,
                cwd         = excluded.cwd,
                created_at  = excluded.created_at,
                wezterm_pid = excluded.wezterm_pid
            """,
            (session_id, int(pane_id), cwd, now, int(wezterm_pid)),
        )
        self._conn.commit()
        return PaneMapping(
            session_id=session_id,
            pane_id=int(pane_id),
            cwd=cwd,
            created_at=now,
            wezterm_pid=int(wezterm_pid),
        )

    def delete(self, session_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM panes WHERE session_id = ?", (session_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()


_default_store: CCWezTermStore | None = None


def get_default_store() -> CCWezTermStore:
    """Process-wide singleton — the FastAPI workers all share one handle."""
    global _default_store
    if _default_store is None:
        _default_store = CCWezTermStore()
    return _default_store
