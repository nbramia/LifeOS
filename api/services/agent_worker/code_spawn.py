"""Operator spawn for `routing='code'` agent-worker sessions.

`/code` (Telegram + `/chat`) calls this helper to create a parentless,
operator-origin session with routing pre-set to ``code``. The prompt and
optional dispatch metadata (``working_dir``, ``plan_mode``, originating
``chat_id``) are enqueued as a JSON pending-message so the worker's
``_dispatch_spawned_sessions`` drains them on the next tick and hands off
to ``CodeExecutor.execute``.

Mirrors ``operator_spawn.create_operator_session`` in shape, but skips
preflight entirely — the route is explicit and the per-task budget reuses
the existing Claude Code wall/cost knobs (``LIFEOS_CLAUDE_TIMEOUT`` etc.).
"""
from __future__ import annotations

import json
import logging

from config.settings import settings

from .session_store import STATUS_CLAIMED, SessionStore, new_session_id


logger = logging.getLogger(__name__)


def _code_budget() -> dict:
    """Per-session budget. Falls back to the legacy Claude Code knobs so
    operators don't have to dual-configure ``LIFEOS_CLAUDE_*`` and a new
    ``LIFEOS_CODE_*`` set during the rollout window."""
    return {
        "wall_seconds": int(settings.claude_timeout_seconds),
        # Token cap is informational for the code route — the CLI manages its
        # own context. Mirror the operator default so budget reporting in
        # ``/agents`` stays meaningful.
        "max_tokens": int(settings.agent_default_max_tokens),
        "max_dollars": float(settings.claude_max_cost_usd),
    }


def spawn_code_session(
    session_store: SessionStore,
    prompt: str,
    *,
    working_dir: str | None = None,
    plan_mode: bool = False,
    chat_id: str | None = None,
) -> dict:
    """Create a parentless ``routing='code'`` session.

    Returns ``{"ok": True, "session_id", "task_id"}`` on success, or
    ``{"ok": False, "error"}`` when ``prompt`` is empty.

    The prompt and dispatch hints are bundled into a single pending-message
    payload (JSON) so the worker can drain them as one unit. Keeping it on
    the same row preserves the operator_spawn invariant that the prompt is
    available before the session row is observable, even though the worker
    runs in a separate process.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}

    session_id = new_session_id()
    task_id = f"code_{session_id.removeprefix('sess_')}"

    payload = {
        "prompt": prompt,
        "working_dir": working_dir,
        "plan_mode": bool(plan_mode),
        "chat_id": chat_id,
    }
    # See operator_spawn for the rationale on enqueueing the prompt before
    # the session row exists.
    session_store.enqueue_message(session_id, "operator", json.dumps(payload))
    session = session_store.create(
        task_id=task_id,
        session_id=session_id,
        status=STATUS_CLAIMED,
        routing="code",
        budget=_code_budget(),
        expected_output="text",
        parent_session_id=None,
        origin="operator",
    )

    logger.info(
        "code spawn: session=%s plan_mode=%s working_dir=%s",
        session.session_id, plan_mode, working_dir,
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "task_id": task_id,
    }


def parse_code_spawn_payload(content: str) -> dict:
    """Decode a pending-message body produced by :func:`spawn_code_session`.

    Falls back to treating ``content`` as a bare prompt so legacy callers
    (and tests that pre-seed a string-only message) keep working.
    """
    try:
        data = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"prompt": content or "", "working_dir": None, "plan_mode": False, "chat_id": None}
    if not isinstance(data, dict) or "prompt" not in data:
        return {"prompt": content or "", "working_dir": None, "plan_mode": False, "chat_id": None}
    return {
        "prompt": str(data.get("prompt") or ""),
        "working_dir": data.get("working_dir"),
        "plan_mode": bool(data.get("plan_mode")),
        "chat_id": data.get("chat_id"),
    }
