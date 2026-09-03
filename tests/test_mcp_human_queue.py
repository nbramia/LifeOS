"""
Tests that the three Human-queue MCP tools (#852) are registered and
reachable over both the stdio and HTTP transports, and that the total tool
count (67 = 59 CURATED_ENDPOINTS + 8 lifeos_agent_*) matches AGENTS.md.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

MCP_SERVER_PATH = Path(__file__).parent.parent / "mcp_server.py"

_HUMAN_QUEUE_TOOL_NAMES = {
    "lifeos_human_queue_add",
    "lifeos_human_queue_list",
    "lifeos_human_queue_resolve",
}


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _server_built_from_live_spec(module):
    """A LifeOSMCPServer with `.tools` built from this checkout's live
    OpenAPI spec, in-process — no HTTP round-trip, no running server. Same
    helper pattern as tests/test_workout_mcp_route.py."""
    from api.main import app
    openapi_spec = app.openapi()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    server.openapi_spec = openapi_spec
    server._build_tools_from_spec()
    return server


class TestCuratedEndpointsRegistration:
    def test_curated_endpoint_count(self):
        """56 pre-#852 + 3 human-queue tools."""
        module = _load_mcp_module()
        assert len(module.CURATED_ENDPOINTS) == 59

    def test_three_tools_in_curated_endpoints(self):
        module = _load_mcp_module()
        names = {c["name"] for c in module.CURATED_ENDPOINTS.values()}
        assert _HUMAN_QUEUE_TOOL_NAMES <= names

    def test_descriptions_within_30_word_budget(self):
        module = _load_mcp_module()
        for cfg in module.CURATED_ENDPOINTS.values():
            if cfg["name"] in _HUMAN_QUEUE_TOOL_NAMES:
                assert len(cfg["description"].split()) <= 30, cfg["name"]

    def test_tools_do_not_require_worker_handle(self):
        """Unlike lifeos_agent_user_ask, these are plain CURATED_ENDPOINTS
        REST-mapped tools — dispatched via _call_api, never
        _handle_inter_agent, so they never touch ctx.worker_handle."""
        for name in _HUMAN_QUEUE_TOOL_NAMES:
            assert not name.startswith("lifeos_agent_")


class TestHttpTransportRegistration:
    def test_tools_list_includes_human_queue_tools(self):
        module = _load_mcp_module()
        server = _server_built_from_live_spec(module)
        app = module.build_http_app(server, bearer_token="test-token")
        client = TestClient(app)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert _HUMAN_QUEUE_TOOL_NAMES <= names

    def test_total_tool_count_matches_agents_md(self):
        """67 = 59 CURATED_ENDPOINTS + 8 lifeos_agent_* — see AGENTS.md's
        mcp_server.py row."""
        module = _load_mcp_module()
        server = _server_built_from_live_spec(module)
        assert len(server.tools) == 67


def _server_built_from_fallback(module):
    """A LifeOSMCPServer with `.tools` built from the offline fallback
    schemas (`_build_tools_fallback`) — the path taken when the OpenAPI
    spec is unreachable, and the deterministic way to exercise the stdio
    registration in a test environment that may have a real lifeos-api
    reachable on localhost:8000 (whose live spec predates this checkout's
    new routes and would otherwise make this test environment-dependent).
    """
    with patch.object(
        module.LifeOSMCPServer, "_load_openapi_spec",
        lambda self: self._build_tools_fallback(),
    ):
        return module.LifeOSMCPServer()


class TestStdioTransportRegistration:
    def test_tools_list_includes_human_queue_tools(self):
        module = _load_mcp_module()
        server = _server_built_from_fallback(module)
        response = module.dispatch(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        names = {t["name"] for t in response["result"]["tools"]}
        assert _HUMAN_QUEUE_TOOL_NAMES <= names

    def test_add_tool_input_schema_requires_title(self):
        module = _load_mcp_module()
        server = _server_built_from_fallback(module)
        response = module.dispatch(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        tools = {t["name"]: t for t in response["result"]["tools"]}
        schema = tools["lifeos_human_queue_add"]["inputSchema"]
        assert "title" in schema["properties"]
        assert "title" in schema.get("required", [])
