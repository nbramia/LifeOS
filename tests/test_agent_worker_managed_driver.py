"""Tests for ManagedAgentsDriver — HTTP request shape + parsing.

These don't hit the real Managed Agents API; we wire `httpx.MockTransport`
that asserts the request shape and returns canned responses. Verifies the
client matches the documented schema at
https://platform.claude.com/docs/en/managed-agents.

**Operator should still smoke-test against a real account** because the API
is in beta and the schema may shift.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from api.services.agent_worker.managed_driver import (
    ManagedAgentsDriver,
    managed_session_cost,
)
from api.services.agent_worker.pricing import MANAGED_SESSION_HOUR_OVERHEAD


def _build_driver(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://test")
    return ManagedAgentsDriver(
        api_key="sk-test",
        base_url="http://test/v1",
        http_client=client,
    )


def _make_session_handler(*, status_body=None, events=None, status_code=200, events_code=200):
    """Build a handler that routes GET /sessions/{id} to a status response and
    GET /sessions/{id}/events to an events response. POST routes return 200 OK.

    Mirrors the live API surface where state lives at two endpoints. Helper
    keeps each test focused on what it's actually asserting.
    """
    status_body = status_body if status_body is not None else {"status": "running", "usage": {}}
    events = events if events is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/events"):
            return httpx.Response(events_code, json={"data": events, "has_more": False})
        if request.method == "GET":
            return httpx.Response(status_code, json=status_body)
        return httpx.Response(200, json={"ok": True})

    return handler


# ---------------------------------------------------------------------------
# Auth + headers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_driver_requires_api_key():
    with pytest.raises(ValueError):
        ManagedAgentsDriver(api_key="")


@pytest.mark.unit
def test_create_session_sends_required_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": "sess_x"})

    _build_driver(handler).create_session(
        agent_id="agent_x", environment_id="env_y",
    )
    h = captured["headers"]
    assert h["x-api-key"] == "sk-test"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["anthropic-beta"] == "managed-agents-2026-04-01"
    assert h["content-type"] == "application/json"


@pytest.mark.unit
def test_default_base_url_points_at_api_anthropic():
    d = ManagedAgentsDriver(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "x"}))))
    assert d.base_url == "https://api.anthropic.com/v1"


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_create_session_request_shape_and_returns_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sess_abc"})

    sid = _build_driver(handler).create_session(
        agent_id="agent_19QrftoZ",
        environment_id="env_42",
        vault_ids=["vlt_xyz"],
        metadata={"lifeos_session_id": "sess_local"},
        title="run smoke task",
    )
    assert sid == "sess_abc"
    assert captured["url"].endswith("/sessions")
    body = captured["body"]
    assert body["agent"] == "agent_19QrftoZ"
    assert body["environment_id"] == "env_42"
    assert body["vault_ids"] == ["vlt_xyz"]
    assert body["metadata"] == {"lifeos_session_id": "sess_local"}
    assert body["title"] == "run smoke task"
    # The new schema has NO inline system_prompt / mcp_servers / connectors / budget;
    # all of those live on the agent preset now.
    assert "system_prompt" not in body
    assert "mcp_servers" not in body
    assert "connectors" not in body
    assert "max_tokens" not in body


@pytest.mark.unit
def test_create_session_omits_optional_fields_when_empty():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sess"})

    _build_driver(handler).create_session(
        agent_id="agent_x", environment_id="env_y",
    )
    body = captured["body"]
    assert "vault_ids" not in body
    assert "metadata" not in body
    assert "title" not in body


@pytest.mark.unit
def test_create_session_accepts_session_id_alias_too():
    """The API returns `id`, but some SDK variants return `session_id`. Accept both."""
    def handler(request):
        return httpx.Response(200, json={"session_id": "sess_aliased"})

    sid = _build_driver(handler).create_session(
        agent_id="agent_x", environment_id="env_y",
    )
    assert sid == "sess_aliased"


@pytest.mark.unit
def test_create_session_raises_when_id_missing():
    def handler(request):
        return httpx.Response(200, json={"foo": "bar"})

    with pytest.raises(RuntimeError, match="missing session id"):
        _build_driver(handler).create_session(
            agent_id="agent_x", environment_id="env_y",
        )


@pytest.mark.unit
def test_create_session_raises_on_http_error():
    def handler(request):
        return httpx.Response(500, text="server error")

    with pytest.raises(httpx.HTTPStatusError):
        _build_driver(handler).create_session(
            agent_id="agent_x", environment_id="env_y",
        )


@pytest.mark.unit
def test_create_session_posts_initial_message_when_provided():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content)))
        if str(request.url).endswith("/sessions"):
            return httpx.Response(200, json={"id": "sess_z"})
        return httpx.Response(200, json={"ok": True})

    sid = _build_driver(handler).create_session(
        agent_id="agent_x", environment_id="env_y",
        initial_message="please do the thing",
    )
    assert sid == "sess_z"
    assert len(calls) == 2
    # First call = create
    assert calls[0][1].endswith("/sessions")
    # Second call = events with user.message
    method, url, body = calls[1]
    assert method == "POST"
    assert url.endswith("/sessions/sess_z/events")
    assert body == {
        "events": [{"type": "user.message", "content": [{"type": "text", "text": "please do the thing"}]}],
    }


# ---------------------------------------------------------------------------
# get_session_state
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_session_state_calls_both_endpoints_and_merges():
    """Status comes from GET /sessions/{id}; events come from GET /sessions/{id}/events.
    Driver fans out to both and merges into one ManagedSessionState."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"data": [
                {"id": "evt_1", "type": "agent.message",
                 "content": [{"type": "text", "text": "hi"}]},
                {"id": "evt_2", "type": "agent.tool_use", "payload": {"name": "Bash"}},
            ]})
        return httpx.Response(200, json={
            "id": "sess_abc", "status": "running",
            "usage": {"input_tokens": 100, "output_tokens": 40},
        })

    state = _build_driver(handler).get_session_state("sess_abc")
    # Both endpoints hit
    assert any(p.endswith("/sessions/sess_abc") for p in calls)
    assert any(p.endswith("/sessions/sess_abc/events") for p in calls)
    # Status from /sessions/{id}, events from /sessions/{id}/events — merged
    assert state.session_id == "sess_abc"
    assert state.status == "running"
    assert state.last_event_id == "evt_2"
    assert len(state.new_events) == 2
    assert state.total_input_tokens == 100
    assert state.total_output_tokens == 40


@pytest.mark.unit
def test_get_session_state_idle_status_is_terminal():
    """The Managed Agents API reports `status=idle` when the agent is done.
    Driver forwards that verbatim (no synthesis needed) — it's already in
    TERMINAL_REMOTE_STATUSES."""
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {"input_tokens": 3, "output_tokens": 42}},
        events=[],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "idle"


@pytest.mark.unit
def test_get_session_state_filters_events_by_id_client_side():
    """The events endpoint doesn't accept an event-id cursor (verified live —
    the API returns a 400 listing valid params as created_at[gt|gte|lt|lte],
    limit, order, page, types[]). Per the docs' reconnect pattern, we list
    all events with order=asc and filter client-side by id > since_event_id."""
    captured = {"events_params": None, "status_params": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            captured["events_params"] = dict(request.url.params)
            # Return events with ULID-like ids — strictly sortable by id.
            return httpx.Response(200, json={"data": [
                {"id": "sevt_001", "type": "x"},
                {"id": "sevt_007", "type": "y"},  # this one is at the cursor
                {"id": "sevt_009", "type": "z"},  # only this should survive
            ]})
        captured["status_params"] = dict(request.url.params)
        return httpx.Response(200, json={"status": "running", "usage": {}})

    state = _build_driver(handler).get_session_state("sess_abc", since_event_id="sevt_007")
    # The /events call uses `order=asc` and NO `after` param (which the API rejects).
    assert captured["events_params"].get("order") == "asc"
    assert "after" not in captured["events_params"]
    # Client-side filter: only events strictly greater than the cursor id.
    assert [e["id"] for e in state.new_events] == ["sevt_009"]
    # Status endpoint never gets the cursor — it's an events-only concept.
    assert captured["status_params"] == {}


@pytest.mark.unit
def test_get_session_state_synthesizes_completed_from_idle_event():
    """When status field hasn't transitioned yet but an idle event arrived,
    we synthesize completed."""
    handler = _make_session_handler(
        status_body={"status": "running", "usage": {"input_tokens": 50, "output_tokens": 80}},
        events=[
            {"id": "evt_1", "type": "agent.message",
             "content": [{"type": "text", "text": "all done!"}]},
            {"id": "evt_2", "type": "session.status_idle"},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "completed"
    assert state.final_text == "all done!"
    assert state.total_output_tokens == 80


@pytest.mark.unit
def test_get_session_state_synthesizes_failed_from_error_event():
    handler = _make_session_handler(
        status_body={"status": "running", "usage": {}},
        events=[
            {"id": "evt_1", "type": "session.error",
             "payload": {"message": "MCP server unreachable"}},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "MCP server unreachable"


@pytest.mark.unit
def test_get_session_state_completes_when_only_mcp_init_errors_with_idle():
    """MCP init errors (`mcp_authentication_failed_error`,
    `mcp_connection_failed_error`) are informational — they fire when a connector
    is unreachable at session-start. The agent works around the missing MCP and
    finishes cleanly using the others. Session must NOT be marked failed."""
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {"input_tokens": 3, "output_tokens": 89}},
        events=[
            {"id": "evt_1", "type": "session.error",
             "error": {"type": "mcp_connection_failed_error",
                       "mcp_server_name": "gmail",
                       "message": "server URL not found"}},
            {"id": "evt_2", "type": "session.error",
             "error": {"type": "mcp_authentication_failed_error",
                       "mcp_server_name": "gcal",
                       "message": "no credential stored"}},
            {"id": "evt_3", "type": "agent.mcp_tool_use"},
            {"id": "evt_4", "type": "session.status_idle"},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "idle"  # raw status forwarded, not overridden by errors
    assert state.init_failed_mcps == ["gmail", "gcal"]


@pytest.mark.unit
def test_get_session_state_still_fails_on_non_init_session_error():
    """Non-MCP-init errors (runtime tool crashes, lifecycle failures, etc.)
    still trigger terminal-failed. Cascading-failure detection from the prior
    fix is preserved for the cases that actually need it."""
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {}},
        events=[
            {"id": "evt_1", "type": "session.error",
             "error": {"type": "tool_dispatch_error",
                       "message": "the agent's bash tool crashed"}},
            {"id": "evt_2", "type": "session.status_idle"},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "failed"
    # tool_dispatch_error is NOT an init failure type — not classified as such.
    assert state.init_failed_mcps == []


@pytest.mark.unit
def test_get_session_state_dedupes_init_failed_mcp_names():
    """Same MCP firing two init errors (e.g., retry exhausted) only shows once."""
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {}},
        events=[
            {"id": "evt_1", "type": "session.error",
             "error": {"type": "mcp_connection_failed_error", "mcp_server_name": "gmail"}},
            {"id": "evt_2", "type": "session.error",
             "error": {"type": "mcp_connection_failed_error", "mcp_server_name": "gmail"}},
            {"id": "evt_3", "type": "session.status_idle"},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.init_failed_mcps == ["gmail"]


@pytest.mark.unit
def test_get_session_state_prefers_failed_when_raw_idle_and_event_error():
    """Regression guard: when the status endpoint reports raw `"idle"` and the
    events endpoint contains a `session.error`, the error must win. Otherwise
    a cascading failure resolves to STATUS_COMPLETED and the reason is lost."""
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {}},
        events=[
            {"id": "evt_1", "type": "session.error",
             "payload": {"message": "mcp init blew up after idle"}},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "mcp init blew up after idle"


@pytest.mark.unit
def test_list_events_paginates_through_has_more():
    """list_events must follow `has_more=true` and concatenate pages, otherwise
    a session that produced more events than the API page size between polls
    would lose the tail (including the terminal agent.message). Pagination
    uses the `page=` query param (the only id-based cursor Anthropic accepts
    is rejected; see test_get_session_state_filters_events_by_id_client_side)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        page = request.url.params.get("page")
        if page is None:
            return httpx.Response(200, json={
                "data": [{"id": "sevt_001", "type": "x"}, {"id": "sevt_002", "type": "y"}],
                "has_more": True,
            })
        if page == "2":
            return httpx.Response(200, json={
                "data": [{"id": "sevt_003", "type": "z"},
                         {"id": "sevt_004", "type": "agent.message",
                          "content": [{"type": "text", "text": "done"}]}],
                "has_more": False,
            })
        raise AssertionError(f"unexpected page: {page}")

    events = _build_driver(handler).list_events("sess")
    assert [e["id"] for e in events] == ["sevt_001", "sevt_002", "sevt_003", "sevt_004"]
    # First call: page=1 implicit (no page param); second: page=2.
    # Both calls also include order=asc.
    assert calls[0] == {"order": "asc"}
    assert calls[1] == {"order": "asc", "page": "2"}


@pytest.mark.unit
def test_list_events_stops_on_empty_page_with_has_more_to_prevent_infinite_loop():
    """Defensive: a misbehaving API that returns `has_more=true` with an empty
    `data` array must not loop forever."""
    def handler(request):
        return httpx.Response(200, json={"data": [], "has_more": True})

    # Should return [] and log a warning, not loop indefinitely.
    events = _build_driver(handler).list_events("sess")
    assert events == []


@pytest.mark.unit
def test_get_session_state_prefers_failed_when_idle_and_error_in_same_batch():
    """If both `session.status_idle` and `session.error` arrive in one poll
    batch, error wins — operator needs the actionable signal. Order in the
    batch doesn't matter."""
    handler_idle_first = _make_session_handler(
        events=[
            {"id": "evt_1", "type": "session.status_idle"},
            {"id": "evt_2", "type": "session.error",
             "payload": {"message": "post-idle cascade failure"}},
        ],
    )
    state = _build_driver(handler_idle_first).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "post-idle cascade failure"

    handler_error_first = _make_session_handler(
        events=[
            {"id": "evt_1", "type": "session.error",
             "payload": {"message": "pre-idle failure"}},
            {"id": "evt_2", "type": "session.status_idle"},
        ],
    )
    state = _build_driver(handler_error_first).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "pre-idle failure"


@pytest.mark.unit
def test_get_session_state_concatenates_text_blocks_from_latest_message():
    handler = _make_session_handler(
        status_body={"status": "idle", "usage": {}},
        events=[
            {"id": "evt_1", "type": "agent.message",
             "content": [{"type": "text", "text": "draft 1"}]},
            {"id": "evt_2", "type": "agent.message",
             "content": [
                 {"type": "text", "text": "final "},
                 {"type": "text", "text": "answer"},
             ]},
        ],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.final_text == "final answer"


@pytest.mark.unit
def test_get_session_state_treats_404_as_cancelled():
    """A session that was DELETEd returns 404 on subsequent polls — we map to cancelled."""
    def handler(request):
        return httpx.Response(404, text="not found")

    state = _build_driver(handler).get_session_state("sess_gone")
    assert state.status == "cancelled"
    assert "not found" in (state.error_reason or "")


@pytest.mark.unit
def test_list_events_returns_empty_on_404():
    """Race: session deleted between status fetch and events fetch.
    list_events returns [] so the caller doesn't crash; the caller's
    parallel 404 on /sessions/{id} already produced the "cancelled" state."""
    def handler(request):
        return httpx.Response(404, text="not found")

    events = _build_driver(handler).list_events("sess_gone", after_id="evt_x")
    assert events == []


@pytest.mark.unit
def test_get_session_state_no_events_no_final_text():
    handler = _make_session_handler(events=[])
    state = _build_driver(handler).get_session_state("sess")
    assert state.final_text is None
    assert state.error_reason is None
    assert state.last_event_id is None


# ---------------------------------------------------------------------------
# post_user_message
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_post_user_message_request_shape():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    _build_driver(handler).post_user_message("sess_abc", "and here is the answer")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/sessions/sess_abc/events")
    assert captured["body"] == {
        "events": [{
            "type": "user.message",
            "content": [{"type": "text", "text": "and here is the answer"}],
        }],
    }


@pytest.mark.unit
def test_post_user_message_raises_on_http_error():
    def handler(request):
        return httpx.Response(400, text="bad")

    with pytest.raises(httpx.HTTPStatusError):
        _build_driver(handler).post_user_message("sess", "x")


@pytest.mark.unit
def test_4xx_response_body_is_logged_before_raising(caplog):
    """Operators need to see the API's validation message ("MCP server host(s)
    blocked by environment network policy", etc.). Default httpx behavior hides
    the body. We log it at WARNING with the response status and path."""
    def handler(request):
        return httpx.Response(400, json={
            "error": {"message": "MCP server host(s) blocked by environment network policy"},
        })

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.HTTPStatusError):
            _build_driver(handler).create_session(
                agent_id="agent_x", environment_id="env_y",
            )

    # Body keyword appears in the warning log
    assert any("MCP server host(s) blocked" in record.getMessage() for record in caplog.records)
    assert any("400" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# kill_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_kill_session_uses_delete_on_sessions_path():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    _build_driver(handler).kill_session("sess", reason="budget")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/sessions/sess")


@pytest.mark.unit
def test_kill_session_swallows_errors():
    """kill_session is best-effort; raising would mask the original failure that triggered it."""
    def boom(request):
        raise httpx.ConnectError("network down")

    # Must not raise.
    _build_driver(boom).kill_session("sess")


# ---------------------------------------------------------------------------
# Cost helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_update_session_sends_post_with_agent_payload():
    """`update_session` POSTs the agent config under `agent` to the session
    URL — full-replacement semantics per the Managed Agents docs."""
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sess_1"})

    driver = _build_driver(handler)
    driver.update_session("sess_1", {"tools": ["lifeos_search", "lifeos_ask"]})

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/sessions/sess_1")
    assert captured["body"] == {
        "agent": {"tools": ["lifeos_search", "lifeos_ask"]}
    }


@pytest.mark.unit
def test_update_session_4xx_surfaces_response_body(caplog):
    """A 4xx response logs the body so beta-API schema mismatches are
    visible rather than swallowed."""
    def handler(request):
        return httpx.Response(
            400, json={"error": "tools[3] is not a valid identifier"}
        )

    driver = _build_driver(handler)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.HTTPStatusError):
            driver.update_session("sess_1", {"tools": ["bogus tool"]})
    body_logged = any(
        "not a valid identifier" in rec.getMessage() for rec in caplog.records
    )
    assert body_logged, "expected 4xx body to be surfaced in WARNING logs"


@pytest.mark.unit
def test_managed_session_cost_includes_overhead():
    cost = managed_session_cost("claude-opus-4-7", 1000, 1000, wall_seconds=3600)
    # 1k input ($0.015) + 1k output ($0.075) + 1 hr * $0.08
    expected = 0.015 + 0.075 + MANAGED_SESSION_HOUR_OVERHEAD
    assert cost == pytest.approx(expected)


@pytest.mark.unit
def test_managed_session_cost_partial_hour():
    # 30 minutes of session-hour overhead.
    cost = managed_session_cost("local", 0, 0, wall_seconds=1800)
    assert cost == pytest.approx(0.5 * MANAGED_SESSION_HOUR_OVERHEAD)


# ---------------------------------------------------------------------------
# Prompt-cache token buckets (#137)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_session_state_parses_cache_creation_and_cache_read_tokens():
    """The four-bucket usage payload from the live API must flow through
    `get_session_state` into the materialized `ManagedSessionState`."""
    handler = _make_session_handler(
        status_body={
            "status": "running",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 81,
                "cache_creation_input_tokens": 109_075,
                "cache_read_input_tokens": 2_000,
            },
        },
        events=[],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.total_input_tokens == 3
    assert state.total_output_tokens == 81
    assert state.total_cache_creation_tokens == 109_075
    assert state.total_cache_read_tokens == 2_000


@pytest.mark.unit
def test_session_state_defaults_cache_buckets_to_zero_when_absent():
    """Older API responses omit the cache buckets — should default to zero."""
    handler = _make_session_handler(
        status_body={"status": "running", "usage": {"input_tokens": 5, "output_tokens": 7}},
        events=[],
    )
    state = _build_driver(handler).get_session_state("sess")
    assert state.total_cache_creation_tokens == 0
    assert state.total_cache_read_tokens == 0


@pytest.mark.unit
def test_managed_session_cost_includes_cache_buckets():
    """cache_creation tokens are 1.25× input rate; cache_read is 0.10× input."""
    # 100k cache_creation at Sonnet ($3/M input × 1.25) = $0.375
    # 50k cache_read at Sonnet ($3/M input × 0.10) = $0.015
    # 1000 input + 1000 output = $0.003 + $0.015 = $0.018
    # No wall time → no overhead.
    cost = managed_session_cost(
        "claude-sonnet-4-6",
        tokens_in=1000,
        tokens_out=1000,
        wall_seconds=0,
        cache_creation_tokens=100_000,
        cache_read_tokens=50_000,
    )
    expected = 0.018 + 0.375 + 0.015
    assert cost == pytest.approx(expected)
