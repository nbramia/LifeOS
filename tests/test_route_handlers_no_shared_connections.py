"""
Static guard: no store reachable from the CRM/people/photos routers caches
a SQLite connection across calls, or opens one with `check_same_thread=False`
(#868 review finding 9) -- except a short, explicit allowlist of connections
verified safe despite outliving a single call (see
PERSISTENT_CONNECTION_EXCEPTIONS below).

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
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent

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

# Deliberate, reviewed exceptions to the "no persistent connection" rule.
# Each entry names the specific self.<attr> that legitimately needs a
# single, long-lived connection rather than a new one per call -- keep this
# to exactly the case(s) documented here, not a general escape hatch.
PERSISTENT_CONNECTION_EXCEPTIONS = {
    # PersonEntityStore.get_all()'s cache invalidation reads SQLite's
    # PRAGMA data_version to detect commits from OTHER connections/processes
    # (e.g. the nightly sync). That pragma's value is only meaningful when
    # read repeatedly from the SAME connection object: a brand-new
    # connection's first read does not reliably reflect the true current
    # counter (verified experimentally -- two persistent connections
    # established at different times diverge to different, non-comparable
    # values after the same external commit), so a fresh-connection-per-call
    # pattern cannot implement this invalidation check at all -- there is no
    # connection-per-call equivalent. Every read of this connection happens
    # inside PersonEntityStore._get_all_cache_lock (a plain threading.Lock),
    # so despite check_same_thread=False, access is fully serialized --
    # never truly concurrent -- which is what makes this safe despite the
    # connection outliving any single call (#869/#880 review; re-verified
    # during the #868/#887 threadpool migration, PR #880 follow-up).
    "api/services/person_entity.py": {"_data_version_conn"},
}


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


def _self_assigned_connect_calls(tree):
    """Yield (attr_name, call_node) for every `self.<attr> = sqlite3.connect(...)`
    assignment anywhere in the module -- the anti-pattern this guard forbids
    unless the attr is in PERSISTENT_CONNECTION_EXCEPTIONS for this module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        is_sqlite_connect_call = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "connect"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "sqlite3"
        )
        if not is_sqlite_connect_call:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                yield target.attr, value


@pytest.mark.parametrize("relative_path", STORE_MODULES)
def test_store_does_not_share_a_connection_across_calls(relative_path):
    path = REPO_ROOT / relative_path
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))

    allowed_attrs = PERSISTENT_CONNECTION_EXCEPTIONS.get(relative_path, set())
    self_assigned = list(_self_assigned_connect_calls(tree))
    self_assigned_calls = {call for _, call in self_assigned}

    disallowed_self_assigned = [attr for attr, _ in self_assigned if attr not in allowed_attrs]
    assert not disallowed_self_assigned, (
        f"{relative_path} assigns a sqlite3.connect(...) result to a "
        f"'self.*' attribute ({disallowed_self_assigned}) -- stores reachable "
        "from the CRM/people/photos routers must open a fresh connection "
        "per call, not cache one across calls/threads (unless added to "
        "PERSISTENT_CONNECTION_EXCEPTIONS with a documented reason)"
    )

    # check_same_thread=False is only legitimate on a connection that is
    # itself self-assigned (any disallowed self-assignment is already
    # caught above; an allowed one is a reviewed exception).
    unsafe_connects = [
        call for call in _sqlite_connect_calls(tree)
        if _has_check_same_thread_false(call) and call not in self_assigned_calls
    ]
    assert not unsafe_connects, (
        f"{relative_path} opens a sqlite3 connection with "
        "check_same_thread=False -- that only makes sense for a connection "
        "shared across threads, which these routers' stores must not do "
        "now that their handlers run on the worker threadpool"
    )
