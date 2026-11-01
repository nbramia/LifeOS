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

    formatter_arguments: list[dict[str, Any] | None] = []

    def fake_format(
        self: mcp_server.LifeOSMCPServer, tool_name: str, data: dict[str, Any],
        arguments: dict[str, Any] | None = None,
    ) -> str:
        formatter_arguments.append(arguments)
        return json.dumps(data)

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", fake_call)
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer,
        "_format_response",
        fake_format,
    )
    srv.formatter_arguments = formatter_arguments
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
def test_tools_call_dispatches_to_handler(
    client: TestClient, bearer_token: str, server: mcp_server.LifeOSMCPServer,
):
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
    assert server.formatter_arguments == [{"query": "hello"}]


@pytest.mark.unit
def test_tools_call_sets_is_error_on_tool_failure(client: TestClient, bearer_token: str, monkeypatch):
    """A tool-level failure must surface as MCP
    `isError: true`, not just as prose inside a structurally-successful
    JSON-RPC result — the same "error" key convention the agent worker's
    ToolRegistry already uses (api/services/agent_worker/tools.py) to decide
    is_error, applied here so an MCP client reading the structured field
    (not just the formatted text) can also tell success from failure."""
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_call_api",
        lambda self, name, args: {"error": "boom"},
    )
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "lifeos_workout_manage", "arguments": {"action": "log", "sets": []}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["isError"] is True


@pytest.mark.unit
def test_tools_call_omits_is_error_on_success(client: TestClient, bearer_token: str):
    """A successful call (no "error" key in the tool's data) must not carry
    `isError` at all — confirms the new field is failure-only, not a blanket
    addition that could confuse existing callers."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    assert "isError" not in resp.json()["result"]


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


# Tailnet-served path: Tailscale Serve proxies tailnet requests from loopback.

LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture
def loopback_client(server: mcp_server.LifeOSMCPServer, bearer_token: str) -> TestClient:
    """A client whose TCP peer is loopback, as a request relayed by Tailscale Serve."""
    return TestClient(mcp_server.build_http_app(server, bearer_token=bearer_token), client=LOOPBACK_PEER)


def _tools_list(client: TestClient, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)


@pytest.mark.unit
@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_loopback_peer_without_a_valid_bearer_is_rejected_on_mcp(loopback_client: TestClient, token):
    assert _tools_list(loopback_client, token).status_code == 401
    assert loopback_client.get("/mcp").status_code == 401


@pytest.mark.unit
def test_loopback_peer_with_the_bearer_lists_the_full_curated_catalog(
    monkeypatch, bearer_token: str
):
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_load_openapi_spec",
        lambda self: self._build_tools_fallback(),
    )
    server = mcp_server.LifeOSMCPServer()
    loopback_client = TestClient(
        mcp_server.build_http_app(server, bearer_token=bearer_token), client=LOOPBACK_PEER
    )
    resp = _tools_list(loopback_client, bearer_token)
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {t["name"] for t in server.tools}
    assert {config["name"] for config in mcp_server.CURATED_ENDPOINTS.values()} <= names
    assert "lifeos_vault_read" in names


@pytest.mark.unit
def test_oauth_tier_includes_vault_read_as_read_only():
    from api.services.mcp_tool_tier import OAUTH_ALLOWED_TOOLS, OAUTH_TOOL_TIER

    assert OAUTH_TOOL_TIER["lifeos_vault_read"].allowed is True
    assert OAUTH_TOOL_TIER["lifeos_vault_read"].read_only is True
    assert "lifeos_vault_read" in OAUTH_ALLOWED_TOOLS


@pytest.mark.unit
def test_public_socket_allowlist_serves_only_the_oauth_paths():
    assert mcp_server.PUBLIC_SOCKET_PATHS == {
        "/mcp",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/oauth/register",
        "/oauth/token",
        "/oauth/revoke",
    }


# --- The full-access token stays off public edges ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("edge_header", [
    {"Tailscale-Funnel-Request": "?1"},
    {"Cf-Connecting-Ip": "203.0.113.9"},
])
def test_full_access_token_is_refused_through_a_public_edge(
    loopback_client: TestClient, bearer_token: str, edge_header: dict[str, str],
):
    resp = loopback_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {bearer_token}", **edge_header},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_full_access_token_is_refused_on_the_public_listener(
    server: mcp_server.LifeOSMCPServer, bearer_token: str,
):
    public = TestClient(
        mcp_server.public_socket_app(mcp_server.build_http_app(server, bearer_token=bearer_token)),
        client=LOOPBACK_PEER,
    )
    assert _tools_list(public, bearer_token).status_code == 401


@pytest.mark.unit
def test_public_bearer_opt_in_accepts_the_token_through_a_public_edge(
    server: mcp_server.LifeOSMCPServer, bearer_token: str,
):
    app = mcp_server.build_http_app(server, bearer_token=bearer_token, allow_public_bearer=True)
    public = TestClient(mcp_server.public_socket_app(app), client=LOOPBACK_PEER)
    assert _tools_list(public, bearer_token).status_code == 200
    via_tunnel = TestClient(app, client=LOOPBACK_PEER).post(
        "/mcp",
        headers={"Authorization": f"Bearer {bearer_token}", "Cf-Connecting-Ip": "203.0.113.9"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert via_tunnel.status_code == 200


@pytest.mark.unit
def test_tailnet_request_with_the_full_access_token_is_accepted(
    loopback_client: TestClient, bearer_token: str,
):
    # Tailscale Serve (no Funnel marker) presents a loopback peer.
    assert _tools_list(loopback_client, bearer_token).status_code == 200


@pytest.mark.unit
def test_non_ascii_token_is_a_401_not_a_server_error(client: TestClient):
    resp = client.post(
        "/mcp",
        headers={"Authorization": "Bearer töken".encode("latin-1")},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


# --- Audit log ------------------------------------------------------------------


@pytest.mark.unit
def test_worker_reaching_call_is_audit_logged_without_argument_values(
    client: TestClient, bearer_token: str, caplog,
):
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "lifeos_task_update",
            "arguments": {"task_id": "abc123", "description": "synthetic-secret-value"},
        }},
    )
    assert resp.status_code == 200
    audit = [r for r in caplog.records if r.getMessage().startswith("mcp audit:")]
    assert len(audit) == 1
    record = audit[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "caller=full-access-token" in message
    assert "edge=local" in message
    assert "tool=lifeos_task_update" in message
    assert "reaches_worker=True" in message
    assert "arg_keys=description,task_id" in message
    assert "synthetic-secret-value" not in message


@pytest.mark.unit
def test_read_call_is_audit_logged_at_info(client: TestClient, bearer_token: str, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
            "name": "lifeos_vault_list", "arguments": {},
        }},
    )
    audit = [r for r in caplog.records if r.getMessage().startswith("mcp audit:")]
    assert len(audit) == 1
    assert audit[0].levelno == logging.INFO
    assert "reaches_worker=False" in audit[0].getMessage()


@pytest.mark.unit
def test_audit_keeps_arguments_a_handler_consumes(client: TestClient, bearer_token: str, caplog, monkeypatch):
    """Handlers pop path parameters out of the arguments they are given; the
    audit line must still name and hash them, or a delete or resolve can't be
    traced to the item it touched."""
    import hashlib
    import logging

    def consuming_call(self, tool_name, arguments):
        arguments.pop("schedule_id", None)
        return {"ok": True}

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", consuming_call)
    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    client.post("/mcp", headers={"Authorization": f"Bearer {bearer_token}"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "lifeos_schedule_delete", "arguments": {"schedule_id": "abc12345"}},
    })
    (message,) = _audit_messages(caplog)
    assert "arg_keys=schedule_id" in message
    empty = hashlib.sha256(b"{}").hexdigest()[:16]
    assert f"args_sha256={empty}" not in message


@pytest.mark.unit
def test_call_api_leaves_the_callers_arguments_intact(monkeypatch):
    """Path parameters go into the URL without being removed from the dict the
    caller passed."""
    from unittest.mock import MagicMock

    srv = mcp_server.LifeOSMCPServer()
    response = MagicMock()
    response.json.return_value = {"id": "abc12345"}
    srv.client = MagicMock()
    srv.client.delete.return_value = response
    arguments = {"schedule_id": "abc12345"}
    srv._call_api("lifeos_schedule_delete", arguments)
    assert arguments == {"schedule_id": "abc12345"}
    assert srv.client.delete.call_args.args[0].endswith("/abc12345")


@pytest.mark.unit
def test_reminder_reply_shows_no_next_trigger_while_disabled():
    srv = mcp_server.LifeOSMCPServer()
    for tool in ("lifeos_reminder_create", "lifeos_reminder_update"):
        disabled = srv._format_response(tool, {
            "id": "abc12345", "name": "Synthetic", "enabled": False,
            "next_trigger_at": "2030-01-01T14:00:00+00:00",
        })
        assert "Next trigger: none (disabled)" in disabled
        enabled = srv._format_response(tool, {
            "id": "abc12345", "name": "Synthetic", "enabled": True,
            "next_trigger_at": "2030-01-01T14:00:00+00:00",
        })
        assert "Next trigger: 2030-01-01T14:00:00+00:00" in enabled
        unscheduled = srv._format_response(tool, {
            "id": "abc12345", "name": "Synthetic", "enabled": True, "next_trigger_at": None,
        })
        assert "Next trigger: not scheduled" in unscheduled


# --- OpenAPI spec load at startup ----------------------------------------------


class _FlakySpecClient:
    """Refuses the first `failures` connections, then serves a spec."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def get(self, url):
        from unittest.mock import MagicMock

        import httpx

        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("Connection refused")
        resp = MagicMock()
        resp.json.return_value = {"paths": {}, "components": {"schemas": {}}}
        return resp


def _server_with_spec_client(monkeypatch, fake, wait_seconds):
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)
    monkeypatch.setattr(mcp_server.httpx, "Client", lambda **kw: fake)
    return mcp_server.LifeOSMCPServer(spec_wait_seconds=wait_seconds)


@pytest.mark.unit
def test_spec_load_waits_for_the_api_when_asked(monkeypatch):
    fake = _FlakySpecClient(failures=3)
    srv = _server_with_spec_client(monkeypatch, fake, wait_seconds=60)
    assert fake.calls == 4
    assert srv.openapi_spec == {"paths": {}, "components": {"schemas": {}}}


@pytest.mark.unit
def test_spec_load_without_a_wait_tries_once_and_falls_back(monkeypatch):
    fake = _FlakySpecClient(failures=3)
    srv = _server_with_spec_client(monkeypatch, fake, wait_seconds=0)
    assert fake.calls == 1
    assert srv.openapi_spec is None
    assert any(t["name"] == "lifeos_health" for t in srv.tools)


@pytest.mark.unit
def test_spec_load_gives_up_after_the_wait(monkeypatch):
    clock = iter(range(0, 10_000, 7))
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: next(clock))
    fake = _FlakySpecClient(failures=10_000)
    srv = _server_with_spec_client(monkeypatch, fake, wait_seconds=30)
    assert 1 < fake.calls < 10
    assert srv.openapi_spec is None
    names = [t["name"] for t in srv.tools]
    assert len(names) == len(set(names))


@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [("", 90.0), ("15", 15.0), ("-3", 0.0), ("soon", 90.0)])
def test_spec_wait_seconds_setting(monkeypatch, raw, expected):
    monkeypatch.setenv("LIFEOS_MCP_SPEC_WAIT_SECONDS", raw)
    assert mcp_server._spec_wait_seconds() == expected


def _audit_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("mcp audit:")]


def _call(req_id, name="lifeos_vault_list", arguments=None) -> dict:
    request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    if req_id is not None:
        request["id"] = req_id
    return request


@pytest.mark.unit
def test_successful_notification_tool_call_is_audited_as_not_failed(
    client: TestClient, bearer_token: str, caplog,
):
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    resp = client.post("/mcp", headers={"Authorization": f"Bearer {bearer_token}"}, json=_call(None))
    assert resp.status_code == 202
    messages = _audit_messages(caplog)
    assert len(messages) == 1
    assert "failed=False" in messages[0]


@pytest.mark.unit
def test_batch_tool_calls_are_audited_once_each(client: TestClient, bearer_token: str, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json=[_call(1, "lifeos_vault_list"), _call(2, "lifeos_task_list")],
    )
    assert resp.status_code == 200
    messages = _audit_messages(caplog)
    assert len(messages) == 2
    assert "tool=lifeos_vault_list" in messages[0]
    assert "tool=lifeos_task_list" in messages[1]


@pytest.mark.unit
def test_audit_failed_flag_tracks_tool_errors(
    client: TestClient, bearer_token: str, caplog, monkeypatch,
):
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    headers = {"Authorization": f"Bearer {bearer_token}"}
    client.post("/mcp", headers=headers, json=_call(1))
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_call_api", lambda self, name, args: {"error": "boom"},
    )
    client.post("/mcp", headers=headers, json=_call(2))

    def _raise(self, name, args):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _raise)
    client.post("/mcp", headers=headers, json=_call(3))
    messages = _audit_messages(caplog)
    assert [("failed=True" in m) for m in messages] == [False, True, True]


@pytest.mark.unit
def test_audit_argument_hash_is_stable_and_argument_specific(
    client: TestClient, bearer_token: str, caplog,
):
    import logging
    import re

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    headers = {"Authorization": f"Bearer {bearer_token}"}
    for i, args in enumerate([{"query": "a"}, {"query": "a"}, {"query": "b"}], start=1):
        client.post("/mcp", headers=headers, json=_call(i, "lifeos_search", args))
    hashes = [re.search(r"args_sha256=(\S+)", m).group(1) for m in _audit_messages(caplog)]
    assert all(re.fullmatch(r"[0-9a-f]{16}", h) for h in hashes)
    assert hashes[0] == hashes[1]
    assert hashes[0] != hashes[2]


@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "lifeos_agent_spawn", "lifeos_task_create", "lifeos_project_start", "lifeos_schedule_trigger",
])
def test_worker_reaching_tools_include_every_agent_entry_point(name):
    assert mcp_server.reaches_worker(name)


@pytest.mark.unit
def test_worker_reaching_tools_are_all_denied_to_connected_apps_or_guarded():
    from api.services.mcp_tool_tier import OAUTH_TOOL_TIER

    for name in mcp_server.WORKER_REACHING_TOOLS:
        tier = OAUTH_TOOL_TIER[name]
        # lifeos_task_create is allowed only with its engine-tag guard.
        assert not tier.allowed or name == "lifeos_task_create", name


@pytest.mark.unit
def test_audit_line_fields_are_restricted_to_one_line(caplog):
    """A tool name or argument key holding a newline and an audit prefix is
    reduced to a safe token, so one call writes exactly one single-line record."""
    import logging

    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    multiline = "lifeos_health\nmcp audit: caller=full-access-token edge=local tool=x"
    request_obj = {
        "method": "tools/call",
        "params": {"name": multiline, "arguments": {multiline: 1, "ok_key": 2}},
    }
    mcp_server._audit_tool_call(request_obj, {"result": {}}, caller="full-access-token", edge=None)
    records = [r.getMessage() for r in caplog.records if r.getMessage().startswith("mcp audit:")]
    assert len(records) == 1
    assert "\n" not in records[0] and "\r" not in records[0]
    assert records[0].count("mcp audit:") == 1
    assert "ok_key" in records[0]
    long = mcp_server._audit_token("a" * 500)
    assert len(long) == 64
    assert mcp_server._audit_token(["x"]) == "list"
