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
    STATUS_YIELDED,
    Session,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from config.settings import settings


logger = logging.getLogger(__name__)


AGENT_TAG = "agent"
RUNNING_TAG = "agent-running"
BLOCKED_TAG = "agent-blocked"
FAILED_TAG = "agent-failed"
BUDGET_EXCEEDED_TAG = "agent-budget-exceeded"


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
        http_client: httpx.Client | None = None,
        preflight_caller=None,  # injectable; defaults to Anthropic Haiku
        local_executor=None,    # injectable LocalExecutor for tests
    ) -> None:
        self.api_base = (api_base or os.environ.get("LIFEOS_API_URL", "http://localhost:8000")).rstrip("/")
        self.session_store = session_store or SessionStore()
        self.transcript_store = transcript_store or TranscriptStore()
        self.spend_tracker = spend_tracker or SpendTracker(
            daily_cap_dollars=settings.agent_daily_cap_dollars,
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.agent_worker_poll_seconds
        self._stop = False
        # Telegram is optional — operator may not have configured it. The
        # worker should still run end-to-end without crashing on a missing bot.
        if telegram_send is None:
            def _noop_telegram(text, chat_id=None):
                return False
            try:
                from api.services.telegram import send_message
                telegram_send = send_message
            except Exception:  # pragma: no cover — defensive
                telegram_send = _noop_telegram
        self._telegram_send = telegram_send
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=10.0)
        self._preflight_caller = preflight_caller  # None → use Anthropic SDK by default
        self._local_executor = local_executor  # lazily instantiated on first use

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
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            self._notify(
                f"⚠️ Agent worker: task left in {session.status!r} from a prior "
                f"run could not be safely resumed — tag rolled back to "
                f"#{AGENT_TAG} for retry. Transcript: "
                f"data/agent_transcripts/{sid}.jsonl"
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
            executor = self._get_local_executor()
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("wake execute failed for %s: %s", session_id, exc)
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
                self._notify(
                    f"⚠️ Agent worker: error while resuming sleeping task "
                    f"'{task.get('description', session.task_id)}': {exc}"
                )
                continue
            self._handle_outcome(session, task, outcome)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _list_agent_tasks(self) -> list[dict[str, Any]]:
        """Fetch open `#agent` tasks from the API."""
        try:
            resp = self._http.get(
                f"{self.api_base}/api/tasks",
                params={"status": "todo", "tag": AGENT_TAG},
            )
            resp.raise_for_status()
            return resp.json().get("tasks", [])
        except Exception as exc:
            logger.warning("failed to list agent tasks: %s", exc)
            return []

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

    # ------------------------------------------------------------------
    # Claim + dispatch
    # ------------------------------------------------------------------

    def _claim(self, task_id: str) -> bool:
        """Atomically swap `#agent` → `#agent-running` and record the session.

        Returns True iff this worker won the race.
        """
        if not self._swap_tag(task_id, AGENT_TAG, RUNNING_TAG):
            return False
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

    def _get_local_executor(self):
        if self._local_executor is None:
            from api.services.agent_worker.local_executor import LocalExecutor
            self._local_executor = LocalExecutor(
                session_store=self.session_store,
                transcript_store=self.transcript_store,
            )
        return self._local_executor

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
            executor = self._get_local_executor()
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("local executor crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"executor crashed: {exc}")
                return
            self._handle_outcome(session, task, outcome)
            return

        # Claude route: deferred to Issue D. Roll the tag back and notify.
        if pre.routing == ROUTE_CLAUDE:
            self._swap_tag(task_id, RUNNING_TAG, AGENT_TAG)
            self.session_store.update_status(task_id, STATUS_BLOCKED)
            self.transcript_store.append(sid, "claude_routing_deferred", {})
            self._notify(
                f"⏸ Agent worker: task '{title}' routed to Claude but Managed "
                f"Agents driver isn't installed yet (Issue D). Task left at "
                f"#{AGENT_TAG} for when D ships."
            )
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
            self._notify(self._completion_summary(session, task, outcome))
            return

        if outcome.status == STATUS_BUDGET_EXCEEDED:
            self._swap_tag(session.task_id, RUNNING_TAG, BUDGET_EXCEEDED_TAG)
            self._notify(
                f"⚠️ Agent worker: task '{title}' hit its budget ({outcome.reason}). "
                f"Transcript: data/agent_transcripts/{sid}.jsonl"
            )
            return

        if outcome.status == STATUS_FAILED:
            self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
            self._notify(
                f"⚠️ Agent worker: task '{title}' failed: {outcome.reason}. "
                f"Transcript: data/agent_transcripts/{sid}.jsonl"
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
        wall = int(time.time()) - int(refreshed.started_at)
        expected = refreshed.expected_output or "text"
        title = task.get("description", session.task_id)
        result_blurb = (outcome.final_text or "(no text response)").strip()
        if len(result_blurb) > 600:
            result_blurb = result_blurb[:600] + "…"
        return (
            f"✅ Agent worker: completed '{title}' "
            f"({expected}) — {tokens:,} tokens, ${refreshed.total_dollars:.2f}, "
            f"{wall}s wall.\n\n{result_blurb}"
        )

    def _mark_failed(self, session: Session, task: dict[str, Any], reason: str) -> None:
        title = task.get("description", session.task_id)
        self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(session.session_id, "failed", {"reason": reason})
        self._notify(f"⚠️ Agent worker: task '{title}' failed: {reason}")

    def _mark_blocked(self, session: Session, task: dict[str, Any], question: str) -> None:
        title = task.get("description", session.task_id)
        self._swap_tag(session.task_id, RUNNING_TAG, BLOCKED_TAG)
        self.session_store.update_status(session.task_id, STATUS_BLOCKED)
        self.transcript_store.append(session.session_id, "blocked", {"question": question})
        # In Issue F this becomes a reply-threaded message and the reply
        # resumes the task. For now it's one-way.
        self._notify(
            f"⏸ Agent worker: task '{title}' needs your input.\n\n{question}\n\n"
            f"(Re-tag the task with #{AGENT_TAG} once you've added the answer in the task title or context.)"
        )

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
    worker = Worker()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s; stopping after current tick", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    worker.run()


if __name__ == "__main__":
    main()
