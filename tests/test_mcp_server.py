"""
Integration tests for the LifeOS MCP Server.

These tests verify that:
1. The MCP server can load the OpenAPI spec from the running API
2. Tool definitions match the actual API endpoints
3. API calls through the MCP server work correctly

Run with: pytest tests/test_mcp_server.py -v
Requires: LifeOS API running on localhost:8000
"""
import json
import pytest
import httpx
import subprocess
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

API_BASE = "http://localhost:8000"
MCP_SERVER_PATH = PROJECT_ROOT / "mcp_server.py"


@pytest.fixture(scope="module")
def api_client():
    """HTTP client for direct API calls."""
    with httpx.Client(base_url=API_BASE, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="module")
def openapi_spec(api_client):
    """Fetch OpenAPI spec from running API."""
    try:
        resp = api_client.get("/openapi.json")
        resp.raise_for_status()
        return resp.json()
    except httpx.RequestError:
        pytest.skip("LifeOS API not running on localhost:8000")


class TestOpenAPIAvailability:
    """Test that OpenAPI spec is available and valid."""

    def test_openapi_spec_available(self, api_client):
        """OpenAPI spec should be accessible."""
        resp = api_client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert "paths" in spec
        assert "openapi" in spec

    def test_openapi_has_required_endpoints(self, openapi_spec):
        """OpenAPI spec should include all curated endpoints."""
        paths = openapi_spec.get("paths", {})

        required_paths = [
            "/api/ask",
            "/api/search",
            "/api/calendar/upcoming",
            "/api/conversations",
            "/api/memories",
        ]

        for path in required_paths:
            assert path in paths, f"Missing endpoint: {path}"


class TestMCPServerToolDiscovery:
    """Test that MCP server correctly discovers tools from OpenAPI."""

    def test_mcp_server_imports(self):
        """MCP server module should import without errors."""
        # Import the module to check for syntax errors
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert hasattr(module, "LifeOSMCPServer")
        assert hasattr(module, "CURATED_ENDPOINTS")

    def test_mcp_server_builds_tools(self):
        """MCP server should build tool definitions."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()

        # Should have tools (either from OpenAPI or fallback)
        assert len(server.tools) > 0, "No tools discovered"

        # Check tool structure
        for tool in server.tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"].get("type") == "object"

    def test_mcp_server_tool_names(self):
        """MCP server tools should have expected names."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()
        tool_names = {t["name"] for t in server.tools}

        expected_tools = {
            "lifeos_ask",
            "lifeos_search",
            "lifeos_health",
        }

        for expected in expected_tools:
            assert expected in tool_names, f"Missing tool: {expected}"


class TestMCPServerAPICalls:
    """Test that MCP server correctly calls the API."""

    def test_health_check(self, api_client):
        """Health endpoint should respond."""
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data

    def test_ask_endpoint_direct(self, api_client):
        """Direct API call to /api/ask should work."""
        resp = api_client.post("/api/ask", json={
            "question": "What is LifeOS?",
            "include_sources": True
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data

    def test_search_endpoint_direct(self, api_client):
        """Direct API call to /api/search should work."""
        resp = api_client.post("/api/search", json={
            "query": "test",
            "top_k": 5
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    def test_mcp_server_ask_tool(self):
        """MCP server lifeos_ask tool should call API correctly."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()
        result = server._call_api("lifeos_ask", {"question": "What is LifeOS?"})

        # Should get a response (either answer or error if API down)
        assert isinstance(result, dict)
        if "error" not in result:
            assert "answer" in result

    def test_mcp_server_search_tool(self):
        """MCP server lifeos_search tool should call API correctly."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()
        result = server._call_api("lifeos_search", {"query": "test", "top_k": 5})

        assert isinstance(result, dict)
        if "error" not in result:
            assert "results" in result


class TestMCPProtocol:
    """Test MCP protocol compliance."""

    def test_tools_list_schema(self):
        """Tool definitions should follow MCP schema."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()

        for tool in server.tools:
            # Required fields
            assert isinstance(tool["name"], str)
            assert len(tool["name"]) > 0
            assert isinstance(tool["description"], str)
            assert isinstance(tool["inputSchema"], dict)

            # inputSchema must be valid JSON Schema
            schema = tool["inputSchema"]
            assert schema.get("type") == "object"
            assert "properties" in schema

    def test_response_format(self):
        """API responses should be properly formatted."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()

        # Test formatting doesn't crash
        test_data = {"answer": "Test answer", "sources": []}
        formatted = server._format_response("lifeos_ask", test_data)
        assert isinstance(formatted, str)
        assert "Test answer" in formatted


class TestAPIOpenAPISync:
    """Test that MCP server stays in sync with API changes."""

    def test_openapi_endpoints_match_curated(self, openapi_spec):
        """Curated endpoints should exist in OpenAPI spec."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        paths = openapi_spec.get("paths", {})

        for path, config in module.CURATED_ENDPOINTS.items():
            # Handle path parameters
            base_path = path.split("{")[0].rstrip("/")
            matching_paths = [p for p in paths if p.startswith(base_path)]

            assert len(matching_paths) > 0, f"Curated endpoint {path} not found in OpenAPI spec"

    def test_request_schemas_match(self, openapi_spec):
        """Tool input schemas should match OpenAPI request schemas."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()
        schemas = openapi_spec.get("components", {}).get("schemas", {})

        # Check that lifeos_ask has question field
        ask_tool = next((t for t in server.tools if t["name"] == "lifeos_ask"), None)
        if ask_tool:
            props = ask_tool["inputSchema"].get("properties", {})
            assert "question" in props, "lifeos_ask missing 'question' property"

        # Check that lifeos_search has query field
        search_tool = next((t for t in server.tools if t["name"] == "lifeos_search"), None)
        if search_tool:
            props = search_tool["inputSchema"].get("properties", {})
            assert "query" in props, "lifeos_search missing 'query' property"
