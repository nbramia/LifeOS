"""Wrapper for Anthropic Managed Agents — the Claude-routed execution path.

Managed Agents (https://platform.claude.com/docs/en/managed-agents/overview,
launched April 2026) hosts the agent loop on Anthropic's infrastructure. The
worker posts a task as a session and polls events until terminal. Session
state, tool execution, and MCP tunneling all live server-side; we only see
the event stream + cost accounting.

This module is intentionally minimal:
- Polling-based event consumption (not SSE) — simpler to drive from the
  single-threaded worker loop, easier to test, recoverable across restarts.
- Endpoint shapes documented inline. **Operator must verify the API shape
  matches their account when first deploying** — Managed Agents is in beta
  and the schema may shift.
- All HTTP calls go through an injectable `httpx.Client` so tests use
  `MockTransport` and never hit the live API.
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


# Terminal session statuses returned by the Managed Agents API.
# Operator: verify these match the values your account actually emits.
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
    """Minimal HTTP wrapper for the Managed Agents control plane."""

    DEFAULT_BASE_URL = "https://platform.claude.com/v1/managed-agents"
    DEFAULT_VERSION_HEADER = "managed-agents-2026-04-01"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        version_header: str = DEFAULT_VERSION_HEADER,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        vault_id: str | None = None,
    ):
        if not api_key:
            raise ValueError("ManagedAgentsDriver requires an api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.version_header = version_header
        self.vault_id = vault_id
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.version_header,
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
        system_prompt: str,
        user_message: str,
        model: str,
        mcp_servers: list[dict] | None = None,
        connectors: list[str] | None = None,
        max_tokens: int | None = None,
        max_dollars: float | None = None,
        max_wall_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a managed session and return its remote session_id.

        Body shape (per docs.anthropic.com/managed-agents/quickstart):
            {
              "model": "claude-opus-4-7",
              "vault_id": "<vault_id>",
              "system_prompt": "...",
              "initial_message": "...",
              "mcp_servers": [{"name": ..., "url": ..., "headers": {...}}],
              "connectors": ["gmail", "google-calendar", ...],
              "max_tokens": 500000,
              "max_dollars": 5.0,
              "max_wall_seconds": 14400
            }
        """
        body: dict[str, Any] = {
            "model": model,
            "system_prompt": system_prompt,
            "initial_message": user_message,
        }
        if self.vault_id:
            body["vault_id"] = self.vault_id
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        if connectors:
            body["connectors"] = connectors
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if max_dollars is not None:
            body["max_dollars"] = float(max_dollars)
        if max_wall_seconds is not None:
            body["max_wall_seconds"] = int(max_wall_seconds)
        if metadata:
            body["metadata"] = metadata

        resp = self._client.post(
            f"{self.base_url}/sessions",
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        sid = data.get("session_id") or data.get("id")
        if not sid:
            raise RuntimeError(f"Managed Agents response missing session id: {data!r}")
        return sid

    def get_session_state(
        self,
        session_id: str,
        since_event_id: str | None = None,
    ) -> ManagedSessionState:
        """Poll a session's current state + any events since `since_event_id`.

        Response shape (per docs.anthropic.com/managed-agents/sessions):
            {
              "session_id": "...",
              "status": "running | completed | failed | cancelled | budget_exceeded",
              "events": [
                {"id": "evt_...", "type": "...", "payload": {...}, "ts": ...},
                ...
              ],
              "usage": {"input_tokens": int, "output_tokens": int},
              "final_text": "..." | null,
              "error": {"message": "..."} | null
            }
        """
        params = {}
        if since_event_id:
            params["since"] = since_event_id
        resp = self._client.get(
            f"{self.base_url}/sessions/{session_id}",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", []) or []
        usage = data.get("usage", {}) or {}
        error = data.get("error") or {}
        last_event_id: str | None = None
        if events:
            last_event_id = events[-1].get("id")
        return ManagedSessionState(
            session_id=session_id,
            status=data.get("status", "running"),
            last_event_id=last_event_id,
            new_events=events,
            total_input_tokens=int(usage.get("input_tokens", 0)),
            total_output_tokens=int(usage.get("output_tokens", 0)),
            final_text=data.get("final_text"),
            error_reason=str(error.get("message", "")) if error else None,
        )

    def post_user_message(self, session_id: str, content: str) -> None:
        """Resume a paused session with a user turn. Used in Issue F when
        the operator answers a clarifying question via Telegram.
        """
        resp = self._client.post(
            f"{self.base_url}/sessions/{session_id}/messages",
            headers=self._headers(),
            json={"role": "user", "content": content},
        )
        resp.raise_for_status()

    def kill_session(self, session_id: str, reason: str = "") -> None:
        """Terminate a session early — used for budget breach / cascade-kill."""
        try:
            self._client.delete(
                f"{self.base_url}/sessions/{session_id}",
                headers=self._headers(),
                params={"reason": reason} if reason else None,
            )
        except Exception as exc:
            logger.warning("kill_session %s failed: %s", session_id, exc)


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
