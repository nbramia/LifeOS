"""Tests for ManagedAgentsDriver — HTTP request shape + parsing.

These don't hit the real Managed Agents API; we wire `httpx.MockTransport`
that asserts the request shape and returns canned responses, so the test
verifies our client matches the documented schema. **Operator should still
smoke-test against a real account** because the API is in beta.
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


def _build_driver(handler, *, vault_id="vlt_test"):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://test")
    return ManagedAgentsDriver(
        api_key="sk-test",
        base_url="http://test/v1/managed-agents",
        http_client=client,
        vault_id=vault_id,
    )


@pytest.mark.unit
def test_driver_requires_api_key():
    with pytest.raises(ValueError):
        ManagedAgentsDriver(api_key="")


@pytest.mark.unit
def test_create_session_request_shape_and_returns_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"session_id": "sess_remote_xyz"})

    driver = _build_driver(handler)
    sid = driver.create_session(
        system_prompt="be useful",
        user_message="say hi",
        model="claude-opus-4-7",
        mcp_servers=[{"name": "lifeos", "url": "http://mcp"}],
        connectors=["gmail"],
        max_tokens=1000,
        max_dollars=2.0,
        max_wall_seconds=300,
    )
    assert sid == "sess_remote_xyz"
    assert captured["url"].endswith("/sessions")
    assert captured["headers"]["x-api-key"] == "sk-test"
    body = captured["body"]
    assert body["model"] == "claude-opus-4-7"
    assert body["system_prompt"] == "be useful"
    assert body["initial_message"] == "say hi"
    assert body["vault_id"] == "vlt_test"
    assert body["mcp_servers"] == [{"name": "lifeos", "url": "http://mcp"}]
    assert body["connectors"] == ["gmail"]
    assert body["max_tokens"] == 1000
    assert body["max_dollars"] == 2.0
    assert body["max_wall_seconds"] == 300


@pytest.mark.unit
def test_create_session_raises_when_id_missing():
    def handler(request):
        return httpx.Response(200, json={"foo": "bar"})

    driver = _build_driver(handler)
    with pytest.raises(RuntimeError, match="missing session id"):
        driver.create_session(
            system_prompt="x", user_message="y", model="claude-opus-4-7",
        )


@pytest.mark.unit
def test_create_session_raises_on_http_error():
    def handler(request):
        return httpx.Response(500, text="server error")

    driver = _build_driver(handler)
    with pytest.raises(httpx.HTTPStatusError):
        driver.create_session(system_prompt="x", user_message="y", model="m")


@pytest.mark.unit
def test_get_session_state_parses_full_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/sessions/sess_abc")
        return httpx.Response(200, json={
            "session_id": "sess_abc",
            "status": "running",
            "events": [
                {"id": "evt_1", "type": "agent.message", "payload": {"text": "hi"}},
                {"id": "evt_2", "type": "tool.call", "payload": {"tool": "Bash"}},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 40},
            "final_text": None,
            "error": None,
        })

    driver = _build_driver(handler)
    state = driver.get_session_state("sess_abc")
    assert state.session_id == "sess_abc"
    assert state.status == "running"
    assert state.last_event_id == "evt_2"
    assert len(state.new_events) == 2
    assert state.total_input_tokens == 100
    assert state.total_output_tokens == 40
    assert state.final_text is None
    assert state.error_reason is None


@pytest.mark.unit
def test_get_session_state_uses_since_cursor():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "status": "running", "events": [], "usage": {}, "final_text": None, "error": None,
        })

    driver = _build_driver(handler)
    driver.get_session_state("sess_abc", since_event_id="evt_7")
    assert captured["params"]["since"] == "evt_7"


@pytest.mark.unit
def test_get_session_state_parses_terminal_with_final_text_and_error():
    def handler(request):
        return httpx.Response(200, json={
            "status": "completed", "events": [], "usage": {"input_tokens": 50, "output_tokens": 80},
            "final_text": "done!", "error": None,
        })

    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "completed"
    assert state.final_text == "done!"
    assert state.total_output_tokens == 80


@pytest.mark.unit
def test_get_session_state_failed_carries_error_message():
    def handler(request):
        return httpx.Response(200, json={
            "status": "failed", "events": [], "usage": {},
            "final_text": None, "error": {"message": "remote tool crashed"},
        })

    state = _build_driver(handler).get_session_state("sess")
    assert state.status == "failed"
    assert state.error_reason == "remote tool crashed"


@pytest.mark.unit
def test_post_user_message_request_shape():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    _build_driver(handler).post_user_message("sess", "and here is the answer")
    assert captured["method"] == "POST"
    assert captured["body"] == {"role": "user", "content": "and here is the answer"}


@pytest.mark.unit
def test_kill_session_uses_delete_and_swallows_errors():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        return httpx.Response(204)

    _build_driver(handler).kill_session("sess", reason="budget")
    assert captured["method"] == "DELETE"

    # And the error path is swallowed (logged, not raised).
    def boom(request):
        return httpx.Response(500)

    _build_driver(boom).kill_session("sess")  # must not raise


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
