"""HTTP wrapper for Anthropic Managed Agents — the Claude-routed execution path.

Managed Agents (https://platform.claude.com/docs/en/managed-agents/overview,
launched April 2026) hosts the agent loop on Anthropic's infrastructure. The
**Agent** preset (model, system prompt, MCP servers, tools, skills) and
**Environment** (where tool calls execute) are configured once in the
Anthropic console and referenced by ID. Per-task, we just create a Session
that points at an agent + environment, then push the task description as
the initial user message.

This module is intentionally minimal:
- Polling-based state consumption (not SSE) — simpler to drive from the
  single-threaded worker loop, easier to test, recoverable across restarts.
- All HTTP calls go through an injectable `httpx.Client` so tests use
  `MockTransport` and never hit the live API.
- Required headers split: `anthropic-version: 2023-06-01` AND
  `anthropic-beta: managed-agents-2026-04-01`. The beta header is its own
  header (it is not folded into the version header).

API surface (verbatim from the Managed Agents docs):
- `POST /v1/sessions` — body: `{agent, environment_id, vault_ids, metadata?, title?}`,
  returns `{id, ...}`.
- `GET  /v1/sessions/{id}` — returns current session state (status + cumulative usage
  + recent events; events are the primary terminal-state signal).
- `POST /v1/sessions/{id}/events` — body: `{events: [{type: "user.message", content: [{type: "text", text}]}]}`.
  Used to post the initial user message and any follow-up turns (e.g. Telegram clarification answers).
- `DELETE /v1/sessions/{id}` — terminate a session early (budget breach / cascade-kill).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from api.services.agent_worker.pricing import (
    MANAGED_SESSION_HOUR_OVERHEAD,
    cost_for,
)


logger = logging.getLogger(__name__)


# Terminal session statuses synthesized by `ManagedAgentsDriver.get_session_state`
# from the event stream. The Managed Agents docs only document event types
# explicitly (session.status_idle = success, session.error = failure). We map
# those to a stable status string here so the executor can compare against a
# fixed set regardless of how the API evolves.
TERMINAL_REMOTE_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "budget_exceeded",
})


@dataclass
class ManagedSessionState:
    """Materialized view of a remote Managed Agents session."""
    session_id: str
    status: str
    last_event_id: str | None = None
    new_events: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    final_text: str | None = None
    error_reason: str | None = None


class ManagedAgentsDriver:
    """Minimal HTTP wrapper for the Managed Agents control plane.

    The driver is stateless apart from the HTTP client + auth headers. The
    operator manages the **agent preset** and **environment** in the Anthropic
    console; the worker passes those IDs through on each session create.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
    DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_ANTHROPIC_BETA = "managed-agents-2026-04-01"
    # Query-param key for the event-stream cursor on GET /sessions/{id}. Doc
    # terminology has varied between SDK previews ("after" vs "since"); kept
    # as a class constant so future API renames are a one-line change.
    EVENT_CURSOR_PARAM = "after"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        anthropic_beta: str = DEFAULT_ANTHROPIC_BETA,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("ManagedAgentsDriver requires an api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.anthropic_version = anthropic_version
        self.anthropic_beta = anthropic_beta
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "anthropic-beta": self.anthropic_beta,
            "content-type": "application/json",
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        initial_message: str | None = None,
        vault_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> str:
        """Create a managed session and return its remote session_id.

        Body shape (per https://platform.claude.com/docs/en/managed-agents/sessions):
            {
              "agent": "agent_…",
              "environment_id": "env_…",
              "vault_ids": ["vlt_…"],   # optional but required for OAuth MCPs
              "metadata": {...},        # optional, surfaced on the session
              "title": "..."            # optional, human-readable
            }

        If `initial_message` is provided, it's posted as a follow-up user event
        immediately after session creation, so the agent starts work without a
        second round trip from the worker's perspective.
        """
        body: dict[str, Any] = {
            "agent": agent_id,
            "environment_id": environment_id,
        }
        if vault_ids:
            body["vault_ids"] = list(vault_ids)
        if metadata:
            body["metadata"] = metadata
        if title:
            body["title"] = title

        resp = self._client.post(
            f"{self.base_url}/sessions",
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        sid = data.get("id") or data.get("session_id")
        if not sid:
            raise RuntimeError(f"Managed Agents response missing session id: {data!r}")

        if initial_message:
            self.post_user_message(sid, initial_message)

        return sid

    def get_session_state(
        self,
        session_id: str,
        since_event_id: str | None = None,
    ) -> ManagedSessionState:
        """Poll a session's current state + any events since `since_event_id`.

        Aggregates events into a stable status string. Per the Managed Agents
        event docs, terminal signals come via events:
          - `session.status_idle` → agent finished; status="completed"
          - `session.error` → fatal; status="failed"
        The status field on the response is forwarded directly when it falls
        in `TERMINAL_REMOTE_STATUSES`; otherwise we synthesize from events.
        """
        params: dict[str, str] = {}
        if since_event_id:
            params[self.EVENT_CURSOR_PARAM] = since_event_id
        resp = self._client.get(
            f"{self.base_url}/sessions/{session_id}",
            headers=self._headers(),
            params=params,
        )
        # Treat 404 as "cancelled" — that's how a DELETEd session appears on
        # subsequent polls.
        if resp.status_code == 404:
            return ManagedSessionState(
                session_id=session_id,
                status="cancelled",
                error_reason="session not found (likely deleted)",
            )
        resp.raise_for_status()
        data = resp.json()

        events = data.get("events", []) or []
        usage = data.get("usage", {}) or {}
        last_event_id: str | None = None
        if events:
            last_event_id = events[-1].get("id")

        # Synthesize a terminal status from events when the response doesn't
        # provide a definitive one.
        raw_status = str(data.get("status", "running")).lower()
        synthesized = _synthesize_status_from_events(events)
        status = synthesized if synthesized else raw_status

        # Concatenate text content from agent.message events as `final_text`.
        # The most recent agent.message before an idle event is "the answer".
        final_text = _extract_final_text(events)

        # Surface error message from session.error events.
        error_reason = _extract_error_reason(events)

        return ManagedSessionState(
            session_id=session_id,
            status=status,
            last_event_id=last_event_id,
            new_events=events,
            total_input_tokens=int(usage.get("input_tokens", 0)),
            total_output_tokens=int(usage.get("output_tokens", 0)),
            final_text=final_text,
            error_reason=error_reason,
        )

    def post_user_message(self, session_id: str, content: str) -> None:
        """Post a user turn to a running session.

        Used to send the initial task description and to resume the session
        after a Telegram clarification reply.
        """
        body = {
            "events": [
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": content}],
                },
            ],
        }
        resp = self._client.post(
            f"{self.base_url}/sessions/{session_id}/events",
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()

    def kill_session(self, session_id: str, reason: str = "") -> None:
        """Terminate a session early — used for budget breach / cascade-kill.

        Best-effort: errors are logged but never raised, because callers use
        this for cleanup paths where re-raising would mask the original cause.
        """
        try:
            self._client.delete(
                f"{self.base_url}/sessions/{session_id}",
                headers=self._headers(),
            )
        except Exception as exc:
            logger.warning("kill_session %s failed: %s (reason=%s)", session_id, exc, reason)


# ---------------------------------------------------------------------------
# Event-stream parsing helpers
# ---------------------------------------------------------------------------

def _synthesize_status_from_events(events: list[dict]) -> str | None:
    """Map event-stream signals to a terminal status string, or None.

    Scans the full batch before deciding so we prefer `"failed"` when both
    `session.error` and `session.status_idle` appear together — error is the
    actionable signal for operators. Returning `"completed"` on first idle
    (the prior behavior) silently swallowed cascading errors.
    """
    has_idle = False
    has_error = False
    for ev in events:
        et = ev.get("type", "")
        if et == "session.status_idle":
            has_idle = True
        elif et == "session.error":
            has_error = True
    if has_error:
        return "failed"
    if has_idle:
        return "completed"
    return None


def _extract_final_text(events: list[dict]) -> str | None:
    """Concatenate text from the most recent agent.message event.

    Older messages are intermediate "thinking out loud" turns; the last
    `agent.message` before idle is the final answer the user sees.
    """
    latest: list[dict] | None = None
    for ev in events:
        if ev.get("type") == "agent.message":
            content = ev.get("content")
            if isinstance(content, list):
                latest = content
    if latest is None:
        return None
    parts: list[str] = []
    for block in latest:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
    return "".join(parts) if parts else None


def _extract_error_reason(events: list[dict]) -> str | None:
    """Pull the message from the first session.error event, if any."""
    for ev in events:
        if ev.get("type") == "session.error":
            payload = ev.get("payload") or ev.get("data") or {}
            if isinstance(payload, dict):
                msg = payload.get("message") or payload.get("error") or ""
                if msg:
                    return str(msg)
    return None


# ---------------------------------------------------------------------------
# Cost helper
# ---------------------------------------------------------------------------

def managed_session_cost(
    model: str,
    tokens_in: int,
    tokens_out: int,
    wall_seconds: float,
) -> float:
    """Total $ for a managed session: token cost + session-hour overhead."""
    tokens = cost_for(model, tokens_in, tokens_out)
    overhead = (wall_seconds / 3600.0) * MANAGED_SESSION_HOUR_OVERHEAD
    return tokens + overhead
