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
    a "[needs clarification]" question (follow-up).
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
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from api.services.task_manager import Task, TaskManager


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


def caller_turn_proof_for_session(
    session_id: str, attempt_id: str, turn_id: str, secret: str,
) -> str:
    """Bind one MCP request to the executor turn that received the secret."""
    values = (session_id, attempt_id, turn_id)
    if not secret or any(not isinstance(value, str) or not value.strip() for value in values):
        return ""
    message = "lifeos-agent-turn-v1\0" + "\0".join(value.strip() for value in values)
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256,
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
# undisclosed API-billed side door (see ADR-018).
NON_API_BILLED_ROOT_ROUTINGS = CLI_ROUTINGS + (HERMES_ROUTING,)


def metered_target_out_of_scope(
    session_store: SessionStore, session: Session, target_executor: str,
) -> bool:
    """True when `session` is not already authorized for `target_executor`
    ('claude' or 'remote' — the two metered engines).

    A lineage rooted in a subscription-billed CLI/Hermes session can never
    unlock a metered target, and the session's own resolved execution must
    already match the requested executor exactly — an agent can only reach
    a metered route its own already-authorized scope already carries, never
    a new one it merely names. Shared by the handoff handler (checked
    against the source turn's own caller session) and the project-child
    write guard in `api/routes/tasks.py` (checked against the project
    owner's session).
    """
    from api.services.agent_worker.execution import ExecutionSpec

    root = session_store.get_by_session_id(
        session.root_session_id or session.session_id,
    ) or session
    source_executor = None
    if session.execution_spec:
        try:
            source_executor = ExecutionSpec.from_dict(session.execution_spec).executor
        except (TypeError, ValueError):
            source_executor = None
    return root.routing in NON_API_BILLED_ROOT_ROUTINGS or source_executor != target_executor


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
    caller_attempt_id: str | None = None
    caller_turn_id: str | None = None
    task_manager: Any | None = None


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
        "name": "lifeos_agent_project_handoff",
        "description": (
            "Convert the current ordinary top-level task into a one-level durable project. "
            "This is terminal for the current executor turn: after a successful call, stop."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "caller_session_id": _CALLER_PROP,
                "caller_proof": _CALLER_PROOF_PROP,
                "caller_attempt_id": {"type": "string"},
                "caller_turn_id": {"type": "string"},
                "caller_turn_proof": {"type": "string"},
                "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "children": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "key": {"type": "string", "minLength": 1, "maxLength": 64},
                            "description": {"type": "string", "minLength": 1, "maxLength": 500},
                            "notes": {"type": "string", "maxLength": 6000},
                            "assignee": {
                                "type": ["string", "null"],
                                "enum": [None, "me", "claude", "codex", "local", "cloud", "cloud-haiku", "cloud-sonnet"],
                            },
                            "execution": {
                                "type": "object", "additionalProperties": False,
                                "properties": {
                                    "executor": {"type": "string", "enum": ["local", "remote", "claude", "claude_code", "codex"]},
                                    "model_id": {"type": "string"},
                                    "effort": {"type": "string", "enum": ["low", "medium", "high", "max"]},
                                    "host": {"type": "string"},
                                    "working_dir": {"type": "string"},
                                },
                            },
                        },
                        "required": ["key", "description"],
                    },
                },
            },
            "required": [
                "caller_session_id", "caller_proof", "caller_attempt_id",
                "caller_turn_id", "caller_turn_proof", "operation_id", "children",
            ],
        },
    },
    {
        "name": "lifeos_agent_project_owner",
        "description": (
            "Review and complete the project you own, as its attested owner. "
            "`accept_child`/`reject_child` act on a review-pending child of "
            "your own project (reject requires `note` and is refused while "
            "the project is paused); accepting a child whose pull request "
            "base is exactly your project's own integration branch also "
            "merges that pull request into it first (`merge_pull_request`, "
            "default true) — a failed merge fails the whole call with "
            "`merge_failed` and leaves the card in review, unmerged and "
            "unaccepted. `complete_project` marks your project done and is "
            "allowed even while your own turn is still live (every other "
            "completion requirement — unresolved children, pending "
            "cancellation, cancelled-children acknowledgement — still "
            "applies), except that it refuses with `integration_unmerged` "
            "while your project's integration branch still has commits the "
            "default branch doesn't; merge that branch into the default "
            "branch yourself, through this repository's own documented "
            "merge process, before calling complete_project again. Scoped "
            "strictly to the project you own and your own current turn."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "caller_session_id": _CALLER_PROP,
                "caller_proof": _CALLER_PROOF_PROP,
                "caller_attempt_id": {"type": "string"},
                "caller_turn_id": {"type": "string"},
                "caller_turn_proof": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["accept_child", "reject_child", "complete_project"],
                },
                "project_id": {"type": "string", "minLength": 1},
                "child_task_id": {
                    "type": "string",
                    "description": "Required for accept_child/reject_child.",
                },
                "note": {
                    "type": "string", "maxLength": 6000,
                    "description": "Required for reject_child.",
                },
                "merge_pull_request": {
                    "type": "boolean",
                    "description": (
                        "accept_child only, default true: also merge the child's recorded "
                        "pull request into your project's integration branch, but only "
                        "when its base is exactly that branch. A merge failure fails the "
                        "whole call with merge_failed and leaves the card unaccepted."
                    ),
                },
                "acknowledge_cancelled_children": {
                    "type": "boolean",
                    "description": "complete_project only: acknowledge cancelled children before completing reduced scope.",
                },
            },
            "required": [
                "caller_session_id", "caller_proof", "caller_attempt_id",
                "caller_turn_id", "caller_turn_proof", "action", "project_id",
            ],
        },
    },
    {
        "name": "lifeos_agent_spawn",
        "description": "Spawn a child agent session that runs in parallel. Returns immediately with a `child_session_id` you can monitor with `lifeos_agent_check` or wait on with `lifeos_agent_yield_until`. Budget is drawn from your remaining lineage budget. Omit the route to inherit an active scoped override (or the caller route). Choose `claude_code` or `codex` according to the capabilities configured for that executor. For legacy `model=claude_code` children, `tier` selects a Claude tier; when omitted, the CLI configured default is used.",
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
    # Optional Claude tier for claude_code children. Ignored for other
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
    # orchestrator the worker drives through Claude Code) is refused it.
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
    # `max_tokens` is opt-in (None means no cap, the default since PR 1) —
    # unlike wall/dollars it has no numeric fallback to coerce to `int`.
    parent_tokens_raw = parent_budget.get("max_tokens")
    parent_tokens = None if parent_tokens_raw is None else int(parent_tokens_raw)
    wall = parent_wall if requested.wall_seconds is None else requested.wall_seconds
    tokens = parent_tokens if requested.max_tokens is None else requested.max_tokens
    dollars = parent_remaining if requested.max_dollars is None else requested.max_dollars
    if (
        isinstance(wall, bool) or not isinstance(wall, int) or wall < 0
        or (
            tokens is not None
            and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0)
        )
        or isinstance(dollars, bool) or not isinstance(dollars, (int, float))
        or not math.isfinite(float(dollars)) or dollars < 0
    ):
        return _err("child budget values must be finite and non-negative", code="invalid_budget")
    # A parent with no token cap has nothing for a child's request to
    # exceed — only compare when the parent actually has one.
    if wall > parent_wall or (
        tokens is not None and parent_tokens is not None and tokens > parent_tokens
    ):
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
        # Inherit the caller's bot ownership (e.g. "doctor" for
        # a Hermes doctor-persona conversation, via hermes_session.py) so the
        # worker's own status/blocked/completion notices for this child
        # route to the same Telegram bot the caller answers on, and that
        # bot's threaded-reply resume (scoped to its own `bot`) can find
        # them. `caller.bot` is already `None` for a primary-rooted lineage
        # and for every pre-existing Hermes/CLI root, so this is
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


def project_handoff(ctx: InterAgentContext, args: dict) -> dict:
    """Stage a durable project for the exact current owning executor turn."""
    import re

    from api.services import agent_board
    from api.services.agent_worker.execution import (
        parse_execution_request,
        parse_legacy_route_alias,
        unsupported_explicit_fields,
    )
    from api.services.task_manager import get_task_manager
    from api.services.task_projects import ProjectHandoffError, ProjectTaskService

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err("caller session not found", code="stale_turn")
    attempt_id = ctx.caller_attempt_id or (args.get("caller_attempt_id") or "").strip()
    turn_id = ctx.caller_turn_id or (args.get("caller_turn_id") or "").strip()
    if (
        not attempt_id or not turn_id
        or caller.attempt_id != attempt_id
        or caller.turn_id != turn_id
        or not ctx.session_store.is_current_turn(caller.task_id, attempt_id, turn_id)
    ):
        return _err("caller does not own the current executor turn", code="stale_turn")

    operation_id = (args.get("operation_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id):
        return _err("operation_id must be a stable 1-128 character key", code="invalid_arg")
    raw_children = args.get("children")
    if not isinstance(raw_children, list) or not 1 <= len(raw_children) <= 20:
        return _err("children must contain between 1 and 20 entries", code="invalid_arg")

    allowed_child = {"key", "description", "notes", "assignee", "execution"}
    allowed_execution = {"executor", "model_id", "effort", "host", "working_dir"}
    normalized_children: list[dict[str, Any]] = []
    keys: set[str] = set()
    from api.services.task_manager import _validate_text_fields

    for index, raw in enumerate(raw_children):
        if not isinstance(raw, dict) or set(raw) - allowed_child:
            return _err(f"child {index} has unknown fields", code="invalid_arg")
        key = (raw.get("key") or "").strip()
        description = (raw.get("description") or "").strip()
        notes = raw.get("notes")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key):
            return _err(f"child {index} key is invalid", code="invalid_arg")
        if key in keys:
            return _err(f"child key {key!r} is duplicated", code="invalid_arg")
        keys.add(key)
        if not description or len(description) > 500:
            return _err(f"child {key} description must be 1-500 characters", code="invalid_arg")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 6000):
            return _err(f"child {key} notes must be at most 6000 characters", code="invalid_arg")
        assignee = raw.get("assignee")
        if assignee is not None:
            if not isinstance(assignee, str):
                return _err(f"child {key} assignee is invalid", code="invalid_assignment")
            assignee = assignee.strip().lower().lstrip("#")
        if assignee == "hermes":
            return _err(
                f"child {key} cannot be assigned to hermes; the operator can "
                "assign it from the board",
                code="hermes_delegation_forbidden",
            )
        allowed_assignees = {"me", *agent_board.AGENT_EXECUTOR_TAGS}
        if assignee is not None and assignee not in allowed_assignees:
            return _err(f"child {key} assignee is invalid", code="invalid_assignment")

        raw_execution = raw.get("execution")
        normalized_execution: dict[str, str] | None = None
        request = None
        if raw_execution is not None:
            if not isinstance(raw_execution, dict) or set(raw_execution) - allowed_execution:
                return _err(f"child {key} execution has unsupported fields", code="invalid_execution")
            if str(raw_execution.get("executor", "")).strip().lower() == "hermes":
                return _err(
                    f"child {key} cannot execute on hermes; the operator can "
                    "assign it from the board",
                    code="hermes_delegation_forbidden",
                )
            parsed = parse_execution_request(raw_execution)
            if not parsed.ok or parsed.request is None or not parsed.request.executor:
                return _err(
                    "; ".join(item.message for item in parsed.diagnostics)
                    or f"child {key} execution.executor is required",
                    code="invalid_execution",
                )
            request = parsed.request
            unsupported = unsupported_explicit_fields(request, request.executor)
            if unsupported:
                return _err(
                    "; ".join(item.message for item in unsupported),
                    code="unsupported_execution_field",
                )
            if request.host:
                from api.services.agent_worker.remote_spawn import (
                    HostResolutionError,
                    api_host_name,
                    resolve_host_target,
                )

                try:
                    resolve_host_target(request.host, api_host_name())
                except HostResolutionError as exc:
                    return _err(str(exc), code="invalid_execution")
            normalized_execution = {
                name: value for name in allowed_execution
                if (value := raw_execution.get(name)) is not None
            }

        alias_request = None
        if assignee and assignee != "me":
            alias = parse_legacy_route_alias(f"#{assignee}")
            alias_request = alias.request
            if alias_request is None:
                return _err(f"child {key} assignee cannot be resolved", code="invalid_assignment")
        if assignee == "me" and request is not None:
            return _err(f"child {key} assigned to me cannot carry agent execution", code="execution_conflict")
        if request is not None and alias_request is not None:
            if request.executor != alias_request.executor or (
                alias_request.model_id and request.model_id
                and alias_request.model_id != request.model_id
            ):
                return _err(
                    f"child {key} execution conflicts with assignee {assignee}",
                    code="execution_conflict",
                )

        target_executor = request.executor if request else (
            alias_request.executor if alias_request else None
        )
        if target_executor in {"claude", "remote"} and metered_target_out_of_scope(
            ctx.session_store, caller, target_executor,
        ):
            return _err(
                f"child {key} requests metered executor {target_executor} outside the source turn's explicit target",
                code="api_billing_blocked",
            )

        child = {"key": key, "description": description, "assignee": assignee}
        if notes is not None:
            child["notes"] = notes
        if normalized_execution is not None:
            child["execution"] = normalized_execution
        try:
            _validate_text_fields(description, notes)
            for value in (normalized_execution or {}).values():
                _validate_text_fields(value)
        except ValueError as exc:
            return _err(f"child {key}: {exc}", code="invalid_arg")
        normalized_children.append(child)

    manager = ctx.task_manager or get_task_manager()
    try:
        return ProjectTaskService(
            manager, ctx.session_store, ctx.transcript_store,
        ).stage_handoff(
            caller, operation_id=operation_id, children=normalized_children,
        )
    except ProjectHandoffError as exc:
        return _err(str(exc), code=exc.code)
    except (ValueError, KeyError) as exc:
        return _err(str(exc), code="invalid_arg")


def _project_is_agent_owned(tags: list[str]) -> bool:
    """Same test `ProjectTaskService._plan_and_delegate_locked` (and the
    worker's `_project_is_agent_owned`) use to decide a project is agent-
    (not operator-) owned: an engine assignee tag, or a Managed consent
    sub-tag."""
    from api.services import agent_board

    if agent_board.derive_assignee(tags) in agent_board.AGENT_ASSIGNEES:
        return True
    normalized = agent_board.normalize_tags(tags)
    return any(tag in normalized for tag in agent_board.MANAGED_AGENT_ASSIGNEES)


# `board_review`'s errors are shared with the operator board routes and
# carry their own, more granular codes. Only the closed set documented on
# `lifeos_agent_project_owner` (`invalid_arg`, `not_found`, `stale_turn`,
# `not_owner`, `not_review`, `paused`, `forbidden`, `conflict`,
# `merge_failed`, `integration_unmerged`) may reach an owner caller; anything
# else is folded onto the nearest documented code here rather than forwarded
# verbatim. `merge_failed`/`integration_unmerged` are never produced by
# `board_review` itself -- they come from this module's own merge-on-accept
# and completion-gate steps, below.
_BOARD_REVIEW_CODE_MAP = {
    "no_session": "not_found",
    "session_running": "conflict",
    "hermes_conversation_missing": "conflict",
    "followup_failed": "conflict",
}


def _owner_facing_code(code: str) -> str:
    return _BOARD_REVIEW_CODE_MAP.get(code, code)


def _merge_child_pr_into_integration_branch(
    ctx: InterAgentContext, child: "Task", integration_branch: str, project_id: str,
) -> dict | None:
    """None when there's nothing to merge (the child recorded no pull
    request, its base isn't exactly ``integration_branch``, it's already
    merged, or it's closed without having been merged) or a targeted merge
    just succeeded; an ``_err(..., code="merge_failed")`` payload when a
    targeted merge was attempted and failed. Never touches a pull request
    whose base isn't exactly the project's own recorded integration
    branch.

    A closed-but-unmerged PR (superseded, abandoned) is treated as nothing
    to merge, not as a failure: `gh` would refuse to merge a closed PR, and
    returning `merge_failed` for it would make a default `accept_child`
    call permanently unable to accept that card unless the owner remembers
    to pass `merge_pull_request=false` every time.

    A successful merge is recorded to the transcript here, independent of
    whatever the caller's subsequent `accept_review` call does with the
    card -- a merge that lands and is then followed by an accept failure
    (a stale card, a store error) must still leave a durable record that
    the pull request was actually merged, rather than that fact living
    only in the return value of a call whose accept half then failed.
    """
    from api.services.agent_worker.git_worktree import merge_pull_request, pr_base_and_state

    if ctx.session_store is None:
        return None
    outcome = ctx.session_store.get_card_outcome(child.id)
    pr_urls = (outcome or {}).get("pr_urls") or []
    if not pr_urls:
        return None
    pr_url = pr_urls[0]
    child_session = ctx.session_store.get(child.id)
    host = child_session.host if child_session is not None else None

    data, error = pr_base_and_state(pr_url, host=host)
    if error:
        return _err(f"could not read {pr_url}: {error}", code="merge_failed")
    if (data or {}).get("baseRefName") != integration_branch:
        return None
    if (data or {}).get("state") in {"MERGED", "CLOSED"}:
        return None

    merged, error = merge_pull_request(pr_url, host=host)
    if not merged:
        return _err(
            f"could not merge {pr_url} into {integration_branch!r}: {error}",
            code="merge_failed",
        )
    if ctx.transcript_store is not None:
        ctx.transcript_store.append(ctx.caller_session_id, "project_owner_merge", {
            "project_id": project_id, "child_task_id": child.id,
            "pr_url": pr_url, "integration_branch": integration_branch,
        })
    return None


def _integration_unmerged_error(
    ctx: InterAgentContext, manager: "TaskManager", project_id: str, integration_branch: str,
) -> dict | None:
    """None when the project's integration branch is fully merged into the
    default branch, or when there's no coding child pull request on record
    yet to derive the repository from (nothing was ever merged onto the
    branch, so there's nothing for this gate to check); an
    ``_err(..., code="integration_unmerged")`` payload otherwise, naming the
    branch and either the comparison result or -- when the remote check
    itself couldn't run (`gh` missing, an unresolvable host, a network
    failure) -- that it couldn't be confirmed. Failing that check closed
    (refusing completion) rather than open matches the contract: it must
    never let a project complete over integration work nobody actually
    verified merged.

    Checks whether the branch still exists before comparing it against the
    default branch: this repository's own documented merge process deletes
    the source branch on merge, and a plain commits-ahead compare against
    an already-deleted head ref 404s exactly the way a genuinely broken
    check would. Treating that 404 as an ordinary check failure would
    refuse completion forever the moment the owner does exactly what the
    guidance tells it to -- a branch confirmed gone (`repo_branch_exists`
    returning `False`, never merely a failed check) is instead the
    terminal "fully merged" state.
    """
    from api.services.agent_worker.git_worktree import (
        repo_branch_exists, repo_compare_ahead_by, repo_default_branch, repo_slug_from_pr_url,
    )
    from api.services.task_projects import PARENT_ID_FIELD, clean_parent_id

    repo_slug = None
    host = None
    if ctx.session_store is not None:
        for task in manager.list_tasks():
            if clean_parent_id(task.fields.get(PARENT_ID_FIELD)) != project_id:
                continue
            outcome = ctx.session_store.get_card_outcome(task.id)
            for url in (outcome or {}).get("pr_urls") or []:
                slug = repo_slug_from_pr_url(url)
                if slug:
                    repo_slug = slug
                    child_session = ctx.session_store.get(task.id)
                    host = child_session.host if child_session is not None else None
                    break
            if repo_slug:
                break
    if repo_slug is None:
        return None

    # `repo_default_branch` runs first, always -- besides being needed for
    # the compare below, its success independently confirms the repository
    # itself exists, which is what makes a subsequent 404 from
    # `repo_branch_exists` unambiguous ("the branch is gone", never "the
    # repository is gone", which also 404s on the branches endpoint and
    # must never be read as "fully merged").
    default_branch, error = repo_default_branch(repo_slug, host=host)
    if error or not default_branch:
        return _err(
            f"could not determine the default branch for {repo_slug} to check whether "
            f"{integration_branch!r} is merged: {error or 'no branch returned'}",
            code="integration_unmerged",
        )

    exists, error = repo_branch_exists(repo_slug, integration_branch, host=host)
    if error:
        return _err(
            f"could not confirm whether {integration_branch!r} still exists in {repo_slug}: {error}",
            code="integration_unmerged",
        )
    if exists is False:
        return None

    ahead_by, error = repo_compare_ahead_by(repo_slug, default_branch, integration_branch, host=host)
    if error or ahead_by is None:
        return _err(
            f"could not confirm {integration_branch!r} is merged into {default_branch!r}: "
            f"{error or 'no result returned'}",
            code="integration_unmerged",
        )
    if ahead_by <= 0:
        return None
    return _err(
        f"integration branch {integration_branch!r} has {ahead_by} commit(s) not yet merged "
        f"into {default_branch!r}; merge it into the default branch through this repository's "
        "own documented merge process, then call complete_project again",
        code="integration_unmerged",
    )


def project_owner(ctx: InterAgentContext, args: dict) -> dict:
    """Attested project-owner review and completion.

    `accept_child`/`reject_child` share their underlying logic with the
    operator's board accept/reject routes (`api/services/board_review.py`);
    accepting a child whose pull request base is exactly the project's
    recorded integration branch also merges it first
    (`_merge_child_pr_into_integration_branch`, `merge_pull_request` arg,
    default true) -- a failed merge fails the whole call with
    `merge_failed` and the card is never accepted. `complete_project` calls
    `ProjectTaskService.complete_project` with `owner_session=caller`,
    which exempts only this exact attested caller from the live-coordinator
    completion guard, but first refuses with `integration_unmerged`
    (`_integration_unmerged_error`) while the project's integration branch
    still has commits the default branch doesn't.

    Authorization is exactly like `project_handoff`: the caller must be
    exactly the project's current owner session (`project_coordinator_
    session_id`), on its exact current turn. Stable error codes: `invalid_arg`,
    `not_found`, `stale_turn`, `not_owner`, `not_review`, `paused`, `forbidden`,
    `conflict`, `merge_failed`, `integration_unmerged`.
    """
    from api.services import agent_board
    from api.services.board_review import BoardReviewError, accept_review, reject_review
    from api.services.task_manager import TaskConflictError, get_task_manager
    from api.services.task_projects import (
        CANCEL_OPERATION_FIELD,
        COORDINATOR_SESSION_FIELD,
        HANDOFF_OPERATION_FIELD,
        INTEGRATION_BRANCH_FIELD,
        PROJECT_PAUSED_FIELD,
        ProjectConflictError,
        ProjectTaskService,
        clean_parent_id,
        field_truthy,
    )

    action = (args.get("action") or "").strip()
    if action not in {"accept_child", "reject_child", "complete_project"}:
        return _err(
            "action must be accept_child, reject_child, or complete_project",
            code="invalid_arg",
        )
    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return _err("project_id is required", code="invalid_arg")

    caller = ctx.session_store.get_by_session_id(ctx.caller_session_id)
    if caller is None:
        return _err("caller session not found", code="stale_turn")
    attempt_id = ctx.caller_attempt_id or (args.get("caller_attempt_id") or "").strip()
    turn_id = ctx.caller_turn_id or (args.get("caller_turn_id") or "").strip()
    if (
        not attempt_id or not turn_id
        or caller.attempt_id != attempt_id
        or caller.turn_id != turn_id
        or not ctx.session_store.is_current_turn(caller.task_id, attempt_id, turn_id)
    ):
        return _err("caller does not own the current executor turn", code="stale_turn")

    manager = ctx.task_manager or get_task_manager()
    project = manager.get(project_id)
    if project is None:
        return _err(f"project {project_id} not found", code="not_found")
    if (project.fields.get(COORDINATOR_SESSION_FIELD) or "") != caller.session_id:
        return _err("caller is not this project's current owner", code="not_owner")
    if project.status in {"done", "cancelled"}:
        return _err("project is already finished", code="forbidden")
    if project.fields.get(CANCEL_OPERATION_FIELD) or project.fields.get(HANDOFF_OPERATION_FIELD):
        return _err("project cancellation or handoff is pending", code="forbidden")
    if not _project_is_agent_owned(project.tags):
        return _err("project is not agent-owned", code="forbidden")

    reviewer = f"owner:{caller.session_id}"

    if action in {"accept_child", "reject_child"}:
        child_task_id = (args.get("child_task_id") or "").strip()
        if not child_task_id:
            return _err("child_task_id is required", code="invalid_arg")
        child = manager.get(child_task_id)
        if child is None:
            return _err(f"child {child_task_id} not found", code="not_found")
        if clean_parent_id(child.fields.get("parent_id")) != project_id:
            return _err("child does not belong to this project", code="not_owner")
        if not agent_board.is_review_pending(child.tags):
            return _err("child is not review-pending", code="not_review")

        if action == "reject_child":
            if field_truthy(project.fields.get(PROJECT_PAUSED_FIELD)):
                return _err("project is paused", code="paused")
            note = (args.get("note") or "").strip()
            if not note:
                return _err("note is required to reject", code="invalid_arg")
            try:
                result = reject_review(
                    manager, ctx.session_store, child_task_id, note, reviewer=reviewer,
                )
            except BoardReviewError as exc:
                return _err(exc.message, code=_owner_facing_code(exc.code))
            ctx.transcript_store.append(caller.session_id, "project_owner_reject", {
                "project_id": project_id, "child_task_id": child_task_id,
            })
            return _ok({
                "task_id": result.task.id, "status": result.task.status,
                "tags": list(result.task.tags),
            })

        merge_pull_request_flag = args.get("merge_pull_request")
        merge_pull_request_flag = True if merge_pull_request_flag is None else bool(merge_pull_request_flag)
        integration_branch = (project.fields.get(INTEGRATION_BRANCH_FIELD) or "").strip()
        if merge_pull_request_flag and integration_branch:
            merge_error = _merge_child_pr_into_integration_branch(
                ctx, child, integration_branch, project_id,
            )
            if merge_error is not None:
                return merge_error

        try:
            result = accept_review(manager, child_task_id, reviewer=reviewer)
        except BoardReviewError as exc:
            return _err(exc.message, code=_owner_facing_code(exc.code))
        ctx.transcript_store.append(caller.session_id, "project_owner_accept", {
            "project_id": project_id, "child_task_id": child_task_id,
        })
        return _ok({
            "task_id": result.task.id, "status": result.task.status,
            "tags": list(result.task.tags),
        })

    # action == "complete_project"
    integration_branch = (project.fields.get(INTEGRATION_BRANCH_FIELD) or "").strip()
    if integration_branch:
        gate_error = _integration_unmerged_error(ctx, manager, project_id, integration_branch)
        if gate_error is not None:
            return gate_error
    acknowledge_cancelled_children = bool(args.get("acknowledge_cancelled_children"))
    service = ProjectTaskService(manager, ctx.session_store, ctx.transcript_store)
    try:
        completed = service.complete_project(
            project_id,
            acknowledge_cancelled_children=acknowledge_cancelled_children,
            owner_session=caller,
        )
    except KeyError:
        return _err(f"project {project_id} not found", code="not_found")
    except (ProjectConflictError, TaskConflictError) as exc:
        return _err(str(exc), code="conflict")
    ctx.transcript_store.append(caller.session_id, "project_owner_complete", {
        "project_id": project_id,
    })
    return _ok({"task_id": completed.id, "status": completed.status})


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
    # Reopen-on-send (follow-up): a spawned CLI child that hits a genuine
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
# local CLI subprocess — a brief grace for the process to exit cleanly.
_LOCAL_KILL_GRACE_S = 2.0
_LOCAL_KILL_POLL_S = 0.1


def _kill_remote_subprocess(
    transcript_store: TranscriptStore, target: Session, *, remote_kill_runner=None,
) -> None:
    """Best-effort terminate a CLI subprocess a `claude_code`/`codex`
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
    REMOTE session.

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
    UNLESS no `remote_pgid` was ever recorded: the
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
    (for local or remote-host CLI sessions) terminate the worker-owned
    subprocess.

    Shared between agent-initiated `kill()` (this module) and the operator
    HTTP kill endpoint (`api/routes/agents.py`). Authorization is the
    caller's responsibility — this helper just runs the mechanics.

    `remote_kill_runner` is a test seam forwarded to
    `_kill_remote_subprocess`/`remote_spawn.kill_remote_process_group` for a
    session whose `host` names a machine other than the API host — None
    (the default) uses a real `subprocess.run` over ssh.

    `managed_stop_verified` is true only after the existing Managed state
    probe observes a terminal provider state.  A successful-looking kill
    request, a missing driver, or the local status transition is not proof.
    """
    managed_failure: str | None = None
    managed_status: str | None = None
    managed_stop_verified = False
    from api.services.agent_worker.executor_lifecycle import (
        CancelResult, ExecutorCapabilities, ExecutorRegistry,
    )

    def cancel_route(session, reason):
        nonlocal managed_failure, managed_status, managed_stop_verified
        if session.managed_agent_session_id and managed_driver is not None:
            try:
                managed_driver.kill_session(session.managed_agent_session_id, reason=reason)
                remote = managed_driver.get_session_state(session.managed_agent_session_id)
                managed_status = remote.status
                managed_stop_verified = remote.status in {
                    "idle", "completed", "failed", "cancelled", "budget_exceeded",
                }
                if not managed_stop_verified:
                    managed_failure = f"managed runtime still reports {remote.status}"
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
    # flipping the DB to FAILED is what the executor's silent-guard keys on,
    # so the row is updated *before* we signal the subprocess. The status flip
    # alone doesn't stop the OS process (the worker's `claude -p` keeps running
    # until the next poll) — this reaps it promptly so an operator kill actually
    # stops compute within seconds.
    return {
        "managed_failure": managed_failure,
        "managed_status": managed_status,
        "managed_stop_verified": managed_stop_verified,
    }


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
    "lifeos_agent_project_handoff": project_handoff,
    "lifeos_agent_project_owner": project_owner,
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
