"""Shared helper for opening a SQLite connection that actually gets closed.

A bare `sqlite3.connect(...)` used directly as a context manager only
commits or rolls back on exit — sqlite3's own connection context manager
never calls `.close()` — so a call site using that idiom leaks a file
descriptor every time it runs. `connect_closing` nests the original
`with conn:` so callers keep the same commit-on-success /
rollback-on-exception behavior, and additionally closes the connection in
a `finally` block on the way out.
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Iterator


@contextlib.contextmanager
def connect_closing(database: str, **kwargs: Any) -> Iterator[sqlite3.Connection]:
    """Open `database`, yield the connection, close it on the way out.

    `**kwargs` pass straight through to `sqlite3.connect` (e.g. `timeout`,
    `isolation_level`). Set `conn.row_factory` inside the `with` block, same
    as with a raw `sqlite3.connect(...)` call, since it must be set on the
    live connection rather than passed as a constructor argument.
    """
    conn = sqlite3.connect(database, **kwargs)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
