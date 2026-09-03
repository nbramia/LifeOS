"""
Tests that the three Human-queue MCP tools (#852) are registered and
reachable over both the stdio and HTTP transports, and that the total tool
count (67 = 59 CURATED_ENDPOINTS + 8 lifeos_agent_*) matches AGENTS.md.
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        REST-mapped tools — dispatched via _call_api's HTTP path, never
        _handle_inter_agent (the only path that touches ctx.worker_handle).
        A real dispatch, not just a name-prefix check: patch _handle_inter_
        agent and assert it's never reached for any of the three tools."""
        module = _load_mcp_module()
        server = module.LifeOSMCPServer()
        fake = MagicMock()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"id": "task-1", "cards": [], "total": 0}
        fake_response.raise_for_status = MagicMock()
        fake.get.return_value = fake_response
        fake.post.return_value = fake_response
        fake.request.return_value = fake_response
        server.client = fake

        with patch.object(module.LifeOSMCPServer, "_handle_inter_agent") as mock_inter_agent:
            server._call_api("lifeos_human_queue_add", {"title": "X"})
            server._call_api("lifeos_human_queue_list", {})
            server._call_api("lifeos_human_queue_resolve", {"id_or_key": "x", "note": "done"})
            mock_inter_agent.assert_not_called()


class TestCallApiDispatch:
    """Real `_call_api` dispatch coverage (#852 review): the tool-count and
    schema tests above never actually invoke the dispatcher against a
    mocked HTTP client."""

    def _server_with_mock_client(self, module, response_json):
        server = module.LifeOSMCPServer()
        fake = MagicMock()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = response_json
        fake_response.raise_for_status = MagicMock()
        fake.get.return_value = fake_response
        fake.post.return_value = fake_response
        fake.request.return_value = fake_response
        server.client = fake
        return server, fake

    def test_resolve_dispatches_put_with_key_in_url_and_note_in_body(self):
        module = _load_mcp_module()
        server, fake = self._server_with_mock_client(module, {"id": "task-1", "status": "done"})

        server._call_api(
            "lifeos_human_queue_resolve", {"id_or_key": "sync:gmail", "note": "done"}
        )

        assert fake.request.call_count == 1
        method, url = fake.request.call_args.args
        assert method == "PUT"
        assert url.endswith("/api/tasks/human-queue/sync:gmail/resolve")
        assert fake.request.call_args.kwargs["json"] == {"note": "done"}

    def test_format_response_list_includes_both_cards_titles_and_keys(self):
        module = _load_mcp_module()
        server = module.LifeOSMCPServer()
        payload = {
            "total": 2,
            "cards": [
                {"id": "t1", "title": "Re-authenticate example service", "key": "example-reauth", "age_hours": 5.2},
                {"id": "t2", "title": "Sync source 'gmail' needs attention", "key": "sync:gmail", "age_hours": 30.0},
            ],
        }
        formatted = server._format_response("lifeos_human_queue_list", payload)
        assert "Re-authenticate example service" in formatted
        assert "example-reauth" in formatted
        assert "Sync source 'gmail' needs attention" in formatted
        assert "sync:gmail" in formatted


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

    def test_resolve_tool_input_schema_requires_id_or_key(self):
        module = _load_mcp_module()
        server = _server_built_from_fallback(module)
        response = module.dispatch(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        tools = {t["name"]: t for t in response["result"]["tools"]}
        schema = tools["lifeos_human_queue_resolve"]["inputSchema"]
        assert "id_or_key" in schema["properties"]
        assert "id_or_key" in schema.get("required", [])
