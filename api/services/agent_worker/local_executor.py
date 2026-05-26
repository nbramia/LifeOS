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

from api.services.agent_worker.pricing import cost_for
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


# Static portion of the system prompt — never changes between sessions. Kept
# as a module-level constant so prompt caches can hit on it; the dynamic
# per-session bits (expected output, soft budget) are appended at call time
# and live in a small trailing section so cache invalidation is minimized.
_SYSTEM_PROMPT_STATIC = """\
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
</environment>

<mcp_routing>
Default to the `lifeos` MCP for any personal-data query. It is faster and
more accurate than scraping the filesystem directly. Use the standard
`Read` / `Write` / `Edit` tools only for files outside the indexed data
set (code, scratch notes, etc.). Use `Bash` for shell operations.
</mcp_routing>

<output_format>
Every task must end with a final assistant turn containing a one-paragraph
text summary. Tool calls alone are not a complete response. After your
last tool call, produce a text turn that summarizes what you did and the
key result. Be concrete: include specific names, counts, decisions, and
links. Skip filler phrases.
</output_format>

<ambiguity>
Do not ask clarifying questions during execution. If a task is genuinely
ambiguous, make a reasonable assumption, do the work, and note the
assumption in your final summary. If you cannot complete the task safely,
say so plainly in your final response.
</ambiguity>

<inter_agent>
Other agent sessions are visible via `lifeos_agent_transcript_read` and
`lifeos_agent_sessions_list`. Spawn child agents with `lifeos_agent_spawn`,
message them with `lifeos_agent_send`, check status with
`lifeos_agent_check`. When you have nothing to do until specific children
finish, call `lifeos_agent_yield_until(children=[...])` — this ends your
session cleanly (no idle billing) and resumes you when the children are
done. Prefer `yield_until` over polling.
</inter_agent>

<sleep>
When you need to wait for external state to change with no child sessions
to await, call the `sleep` tool rather than busy-looping.
</sleep>"""


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
    del session_id, parent_session_id  # logging-only artifacts, not for the model
    wall = budget.get("wall_seconds")
    max_tokens = budget.get("max_tokens")
    max_dollars = budget.get("max_dollars")
    dollars_str = f"~${max_dollars}" if max_dollars is not None else "unset"
    return (
        _SYSTEM_PROMPT_STATIC
        + "\n\n<this_task>\n"
        + f"expected_output={expected_output}; "
        + f"soft budget ~{wall}s wall / ~{max_tokens} tokens / {dollars_str}.\n"
        + "</this_task>"
    )


def _user_message_for(task: dict) -> str:
    """Build the opening user turn from the task description."""
    title = (task.get("description") or "").strip()
    context = task.get("context")
    parts = [f"Task: {title}"]
    if context:
        parts.append(f"Context: {context}")
    parts.append("Please complete this task using the tools available.")
    return "\n\n".join(parts)


class LocalExecutor:
    """Drives one turn or one yielded resumption per call to `execute`."""

    def __init__(
        self,
        session_store: SessionStore,
        transcript_store: TranscriptStore,
        tool_registry: ToolRegistry | None = None,
        llm_client=None,
        model_name: str = "local",
    ):
        self.session_store = session_store
        self.transcript_store = transcript_store
        # Lazy-imported to keep test surface small.
        if tool_registry is None:
            tool_registry = ToolRegistry()
        self.tools = tool_registry
        if llm_client is None:
            from api.services.llm_client import LocalLLMClient
            llm_client = LocalLLMClient()
        self.llm = llm_client
        self.model_name = model_name

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Run the agent loop for one session.

        - If `messages` table is empty for this session, seed the conversation
          with a system message and a user turn built from the task.
        - Loops until the model produces a final answer, the budget is hit,
          a tool yields (sleep), or an error.
        """
        sid = session.session_id
        budget = session.budget or {}
        self.session_store.update_status(session.task_id, STATUS_RUNNING)

        if not self.session_store.get_messages(sid):
            self._seed_conversation(session, task, budget)

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
            if budget.get("max_dollars") is not None and (updated.total_dollars or 0) >= budget["max_dollars"]:
                return self._finalize_budget_exceeded(session, "max_dollars")

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
                response = self._call_llm(sid)
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
                result: ToolResult = self.tools.dispatch(name, args)
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
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result.output,
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
                return ExecutorOutcome(status=STATUS_YIELDED)

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

    def _call_llm(self, session_id: str):
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

        return self.llm.create(
            messages=messages_for_llm,
            system=system_text or None,
            max_tokens=PER_TURN_MAX_TOKENS,
            tools=self.tools.definitions(),
        )

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
        usage = getattr(response, "usage", None)
        if not usage:
            return
        tokens_in = getattr(usage, "input_tokens", 0)
        tokens_out = getattr(usage, "output_tokens", 0)
        dollars = cost_for(self.model_name, tokens_in, tokens_out)
        self.session_store.record_spend(session.task_id, tokens_in, tokens_out, dollars)

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

    def _finalize_completed(self, session, final_text: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_COMPLETED)
        self.transcript_store.append(
            session.session_id, "completed", {"final_chars": len(final_text or "")}
        )
        return ExecutorOutcome(status=STATUS_COMPLETED, final_text=final_text or "")

    def _finalize_failed(self, session, reason: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(session.session_id, "failed", {"reason": reason})
        return ExecutorOutcome(status=STATUS_FAILED, reason=reason)

    def _finalize_budget_exceeded(self, session, kind: str) -> ExecutorOutcome:
        self.session_store.update_status(session.task_id, STATUS_BUDGET_EXCEEDED)
        self.transcript_store.append(
            session.session_id, "budget_exceeded", {"kind": kind}
        )
        return ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, reason=f"budget exceeded ({kind})")

    def _finalize_sleeping(self, session, seconds: int) -> ExecutorOutcome:
        wake_at = int(time.time()) + int(seconds)
        self.session_store.add_sleep(session.session_id, wake_at=wake_at)
        self.session_store.update_status(session.task_id, STATUS_YIELDED)
        self.transcript_store.append(
            session.session_id, "sleep", {"seconds": int(seconds), "wake_at": wake_at}
        )
        return ExecutorOutcome(status=STATUS_YIELDED, wake_at=wake_at)
