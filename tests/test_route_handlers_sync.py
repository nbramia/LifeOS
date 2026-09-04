"""
Static guard: CRM, people, and photos route handlers run off the event loop
when they can (#868).

Every request to these routers used to run as `async def` even when its body
did no async work at all, so FastAPI dispatched it straight onto the single
event loop instead of the worker threadpool. One slow handler (a full
people-list scan, a relationship tone analysis) then blocked every other
request on the process — chat, Telegram, voice, MCP, and other CRM tabs —
until it finished. A plain `def` handler is dispatched to the threadpool
automatically; an `async def` handler with nothing to await gets none of
that and instead runs inline on the loop.

This walks the CRM, people, and photos routers and fails if any handler is a
coroutine function whose own body contains no `await`/`async for`/`async
with` — the exact regression this issue fixes. This scope is deliberate:
the same convention applies project-wide (see
docs/specs/standards/python-conventions.md), but an AST scan of the rest of
`api/routes/` at review time found 95 more `async def` handlers with no
`await` across 23 other modules — converting those is out of scope for
#868, so this guard, like the docs it enforces, is scoped to the three
routers this issue actually changed.

The check is AST-based rather than a substring search on the source text:
`"await" not in source` would pass vacuously for a handler whose docstring
or a comment merely mentions the word "await" without a real await
anywhere in the body.
"""
import ast
import inspect
import textwrap

import pytest

from api.routes.crm import router as crm_router
from api.routes.people import router as people_router
from api.routes.photos import router as photos_router

pytestmark = pytest.mark.unit


def _handlers(router):
    """Return (path, endpoint) pairs for a router's routes, deduplicated."""
    seen = set()
    handlers = []
    for route in router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or endpoint in seen:
            continue
        seen.add(endpoint)
        handlers.append((route.path, endpoint))
    return handlers


class _AsyncConstructFinder(ast.NodeVisitor):
    """Finds `await`/`async for`/`async with` in a function's own body,
    without descending into any nested function/lambda — an inner
    function's own await doesn't make the outer one truly async."""

    def __init__(self):
        self.found = False

    def visit_Await(self, node):
        self.found = True

    def visit_AsyncFor(self, node):
        self.found = True

    def visit_AsyncWith(self, node):
        self.found = True

    def visit_FunctionDef(self, node):
        pass  # don't descend into nested defs

    def visit_AsyncFunctionDef(self, node):
        pass

    def visit_Lambda(self, node):
        pass


def _has_own_async_construct(fn) -> bool:
    """True if `fn`'s own body (not any nested def) contains an `await`,
    `async for`, or `async with`."""
    source = textwrap.dedent(inspect.getsource(fn))
    func_node = ast.parse(source).body[0]
    finder = _AsyncConstructFinder()
    finder.generic_visit(func_node)  # visits func_node's children, not func_node itself
    return finder.found


ROUTERS = [
    ("crm", crm_router),
    ("people", people_router),
    ("photos", photos_router),
]


@pytest.mark.parametrize("router_name,router", ROUTERS)
def test_async_handlers_all_contain_an_await(router_name, router):
    """
    Every `async def` handler in these routers must contain a real
    `await`/`async for`/`async with` in its own body. A coroutine function
    with none of those runs synchronously on the event loop anyway (getting
    none of asyncio's concurrency) while also blocking every other request
    on the process — strictly worse than a plain `def`, which FastAPI
    dispatches to the threadpool automatically.
    """
    violations = []
    for path, endpoint in _handlers(router):
        if not inspect.iscoroutinefunction(endpoint):
            continue
        if not _has_own_async_construct(endpoint):
            violations.append(f"{endpoint.__name__} ({path})")

    assert not violations, (
        f"{router_name} router: async def handlers with no await "
        f"(should be plain def): {', '.join(violations)}"
    )


def test_at_least_one_handler_remains_async_per_await_router():
    """
    Sanity check on the test itself: crm and people each keep at least one
    genuinely async handler (fact extraction / source import in crm.py; the
    legacy v2 wrappers folded into their sync targets in people.py leave no
    async handler there, which is expected). This guards against the
    parametrized test above passing vacuously if `_handlers` ever stopped
    finding routes.
    """
    crm_handlers = _handlers(crm_router)
    assert crm_handlers, "expected to find CRM route handlers"
    assert any(inspect.iscoroutinefunction(fn) for _, fn in crm_handlers), (
        "expected at least one genuinely async handler in api/routes/crm.py "
        "(e.g. fact extraction, which awaits an LLM call)"
    )
