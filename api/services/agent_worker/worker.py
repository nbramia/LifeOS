"""Main poll loop for the LifeOS agent worker.

Per-tick flow:
  - wake any sessions whose sleep timer has expired
  - check the daily spend cap
  - list todo+#agent tasks from the API
  - for each unclaimed candidate: atomic tag swap, preflight, route
    (local → run on Gemma; claude → defer to Issue D; ask/ambiguous → block)
  - on terminal outcomes, swap to the matching #agent-* status tag and
    notify via Telegram

The worker is a stand-alone process (`python -m api.services.agent_worker.worker`)
managed by the `lifeos-agent-worker.service` systemd unit. It does not import
the FastAPI app — all task ops go through `/api/tasks`. This keeps the worker
trivially restartable and lets the API enforce its own locking.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

import httpx

from api.services.agent_worker.preflight import (
    ROUTE_ASK,
    ROUTE_CLAUDE,
    ROUTE_LOCAL,
    PreflightResult,
    run_preflight,
)
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    Session,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.interaction_store import build_obsidian_link
from config.settings import settings


logger = logging.getLogger(__name__)

# Inline-summary cap. Telegram allows ~4096 chars; we leave headroom for
# the worker's header (icon + title + token/cost line) and any footer
# (init-failed MCPs). Above this we spill the body to a vault note and
# put just a 1-line preview + obsidian:// link in the Telegram message.
_INLINE_SUMMARY_MAX_CHARS = 2000


def _worker_label(routing: str | None) -> str:
    """Telegram-message prefix that names the route — operator wants to
    know at a glance whether a result came from local Gemma or cloud
    Claude. Defaults to the generic "Agent worker" when the routing
    isn't known yet (e.g., startup recovery messages)."""
    if routing == ROUTE_LOCAL:
        return "Local agent worker"
    if routing == ROUTE_CLAUDE:
        return "Cloud agent worker"
    return "Agent worker"


AGENT_TAG = "agent"
RUNNING_TAG = "agent-running"
COMPLETED_TAG = "agent-completed"
BLOCKED_TAG = "agent-blocked"
FAILED_TAG = "agent-failed"
BUDGET_EXCEEDED_TAG = "agent-budget-exceeded"

# Task statuses that the worker will pick up for execution. `todo` is the
# everyday-task default; `urgent` (Obsidian Tasks `[!]`) is for high-priority
# items the operator wants run ahead of the queue — both should trigger the
# agent if tagged `#agent`. The list API only accepts a single status per
# request, so we fan out and dedupe.
AGENT_PICKUP_STATUSES = ("todo", "urgent")


class Worker:
    """Single-process poll loop. One instance per process."""

    def __init__(
        self,
        api_base: str | None = None,
        session_store: SessionStore | None = None,
        transcript_store: TranscriptStore | None = None,
        spend_tracker: SpendTracker | None = None,
        poll_seconds: float | None = None,
        telegram_send=None,  # injectable for tests
        telegram_send_with_id=None,  # injectable; returns sent message_id (or None)
        http_client: httpx.Client | None = None,
        preflight_caller=None,    # injectable; defaults to Anthropic Haiku
        local_executor=None,      # injectable LocalExecutor for tests
        managed_executor=None,    # injectable ManagedExecutor for tests
    ) -> None:
        self.api_base = (api_base or os.environ.get("LIFEOS_API_URL", "http://localhost:8000")).rstrip("/")
        self.session_store = session_store or SessionStore()
        self.transcript_store = transcript_store or TranscriptStore()
        self.spend_tracker = spend_tracker or SpendTracker(
            daily_cap_dollars=settings.agent_daily_cap_dollars,
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.agent_worker_poll_seconds
        self._stop = False
        # Telegram senders default to no-ops. Tests get isolation for free
        # (no risk of a test ever hitting the operator's real chat), and
        # production wiring is explicit in `main()`. The previous default
        # — auto-importing the real Telegram module — leaked a test stub
        # message to a real operator chat once; never again.
        def _noop_telegram(text, chat_id=None):
            return False
        def _noop_with_id(text):
            return None
        self._telegram_send = telegram_send if telegram_send is not None else _noop_telegram
        self._telegram_send_with_id = (
            telegram_send_with_id if telegram_send_with_id is not None else _noop_with_id
        )
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=10.0)
        self._preflight_caller = preflight_caller  # None → use Anthropic SDK by default
        self._local_executor = local_executor  # lazily instantiated on first use
        self._managed_executor = managed_executor  # lazily instantiated on first claude task
        self._warn_deprecated_settings()

    @staticmethod
    def _warn_deprecated_settings() -> None:
        """Log a single warning if deprecated env vars are still set.

        `LIFEOS_AGENT_CONNECTORS` and `LIFEOS_AGENT_EXTRA_MCP_SERVERS` were
        used by the pre-refactor driver to build per-session MCP / connector
        lists. The current driver expects those to live on the agent preset
        (configured in the Anthropic console) and ignores both fields. Operators
        with stale .env files would otherwise silently lose configuration, so
        surface a clear deprecation message at startup.
        """
        deprecated = []
        if getattr(settings, "agent_connectors", "") or "":
            deprecated.append("LIFEOS_AGENT_CONNECTORS")
        if getattr(settings, "agent_extra_mcp_servers", "") or "":
            deprecated.append("LIFEOS_AGENT_EXTRA_MCP_SERVERS")
        if deprecated:
            logger.warning(
                "Deprecated env var(s) set and ignored: %s. MCP servers and "
                "connectors now live on the Managed Agents preset "
                "(LIFEOS_AGENT_PRESET_ID), not in session creation. Remove "
                "these from .env to silence this warning. See "
                "docs/guides/agent-worker-setup.md.",
                ", ".join(deprecated),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Mark the loop for graceful shutdown. Safe to call from a signal."""
        self._stop = True

    def run(self) -> None:
        """Main loop. Runs until `stop()` is called or the process is killed."""
        logger.info(
            "agent worker starting (api=%s, poll=%ss, daily_cap=$%s)",
            self.api_base, self.poll_seconds, self.spend_tracker.daily_cap_dollars,
        )
        # Recover any sessions left non-terminal by a previous crash before
        # starting fresh poll cycles (issue #100 acceptance: restart-resumable).
        self.resume_pending()
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover — loop guard
                logger.exception("worker tick failed: %s", exc)
            # Sleep in small slices so SIGTERM is honored promptly.
            slept = 0.0
            while slept < self.poll_seconds and not self._stop:
                step = min(1.0, self.poll_seconds - slept)
                time.sleep(step)
                slept += step
        logger.info("agent worker stopped")
        if self._owns_http_client:
            self._http.close()

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def resume_pending(self) -> int:
        """Finalize sessions left non-terminal by a previous crash.

        - STATUS_YIELDED with a `sleeps` row: leave alone — the wake-up loop
          in `tick()` will pick it up at the right time.
        - STATUS_RUNNING / STATUS_CLAIMED / STATUS_BLOCKED: mark FAILED and
          roll the tag back to #agent so the operator can retry. We can't
          safely re-enter a partially-driven LLM conversation without risking
          duplicate side effects (file writes, API calls, etc.).
        """
        pending = self.session_store.list_non_terminal()
        if not pending:
            return 0
        recovered = 0
        for session in pending:
            sid = session.session_id
            # Sleeping sessions are healthy — main loop will wake them.
            if session.status == STATUS_YIELDED:
                continue
            # Blocked sessions are waiting on the user; leave alone.
            if session.status == STATUS_BLOCKED:
                continue
            self.transcript_store.append(sid, "resume_failed", {"prior_status": session.status})
            self._swap_tag(session.task_id, RUNNING_TAG, AGENT_TAG)
            # Rolling back to #agent — return the vault checkbox to "todo"
            # so the operator sees the task as un-started rather than stuck
            # in "in_progress".
            self._set_task_status(session.task_id, "todo")
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            self._notify(
                f"⚠️ {_worker_label(session.routing)}: task left in {session.status!r} from a prior "
                f"run could not be safely resumed — tag rolled back to "
                f"#{AGENT_TAG} for retry. Transcript: "
                f"`data/agent_transcripts/{sid}.jsonl`"
            )
            recovered += 1
        if recovered:
            logger.info("rolled back %d non-resumable session(s) on startup", recovered)
        return recovered

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def tick(self) -> int:
        """Process one poll cycle. Returns the number of tasks handled (for tests)."""
        # Use the configured per-task default budget as the "can I afford to
        # start the cheapest task right now?" estimate. Calling with 0.0 would
        # let claims through even at cap=0 — see SpendTracker.can_start_task
        # for the pause semantics.
        estimate = settings.agent_default_budget_dollars
        if not self.spend_tracker.can_start_task(estimate):
            logger.info(
                "daily spend cap reached or paused (cap=$%s, today=$%.2f); skipping poll",
                self.spend_tracker.daily_cap_dollars, self.spend_tracker.today_total(),
            )
            return 0

        # First, resume any sleeping sessions whose wake time has arrived.
        self._wake_sleeping_sessions()
        # Then advance any in-flight Managed Agents sessions one polling step.
        self._poll_managed_sessions()
        # Resume yielded sessions whose children have all reached terminal state.
        self._resume_yielded_for_children()
        # Dispatch any newly-spawned sessions (no #agent task — created via
        # lifeos_agent_spawn). They show up with status=claimed and a routing.
        self._dispatch_spawned_sessions()
        # Resume blocked sessions whose Telegram clarifications have arrived.
        self._process_clarification_answers()
        # Timeout long-unanswered clarifications.
        self._timeout_stale_clarifications()

        candidates = self._list_agent_tasks()
        handled = 0
        for task in candidates:
            task_id = task.get("id")
            if not task_id:
                continue
            # Skip tasks we've already claimed in a previous run.
            if self.session_store.get(task_id) is not None:
                continue
            if not self._claim(task_id):
                continue
            self._dispatch(task)
            handled += 1
        return handled

    def _resume_yielded_for_children(self) -> None:
        """Resume yielded sessions whose listed children have all terminated.

        Local sessions get the children's outputs injected as a new user turn
        and re-enter the executor loop. Managed yield-and-resume is not yet
        supported (session was killed remotely; re-creation with full history
        transfer is a follow-up PR) — for now those land in FAILED with a
        clear reason so the operator can re-tag.
        """
        from api.services.agent_worker.session_store import TERMINAL_STATUSES as _TS
        yielded = self.session_store.list_yielded_waiting_on_children()
        for session in yielded:
            children = session.yield_waiting_for or []
            if not children:
                continue
            child_sessions = self.session_store.list_by_session_ids(children)
            all_done = (
                len(child_sessions) == len(children)
                and all(c.status in _TS for c in child_sessions)
            )
            if not all_done:
                continue

            task = self._fetch_task(session.task_id) or {
                "id": session.task_id, "description": session.task_id,
            }

            # Managed yield-resume isn't supported yet — short-circuit with a
            # clear failure rather than scrambling the local message history
            # for nothing.
            if session.routing != "local":
                self.transcript_store.append(
                    session.session_id, "managed_yield_resume_unsupported", {},
                )
                self._mark_failed(
                    session, task,
                    "managed yield-and-resume not yet supported (children "
                    "finished but parent cannot resume)",
                )
                continue

            # Build the resume turn, then commit state updates only after we
            # know the executor returned cleanly. This keeps the session
            # restartable if the executor crashes mid-resume.
            summary_lines = ["Children completed:"]
            for c in child_sessions:
                tokens = (c.total_input_tokens or 0) + (c.total_output_tokens or 0)
                summary_lines.append(
                    f"- {c.session_id} [{c.status}] — {tokens} tokens, ${c.total_dollars:.2f}"
                )
            resume_message = "\n".join(summary_lines)

            executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                # Append the resume message first so the executor sees it on
                # the next turn; clear yield_waiting_for *after* the executor
                # returns so a crash leaves the session retryable (still
                # yielded with the same children list).
                self.session_store.append_message(session.session_id, "user", resume_message)
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("resume after children crashed for %s: %s",
                                 session.task_id, exc)
                # Leave yield_waiting_for set so the next tick re-attempts.
                self._mark_failed(session, task, f"resume crashed: {exc}")
                continue
            self.session_store.set_yield_waiting_for(session.task_id, None)
            self.transcript_store.append(session.session_id, "resume_after_children", {
                "children": children,
            })
            self._handle_outcome(session, task, outcome)

    def _dispatch_spawned_sessions(self) -> None:
        """Pick up sessions created via lifeos_agent_spawn that have no #agent
        task backing — they show up with status=claimed and an explicit routing.

        Synchronous in this MVP: a long-running local child blocks the tick
        loop. Acceptable while local concurrency cap = 1; a future PR could
        move to a worker pool.
        """
        claimed = self.session_store.list_by_status(STATUS_CLAIMED)
        for session in claimed:
            # Skip top-level claimed sessions (those came from the tick claim
            # path and will be dispatched by _dispatch). Spawned children have
            # a parent_session_id set.
            if not session.parent_session_id:
                continue
            # The first pending message is the prompt from the parent — drain
            # it so the executor's seeded user turn picks it up.
            pending = self.session_store.drain_pending_messages(session.session_id)
            description = pending[0]["content"] if pending else session.session_id
            task = {"id": session.task_id, "description": description}
            if session.routing == "local":
                executor = self._get_local_executor(caller_session_id=session.session_id)
                try:
                    outcome = executor.execute(session, task)
                except Exception as exc:
                    logger.exception("spawned local execute crashed for %s: %s", session.task_id, exc)
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    continue
                self._handle_outcome(session, task, outcome)
            elif session.routing == "claude":
                managed = self._get_managed_executor()
                if managed is None:
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    self.transcript_store.append(
                        session.session_id, "spawn_failed_no_managed", {},
                    )
                    continue
                try:
                    outcome = managed.start(session, task)
                except Exception as exc:
                    logger.exception("spawned managed start crashed for %s: %s", session.task_id, exc)
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    continue
                if outcome.status == STATUS_FAILED:
                    self._handle_outcome(session, task, outcome)
                # On RUNNING, let _poll_managed_sessions handle the rest.

    def _poll_managed_sessions(self) -> None:
        """Advance all in-flight Managed Agents sessions one polling step."""
        active = self.session_store.list_active_managed()
        if not active:
            return
        managed = self._get_managed_executor()
        if managed is None:
            return  # operator removed credentials between starts; sessions are stuck
        for session in active:
            task = self._fetch_task(session.task_id) or {
                "id": session.task_id,
                "description": session.task_id,
            }
            pre_dollars = session.total_dollars or 0.0
            try:
                outcome = managed.poll(session)
            except Exception as exc:
                logger.exception("managed.poll crashed for %s: %s", session.task_id, exc)
                self._mark_failed(session, task, f"managed poll crashed: {exc}")
                continue
            # Push the per-poll dollar delta into the daily ledger so the
            # global cap reflects in-flight managed cost, not just claim-time
            # estimates.
            refreshed = self.session_store.get(session.task_id)
            delta_dollars = max(0.0, (refreshed.total_dollars or 0.0) - pre_dollars) if refreshed else 0.0
            if delta_dollars > 0:
                self.spend_tracker.record(delta_dollars)
            if outcome.status != STATUS_RUNNING:
                self._handle_outcome(session, task, outcome)

    def _process_clarification_answers(self) -> None:
        """Resume sessions whose Telegram replies arrived.

        The Telegram listener (api/services/telegram.py) deposits user
        replies into `pending_questions.answer`. Each tick we drain any
        answered+unprocessed rows. Two kinds:

        * kind="clarification" — agent asked a question mid-task, session
          is BLOCKED. Inject the answer as a user turn, swap the task tag
          back to `#agent-running`, and re-invoke the executor.

        * kind="followup" — task already completed, operator replied on
          the completion message to continue the thread (e.g., "now turn
          this into a .md"). Reopen the COMPLETED session: append the
          reply as a new user turn, swap `#agent-completed` →
          `#agent-running`, and re-run the executor. The agent retains
          full prior context because the conversation history is
          preserved.

        Special case for routing-ask: when the session's routing is "ask",
        the answer tells us which model to use. We parse "local"/"claude"
        out of the answer text and update session.routing accordingly
        before dispatching.
        """
        answered = self.session_store.list_answered_unprocessed_questions()
        for q in answered:
            session_id = q["session_id"]
            task_id = q["task_id"]
            session = self.session_store.get_by_session_id(session_id)
            if session is None:
                # Stale — session was deleted? Mark processed so we don't loop.
                self.session_store.mark_question_processed(q["id"])
                continue

            answer = q["answer"] or ""
            kind = q.get("kind") or "clarification"

            if kind == "followup":
                self._resume_as_followup(q, session, answer)
                continue

            # Routing-ask resolution: if session.routing == "ask", the answer
            # should contain "local" or "claude". Update the session's routing
            # before dispatching so the right executor handles the resume.
            if session.routing == "ask":
                resolved = self._parse_routing_answer(answer)
                if resolved is None:
                    # Couldn't parse — re-ask. Mark processed but send a new
                    # clarification asking specifically for the model name.
                    self.session_store.mark_question_processed(q["id"])
                    self.transcript_store.append(session_id, "routing_ask_unparseable", {
                        "answer_chars": len(answer),
                    })
                    self.ask_user_via_telegram(
                        session_id, task_id,
                        "I couldn't tell which model you wanted. "
                        "Please reply 'local' or 'claude'.",
                    )
                    continue
                self.session_store.set_routing_and_budget(
                    task_id, routing=resolved,
                    budget=session.budget, expected_output=session.expected_output,
                )
                self.transcript_store.append(session_id, "routing_resolved", {
                    "from": "ask", "to": resolved,
                })
                # Refresh the session view so downstream code sees the new routing.
                session = self.session_store.get_by_session_id(session_id)

            # Inject the answer as a user message.
            self.session_store.append_message(
                session_id, "user",
                f"(user answered via Telegram) {answer}",
            )
            self.transcript_store.append(session_id, "clarification_answered", {
                "question_id": q["id"],
                "answer_chars": len(answer),
            })
            self._swap_tag(task_id, BLOCKED_TAG, RUNNING_TAG)
            self._set_task_status(task_id, "in_progress")
            self.session_store.update_status(task_id, STATUS_RUNNING)

            task = self._fetch_task(task_id) or {"id": task_id, "description": task_id}
            if session.routing == "local":
                executor = self._get_local_executor(caller_session_id=session_id)
                try:
                    outcome = executor.execute(session, task)
                except Exception as exc:
                    logger.exception(
                        "clarification resume crashed for %s: %s", task_id, exc,
                    )
                    # Leave question unprocessed so retry can be attempted.
                    self._mark_failed(session, task, f"clarification resume crashed: {exc}")
                    continue
                # Only mark processed once the executor returns cleanly — if
                # the worker crashes mid-execute, the question stays open and
                # the next tick re-attempts the resume.
                self.session_store.mark_question_processed(q["id"])
                self._handle_outcome(session, task, outcome)
            elif session.routing == "claude":
                managed = self._get_managed_executor()
                if managed is None:
                    self.session_store.mark_question_processed(q["id"])
                    self._mark_failed(
                        session, task,
                        "claude route resolved but Managed Agents isn't configured",
                    )
                    continue
                try:
                    outcome = managed.start(session, task)
                except Exception as exc:
                    logger.exception(
                        "clarification resume claude.start crashed for %s: %s", task_id, exc,
                    )
                    self._mark_failed(session, task, f"clarification resume crashed: {exc}")
                    continue
                self.session_store.mark_question_processed(q["id"])
                if outcome.status == STATUS_FAILED:
                    self._handle_outcome(session, task, outcome)
                # otherwise: managed session is now running, _poll_managed_sessions takes over
            else:
                # Other managed-side clarification (mid-loop lifeos_agent_user_ask):
                # not yet supported because the original session was killed.
                self.session_store.mark_question_processed(q["id"])
                self.transcript_store.append(
                    session_id, "managed_clarification_resume_unsupported", {},
                )
                self._mark_failed(
                    session, task,
                    "managed clarification resume not yet supported",
                )

    def _resume_as_followup(self, q: dict, session: Session, answer: str) -> None:
        """Operator replied to a completion message — reopen the COMPLETED
        session as a follow-up turn. The conversation history is preserved
        so the agent retains full context ("turn this into a .md" works
        because "this" is still in the assistant's prior turn).
        """
        sid = session.session_id
        task_id = session.task_id

        self._swap_tag(task_id, COMPLETED_TAG, RUNNING_TAG)
        self._set_task_status(task_id, "in_progress")
        self.session_store.update_status(task_id, STATUS_RUNNING)
        self.transcript_store.append(sid, "followup_received", {
            "question_id": q["id"], "answer_chars": len(answer),
        })

        task = self._fetch_task(task_id) or {"id": task_id, "description": task_id}

        if session.routing == ROUTE_LOCAL:
            self.session_store.append_message(
                sid, "user", f"(user replied on Telegram) {answer}",
            )
            executor = self._get_local_executor(caller_session_id=sid)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("followup local resume crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"followup resume crashed: {exc}")
                self.session_store.mark_question_processed(q["id"])
                return
            self.session_store.mark_question_processed(q["id"])
            self._handle_outcome(session, task, outcome)
            return

        if session.routing == ROUTE_CLAUDE:
            managed = self._get_managed_executor()
            if managed is None or managed.driver is None:
                self._mark_failed(
                    session, task,
                    "followup arrived but Managed Agents isn't configured",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            # Post the operator's reply as a new user turn on the existing
            # managed session. If Anthropic has already GC'd that session
            # (or it was killed), `post_user_message` 404s; we tell the
            # operator their thread can't be resumed cleanly.
            remote_id = session.managed_agent_session_id
            if not remote_id:
                self._mark_failed(
                    session, task,
                    "followup arrived but no managed session id on record",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            try:
                managed.driver.post_user_message(remote_id, answer)
            except Exception as exc:
                # The remote session may have been cleaned up — surface a
                # clear message so the operator can re-create the task if
                # they want a fresh thread.
                logger.warning(
                    "followup post_user_message failed for %s: %s",
                    remote_id, exc,
                )
                self._mark_failed(
                    session, task,
                    f"followup couldn't be delivered to the existing managed "
                    f"session (it may have been cleaned up): {type(exc).__name__}",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            # The next _poll_managed_sessions tick picks it up and we'll
            # send a fresh completion notification when it finishes.
            self.session_store.mark_question_processed(q["id"])
            return

        # Unknown routing — shouldn't happen post-preflight.
        self._mark_failed(session, task, f"followup with unknown routing: {session.routing}")
        self.session_store.mark_question_processed(q["id"])

    @staticmethod
    def _parse_routing_answer(answer: str) -> str | None:
        """Best-effort parse of a "local" / "claude" answer from a free-text
        Telegram reply. Returns "local", "claude", or None when ambiguous.

        Handles combined ambiguity+routing replies like "1. John Doe 2. local"
        by scanning for the model keyword anywhere in the text. If both
        keywords appear, returns the one occurring later (operator's most
        recent statement wins).
        """
        if not answer:
            return None
        lowered = answer.lower()
        local_idx = max(lowered.rfind("local"), lowered.rfind("gemma"))
        claude_idx = max(lowered.rfind("claude"), lowered.rfind("opus"))
        if local_idx < 0 and claude_idx < 0:
            return None
        return "local" if local_idx > claude_idx else "claude"

    def _timeout_stale_clarifications(self) -> None:
        """Send a one-time nudge for clarifications older than the configured
        timeout. The task stays at #agent-blocked permanently after that — the
        operator can manually re-tag with #agent to retry.
        """
        timeout_seconds = settings.agent_clarification_timeout_hours * 3600
        cutoff = int(time.time()) - timeout_seconds
        stale = self.session_store.list_timed_out_questions(cutoff)
        for q in stale:
            self.session_store.mark_question_timed_out(q["id"])
            self.transcript_store.append(q["session_id"], "clarification_timed_out", {
                "question_id": q["id"],
            })
            stale_session = self.session_store.get_by_session_id(q["session_id"])
            label = _worker_label(stale_session.routing if stale_session else None)
            self._notify(
                f"⏰ {label}: task is still waiting on your reply.\n\n"
                f"Question: {q['question'][:300]}\n\n"
                f"(Task remains at #{BLOCKED_TAG}. Reply to the original "
                f"question to unblock, or re-tag with #{AGENT_TAG} to retry.)"
            )

    def ask_user_via_telegram(
        self,
        session_id: str,
        task_id: str,
        question: str,
    ) -> int | None:
        """Send a clarification question via Telegram and record the
        sent_message_id so the reply-thread hook can match it back.

        Returns the Telegram message_id (or None if Telegram isn't configured
        — caller should mark the session blocked anyway since we can't ask).
        """
        try:
            sent_id = self._telegram_send_with_id(question)
        except Exception as exc:
            logger.warning(f"ask_user_via_telegram failed: {exc}")
            return None
        if sent_id is None:
            return None
        self.session_store.create_pending_question(
            session_id=session_id,
            task_id=task_id,
            question=question,
            sent_message_id=sent_id,
        )
        self.transcript_store.append(session_id, "clarification_sent", {
            "sent_message_id": sent_id,
            "question_chars": len(question),
        })
        return sent_id

    def _wake_sleeping_sessions(self) -> None:
        """Resume any sessions whose `sleeps` row has expired."""

        due = self.session_store.due_sleeps()
        if not due:
            return
        for session_id in due:
            session = self.session_store.get_by_session_id(session_id)
            if session is None:
                # Defensive — stale sleep row referencing a deleted session.
                self.session_store.remove_sleep(session_id)
                continue
            self.session_store.remove_sleep(session_id)
            self.transcript_store.append(session_id, "wake", {})
            # Re-fetch the task description from the API so the conversation
            # context stays accurate (someone may have edited the title).
            task = self._fetch_task(session.task_id) or {"id": session.task_id, "description": ""}
            executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("wake execute failed for %s: %s", session_id, exc)
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
                self._notify(
                    f"⚠️ {_worker_label(session.routing)}: error while resuming sleeping task "
                    f"'{task.get('description', session.task_id)}': {exc}"
                )
                continue
            self._handle_outcome(session, task, outcome)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _list_agent_tasks(self) -> list[dict[str, Any]]:
        """Fetch open `#agent` tasks from the API.

        The list endpoint only filters on a single status string, so we fan
        out across `AGENT_PICKUP_STATUSES` (`todo` + `urgent` by default) and
        dedupe by task id. A task that appears under both statuses in quick
        succession is still claimed once.
        """
        seen: set[str] = set()
        all_tasks: list[dict[str, Any]] = []
        for status in AGENT_PICKUP_STATUSES:
            try:
                resp = self._http.get(
                    f"{self.api_base}/api/tasks",
                    params={"status": status, "tag": AGENT_TAG},
                )
                resp.raise_for_status()
                for task in resp.json().get("tasks", []):
                    tid = task.get("id")
                    if tid and tid not in seen:
                        seen.add(tid)
                        all_tasks.append(task)
            except Exception as exc:
                logger.warning("failed to list agent tasks (status=%s): %s", status, exc)
        return all_tasks

    def _swap_tag(self, task_id: str, from_tag: str, to_tag: str) -> bool:
        try:
            resp = self._http.post(
                f"{self.api_base}/api/tasks/{task_id}/swap-tag",
                params={"from": from_tag, "to": to_tag},
            )
            resp.raise_for_status()
            return bool(resp.json().get("swapped"))
        except Exception as exc:
            logger.warning("swap_tag failed for %s: %s", task_id, exc)
            return False

    def _complete_task(self, task_id: str) -> bool:
        try:
            resp = self._http.put(f"{self.api_base}/api/tasks/{task_id}/complete")
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("complete failed for %s: %s", task_id, exc)
            return False

    def _set_task_status(self, task_id: str, status: str) -> bool:
        """Update the vault checkbox status alongside an `#agent-*` tag swap.

        The operator wants Obsidian's status (the `- [ ]` / `- [/]` / `- [?]`
        checkbox symbol) to track execution state, not just the tag. This
        is fire-and-forget — failure is logged but does not block the
        transition; the tag itself remains the worker's source of truth.
        """
        try:
            resp = self._http.put(
                f"{self.api_base}/api/tasks/{task_id}",
                json={"status": status},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("set_task_status(%s, %s) failed: %s", task_id, status, exc)
            return False

    # ------------------------------------------------------------------
    # Claim + dispatch
    # ------------------------------------------------------------------

    def _claim(self, task_id: str) -> bool:
        """Atomically swap `#agent` → `#agent-running` and record the session.

        Returns True iff this worker won the race.
        """
        if not self._swap_tag(task_id, AGENT_TAG, RUNNING_TAG):
            return False
        # Sync vault status to in_progress so the operator can see at a
        # glance which tasks are actively being worked on, not just by
        # tag color.
        self._set_task_status(task_id, "in_progress")
        try:
            session = self.session_store.create(
                task_id=task_id,
                status=STATUS_CLAIMED,
            )
            self.transcript_store.append(
                session.session_id,
                "claim",
                {"task_id": task_id, "worker": "agent-worker"},
            )
            return True
        except Exception as exc:
            # Already-claimed by a sibling worker, or DB hiccup. Try to un-do
            # the tag swap so the task remains pickable.
            logger.error("session create failed for %s: %s", task_id, exc)
            if not self._swap_tag(task_id, RUNNING_TAG, AGENT_TAG):
                # Rollback failed — task is stuck at #agent-running. Notify so
                # the operator can intervene before this silently strands work.
                self._notify(
                    f"⚠️ Agent worker: failed to claim task {task_id} and could "
                    f"not roll back tag. Task stuck at #{RUNNING_TAG} — please "
                    f"re-tag manually if you want it retried."
                )
            return False

    def _get_local_executor(self, caller_session_id: str | None = None):
        """Return the local executor configured for `caller_session_id`.

        The inter-agent tool context depends on the caller's identity, so we
        rebuild the tool registry when the caller changes. For tests an
        injected executor wins.
        """
        if self._local_executor is not None:
            return self._local_executor
        from api.services.agent_worker.inter_agent import Caps, InterAgentContext
        from api.services.agent_worker.local_executor import LocalExecutor
        from api.services.agent_worker.tools import ToolRegistry
        ctx = None
        if caller_session_id:
            ctx = InterAgentContext(
                session_store=self.session_store,
                transcript_store=self.transcript_store,
                caller_session_id=caller_session_id,
                caps=Caps(
                    max_spawn_depth=settings.agent_max_spawn_depth,
                    max_descendants_per_root=settings.agent_max_descendants_per_root,
                    max_concurrent_local=settings.agent_max_concurrent_local,
                    max_concurrent_managed=settings.agent_max_concurrent_managed,
                ),
                worker_handle=self,
            )
        registry = ToolRegistry(inter_agent_context=ctx)
        return LocalExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            tool_registry=registry,
        )

    def _get_managed_executor(self):
        """Return the cached ManagedExecutor, lazily constructed.

        Returns None if Managed Agents isn't configured — the dispatcher then
        parks the task at #agent-blocked with an operator-facing explanation.

        Required settings (all must be set):
          - `anthropic_api_key` — for the control-plane auth header
          - `agent_preset_id` — `agent_…` ID created in the Anthropic console.
            Holds the model, system prompt, MCP servers, and tools.
          - `agent_environment_id` — `env_…` ID for where tool calls execute
            (cloud container by default; self-hosted sandbox in #111).
        Optional:
          - `agent_vault_id` — `vlt_…` ID supplying OAuth credentials for
            MCP servers declared in the agent preset. Without it, OAuth-
            protected MCPs (Gmail, Slack, etc.) reject the agent's calls.
        """
        if self._managed_executor is not None:
            return self._managed_executor
        api_key = settings.anthropic_api_key
        agent_id = settings.agent_preset_id
        environment_id = settings.agent_environment_id
        if not api_key or not agent_id or not environment_id:
            return None
        from api.services.agent_worker.managed_driver import ManagedAgentsDriver
        from api.services.agent_worker.managed_executor import ManagedExecutor
        driver = ManagedAgentsDriver(api_key=api_key)
        vault_ids = [settings.agent_vault_id] if settings.agent_vault_id else []
        self._managed_executor = ManagedExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            driver=driver,
            agent_id=agent_id,
            environment_id=environment_id,
            vault_ids=vault_ids,
            model=settings.agent_managed_model,
        )
        return self._managed_executor

    def _fetch_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            resp = self._http.get(f"{self.api_base}/api/tasks/{task_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("fetch_task %s failed: %s", task_id, exc)
            return None

    def _dispatch(self, task: dict[str, Any]) -> None:
        """Run preflight + route the task to the appropriate executor."""
        task_id = task["id"]
        title = task.get("description", task_id)
        session = self.session_store.get(task_id)
        if session is None:  # pragma: no cover — _claim just inserted it
            logger.error("no session for claimed task %s — skipping", task_id)
            return
        sid = session.session_id

        # Preflight: budget, routing, ambiguity, sanity.
        try:
            pre: PreflightResult = run_preflight(
                title=title,
                tags=task.get("tags", []),
                caller=self._preflight_caller,
            )
        except Exception as exc:
            logger.exception("preflight crashed for %s: %s", task_id, exc)
            self._mark_failed(session, task, f"preflight crashed: {exc}")
            return

        self.transcript_store.append(sid, "preflight", {
            "routing": pre.routing,
            "routing_reason": pre.routing_reason,
            "expected_output": pre.expected_output,
            "ambiguity": pre.ambiguity.question if pre.ambiguity else None,
            "sane": pre.sane,
            "sane_reason": pre.sane_reason,
            "budget": {
                "wall_seconds": pre.budget.wall_seconds,
                "max_tokens": pre.budget.max_tokens,
                "max_dollars": pre.budget.max_dollars,
            },
        })

        # Persist routing + budget onto the session row for the executor to see.
        budget_json = {
            "wall_seconds": pre.budget.wall_seconds,
            "max_tokens": pre.budget.max_tokens,
            "max_dollars": pre.budget.max_dollars,
        }
        self.session_store.set_routing_and_budget(
            task_id,
            routing=pre.routing,
            budget=budget_json,
            expected_output=pre.expected_output,
        )
        session = self.session_store.get(task_id)  # refresh

        # Sanity gate.
        if not pre.sane:
            self._mark_failed(
                session, task,
                f"preflight flagged task as unsafe to run: {pre.sane_reason}",
            )
            return

        # Ambiguity / ask-routing → block on user input (Issue F closes this loop).
        question_parts: list[str] = []
        if pre.ambiguity:
            question_parts.append(pre.ambiguity.question)
        if pre.routing == ROUTE_ASK:
            question_parts.append(
                "Should I run this on the local Gemma model or on Claude Opus? "
                "Reply 'local' or 'claude'."
            )
        if question_parts:
            self._mark_blocked(session, task, " ".join(question_parts))
            return

        # Local route: run the executor.
        if pre.routing == ROUTE_LOCAL:
            executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("local executor crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"executor crashed: {exc}")
                return
            self._handle_outcome(session, task, outcome)
            return

        # Claude route: hand off to Managed Agents.
        if pre.routing == ROUTE_CLAUDE:
            managed = self._get_managed_executor()
            if managed is None:
                # Operator hasn't configured Managed Agents (no API key or vault).
                self._swap_tag(task_id, RUNNING_TAG, BLOCKED_TAG)
                self._set_task_status(task_id, "blocked")
                self.session_store.update_status(task_id, STATUS_BLOCKED)
                self.transcript_store.append(sid, "managed_not_configured", {})
                self._notify(
                    f"⏸ {_worker_label(ROUTE_CLAUDE)}: task '{title}' routed to Claude but "
                    f"Managed Agents isn't configured. Set ANTHROPIC_API_KEY, "
                    f"LIFEOS_AGENT_PRESET_ID, and LIFEOS_AGENT_ENVIRONMENT_ID "
                    f"in .env (see docs/guides/agent-worker-setup.md for the "
                    f"console flow), then retag with #{AGENT_TAG}."
                )
                return
            try:
                outcome = managed.start(session, task)
            except Exception as exc:
                logger.exception("managed.start crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"managed start crashed: {exc}")
                return
            # Don't finalize on START — the session is running remotely.
            # `_poll_managed_sessions` in the next tick will pick it up.
            if outcome.status == STATUS_FAILED:
                self._handle_outcome(session, task, outcome)
            return

        # Should not reach here — routing was validated in preflight.
        self._mark_failed(session, task, f"unknown routing: {pre.routing}")

    # ------------------------------------------------------------------
    # Outcome handling (shared between fresh dispatch and sleep wake-up)
    # ------------------------------------------------------------------

    def _handle_outcome(self, session: Session, task: dict[str, Any], outcome) -> None:
        title = task.get("description", session.task_id)
        sid = session.session_id

        if outcome.status == STATUS_COMPLETED:
            self._complete_task(session.task_id)  # mark `done` in the vault
            # Swap the tag so the task surfaces as #agent-completed for symmetry
            # with the failed / budget-exceeded / blocked terminal tags. Failure
            # of the swap is non-critical — _swap_tag logs and the task is
            # already marked done in the vault.
            self._swap_tag(session.task_id, RUNNING_TAG, COMPLETED_TAG)
            # Send via the with-id sender so we can match a future Telegram
            # reply to this completion message and resume the task as a
            # follow-up turn (e.g., "now turn this into a .md in my vault").
            # When the with-id sender returns None (bot not configured, or
            # a test stub that doesn't capture ids) fall back to the plain
            # _notify path so the operator at least sees the result.
            body = self._completion_summary(session, task, outcome)
            sent_id = self._telegram_send_with_id(body)
            if sent_id is None:
                self._notify(body)
            else:
                try:
                    self.session_store.register_completion_followup(
                        session_id=sid,
                        task_id=session.task_id,
                        sent_message_id=sent_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "register_completion_followup failed for %s: %s",
                        session.task_id, exc,
                    )
            return

        label = _worker_label(session.routing)
        if outcome.status == STATUS_BUDGET_EXCEEDED:
            self._swap_tag(session.task_id, RUNNING_TAG, BUDGET_EXCEEDED_TAG)
            self._set_task_status(session.task_id, "cancelled")
            self._notify(
                f"⚠️ {label}: task '{title}' hit its budget ({outcome.reason}). "
                f"Transcript: `data/agent_transcripts/{sid}.jsonl`"
            )
            return

        if outcome.status == STATUS_FAILED:
            self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
            self._set_task_status(session.task_id, "cancelled")
            self._notify(
                f"⚠️ {label}: task '{title}' failed: {outcome.reason}. "
                f"Transcript: `data/agent_transcripts/{sid}.jsonl`"
            )
            return

        if outcome.status == STATUS_YIELDED:
            # Session is sleeping. The transcript already records "sleep".
            # No Telegram on yield — operator only hears about terminal states.
            return

        logger.warning("unhandled outcome status %r for %s", outcome.status, session.task_id)

    def _completion_summary(self, session: Session, task: dict[str, Any], outcome) -> str:
        refreshed = self.session_store.get(session.task_id) or session
        tokens = (refreshed.total_input_tokens or 0) + (refreshed.total_output_tokens or 0)
        # Use active seconds (excludes sleeps) so the figure reflects real
        # work, not wall time since the session was first created.
        active_s = int(refreshed.total_active_seconds or 0)
        expected = refreshed.expected_output or "text"
        title = task.get("description", session.task_id)
        label = _worker_label(refreshed.routing or session.routing)

        # Body: prefer the agent's final text. When it's empty (the agent
        # used a tool and idled without summarizing — sometimes happens with
        # Sonnet on tight-budget tasks), surface a transcript pointer so the
        # operator can inspect what the agent actually did instead of seeing
        # a blank message. The transcript captures every tool call.
        final_text = (outcome.final_text or "").strip()
        if not final_text:
            # The agent did work but never produced a final assistant
            # message. Common with Sonnet on tasks that end with a tool
            # call (e.g., drafting an email via lifeos_gmail_draft and
            # not bothering to summarize after). Surface whatever the
            # last meaningful tool produced so the operator sees the
            # actual result (the draft, the calendar event, etc.) rather
            # than just a transcript pointer.
            recovered = self._recover_result_from_transcript(session.session_id)
            if recovered:
                result_blurb = (
                    f"(no final text from the agent — surfacing the last "
                    f"tool result instead)\n\n{recovered}"
                )
            else:
                result_blurb = (
                    f"(agent idled without a final text reply — check transcript at "
                    f"`data/agent_transcripts/{session.session_id}.jsonl` for tool-use detail)"
                )
        elif len(final_text) <= _INLINE_SUMMARY_MAX_CHARS:
            result_blurb = final_text
        else:
            # Spill the full body to a vault note and link to it instead of
            # truncating mid-answer. The agent's system prompts now ask the
            # agent itself to create artifacts for long outputs, but the
            # operator-facing UX shouldn't depend on the agent following
            # that guidance: any over-length response gets spilled here.
            spillover = self._spill_to_vault(session, task, final_text)
            if spillover is None:
                # Vault not configured or write failed — preserve the old
                # behavior so the operator still gets *something* readable.
                result_blurb = final_text[:_INLINE_SUMMARY_MAX_CHARS] + "…"
            else:
                rel_path, obsidian_url = spillover
                preview = final_text.split("\n\n", 1)[0].strip()
                if len(preview) > 400:
                    preview = preview[:400].rsplit(" ", 1)[0] + "…"
                # Wrap path + URL in Markdown link form so Telegram (which
                # uses parse_mode=Markdown) doesn't interpret underscores in
                # the path/URL as italic markers. Inline path also gets
                # backticks so it renders as code and stays copy-pasteable.
                result_blurb = (
                    f"{preview}\n\n"
                    f"Full answer saved to vault: `{rel_path}`\n"
                    f"[Open in Obsidian]({obsidian_url})"
                )

        # Footer: when some MCP servers failed to initialize during the session,
        # list them so the operator can fix or remove the broken connectors from
        # the agent preset. Only populated by the managed (cloud) path.
        init_failed = getattr(outcome, "init_failed_mcps", None) or []
        footer = ""
        if init_failed:
            footer = f"\n\nNote: {len(init_failed)} MCP server(s) unavailable this session: {', '.join(init_failed)}"

        return (
            f"✅ {label}: completed '{title}' "
            f"({expected}) — {tokens:,} tokens, ${refreshed.total_dollars:.2f}, "
            f"{active_s}s active.\n\n{result_blurb}{footer}"
        )

    def _recover_result_from_transcript(self, session_id: str) -> str:
        """When the agent's `final_text` is empty, scan the transcript for
        the last successful tool result and use it as the operator-facing
        body. Caps the recovered text to fit the inline-summary budget
        (anything bigger gets ellipsized — the full content is in the
        transcript). Returns empty string when nothing usable exists.

        Looks at both shapes:
        - Local executor: `tool_call` events with `is_error=False` and a
          non-trivial `output_chars`. The full output isn't recorded in
          the transcript itself; the agent didn't summarize and the
          transcript only captured metadata, so we report what tool was
          called.
        - Managed executor: `managed_event_agent.mcp_tool_result` and
          `managed_event_agent.tool_result` events carry the actual
          textual content in `payload.content[*].text`. Those are the
          interesting ones — when the cloud agent drafts an email, the
          email body lands here.
        """
        import json as _json

        try:
            path = self.transcript_store.dir / f"{session_id}.jsonl"
            if not path.exists():
                return ""
            tool_results: list[tuple[str, str]] = []  # (tool_name, text)
            tool_calls: list[str] = []  # local-executor tool names

            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    kind = d.get("kind", "")
                    payload = d.get("payload", {}) or {}

                    if kind == "tool_call" and not payload.get("is_error"):
                        # Local-executor shape — metadata only, no body
                        tool_calls.append(str(payload.get("tool", "tool")))

                    if kind in (
                        "managed_event_agent.mcp_tool_result",
                        "managed_event_agent.tool_result",
                    ) and not payload.get("is_error"):
                        # Managed-executor shape — body is in content[*].text
                        for c in payload.get("content", []) or []:
                            text = c.get("text") if isinstance(c, dict) else None
                            if text:
                                tool_name = (
                                    payload.get("mcp_tool_use_id")
                                    or payload.get("name")
                                    or "tool"
                                )
                                tool_results.append((tool_name, text))

            if tool_results:
                # Most recent result wins.
                _, text = tool_results[-1]
                cap = _INLINE_SUMMARY_MAX_CHARS - 200
                if len(text) > cap:
                    text = text[:cap].rsplit(" ", 1)[0] + "…"
                return text

            if tool_calls:
                # Local executor doesn't capture body in transcript; at
                # least name the work that happened so the operator
                # knows it wasn't a no-op.
                last = tool_calls[-1]
                return f"(last tool called: `{last}` — see transcript for arguments)"
        except Exception as exc:
            logger.warning("recover_result_from_transcript failed for %s: %s",
                          session_id, exc)
        return ""

    def _spill_to_vault(
        self, session: Session, task: dict[str, Any], final_text: str,
    ) -> tuple[str, str] | None:
        """Write the agent's full response to a vault Markdown file and
        return (vault-relative path, obsidian:// URL). Returns None when
        the vault path is unset or the write fails — caller falls back to
        a truncated inline summary so the operator never loses content
        entirely.

        File layout: `<vault>/Inbox/Agent Output/<YYYY-MM-DD>-<slug>.md`.
        Inbox is the standard landing folder for Obsidian setups and
        keeps these next to other unsorted captures.
        """
        from datetime import datetime
        import re

        vault_root = settings.vault_path
        if not vault_root:
            return None
        try:
            vault_root = vault_root.expanduser() if hasattr(vault_root, "expanduser") else vault_root
        except Exception:
            return None

        title = (task.get("description") or session.task_id).strip()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60] or session.task_id
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        filename = f"{today}-{slug}.md"
        folder = "Inbox/Agent Output"

        try:
            target_dir = vault_root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            file_path = target_dir / filename
            # Frontmatter gives Obsidian / Dataview something to query on
            # without changing how the body renders.
            frontmatter = (
                "---\n"
                f"task: {title}\n"
                f"session_id: {session.session_id}\n"
                f"routing: {session.routing or 'unknown'}\n"
                f"created: {today}\n"
                "source: agent-worker\n"
                "---\n\n"
            )
            file_path.write_text(frontmatter + final_text, encoding="utf-8")
        except Exception as exc:
            logger.warning("vault spillover write failed for %s: %s", session.task_id, exc)
            return None

        rel_path = f"{folder}/{filename}"
        obsidian_url = build_obsidian_link(str(file_path), str(vault_root))
        return (rel_path, obsidian_url)

    def _mark_failed(self, session: Session, task: dict[str, Any], reason: str) -> None:
        title = task.get("description", session.task_id)
        self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
        self._set_task_status(session.task_id, "cancelled")
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(session.session_id, "failed", {"reason": reason})
        self._notify(f"⚠️ {_worker_label(session.routing)}: task '{title}' failed: {reason}")

    def _mark_blocked(self, session: Session, task: dict[str, Any], question: str) -> None:
        title = task.get("description", session.task_id)
        self._swap_tag(session.task_id, RUNNING_TAG, BLOCKED_TAG)
        self._set_task_status(session.task_id, "blocked")
        self.session_store.update_status(session.task_id, STATUS_BLOCKED)
        self.transcript_store.append(session.session_id, "blocked", {"question": question})

        # If the blocked session has a running remote Managed Agents session,
        # kill it now so session-hour billing stops during the (potentially
        # 3-day) clarification wait. Managed resume after kill isn't supported
        # in this MVP — the operator effectively retries by re-tagging once
        # they reply.
        if session.managed_agent_session_id:
            managed = self._get_managed_executor()
            if managed is not None and managed.driver is not None:
                try:
                    managed.driver.kill_session(
                        session.managed_agent_session_id,
                        reason="blocked_for_clarification",
                    )
                except Exception as exc:
                    logger.warning("kill_session %s on block failed: %s",
                                   session.managed_agent_session_id, exc)

        # Issue F: send the question with reply-threading enabled so the user's
        # reply lands in pending_questions.answer and the worker resumes.
        body = (
            f"⏸ {_worker_label(session.routing)}: task '{title}' needs your input.\n\n"
            f"{question}\n\n"
            "Reply to this message to answer."
        )
        sent_id = self.ask_user_via_telegram(
            session.session_id, session.task_id, body,
        )
        if sent_id is None:
            # Telegram not configured — fall back to the legacy one-way
            # message so the operator at least sees the question.
            self._notify(body)

    def _notify(self, text: str) -> None:
        try:
            self._telegram_send(text)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("telegram notify failed: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LIFEOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Wire up real Telegram senders in production. Worker() defaults to
    # no-op senders so tests can't accidentally hit a real chat — see
    # comment in __init__. If telegram.py isn't importable or the bot
    # isn't configured, the no-op fallbacks let the worker still run.
    telegram_send = None
    telegram_send_with_id = None
    try:
        from api.services.telegram import send_message, send_message_capture_id
        telegram_send = send_message
        telegram_send_with_id = send_message_capture_id
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Telegram module not importable; running with no-op senders: %s", exc)
    worker = Worker(
        telegram_send=telegram_send,
        telegram_send_with_id=telegram_send_with_id,
    )

    def _handle_signal(signum, _frame):
        logger.info("received signal %s; stopping after current tick", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    worker.run()


if __name__ == "__main__":
    main()
