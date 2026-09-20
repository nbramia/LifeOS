"""Ratchet against a `sqlite3.connect(...)` call site that never closes.

`with sqlite3.connect(...) as conn:` only commits or rolls back on exit —
sqlite3's own connection context manager never calls `.close()`. A bare
`conn = sqlite3.connect(...)` returned from a helper and never wrapped in a
caller's `try`/`finally` has the same effect. Either shape leaks a file
descriptor every time it runs, and neither is limited to the `with` form —
a regression could reintroduce the bug as a bare `return sqlite3.connect(...)`
inside a `_connect`-style method with no `with` keyword anywhere in sight.
So the rule enforced here is broader than the shape of `with`: no file under
`api/` may contain the text `sqlite3.connect(` at all, except
`api/services/sqlite_connect.py` (`connect_closing`, the one place meant to
call it directly) and an explicit allowlist of modules that open a
connection this way but are individually verified to close it — most via a
`try`/`finally` (or `contextlib.closing`) around every call site, a few via
a single connection held for the life of the owning object and closed
through its own explicit teardown.

Two allowlist groups are called out separately in `_ALLOWLIST_VERIFIED_SAFE`
and `_ALLOWLIST_KNOWN_GAP` below — see the comment above each for what
distinguishes them. Both are checked the same way: a file belongs on either
list exactly as long as one of its lines matches, and a match outside both
lists fails the scan.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PATTERN = re.compile(r"sqlite3\.connect\(")
_HELPER_MODULE = "api/services/sqlite_connect.py"

# Every call site in each of these files is either inside a
# `with contextlib.closing(sqlite3.connect(...)) as conn:` block, or opens
# the connection through a `_get_connection`/`_connect`-style helper whose
# every caller wraps the call in `try: ... finally: conn.close()` — or, for
# `cc_wezterm_store.py`/`job_queue.py`, a single connection held for the
# life of the owning object with its own explicit close (`JobQueue` routes
# even its schema-init call through the same `_conn` helper every other
# method uses).
_ALLOWLIST_VERIFIED_SAFE = frozenset({
    "api/routes/journal_trends.py",
    "api/services/apple_photos.py",
    "api/services/backup_retention.py",
    "api/services/bm25_index.py",
    "api/services/cc_wezterm_store.py",
    "api/services/conversation_store.py",
    "api/services/fitness_store.py",
    "api/services/gmail_draft_ledger.py",
    "api/services/gmail_skip_cache.py",
    "api/services/imessage.py",
    "api/services/interaction_store.py",
    "api/services/job_queue.py",
    "api/services/journal_ingest_store.py",
    "api/services/person_facts.py",
    "api/services/relationship.py",
    "api/services/relationship_insights.py",
    "api/services/slack_indexer.py",
    "api/services/source_entity.py",
    "api/services/tone_analysis_store.py",
    "api/services/whatsapp.py",
})

# Each of these opens a connection and closes it only on the path that
# reaches the end of the function (or, for `aggregate_cache.py`, drops its
# cached connection on a read failure without closing it first) — there is
# no `try`/`finally` guarding the close, so an exception between open and
# close leaks the file descriptor. Narrower than the per-call leak
# `connect_closing`'s call sites had (this only leaks on an error path, not
# every call), and none of these use the `_connect`-returns-unclosed-bare-
# connection shape that pattern targets. `person_entity.py` is a different
# shape again: `PersonEntityStore._get_data_version_connection()` lazily
# opens a single connection and holds it for the life of the store, and the
# class has no close or teardown method at all, so every instance keeps
# that connection open until process exit rather than leaking only on an
# error path. Listed here rather than fixed as part of the same change that
# broadened this scan.
_ALLOWLIST_KNOWN_GAP = frozenset({
    "api/routes/crm.py",
    "api/services/aggregate_cache.py",
    "api/services/link_override.py",
    "api/services/person_entity.py",
    "api/services/person_stats.py",
    "api/services/relationship_discovery.py",
    "api/services/sync_health.py",
})

_ALLOWLIST = _ALLOWLIST_VERIFIED_SAFE | _ALLOWLIST_KNOWN_GAP


def _matching_lines(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [i + 1 for i, line in enumerate(text.splitlines()) if _PATTERN.search(line)]


@pytest.mark.unit
def test_no_unclosed_sqlite_connect_outside_allowlist():
    violations = []
    for path in sorted((_REPO_ROOT / "api").rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _HELPER_MODULE or rel in _ALLOWLIST:
            continue
        for lineno in _matching_lines(path):
            violations.append(f"{rel}:{lineno}")
    assert not violations, (
        "found `sqlite3.connect(...)` outside connect_closing and the "
        "allowlist (see module docstring):\n" + "\n".join(violations)
    )


@pytest.mark.unit
def test_allowlist_has_no_stale_entries():
    stale = [rel for rel in sorted(_ALLOWLIST) if not _matching_lines(_REPO_ROOT / rel)]
    assert not stale, (
        "these allowlist entries contain no matching lines — remove "
        "them from _ALLOWLIST_VERIFIED_SAFE / _ALLOWLIST_KNOWN_GAP:\n"
        + "\n".join(stale)
    )
