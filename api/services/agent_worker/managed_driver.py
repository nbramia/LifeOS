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


# Terminal session statuses returned by `GET /v1/sessions/{id}`. The Managed
# Agents API reports `"idle"` when the agent has finished and has nothing more
# to do — that's the canonical success terminal. We keep `"completed"` in the
# set too as a synthesized alias (from `session.status_idle` events when the
# session.status field hasn't transitioned yet — observed in live testing).
TERMINAL_REMOTE_STATUSES = frozenset({
    "idle",
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
        self._raise_for_status_with_body(resp)
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
        """Poll a session's current state — status + cumulative usage + new events.

        Live API responses (confirmed 2026-05-26) split the data across two endpoints:
          - `GET /v1/sessions/{id}` returns `{status, usage, ...}` but NOT events.
            The `status` field reports `"running"` until the agent finishes, then
            transitions to `"idle"` (Anthropic's terminal-success value).
          - `GET /v1/sessions/{id}/events` returns `{data: [...], first_id,
            last_id, has_more}` — the canonical event stream. `agent.message`,
            `session.error`, `session.status_idle`, etc. all live here.

        This method calls both and merges. `since_event_id` is the cursor against
        the events endpoint. The status response also carries a 404 → cancelled
        signal (DELETEd sessions return 404 on subsequent polls).
        """
        # 1. Session status + usage
        resp = self._client.get(
            f"{self.base_url}/sessions/{session_id}",
            headers=self._headers(),
        )
        # Treat 404 as "cancelled" — that's how a DELETEd session appears on
        # subsequent polls.
        if resp.status_code == 404:
            return ManagedSessionState(
                session_id=session_id,
                status="cancelled",
                error_reason="session not found (likely deleted)",
            )
        self._raise_for_status_with_body(resp)
        data = resp.json()
        usage = data.get("usage", {}) or {}
        raw_status = str(data.get("status", "running")).lower()

        # 2. New events since cursor (separate endpoint)
        events = self.list_events(session_id, after_id=since_event_id)
        last_event_id: str | None = None
        if events:
            last_event_id = events[-1].get("id")

        # Resolve final status. A `session.error` in the batch must win over
        # a raw `idle`, otherwise the session reports completed and the
        # cascading failure is silently lost. `_synthesize_status_from_events`
        # returns "failed" if ANY error event is present, else "completed"
        # if any idle event, else None.
        synthesized = _synthesize_status_from_events(events)
        if synthesized == "failed":
            status = "failed"
        elif raw_status in TERMINAL_REMOTE_STATUSES:
            status = raw_status
        elif synthesized:
            status = synthesized
        else:
            status = raw_status

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

    # Hard cap on pagination loop in `list_events` — protects against a
    # misbehaving API or runaway session producing millions of events. 1000
    # pages × the API's page size is far more than any real session will
    # ever generate; if we hit this we'd rather drop and warn than hang.
    _MAX_EVENTS_PAGES = 1000

    def list_events(
        self,
        session_id: str,
        after_id: str | None = None,
    ) -> list[dict]:
        """Fetch all new session events since `after_id` (or all if None).

        `GET /v1/sessions/{id}/events?after=<cursor>` returns
        `{"data": [{"id", "type", "payload"|"content"|"error", ...}, ...],
          "first_id", "last_id", "has_more"}`. Loops while `has_more=True`
        so the caller never silently loses the tail of a paginated batch —
        without this, a session that produced more events than the API page
        size between polls would have its terminal `agent.message` dropped
        and the Telegram completion summary would be wrong.

        Returns the raw event list (concatenated across pages); callers
        (e.g. `get_session_state`) handle the cursor advance and synthesis.
        """
        all_events: list[dict] = []
        cursor = after_id
        for _ in range(self._MAX_EVENTS_PAGES):
            params: dict[str, str] = {}
            if cursor:
                params[self.EVENT_CURSOR_PARAM] = cursor
            resp = self._client.get(
                f"{self.base_url}/sessions/{session_id}/events",
                headers=self._headers(),
                params=params,
            )
            if resp.status_code == 404:
                # Session deleted mid-poll. Caller's status fetch already
                # turned this into "cancelled"; return what we have.
                return all_events
            self._raise_for_status_with_body(resp)
            data = resp.json()
            page = data.get("data", []) or []
            all_events.extend(page)
            if not data.get("has_more"):
                return all_events
            if not page:
                # Defensive: has_more=True but empty page would loop forever.
                logger.warning("list_events: has_more=true with empty page; stopping")
                return all_events
            cursor = page[-1].get("id")
            if not cursor:
                logger.warning("list_events: has_more=true but last event has no id; stopping")
                return all_events
        logger.warning(
            "list_events: hit pagination cap (%d pages) for session %s",
            self._MAX_EVENTS_PAGES, session_id,
        )
        return all_events

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
        self._raise_for_status_with_body(resp)

    @staticmethod
    def _raise_for_status_with_body(resp: httpx.Response) -> None:
        """Like `resp.raise_for_status()` but logs the response body for 4xx
        errors before raising. The default httpx behavior hides the body, which
        makes beta-API schema mismatches opaque to operators.

        Safe to log: response bodies for 4xx errors carry the API's validation
        message (e.g. "MCP server host(s) blocked by environment network policy").
        API keys live in REQUEST headers, not in response bodies.
        """
        if resp.is_success:
            return
        if 400 <= resp.status_code < 500:
            # Truncate to avoid unbounded log volume from a misbehaving API.
            body_preview = (resp.text or "")[:2048]
            # `resp.request` is set by real transports including MockTransport,
            # but defensively guard against hand-constructed Response objects.
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "?") if req is not None else "?"
            path = getattr(getattr(req, "url", None), "path", "?") if req is not None else "?"
            logger.warning(
                "Managed Agents %s %s → %d: %s",
                method, path, resp.status_code, body_preview,
            )
        resp.raise_for_status()

    def kill_session(self, session_id: str, reason: str = "") -> None:
        """Terminate a session early — used for budget breach / cascade-kill.

        Best-effort: errors are logged but never raised, because callers use
        this for cleanup paths where re-raising would mask the original cause.
        4xx response bodies are still surfaced via the standard helper before
        the exception is swallowed.
        """
        try:
            resp = self._client.delete(
                f"{self.base_url}/sessions/{session_id}",
                headers=self._headers(),
            )
            self._raise_for_status_with_body(resp)
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
