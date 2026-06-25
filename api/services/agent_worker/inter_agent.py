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
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_FAILED,
    STATUS_YIELDED,
    TERMINAL_STATUSES,
    Session,
    SessionStore,
    new_session_id,
)
from api.services.agent_worker.transcript_store import TranscriptStore


logger = logging.getLogger(__name__)


# Engines an in-flight agent may spawn a child on. `claude`/`local` are the
# in-process routes (Managed Agents API / Gemma); `claude_code`/`codex` are the
# CLI routes, used for capability fallback (browser/GUI, native computer use).
# CLI routes are subscription-billed, so they skip the per-token dollar ceiling
# and reuse the managed concurrency cap.
CLI_ROUTINGS = ("claude_code", "codex")
SPAWN_MODELS = ("claude", "local", *CLI_ROUTINGS)


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


def _with_caller(props: dict, required: list[str]) -> dict:
    """Inject `caller_session_id` into a tool schema."""
    new_props = {"caller_session_id": _CALLER_PROP, **props}
    return {
        "type": "object",
        "properties": new_props,
        "required": ["caller_session_id"] + list(required),
    }


INTER_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lifeos_agent_spawn",
        "description": "Spawn a child agent session that runs in parallel. Returns immediately with a `child_session_id` you can monitor with `lifeos_agent_check` or wait on with `lifeos_agent_yield_until`. Budget is drawn from your remaining lineage budget. Use `claude_code` to delegate work that needs a real browser / GUI automation — its `--chrome` browser works headless, unlike Codex's (whose computer use is a desktop-app-only feature, unavailable here). For `claude_code` children, pick `tier` by task difficulty: `haiku` for simple lookups / quick web research, `sonnet` for moderate work, `opus` (default) for hard reasoning.",
        "input_schema": _with_caller({
            "prompt": {"type": "string", "description": "Task description for the child agent"},
            "model": {"type": "string", "enum": ["claude", "local", "claude_code", "codex"], "description": "Which executor to run the child on. claude=Managed Agents (cloud API), local=Gemma, claude_code=Claude Code CLI (has a working --chrome browser), codex=Codex CLI (code/general tasks; no browser in headless mode)."},
            "tier": {"type": "string", "enum": ["haiku", "sonnet", "opus"], "description": "For model=claude_code only: which Claude tier the child CLI runs. Use 'haiku' for simple lookups/quick research, 'sonnet' for moderate tasks, 'opus' (default) for hard reasoning. Ignored for other engines."},
            "max_dollars": {"type": "number", "description": "Optional per-child dollar budget"},
            "max_tokens": {"type": "integer", "description": "Optional per-child token budget"},
            "wall_seconds": {"type": "integer", "description": "Optional per-child wall-clock budget"},
            "expected_output": {"type": "string", "enum": ["text", "file", "external_action", "structured"]},
        }, required=["prompt", "model"]),
    },
    {
        "name": "lifeos_agent_send",
        "description": "Append a user-role message to another agent session. For sessions that are yielded or sleeping, the message is queued and delivered on resume.",
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
            "routing": {"type": "string", "enum": ["claude", "local", "claude_code", "codex"]},
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
    if model not in SPAWN_MODELS:
        return _err(
            "model must be one of 'claude', 'local', 'claude_code', 'codex'",
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
    claude_code_model = tier if model == "claude_code" else None

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
        # claude (managed) and the CLI routes (claude_code/codex) share the
        # managed concurrency cap, counted per their own routing.
        active = ctx.session_store.count_active_by_routing(model) - caller_excluded
        if active >= ctx.caps.max_concurrent_managed:
            return _err(
                f"{active} {model} sessions already running (cap {ctx.caps.max_concurrent_managed})",
                code="cap_concurrency_managed",
            )

    # Budget: child's max_dollars cannot exceed parent's remaining. CLI routes
    # are subscription-billed (no per-token draw), so they skip the ceiling —
    # mirrors the worker's cost-gate skip for claude_code/codex.
    parent_budget = caller.budget or {}
    spent = caller.total_dollars or 0.0
    parent_remaining = (parent_budget.get("max_dollars", 0.0) or 0.0) - spent
    if model in CLI_ROUTINGS:
        requested_dollars = max(0.0, float(args.get("max_dollars") or parent_remaining or 0.0))
    else:
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
        claude_code_model=claude_code_model,
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


def _kill_local_subprocess(
    transcript_store: TranscriptStore, target: Session,
) -> None:
    """Best-effort terminate the local CLI subprocess owned by a LOCAL session.

    The `claude_code` / `codex` executors run a `subprocess.Popen` in the
    *worker* process and record its pid/process-group id via a `claude_code_pid`
    / `codex_pid` transcript event (the subprocess is its own session leader via
    `start_new_session=True`). The operator kill runs in the *API* process, so it
    can only reach that subprocess by signalling the recorded pgid. Same-user
    `killpg` is permitted; the worker's `proc.wait()` reaps the dead child.

    Best-effort by contract: a missing pid event, a stale pid (process already
    gone), or a signalling error must NOT break teardown — the managed kill, DB
    flip, and transcript event have already run by the time this is called.
    """
    if target.routing not in CLI_ROUTINGS:
        return  # pure managed/cloud sessions own no local subprocess

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
) -> dict[str, Any]:
    """Tear down a single session: kill the managed remote (best-effort),
    flip the local DB status to FAILED, append a transcript event, and
    (for local CLI sessions) terminate the worker-owned subprocess.

    Shared between agent-initiated `kill()` (this module) and the operator
    HTTP kill endpoint (`api/routes/agents.py`). Authorization is the
    caller's responsibility — this helper just runs the mechanics.

    Returns `{"managed_failure": <reason or None>}` so the caller can
    surface partial-success in its response.
    """
    managed_failure: str | None = None
    if target.managed_agent_session_id and managed_driver is not None:
        try:
            managed_driver.kill_session(
                target.managed_agent_session_id,
                reason=transcript_payload.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to local-only on remote failure
            managed_failure = str(exc)
            logger.warning(
                "kill_session %s failed: %s",
                target.managed_agent_session_id, exc,
            )
    session_store.update_status(target.task_id, STATUS_FAILED)
    transcript_store.append(target.session_id, transcript_kind, transcript_payload)
    # #379: flipping the DB to FAILED is what the executor's silent-guard keys on,
    # so the row is updated *before* we signal the subprocess. The status flip
    # alone doesn't stop the OS process (the worker's `claude -p` keeps running
    # until the next poll) — this reaps it promptly so an operator kill actually
    # stops compute within seconds.
    _kill_local_subprocess(transcript_store, target)
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
    ctx.session_store.update_status(caller.task_id, STATUS_BLOCKED)
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
