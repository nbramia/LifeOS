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
coroutine function whose own source contains no `await` — the exact
regression this issue fixes.
"""
import inspect

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


ROUTERS = [
    ("crm", crm_router),
    ("people", people_router),
    ("photos", photos_router),
]


@pytest.mark.parametrize("router_name,router", ROUTERS)
def test_async_handlers_all_contain_an_await(router_name, router):
    """
    Every `async def` handler in these routers must contain an `await` in
    its own body. A coroutine function with no `await` runs synchronously on
    the event loop anyway (getting none of asyncio's concurrency) while also
    blocking every other request on the process — strictly worse than a
    plain `def`, which FastAPI dispatches to the threadpool automatically.
    """
    violations = []
    for path, endpoint in _handlers(router):
        if not inspect.iscoroutinefunction(endpoint):
            continue
        source = inspect.getsource(endpoint)
        if "await" not in source:
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
