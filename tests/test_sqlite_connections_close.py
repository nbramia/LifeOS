"""Ratchet against `with sqlite3.connect(...)` call sites that never close.

sqlite3's own connection context manager only commits or rolls back on
exit — it never calls `.close()`. A `with sqlite3.connect(...) as conn:` (or
`with _DB_LOCK, sqlite3.connect(...) as conn:`) call site therefore leaks a
file descriptor every time it runs. The fix is a small contextmanager that
opens the connection, nests the original `with conn:` for the same
commit/rollback behavior, and closes it in a `finally` block (see
`SessionStore._connect`, `UsageLedger._connect`, `agent_viz_summary._connect`,
`SpendTracker._connect`, and `CaptureLedger._connect` for the pattern).

This scans every module under `api/` for the raw pattern coming back. A
shrinking allowlist covers modules not yet migrated: an entry drops out
once its file no longer matches, and a match outside the allowlist fails
the scan.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PATTERN = re.compile(r"with\s+(?:_DB_LOCK,\s*)?sqlite3\.connect\(")

# Files that still open a connection this way without closing it. Remove an
# entry once its module is migrated to a closing contextmanager; leaving a
# fixed file listed here fails just as loudly as a new violation would.
_ALLOWLIST = frozenset({
    "api/services/agent_viz_label_override.py",
    "api/services/gsheet_sync.py",
    "api/services/hermes_persona_thread_store.py",
    "api/services/hermes_question_thread_store.py",
    "api/services/job_queue.py",
    "api/services/perf_trace.py",
    "api/services/usage_store.py",
})


def _matching_lines(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [i + 1 for i, line in enumerate(text.splitlines()) if _PATTERN.search(line)]


@pytest.mark.unit
def test_no_unclosed_sqlite_connect_context_managers_outside_allowlist():
    violations = []
    for path in sorted((_REPO_ROOT / "api").rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        for lineno in _matching_lines(path):
            violations.append(f"{rel}:{lineno}")
    assert not violations, (
        "found `with sqlite3.connect(...)` (never closed — see module "
        "docstring) outside the allowlist:\n" + "\n".join(violations)
    )


@pytest.mark.unit
def test_allowlist_has_no_stale_entries():
    stale = [rel for rel in sorted(_ALLOWLIST) if not _matching_lines(_REPO_ROOT / rel)]
    assert not stale, (
        "these allowlist entries no longer contain the pattern — remove "
        "them from _ALLOWLIST:\n" + "\n".join(stale)
    )
