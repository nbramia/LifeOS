"""Operator spawn for `routing='codex'` agent-worker sessions.

`/codex` (Telegram + `/chat`) calls this helper to create a parentless,
operator-origin session with routing pre-set to ``codex``. The prompt and
optional dispatch metadata (``working_dir``, originating ``chat_id``) are
enqueued as a JSON pending-message so the worker's
``_dispatch_spawned_sessions`` drains them on the next tick and hands off
to ``CodexExecutor.execute``.

Mirrors :mod:`claude_code_spawn` in shape; differs only in the routing tag and
the omission of ``plan_mode`` (Codex doesn't have an equivalent flag).
"""
from __future__ import annotations

import json
import logging

from config.settings import settings

from .session_store import STATUS_CLAIMED, SessionStore, new_session_id


logger = logging.getLogger(__name__)


def _codex_budget() -> dict:
    """Per-session budget. Reuses the same wall/cost knobs as /claude so
    operators don't have to dual-configure."""
    return {
        "wall_seconds": int(settings.claude_timeout_seconds),
        "max_tokens": int(settings.agent_default_max_tokens),
        "max_dollars": float(settings.claude_max_cost_usd),
    }


def spawn_codex_session(
    session_store: SessionStore,
    prompt: str,
    *,
    working_dir: str | None = None,
    chat_id: str | None = None,
) -> dict:
    """Create a parentless ``routing='codex'`` session.

    Returns ``{"ok": True, "session_id", "task_id"}`` on success, or
    ``{"ok": False, "error"}`` when ``prompt`` is empty.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}

    session_id = new_session_id()
    task_id = f"codex_{session_id.removeprefix('sess_')}"

    payload = {
        "prompt": prompt,
        "working_dir": working_dir,
        "chat_id": chat_id,
    }
    session_store.enqueue_message(session_id, "operator", json.dumps(payload))
    session = session_store.create(
        task_id=task_id,
        session_id=session_id,
        status=STATUS_CLAIMED,
        routing="codex",
        budget=_codex_budget(),
        expected_output="text",
        parent_session_id=None,
        origin="operator",
    )

    logger.info(
        "codex spawn: session=%s working_dir=%s",
        session.session_id, working_dir,
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "task_id": task_id,
    }


def parse_codex_spawn_payload(content: str) -> dict:
    """Decode a pending-message body produced by :func:`spawn_codex_session`.

    Falls back to treating ``content`` as a bare prompt so legacy callers
    (and tests that pre-seed a string-only message) keep working.
    """
    try:
        data = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"prompt": content or "", "working_dir": None, "chat_id": None}
    if not isinstance(data, dict) or "prompt" not in data:
        return {"prompt": content or "", "working_dir": None, "chat_id": None}
    return {
        "prompt": str(data.get("prompt") or ""),
        "working_dir": data.get("working_dir"),
        "chat_id": data.get("chat_id"),
    }
