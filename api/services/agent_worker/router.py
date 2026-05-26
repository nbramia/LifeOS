"""Routes a task to the appropriate executor based on preflight output.

Issue C wires only the `local` branch. `claude` rolls the tag back to
`#agent` and logs; Issue D adds the managed-agents driver and finishes the
wiring. `ask` is handled before routing by the worker — preflight returns
`routing="ask"` and the worker sends a clarification + parks the task as
`#agent-blocked` without ever invoking the router.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from api.services.agent_worker.local_executor import ExecutorOutcome, LocalExecutor
from api.services.agent_worker.preflight import ROUTE_CLAUDE, ROUTE_LOCAL


logger = logging.getLogger(__name__)


def dispatch(
    session,
    task: dict[str, Any],
    *,
    local_executor: LocalExecutor,
    on_claude_unavailable: Callable[[], None] | None = None,
) -> ExecutorOutcome | None:
    """Run the executor for `session`. Returns the outcome, or None when the
    routing destination isn't implemented yet (caller decides how to handle).
    """
    routing = session.routing
    if routing == ROUTE_LOCAL:
        return local_executor.execute(session, task)

    if routing == ROUTE_CLAUDE:
        logger.info(
            "Claude routing not yet implemented (Issue D) — leaving task for later"
        )
        if on_claude_unavailable is not None:
            on_claude_unavailable()
        return None

    logger.warning("router: unknown routing %r — skipping", routing)
    return None
