"""Internal executor lifecycle adapters.

The worker has several deliberately different drivers (in-process, polling,
HTTP, and CLI).  This module gives the worker one small, route-aware seam for
the lifecycle operations that must not silently change engines.  It is an
internal contract: it does not alter MCP or HTTP payloads.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from typing import Any, Callable, Protocol

from api.services.agent_worker.local_executor import (
    ExecutorOutcome,
)
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
)


@dataclass(frozen=True)
class ExecutorCapabilities:
    """Operations an executor can perform without a route fallback."""

    start: bool = False
    resume: bool = False
    resume_after_children: bool = False
    cancel: bool = False


@dataclass(frozen=True)
class CancelResult:
    """Stable internal cancellation result with target identity."""

    cancelled: bool
    idempotent: bool = False
    reason: str = ""
    session_id: str | None = None
    attempt_id: str | None = None
    turn_id: str | None = None
    continuation_id: str | None = None


class ExecutorAdapter(Protocol):
    route: str
    capabilities: ExecutorCapabilities

    def start(self, session: Any, request: Any) -> ExecutorOutcome: ...
    def resume(self, session: Any, message: str, working_dir: str | None = None) -> ExecutorOutcome: ...
    def resume_after_children(
        self, session: Any, request: Any, child_results: list[Any]
    ) -> ExecutorOutcome: ...
    def cancel(self, session: Any, reason: str = "") -> CancelResult: ...


def continuation_id_for(session: Any) -> str | None:
    """Return the persisted continuation identity for a session."""
    route = getattr(session, "routing", None)
    if route in {"claude_code", "codex"}:
        return getattr(session, "claude_code_session_id", None)
    if route == "claude":
        return getattr(session, "managed_agent_session_id", None)
    if route == "hermes":
        return getattr(session, "conversation_id", None)
    return None


def attempt_id_for(session: Any) -> str | None:
    """Return the immutable attempt identity persisted by SessionStore.

    Legacy in-memory snapshots get a namespaced compatibility identity rather
    than reusing ``session_id`` as an attempt key.
    """
    attempt_id = getattr(session, "attempt_id", None)
    if attempt_id:
        return attempt_id
    sid = getattr(session, "session_id", None)
    return f"legacy:{sid}" if sid else None


def turn_id_for(session: Any) -> str | None:
    """Return the current immutable executor-turn identity."""
    return getattr(session, "turn_id", None)


def normalize_outcome(
    outcome: ExecutorOutcome,
    session: Any,
    *,
    route: str | None = None,
    continuation_id: str | None = None,
) -> ExecutorOutcome:
    """Attach durable identity and enforce explicit terminal evidence.

    Existing drivers predate this adapter and expose terminal evidence in
    ``exit_meta``.  We only reject a completed result when a driver explicitly
    reports missing evidence; absence of an old field remains compatible.
    """
    route = route or getattr(session, "routing", None)
    evidence = dict(getattr(outcome, "termination_evidence", {}) or {})
    evidence.update(getattr(outcome, "exit_meta", {}) or {})
    if outcome.status == STATUS_COMPLETED:
        if evidence.get("terminal_success") is False:
            outcome = replace(
                outcome,
                status=STATUS_FAILED,
                reason="terminal success evidence missing",
            )
        elif evidence.get("done_seen") is False:
            outcome = replace(
                outcome,
                status=STATUS_FAILED,
                reason="terminal success evidence missing",
            )
        elif evidence.get("stream_terminal_event_seen") is False:
            outcome = replace(
                outcome,
                status=STATUS_FAILED,
                reason="terminal success evidence missing",
            )
    return replace(
        outcome,
        session_id=getattr(outcome, "session_id", None) or getattr(session, "session_id", None),
        attempt_id=getattr(outcome, "attempt_id", None) or attempt_id_for(session),
        turn_id=getattr(outcome, "turn_id", None) or turn_id_for(session),
        executor=getattr(outcome, "executor", None) or route,
        continuation_id=(
            getattr(outcome, "continuation_id", None)
            or continuation_id
            or continuation_id_for(session)
        ),
        termination_evidence=evidence,
    )


class _Adapter:
    """Compatibility adapter around an existing executor instance."""

    def __init__(
        self,
        route: str,
        executor: Any,
        *,
        capabilities: ExecutorCapabilities,
        child_resume: Callable[[Any, Any, list[Any]], ExecutorOutcome] | None = None,
        cancel_fn: Callable[[Any, str], CancelResult] | None = None,
        session_store: Any | None = None,
    ) -> None:
        self.route = route
        self.executor = executor
        self.capabilities = capabilities
        self._child_resume = child_resume
        self._cancel_fn = cancel_fn
        self._session_store = session_store

    def _outcome(self, outcome: ExecutorOutcome, session: Any) -> ExecutorOutcome:
        # Executors persist the turn immediately before their side effect.  A
        # caller may still hold the pre-turn Session object, so normalize from
        # the persisted row whenever this adapter has a store.  This is what
        # makes attempt_id/turn_id in the returned outcome authoritative.
        persisted = session
        if self._session_store is not None:
            try:
                persisted = self._session_store.get(getattr(session, "task_id", "")) or session
            except Exception:
                persisted = session
        return normalize_outcome(outcome, persisted, route=self.route)

    def start(self, session: Any, request: Any) -> ExecutorOutcome:
        if hasattr(self.executor, "start") and self.route == "claude":
            result = self.executor.start(session, request)
        else:
            result = self.executor.execute(session, request)
        return self._outcome(result, session)

    def resume(self, session: Any, message: str, working_dir: str | None = None) -> ExecutorOutcome:
        resume = getattr(self.executor, "resume", None)
        if callable(resume):
            if working_dir is not None:
                return self._outcome(resume(session, message, working_dir=working_dir), session)
            return self._outcome(resume(session, message), session)
        # Local/remote share the conversation store and intentionally reuse
        # execute() for a new user turn; this is still native to that engine,
        # not a fallback to another route.
        execute = getattr(self.executor, "execute", None)
        if callable(execute):
            if self._session_store is not None:
                appended = self._session_store.append_message(
                    session.session_id, "user", message,
                    attempt_id=getattr(session, "attempt_id", None),
                    turn_id=getattr(session, "turn_id", None),
                )
                if appended is None:
                    return self._outcome(
                        ExecutorOutcome(status=STATUS_RUNNING, reason="stale session attempt/turn"),
                        session,
                    )
            return self._outcome(
                execute(session, {"id": session.task_id, "description": message}),
                session,
            )
        return unsupported_resume(session, self.route)

    def resume_after_children(
        self, session: Any, request: Any, child_results: list[Any]
    ) -> ExecutorOutcome:
        if self._child_resume is not None:
            return self._outcome(self._child_resume(session, request, child_results), session)
        message = request if isinstance(request, str) else str(request)
        if self._session_store is not None:
            appended = self._session_store.append_message(
                session.session_id, "user", message,
                attempt_id=getattr(session, "attempt_id", None),
                turn_id=getattr(session, "turn_id", None),
            )
            if appended is None:
                return self._outcome(
                    ExecutorOutcome(status=STATUS_RUNNING, reason="stale session attempt/turn"),
                    session,
                )
        resume = getattr(self.executor, "resume", None)
        if callable(resume):
            return self._outcome(resume(session, message), session)
        execute = getattr(self.executor, "execute", None)
        if callable(execute):
            return self._outcome(execute(session, {"id": session.task_id, "description": message}), session)
        return unsupported_resume(session, self.route)

    def cancel(self, session: Any, reason: str = "") -> CancelResult:
        if self._cancel_fn is not None:
            return self._cancel_fn(session, reason)
        cancel = getattr(self.executor, "cancel", None)
        if callable(cancel):
            result = cancel(session, reason)
            if isinstance(result, CancelResult):
                return result
        return CancelResult(
            cancelled=False,
            reason="unsupported_cancel",
            session_id=getattr(session, "session_id", None),
            attempt_id=attempt_id_for(session),
            turn_id=turn_id_for(session),
            continuation_id=continuation_id_for(session),
        )


def unsupported_resume(session: Any, route: str) -> ExecutorOutcome:
    """Stable compatibility rejection; callers must not mutate yield state."""
    return normalize_outcome(
        ExecutorOutcome(
            status=STATUS_FAILED,
            reason="unsupported_resume",
            termination_evidence={"reason_code": "unsupported_resume", "route": route},
        ),
        session,
        route=route,
    )


_CAPABILITIES = {
    "local": ExecutorCapabilities(True, True, True, True),
    "remote": ExecutorCapabilities(True, True, True, True),
    "claude": ExecutorCapabilities(True, False, True, True),
    "hermes": ExecutorCapabilities(True, True, True, True),
    "claude_code": ExecutorCapabilities(True, True, True, True),
    "codex": ExecutorCapabilities(True, True, True, True),
}


def route_supports_resume_after_children(route: str | None) -> bool:
    """Validate a route before a caller mutates ``yielded`` state."""
    return bool(route and _CAPABILITIES.get(route, ExecutorCapabilities()).resume_after_children)


class ExecutorRegistry:
    """Route registry with no implicit LocalExecutor fallback."""

    def __init__(self, session_store: Any | None = None) -> None:
        self._adapters: dict[str, ExecutorAdapter] = {}
        self._inflight: set[tuple[str, str, str]] = set()
        self._cancelled: set[tuple[str, str, str | None]] = set()
        self._lock = threading.Lock()
        self._session_store = session_store

    def register(self, route: str, adapter: ExecutorAdapter) -> None:
        self._adapters[route] = adapter

    def get(self, route: str | None) -> ExecutorAdapter | None:
        return self._adapters.get(route or "")

    def capabilities(self, route: str | None) -> ExecutorCapabilities:
        adapter = self.get(route)
        return adapter.capabilities if adapter else ExecutorCapabilities()

    def supports(self, route: str | None, operation: str) -> bool:
        return bool(getattr(self.capabilities(route), operation, False))

    def begin(self, session: Any, operation: str) -> bool:
        key = (getattr(session, "session_id", ""), attempt_id_for(session) or "", operation)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
        return True

    def finish(self, session: Any, operation: str) -> None:
        key = (getattr(session, "session_id", ""), attempt_id_for(session) or "", operation)
        with self._lock:
            self._inflight.discard(key)

    def cancel_once(self, session: Any, reason: str = "") -> CancelResult:
        if self._session_store is not None:
            try:
                session = self._session_store.get(getattr(session, "task_id", "")) or session
            except Exception:
                pass
        task_id = getattr(session, "task_id", "")
        key = (
            getattr(session, "session_id", ""),
            attempt_id_for(session) or "",
            turn_id_for(session),
        )
        if getattr(session, "status", None) in TERMINAL_STATUSES:
            return CancelResult(
                cancelled=False,
                idempotent=True,
                reason="already_terminal",
                session_id=key[0],
                attempt_id=key[1],
                turn_id=turn_id_for(session),
                continuation_id=continuation_id_for(session),
            )
        adapter = self.get(getattr(session, "routing", None))
        if adapter is None or not adapter.capabilities.cancel:
            return CancelResult(
                cancelled=False,
                reason="unsupported_cancel",
                session_id=key[0],
                attempt_id=key[1],
                turn_id=turn_id_for(session),
                continuation_id=continuation_id_for(session),
            )
        with self._lock:
            if key in self._cancelled:
                return CancelResult(
                    cancelled=False,
                    idempotent=True,
                    reason="already_cancelled",
                    session_id=key[0],
                    attempt_id=key[1],
                    turn_id=turn_id_for(session),
                    continuation_id=continuation_id_for(session),
                )
            self._cancelled.add(key)
        # Persist the exact cancellation fence before signalling the engine.
        # This ordering lets a clean late return observe the terminal decision
        # even when the provider takes time to close its stream/process.
        if self._session_store is not None:
            try:
                persisted = self._session_store.mark_cancelled(
                    task_id,
                    attempt_id=key[1],
                    turn_id=turn_id_for(session),
                    reason=reason,
                )
                if not persisted:
                    return CancelResult(
                        cancelled=False,
                        idempotent=True,
                        reason="stale session attempt/turn",
                        session_id=key[0],
                        attempt_id=key[1],
                        turn_id=turn_id_for(session),
                        continuation_id=continuation_id_for(session),
                    )
            except Exception:
                # Cancellation must remain best-effort if the durable store
                # is unavailable; the engine is still signalled below.
                pass
        result = adapter.cancel(session, reason)
        return replace(
            result,
            session_id=result.session_id or key[0],
            attempt_id=result.attempt_id or key[1],
            turn_id=result.turn_id or turn_id_for(session),
            continuation_id=result.continuation_id or continuation_id_for(session),
        )


def adapter_for(
    route: str,
    executor: Any,
    *,
    session_store: Any | None = None,
    child_resume: Callable[[Any, Any, list[Any]], ExecutorOutcome] | None = None,
    cancel_fn: Callable[[Any, str], CancelResult] | None = None,
) -> ExecutorAdapter:
    """Build a route adapter; unknown routes are deliberately rejected."""
    if route not in _CAPABILITIES:
        raise ValueError(f"unsupported executor route: {route}")
    return _Adapter(
        route,
        executor,
        capabilities=_CAPABILITIES[route],
        child_resume=child_resume,
        cancel_fn=cancel_fn,
        session_store=session_store,
    )


__all__ = [
    "CancelResult",
    "ExecutorAdapter",
    "ExecutorCapabilities",
    "ExecutorRegistry",
    "adapter_for",
    "attempt_id_for",
    "continuation_id_for",
    "normalize_outcome",
    "route_supports_resume_after_children",
    "turn_id_for",
    "unsupported_resume",
]
