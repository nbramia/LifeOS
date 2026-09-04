"""
Static guard: no store reachable from the CRM/people/photos routers caches
a SQLite connection across calls, or opens one with `check_same_thread=False`
(#868 review finding 9).

Acceptance criterion 6 for #868 is that these routers' stores keep opening a
fresh `sqlite3.connect()` per call rather than sharing one across threads —
that is what makes dispatching their handlers to worker threads safe at all.
This was verified by inspection at review time rather than tested; this
guard locks the invariant in going forward.

A cached connection would show up as an instance attribute assigned the
result of `sqlite3.connect(...)` (e.g. `self._conn = sqlite3.connect(...)`
in `__init__` or anywhere else) — the opposite of the established pattern
(`api/services/imessage.py`, `docs/specs/standards/python-conventions.md`
§ Database Access Pattern) of opening and closing a connection within each
method. `check_same_thread=False` is SQLite's own opt-out of the safety
check that would otherwise raise if a connection built on one thread were
used from another; needing it at all would mean a connection is being
shared across threads, which these stores must never do now that their
callers run on the worker threadpool.

One narrow, explicitly-marked exception exists: `PersonEntityStore`'s
persistent connection (`api/services/person_entity.py`,
`_get_data_version_connection`) is kept open across calls solely to read
SQLite's `PRAGMA data_version` counter cheaply -- every call site is reached
only while holding `_get_all_cache_lock`, so cross-thread use is serialized
and reopening it per call would defeat the point of caching `get_all()`. A
`sqlite3.connect(...)` call (or the `self.<attr> = ...` assignment wrapping
it) is exempt from BOTH checks above, but only when the marker comment
`# threadpool-safe: pragma-only, guarded by lock` (see `_MARKER` below)
appears on one of the source lines the call/assignment spans. Any other
`check_same_thread=False` or self-cached connection -- marked or not --
still fails the guard; the marker is not a generic escape hatch, it is tied
to this one construct via `test_unmarked_check_same_thread_false_still_fails`.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent

# Marker comment that narrowly allowlists one specific pattern: a persistent,
# lock-guarded, pragma-only connection (see module docstring). A connect
# call or self-assignment is exempt from the checks below only when this
# exact string appears on one of the source lines it spans -- it is not a
# blanket opt-out for check_same_thread=False or connection caching.
_MARKER = "# threadpool-safe: pragma-only, guarded by lock"

# Every store module reachable from api/routes/crm.py, api/routes/people.py,
# and api/routes/photos.py (verified by grepping their imports at review
# time -- see the PR's review response for the exact call graph).
STORE_MODULES = [
    "api/services/person_entity.py",
    "api/services/interaction_store.py",
    "api/services/source_entity.py",
    "api/services/relationship.py",
    "api/services/person_facts.py",
    "api/services/relationship_insights.py",
    "api/services/apple_photos.py",
]


def _sqlite_connect_calls(tree):
    """Yield every ast.Call node that is a `sqlite3.connect(...)` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_sqlite_connect = (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id == "sqlite3"
        )
        if is_sqlite_connect:
            yield node


def _has_check_same_thread_false(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "check_same_thread" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _node_has_marker(source_lines: list[str], node: ast.AST) -> bool:
    """True if `_MARKER` appears on any source line the node spans.

    Both the call to `sqlite3.connect(...)` and the `self.<attr> = ...`
    assignment wrapping it span the same lines for a multi-line call (the
    assignment's line range is a superset of the call's), so this one
    helper covers both guard checks below.
    """
    end_lineno = node.end_lineno or node.lineno
    for lineno in range(node.lineno, end_lineno + 1):
        if _MARKER in source_lines[lineno - 1]:
            return True
    return False


def _is_sqlite_connect_call(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "connect"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "sqlite3"
    )


def _unsafe_check_same_thread_calls(tree: ast.AST, source_lines: list[str]) -> list[ast.Call]:
    """`sqlite3.connect(..., check_same_thread=False)` calls not covered by
    `_MARKER` on one of their source lines."""
    return [
        call
        for call in _sqlite_connect_calls(tree)
        if _has_check_same_thread_false(call) and not _node_has_marker(source_lines, call)
    ]


def _caches_connection_on_self(tree: ast.AST, source_lines: list[str]) -> bool:
    """True if any `self.<attr> = sqlite3.connect(...)` assignment exists
    anywhere in the module -- the anti-pattern this guard forbids -- unless
    `_MARKER` appears on one of the lines the assignment spans."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_sqlite_connect_call(node.value):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                if _node_has_marker(source_lines, node):
                    continue
                return True
    return False


@pytest.mark.parametrize("relative_path", STORE_MODULES)
def test_store_does_not_share_a_connection_across_calls(relative_path):
    path = REPO_ROOT / relative_path
    source = path.read_text()
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))

    unsafe_connects = _unsafe_check_same_thread_calls(tree, source_lines)
    assert not unsafe_connects, (
        f"{relative_path} opens a sqlite3 connection with "
        "check_same_thread=False -- that only makes sense for a connection "
        "shared across threads, which these routers' stores must not do "
        "now that their handlers run on the worker threadpool (unless the "
        f"connection carries the {_MARKER!r} marker -- see module docstring)"
    )

    assert not _caches_connection_on_self(tree, source_lines), (
        f"{relative_path} assigns a sqlite3.connect(...) result to a "
        "'self.*' attribute -- stores reachable from the CRM/people/photos "
        "routers must open a fresh connection per call, not cache one "
        f"across calls/threads (unless it carries the {_MARKER!r} marker -- "
        "see module docstring)"
    )


def test_unmarked_check_same_thread_false_still_fails():
    """The marker exemption is narrowly scoped to a marked line -- an
    otherwise-identical unmarked `check_same_thread=False` connection must
    still fail the guard, whether or not it is also cached on `self`."""
    source = '''
import sqlite3

class Store:
    def __init__(self):
        self._conn = sqlite3.connect(
            "example.db",
            check_same_thread=False,
        )
'''
    source_lines = source.splitlines()
    tree = ast.parse(source)

    assert _unsafe_check_same_thread_calls(tree, source_lines), (
        "an unmarked check_same_thread=False connection should still be "
        "flagged by the guard"
    )
    assert _caches_connection_on_self(tree, source_lines), (
        "an unmarked self-cached connection should still be flagged by "
        "the guard"
    )
