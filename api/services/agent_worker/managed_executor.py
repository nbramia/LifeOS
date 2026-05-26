"""Manages the lifecycle of a Claude-routed (Managed Agents) session.

Mirrors `LocalExecutor` in shape so the worker can treat both paths uniformly:

  - `start(session, task) -> ExecutorOutcome` — creates the remote session,
    returns immediately with status STATUS_RUNNING so the tick loop can move on
  - `poll(session) -> ExecutorOutcome` — fetches events accumulated since the
    last poll, mirrors to the transcript, updates token + dollar totals, and
    finalizes if the remote session reached a terminal state

The poll-based design (instead of long-lived SSE) keeps the worker single-
threaded and trivially restartable: state is in the DB, the only client-side
context needed is `last_event_id` for the resume cursor.
"""
from __future__ import annotations

import logging
import time

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.managed_driver import (
    TERMINAL_REMOTE_STATUSES,
    ManagedAgentsDriver,
)
from api.services.agent_worker.pricing import (
    MANAGED_SESSION_HOUR_OVERHEAD,
    cost_for,
)
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


logger = logging.getLogger(__name__)


def _user_message_for(task: dict, session_id: str, expected_output: str, budget: dict) -> str:
    """The initial user turn sent to a managed session.

    Purely task-specific. Persona, ambiguity policy, and output-format
    requirements all live in the agent preset (Anthropic console). The
    `session_id` parameter is accepted for back-compat but not injected
    into the message body — it's already in the session metadata and the
    model can't act on it.

    Budget is framed as a *soft* target: Anthropic doesn't enforce it
    server-side, the worker kills the session externally on breach.
    Saying "soft" prevents the model from over-regulating its own pacing.
    """
    del session_id  # already in session metadata; not needed in the user message
    title = (task.get("description") or "").strip()
    context = task.get("context")
    max_dollars = budget.get("max_dollars")
    dollars_str = f"~${max_dollars}" if max_dollars is not None else "unset"
    parts = [f"Task: {title}"]
    if context:
        parts.append(f"Context: {context}")
    parts.append(
        f"expected_output={expected_output}; "
        f"soft budget ~{budget.get('wall_seconds')}s wall / "
        f"~{budget.get('max_tokens')} tokens / {dollars_str}."
    )
    return "\n\n".join(parts)


class ManagedExecutor:
    """Coordinates a Managed Agents session from create through terminal event.

    All persona / tool / MCP / model config lives in the agent preset (created
    once in the Anthropic console). The executor just glues a task description
    to an agent_id + environment_id and polls for completion.
    """

    def __init__(
        self,
        session_store: SessionStore,
        transcript_store: TranscriptStore,
        driver: ManagedAgentsDriver,
        agent_id: str,
        environment_id: str,
        vault_ids: list[str] | None = None,
        model: str = "claude-sonnet-4-6",
    ):
        self.session_store = session_store
        self.transcript_store = transcript_store
        self.driver = driver
        self.agent_id = agent_id
        self.environment_id = environment_id
        self.vault_ids = list(vault_ids) if vault_ids else []
        # `model` is informational only — the actual model is whatever the
        # agent preset says. Kept for token-cost accounting (pricing.cost_for).
        self.model = model

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, session, task: dict) -> ExecutorOutcome:
        """Create the remote session and post the initial user message.

        On success: STATUS_RUNNING with `managed_agent_session_id` populated.
        On failure (network, API rejection, etc.): STATUS_FAILED.
        """
        sid = session.session_id
        budget = session.budget or {}
        self.session_store.update_status(session.task_id, STATUS_RUNNING)
        # Drop any cursor state from a prior session on this task_id. Without
        # this, the new session's first poll would carry a stale last_event_id
        # belonging to a now-defunct remote session and the events endpoint
        # would 400 on every subsequent poll.
        self.session_store.reset_managed_cursor(session.task_id)
        self.transcript_store.append(sid, "managed_start",
                                     {"agent_id": self.agent_id, "environment_id": self.environment_id})

        try:
            remote_id = self.driver.create_session(
                agent_id=self.agent_id,
                environment_id=self.environment_id,
                vault_ids=self.vault_ids,
                initial_message=_user_message_for(
                    task, sid, session.expected_output or "text", budget,
                ),
                metadata={"lifeos_session_id": sid, "task_id": session.task_id},
                title=(task.get("description") or "")[:100] or None,
            )
        except Exception as exc:
            # Log without exc_info — httpx exceptions can attach the request
            # object whose headers include the API key.
            logger.error("managed create_session failed for %s: %s", sid, type(exc).__name__)
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            self.transcript_store.append(sid, "managed_create_failed", {"error_type": type(exc).__name__})
            return ExecutorOutcome(status=STATUS_FAILED, reason=f"create_session failed: {type(exc).__name__}")

        self.session_store.set_managed_session_id(session.task_id, remote_id)
        self.transcript_store.append(sid, "managed_created", {"remote_id": remote_id})
        # Mark as STATUS_RUNNING but with a remote handle attached. The
        # worker's tick loop will pick it up via _poll_managed_sessions on
        # subsequent ticks.
        return ExecutorOutcome(status=STATUS_RUNNING)

    def poll(self, session) -> ExecutorOutcome:
        """Fetch new events from the remote session and advance state.

        Returns an ExecutorOutcome reflecting the current state:
        - STATUS_RUNNING — no terminal event yet (worker continues polling)
        - STATUS_COMPLETED / STATUS_FAILED / STATUS_BUDGET_EXCEEDED — terminal
        """
        if not session.managed_agent_session_id:
            return ExecutorOutcome(status=STATUS_FAILED, reason="no managed_agent_session_id")

        sid = session.session_id
        remote_id = session.managed_agent_session_id
        last_event_id = self.session_store.get_managed_last_event_id(session.task_id)

        try:
            state = self.driver.get_session_state(remote_id, since_event_id=last_event_id)
        except Exception as exc:
            # Transient errors: keep going, try again next poll. Don't kill
            # the session here — operator may want to retry.
            logger.warning("managed poll failed for %s: %s", remote_id, exc)
            return ExecutorOutcome(status=STATUS_RUNNING, reason=f"poll error: {exc}")

        # Mirror events to transcript.
        for event in state.new_events:
            self.transcript_store.append(sid, f"managed_event_{event.get('type', 'unknown')}", event)

        # 1. Token spend delta — compare absolute remote totals to our row.
        delta_in = max(0, state.total_input_tokens - (session.total_input_tokens or 0))
        delta_out = max(0, state.total_output_tokens - (session.total_output_tokens or 0))
        token_delta_dollars = cost_for(self.model, delta_in, delta_out)
        if delta_in or delta_out:
            self.session_store.record_spend(
                session.task_id, delta_in, delta_out, token_delta_dollars,
            )

        # 2. Session-hour overhead delta — we only want to add what's accrued
        # since the last poll. The driver doesn't tell us total wall time, so
        # we derive it from `started_at` and book the *delta* over the
        # already-accrued figure stored on the row.
        wall_so_far = max(0.0, time.time() - float(session.started_at))
        cumulative_hourly = (wall_so_far / 3600.0) * MANAGED_SESSION_HOUR_OVERHEAD
        prior_hourly = self.session_store.get_accrued_session_hour_dollars(session.task_id)
        hourly_delta = max(0.0, cumulative_hourly - prior_hourly)
        if hourly_delta > 0:
            self.session_store.add_session_hour_overhead(session.task_id, hourly_delta)
            self.session_store.set_accrued_session_hour_dollars(session.task_id, cumulative_hourly)

        # 3. Mid-run budget breach: kill the remote session before it racks
        # up more cost. The check uses the refreshed in-flight totals so the
        # session-hour delta we just booked is included.
        budget = session.budget or {}
        refreshed = self.session_store.get(session.task_id)
        breach = self._budget_breach(refreshed, budget)
        if breach:
            try:
                self.driver.kill_session(remote_id, reason=f"budget_exceeded:{breach}")
            except Exception as exc:  # pragma: no cover — best-effort kill
                logger.warning("kill_session %s failed: %s", remote_id, exc)
            self.session_store.update_status(session.task_id, STATUS_BUDGET_EXCEEDED)
            self.transcript_store.append(sid, "budget_exceeded", {"kind": breach, "source": "client"})
            return ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, reason=f"budget exceeded ({breach})")

        if state.last_event_id:
            self.session_store.set_managed_last_event_id(session.task_id, state.last_event_id)

        # Cache the latest non-empty agent.message text. `get_session_state`
        # advances a cursor, so a poll that returns only `session.status_idle`
        # will have `final_text=None` even though the agent did produce text
        # in an earlier batch. Persisting here guarantees the finalize step
        # surfaces real output instead of an empty completion summary.
        if state.final_text:
            self.session_store.set_managed_final_text(session.task_id, state.final_text)

        # Terminal handling.
        if state.status in TERMINAL_REMOTE_STATUSES:
            return self._finalize_remote(session, state)

        return ExecutorOutcome(status=STATUS_RUNNING)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _budget_breach(refreshed_session, budget: dict) -> str | None:
        """Return the budget kind that was exceeded, or None."""
        if not budget:
            return None
        if budget.get("max_tokens"):
            used = (refreshed_session.total_input_tokens or 0) + (refreshed_session.total_output_tokens or 0)
            if used >= budget["max_tokens"]:
                return "max_tokens"
        if budget.get("max_dollars") is not None:
            if (refreshed_session.total_dollars or 0) >= budget["max_dollars"]:
                return "max_dollars"
        return None

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _finalize_remote(self, session, state) -> ExecutorOutcome:
        # Session-hour overhead has already been booked incrementally in
        # poll(), so finalize doesn't need to add anything more — just record
        # the terminal status.
        # The Managed Agents API reports a successful terminal as `"idle"`
        # (live-confirmed 2026-05-26). `"completed"` is kept here as a
        # synthesized alias for forward-compat in case the API later transitions
        # status fields between the two.
        if state.status in ("idle", "completed"):
            # `state.final_text` reflects only events in *this* poll batch. If
            # the agent.message arrived in a prior batch and only the idle
            # event arrived now, fall back to the cached value persisted by
            # poll().
            final_text = state.final_text or self.session_store.get_managed_final_text(session.task_id) or ""
            self.session_store.update_status(session.task_id, STATUS_COMPLETED)
            self.transcript_store.append(
                session.session_id, "managed_completed",
                {"final_chars": len(final_text),
                 "remote_status": state.status,
                 "init_failed_mcps": list(state.init_failed_mcps)},
            )
            return ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=final_text,
                init_failed_mcps=list(state.init_failed_mcps),
            )

        if state.status == "budget_exceeded":
            self.session_store.update_status(session.task_id, STATUS_BUDGET_EXCEEDED)
            self.transcript_store.append(session.session_id, "managed_budget_exceeded", {})
            return ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED,
                                   reason=state.error_reason or "remote budget exceeded")

        if state.status == "cancelled":
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            reason = state.error_reason or "session cancelled remotely"
            self.transcript_store.append(session.session_id, "managed_cancelled", {"reason": reason})
            return ExecutorOutcome(status=STATUS_FAILED, reason=f"cancelled: {reason}")

        # failed (or unknown terminal)
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        reason = state.error_reason or f"remote status={state.status}"
        self.transcript_store.append(session.session_id, "managed_failed", {"reason": reason})
        return ExecutorOutcome(status=STATUS_FAILED, reason=reason)
