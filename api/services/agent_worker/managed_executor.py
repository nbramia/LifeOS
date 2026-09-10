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

import json
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
from api.services.agent_worker.usage_ledger import (
    MEASURED,
    UsageLedger,
    UsageObservation,
    identity_for_session,
    reservation_id_for_session,
)


logger = logging.getLogger(__name__)


# Runaway detection thresholds (#139 Section 5). Picked behaviorally, not
# from cost/wall time, because thresholds based on tokens or seconds vary
# with task class; tool-call counts scale uniformly across task types.
TOOL_LOOP_KILL_THRESHOLD = 4
NO_PROGRESS_KILL_THRESHOLD = 15
MAX_TOOL_RESULT_CHARS = 20_000  # Matches local_executor's truncation behavior.
TRUNCATION_MARKER = " […truncated]"


def _tool_signature(event: dict) -> str | None:
    """Stable key for tool-loop detection: `(name, sorted-args-JSON)`.

    Returns None for non-`tool_use` events. Args are JSON-canonicalized with
    sorted keys so an agent that re-orders kwargs between calls still gets
    detected. Falls back to the raw `input` if it isn't a dict (rare, but
    don't crash the worker on a schema surprise).
    """
    if event.get("type") != "tool_use":
        return None
    name = event.get("name") or event.get("tool_name") or ""
    raw_input = event.get("input") or event.get("arguments") or {}
    try:
        args_key = json.dumps(raw_input, sort_keys=True, default=str)
    except Exception:
        args_key = repr(raw_input)
    return f"{name}::{args_key}"


def _truncate_oversized_tool_result(event: dict) -> dict:
    """Return a copy of `event` with its tool_result payload truncated if
    it exceeds MAX_TOOL_RESULT_CHARS.

    The full payload is still on Anthropic's side — this only shrinks what
    we mirror into our transcript file. The transcript is what we re-feed
    to parents on resume-after-children, so truncating here directly cuts
    that re-feed cost. The marker preserves the truncated-ness signal.
    """
    if event.get("type") != "tool_result":
        return event
    content = event.get("content")
    if not isinstance(content, str) or len(content) <= MAX_TOOL_RESULT_CHARS:
        return event
    truncated = content[: MAX_TOOL_RESULT_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    new_event = dict(event)
    new_event["content"] = truncated
    new_event["_truncated_from_chars"] = len(content)
    return new_event


def _user_message_for(task: dict, session_id: str, expected_output: str, budget: dict) -> str:
    """The initial user turn sent to a managed session.

    Prepended with the LifeOS capabilities preamble (see
    `capabilities_preamble.py`) so the agent knows what data and tools
    are available before it reads the task. The preamble is the same on
    every call and benefits from prompt caching after the first request.

    Persona, ambiguity policy, and output-format requirements live in
    the agent preset (Anthropic console).

    Budget is framed as a *soft* target: Anthropic doesn't enforce it
    server-side, the worker kills the session externally on breach.
    Saying "soft" prevents the model from over-regulating its own pacing.

    `lifeos_session_id` is injected so the agent can pass it as
    `caller_session_id` when invoking any `lifeos_agent_*` tool over
    the LifeOS MCP — that field is required by the dispatcher and
    can't be inferred server-side because the request crosses a
    process boundary.
    """
    from api.services.agent_worker.capabilities_preamble import CAPABILITIES_PREAMBLE
    title = (task.get("description") or "").strip()
    context = task.get("context")
    max_dollars = budget.get("max_dollars")
    dollars_str = f"~${max_dollars}" if max_dollars is not None else "unset"
    parts = [CAPABILITIES_PREAMBLE, f"Task: {title}"]
    if context:
        parts.append(f"Context: {context}")
    notes = (task.get("notes") or "").strip()
    if notes:
        # Reassignment notes include a bounded transcript/output handoff;
        # retain the latest operator direction without allowing an unbounded
        # vault note to dominate the first remote prompt.
        parts.append(f"Task notes:\n{notes[-6000:]}")
    from api.services.agent_worker.inter_agent import caller_proof_for_session
    from config.settings import settings
    proof = caller_proof_for_session(session_id, getattr(settings, "mcp_bearer_token", ""))
    identity = f"lifeos_session_id={session_id}; "
    if proof:
        identity += f"lifeos_session_proof={proof}; "
    parts.append(
        f"today={_today()}; "
        f"{identity}"
        f"expected_output={expected_output}; "
        f"soft budget ~{budget.get('wall_seconds')}s wall / "
        f"~{budget.get('max_tokens')} tokens / {dollars_str}."
    )
    return "\n\n".join(parts)


def _today() -> str:
    """Local today as YYYY-MM-DD (weekday). Module-level for test override."""
    from datetime import datetime
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d (%A)")


def _sanitize_title(text: str) -> str | None:
    """Strip Unicode control / format chars from a session title.

    Live bug: spawned children's titles came from the parent's freeform
    spawn prompt, which included `\\n` newlines. Anthropic 400'd with
    `"title: must not contain Unicode control or format characters"`.
    We collapse any whitespace-control chars to single spaces, drop other
    control / format codepoints entirely, then trim to 100 chars (the
    title field is for display only). Returns None for an empty result
    so the API uses the model's default.
    """
    import unicodedata
    if not text:
        return None
    cleaned_chars: list[str] = []
    for c in text:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf"):  # control / format
            if c in (" ", "\t", "\n", "\r") or c.isspace():
                if cleaned_chars and cleaned_chars[-1] != " ":
                    cleaned_chars.append(" ")
            # else: drop entirely
        else:
            cleaned_chars.append(c)
    cleaned = "".join(cleaned_chars).strip()[:100]
    return cleaned or None


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
        model: str = "claude-sonnet-5",
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

    def _is_current(self, session) -> bool:
        return self.session_store.is_current_turn(
            session.task_id, session.attempt_id, session.turn_id,
        )

    def _is_cancelled(self, session) -> bool:
        """Whether this exact Managed turn has a durable cancellation fence."""
        return self.session_store.is_cancelled(
            session.task_id, session.attempt_id, session.turn_id,
        )

    @staticmethod
    def _with_identity(session, outcome: ExecutorOutcome) -> ExecutorOutcome:
        """Attach the persisted turn snapshot to every managed outcome."""
        from dataclasses import replace
        return replace(
            outcome,
            session_id=getattr(outcome, "session_id", None) or session.session_id,
            attempt_id=getattr(outcome, "attempt_id", None) or session.attempt_id,
            turn_id=getattr(outcome, "turn_id", None) or session.turn_id,
            executor="claude",
            continuation_id=(
                getattr(outcome, "continuation_id", None)
                or session.managed_agent_session_id
            ),
        )

    def start(self, session, task: dict) -> ExecutorOutcome:
        """Create the remote session, apply the per-class tool filter (if any),
        and post the initial user message.

        Flow (#139 §3):
          1. POST /v1/sessions — container provisioned, no LLM cost yet.
          2. POST /v1/sessions/{id} with the per-class tool filter (full
             agent.tools replacement). Skipped for `preset_class=fullstack`
             or unset (no filter, use preset as-is). LLM cost still zero.
          3. POST /v1/sessions/{id}/events with user.message — cache_creation
             fires now, scoped to the filtered tool set.

        On success: STATUS_RUNNING with `managed_agent_session_id` populated.
        On failure (network, API rejection, etc.): STATUS_FAILED.
        """
        session = self.session_store.begin_executor_turn(
            session.task_id, "start", session=session,
        )
        sid = session.session_id
        budget = session.budget or {}
        self.session_store.update_status(
            session.task_id, STATUS_RUNNING,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        # Drop any cursor state from a prior session on this task_id. Without
        # this, the new session's first poll would carry a stale last_event_id
        # belonging to a now-defunct remote session and the events endpoint
        # would 400 on every subsequent poll.
        self.session_store.reset_managed_cursor(
            session.task_id, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        self.transcript_store.append(sid, "managed_start",
                                     {"agent_id": self.agent_id, "environment_id": self.environment_id})

        initial_message = _user_message_for(
            task, sid, session.expected_output or "text", budget,
        )
        preset_class = getattr(session, "preset_class", None)

        try:
            # Step 1: create the session WITHOUT initial_message. Provisions
            # the container; no agent loop runs and no LLM cost is incurred.
            remote_id = self.driver.create_session(
                agent_id=self.agent_id,
                environment_id=self.environment_id,
                vault_ids=self.vault_ids,
                metadata={"lifeos_session_id": sid, "task_id": session.task_id},
                title=_sanitize_title(task.get("description") or ""),
            )
        except Exception as exc:
            # Log without exc_info — httpx exceptions can attach the request
            # object whose headers include the API key.
            logger.error("managed create_session failed for %s: %s", sid, type(exc).__name__)
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(sid, "managed_create_failed", {"error_type": type(exc).__name__})
            return self._with_identity(
                session, ExecutorOutcome(status=STATUS_FAILED, reason=f"create_session failed: {type(exc).__name__}")
            )

        # Cancellation/reopen can race the remote provisioning call.  Never
        # attach or drive a remote session for an obsolete local turn.
        if not self._is_current(session):
            try:
                self.driver.kill_session(remote_id, reason="stale_lifecycle_turn")
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("stale managed session cleanup failed for %s", remote_id)
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))

        self.session_store.set_managed_session_id(
            session.task_id, remote_id,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        self.transcript_store.append(sid, "managed_created", {"remote_id": remote_id})

        # Step 2: apply per-class tool filter if one was selected. Skipped for
        # fullstack / unset / unknown classes. Best-effort — a filter failure
        # falls back to the full preset rather than aborting the session.
        from api.services.agent_worker.tool_filter import class_to_tool_filter
        agent_payload = class_to_tool_filter(preset_class) if preset_class else None
        if agent_payload is not None:
            try:
                self.driver.update_session(remote_id, agent_payload)
                self.transcript_store.append(
                    sid, "managed_filter_applied",
                    {"preset_class": preset_class, "tool_count": len(agent_payload.get("tools", []))},
                )
            except Exception as exc:
                logger.warning(
                    "managed update_session failed for %s (class=%s): %s — falling back to full preset",
                    sid, preset_class, type(exc).__name__,
                )
                self.transcript_store.append(
                    sid, "managed_filter_failed",
                    {"preset_class": preset_class, "error_type": type(exc).__name__},
                )

        # Step 3: post the initial user message. cache_creation fires here on
        # the (possibly filtered) tool set.
        if not self._is_current(session):
            try:
                self.driver.kill_session(remote_id, reason="stale_lifecycle_turn")
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("stale managed session cleanup failed for %s", remote_id)
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
        try:
            self.driver.post_user_message(remote_id, initial_message)
        except Exception as exc:
            logger.error("managed post_user_message failed for %s: %s", sid, type(exc).__name__)
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(
                sid, "managed_post_failed", {"error_type": type(exc).__name__},
            )
            return self._with_identity(session, ExecutorOutcome(
                status=STATUS_FAILED,
                reason=f"post_user_message failed: {type(exc).__name__}",
            ))

        # The worker's tick loop will pick this up via _poll_managed_sessions
        # on subsequent ticks.
        return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))

    def poll(self, session) -> ExecutorOutcome:
        """Fetch new events from the remote session and advance state.

        Returns an ExecutorOutcome reflecting the current state:
        - STATUS_RUNNING — no terminal event yet (worker continues polling)
        - STATUS_COMPLETED / STATUS_FAILED / STATUS_BUDGET_EXCEEDED — terminal
        """
        poll_started = time.monotonic()
        if not self._is_current(session):
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
        if not session.managed_agent_session_id:
            return self._with_identity(
                session, ExecutorOutcome(status=STATUS_FAILED, reason="no managed_agent_session_id")
            )

        sid = session.session_id
        remote_id = session.managed_agent_session_id
        last_event_id = self.session_store.get_managed_last_event_id(
            session.task_id, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )

        try:
            state = self.driver.get_session_state(remote_id, since_event_id=last_event_id)
        except Exception as exc:
            # Transient errors: keep going, try again next poll. Don't kill
            # the session here — operator may want to retry.
            logger.warning("managed poll failed for %s: %s", remote_id, exc)
            return self._with_identity(
                session, ExecutorOutcome(status=STATUS_RUNNING, reason=f"poll error: {exc}")
            )

        if not self._is_current(session):
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
        self.session_store.record_active_seconds(
            session.task_id, time.monotonic() - poll_started,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )

        # Mirror events to transcript, truncating oversized tool_results so
        # the transcript doesn't bloat for huge payloads that we'd re-feed
        # to parents during spawn-after-children resume.
        for event in state.new_events:
            stored = _truncate_oversized_tool_result(event)
            self.transcript_store.append(sid, f"managed_event_{stored.get('type', 'unknown')}", stored)

        # Update runaway counters from the new events (#139 Section 5).
        runaway_kind = self._detect_runaway(
            session.task_id, state.new_events,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        if runaway_kind:
            try:
                self.driver.kill_session(remote_id, reason=runaway_kind)
            except Exception as exc:  # pragma: no cover — best-effort kill
                logger.warning("kill_session %s failed: %s", remote_id, exc)
            self.session_store.update_status(
                session.task_id, STATUS_BUDGET_EXCEEDED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(sid, "runaway_killed", {"kind": runaway_kind})
            return self._with_identity(session, ExecutorOutcome(
                status=STATUS_BUDGET_EXCEEDED,
                reason=f"runaway killed ({runaway_kind})",
            ))

        # 1. Token spend delta. Managed's provider totals are cumulative over
        # the remote session, while a LifeOS session can have several turns.
        # Keep a provider high-water mark and a per-turn baseline so a resumed
        # turn gets a relative cumulative snapshot instead of replaying all
        # prior provider usage into a new canonical ledger key.
        sid, attempt_id, turn_id = identity_for_session(session)
        raw_totals = {
            "input_tokens": max(0, int(state.total_input_tokens)),
            "output_tokens": max(0, int(state.total_output_tokens)),
            "cache_creation_tokens": max(0, int(state.total_cache_creation_tokens)),
            "cache_read_tokens": max(0, int(state.total_cache_read_tokens)),
        }
        prior = self.session_store.get_managed_usage_snapshot(
            session.task_id, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        same_provider = prior.get("provider_session_id") == remote_id
        prior_highwater = {
            name: int(prior.get(name, 0) or 0) if same_provider else 0
            for name in raw_totals
        }
        turn_key = f"{attempt_id}:{turn_id}"
        if same_provider and prior.get("turn_key") != turn_key:
            baseline = dict(prior_highwater)
        elif same_provider:
            baseline = {
                name: int(prior.get(f"baseline_{name}", 0) or 0)
                for name in raw_totals
            }
        else:
            baseline = {name: 0 for name in raw_totals}
        relative_totals = {
            name: max(0, raw_totals[name] - baseline[name])
            for name in raw_totals
        }
        provider_advanced = any(raw_totals[name] > prior_highwater[name] for name in raw_totals)
        if provider_advanced and any(relative_totals.values()):
            # The Managed driver exposes cumulative totals. Feed a normalized
            # per-turn snapshot to the canonical ledger so reconnect/replay
            # and resumed turns remain monotonic without cross-turn replay.
            try:
                UsageLedger(self.session_store.db_path).record(UsageObservation(
                    session_id=sid, attempt_id=attempt_id, turn_id=turn_id,
                    source="managed_executor",
                    input_tokens=relative_totals["input_tokens"],
                    output_tokens=relative_totals["output_tokens"],
                    cache_creation_tokens=relative_totals["cache_creation_tokens"],
                    cache_read_tokens=relative_totals["cache_read_tokens"],
                    input_kind=MEASURED, output_kind=MEASURED,
                    cache_creation_kind=MEASURED, cache_read_kind=MEASURED,
                    cost_usd=cost_for(
                        self.model,
                        relative_totals["input_tokens"],
                        relative_totals["output_tokens"],
                        cache_creation_tokens=relative_totals["cache_creation_tokens"],
                        cache_read_tokens=relative_totals["cache_read_tokens"],
                    ),
                    cost_kind=MEASURED, billing_class="metered",
                    requested_engine="claude", requested_model=getattr(session, "model", None) or self.model,
                    # The configured Managed model is a request, not
                    # authoritative evidence of what the provider served.
                    # Keep served identity unknown unless the provider emits
                    # an explicit served-model record.
                    evidence_source="managed_driver_usage_state",
                    source_event_id=state.last_event_id or (
                        f"totals:{raw_totals['input_tokens']}:{raw_totals['output_tokens']}:"
                        f"{raw_totals['cache_creation_tokens']}:{raw_totals['cache_read_tokens']}"
                    ),
                    event_id=f"managed:{sid}:{attempt_id}:{turn_id}:"
                    f"{state.last_event_id or raw_totals['input_tokens']}",
                    cumulative=True,
                    reservation_id=reservation_id_for_session(session),
                ))
            except Exception:  # noqa: BLE001 — accounting cannot alter execution
                logger.warning("managed usage ledger write failed for %s", session.task_id, exc_info=True)
        next_highwater = {
            name: max(prior_highwater[name], raw_totals[name])
            for name in raw_totals
        }
        self.session_store.set_managed_usage_snapshot(
            session.task_id,
            {
                "provider_session_id": remote_id,
                "turn_key": turn_key,
                **{f"baseline_{name}": baseline[name] for name in raw_totals},
                **next_highwater,
            },
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )

        # 2. Session-hour overhead delta — we only want to add what's accrued
        # since the last poll. The driver doesn't tell us total wall time, so
        # we derive it from `started_at` and book the *delta* over the
        # already-accrued figure stored on the row.
        wall_so_far = max(0.0, time.time() - float(session.started_at))
        cumulative_hourly = (wall_so_far / 3600.0) * MANAGED_SESSION_HOUR_OVERHEAD
        prior_hourly = self.session_store.get_accrued_session_hour_dollars(
            session.task_id, attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        hourly_delta = max(0.0, cumulative_hourly - prior_hourly)
        if hourly_delta > 0:
            self.session_store.add_session_hour_overhead(
                session.task_id, hourly_delta,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.session_store.set_accrued_session_hour_dollars(
                session.task_id, cumulative_hourly,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )

        # 3. Mid-run budget breach: kill the remote session before it racks
        # up more cost. The check uses the refreshed in-flight totals so the
        # session-hour delta we just booked is included.
        budget = session.budget or {}
        refreshed = self.session_store.get(session.task_id)
        if refreshed is None or not self._is_current(session):
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
        breach = self._budget_breach(refreshed, budget)
        if breach:
            try:
                self.driver.kill_session(remote_id, reason=f"budget_exceeded:{breach}")
            except Exception as exc:  # pragma: no cover — best-effort kill
                logger.warning("kill_session %s failed: %s", remote_id, exc)
            self.session_store.update_status(
                session.task_id, STATUS_BUDGET_EXCEEDED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(sid, "budget_exceeded", {"kind": breach, "source": "client"})
            return self._with_identity(
                session, ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, reason=f"budget exceeded ({breach})")
            )

        if state.last_event_id:
            self.session_store.set_managed_last_event_id(
                session.task_id, state.last_event_id,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )

        # Cache the latest non-empty agent.message text. `get_session_state`
        # advances a cursor, so a poll that returns only `session.status_idle`
        # will have `final_text=None` even though the agent did produce text
        # in an earlier batch. Persisting here guarantees the finalize step
        # surfaces real output instead of an empty completion summary.
        if state.final_text:
            self.session_store.set_managed_final_text(
                session.task_id, state.final_text,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )

        # Terminal handling.
        if state.status in TERMINAL_REMOTE_STATUSES:
            return self._finalize_remote(session, state)

        return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _detect_runaway(
        self, task_id: str, new_events: list[dict], *,
        attempt_id: str | None = None, turn_id: str | None = None,
    ) -> str | None:
        """Walk `new_events`, update persisted runaway counters, and return
        a kill reason if any threshold tripped — else None.

        Two behavioral signals:
        - `tool_loop_detected`: same tool + identical args fires
          TOOL_LOOP_KILL_THRESHOLD consecutive times with no intervening
          *different* tool call. A different tool resets the count, so the
          agent can legitimately retry after a transient failure (e.g. a
          tool returning an error then immediately retrying same args ONCE
          is fine; 4× in a row with no diversity is not).
        - `no_text_in_15_tool_calls`: NO_PROGRESS_KILL_THRESHOLD `tool_use`
          events fire without any intervening `agent.message`. Indicates
          the agent is tool-thrashing without surfacing its reasoning.

        Persisted state survives worker restarts so cross-poll counting is
        consistent. State is reset by `agent.message` (no-progress) or a
        change in tool signature (tool-loop).
        """
        state = self.session_store.get_runaway_state(
            task_id, attempt_id=attempt_id, turn_id=turn_id,
        )
        signature = state["tool_loop_signature"]
        loop_count = state["tool_loop_count"]
        since_msg = state["tool_calls_since_message"]

        kill: str | None = None
        for event in new_events:
            etype = event.get("type", "")
            if etype == "agent.message":
                since_msg = 0
                continue
            if etype != "tool_use":
                continue
            since_msg += 1
            if since_msg >= NO_PROGRESS_KILL_THRESHOLD:
                kill = "no_text_in_15_tool_calls"
                break
            sig = _tool_signature(event)
            if sig is None:
                continue
            if sig == signature:
                loop_count += 1
            else:
                signature = sig
                loop_count = 1
            if loop_count >= TOOL_LOOP_KILL_THRESHOLD:
                kill = "tool_loop_detected"
                break

        self.session_store.set_runaway_state(
            task_id,
            tool_loop_signature=signature,
            tool_loop_count=loop_count,
            tool_calls_since_message=since_msg,
            attempt_id=attempt_id,
            turn_id=turn_id,
        )
        return kill

    @staticmethod
    def _budget_breach(refreshed_session, budget: dict) -> str | None:
        """Return the budget kind that was exceeded, or None.

        Dollars-first: with cache_creation and cache_read now in the dollar
        total, `total_dollars` is the authoritative spend signal. `max_tokens`
        remains a soft secondary gate — it only counts uncached input +
        output, which understates cache-heavy cost, but is preserved so
        existing token-only callers keep working.
        """
        if not budget:
            return None
        if budget.get("max_dollars") is not None:
            if (refreshed_session.total_dollars or 0) >= budget["max_dollars"]:
                return "max_dollars"
        if budget.get("max_tokens"):
            used = (refreshed_session.total_input_tokens or 0) + (refreshed_session.total_output_tokens or 0)
            if used >= budget["max_tokens"]:
                return "max_tokens"
        return None

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    # When status flips to a terminal value, Anthropic's events endpoint
    # often lags by a few seconds before reflecting the agent's final
    # rounds of activity. Without this delay+re-poll, the transcript
    # ends up missing the last batch of events even though they were
    # billed in the session totals. 5s was empirically enough to catch
    # the gap observed on task 614217bb (final ~5000 output tokens of
    # agent work that never made it to the transcript on the in-flight
    # polls). Worst case the operator waits an extra 5s for the Telegram
    # completion alert — fine.
    _TERMINAL_BACKFILL_DELAY_SECONDS = 5.0

    def _backfill_events_on_terminal(self, session, state) -> None:
        """Re-fetch events one more time after a brief delay, in case
        Anthropic's events endpoint lagged behind the status endpoint.
        Appends any newly-visible events to the transcript (dedupe by
        event id against what's already there)."""
        remote_id = session.managed_agent_session_id
        if not remote_id or not self._is_current(session):
            return
        time.sleep(self._TERMINAL_BACKFILL_DELAY_SECONDS)
        if not self._is_current(session):
            return
        try:
            late_events = self.driver.list_events(remote_id, after_id=None)
        except Exception as exc:
            logger.warning("backfill list_events failed for %s: %s", remote_id, exc)
            return
        if not late_events:
            return
        # Dedupe against what's already in the transcript. Read each line
        # once and pluck `payload.id`.
        seen_ids: set[str] = set()
        for ev in self.transcript_store.iter_events(session.session_id):
            payload = ev.get("payload", {}) or {}
            eid = payload.get("id")
            if eid:
                seen_ids.add(eid)
        added = 0
        for event in late_events:
            eid = event.get("id")
            if not eid or eid in seen_ids:
                continue
            stored = _truncate_oversized_tool_result(event)
            self.transcript_store.append(
                session.session_id,
                f"managed_event_{stored.get('type', 'unknown')}",
                stored,
            )
            added += 1
        if added:
            logger.info("backfilled %d late event(s) for %s after terminal",
                        added, session.task_id)
            # Also refresh final_text from the now-complete event stream so
            # the operator sees the real answer, not an early-batch stub.
            from api.services.agent_worker.managed_driver import _extract_final_text
            refreshed = _extract_final_text(late_events)
            if refreshed and refreshed != state.final_text:
                state.final_text = refreshed
                self.session_store.set_managed_final_text(
                    session.task_id, refreshed,
                    attempt_id=session.attempt_id, turn_id=session.turn_id,
                )

    def _finalize_remote(self, session, state) -> ExecutorOutcome:
        if not self._is_current(session):
            return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
        if self._is_cancelled(session):
            return self._with_identity(
                session,
                ExecutorOutcome(status=STATUS_FAILED, reason="managed turn cancelled"),
            )
        # Session-hour overhead has already been booked incrementally in
        # poll(), so finalize doesn't need to add anything more — just record
        # the terminal status.
        # The Managed Agents API reports a successful terminal as `"idle"`
        # (live-confirmed 2026-05-26). `"completed"` is kept here as a
        # synthesized alias for forward-compat in case the API later transitions
        # status fields between the two.
        if state.status in ("idle", "completed"):
            # Wait for Anthropic's events endpoint to catch up, then
            # backfill anything we missed. The status endpoint goes
            # terminal before /events fully reflects the final rounds.
            self._backfill_events_on_terminal(session, state)
            if not self._is_current(session):
                return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
            if self._is_cancelled(session):
                return self._with_identity(
                    session,
                    ExecutorOutcome(status=STATUS_FAILED, reason="managed turn cancelled"),
                )
            # `state.final_text` reflects only events in *this* poll batch. If
            # the agent.message arrived in a prior batch and only the idle
            # event arrived now, fall back to the cached value persisted by
            # poll().
            final_text = state.final_text or self.session_store.get_managed_final_text(
                session.task_id, attempt_id=session.attempt_id, turn_id=session.turn_id,
            ) or ""
            completed = self.session_store.update_status(
                session.task_id, STATUS_COMPLETED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            if not completed:
                if self._is_cancelled(session):
                    return self._with_identity(
                        session,
                        ExecutorOutcome(status=STATUS_FAILED, reason="managed turn cancelled"),
                    )
                return self._with_identity(session, ExecutorOutcome(status=STATUS_RUNNING))
            # Persist the body alongside its length. The final text is also
            # sent to Telegram, but the transcript is the only durable record
            # an operator can grep later to audit what an agent actually said.
            # For cloud sessions the agent.message event is also in the
            # transcript verbatim, but it's wrapped in a managed_event_… line
            # — keeping the parsed text here is materially easier to grep.
            self.transcript_store.append(
                session.session_id, "managed_completed",
                {"final_chars": len(final_text),
                 "final_text": final_text,
                 "remote_status": state.status,
                 "init_failed_mcps": list(state.init_failed_mcps)},
            )
            return self._with_identity(session, ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=final_text,
                init_failed_mcps=list(state.init_failed_mcps),
            ))

        if state.status == "budget_exceeded":
            self.session_store.update_status(
                session.task_id, STATUS_BUDGET_EXCEEDED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(session.session_id, "managed_budget_exceeded", {})
            return self._with_identity(session, ExecutorOutcome(
                status=STATUS_BUDGET_EXCEEDED,
                reason=state.error_reason or "remote budget exceeded",
            ))

        if state.status == "cancelled":
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            reason = state.error_reason or "session cancelled remotely"
            self.transcript_store.append(session.session_id, "managed_cancelled", {"reason": reason})
            return self._with_identity(
                session, ExecutorOutcome(status=STATUS_FAILED, reason=f"cancelled: {reason}")
            )

        # failed (or unknown terminal)
        self.session_store.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        reason = state.error_reason or f"remote status={state.status}"
        self.transcript_store.append(session.session_id, "managed_failed", {"reason": reason})
        return self._with_identity(session, ExecutorOutcome(status=STATUS_FAILED, reason=reason))
