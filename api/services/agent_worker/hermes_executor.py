"""Drives one Hermes turn from inside the agent worker (#851).

Mirrors the executor surface used by `ClaudeCodeExecutor`/`CodexExecutor`/
`LocalExecutor` — one `execute(session, task)` call, one `ExecutorOutcome`
— but the "subprocess" is a single synchronous HTTP round trip to the
configured Hermes backend, using the SAME request-building
(`_build_envelope`) and persistence (`_HermesTurnPersister`) the `/chat`
Hermes proxy uses, so a board-assigned Hermes turn's conversation and usage
rows are indistinguishable from one that came through `/chat`.

Runs synchronously on the worker's tick thread (unlike the CLI routes,
which run off-tick through the CLI dispatch pool) because a Hermes turn is
one bounded HTTP request/response, not a long-lived subprocess.
"""
from __future__ import annotations

import json
import logging
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
    ) -> None:
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._client_factory = http_client_factory or _default_client_factory
        self._persona_id = persona_id

    def execute(self, session, task: dict) -> ExecutorOutcome:
        sid = session.session_id
        prompt = self._build_prompt(task)
        if not prompt:
            self.transcript_store.append(sid, "hermes_no_prompt", {})
            return ExecutorOutcome(status=STATUS_FAILED, reason="empty prompt")

        if not settings.hermes_backend_url:
            self.transcript_store.append(sid, "hermes_not_configured", {})
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason="Hermes backend not configured (LIFEOS_HERMES_BACKEND_URL)",
            )

        raw_body = json.dumps({"question": prompt, "persona_id": self._persona_id}).encode("utf-8")
        try:
            envelope_body = _build_envelope(raw_body)
        except Exception as exc:  # noqa: BLE001 — _build_envelope raises HTTPException
            self.transcript_store.append(sid, "hermes_envelope_failed", {"error": str(exc)})
            return ExecutorOutcome(status=STATUS_FAILED, reason=f"envelope build failed: {exc}")

        self.session_store.update_status(session.task_id, STATUS_RUNNING)
        self.transcript_store.append(sid, "hermes_spawn", {"question_chars": len(prompt)})

        persister = _HermesTurnPersister(question=prompt, persona_id=self._persona_id)
        url = f"{settings.hermes_backend_url.rstrip('/')}/api/ask/stream"
        headers = {"Content-Type": "application/json"}
        if settings.hermes_backend_token:
            headers["Authorization"] = f"Bearer {settings.hermes_backend_token}"

        try:
            client = self._client_factory()
            try:
                with client.stream("POST", url, content=envelope_body, headers=headers) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes():
                        persister.observe(chunk)
            finally:
                # A factory-provided client (real or fake) may or may not
                # support context-manager close; best-effort only.
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        except Exception as exc:  # noqa: BLE001 — network/HTTP errors of every shape
            persister.finalize()
            self._record_reported_model(session, persister)
            self.transcript_store.append(sid, "hermes_request_failed", {"error": str(exc)})
            return ExecutorOutcome(status=STATUS_FAILED, reason=f"hermes request failed: {exc}")

        persister.finalize()
        self._record_reported_model(session, persister)

        conversation_id = persister.conversation_id
        final_text = persister.content_text.strip()

        if conversation_id:
            self.session_store.set_conversation_id(session.task_id, conversation_id)
        self.transcript_store.append(sid, "hermes_completed", {
            "conversation_id": conversation_id,
            "final_chars": len(final_text),
            "done_seen": persister.done_seen,
        })

        if not final_text:
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason="hermes turn produced no content" + (
                    "" if persister.done_seen else " (stream truncated before completion)"
                ),
            )

        return ExecutorOutcome(status=STATUS_COMPLETED, final_text=final_text)

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
            self.session_store.set_hermes_model(session.task_id, persister.reported_model)
        except Exception:  # noqa: BLE001 — never let a store failure fail the turn
            logger.warning(
                "hermes turn persistence: failed to record reported model for %s",
                session.task_id, exc_info=True,
            )

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
