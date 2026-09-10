"""Inter-agent coordination tools (`lifeos_agent_*` family).

These tools let an in-flight agent session spawn, message, and coordinate with
other agent sessions — turning the worker into a multi-agent supervisor. The
core primitives are:

  - **spawn**: create a child session on a canonical worker executor with budget
    drawn from the caller's lineage budget. Returns immediately with a
    `child_session_id`.
  - **send**: append a user-role message to a peer/child session. For yielded
    sessions, the message is queued for the next resume. Sending to your own
    completed CLI child reopens it (the message becomes its next turn, resumed
    with full prior context) — the answer path for a child that completed with
    a "[needs clarification]" question (#356 follow-up).
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

import hashlib
import hmac
import logging
import math
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

from api.services.agent_worker import doctor_repair
from api.services.agent_worker.hermes_session import HERMES_ROUTING
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_YIELDED,
    TERMINAL_STATUSES,
    Session,
    SessionStore,
    new_session_id,
)
from api.services.agent_worker.transcript_store import TranscriptStore


logger = logging.getLogger(__name__)


def caller_proof_for_session(session_id: str, secret: str) -> str:
    """Create the transport proof binding an MCP call to one worker session.

    The session id is useful routing metadata, but is not itself authority:
    remote MCP callers must also present this HMAC proof. The secret is the
    already-authenticated MCP transport secret and is never persisted.
    """
    if not isinstance(session_id, str) or not session_id.strip() or not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"), session_id.strip().encode("utf-8"), hashlib.sha256,
    ).hexdigest()


# Engines an in-flight agent may spawn a child on. `claude`/`local` are the
# in-process routes (Managed Agents API / Gemma); `claude_code`/`codex` are the
# CLI routes, used for capability fallback (browser/GUI, native computer use).
# CLI routes are subscription-billed, so they skip the per-token dollar ceiling
# and reuse the managed concurrency cap.
CLI_ROUTINGS = ("claude_code", "codex")
SPAWN_MODELS = ("claude", "local", "remote", "hermes", *CLI_ROUTINGS)

# Root routings that must never be allowed to spawn an API-billed
# (`model="claude"`) child — see the `model == "claude"` guard in `spawn()`
# below. This is deliberately a SEPARATE set from `CLI_ROUTINGS`: that one
# also gates behavior specific to an actual CLI subprocess (spawn's own
# dollar-ceiling skip, `send()`'s reopen-on-send, `kill()`'s local-subprocess
# teardown) that doesn't apply to Hermes — Hermes is an external agent
# harness, not a CLI child this worker manages. Hermes belongs here for the
# same reason claude_code/codex do: it's not billed via the Anthropic API,
# so a Hermes-rooted lineage opening `model="claude"` would be an
# undisclosed API-billed side door (#640, extending #578 / ADR-018).
NON_API_BILLED_ROOT_ROUTINGS = CLI_ROUTINGS + (HERMES_ROUTING,)


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

    `worker_handle` is optional; when present, `lifeos_agent_user_ask` can route
    a clarifying question through the worker's Telegram pipeline.
    """
    session_store: SessionStore
    transcript_store: TranscriptStore
    caller_session_id: str
    caps: Caps
    managed_driver: Any | None = None
    worker_handle: Any | None = None  # Worker — circular import avoided


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


def _repair_workflow_for(ctx: "InterAgentContext", caller: Session) -> str | None:
    """The repair a spawn would execute for: the caller's own workflow, or
    its root's. Read from the root so an intermediate child cannot launder a
    dispatch past the repair's approval gate."""
    if caller.workflow_id:
        return caller.workflow_id
    root_id = caller.root_session_id or caller.session_id
    if root_id == caller.session_id:
        return None
    root = ctx.session_store.get_by_session_id(root_id)
    return root.workflow_id if root else None


def _update_status_for_session(
    session_store: SessionStore, session: Session, status: str,
) -> bool:
    """Write status only for this exact lifecycle attempt/turn.

    The store keeps the identity arguments optional for old callers and rows,
    but inter-agent mutations always pass the snapshot they read.  The store's
    durable cancellation fence then makes a late yield/block write a no-op.
    """
    result = session_store.update_status(
        session.task_id,
        status,
        attempt_id=getattr(session, "attempt_id", None),
        turn_id=getattr(session, "turn_id", None),
    )
    # SessionStore returns a bool on lifecycle-aware deployments. Treat a
    # legacy store's ``None`` return as success so old adapters retain their
    # historical behavior; only an explicit False means the fence rejected
    # this exact snapshot.
    return result is not False


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format) — shared by local and managed agents.
# ---------------------------------------------------------------------------

# Every inter-agent tool requires `caller_session_id` — the calling agent's
# own session id, which it should pass from the `lifeos_session_id` field
# in its task brief / user message. The MCP HTTP layer (cloud path) can't
# infer this server-side because the request crosses a process boundary;
# the local dispatcher does inject it from context, overriding whatever
# the agent passes. Declaring it required in the schema teaches the cloud
# agent to include it.
_CALLER_PROP = {
    "type": "string",
    "description": "Your own session_id, copied verbatim from the "
                   "`lifeos_session_id=` field in your task brief.",
}
_CALLER_PROOF_PROP = {
    "type": "string",
    "description": "Transport proof binding caller_session_id to this MCP connection.",
}


def _with_caller(props: dict, required: list[str]) -> dict:
    """Inject `caller_session_id` into a tool schema."""
    new_props = {
        "caller_session_id": _CALLER_PROP,
        "caller_proof": _CALLER_PROOF_PROP,
        **props,
    }
    return {
        "type": "object",
        "properties": new_props,
        "required": ["caller_session_id", "caller_proof"] + list(required),
    }


INTER_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lifeos_agent_spawn",
        "description": "Spawn a child agent session that runs in parallel. Returns immediately with a `child_session_id` you can monitor with `lifeos_agent_check` or wait on with `lifeos_agent_yield_until`. Budget is drawn from your remaining lineage budget. Omit the route to inherit an active scoped override (or the caller route). Use `claude_code` to delegate work that needs a real browser / GUI automation — its `--chrome` browser works headless, unlike Codex's (whose computer use is a desktop-app-only feature, unavailable here). For legacy `model=claude_code` children, `tier` selects a Claude tier; when omitted, the CLI configured default is used.",
        "input_schema": _with_caller({
            "prompt": {"type": "string", "description": "Task description for the child agent"},
            "model": {"type": "string", "enum": ["claude", "local", "remote", "hermes", "claude_code", "codex"], "description": "Legacy executor selector; canonical callers may instead supply execution.executor."},
            "tier": {"type": "string", "enum": ["haiku", "sonnet", "opus"], "description": "Legacy model=claude_code only: which Claude tier the child CLI runs. When omitted, the CLI configured default is used. Ignored for other engines."},
            "max_dollars": {"type": "number", "description": "Optional per-child dollar budget"},
            "max_tokens": {"type": "integer", "description": "Optional per-child token budget"},
            "wall_seconds": {"type": "integer", "description": "Optional per-child wall-clock budget"},
            "expected_output": {"type": "string", "enum": ["text", "file", "external_action", "structured"]},
            "execution": {
                "type": "object",
                "description": "Canonical execution choices; provider/runtime and lineage are server-derived.",
                "properties": {
                    "executor": {"type": "string", "enum": ["local", "remote", "claude", "hermes", "claude_code", "codex"]},
                    "model_id": {"type": "string"},
                    "effort": {"type": "string", "enum": ["low", "medium", "high", "max"]},
                    "host": {"type": "string"},
                    "working_dir": {"type": "string"},
                    "budget": {"type": "object"},
                    "constraints": {"type": "object"},
                },
                "additionalProperties": False,
            },
        }, required=["prompt"]),
    },
    {
        "name": "lifeos_agent_send",
        "description": "Append a user-role message to another agent session. For sessions that are yielded or sleeping, the message is queued and delivered on resume. Sending to your own COMPLETED claude_code/codex child REOPENS it: your message becomes its next turn and it resumes with its full prior context. Use this to answer a child whose output contains '[needs clarification] ...', then wait on it again with `lifeos_agent_yield_until` (send the answer BEFORE yielding).",
        "input_schema": _with_caller({
            "session_id": {"type": "string"},
            "message": {"type": "string"},
        }, required=["session_id", "message"]),
    },
    {
        "name": "lifeos_agent_check",
        "description": "Non-blocking status of an agent session: current status, tokens/dollars used, last activity. Use for short-polling; prefer `lifeos_agent_yield_until` for waits >1 minute.",
        "input_schema": _with_caller({
            "session_id": {"type": "string"},
        }, required=["session_id"]),
    },
    {
        "name": "lifeos_agent_yield_until",
        "description": "Pause yourself until all listed children reach a terminal state. Your current session ends; when the condition is met, a fresh resumed session is created with the children's outputs injected as a new user turn. This is the preferred wait primitive — no idle billing.",
        "input_schema": _with_caller({
            "children": {"type": "array", "items": {"type": "string"}, "description": "session_ids to wait on"},
            "reason": {"type": "string", "description": "Why you're yielding (one short sentence)"},
        }, required=["children"]),
    },
    {
        "name": "lifeos_agent_kill",
        "description": "Terminate a descendant session. Only allowed on sessions whose lineage descends from yours.",
        "input_schema": _with_caller({
            "session_id": {"type": "string"},
            "reason": {"type": "string"},
        }, required=["session_id"]),
    },
    {
        "name": "lifeos_agent_transcript_read",
        "description": "Read the event log (JSONL transcript) of any agent session — your own, a child's, or any sibling/peer.",
        "input_schema": _with_caller({
            "session_id": {"type": "string"},
            "since_turn": {"type": "integer", "description": "Optional: skip events before this index"},
        }, required=["session_id"]),
    },
    {
        "name": "lifeos_agent_sessions_list",
        "description": "List recent agent sessions, optionally filtered by status / routing / parent.",
        "input_schema": _with_caller({
            "status": {"type": "string"},
            "routing": {"type": "string", "enum": ["claude", "local", "remote", "hermes", "claude_code", "codex"]},
            "parent_session_id": {"type": "string"},
            "limit": {"type": "integer"},
        }, required=[]),
    },
    {
        "name": "lifeos_agent_user_ask",
        "description": "Ask the operator a clarifying question via Telegram and pause until they reply. Your session ends; the worker resumes it (with the user's answer injected as a new user turn) once the reply arrives. Use sparingly — only when you genuinely cannot proceed without operator input.",
        "input_schema": _with_caller({
            "question": {"type": "string", "description": "The question to ask the operator. Keep it short and specific."},
        }, required=["question"]),
    },
    {
        "name": "lifeos_agent_execution_override",
        "description": (
            "Set or clear a temporary execution override for future resolution. "
            "Session scope affects only your session; lineage scope is root-only "
            "and affects future resolutions in that lineage. Already-resolved "
            "execution snapshots never change."
        ),
        "input_schema": _with_caller({
            "operation": {"type": "string", "enum": ["set", "clear"]},
            "scope": {"type": "string", "enum": ["session", "lineage"]},
            "execution": {
                "type": "object",
                "description": "Route/model/effort/host/working-directory values to inherit.",
                "properties": {
                    "executor": {"type": "string", "enum": ["local", "remote", "claude", "hermes", "claude_code", "codex"]},
                    "model_id": {"type": "string"},
                    "effort": {"type": "string", "enum": ["low", "medium", "high", "max"]},
                    "host": {"type": "string"},
                    "working_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "expires_at": {
                "type": "string",
                "description": "Optional ISO-8601 expiry; applies only to future resolutions.",
            },
        }, required=["operation", "scope"]),
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
    from dataclasses import asdict, replace
    from api.services.agent_worker.execution import (
        Budget as ExecutionBudget,
        ExecutionRequest,
        parse_execution_request,
        unsupported_explicit_fields,
    )

    canonical_raw = args.get("execution")
    legacy_model = args.get("model")
    if canonical_raw is not None:
        parse_payload = canonical_raw
        if (
            isinstance(canonical_raw, dict)
            and legacy_model
            and "executor" not in canonical_raw
        ):
            parse_payload = {**canonical_raw, "executor": legacy_model}
        parsed = parse_execution_request(parse_payload)
        if not parsed.ok:
            return _err(
                "; ".join(item.message for item in parsed.diagnostics),
                code="invalid_execution",
            )
        execution_request = parsed.request
    else:
        execution_request = ExecutionRequest(
            executor=legacy_model if legacy_model in SPAWN_MODELS else None,
        )

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")
    inherited_override = ctx.session_store.get_execution_override(
        session_id=caller.session_id,
        root_session_id=caller.root_session_id or caller.session_id,
    )
    # An omitted child route is the reachable consumer for a bounded temporary
    # override. The session row keeps the caller route as its fallback so an
    # override that expires before resolution does not become permanent.
    model = (
        legacy_model
        or execution_request.executor
        or (inherited_override.executor if inherited_override else None)
        or caller.routing
    )
    if model not in SPAWN_MODELS:
        return _err(
            "model or execution.executor must name a supported executor",
            code="invalid_arg",
        )
    # Optional Claude tier for claude_code children (#349). Ignored for other
    # engines so the caller can pass it uniformly without an error.
    tier = (args.get("tier") or "").strip().lower() or None
    if tier and tier not in ("haiku", "sonnet", "opus"):
        return _err(
            "tier must be one of 'haiku', 'sonnet', 'opus'",
            code="invalid_arg",
        )
    claude_code_model = tier if legacy_model == "claude_code" else None

    if canonical_raw is not None:
        if execution_request.executor and execution_request.executor != model:
            return _err("model engine conflicts with execution.executor", code="execution_conflict")
        if execution_request.model_id and tier:
            return _err("tier conflicts with execution.model_id", code="execution_conflict")
        unsupported = unsupported_explicit_fields(execution_request, model)
        if unsupported:
            return _err(
                "; ".join(item.message for item in unsupported),
                code="unsupported_execution_field",
            )
    provisional_routing = (
        model if legacy_model or execution_request.executor else caller.routing
    )

    # A subscription-billed lineage must not be able to open an API-billed side
    # door. `model="claude"` runs the child on Managed Agents — the Anthropic
    # API — so a lineage rooted in a CLI session (the doctor, and every other
    # orchestrator the worker drives through Claude Code) is refused it (#578).
    # Read from the ROOT, not the caller, so an intermediate local child can't
    # launder the spawn. This is what makes "CLI routes are subscription-billed"
    # above a fact rather than an assumption — the sibling half is the
    # executor's env strip (ClaudeCodeExecutor._clean_env), which denies the CLI
    # itself any API credential.
    if model in {"claude", "remote"}:
        root_session = ctx.session_store.get_by_session_id(
            caller.root_session_id or caller.session_id
        ) or caller
        if root_session.routing in NON_API_BILLED_ROOT_ROUTINGS:
            return _err(
                f"this lineage is subscription-billed (root session routing="
                f"{root_session.routing}); model='{model}' is metered. "
                f"Use model='claude_code' (with tier=) for a "
                f"Claude child, or 'local' for the on-box model.",
                code="api_billing_blocked",
            )

    # The doctor's single human gate, enforced at LifeOS's own dispatch
    # boundary. Spawning a child is how a repair gets implementation work
    # done, so it requires an approved goal revision. The supervisor's own
    # session keeps running throughout, which is what leaves read-only
    # diagnosis available before approval; this gate governs what LifeOS
    # dispatches, not what a running CLI process is permitted to do.
    repair_workflow_id = _repair_workflow_for(ctx, caller)
    if repair_workflow_id:
        decision = doctor_repair.dispatch_allowed(
            ctx.session_store.get_repair(repair_workflow_id), "implement",
        )
        if not decision.allowed:
            return _err(
                f"repair {repair_workflow_id} cannot dispatch implementation "
                f"work: {decision.reason}. Propose a goal and get it approved "
                f"before spawning a worker to implement it.",
                code=decision.reason,
            )

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
        # claude (managed) and the CLI routes (claude_code/codex) share the
        # managed concurrency cap, counted per their own routing.
        active = ctx.session_store.count_active_by_routing(model) - caller_excluded
        if active >= ctx.caps.max_concurrent_managed:
            return _err(
                f"{active} {model} sessions already running (cap {ctx.caps.max_concurrent_managed})",
                code="cap_concurrency_managed",
            )

    # Resolve canonical/legacy budget once, then enforce every child ceiling.
    parent_budget = caller.budget or {}
    spent = caller.total_dollars or 0.0
    parent_remaining = (parent_budget.get("max_dollars", 0.0) or 0.0) - spent
    if execution_request.budget is not None and any(
        args.get(name) is not None for name in ("max_dollars", "max_tokens", "wall_seconds")
    ):
        return _err("legacy budget fields conflict with execution.budget", code="execution_conflict")
    requested = execution_request.budget or ExecutionBudget(
        wall_seconds=args.get("wall_seconds"), max_tokens=args.get("max_tokens"),
        max_dollars=args.get("max_dollars"),
    )
    parent_wall = int(parent_budget.get("wall_seconds", 14400))
    parent_tokens = int(parent_budget.get("max_tokens", 500_000))
    wall = parent_wall if requested.wall_seconds is None else requested.wall_seconds
    tokens = parent_tokens if requested.max_tokens is None else requested.max_tokens
    dollars = parent_remaining if requested.max_dollars is None else requested.max_dollars
    if (
        isinstance(wall, bool) or not isinstance(wall, int) or wall < 0
        or isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        or isinstance(dollars, bool) or not isinstance(dollars, (int, float))
        or not math.isfinite(float(dollars)) or dollars < 0
    ):
        return _err("child budget values must be finite and non-negative", code="invalid_budget")
    if wall > parent_wall or tokens > parent_tokens:
        return _err("child token/wall budget exceeds parent budget", code="budget_exceeded")
    # Subscription-backed CLI children still inherit the parent's canonical
    # ceiling. A route's billing class may affect pricing, but it must never
    # become a budget-bypass side door for a canonical request.
    if dollars > parent_remaining + 1e-6:
        return _err(
            f"requested ${dollars:.2f} exceeds parent remaining ${parent_remaining:.2f}",
            code="budget_exceeded",
        )
    child_budget = {
        "wall_seconds": wall, "max_tokens": tokens,
        "max_dollars": float(dollars),
    }
    execution_request = replace(
        execution_request, budget=ExecutionBudget(**child_budget),
    )
    expected_output = args.get("expected_output") or "text"

    # Create the session. We use a synthetic task_id since spawned sessions
    # don't have a backing #agent task in the user's task list.
    child_task_id = f"spawn_{new_session_id().removeprefix('sess_')}"
    child_session_id = new_session_id()
    ctx.session_store.create(
        task_id=child_task_id,
        session_id=child_session_id,
        status=STATUS_CLAIMED,
        routing=provisional_routing,
        budget=child_budget,
        expected_output=expected_output,
        parent_session_id=caller.session_id,
        root_session_id=root,
        spawn_depth=new_depth,
        claude_code_model=claude_code_model,
        # #684 review: inherit the caller's bot ownership (e.g. "doctor" for
        # a Hermes doctor-persona conversation, via hermes_session.py) so the
        # worker's own status/blocked/completion notices for this child
        # route to the same Telegram bot the caller answers on, and that
        # bot's threaded-reply resume (scoped to its own `bot`) can find
        # them. `caller.bot` is already `None` for a primary-rooted lineage
        # and for every pre-existing (pre-#684) Hermes/CLI root, so this is
        # additive — a lineage that never had bot ownership still doesn't.
        bot=caller.bot,
        model=(caller.model if caller.routing == provisional_routing else None),
        effort=(caller.effort if caller.routing == provisional_routing else None),
        execution_request=asdict(execution_request),
        # A child executes for the same repair as its lineage, so the repair
        # record sees every session it owns without a second workflow.
        workflow_id=repair_workflow_id,
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
        "tier": claude_code_model,
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


def execution_override(ctx: InterAgentContext, args: dict) -> dict:
    """Set or clear an override whose identity comes only from the caller."""
    from datetime import datetime, timezone

    from api.services.agent_worker.execution import (
        TemporaryOverride,
        parse_execution_request,
        unsupported_explicit_fields,
    )

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")
    operation = args.get("operation")
    scope = args.get("scope")
    if operation not in {"set", "clear"} or scope not in {"session", "lineage"}:
        return _err("operation and scope are required", code="invalid_arg")

    root_id = caller.root_session_id or caller.session_id
    if scope == "lineage" and caller.session_id != root_id:
        return _err("only the lineage root can change a lineage override", code="forbidden")
    scope_id = caller.session_id if scope == "session" else root_id
    if operation == "clear":
        if args.get("execution") is not None or args.get("expires_at") is not None:
            return _err("clear does not accept execution or expires_at", code="invalid_arg")
        ctx.session_store.clear_execution_override(scope=scope, scope_id=scope_id)
        return _ok({"cleared": True, "scope": scope})

    raw = args.get("execution")
    if raw is None:
        return _err("execution is required for operation=set", code="invalid_arg")
    parsed = parse_execution_request(raw)
    if not parsed.ok:
        return _err(
            "; ".join(item.message for item in parsed.diagnostics),
            code="invalid_execution",
        )
    request = parsed.request
    if request.budget is not None or request.constraints != request.constraints.__class__():
        return _err(
            "temporary overrides support executor, model_id, effort, host, and working_dir only",
            code="unsupported_execution_field",
        )
    route_dependent = (request.model_id, request.effort, request.host, request.working_dir)
    if request.executor is None and any(value is not None for value in route_dependent):
        return _err(
            "temporary override fields require execution.executor",
            code="invalid_execution",
        )
    if request.executor is None:
        return _err("temporary override must set at least one field", code="invalid_execution")
    unsupported = unsupported_explicit_fields(request)
    if unsupported:
        return _err(
            "; ".join(item.message for item in unsupported),
            code="unsupported_execution_field",
        )
    expires_at = None
    if args.get("expires_at") is not None:
        try:
            expires_at = datetime.fromisoformat(str(args["expires_at"]))
            if expires_at.tzinfo is None:
                raise ValueError("timezone required")
        except (TypeError, ValueError):
            return _err("expires_at must be a timezone-aware ISO-8601 timestamp", code="invalid_arg")
    now = datetime.now(timezone.utc)
    override = TemporaryOverride(
        scope=scope,
        scope_id=scope_id,
        created_at=now,
        executor=request.executor,
        model_id=request.model_id,
        effort=request.effort,
        host=request.host,
        working_dir=request.working_dir,
        expires_at=expires_at,
    )
    try:
        ctx.session_store.set_execution_override(override)
    except (TypeError, ValueError) as exc:
        return _err(str(exc), code="invalid_arg")
    return _ok({"override": override.to_dict()})


def send(ctx: InterAgentContext, args: dict) -> dict:
    target_id = args.get("session_id", "").strip()
    message = (args.get("message") or "").strip()
    if not target_id or not message:
        return _err("session_id and message are required", code="invalid_arg")
    target = ctx.session_store.get_by_session_id(target_id)
    if target is None:
        return _err(f"session {target_id} not found", code="not_found")
    # Reopen-on-send (#356 follow-up): a spawned CLI child that hits a genuine
    # fork folds "[needs clarification] …" into its output and COMPLETES (a
    # BLOCKED child would strand its yielded parent). But its CLI session
    # persists on disk under claude_code_session_id, so the direct parent can
    # answer by just sending: the message is queued as the child's next turn
    # and the status flips back to CLAIMED, which the spawned-session
    # dispatcher resumes via `-r <claude_code_session_id>` — full prior
    # context, no re-derivation. Restricted to COMPLETED (a FAILED child has
    # nothing coherent to continue), to CLI routings (local/managed have no
    # on-disk resume path here), and to a persisted CLI session id (`-r`
    # needs the UUID; without it the child would strand CLAIMED).
    reopen = (
        target.status == STATUS_COMPLETED
        and target.parent_session_id == ctx.caller_session_id
        and target.routing in CLI_ROUTINGS
        and bool(target.claude_code_session_id)
    )
    if target.status in TERMINAL_STATUSES and not reopen:
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

    # A reopen is a new immutable execution attempt.  Rotate the attempt
    # before enqueueing so a queued callback from the cancelled attempt can
    # never consume this message or publish its late result into the new one.
    if reopen:
        try:
            target = ctx.session_store.begin_new_execution(target.task_id)
        except ValueError:
            # The terminal snapshot was superseded while this call was in
            # flight (most commonly by cancellation). Do not turn that race
            # into a successful reopen.
            current = ctx.session_store.get_by_session_id(target_id)
            return _err(
                f"session {target_id} is no longer reopenable "
                f"({current.status if current else 'missing'})",
                code="terminal",
            )

    # Always queue. For an actively-running local session, the executor picks
    # up pending messages at the start of each turn. For a yielded session,
    # delivery happens on resume.
    msg_id = ctx.session_store.enqueue_message(
        target_id, ctx.caller_session_id, message,
        attempt_id=target.attempt_id, turn_id=target.turn_id,
    )
    if not msg_id:
        return _err(
            f"session {target_id} is no longer active",
            code="cancelled",
        )
    if reopen:
        # Flip AFTER the enqueue so the dispatch tick can never claim the
        # child before its resume message exists (an empty resume prompt).
        if not _update_status_for_session(ctx.session_store, target, STATUS_CLAIMED):
            return _err(
                f"session {target_id} is no longer active",
                code="cancelled",
            )
    ctx.transcript_store.append(target_id, "inter_agent_send", {
        "from": ctx.caller_session_id, "chars": len(message),
    })
    if reopen:
        ctx.transcript_store.append(target_id, "inter_agent_send_reopen", {
            "from": ctx.caller_session_id,
            "claude_code_session_id": target.claude_code_session_id,
        })
        return _ok({"delivered": True, "message_id": msg_id, "reopened": True})
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

    # Validate the native continuation before mutating either the wait list or
    # status.  An old/unknown backend must fail closed instead of leaving the
    # caller permanently yielded for a worker path that can only fall through
    # to LocalExecutor (which is forbidden for child waits).
    from api.services.agent_worker.executor_lifecycle import route_supports_resume_after_children
    if not route_supports_resume_after_children(caller.routing):
        return _err(
            "executor cannot resume after children",
            code="unsupported_resume",
        )

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

    if ctx.session_store.set_yield_waiting_for(
        caller.task_id,
        children,
        attempt_id=getattr(caller, "attempt_id", None),
        turn_id=getattr(caller, "turn_id", None),
    ) is False:
        return _err(
            f"session {caller.session_id} is no longer active",
            code="cancelled",
        )
    if not _update_status_for_session(ctx.session_store, caller, STATUS_YIELDED):
        return _err(
            f"session {caller.session_id} is no longer active",
            code="cancelled",
        )
    ctx.transcript_store.append(caller.session_id, "yield", {
        "children": children, "reason": args.get("reason", ""),
    })

    # For cloud callers: kill the remote Anthropic session NOW. Without
    # this the cloud agent keeps running on Anthropic's side after the
    # tool returns — the tool result `{"yielded":true}` doesn't break
    # the server's generation loop, so the agent will do "extra"
    # post-yield work (which is what caused the 30KB threads-JSON dump
    # to become a user-facing completion message in the multi-agent
    # test of 2026-05-27). When children finish, the worker creates a
    # *fresh* managed session for the resume — see
    # `_resume_yielded_for_children` in worker.py.
    if caller.routing == "claude" and caller.managed_agent_session_id:
        if ctx.managed_driver is not None:
            try:
                ctx.managed_driver.kill_session(
                    caller.managed_agent_session_id,
                    reason="yield_until",
                )
                ctx.transcript_store.append(
                    caller.session_id, "yield_killed_remote",
                    {"remote_id": caller.managed_agent_session_id},
                )
            except Exception as exc:
                # Non-fatal: the cloud agent may have already exited the
                # turn naturally. Log and continue — the worker's resume
                # path doesn't depend on the kill succeeding.
                logger.warning(
                    "yield_until kill_session failed for %s: %s",
                    caller.managed_agent_session_id, exc,
                )
        else:
            # Driver not wired into the context. Worker-side ticks will
            # see this session as yielded; the next poll cycle of the
            # remote session will eventually pick up the end_turn that
            # Anthropic emits naturally — but until then the cloud agent
            # may keep working post-yield.
            logger.warning(
                "yield_until: no managed_driver in context to kill remote "
                "session %s — cloud agent may continue running post-yield",
                caller.managed_agent_session_id,
            )

    return _ok({"yielded": True, "waiting_on": children})


# How long to wait between a SIGTERM and the follow-up SIGKILL when reaping a
# local CLI subprocess (#379) — a brief grace for the process to exit cleanly.
_LOCAL_KILL_GRACE_S = 2.0
_LOCAL_KILL_POLL_S = 0.1


def _kill_remote_subprocess(
    transcript_store: TranscriptStore, target: Session, *, remote_kill_runner=None,
) -> None:
    """(#851) Best-effort terminate a CLI subprocess a `claude_code`/`codex`
    executor spawned on a board-assigned HOST other than the API host.

    Signalling a local pid/pgid (as `_kill_local_subprocess` does) can't
    reach a remote process — the executor recorded the REMOTE process
    group id instead (`session.remote_pgid`, echoed by the ssh wrapper's
    first stdout line; see `remote_spawn.build_remote_argv`). This runs
    `ssh <target> kill -- -<pgid>` through an injectable runner (production
    default: a real `subprocess.run`; tests inject a fake) — see
    `remote_spawn.kill_remote_process_group`.

    Best-effort by contract, same as `_kill_local_subprocess`: a missing
    pgid, an unregistered host, or an ssh failure must never break teardown.
    """
    from api.services.agent_worker.remote_spawn import kill_remote_process_group
    from config.settings import settings

    pgid = getattr(target, "remote_pgid", None)
    if pgid is None:
        return  # no remote_pgid recorded yet (subprocess never spawned, or crashed pre-spawn)
    ssh_target = settings.agent_hosts.get(target.host or "")
    if not ssh_target:
        logger.warning(
            "remote kill: host %r for session %s is not in agent_hosts — nothing to signal",
            target.host, target.session_id,
        )
        return
    ok = kill_remote_process_group(target=ssh_target, pgid=pgid, runner=remote_kill_runner)
    transcript_store.append(target.session_id, "remote_subprocess_kill_attempted", {
        "host": target.host, "pgid": pgid, "ok": ok,
    })


def _kill_local_subprocess(
    transcript_store: TranscriptStore, target: Session, *, remote_kill_runner=None,
) -> None:
    """Best-effort terminate the CLI subprocess owned by a LOCAL or
    (#851) REMOTE session.

    The `claude_code` / `codex` executors run a `subprocess.Popen` in the
    *worker* process and record its pid/process-group id via a `claude_code_pid`
    / `codex_pid` transcript event (the subprocess is its own session leader via
    `start_new_session=True`). The operator kill runs in the *API* process, so it
    can only reach that subprocess by signalling the recorded pgid. Same-user
    `killpg` is permitted; the worker's `proc.wait()` reaps the dead child.

    A session whose `host` names a machine other than the API host never
    ran a local subprocess at all — its argv was wrapped in `ssh` instead
    (see `remote_spawn.build_remote_argv`), so it's dispatched to
    `_kill_remote_subprocess` instead of the local killpg path below —
    UNLESS no `remote_pgid` was ever recorded (round 1, finding #3): the
    executor's pgid read can itself hang (a stalled ssh client that
    connected but never answered), in which case there's no remote process
    to signal yet and the only thing this operator kill CAN reach is the
    hung LOCAL `ssh` client process — the same `claude_code_pid`/`codex_pid`
    transcript event below already recorded its pid (see
    `ClaudeCodeExecutor._run`/`CodexExecutor._run`, the "remote": True
    branch, which appends that event with the local ssh client's own
    `proc.pid` regardless of whether the pgid line ever arrived). Falling
    through to the local pid path is what makes that hang killable.

    Best-effort by contract: a missing pid event, a stale pid (process already
    gone), or a signalling error must NOT break teardown — the managed kill, DB
    flip, and transcript event have already run by the time this is called.
    """
    if target.routing not in CLI_ROUTINGS:
        return  # pure managed/cloud sessions own no local subprocess

    host = (getattr(target, "host", None) or "").strip()
    if host:
        from api.services.agent_worker.remote_spawn import api_host_name as _api_host_name
        if host != _api_host_name():
            if getattr(target, "remote_pgid", None) is not None:
                _kill_remote_subprocess(transcript_store, target, remote_kill_runner=remote_kill_runner)
                return
            # No remote_pgid recorded — fall through to the local pid path
            # below, which can still reach the hung local ssh client.

    # Find the most recent pid event in the transcript. Codex records `codex_pid`;
    # claude_code records `claude_code_pid`. The latest wins (a resumed session
    # spawns a fresh subprocess and appends a newer event).
    pid: int | None = None
    pgid: int | None = None
    try:
        for ev in reversed(transcript_store.read(target.session_id)):
            if ev.get("kind") in ("claude_code_pid", "codex_pid"):
                payload = ev.get("payload") or {}
                pid = payload.get("pid")
                pgid = payload.get("pgid", pid)
                break
    except Exception as exc:  # noqa: BLE001 — never break teardown on a read error
        logger.warning("local kill: transcript read for %s failed: %s",
                       target.session_id, exc)
        return
    if pid is None:
        return  # subprocess never recorded a pid (e.g. crashed before spawn)
    if pgid is None:
        pgid = pid

    try:
        # Best-effort liveness check. If the pid is already gone we skip
        # signalling entirely. This only *narrows* the pid-reuse TOCTOU window —
        # it does not eliminate it: the pid could be reused between this probe
        # and the killpg below. Acceptable because killpg targets the recorded
        # pgid (the CLI's own process group via start_new_session), so a reused
        # pid would have to also lead an identically-numbered group to be hit.
        os.kill(pid, 0)
    except ProcessLookupError:
        return  # already gone — nothing to signal
    except (PermissionError, OSError) as exc:
        logger.warning("local kill: liveness check for pid %s failed: %s", pid, exc)
        return

    sig_used = "SIGTERM"
    try:
        os.killpg(pgid, signal.SIGTERM)
        # Bounded grace for a clean exit, polling the group LEADER pid.
        deadline = time.monotonic() + _LOCAL_KILL_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break  # leader exited under SIGTERM
            time.sleep(_LOCAL_KILL_POLL_S)
        # SIGKILL sweep the whole group regardless: the grace loop only watches
        # the leader, but a group child can outlive the leader (the leader exits
        # while a spawned grandchild lingers). The sweep reaps any survivors.
        # ProcessLookupError means the group is already fully gone (leader exited
        # cleanly, no lingering children) — that's the no-op success path.
        try:
            os.killpg(pgid, signal.SIGKILL)
            sig_used = "SIGKILL"
        except ProcessLookupError:
            pass  # group already gone — clean exit under SIGTERM
    except ProcessLookupError:
        pass  # raced to exit between the liveness check and the SIGTERM — fine
    except (PermissionError, OSError) as exc:
        logger.warning("local kill: killpg(%s) failed: %s", pgid, exc)
        return

    transcript_store.append(target.session_id, "local_subprocess_killed", {
        "pid": pid, "pgid": pgid, "signal": sig_used,
    })


def teardown_session(
    session_store: SessionStore,
    transcript_store: TranscriptStore,
    target: Session,
    transcript_kind: str,
    transcript_payload: dict,
    managed_driver: Any | None = None,
    remote_kill_runner=None,
) -> dict[str, Any]:
    """Tear down a single session: kill the managed remote (best-effort),
    flip the local DB status to FAILED, append a transcript event, and
    (for local or #851 remote-host CLI sessions) terminate the worker-owned
    subprocess.

    Shared between agent-initiated `kill()` (this module) and the operator
    HTTP kill endpoint (`api/routes/agents.py`). Authorization is the
    caller's responsibility — this helper just runs the mechanics.

    `remote_kill_runner` (#851) is a test seam forwarded to
    `_kill_remote_subprocess`/`remote_spawn.kill_remote_process_group` for a
    session whose `host` names a machine other than the API host — None
    (the default) uses a real `subprocess.run` over ssh.

    Returns `{"managed_failure": <reason or None>}` so the caller can
    surface partial-success in its response.
    """
    managed_failure: str | None = None
    from api.services.agent_worker.executor_lifecycle import (
        CancelResult, ExecutorCapabilities, ExecutorRegistry,
    )

    def cancel_route(session, reason):
        nonlocal managed_failure
        if session.managed_agent_session_id and managed_driver is not None:
            try:
                managed_driver.kill_session(session.managed_agent_session_id, reason=reason)
            except Exception as exc:  # noqa: BLE001 — local teardown still proceeds
                managed_failure = str(exc)
                logger.warning("kill_session %s failed: %s", session.managed_agent_session_id, exc)
        _kill_local_subprocess(transcript_store, session, remote_kill_runner=remote_kill_runner)
        return CancelResult(
            cancelled=True, reason=reason or "cancelled", session_id=session.session_id,
            attempt_id=getattr(session, "attempt_id", None), turn_id=getattr(session, "turn_id", None),
        )

    # Operator/API teardown and in-process agent kill share the exact same
    # once-only registry guard. The persisted failed marker is also what the
    # worker-side Hermes cancellation watcher uses to close a blocked stream.
    registry = ExecutorRegistry(session_store=session_store)
    registry.register(
        target.routing or "",
        type("_TeardownAdapter", (), {
            "route": target.routing or "",
            "capabilities": ExecutorCapabilities(cancel=True),
            "cancel": staticmethod(cancel_route),
        })(),
    )
    result = registry.cancel_once(target, transcript_payload.get("reason", ""))
    if not result.cancelled and not result.idempotent:
        logger.warning("cancel registry rejected %s: %s", target.session_id, result.reason)
    transcript_store.append(target.session_id, transcript_kind, transcript_payload)
    # #379: flipping the DB to FAILED is what the executor's silent-guard keys on,
    # so the row is updated *before* we signal the subprocess. The status flip
    # alone doesn't stop the OS process (the worker's `claude -p` keeps running
    # until the next poll) — this reaps it promptly so an operator kill actually
    # stops compute within seconds.
    return {"managed_failure": managed_failure}


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

    if ctx.worker_handle is not None and hasattr(ctx.worker_handle, "cancel_session"):
        result = ctx.worker_handle.cancel_session(target, args.get("reason", ""))
        if not result.cancelled and not result.idempotent:
            return _err(result.reason or "cancel failed", code="cancel_failed")
        ctx.transcript_store.append(target_id, "killed", {
            "by": caller.session_id,
            "reason": args.get("reason", ""),
            "managed_remote": target.managed_agent_session_id,
        })
    else:
        teardown_session(
            ctx.session_store,
            ctx.transcript_store,
            target,
            transcript_kind="killed",
            transcript_payload={
                "by": caller.session_id,
                "reason": args.get("reason", ""),
                "managed_remote": target.managed_agent_session_id,
            },
            managed_driver=ctx.managed_driver,
        )
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

def user_ask(ctx: InterAgentContext, args: dict) -> dict:
    """Agent-initiated Telegram clarification. Marks the caller blocked and
    queues the question with the user via the worker."""
    question = (args.get("question") or "").strip()
    if not question:
        return _err("question is required", code="invalid_arg")
    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err(f"caller session {ctx.caller_session_id} not found", code="no_caller")
    if ctx.worker_handle is None:
        return _err(
            "lifeos_agent_user_ask is only available inside a running session",
            code="no_worker",
        )

    sent_id = ctx.worker_handle.ask_user_via_telegram(
        session_id=caller.session_id,
        task_id=caller.task_id,
        question=question,
    )
    if sent_id is None:
        return _err(
            "could not send Telegram message — bot token may not be configured",
            code="telegram_unavailable",
        )

    # Mark the caller blocked so the worker stops driving its loop.
    if not _update_status_for_session(ctx.session_store, caller, STATUS_BLOCKED):
        return _err(
            f"session {caller.session_id} is no longer active",
            code="cancelled",
        )
    ctx.transcript_store.append(caller.session_id, "user_ask", {
        "sent_message_id": sent_id, "question_chars": len(question),
    })
    return _ok({"asked": True, "sent_message_id": sent_id, "blocked": True})


DISPATCH_TABLE = {
    "lifeos_agent_spawn": spawn,
    "lifeos_agent_send": send,
    "lifeos_agent_check": check,
    "lifeos_agent_yield_until": yield_until,
    "lifeos_agent_kill": kill,
    "lifeos_agent_transcript_read": transcript_read,
    "lifeos_agent_sessions_list": sessions_list,
    "lifeos_agent_user_ask": user_ask,
    "lifeos_agent_execution_override": execution_override,
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
