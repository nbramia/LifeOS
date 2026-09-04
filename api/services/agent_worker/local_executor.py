"""Local agent loop for #agent #local tasks.

The executor drives one session of conversation with the local llama-server
(Gemma in this LifeOS) through `llm_client.LocalLLMClient`. Each turn:

  1. Load the session's prior messages from the DB
  2. Call the LLM with the tool catalog
  3. Persist the assistant turn + token usage
  4. Check wall + token budgets — kill the loop on breach
  5. If the model produced tool calls, dispatch each, append tool_result
     blocks to the conversation, and loop
  6. If the model produced a final text answer, mark the session complete

Sleep is a "yield": the executor writes a `sleeps` row, returns
`ExecutorOutcome(status="sleeping", ...)`, and the worker's main loop wakes
the session at the requested time by calling `execute(session)` again.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from api.services.agent_worker.delegation import INTER_AGENT_BLOCK
from api.services.agent_worker.pricing import cost_for, is_known_model
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    SessionStore,
)
from api.services.agent_worker.tools import ToolRegistry, ToolResult
from api.services.agent_worker.transcript_store import TranscriptStore


logger = logging.getLogger(__name__)


# Per-turn ceiling on tool calls. Prevents a single broken turn from
# exhausting tools forever before the budget check fires.
MAX_TOOL_CALLS_PER_TURN = 16

# Per-LLM-call max_tokens cap. The real ceiling is the session's token
# budget; this is just the per-request ask so we don't burn cap in one go.
PER_TURN_MAX_TOKENS = 4096

# Per-tool-result char cap. Anything beyond this gets truncated with a
# pointer telling the agent to refine its query. Gemma's context window
# is 32k tokens; without this, a single `grep -r` or large file Read
# would push the session's conversation past that limit within a few
# turns and the llama-server connection would drop mid-call.
# 6000 chars ≈ 1500 tokens — enough to capture useful results, small
# enough to keep ~20 tool calls in context simultaneously.
MAX_TOOL_RESULT_CHARS = 6000

# Retry budget for LLM calls. llama-server can transiently drop the
# connection under memory pressure ("Server disconnected without sending
# a response"); a single retry after a short backoff usually recovers.
LLM_RETRY_ATTEMPTS = 2
LLM_RETRY_BACKOFF_SECONDS = 1.5


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Connection-shaped errors from llama-server are worth retrying;
    schema / validation errors are not. We match on the exception class
    name and message substring so the test surface stays free of the
    httpx import (the LLM client wraps httpx but doesn't re-export its
    exception types)."""
    msg = str(exc).lower()
    if any(s in msg for s in (
        "server disconnected",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "timed out",
        "read timeout",
        "connection refused",
    )):
        return True
    cls = type(exc).__name__
    return cls in {
        "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
        "ConnectTimeout", "ReadTimeout", "PoolTimeout", "NetworkError",
    }


def _normalize_tool_calls(raw_calls) -> list[dict]:
    """Normalize tool_calls into Anthropic-shape dicts.

    `LocalLLMClient` returns raw OpenAI-format calls
    (`{"id": ..., "type": "function", "function": {"name": ..., "arguments": "<json>"}}`).
    `AnthropicLLMClient` returns Anthropic-shape `_ToolUseBlock` instances
    or dicts with `name`/`input`. The executor wants a single shape so the
    dispatcher and persistence layer stay simple. This function detects the
    shape and produces `{"id", "name", "input"}` dicts in all cases.
    """
    if not raw_calls:
        return []
    normalized: list[dict] = []
    for call in raw_calls:
        # Anthropic-shape: object/dict with a `.name` and `.input`.
        anth_name = getattr(call, "name", None)
        anth_input = getattr(call, "input", None)
        if anth_name and anth_input is not None:
            normalized.append({
                "id": getattr(call, "id", "") or f"call_{uuid.uuid4().hex[:12]}",
                "name": anth_name,
                "input": anth_input or {},
            })
            continue
        if isinstance(call, dict):
            if "function" in call and isinstance(call["function"], dict):
                # OpenAI-shape — `arguments` is a JSON string.
                func = call["function"]
                args_raw = func.get("arguments", "{}")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = args_raw or {}
                normalized.append({
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": func.get("name", ""),
                    "input": args,
                })
            elif "name" in call:
                # Anthropic-shape dict.
                normalized.append({
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": call["name"],
                    "input": call.get("input") or {},
                })
    return normalized


@dataclass
class ExecutorOutcome:
    """What happened during a single `execute(session)` invocation."""
    status: str   # one of STATUS_COMPLETED / STATUS_FAILED / STATUS_BUDGET_EXCEEDED / STATUS_YIELDED
    final_text: str = ""
    reason: str = ""
    wake_at: int | None = None   # set when status == STATUS_YIELDED (sleep)
    # Managed-agents-only: MCP servers that failed to initialize during the
    # remote session. Worker uses this to append a footer to the completion
    # summary so the operator knows which connectors are broken.
    init_failed_mcps: list[str] = field(default_factory=list)
    # #699 — non-empty only when this session actually ran on the flag-gated
    # remote fallback provider (the model id it ran on). Empty for the
    # ordinary local llama-server path, including every install with the
    # flag off or the remote provider unconfigured. worker.py uses this to
    # report what actually served the session (the #658 principle: report
    # observed, not configured) instead of just the static "local" label.
    served_by: str = ""
    # #760 — claude_code/codex-only: how many [NOTIFY]s the executor sent
    # during this run (always 0 for codex, which has no notify convention).
    # Consumed by completion_signal.has_positive_completion_signal to decide
    # whether a nominal STATUS_COMPLETED outcome is an earned completion or
    # an interrupted mid-work session.
    notifications_sent: int = 0
    # #760 — claude_code/codex-only: best-effort description of how the CLI
    # subprocess ended (returncode / signal / timed_out / whether a genuine
    # terminal stream event was seen), for the terminal transcript event.
    exit_meta: dict = field(default_factory=dict)


# Static portion of the system prompt — never changes between sessions. Kept
# as a module-level constant so prompt caches can hit on it; the dynamic
# per-session bits (expected output, soft budget) are appended at call time
# and live in a small trailing section so cache invalidation is minimized.
_SYSTEM_PROMPT_STATIC = (
    """\
<role>
You are an autonomous task executor running inside LifeOS, the operator's
personal-assistant system. You receive a single task from the operator's
task list and complete it end to end without further input from them.
</role>

<environment>
You run locally on the operator's machine. Your bash, read, write, edit,
glob, and grep tools operate on the operator's actual filesystem. The
`lifeos` MCP exposes the operator's structured personal data (calendar,
gmail, drive, photos, contacts, financial transactions, notes, tasks,
reminders, person profiles, conversation history).

The operator's vault path is in the `LIFEOS_VAULT_PATH` environment
variable — run `echo "$LIFEOS_VAULT_PATH"` via Bash if you need the
literal value. Markdown notes written under that path become Obsidian
notes the operator can read on their phone immediately.
</environment>

<mcp_routing>
Default to the `lifeos` MCP for any personal-data query. It is faster and
more accurate than scraping the filesystem directly. Use the standard
`Read` / `Write` / `Edit` tools only for files outside the indexed data
set (code, scratch notes, etc.). Use `Bash` for shell operations.
</mcp_routing>

<output_format>
Every task must end with a final assistant turn containing a text
summary. Tool calls alone are not a complete response. After your last
tool call, produce a text turn that summarizes what you did and the key
result. Be concrete: include specific names, counts, decisions, and
links. Skip filler phrases.

Critically: your final turn must report results, not intentions. Never
end with "I'll do X next" or "let me now Y" — those are promises, not
completions. If you genuinely need more turns, take them now; only end
the session when the task is actually done.

The summary is delivered to the operator via Telegram, which does NOT
render Markdown tables, headings (`#`), or code-block borders nicely.
Prefer prose, bullets, and bold/italic emphasis. Avoid pipe-table
syntax — write a short list with "Title — date/time" lines instead.

When the natural output is longer than a few short paragraphs, or is
inherently tabular (a list of rows with multiple columns), or is a
deliverable the operator will reuse (a report, draft, plan, comparison),
create an artifact and link to it in your summary rather than dumping
the body inline. Vault artifacts go in the `output_dir` path provided
in this_task — same location the worker's spillover uses, so everything
the operator's agents produce lands in one consistent folder.
  - **Notes / reports / drafts**: write a Markdown file with `Write` to
    `<output_dir>/<YYYY-MM-DD>-<descriptive-slug>.md`. Obsidian picks
    it up automatically.
  - **Tabular data**: write a CSV under the same `<output_dir>`.
    Avoid Markdown pipe tables in either Telegram or vault notes.
  - **Inline summary**: a 1–3 sentence Telegram message that says what
    you did and the full path to the artifact.
</output_format>

<ambiguity>
Do not ask clarifying questions during execution unless required in
order to complete the task. If possible, make a reasonable assumption,
make an attempt, and if it doesn't work, try something else. Be
persistent — your goal is to make the experience delightful for the
user. Just note the assumptions made in your final summary. If you
cannot complete the task safely, say so plainly in your final response.
</ambiguity>

"""
    + INTER_AGENT_BLOCK
    + """

<sleep>
When you need to wait for external state to change with no child sessions
to await, call the `sleep` tool rather than busy-looping.
</sleep>"""
)


def _system_prompt(session_id: str, expected_output: str, budget, parent_session_id: str | None = None) -> str:
    """System message for the executor agent.

    Structured per Anthropic's prompt-engineering best practices (XML
    section tags, positive framing, explicit final-summary requirement).
    Static content lives in `_SYSTEM_PROMPT_STATIC` to maximize prompt-
    cache hits; only the small dynamic trailer changes per session.

    `session_id` and `parent_session_id` are accepted for backwards
    compatibility with the issue #103 §5 inter-agent flow but no longer
    injected into the prompt body — the model can't act on either, and
    both are tracked in `lifeos_agent_sessions_list` / transcripts.
    """
    del parent_session_id  # logging-only, not for the model
    wall = budget.get("wall_seconds")
    max_tokens = budget.get("max_tokens")
    max_dollars = budget.get("max_dollars")
    dollars_str = f"~${max_dollars}" if max_dollars is not None else "unset"
    # The local LLM has a fixed training cutoff; without today's date it
    # hallucinates plausible-looking but wrong dates when interpreting
    # calendar / task / due-date data. Inject the operator's local date
    # so day-relative reasoning works.
    today = _today()
    output_dir = _output_dir()
    return (
        _SYSTEM_PROMPT_STATIC
        + "\n\n<this_task>\n"
        + f"today={today}; "
        + f"lifeos_session_id={session_id}; "
        + f"output_dir={output_dir}; "
        + f"expected_output={expected_output}; "
        + f"soft budget ~{wall}s wall / ~{max_tokens} tokens / {dollars_str}.\n"
        + "</this_task>"
    )


def _today() -> str:
    """Local today as YYYY-MM-DD (weekday). Module-level for test override."""
    from datetime import datetime
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d (%A)")


def _output_dir() -> str:
    """Resolved absolute path where the agent should write artifacts. Joins
    `LIFEOS_VAULT_PATH` with `LIFEOS_AGENT_OUTPUT_DIR` so the local agent
    and the worker's spillover land in the same folder."""
    from pathlib import Path
    from config.settings import settings as _settings
    vault = _settings.vault_path
    if not vault:
        return _settings.agent_output_dir  # vault-relative only
    try:
        vault = vault.expanduser() if hasattr(vault, "expanduser") else Path(vault)
    except Exception:
        vault = Path(str(vault))
    return str(Path(vault) / _settings.agent_output_dir)


def _worker_repo_root():
    """Repository root containing the `api/` package this worker process
    is running from — derived from this file's own location, never a
    configured or hardcoded path, so it tracks wherever the install
    actually lives (dev worktree, canonical checkout, etc.). Used to
    refuse a named working directory that resolves to, lives inside, or
    would contain the worker's own checkout (#925). Module-level for test
    override, same idiom as `_today()`."""
    from pathlib import Path
    return Path(__file__).resolve().parents[3]


def _resolve_task_working_dir(task: dict) -> tuple[str | None, str | None]:
    """Extract and validate `task["fields"]["working_dir"]` (#925) — the
    same `[key:: value]` inline-field convention `assignment.py` uses for
    `host`/`model`/`effort`. Shared by both routes this executor serves
    (`local` and the #809 remote-forced route): neither guesses a
    directory from the task title the way the CLI routes'
    `directory_resolver` does — unset stays unset.

    Returns `(resolved_dir, None)` when no directory is named (→ `None`,
    unchanged behavior) or when the named one passes every guard.
    Returns `(None, reason)` when the task must be refused before any
    model call: the path doesn't exist, isn't a directory, resolves to
    (or inside) the worker's own checkout, or is an ancestor that would
    *contain* the checkout — naming a parent directory would let
    `tools._resolve_within_base` approve reads/writes anywhere under it,
    including the checkout, which is the same hazard the direct case
    guards against, just approached from the other direction. Resolution
    is realpath-based (`Path.resolve()` follows symlinks and collapses
    `..`), so a directory that only *looks* safe is caught here, not at
    first tool use — `tools._resolve_within_base` re-checks every
    individual file path against this same resolved base for the same
    reason.
    """
    from pathlib import Path

    fields = task.get("fields") or {}
    raw = fields.get("working_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    raw = raw.strip()
    candidate = Path(raw).expanduser()
    if not candidate.exists():
        return None, f"working directory does not exist: {raw}"
    if not candidate.is_dir():
        return None, f"working directory is not a directory: {raw}"
    resolved = candidate.resolve()
    worker_root = _worker_repo_root()
    if resolved == worker_root or worker_root in resolved.parents:
        return None, f"working directory refuses the worker's own checkout: {raw}"
    if resolved in worker_root.parents:
        return None, f"working directory would contain the worker's own checkout: {raw}"
    return str(resolved), None


def _user_message_for(task: dict) -> str:
    """Build the opening user turn from the task description.

    Prepended with the LifeOS capabilities preamble so the local-routed
    agent (Gemma) has the same situational awareness as the cloud route.
    """
    from api.services.agent_worker.capabilities_preamble import CAPABILITIES_PREAMBLE
    title = (task.get("description") or "").strip()
    context = task.get("context")
    parts = [CAPABILITIES_PREAMBLE, f"Task: {title}"]
    if context:
        parts.append(f"Context: {context}")
    parts.append("Please complete this task using the tools available.")
    return "\n\n".join(parts)


def _default_llm_client(model_name: str) -> tuple[object, str, bool]:
    """Choose the LLM client + model attribution for a production
    (no explicit `llm_client` injected) local executor. Returns
    `(client, model_name, is_remote)`.

    Default: bare `LocalLLMClient()` against the local llama-server,
    `model_name` unchanged (the caller's default, "local"), `is_remote`
    False — byte-identical to the executor's pre-#699 construction
    whenever the flag is off or the remote provider (#654) isn't fully
    configured. That's the operator's standing behavior-neutrality
    requirement, so both conditions are checked before this function does
    anything else network-shaped.

    When `settings.agent_remote_executor` is on AND the remote OpenAI-
    compatible provider is configured, this does exactly one cheap
    reachability check against the local llama-server —
    `LocalLLMClient.is_available()`, a single short-timeout GET /health —
    at session-construction time. Deliberately not a background prober:
    llama-server either answers right now or it doesn't, and checking
    once, right when we're about to use it, is cheap and honest. This is
    also the #688 lesson applied in reverse — that issue was "configured"
    silently standing in for "reachable"; the mirror-image mistake here
    would be treating "remote is configured" as license to skip actually
    checking whether local is still up, so the check happens regardless
    of how confident the config looks.

    - Local reachable   → local wins. Explicit `#agent local` routing on a
      host with a live llama-server is unaffected — remote is a fallback,
      not a replacement.
    - Local unreachable → build a `LocalLLMClient` pointed at the remote
      provider instead (with `is_remote=True` and the remote model id for
      attribution), so the session actually runs instead of dying with a
      connection error on every call.
    """
    from api.services.llm_client import LocalLLMClient
    from config.settings import settings as _settings

    if _settings.agent_remote_executor and _settings.remote_llm_configured:
        local_client = LocalLLMClient()
        if not local_client.is_available():
            remote_client = LocalLLMClient(
                base_url=_settings.remote_llm_base_url,
                model=_settings.remote_llm_model,
                api_key=_settings.remote_llm_api_key,
                timeout=_settings.remote_llm_timeout,
            )
            return remote_client, _settings.remote_llm_model, True
        return local_client, model_name, False
    return LocalLLMClient(), model_name, False


def _remote_only_llm_client() -> tuple[object, str, bool]:
    """(#809) Construct an LLM client pointed unconditionally at the
    configured remote OpenAI-compatible provider — the `#cloud` tag's
    executor. Returns `(client, model_name, is_remote=True)`.

    Unlike `_default_llm_client`, this never checks local llama-server
    reachability and never falls back to local: `#cloud` means "run on the
    remote provider", full stop, not "prefer it, fall back if it's down".
    That's a deliberate, narrow difference from the #699 fallback above —
    same underlying `LocalLLMClient` pointed at the same settings, but a
    first-class route (`ROUTE_REMOTE`) rather than a contingency for when
    local is unreachable. Also unlike `_default_llm_client`, this is not
    gated on `settings.agent_remote_executor` — that flag is scoped to the
    fallback behavior on the `local` route; the operator tagging a task
    `#cloud` is itself the opt-in for this path.

    Callers (`worker.py`'s `_get_remote_executor`) must confirm
    `settings.remote_llm_configured` themselves and park the task otherwise
    (`_dispatch`'s `ROUTE_REMOTE` branch) — this function assumes that's
    already true and does not re-check it, so it never raises for a missing
    config; it would simply construct a client that fails on first use.
    """
    from api.services.llm_client import LocalLLMClient
    from config.settings import settings as _settings

    client = LocalLLMClient(
        base_url=_settings.remote_llm_base_url,
        model=_settings.remote_llm_model,
        api_key=_settings.remote_llm_api_key,
        timeout=_settings.remote_llm_timeout,
    )
    return client, _settings.remote_llm_model, True


class LocalExecutor:
    """Drives one turn or one yielded resumption per call to `execute`."""

    def __init__(
        self,
        session_store: SessionStore,
        transcript_store: TranscriptStore,
        tool_registry: ToolRegistry | None = None,
        llm_client=None,
        model_name: str = "local",
        is_remote: bool = False,
    ):
        self.session_store = session_store
        self.transcript_store = transcript_store
        # Lazy-imported to keep test surface small.
        if tool_registry is None:
            tool_registry = ToolRegistry()
        self.tools = tool_registry
        if llm_client is None:
            llm_client, model_name, is_remote = _default_llm_client(model_name)
        self.llm = llm_client
        self.model_name = model_name
        # (#699) True iff this executor is running on the flag-gated remote
        # fallback provider rather than the local llama-server. Drives both
        # spend pricing (_record_spend) and the served_by attribution on
        # ExecutorOutcome. Only ever True via `_default_llm_client`'s own
        # selection, or a test that injects it explicitly alongside a fake
        # `llm_client` — never a side effect of settings alone.
        self.is_remote = is_remote

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Run the agent loop for one session.

        - If the conversation has never been seeded with a system prompt
          (e.g., preflight blocked for ambiguity before the executor ran,
          and the worker is now resuming after a Telegram clarification
          reply), seed it now and keep any pre-existing messages (the
          injected user answer) at the end so the agent sees both the
          original task and the operator's clarification.
        - Loops until the model produces a final answer, the budget is hit,
          a tool yields (sleep), or an error.
        """
        sid = session.session_id
        budget = session.budget or {}
        self.session_store.update_status(session.task_id, STATUS_RUNNING)

        # (#925) Working-directory guard — before any conversation seeding
        # or LLM call, so a refused directory never reaches the model.
        working_dir, wd_error = _resolve_task_working_dir(task)
        if wd_error:
            return self._finalize_failed(session, wd_error)
        # Forwarded to every tool dispatch this call makes. Built once as a
        # kwargs dict (rather than always passing `base_dir=working_dir`)
        # so the no-directory case calls `dispatch(name, args)` exactly as
        # it always has — byte-identical, including to a test double that
        # only accepts the two positional args.
        dispatch_kwargs = {"base_dir": working_dir} if working_dir else {}

        existing = self.session_store.get_messages(sid)
        has_system = any(m.get("role") == "system" for m in existing)
        if not has_system:
            # First execution OR a resume from a preflight-blocked session
            # that never seeded. The worker may have pre-injected a user
            # "answer" message; seed cleanly with system+task first, then
            # re-append the answer so the model sees both the original
            # task and the operator's clarification in the right order.
            preexisting = list(existing)
            self.session_store.clear_messages(sid)
            self._seed_conversation(session, task, budget)
            for m in preexisting:
                role = m.get("role")
                content = m.get("content", "")
                if role in ("user", "assistant"):
                    self.session_store.append_message(sid, role, content)

        while True:
            # Wall-clock budget check — uses cumulative active seconds, not
            # wall-from-start (so sleeps don't eat into the run budget).
            updated = self.session_store.get(session.task_id)
            if budget.get("wall_seconds"):
                if (updated.total_active_seconds or 0) >= budget["wall_seconds"]:
                    return self._finalize_budget_exceeded(session, "wall_seconds")
            tokens_used = (updated.total_input_tokens or 0) + (updated.total_output_tokens or 0)
            if budget.get("max_tokens") and tokens_used >= budget["max_tokens"]:
                return self._finalize_budget_exceeded(session, "max_tokens")
            # No per-session dollar cap in this loop — on the local route
            # inference is free, so total_dollars is always 0. On the #809
            # remote-forced route (`self.is_remote`) it is real, non-zero
            # spend (see `_record_spend`), but this loop still doesn't cap
            # it mid-run — the same pre-existing gap #699's remote-fallback
            # path already shipped with, not something #809 introduces.
            # (max_tokens + wall_seconds still bound runaway sessions either
            # way; the lineage guard below still caps a family whose *root*
            # is a paid managed session.)

            # Lineage budget — for sessions with descendants, the *root* budget
            # caps the total spend across the family. When breached we cascade-
            # kill the entire lineage so a runaway sub-tree can't keep burning
            # tokens after the root's budget is exhausted.
            root_id = updated.root_session_id or updated.session_id
            if root_id != updated.session_id:
                root = self.session_store.get_by_session_id(root_id)
                if root and root.budget:
                    root_cap = (root.budget or {}).get("max_dollars")
                    if root_cap is not None:
                        lineage_spend = self.session_store.lineage_total_dollars(root_id)
                        if lineage_spend >= float(root_cap):
                            self._cascade_kill_lineage(root_id, reason="lineage_max_dollars")
                            return self._finalize_budget_exceeded(session, "lineage_max_dollars")

            turn_start = time.time()
            try:
                response = self._call_llm(sid, effort=session.effort)
            except Exception as exc:
                logger.exception("local executor LLM call failed: %s", exc)
                # Charge the time we spent trying so the budget reflects real
                # work even on failure.
                self.session_store.record_active_seconds(session.task_id, time.time() - turn_start)
                return self._finalize_failed(session, f"LLM call failed: {exc}")

            # Normalize tool_calls to a single shape (OpenAI ↔ Anthropic).
            normalized_calls = _normalize_tool_calls(response.tool_calls)
            truncated_calls = normalized_calls[:MAX_TOOL_CALLS_PER_TURN]

            self._persist_assistant_turn(sid, response, truncated_calls)
            self._record_spend(session, response)
            self.session_store.record_active_seconds(session.task_id, time.time() - turn_start)

            if not truncated_calls:
                # Final answer — no more tool calls expected.
                return self._finalize_completed(session, response.text)

            yielded_seconds: int | None = None
            yielded_for_children = False
            tool_results: list[dict] = []
            for call in truncated_calls:
                name = call["name"]
                args = call["input"]
                call_id = call["id"]
                result: ToolResult = self.tools.dispatch(name, args, **dispatch_kwargs)
                self.transcript_store.append(
                    sid,
                    "tool_call",
                    {
                        "tool": name,
                        "arguments": args,
                        "is_error": result.is_error,
                        "output_chars": len(result.output),
                    },
                )
                # Cap the result the model sees. The full output is still
                # recorded in the transcript above (`output_chars` plus
                # whatever subsequent code paths log) — only the in-context
                # copy is truncated, so the operator can still audit what
                # the tool actually returned.
                content = result.output
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    content = (
                        content[:MAX_TOOL_RESULT_CHARS]
                        + f"\n\n[…truncated to {MAX_TOOL_RESULT_CHARS} chars; "
                        f"original was {len(result.output)} chars. "
                        "Refine your query / read a narrower range if you "
                        "need more.]"
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": result.is_error,
                })
                if result.yield_seconds is not None:
                    if result.yield_seconds < 0:
                        # Sentinel for lifeos_agent_yield_until — yield without
                        # a timer; the worker resumes on child terminal events.
                        yielded_for_children = True
                    else:
                        yielded_seconds = result.yield_seconds
                    # Stop dispatching further tools this turn — we're going to yield.
                    break

            # If the agent yielded mid-turn, we may have fewer tool_results
            # than persisted tool_use blocks. Pad with synthetic results so
            # the next turn satisfies Anthropic's 1:1 invariant.
            if (yielded_seconds is not None or yielded_for_children) and len(tool_results) < len(truncated_calls):
                already_handled = {tr["tool_use_id"] for tr in tool_results}
                for call in truncated_calls:
                    if call["id"] not in already_handled:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": "skipped — sibling tool requested a yield first",
                            "is_error": False,
                        })

            # Persist the tool_results as a user-role turn (Anthropic convention).
            self.session_store.append_message(sid, "user", tool_results)

            if yielded_for_children:
                # Status is already set by the calling tool — either
                # lifeos_agent_yield_until (children case) or
                # lifeos_agent_user_ask (Telegram clarification case). Distinguish
                # in the transcript by whether yield_waiting_for is populated.
                refreshed = self.session_store.get(session.task_id)
                kind = "yielded_for_children" if refreshed.yield_waiting_for else "yielded_for_user"
                self.transcript_store.append(sid, kind, {})
                return ExecutorOutcome(status=STATUS_YIELDED, served_by=self._served_by())

            if yielded_seconds is not None:
                return self._finalize_sleeping(session, yielded_seconds)

    # ------------------------------------------------------------------
    # Conversation persistence
    # ------------------------------------------------------------------

    def _seed_conversation(self, session, task: dict, budget: dict) -> None:
        sid = session.session_id
        system = _system_prompt(
            sid, session.expected_output or "text", budget,
            parent_session_id=session.parent_session_id,
        )
        # We store the system message as a "system" role row so future calls
        # can rebuild the conversation; the LLM client API takes system
        # separately so we strip it when calling.
        self.session_store.append_message(sid, "system", system)
        self.session_store.append_message(sid, "user", _user_message_for(task))
        self.transcript_store.append(sid, "seed", {"task_id": session.task_id})

    def _call_llm(self, session_id: str, effort: str | None = None):
        history = self.session_store.get_messages(session_id)
        # Separate the system message from the user/assistant/tool history.
        system_text = ""
        messages_for_llm: list[dict] = []
        for entry in history:
            if entry["role"] == "system":
                content = entry["content"]
                system_text = content if isinstance(content, str) else json.dumps(content)
            else:
                messages_for_llm.append(entry)

        # (#851) Per-session thinking override. `effort` is the board's
        # assignment field (`low|medium|high|max`); `high`/`max` turns
        # thinking on, `low`/`medium` turns it off, and unset/unrecognized
        # falls back to `settings.local_agent_enable_thinking` — same
        # mapping `agent_loop.run_agent_loop` uses for the chat surface,
        # applied here per-SESSION instead of via the global setting so
        # concurrent agent-worker sessions with different assigned efforts
        # don't race each other over one process-wide flag.
        #
        # Gated on `isinstance(self.llm, LocalLLMClient)` — the exact same
        # check `run_agent_loop` uses — for two independent reasons: (1) the
        # #809 remote-forced route (`self.is_remote`) is ALSO a LocalLLMClient
        # instance (same OpenAI-compatible plumbing) but isn't llama-server —
        # it doesn't understand llama-server's `chat_template_kwargs` switch,
        # so `enable_thinking` must never reach it (mirrors `run_agent_loop`'s
        # `not force_remote` gate); an isinstance check alone wouldn't exclude
        # it, so `self.is_remote` is checked too. (2) a test-injected fake LLM
        # client's `create()` may not accept `enable_thinking` at all — gating
        # on the concrete class keeps every existing fake-client test
        # byte-identical.
        create_kwargs: dict = {}
        from api.services.llm_client import LocalLLMClient
        if isinstance(self.llm, LocalLLMClient) and not self.is_remote:
            from api.services.agent_worker.assignment import local_thinking_for_effort
            from config.settings import settings as _settings
            override = local_thinking_for_effort(effort)
            enable_thinking = override if override is not None else (
                None if _settings.local_agent_enable_thinking else False
            )
            create_kwargs["enable_thinking"] = enable_thinking

        # Retry transient connection drops. llama-server occasionally
        # disconnects mid-request under memory pressure ("Server
        # disconnected without sending a response"); systemd restarts it,
        # and a single retry after a short backoff usually succeeds.
        # Don't retry on structural errors (4xx-equivalents) — only on
        # connection-shaped exceptions.
        last_exc: Exception | None = None
        for attempt in range(LLM_RETRY_ATTEMPTS):
            try:
                return self.llm.create(
                    messages=messages_for_llm,
                    system=system_text or None,
                    max_tokens=PER_TURN_MAX_TOKENS,
                    tools=self.tools.definitions(),
                    **create_kwargs,
                )
            except Exception as exc:
                if not _is_transient_llm_error(exc) or attempt == LLM_RETRY_ATTEMPTS - 1:
                    raise
                last_exc = exc
                logger.warning(
                    "local LLM call attempt %d/%d failed transiently (%s); "
                    "retrying after %.1fs",
                    attempt + 1, LLM_RETRY_ATTEMPTS, type(exc).__name__,
                    LLM_RETRY_BACKOFF_SECONDS,
                )
                time.sleep(LLM_RETRY_BACKOFF_SECONDS)
        # Unreachable — the loop either returns or raises.
        raise last_exc  # type: ignore[misc]

    def _persist_assistant_turn(self, session_id: str, response, normalized_calls: list[dict]) -> None:
        """Persist the assistant turn using the *truncated* normalized call list
        so tool_use and tool_result block counts stay 1:1 in later turns.
        """
        content_blocks: list[dict] = []
        if response.text:
            content_blocks.append({"type": "text", "text": response.text})
        for call in normalized_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
            })
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
        self.session_store.append_message(
            session_id, "assistant", content_blocks,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )

    def _record_spend(self, session, response) -> None:
        """Record real spend for one turn.

        This is a **record** path, not an estimate: an unrecognized model
        must not be silently billed at cost_for's conservative fallback
        rate (that's the right call for a *budget* estimate, wrong here) --
        it's recorded as $0 and the session is flagged `unpriced` instead
        (#669).

        (#699) The remote fallback provider's model id (e.g. a Fireworks
        path) is never in pricing.PRICING -- that table is Anthropic-only
        plus the free "local" sentinel. Its rate, when known, comes from
        `settings.remote_llm_{input,output}_price_per_mtok` instead (set
        by #654), mirroring the `force_remote` branch in
        `agent_loop.py`'s `_track_usage` exactly. Unset rates still record
        as real, unpriced spend -- never fallback-priced, same #669
        convention as the unknown-model branch below.
        """
        usage = getattr(response, "usage", None)
        if not usage:
            return
        tokens_in = getattr(usage, "input_tokens", 0)
        tokens_out = getattr(usage, "output_tokens", 0)
        if self.is_remote:
            from config.settings import settings as _settings
            input_price = _settings.remote_llm_input_price_per_mtok
            output_price = _settings.remote_llm_output_price_per_mtok
            if input_price is None or output_price is None:
                dollars = 0.0
                unpriced = True
            else:
                dollars = (
                    (tokens_in / 1_000_000) * input_price
                    + (tokens_out / 1_000_000) * output_price
                )
                unpriced = False
        elif is_known_model(self.model_name):
            dollars = cost_for(self.model_name, tokens_in, tokens_out)
            unpriced = False
        else:
            dollars = 0.0
            unpriced = True
        self.session_store.record_spend(
            session.task_id, tokens_in, tokens_out, dollars, unpriced=unpriced
        )

    # ------------------------------------------------------------------
    # Finalizers
    # ------------------------------------------------------------------

    def _cascade_kill_lineage(self, root_session_id: str, reason: str) -> None:
        """Mark all non-terminal descendants of `root_session_id` as FAILED.

        Called when the root's lineage-aggregate budget is exhausted so a
        runaway sub-tree can't keep spending after the root has hit its cap.
        Managed-driven children are also killed remotely if a driver is
        available (the parent local executor doesn't have one — that's
        handled by Worker._cascade_kill_managed_children when triggered from
        the worker side; here we just flip DB status).
        """
        from api.services.agent_worker.session_store import STATUS_FAILED, TERMINAL_STATUSES
        for descendant in self.session_store.list_descendants(root_session_id):
            if descendant.status in TERMINAL_STATUSES:
                continue
            self.session_store.update_status(descendant.task_id, STATUS_FAILED)
            self.transcript_store.append(
                descendant.session_id, "cascade_killed",
                {"root": root_session_id, "reason": reason},
            )

    def _served_by(self) -> str:
        """(#699) Model id that actually ran this session, when that
        differs from what the routing name ("local") implies -- i.e. this
        executor is on the flag-gated remote fallback. Empty for the
        ordinary local llama-server path, so a caller (worker.py) only
        needs to add anything to its messaging when there's something
        worth reporting (#658: report observed, not configured)."""
        return self.model_name if self.is_remote else ""

    def _finalize_completed(self, session, final_text: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_COMPLETED)
        # Persist the body, not just the length. The final text is also sent
        # to Telegram, but the transcript is the only durable record an
        # operator can grep later to see what an agent actually said.
        self.transcript_store.append(
            session.session_id, "completed",
            {"final_chars": len(final_text or ""), "final_text": final_text or ""},
        )
        return ExecutorOutcome(
            status=STATUS_COMPLETED, final_text=final_text or "", served_by=self._served_by(),
        )

    def _finalize_failed(self, session, reason: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(session.session_id, "failed", {"reason": reason})
        return ExecutorOutcome(status=STATUS_FAILED, reason=reason, served_by=self._served_by())

    def _finalize_budget_exceeded(self, session, kind: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_BUDGET_EXCEEDED)
        self.transcript_store.append(
            session.session_id, "budget_exceeded", {"kind": kind}
        )
        return ExecutorOutcome(
            status=STATUS_BUDGET_EXCEEDED, reason=f"budget exceeded ({kind})",
            served_by=self._served_by(),
        )

    def _finalize_sleeping(self, session, seconds: int) -> ExecutorOutcome:
        wake_at = int(time.time()) + int(seconds)
        self.session_store.add_sleep(session.session_id, wake_at=wake_at)
        self.session_store.update_status(session.task_id, STATUS_YIELDED)
        self.transcript_store.append(
            session.session_id, "sleep", {"seconds": int(seconds), "wake_at": wake_at}
        )
        return ExecutorOutcome(status=STATUS_YIELDED, wake_at=wake_at, served_by=self._served_by())
