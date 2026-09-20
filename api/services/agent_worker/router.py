"""Routes a task to the appropriate executor based on preflight output.

`local` routing runs through `local_executor.execute()`. `claude` routing
(the Anthropic-API/Managed-Agents route) isn't dispatched here — it logs and
rolls the tag back to `#agent` via `on_claude_unavailable`. `ask` is handled
before routing by the worker — preflight returns `routing="ask"` and the
worker sends a clarification + parks the task as `#agent-blocked` without
ever invoking the router.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from api.services.agent_worker.local_executor import ExecutorOutcome, LocalExecutor
from api.services.agent_worker.preflight import ROUTE_CLAUDE, ROUTE_CLAUDE_CODE, ROUTE_LOCAL


logger = logging.getLogger(__name__)


def dispatch(
    session,
    task: dict[str, Any],
    *,
    local_executor: LocalExecutor,
    on_claude_unavailable: Callable[[], None] | None = None,
) -> ExecutorOutcome | None:
    """Run the executor for `session`. Returns the outcome, or None when this
    function doesn't dispatch the routing destination itself (caller decides
    how to handle).
    """
    routing = session.routing
    if routing == ROUTE_LOCAL:
        return local_executor.execute(session, task)

    if routing == ROUTE_CLAUDE:
        logger.info(
            "Claude routing is dispatched by the worker directly, not this router — leaving task for later"
        )
        if on_claude_unavailable is not None:
            on_claude_unavailable()
        return None

    if routing == ROUTE_CLAUDE_CODE:
        # claude_code sessions skip preflight entirely (they're created with
        # routing="claude_code" pre-set by the /claude spawn surface or the
        # #claude tag handler in preflight) and are dispatched from
        # `_dispatch_spawned_sessions` or `_dispatch`'s CLI branch, not
        # from this router.
        logger.warning("router: ROUTE_CLAUDE_CODE is dispatched from the spawned-session path, not preflight")
        return None

    logger.warning("router: unknown routing %r — skipping", routing)
    return None
