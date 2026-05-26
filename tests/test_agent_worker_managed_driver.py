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
def test_get_session_state_parses_basic_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/sessions/sess_abc")
        return httpx.Response(200, json={
            "id": "sess_abc",
            "status": "running",
            "events": [
                {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "hi"}]},
                {"id": "evt_2", "type": "agent.tool_use", "payload": {"name": "Bash"}},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 40},
        })

    state = _build_driver(handler).get_session_state("sess_abc")
    assert state.session_id == "sess_abc"
    assert state.status == "running"
    assert state.last_event_id == "evt_2"
    assert len(state.new_events) == 2
    assert state.total_input_tokens == 100
    assert state.total_output_tokens == 40


@pytest.mark.unit
def test_get_session_state_uses_after_cursor():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"status": "running", "events": [], "usage": {}})

    _build_driver(handler).get_session_state("sess_abc", since_event_id="evt_7")
    assert captured["params"]["after"] == "evt_7"


@pytest.mark.unit
def test_get_session_state_synthesizes_completed_from_idle_event():
    """When raw status doesn't say "completed", but an idle event arrived, we synthesize."""
    def handler(request):
        return httpx.Response(200, json={
            "status": "running",  # API hasn't transitioned status yet
            "events": [
                {"id": "evt_1", "type": "agent.message",
                 "content": [{"type": "text", "text": "all done!"}]},
                {"id": "evt_2", "type": "session.status_idle"},
            ],
            "usage": {"input_tokens": 50, "output_tokens": 80},
        })

    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "completed"
    assert state.final_text == "all done!"
    assert state.total_output_tokens == 80


@pytest.mark.unit
def test_get_session_state_synthesizes_failed_from_error_event():
    def handler(request):
        return httpx.Response(200, json={
            "status": "running",
            "events": [
                {"id": "evt_1", "type": "session.error",
                 "payload": {"message": "MCP server unreachable"}},
            ],
            "usage": {},
        })

    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "MCP server unreachable"


@pytest.mark.unit
def test_get_session_state_prefers_failed_when_idle_and_error_in_same_batch():
    """If both `session.status_idle` and `session.error` arrive in one poll
    batch, error wins — operator needs the actionable signal. Order in the
    batch doesn't matter."""
    def handler_idle_first(request):
        return httpx.Response(200, json={
            "status": "running",
            "events": [
                {"id": "evt_1", "type": "session.status_idle"},
                {"id": "evt_2", "type": "session.error",
                 "payload": {"message": "post-idle cascade failure"}},
            ],
            "usage": {},
        })

    state = _build_driver(handler_idle_first).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "post-idle cascade failure"

    def handler_error_first(request):
        return httpx.Response(200, json={
            "status": "running",
            "events": [
                {"id": "evt_1", "type": "session.error",
                 "payload": {"message": "pre-idle failure"}},
                {"id": "evt_2", "type": "session.status_idle"},
            ],
            "usage": {},
        })

    state = _build_driver(handler_error_first).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "pre-idle failure"


@pytest.mark.unit
def test_get_session_state_concatenates_text_blocks_from_latest_message():
    def handler(request):
        return httpx.Response(200, json={
            "status": "completed",
            "events": [
                {"id": "evt_1", "type": "agent.message",
                 "content": [{"type": "text", "text": "draft 1"}]},
                {"id": "evt_2", "type": "agent.message",
                 "content": [
                     {"type": "text", "text": "final "},
                     {"type": "text", "text": "answer"},
                 ]},
            ],
            "usage": {},
        })

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
def test_get_session_state_no_events_no_final_text():
    def handler(request):
        return httpx.Response(200, json={"status": "running", "events": [], "usage": {}})

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
