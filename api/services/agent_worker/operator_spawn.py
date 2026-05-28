"""Operator root-spawn (#235, Phase 2 of #233).

`lifeos_agent_spawn` (inter_agent.py) is same-lineage only — it requires a
parent agent session. This module adds the operator-initiated *root* spawn: a
parentless session created on demand from Telegram or chat, with no backing
`#agent` vault task.

Routing follows the override-then-preflight rule locked with the user:
explicit ``local`` / ``claude`` wins; otherwise ``run_preflight`` decides,
falling back to the existing Telegram clarification flow on ``ROUTE_ASK``.

The created session is marked ``origin='operator'`` so the worker's
``_dispatch_spawned_sessions`` claims it even though it has no parent. The
prompt is enqueued as a pending message (the worker drains it as the task
description on dispatch), mirroring how spawned children are seeded.
"""
from __future__ import annotations

import logging

from config.settings import settings

from .preflight import ROUTE_ASK, ROUTE_CLAUDE, ROUTE_LOCAL, run_preflight
from .session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    SessionStore,
    new_session_id,
)

logger = logging.getLogger(__name__)


def _operator_budget() -> dict:
    return {
        "wall_seconds": settings.agent_default_wall_seconds,
        "max_tokens": settings.agent_default_max_tokens,
        "max_dollars": settings.agent_default_budget_dollars,
    }


def create_operator_session(
    session_store: SessionStore,
    prompt: str,
    *,
    explicit_routing: str | None = None,
    preflight_caller=None,
    budget: dict | None = None,
) -> dict:
    """Create a parentless operator-spawned session.

    Returns a result dict:
      {"ok": True, "session_id", "task_id", "routing", "needs_routing",
       "routing_source"}  on success, or
      {"ok": False, "error"}  when the prompt is empty or preflight rejects it.

    When ``needs_routing`` is True the routing came back ``ask``: the session
    is parked at ``blocked`` with ``routing='ask'`` and the caller must send the
    "local or claude?" clarification (registering a pending_question) so the
    worker resolves it. Otherwise the session is ``claimed`` and the worker's
    next tick dispatches it.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}

    routing_source = "explicit"
    if explicit_routing in (ROUTE_LOCAL, ROUTE_CLAUDE):
        routing = explicit_routing
    else:
        routing_source = "preflight"
        pre = run_preflight(title=prompt, tags=[], caller=preflight_caller)
        if not pre.sane:
            return {
                "ok": False,
                "error": f"preflight flagged this task as unsafe to run: {pre.sane_reason}",
            }
        routing = pre.routing  # local / claude / ask

    needs_routing = routing == ROUTE_ASK
    status = STATUS_BLOCKED if needs_routing else STATUS_CLAIMED
    session_id = new_session_id()
    task_id = f"op_{session_id.removeprefix('sess_')}"

    # Enqueue the prompt BEFORE the session row exists. The worker is a separate
    # process ticking against the shared DB; if it observed a CLAIMED operator
    # session before the prompt landed, _dispatch_spawned_sessions would drain
    # zero pending messages and run the agent with the synthetic session id as
    # its task description. pending_messages has no FK on session_id, so seeding
    # it first is safe — the row is only ever read once the session is dispatched.
    # The prompt becomes the dispatched session's task description (drained by
    # _dispatch_spawned_sessions) so the executor seeds the real task.
    session_store.enqueue_message(session_id, "operator", prompt)
    session = session_store.create(
        task_id=task_id,
        session_id=session_id,
        status=status,
        routing=routing,
        budget=budget or _operator_budget(),
        expected_output="text",
        parent_session_id=None,
        origin="operator",
    )

    logger.info(
        "operator spawn: session=%s routing=%s (%s) needs_routing=%s",
        session.session_id, routing, routing_source, needs_routing,
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "task_id": task_id,
        "routing": routing,
        "needs_routing": needs_routing,
        "routing_source": routing_source,
    }
