"""Inter-agent coordination tools (`lifeos_agent_*` family).

These tools let an in-flight agent session spawn, message, and coordinate with
other agent sessions — turning the worker into a multi-agent supervisor. The
core primitives are:

  - **spawn**: create a child session (Claude or local) with budget drawn from
    the caller's lineage budget. Returns immediately with a `child_session_id`.
  - **send**: append a user-role message to a peer/child session. For yielded
    sessions, the message is queued for the next resume.
  - **check**: non-blocking status snapshot of any session.
  - **yield_until**: terminate the caller's session until specified children
    reach a terminal state; on resume, children's outputs are injected as a
    new user turn. The primary coordination primitive — avoids idle billing
    on Managed Agents.
  - **kill**: terminate a descendant session.
  - **transcript_read**: read the JSONL transcript of any session.
  - **sessions_list**: filtered listing of recent sessions.

This module exposes the tools as plain Python functions taking an `InterAgentContext`
that bundles the worker's stores + caller's session_id. The `ToolRegistry`
(local executor) and `mcp_server.py` (managed agents) both wrap these into
tool definitions.

Security model: no-sandbox per AGENTS.md. The MCP-side wrapping passes
`caller_session_id` as a parameter — operators trust their own agents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_FAILED,
    STATUS_YIELDED,
    TERMINAL_STATUSES,
    SessionStore,
    new_session_id,
)
from api.services.agent_worker.transcript_store import TranscriptStore


logger = logging.getLogger(__name__)


# Caps enforced on spawn. Operator overrides via settings (see `Caps` dataclass).
DEFAULT_MAX_SPAWN_DEPTH = 3
DEFAULT_MAX_DESCENDANTS_PER_ROOT = 50
DEFAULT_MAX_CONCURRENT_LOCAL = 1
DEFAULT_MAX_CONCURRENT_MANAGED = 10


@dataclass
class Caps:
    """Concurrency / lineage limits applied to spawn."""
    max_spawn_depth: int = DEFAULT_MAX_SPAWN_DEPTH
    max_descendants_per_root: int = DEFAULT_MAX_DESCENDANTS_PER_ROOT
    max_concurrent_local: int = DEFAULT_MAX_CONCURRENT_LOCAL
    max_concurrent_managed: int = DEFAULT_MAX_CONCURRENT_MANAGED


@dataclass
class InterAgentContext:
    """Bundle of dependencies the tools need.

    Distributed by the local executor (in-process) or the worker (when
    receiving a managed-side MCP call) so the tool functions can operate
    without knowing how they were called. `managed_driver` is optional —
    when present, `kill` and other tools can reach remote managed sessions;
    when absent, managed targets are killed in the DB only.
    """
    session_store: SessionStore
    transcript_store: TranscriptStore
    caller_session_id: str
    caps: Caps
    managed_driver: Any | None = None


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _ok(payload: dict | None = None) -> dict:
    out = {"ok": True}
    if payload:
        out.update(payload)
    return out


def _err(message: str, code: str = "error") -> dict:
    return {"ok": False, "error": code, "message": message}


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format) — shared by local and managed agents.
# ---------------------------------------------------------------------------

INTER_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lifeos_agent_spawn",
        "description": "Spawn a child agent session that runs in parallel. Returns immediately with a `child_session_id` you can monitor with `lifeos_agent_check` or wait on with `lifeos_agent_yield_until`. Budget is drawn from your remaining lineage budget.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description for the child agent"},
                "model": {"type": "string", "enum": ["claude", "local"], "description": "Which executor to run the child on"},
                "max_dollars": {"type": "number", "description": "Optional per-child dollar budget"},
                "max_tokens": {"type": "integer", "description": "Optional per-child token budget"},
                "wall_seconds": {"type": "integer", "description": "Optional per-child wall-clock budget"},
                "expected_output": {"type": "string", "enum": ["text", "file", "external_action", "structured"]},
            },
            "required": ["prompt", "model"],
        },
    },
    {
        "name": "lifeos_agent_send",
        "description": "Append a user-role message to another agent session. For sessions that are yielded or sleeping, the message is queued and delivered on resume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "lifeos_agent_check",
        "description": "Non-blocking status of an agent session: current status, tokens/dollars used, last activity. Use for short-polling; prefer `lifeos_agent_yield_until` for waits >1 minute.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "lifeos_agent_yield_until",
        "description": "Pause yourself until all listed children reach a terminal state. Your current session ends; when the condition is met, a fresh resumed session is created with the children's outputs injected as a new user turn. This is the preferred wait primitive — no idle billing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "children": {"type": "array", "items": {"type": "string"}, "description": "session_ids to wait on"},
                "reason": {"type": "string", "description": "Why you're yielding (one short sentence)"},
            },
            "required": ["children"],
        },
    },
    {
        "name": "lifeos_agent_kill",
        "description": "Terminate a descendant session. Only allowed on sessions whose lineage descends from yours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "lifeos_agent_transcript_read",
        "description": "Read the event log (JSONL transcript) of any agent session — your own, a child's, or any sibling/peer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "since_turn": {"type": "integer", "description": "Optional: skip events before this index"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "lifeos_agent_sessions_list",
        "description": "List recent agent sessions, optionally filtered by status / routing / parent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "routing": {"type": "string", "enum": ["claude", "local"]},
                "parent_session_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def spawn(ctx: InterAgentContext, args: dict) -> dict:
    """Create a child session. Does NOT dispatch — the worker's next tick
    picks up the new session and routes it through the executors.
    """
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _err("prompt is required", code="invalid_arg")
    model = args.get("model")
    if model not in ("claude", "local"):
        return _err("model must be 'claude' or 'local'", code="invalid_arg")

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")

    # Cap: spawn depth.
    new_depth = (caller.spawn_depth or 0) + 1
    if new_depth > ctx.caps.max_spawn_depth:
        return _err(
            f"spawn depth {new_depth} exceeds cap {ctx.caps.max_spawn_depth}",
            code="cap_spawn_depth",
        )

    # Cap: descendants per root.
    root = caller.root_session_id or caller.session_id
    descendant_count = ctx.session_store.count_descendants(root)
    if descendant_count >= ctx.caps.max_descendants_per_root:
        return _err(
            f"root {root} already has {descendant_count} descendants "
            f"(cap {ctx.caps.max_descendants_per_root})",
            code="cap_descendants",
        )

    # Cap: concurrency per routing. The caller doesn't count toward its own
    # cap — the expected pattern is "parent calls spawn, then yield_until";
    # the parent's session is non-terminal at spawn time but will yield
    # immediately after.
    caller_excluded = 1 if caller.routing == model else 0
    if model == "local":
        active = ctx.session_store.count_active_by_routing("local") - caller_excluded
        if active >= ctx.caps.max_concurrent_local:
            return _err(
                f"{active} local sessions already running (cap {ctx.caps.max_concurrent_local})",
                code="cap_concurrency_local",
            )
    else:
        active = ctx.session_store.count_active_by_routing("claude") - caller_excluded
        if active >= ctx.caps.max_concurrent_managed:
            return _err(
                f"{active} managed sessions already running (cap {ctx.caps.max_concurrent_managed})",
                code="cap_concurrency_managed",
            )

    # Budget: child's max_dollars cannot exceed parent's remaining.
    parent_budget = caller.budget or {}
    spent = caller.total_dollars or 0.0
    parent_remaining = (parent_budget.get("max_dollars", 0.0) or 0.0) - spent
    requested_dollars = float(args.get("max_dollars") or parent_remaining)
    if requested_dollars > parent_remaining + 1e-6:
        return _err(
            f"requested ${requested_dollars:.2f} exceeds parent remaining ${parent_remaining:.2f}",
            code="budget_exceeded",
        )

    child_budget = {
        "wall_seconds": int(args.get("wall_seconds") or parent_budget.get("wall_seconds", 14400)),
        "max_tokens": int(args.get("max_tokens") or parent_budget.get("max_tokens", 500_000)),
        "max_dollars": float(requested_dollars),
    }
    expected_output = args.get("expected_output") or "text"

    # Create the session. We use a synthetic task_id since spawned sessions
    # don't have a backing #agent task in the user's task list.
    child_task_id = f"spawn_{new_session_id().removeprefix('sess_')}"
    child_session_id = new_session_id()
    ctx.session_store.create(
        task_id=child_task_id,
        session_id=child_session_id,
        status=STATUS_CLAIMED,
        routing=model,
        budget=child_budget,
        expected_output=expected_output,
        parent_session_id=caller.session_id,
        root_session_id=root,
        spawn_depth=new_depth,
    )
    # The prompt becomes the child's task description (used by the executor's
    # _seed_conversation) so the system prompt + inter-agent guidance run as
    # normal. We also stash the prompt in pending_messages as a fallback for
    # the worker's task lookup (children have no API-backed task).
    ctx.session_store.enqueue_message(child_session_id, caller.session_id, prompt)
    ctx.transcript_store.append(child_session_id, "spawn", {
        "parent_session_id": caller.session_id,
        "root_session_id": root,
        "spawn_depth": new_depth,
        "model": model,
        "prompt_chars": len(prompt),
    })
    ctx.transcript_store.append(caller.session_id, "spawned_child", {
        "child_session_id": child_session_id,
        "model": model,
    })
    logger.info("spawn: caller=%s child=%s model=%s depth=%d",
                caller.session_id, child_session_id, model, new_depth)
    return _ok({
        "child_session_id": child_session_id,
        "task_id": child_task_id,
        "budget": child_budget,
    })


def send(ctx: InterAgentContext, args: dict) -> dict:
    target_id = args.get("session_id", "").strip()
    message = (args.get("message") or "").strip()
    if not target_id or not message:
        return _err("session_id and message are required", code="invalid_arg")
    target = ctx.session_store.get_by_session_id(target_id)
    if target is None:
        return _err(f"session {target_id} not found", code="not_found")
    if target.status in TERMINAL_STATUSES:
        return _err(f"session {target_id} is terminal ({target.status})", code="terminal")

    # Same-root lineage check — agents can't inject messages into unrelated
    # sessions. Matches the security model of `kill` and `yield_until`.
    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")
    caller_root = caller.root_session_id or caller.session_id
    target_root = target.root_session_id or target.session_id
    if target_root != caller_root:
        return _err(
            f"session {target_id} is not in your lineage",
            code="forbidden",
        )

    # Always queue. For an actively-running local session, the executor picks
    # up pending messages at the start of each turn. For a yielded session,
    # delivery happens on resume.
    msg_id = ctx.session_store.enqueue_message(target_id, ctx.caller_session_id, message)
    ctx.transcript_store.append(target_id, "inter_agent_send", {
        "from": ctx.caller_session_id, "chars": len(message),
    })
    return _ok({"delivered": True, "message_id": msg_id, "queued": target.status == STATUS_YIELDED})


def check(ctx: InterAgentContext, args: dict) -> dict:
    target_id = args.get("session_id", "").strip()
    if not target_id:
        return _err("session_id is required", code="invalid_arg")
    target = ctx.session_store.get_by_session_id(target_id)
    if target is None:
        return _err(f"session {target_id} not found", code="not_found")
    tokens = (target.total_input_tokens or 0) + (target.total_output_tokens or 0)
    return _ok({
        "session_id": target_id,
        "status": target.status,
        "routing": target.routing,
        "tokens_used": tokens,
        "dollars_used": float(target.total_dollars or 0.0),
        "last_activity_at": target.last_activity_at,
        "parent_session_id": target.parent_session_id,
    })


def yield_until(ctx: InterAgentContext, args: dict) -> dict:
    children_raw = args.get("children") or []
    if not isinstance(children_raw, list) or not children_raw:
        return _err("children must be a non-empty list of session_ids", code="invalid_arg")
    children = [str(c) for c in children_raw if isinstance(c, str)]
    if not children:
        return _err("no valid session_ids in children", code="invalid_arg")

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")

    # Verify all listed children exist and are descendants of caller's lineage
    # (so an agent can't yield on someone else's family).
    children_sessions = ctx.session_store.list_by_session_ids(children)
    found_ids = {s.session_id for s in children_sessions}
    missing = [c for c in children if c not in found_ids]
    if missing:
        return _err(f"unknown children: {missing}", code="not_found")
    root = caller.root_session_id or caller.session_id
    foreign = [s.session_id for s in children_sessions if s.root_session_id != root]
    if foreign:
        return _err(f"children {foreign} are not in your lineage", code="forbidden")

    ctx.session_store.set_yield_waiting_for(caller.task_id, children)
    ctx.session_store.update_status(caller.task_id, STATUS_YIELDED)
    ctx.transcript_store.append(caller.session_id, "yield", {
        "children": children, "reason": args.get("reason", ""),
    })
    return _ok({"yielded": True, "waiting_on": children})


def kill(ctx: InterAgentContext, args: dict) -> dict:
    target_id = args.get("session_id", "").strip()
    if not target_id:
        return _err("session_id is required", code="invalid_arg")
    target = ctx.session_store.get_by_session_id(target_id)
    if target is None:
        return _err(f"session {target_id} not found", code="not_found")

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")
    caller_root = caller.root_session_id or caller.session_id
    if target.root_session_id != caller_root or target.session_id == caller.session_id:
        return _err("can only kill descendants of your own root", code="forbidden")
    if target.status in TERMINAL_STATUSES:
        return _ok({"killed": False, "reason": f"already {target.status}"})

    # Managed children: kill the remote session too so it stops accruing
    # tokens / session-hour overhead. Without a driver we can only flip the
    # local DB status; the worker's next managed poll will then see the
    # local FAILED status and treat the session as terminal even if the
    # remote still reports running.
    if target.managed_agent_session_id and ctx.managed_driver is not None:
        try:
            ctx.managed_driver.kill_session(
                target.managed_agent_session_id,
                reason=args.get("reason", ""),
            )
        except Exception as exc:
            logger.warning("kill_session %s failed: %s",
                           target.managed_agent_session_id, exc)

    ctx.session_store.update_status(target.task_id, STATUS_FAILED)
    ctx.transcript_store.append(target_id, "killed", {
        "by": caller.session_id, "reason": args.get("reason", ""),
        "managed_remote": target.managed_agent_session_id,
    })
    return _ok({"killed": True})


def transcript_read(ctx: InterAgentContext, args: dict) -> dict:
    target_id = args.get("session_id", "").strip()
    if not target_id:
        return _err("session_id is required", code="invalid_arg")
    since_turn = int(args.get("since_turn") or 0)
    events = ctx.transcript_store.read(target_id)
    if since_turn:
        events = events[since_turn:]
    return _ok({"session_id": target_id, "events": events, "count": len(events)})


def sessions_list(ctx: InterAgentContext, args: dict) -> dict:
    sessions = ctx.session_store.list_sessions(
        status=args.get("status"),
        routing=args.get("routing"),
        parent_session_id=args.get("parent_session_id"),
        limit=int(args.get("limit") or 200),
    )
    return _ok({
        "sessions": [
            {
                "session_id": s.session_id,
                "task_id": s.task_id,
                "status": s.status,
                "routing": s.routing,
                "parent_session_id": s.parent_session_id,
                "root_session_id": s.root_session_id,
                "tokens_used": (s.total_input_tokens or 0) + (s.total_output_tokens or 0),
                "dollars_used": float(s.total_dollars or 0.0),
                "started_at": s.started_at,
                "last_activity_at": s.last_activity_at,
            }
            for s in sessions
        ],
        "count": len(sessions),
    })


# ---------------------------------------------------------------------------
# Public dispatcher — used by ToolRegistry and the MCP wrapper.
# ---------------------------------------------------------------------------

DISPATCH_TABLE = {
    "lifeos_agent_spawn": spawn,
    "lifeos_agent_send": send,
    "lifeos_agent_check": check,
    "lifeos_agent_yield_until": yield_until,
    "lifeos_agent_kill": kill,
    "lifeos_agent_transcript_read": transcript_read,
    "lifeos_agent_sessions_list": sessions_list,
}


def is_inter_agent_tool(name: str) -> bool:
    return name in DISPATCH_TABLE


def dispatch(ctx: InterAgentContext, name: str, args: dict) -> dict:
    handler = DISPATCH_TABLE.get(name)
    if handler is None:
        return _err(f"unknown inter-agent tool: {name}", code="unknown_tool")
    try:
        return handler(ctx, args or {})
    except Exception as exc:
        logger.exception("inter-agent tool %s crashed: %s", name, exc)
        return _err(f"{name} crashed: {exc}", code="crashed")
