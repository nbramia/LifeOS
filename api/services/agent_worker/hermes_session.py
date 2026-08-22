"""Hermes turn -> agent-worker session identity (#640).

Hermes runs `mcp_server.py` as a stdio MCP server and gets the whole
`lifeos_agent_*` family advertised to it (see `inter_agent.py`), but every
one of those tools requires a resolvable `caller_session_id` — Hermes has no
agent-worker session of its own to supply one, so every call would fail with
`no_caller`. This module gives it one: a real row in the shared
`SessionStore`, created (or reused) by the Hermes proxy
(`api/routes/hermes_proxy.py`) before each turn is forwarded, and exposed to
Hermes as `lifeos_context.turn.caller_session_id`.

Lifecycle: per-conversation, not per-turn. A session Hermes spawns via
`lifeos_agent_spawn` on turn N can easily outlive that turn — the operator
may not check back in until turn N+3 — so the spend caps in `inter_agent.py`
(`max_spawn_depth`, `max_descendants_per_root`) need one stable lineage root
per conversation rather than a fresh one every turn (which would silently
reset those caps each time). The session id is deterministic in the
conversation's id (a plain hash, no extra lookup table needed): the same
`conversation_id` always resolves to the same `session_id`, so nothing has
to be threaded between this call and any other request handling the same
conversation.

Turn 1 of a brand-new conversation is the one gap this can't close: the
browser sends no `conversation_id` yet on that request (Hermes mints one,
and the proxy only learns it after the fact, from the SSE stream — see
`_HermesTurnPersister` in `hermes_proxy.py`), so there is nothing to hash
against. That turn gets its own one-off root instead. Anything it spawns is
still fully usable on a later turn — `lifeos_agent_check` has no lineage
restriction (see `inter_agent.check()`) — it just isn't threaded into the
conversation's longer-lived root that exists from turn 2 onward. Closing
that gap would mean smuggling state between two independently invoked
callbacks (`transform_body` / `make_observer` in `api/routes/_proxy.py`) for
a single-turn window that has no bearing on the spend guard or the
cross-turn `check` use case this module exists for.

The session is never dispatched by the worker: `status=STATUS_YIELDED` with
no `yield_waiting_for` is inert — skipped by `_wake_sleeping_sessions`
(driven by the `sleeps` table, not session status), by
`_resume_yielded_for_children` (requires `yield_waiting_for IS NOT NULL`),
and by `resume_pending`'s crash-recovery rollback (`STATUS_YIELDED` is left
alone unconditionally on worker restart) — see `worker.py`. `origin="hermes"`
(not `"operator"`) keeps `_dispatch_spawned_sessions` from ever claiming it
the way it claims operator root-spawns. It exists purely as an
identity/lineage anchor for `lifeos_agent_*` calls Hermes itself makes.
"""
from __future__ import annotations

import hashlib
import sqlite3

from config.settings import settings

from .session_store import STATUS_YIELDED, SessionStore, new_session_id

# Caller routing tag for a Hermes-rooted session. Checked by the spend guard
# in `inter_agent.spawn()` (see `NON_API_BILLED_ROOT_ROUTINGS`) so a
# Hermes-rooted lineage can't spawn an API-billed (`model="claude"`) child
# without an operator saying so explicitly — the same treatment a
# `claude_code`/`codex` root already gets (ADR-018).
HERMES_ROUTING = "hermes"


def _deterministic_session_id(conversation_id: str) -> str:
    """Stable session_id for a given Hermes conversation_id — same input,
    same output, every time, so no separate conversation->session lookup
    table is needed."""
    digest = hashlib.sha256(f"hermes:{conversation_id}".encode()).hexdigest()[:16]
    return f"sess_herm{digest}"


def _hermes_budget() -> dict:
    """Same default budget an operator root-spawn gets (see
    `operator_spawn._operator_budget`) — without this, a spawned child with
    no explicit `max_dollars` would compute a $0 budget against this
    session's empty default and be created un-runnable."""
    return {
        "wall_seconds": settings.agent_default_wall_seconds,
        "max_tokens": settings.agent_default_max_tokens,
        "max_dollars": settings.agent_default_budget_dollars,
    }


def resolve_hermes_caller_session_id(
    session_store: SessionStore, conversation_id: str | None,
) -> str:
    """Return the `caller_session_id` Hermes should use for this turn,
    creating the backing session row on first use.

    Idempotent for a given `conversation_id`: concurrent requests, or later
    turns, resolve to the same session. `conversation_id` of `None` (turn 1
    of a brand-new conversation — see module docstring) always mints a
    fresh, one-off session instead.
    """
    if not conversation_id:
        session_id = new_session_id()
    else:
        session_id = _deterministic_session_id(conversation_id)
        existing = session_store.get_by_session_id(session_id)
        if existing is not None:
            return session_id

    task_id = f"hermes_{session_id.removeprefix('sess_')}"
    try:
        session_store.create(
            task_id=task_id,
            session_id=session_id,
            status=STATUS_YIELDED,
            routing=HERMES_ROUTING,
            budget=_hermes_budget(),
            expected_output="text",
            origin="hermes",
        )
    except sqlite3.IntegrityError:
        # Lost a create race against a concurrent request for the same
        # conversation (create()'s documented signal for a duplicate
        # task_id) — the other request's row is authoritative and already
        # has this exact deterministic session_id.
        pass
    return session_id
