"""Tests for the MCP server HTTP transport and bearer-token auth.

The HTTP transport is exposed by `mcp_server.py --transport http` so Anthropic
Managed Agents (and other remote callers) can reach LifeOS tools over the
internet. These tests exercise the auth gate and the request dispatcher
without needing the live API server: tool calls are short-circuited by
monkey-patching `LifeOSMCPServer._call_api`.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import mcp_server


@pytest.fixture
def bearer_token() -> str:
    return "test-secret-token"


@pytest.fixture
def server(monkeypatch) -> mcp_server.LifeOSMCPServer:
    """A server with stubbed _call_api so tools/call returns deterministic data."""
    srv = mcp_server.LifeOSMCPServer()

    def fake_call(self: mcp_server.LifeOSMCPServer, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"echo": {"tool": tool_name, "arguments": arguments}}

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", fake_call)
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer,
        "_format_response",
        lambda self, tool_name, data: json.dumps(data),
    )
    return srv


@pytest.fixture
def client(server: mcp_server.LifeOSMCPServer, bearer_token: str) -> TestClient:
    app = mcp_server.build_http_app(server, bearer_token=bearer_token)
    return TestClient(app)


def _initialize_request(req_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }


@pytest.mark.unit
def test_missing_authorization_header_returns_401(client: TestClient):
    resp = client.post("/mcp", json=_initialize_request())
    assert resp.status_code == 401


@pytest.mark.unit
def test_wrong_bearer_token_returns_401(client: TestClient):
    resp = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_non_bearer_scheme_returns_401(client: TestClient):
    resp = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_valid_bearer_token_returns_200_with_jsonrpc(client: TestClient, bearer_token: str):
    resp = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "result" in body
    assert body["result"]["protocolVersion"] == "2024-11-05"
    assert body["result"]["serverInfo"]["name"] == "lifeos"


@pytest.mark.unit
def test_tools_list_returns_registered_tools(client: TestClient, bearer_token: str):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    tools = body["result"]["tools"]
    assert isinstance(tools, list)
    assert len(tools) > 0
    # Sanity-check a well-known tool exists
    names = {t["name"] for t in tools}
    assert "lifeos_search" in names


@pytest.mark.unit
def test_tools_call_dispatches_to_handler(client: TestClient, bearer_token: str):
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["content"][0]["type"] == "text"
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["echo"]["tool"] == "lifeos_search"
    assert payload["echo"]["arguments"] == {"query": "hello"}


@pytest.mark.unit
def test_notification_returns_202_no_body(client: TestClient, bearer_token: str):
    """JSON-RPC notifications (no id) get 202 Accepted per MCP streamable-HTTP spec."""
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 202
    assert resp.content == b""


@pytest.mark.unit
def test_unknown_method_returns_jsonrpc_error(client: TestClient, bearer_token: str):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "no/such/method"},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32601  # Method not found


@pytest.mark.unit
def test_malformed_json_returns_parse_error_envelope(client: TestClient, bearer_token: str):
    """Per JSON-RPC 2.0 spec, malformed JSON → -32700 with id null."""
    resp = client.post(
        "/mcp",
        content=b"not json",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32700
    assert body["error"]["message"] == "Parse error"


@pytest.mark.unit
def test_bearer_with_extra_whitespace_accepted(client: TestClient, bearer_token: str):
    """Operator copy-paste pitfall: extra spaces around the token shouldn't 401."""
    resp = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={"Authorization": f"Bearer   {bearer_token}  "},
    )
    assert resp.status_code == 200


@pytest.mark.unit
def test_batch_of_notifications_returns_202(client: TestClient, bearer_token: str):
    """A batch where every entry is a notification has no responses → 202."""
    resp = client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 202
    assert resp.content == b""


@pytest.mark.unit
def test_batch_mixed_returns_only_non_notification_responses(client: TestClient, bearer_token: str):
    """A mixed batch returns responses only for the entries that had an id."""
    resp = client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 42, "method": "tools/list"},
        ],
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == 42


@pytest.mark.unit
def test_build_http_app_requires_bearer_token():
    """Server refuses to build the HTTP app if no bearer token is configured."""
    srv = mcp_server.LifeOSMCPServer()
    with pytest.raises(ValueError, match="bearer"):
        mcp_server.build_http_app(srv, bearer_token="")


@pytest.mark.unit
def test_stdio_dispatch_ignores_bearer(server: mcp_server.LifeOSMCPServer):
    """The stdio path uses dispatch() directly with no auth — local trust."""
    response = mcp_server.dispatch(
        server,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
    )
    assert response is not None
    assert response["id"] == 5
    assert "tools" in response["result"]


@pytest.mark.unit
def test_stdio_notification_returns_none(server: mcp_server.LifeOSMCPServer):
    """Notifications produce no response over stdio (no line written)."""
    response = mcp_server.dispatch(
        server,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response is None
