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
from dataclasses import dataclass

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


@dataclass
class ExecutorOutcome:
    """What happened during a single `execute(session)` invocation."""
    status: str   # one of STATUS_COMPLETED / STATUS_FAILED / STATUS_BUDGET_EXCEEDED / STATUS_YIELDED
    final_text: str = ""
    reason: str = ""
    wake_at: int | None = None   # set when status == STATUS_YIELDED (sleep)


def _system_prompt(session_id: str, expected_output: str, budget) -> str:
    """System message for the executor agent."""
    return (
        "You are an autonomous agent running inside LifeOS, handling a single "
        "task from the user's task manager. You have access to file, shell, "
        "and LifeOS data tools. Work concisely and stop when the task is "
        "complete — your final assistant turn (with no tool calls) is the "
        f"answer the user will see.\n\n"
        f"Your session_id is {session_id}. Your expected output shape is "
        f"`{expected_output}`. Your budget is wall={budget.get('wall_seconds')}s, "
        f"max_tokens={budget.get('max_tokens')}, max_dollars=${budget.get('max_dollars')}.\n\n"
        "If you need to wait for external state to change, call the `sleep` "
        "tool rather than busy-looping. If you cannot complete the task "
        "safely or have hit an ambiguity you cannot resolve from context, "
        "say so plainly in your final response — do not invent answers."
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
        wall_deadline = self._wall_deadline(session, budget)
        self.session_store.update_status(session.task_id, STATUS_RUNNING)

        if not self.session_store.get_messages(sid):
            self._seed_conversation(session, task, budget)

        while True:
            # Wall-clock budget check.
            if wall_deadline and time.time() > wall_deadline:
                return self._finalize_budget_exceeded(session, "wall_seconds")

            # Token budget check.
            updated = self.session_store.get(session.task_id)
            tokens_used = (updated.total_input_tokens or 0) + (updated.total_output_tokens or 0)
            if budget.get("max_tokens") and tokens_used >= budget["max_tokens"]:
                return self._finalize_budget_exceeded(session, "max_tokens")
            # Dollar budget (local is $0 but the path is exercised for parity).
            if budget.get("max_dollars") is not None and (updated.total_dollars or 0) >= budget["max_dollars"]:
                return self._finalize_budget_exceeded(session, "max_dollars")

            try:
                response = self._call_llm(sid)
            except Exception as exc:
                logger.exception("local executor LLM call failed: %s", exc)
                return self._finalize_failed(session, f"LLM call failed: {exc}")

            self._persist_assistant_turn(sid, response)
            self._record_spend(session, response)

            tool_calls = response.tool_calls or []
            if not tool_calls:
                # Final answer — no more tool calls expected.
                return self._finalize_completed(session, response.text)

            yielded_seconds: int | None = None
            tool_results: list[dict] = []
            for i, call in enumerate(tool_calls[:MAX_TOOL_CALLS_PER_TURN]):
                name = getattr(call, "name", None) or call.get("name", "")
                args = getattr(call, "input", None) or call.get("input", {})
                call_id = (getattr(call, "id", None)
                           or call.get("id")
                           or f"call_{uuid.uuid4().hex[:12]}")
                result: ToolResult = self.tools.dispatch(name, args or {})
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
                    yielded_seconds = result.yield_seconds
                    # Stop dispatching further tools this turn — we're going to yield.
                    break

            # Persist the tool_results as a user-role turn (Anthropic convention).
            self.session_store.append_message(sid, "user", tool_results)

            if yielded_seconds is not None:
                return self._finalize_sleeping(session, yielded_seconds)

    # ------------------------------------------------------------------
    # Conversation persistence
    # ------------------------------------------------------------------

    def _seed_conversation(self, session, task: dict, budget: dict) -> None:
        sid = session.session_id
        system = _system_prompt(sid, session.expected_output or "text", budget)
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

    def _persist_assistant_turn(self, session_id: str, response) -> None:
        # Build an Anthropic-style content list mixing text + tool_use blocks.
        content_blocks: list[dict] = []
        if response.text:
            content_blocks.append({"type": "text", "text": response.text})
        for call in (response.tool_calls or []):
            name = getattr(call, "name", None) or call.get("name", "")
            args = getattr(call, "input", None) or call.get("input", {})
            call_id = (getattr(call, "id", None)
                       or call.get("id")
                       or f"call_{uuid.uuid4().hex[:12]}")
            content_blocks.append({
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": args,
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

    def _wall_deadline(self, session, budget: dict) -> float | None:
        wall = budget.get("wall_seconds")
        if not wall:
            return None
        return float(session.started_at) + float(wall)

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
