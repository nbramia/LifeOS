"""Drives one Hermes turn from inside the agent worker (#851).

Mirrors the executor surface used by `ClaudeCodeExecutor`/`CodexExecutor`/
`LocalExecutor` — one `execute(session, task)` call, one `ExecutorOutcome`
— but the "subprocess" is a single synchronous HTTP round trip to the
configured Hermes backend, using the SAME request-building
(`_build_envelope`) and persistence (`_HermesTurnPersister`) the `/chat`
Hermes proxy uses, so a board-assigned Hermes turn's conversation and usage
rows are indistinguishable from one that came through `/chat`.

The worker submits each turn to its bounded dispatch pool. The executor keeps
the configured HTTP read-idle timeout and adds a separate absolute deadline so
one upstream request cannot monopolize the worker tick or run forever while it
continues emitting chunks.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import httpx

from api.routes.hermes_proxy import _HermesTurnPersister, _build_envelope
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.usage_ledger import (
    MEASURED,
    UNKNOWN,
    UsageLedger,
    UsageObservation,
    identity_for_session,
    reservation_id_for_session,
)
from api.services.agent_worker.executor_lifecycle import attempt_id_for
from config.settings import settings


logger = logging.getLogger(__name__)

# Same read timeout the browser-facing proxy gives a Hermes turn
# (api/routes/_proxy.py's TIMEOUT) — a board-assigned turn deserves the
# same patience as one typed into /chat.
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=5.0)

HttpClientFactory = Callable[[], httpx.Client]


def _default_client_factory() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT)


class HermesExecutor:
    """Run one Hermes turn synchronously and persist its state.

    Constructor injection points (test seams):
      - `http_client_factory` — override to return a fake/mocked sync
        `httpx.Client` whose `.stream()` yields a scripted SSE response,
        so tests never touch the network.
      - `persona_id` — which Hermes persona a board-assigned card opens a
        conversation as. Defaults to "primary"; a future card field could
        override this, but the issue doesn't ask for one.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        transcript_store: TranscriptStore,
        http_client_factory: Optional[HttpClientFactory] = None,
        persona_id: str = "primary",
        usage_ledger: UsageLedger | None = None,
    ) -> None:
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._client_factory = http_client_factory or _default_client_factory
        self._persona_id = persona_id
        self._usage_ledger = usage_ledger
        self._active_clients: dict[
            tuple[str, str | None, str | None], httpx.Client
        ] = {}
        self._cancelled: set[tuple[str, str | None, str | None]] = set()
        self._active_lock = threading.Lock()

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Start a new Hermes conversation turn."""
        return self._execute_turn(session, task, operation="execute")

    def resume(self, session, message: str, working_dir: str | None = None) -> ExecutorOutcome:
        """Continue the persisted Hermes conversation for this session."""
        task = {"id": session.task_id, "description": message}
        return self._execute_turn(
            session,
            task,
            conversation_id=getattr(session, "conversation_id", None),
            operation="resume",
        )

    def resume_after_children(
        self, session, request: str, child_results: list[object] | None = None,
    ) -> ExecutorOutcome:
        """Resume the same Hermes conversation after child completion."""
        return self.resume(session, request)

    def _execute_turn(
        self, session, task: dict, *, conversation_id: str | None = None,
        operation: str = "execute",
    ) -> ExecutorOutcome:
        sid = session.session_id
        prompt = self._build_prompt(task)
        if not prompt:
            self.transcript_store.append(sid, "hermes_no_prompt", {})
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=getattr(session, "attempt_id", None),
                turn_id=getattr(session, "turn_id", None),
            )
            return ExecutorOutcome(status=STATUS_FAILED, reason="empty prompt")

        if not settings.hermes_backend_url:
            self.transcript_store.append(sid, "hermes_not_configured", {})
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=getattr(session, "attempt_id", None),
                turn_id=getattr(session, "turn_id", None),
            )
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason="Hermes backend not configured (LIFEOS_HERMES_BACKEND_URL)",
            )

        try:
            session = self.session_store.begin_executor_turn(
                session.task_id, operation, session=session,
            )
        except RuntimeError as exc:
            # A queued turn can be cancelled after its dispatch snapshot was
            # taken but before the executor gets here.  Do not allocate a new
            # turn on a terminal attempt, and never let that late callback
            # reach the upstream stream.
            self.transcript_store.append(sid, "hermes_start_rejected", {
                "reason": str(exc),
                "attempt_id": getattr(session, "attempt_id", None),
                "turn_id": getattr(session, "turn_id", None),
            })
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason="hermes turn cancelled before start",
                session_id=sid,
                attempt_id=attempt_id_for(session),
                turn_id=getattr(session, "turn_id", None),
                executor="hermes",
                termination_evidence={
                    "cancelled": True,
                    "terminal_success": False,
                },
            )
        sid = session.session_id
        attempt_id = session.attempt_id
        turn_id = session.turn_id
        active_key = (sid, attempt_id, turn_id)

        request_body = {"question": prompt, "persona_id": self._persona_id}
        if conversation_id:
            request_body["conversation_id"] = conversation_id
        raw_body = json.dumps(request_body).encode("utf-8")
        try:
            envelope_body = _build_envelope(raw_body)
        except Exception as exc:  # noqa: BLE001 — _build_envelope raises HTTPException
            self.transcript_store.append(sid, "hermes_envelope_failed", {"error": str(exc)})
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=attempt_id, turn_id=turn_id,
            )
            return ExecutorOutcome(status=STATUS_FAILED, reason=f"envelope build failed: {exc}")

        # Cancellation can win after begin_executor_turn() allocated the
        # immutable ids but before the upstream request is created.  The
        # compare-and-set fences that exact attempt/turn and keeps a FAILED
        # row from being resurrected as RUNNING.
        started = self.session_store.mark_executor_turn_running(
            session.task_id, attempt_id, turn_id,
        )
        cancelled_before_stream = self._is_cancelled(sid, attempt_id, turn_id)
        if not started or cancelled_before_stream:
            if started and cancelled_before_stream:
                self.session_store.update_status(
                    session.task_id, STATUS_FAILED,
                    attempt_id=attempt_id, turn_id=turn_id,
                )
            self.transcript_store.append(sid, "hermes_start_rejected", {
                "reason": "cancelled before upstream stream",
                "attempt_id": attempt_id,
                "turn_id": turn_id,
            })
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason="hermes turn cancelled before start",
                session_id=sid,
                attempt_id=attempt_id,
                turn_id=turn_id,
                executor="hermes",
                termination_evidence={
                    "cancelled": True,
                    "terminal_success": False,
                },
            )
        self.transcript_store.append(sid, "hermes_spawn", {"question_chars": len(prompt)})

        persister = _HermesTurnPersister(
            question=prompt,
            persona_id=self._persona_id,
            # The board turn's local attempt/turn is the canonical identity;
            # the proxy persister must not also materialize a conversation-
            # based usage key for the same logical turn.
            persist_usage=False,
            write_guard=lambda: self.session_store.is_current_turn(
                session.task_id, attempt_id, turn_id,
            ),
        )
        url = f"{settings.hermes_backend_url.rstrip('/')}/api/ask/stream"
        headers = {"Content-Type": "application/json"}
        if settings.hermes_backend_token:
            headers["Authorization"] = f"Bearer {settings.hermes_backend_token}"

        budget = getattr(session, "budget", None) or {}
        wall_seconds = budget.get("wall_seconds") or getattr(
            settings, "claude_timeout_seconds", 3600,
        )
        started_at = getattr(session, "started_at", None)
        elapsed = max(0.0, time.time() - float(started_at)) if started_at else 0.0
        remaining_wall = max(0.01, float(wall_seconds) - elapsed)
        deadline = time.monotonic() + remaining_wall

        try:
            client = self._client_factory()
            with self._active_lock:
                self._active_clients[active_key] = client
            stream_error: list[BaseException] = []
            stream_done = threading.Event()
            close_lock = threading.Lock()
            closed = False

            def close_client() -> None:
                nonlocal closed
                with close_lock:
                    if closed:
                        return
                    closed = True
                close = getattr(client, "close", None)
                if callable(close):
                    close()
                with self._active_lock:
                    if self._active_clients.get(active_key) is client:
                        self._active_clients.pop(active_key, None)

            def watch_cancellation() -> None:
                # Operator/inter-agent cancellation is persisted by the
                # shared teardown path.  Polling the identity as well as the
                # status catches both cancellation and a newer attempt while
                # the upstream is blocked between SSE chunks.
                while not stream_done.wait(0.05):
                    current = self.session_store.get(session.task_id)
                    if current is None or not self.session_store.is_current_turn(
                        session.task_id, attempt_id, turn_id,
                    ) or current.status in {STATUS_FAILED, "budget_exceeded"}:
                        with self._active_lock:
                            self._cancelled.add((sid, attempt_id, turn_id))
                        close_client()
                        return

            def consume() -> None:
                try:
                    with client.stream("POST", url, content=envelope_body, headers=headers) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_bytes():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("hermes absolute turn deadline exceeded")
                            persister.observe(chunk)
                except BaseException as exc:  # propagate to the bounded caller
                    stream_error.append(exc)
                finally:
                    close_client()
                    stream_done.set()

            thread = threading.Thread(
                target=consume, name="hermes-turn", daemon=True,
            )
            watcher = threading.Thread(
                target=watch_cancellation, name="hermes-cancel-watch", daemon=True,
            )
            thread.start()
            watcher.start()
            remaining = max(0.01, deadline - time.monotonic())
            if not stream_done.wait(remaining):
                close_client()
                # Give a cooperative httpx/fake stream a short chance to
                # unwind; an uncooperative reader remains daemonized and can
                # never publish a terminal outcome after timeout return.
                thread.join(0.1)
                raise TimeoutError("hermes absolute turn deadline exceeded")
            if stream_error:
                raise stream_error[0]
        except Exception as exc:  # noqa: BLE001 — network/HTTP errors of every shape
            persister.finalize()
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=attempt_id, turn_id=turn_id,
            )
            self._record_reported_model(session, persister)
            self._record_usage(session, persister)
            final_text = persister.content_text.strip()
            done_seen = persister.done_seen
            error_seen = persister.error_seen
            self.transcript_store.append(sid, "hermes_request_failed", {
                "error": str(exc),
                "conversation_id": persister.conversation_id,
                "partial_chars": len(final_text),
                "done_seen": done_seen,
                "error_seen": error_seen,
                "stream_truncated": not done_seen,
            })
            cancelled = self._is_cancelled(sid, attempt_id, turn_id)
            return ExecutorOutcome(
                status=STATUS_FAILED,
                final_text=final_text,
                reason=(
                    "hermes turn cancelled"
                    if cancelled
                    else "hermes absolute turn deadline exceeded"
                    if isinstance(exc, TimeoutError)
                    else f"hermes request failed: {exc}"
                ),
                session_id=session.session_id,
                attempt_id=attempt_id_for(session),
                turn_id=getattr(session, "turn_id", None),
                executor="hermes",
                continuation_id=persister.conversation_id or conversation_id,
                termination_evidence={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": not done_seen,
                    "terminal_success": False,
                    "absolute_deadline": isinstance(exc, TimeoutError) and not cancelled,
                    "cancelled": cancelled,
                },
                exit_meta={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": not done_seen,
                },
            )

        persister.finalize()
        self._record_reported_model(session, persister)
        self._record_usage(session, persister)

        conversation_id = persister.conversation_id
        final_text = persister.content_text.strip()
        done_seen = persister.done_seen
        error_seen = persister.error_seen
        stream_truncated = not done_seen

        if self._is_cancelled(sid, attempt_id, turn_id):
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=attempt_id, turn_id=turn_id,
            )
            return ExecutorOutcome(
                status=STATUS_FAILED,
                final_text=final_text,
                reason="hermes turn cancelled",
                session_id=session.session_id,
                attempt_id=attempt_id_for(session),
                turn_id=getattr(session, "turn_id", None),
                executor="hermes",
                continuation_id=conversation_id,
                termination_evidence={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": stream_truncated,
                    "terminal_success": False,
                    "cancelled": True,
                },
                exit_meta={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": stream_truncated,
                },
            )

        if conversation_id:
            self.session_store.set_conversation_id(
                session.task_id, conversation_id,
                attempt_id=attempt_id, turn_id=turn_id,
            )
        terminal_valid = bool(final_text) and done_seen and not error_seen
        if not terminal_valid:
            if error_seen:
                reason = "hermes stream reported an error"
                event_kind = "hermes_stream_failed"
            elif not done_seen:
                reason = "hermes stream ended before terminal done"
                event_kind = "hermes_stream_interrupted"
            else:
                reason = "hermes turn produced no content"
                event_kind = "hermes_stream_failed"
            self.transcript_store.append(sid, event_kind, {
                "conversation_id": conversation_id,
                "partial_chars": len(final_text),
                "done_seen": done_seen,
                "error_seen": error_seen,
                "stream_truncated": stream_truncated,
            })
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=attempt_id, turn_id=turn_id,
            )
            return ExecutorOutcome(
                status=STATUS_FAILED,
                final_text=final_text,
                reason=reason,
                session_id=session.session_id,
                attempt_id=attempt_id_for(session),
                turn_id=getattr(session, "turn_id", None),
                executor="hermes",
                continuation_id=conversation_id or persister.conversation_id,
                usage={
                    "model": persister.reported_model,
                    "input_tokens": getattr(persister, "_usage_input_tokens", 0),
                    "output_tokens": getattr(persister, "_usage_output_tokens", 0),
                },
                termination_evidence={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": stream_truncated,
                    "terminal_success": False,
                },
                exit_meta={
                    "done_seen": done_seen,
                    "error_seen": error_seen,
                    "stream_truncated": stream_truncated,
                },
            )

        completed = self.session_store.update_status(
            session.task_id, STATUS_COMPLETED,
            attempt_id=attempt_id, turn_id=turn_id,
        )
        if not completed:
            # A cancellation may land after the pre-success check but before
            # this write.  Preserve FAILED and keep stale attempts fenced.
            if self._is_cancelled(sid, attempt_id, turn_id):
                return ExecutorOutcome(
                    status=STATUS_FAILED,
                    final_text=final_text,
                    reason="hermes turn cancelled",
                    session_id=session.session_id,
                    attempt_id=attempt_id,
                    turn_id=turn_id,
                    executor="hermes",
                    continuation_id=conversation_id or persister.conversation_id,
                    termination_evidence={
                        "done_seen": done_seen,
                        "error_seen": error_seen,
                        "stream_truncated": stream_truncated,
                        "terminal_success": False,
                        "cancelled": True,
                    },
                )
            return ExecutorOutcome(
                status=STATUS_RUNNING,
                reason="stale Hermes turn",
                session_id=session.session_id,
                attempt_id=attempt_id,
                turn_id=turn_id,
                executor="hermes",
            )
        self.transcript_store.append(sid, "hermes_completed", {
            "conversation_id": conversation_id,
            "final_chars": len(final_text),
            "done_seen": done_seen,
            "error_seen": error_seen,
            "stream_truncated": stream_truncated,
        })
        return ExecutorOutcome(
            status=STATUS_COMPLETED,
            final_text=final_text,
            session_id=session.session_id,
            attempt_id=attempt_id_for(session),
            turn_id=getattr(session, "turn_id", None),
            executor="hermes",
            continuation_id=conversation_id or persister.conversation_id,
            usage={
                "model": persister.reported_model,
                "input_tokens": getattr(persister, "_usage_input_tokens", 0),
                "output_tokens": getattr(persister, "_usage_output_tokens", 0),
            },
            termination_evidence={
                "done_seen": done_seen,
                "error_seen": error_seen,
                "stream_truncated": stream_truncated,
                "terminal_success": terminal_valid,
            },
            exit_meta={
                "done_seen": done_seen,
                "error_seen": error_seen,
                "stream_truncated": stream_truncated,
            },
        )

    def _is_cancelled(
        self, session_id: str, attempt_id: str | None, turn_id: str | None,
    ) -> bool:
        with self._active_lock:
            local = (
                (session_id, attempt_id, turn_id) in self._cancelled
                or (session_id, attempt_id, None) in self._cancelled
            )
        if local:
            return True
        # Operator/API teardown can run in a different process.  The shared
        # exact cancellation guard closes that cross-process race.
        return bool(self.session_store.is_cancelled(
            getattr(self.session_store.get_by_session_id(session_id), "task_id", ""),
            attempt_id,
            turn_id,
        ))

    def cancel(self, session, reason: str = ""):
        """Close the active upstream request for this exact Hermes session."""
        from api.services.agent_worker.executor_lifecycle import (
            CancelResult,
            attempt_id_for,
            turn_id_for,
        )

        sid = session.session_id
        attempt_id = attempt_id_for(session)
        turn_id = getattr(session, "turn_id", None)
        with self._active_lock:
            self._cancelled.add((sid, attempt_id, turn_id))
            client = self._active_clients.get((sid, attempt_id, turn_id))
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self.transcript_store.append(sid, "cancel_requested", {
            "reason": reason,
            "conversation_id": getattr(session, "conversation_id", None),
        })
        return CancelResult(
            cancelled=True,
            reason=reason or "cancelled",
            session_id=sid,
            attempt_id=attempt_id_for(session),
            turn_id=turn_id_for(session),
            continuation_id=getattr(session, "conversation_id", None),
        )

    def _record_reported_model(self, session, persister: _HermesTurnPersister) -> None:
        """Record the model Hermes reported for THIS turn onto THIS
        session's own row — run on both the success and failure exit
        paths, right after `persister.finalize()`, so a turn whose
        connection dropped after Hermes reported usage still gets credited:
        it ran on that model regardless of how the request ended. Wrapped
        so a persistence failure here can never turn an otherwise-completed
        turn into a failed one, matching this class's existing tolerance
        for store errors elsewhere (`_HermesTurnPersister.finalize` itself
        swallows its own store failures for the same reason)."""
        if not persister.reported_model:
            return
        try:
            self.session_store.set_hermes_model(
                session.task_id, persister.reported_model,
                attempt_id=getattr(session, "attempt_id", None),
                turn_id=getattr(session, "turn_id", None),
            )
        except Exception:  # noqa: BLE001 — never let a store failure fail the turn
            logger.warning(
                "hermes turn persistence: failed to record reported model for %s",
                session.task_id, exc_info=True,
            )

    def _record_usage(self, session, persister: _HermesTurnPersister) -> None:
        """Feed board-assigned Hermes usage into the canonical ledger.

        The bridge's ``model`` label is a requested/upstream label, not proof
        of the model that served a fallback turn.  Consequently this adapter
        intentionally leaves all served identity fields unknown.  The legacy
        per-session ``hermes_model`` readout remains untouched for compatibility.
        """
        if not getattr(persister, "_usage_captured", False):
            return
        try:
            sid, attempt_id, turn_id = identity_for_session(session)
            spec = getattr(session, "execution_spec", None) or {}
            requested_model = getattr(session, "model", None) or spec.get("model_id")
            cost = float(getattr(persister, "_usage_cost_usd", 0.0))
            unpriced = bool(getattr(persister, "_usage_unpriced", False))
            ledger = self._usage_ledger or UsageLedger(self.session_store.db_path)
            ledger.record(UsageObservation(
                session_id=sid,
                attempt_id=attempt_id,
                turn_id=turn_id,
                source="hermes_executor",
                source_event_id=getattr(persister, "conversation_id", None),
                event_id=f"hermes:{sid}:{attempt_id}:{turn_id}",
                input_tokens=int(getattr(persister, "_usage_input_tokens", 0)),
                output_tokens=int(getattr(persister, "_usage_output_tokens", 0)),
                input_kind=MEASURED,
                output_kind=MEASURED,
                cost_usd=None if unpriced else cost,
                cost_kind=UNKNOWN if unpriced else MEASURED,
                billing_class="unknown" if unpriced else "metered",
                requested_engine="hermes",
                requested_model=requested_model,
                evidence_source="hermes_usage_event",
                reservation_id=reservation_id_for_session(session),
            ))
            # Keep the long-standing admin/conversation readout materialized,
            # but bind it to this same canonical key. The proxy persister is
            # disabled for board turns, so this is the sole legacy projection
            # for the turn.
            from api.routes import hermes_proxy
            usage_store = hermes_proxy.get_usage_store()
            has_key = getattr(usage_store, "has_usage_key", None)
            if not callable(has_key) or not has_key(f"{sid}:{attempt_id}:{turn_id}"):
                usage_store.record_usage(
                    model=persister.reported_model,
                    input_tokens=int(getattr(persister, "_usage_input_tokens", 0)),
                    output_tokens=int(getattr(persister, "_usage_output_tokens", 0)),
                    cost_usd=cost,
                    conversation_id=getattr(persister, "conversation_id", None),
                    unpriced=unpriced,
                    usage_key=f"{sid}:{attempt_id}:{turn_id}",
                    requested_engine="hermes",
                    requested_model=requested_model,
                    billing_class="unknown" if unpriced else "metered",
                    evidence_source="hermes_usage_event",
                )
        except Exception:  # noqa: BLE001 — accounting must not change turn outcome
            logger.warning("hermes turn ledger write failed for %s", session.task_id, exc_info=True)

    @staticmethod
    def _build_prompt(task: dict) -> str:
        """The card's title (`task["description"]`) plus its notes, if any
        — mirrors the CLI routes' `task["description"]`-as-prompt
        convention, extended with notes since a Hermes conversation has no
        separate "system prompt" slot for extra context the way a CLI
        invocation's `--append-system-prompt` does."""
        title = (task.get("description") or "").strip()
        notes = (task.get("notes") or "").strip()
        if title and notes:
            return f"{title}\n\n{notes}"
        return title or notes
