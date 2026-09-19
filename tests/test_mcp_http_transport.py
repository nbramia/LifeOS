"""Tests for the MCP server HTTP transport and bearer-token auth.

The HTTP transport is exposed by `mcp_server.py --transport http` so Anthropic
Managed Agents (and other remote callers) can reach LifeOS tools over the
internet. These tests exercise the auth gate and the request dispatcher
without needing the live API server: tool calls are short-circuited by
monkey-patching `LifeOSMCPServer._call_api`.
"""
from __future__ import annotations

import io
import json
import logging
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


# ---------------------------------------------------------------------------
# Tool allowlist enforcement at the transport level. `server`/`client` above
# build a plain `LifeOSMCPServer()` (no allowlist) — the shape the existing
# :8765 instance runs today — so those fixtures double as the "unaffected
# behavior" baseline here.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_unset_allowlist_lists_and_calls_every_tool(client: TestClient, bearer_token: str, server: mcp_server.LifeOSMCPServer):
    """The existing :8765 instance (no allowlist configured) must keep
    listing and accepting every registered tool — the allowlist mechanism
    is opt-in and must not narrow default behavior."""
    list_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    listed_names = {t["name"] for t in list_resp.json()["result"]["tools"]}
    assert listed_names == {t["name"] for t in server.tools}
    assert len(listed_names) > 1

    for name in listed_names:
        call_resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}},
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        body = call_resp.json()
        assert "result" in body, f"{name} was rejected instead of dispatched: {body}"
        assert body["result"].get("isError") is not True


@pytest.mark.unit
def test_disallowed_tool_rejected_before_call_api(bearer_token: str, monkeypatch):
    """A tool outside the configured allowlist must be rejected by
    tools/call without `_call_api` (and therefore the LifeOS API) ever being
    reached — and hidden from tools/list, the same boundary."""
    restricted = mcp_server.LifeOSMCPServer(allowed_tools=frozenset({"lifeos_health"}))

    def _fail_if_called(self, tool_name, arguments):
        raise AssertionError(f"_call_api must not be reached for {tool_name!r}")

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _fail_if_called)
    app = mcp_server.build_http_app(restricted, bearer_token=bearer_token)
    restricted_client = TestClient(app)

    list_resp = restricted_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    listed_names = {t["name"] for t in list_resp.json()["result"]["tools"]}
    assert listed_names == {"lifeos_health"}
    assert "lifeos_search" not in listed_names

    call_resp = restricted_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert call_resp.status_code == 200
    body = call_resp.json()
    assert body["result"]["isError"] is True


@pytest.mark.unit
def test_bearer_token_redaction_filter_redacts_message():
    """Unit-level check of the redaction logic itself, independent of
    logger/handler wiring: a record whose rendered message contains an
    `Authorization: Bearer <token>` header comes back with the token
    redacted, never verbatim."""
    from api.services.log_redaction import BearerTokenRedactionFilter

    secret = "super-secret-instinct-token-abc123"
    record = logging.LogRecord(
        name="mcp_server", level=logging.ERROR, pathname=__file__, lineno=1,
        msg=f"unexpected failure — Authorization: Bearer {secret}", args=(), exc_info=None,
    )
    BearerTokenRedactionFilter().filter(record)
    assert secret not in record.getMessage()
    assert "Bearer <REDACTED>" in record.getMessage()


@pytest.mark.unit
def test_build_http_app_installs_bearer_redaction_filter(server: mcp_server.LifeOSMCPServer, bearer_token: str):
    """`build_http_app` must wire the backstop redaction filter onto the
    process's root logger — not just define it. Starts from a clean slate
    (stripping any instance a prior test's `build_http_app` call already
    installed) so this only passes if *this* call installed one."""
    from api.services.log_redaction import BearerTokenRedactionFilter

    root = logging.getLogger()
    for f in list(root.filters):
        if isinstance(f, BearerTokenRedactionFilter):
            root.removeFilter(f)
    for h in root.handlers:
        for f in list(h.filters):
            if isinstance(f, BearerTokenRedactionFilter):
                h.removeFilter(f)
    assert not any(isinstance(f, BearerTokenRedactionFilter) for f in root.filters)

    mcp_server.build_http_app(server, bearer_token=bearer_token)

    assert any(isinstance(f, BearerTokenRedactionFilter) for f in root.filters)


@pytest.mark.unit
def test_401_response_body_never_contains_the_token(client: TestClient, bearer_token: str):
    """The 401 paths (missing/wrong credential) must never echo the
    configured bearer token back in the response body."""
    resp = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert bearer_token not in resp.text


@pytest.mark.unit
def test_missing_header_401_response_body_never_contains_the_token(client: TestClient, bearer_token: str):
    """No-`Authorization`-header 401 is a distinct code path from the
    wrong-token 401 above (`_check_auth` raises before ever comparing
    tokens) — must not echo the configured token either."""
    resp = client.post("/mcp", json=_initialize_request())
    assert resp.status_code == 401
    assert bearer_token not in resp.text


@pytest.mark.unit
def test_call_api_exception_response_never_contains_the_token(client: TestClient, bearer_token: str, monkeypatch):
    """A `_call_api` exception is caught by `dispatch()`'s outer handler and
    turned into `str(e)` inside the JSON-RPC error body — that path must
    never surface the configured bearer token, even though nothing today
    threads it into an exception message."""
    def _raise(self, tool_name, arguments):
        raise RuntimeError("upstream connection failed")

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _raise)
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    assert bearer_token not in resp.text


@pytest.mark.unit
def test_allowed_call_on_restricted_instance_reaches_call_api_and_succeeds(bearer_token: str, monkeypatch):
    """The allowlist boundary must let an allowed call through end to end —
    not just reject a disallowed one. A restricted instance's own approved
    tool must reach `_call_api` and come back without `isError`."""
    restricted = mcp_server.LifeOSMCPServer(allowed_tools=frozenset({"lifeos_health"}))
    calls = []

    def fake_call(self, tool_name, arguments):
        calls.append(tool_name)
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", fake_call)
    app = mcp_server.build_http_app(restricted, bearer_token=bearer_token)
    restricted_client = TestClient(app)

    resp = restricted_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lifeos_health", "arguments": {}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    assert calls == ["lifeos_health"]
    assert resp.json()["result"].get("isError") is not True


@pytest.mark.unit
def test_disallowed_tool_rejected_in_batch_request(bearer_token: str, monkeypatch):
    """The allowlist check in `dispatch()` runs per-request regardless of
    transport shape — a disallowed tool inside a JSON-RPC batch must be
    rejected the same way a single request is, without reaching `_call_api`."""
    restricted = mcp_server.LifeOSMCPServer(allowed_tools=frozenset({"lifeos_health"}))

    def _fail_if_called(self, tool_name, arguments):
        raise AssertionError(f"_call_api must not be reached for {tool_name!r}")

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _fail_if_called)
    app = mcp_server.build_http_app(restricted, bearer_token=bearer_token)
    restricted_client = TestClient(app)

    resp = restricted_client.post(
        "/mcp",
        json=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
            },
        ],
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["result"]["isError"] is True


@pytest.mark.unit
def test_disallowed_tool_rejected_on_root_route(bearer_token: str, monkeypatch):
    """The `/` route shares `_handle` with `/mcp` — a disallowed tool posted
    there must be rejected the same way, without reaching `_call_api`."""
    restricted = mcp_server.LifeOSMCPServer(allowed_tools=frozenset({"lifeos_health"}))

    def _fail_if_called(self, tool_name, arguments):
        raise AssertionError(f"_call_api must not be reached for {tool_name!r}")

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _fail_if_called)
    app = mcp_server.build_http_app(restricted, bearer_token=bearer_token)
    restricted_client = TestClient(app)

    resp = restricted_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lifeos_search", "arguments": {"query": "hello"}},
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["isError"] is True


@pytest.mark.unit
def test_redaction_covers_child_logger_and_uvicorn_error_logger():
    """Behavioral pin for the redaction backstop's effective coverage — not
    just that a filter object exists somewhere, but that a record logged
    against a plain `mcp_server` child logger (redacted via the root-handler
    install `build_http_app` performs) AND a record logged through
    `uvicorn.error` (redacted via the log_config `run_http` passes to
    uvicorn, since uvicorn.run() otherwise installs its own handlers that
    never propagate to root) both come back with the token redacted."""
    import logging.config

    from api.services.log_redaction import install_bearer_token_redaction_filter

    secret = "synthetic-instinct-token-abc123"

    root = logging.getLogger()
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_error_logger = logging.getLogger("uvicorn.error")

    prior = {
        "root_handlers": list(root.handlers),
        "root_filters": list(root.filters),
        "uvicorn_handlers": list(uvicorn_logger.handlers),
        "uvicorn_propagate": uvicorn_logger.propagate,
        "uvicorn_level": uvicorn_logger.level,
        "uvicorn_error_handlers": list(uvicorn_error_logger.handlers),
        "uvicorn_error_propagate": uvicorn_error_logger.propagate,
        "uvicorn_error_level": uvicorn_error_logger.level,
    }

    child_buf = io.StringIO()
    uvicorn_buf = io.StringIO()
    try:
        root.handlers = [logging.StreamHandler(child_buf)]
        install_bearer_token_redaction_filter()

        logging.config.dictConfig(mcp_server._uvicorn_log_config_with_redaction())
        uvicorn_logger.handlers[0].stream = uvicorn_buf

        logging.getLogger("mcp_server").error(f"failure — Authorization: Bearer {secret}")
        uvicorn_error_logger.error(f"failure — Authorization: Bearer {secret}")

        child_output = child_buf.getvalue()
        uvicorn_output = uvicorn_buf.getvalue()
    finally:
        root.handlers = prior["root_handlers"]
        root.filters = prior["root_filters"]
        uvicorn_logger.handlers = prior["uvicorn_handlers"]
        uvicorn_logger.propagate = prior["uvicorn_propagate"]
        uvicorn_logger.setLevel(prior["uvicorn_level"])
        uvicorn_error_logger.handlers = prior["uvicorn_error_handlers"]
        uvicorn_error_logger.propagate = prior["uvicorn_error_propagate"]
        uvicorn_error_logger.setLevel(prior["uvicorn_error_level"])

    assert secret not in child_output
    assert secret not in uvicorn_output
    assert "Bearer <REDACTED>" in child_output
    assert "Bearer <REDACTED>" in uvicorn_output


@pytest.mark.unit
def test_run_http_passes_redacting_log_config_to_uvicorn(
    server: mcp_server.LifeOSMCPServer, monkeypatch
):
    """`run_http` must pass the redacting log_config to `uvicorn.run` —
    a regression to a bare `uvicorn.run(app, host=..., port=..., log_level=...)`
    with no `log_config` argument would leave every uvicorn-installed handler
    unfiltered, and no test that only exercises
    `_uvicorn_log_config_with_redaction()` directly would catch it."""
    import uvicorn

    captured: dict[str, Any] = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    mcp_server.run_http(server, "127.0.0.1", 0, "synthetic-token")

    assert "log_config" in captured, captured
    handlers = captured["log_config"].get("handlers", {})
    assert handlers, captured["log_config"]
    for name, handler in handlers.items():
        assert "bearer_redact" in handler.get("filters", []), (name, handler)
