"""
Integration tests for the LifeOS MCP Server.

These tests verify that:
1. The MCP server can load the OpenAPI spec from the running API
2. Tool definitions match the actual API endpoints
3. API calls through the MCP server work correctly

Run with: pytest tests/test_mcp_server.py -v
Requires: LifeOS API running on localhost:8000
"""
import pytest
import httpx
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
    with httpx.Client(base_url=API_BASE, timeout=90.0) as client:
        yield client


@pytest.fixture(scope="module")
def openapi_spec():
    """OpenAPI spec of the code under test (in-process, no server needed).

    Built from api.main.app rather than fetched from localhost:8000 — a
    running server may be on older code than this checkout, which would
    make the curated-endpoint sync checks fail for any PR that adds an
    endpoint and its curated entry together.
    """
    from api.main import app
    return app.openapi()


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

    def test_mcp_server_tool_names(self, openapi_spec):
        """MCP server tools should have expected names.

        Builds the tool list from this checkout's OpenAPI spec — a running
        server may be on older code without newly added endpoints.
        """
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
            server = module.LifeOSMCPServer()
        server.openapi_spec = openapi_spec
        server._build_tools_from_spec()
        tool_names = {t["name"] for t in server.tools}

        expected_tools = {
            "lifeos_ask",
            "lifeos_search",
            "lifeos_health",
            "lifeos_slack_my_messages",
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

        for path_key, config in module.CURATED_ENDPOINTS.items():
            # Skip tools with custom handlers (no direct API endpoint mapping)
            if config.get("custom_handler"):
                continue
            # Use explicit path if provided, otherwise strip :METHOD suffix
            actual_path = config.get("path", path_key.split(":")[0])
            # Handle path parameters
            base_path = actual_path.split("{")[0].rstrip("/")
            matching_paths = [p for p in paths if p.startswith(base_path)]

            assert len(matching_paths) > 0, f"Curated endpoint {actual_path} not found in OpenAPI spec"

    def test_request_schemas_match(self, openapi_spec):
        """Tool input schemas should match OpenAPI request schemas."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        server = module.LifeOSMCPServer()

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


# ---------------------------------------------------------------------------
# Tool description sizing (#139 §1) — keep cache_creation cheap.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_curated_endpoint_descriptions_average_under_30_words():
    """Every tool description ships into the MCP system prompt, contributing
    to cache_creation cost on every fresh managed session. Keep the average
    under 30 words; no single description over 30 words.

    Run `python -c "from mcp_server import CURATED_ENDPOINTS; ..."` to
    measure manually."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    descriptions = [
        cfg["description"] for cfg in module.CURATED_ENDPOINTS.values()
    ]
    word_counts = [len(d.split()) for d in descriptions]
    avg = sum(word_counts) / len(word_counts)
    over_threshold = [
        (cfg["name"], len(cfg["description"].split()))
        for cfg in module.CURATED_ENDPOINTS.values()
        if len(cfg["description"].split()) > 30
    ]
    assert avg <= 30, f"avg description word count too high: {avg:.1f}"
    assert not over_threshold, (
        f"tools with >30-word description: {over_threshold}. "
        "Trim them — every word lands in cache_creation."
    )


# ---------------------------------------------------------------------------
# Per-session tool result cache integration (#139 §4)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_call_api_caches_get_results_per_session():
    """Identical GET-shaped calls within the same session hit the cache:
    the second call MUST NOT round-trip to the API."""
    import importlib.util
    from unittest.mock import MagicMock
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    server = module.LifeOSMCPServer()
    # Replace the httpx client with a mock so we can count call counts.
    fake = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"events": [{"title": "standup"}]}
    fake_response.raise_for_status = MagicMock()
    fake.get.return_value = fake_response
    server.client = fake

    # First call: round-trips.
    r1 = server._call_api(
        "lifeos_calendar_upcoming", {"days": 7}, session_id="sess_X"
    )
    # Second call: should hit the cache, not the HTTP client.
    r2 = server._call_api(
        "lifeos_calendar_upcoming", {"days": 7}, session_id="sess_X"
    )
    assert r1 == r2
    assert fake.get.call_count == 1, "expected exactly one HTTP call (second served from cache)"


@pytest.mark.unit
def test_call_api_does_not_cache_writes():
    """POST/PUT/DELETE tools are never cached."""
    import importlib.util
    from unittest.mock import MagicMock
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    server = module.LifeOSMCPServer()
    fake = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"id": "draft_1"}
    fake_response.raise_for_status = MagicMock()
    fake.post.return_value = fake_response
    server.client = fake

    server._call_api(
        "lifeos_gmail_draft",
        {"to": "x@y", "subject": "hi", "body": "ok"},
        session_id="sess_X",
    )
    server._call_api(
        "lifeos_gmail_draft",
        {"to": "x@y", "subject": "hi", "body": "ok"},
        session_id="sess_X",
    )
    # Both calls hit the API — POST is never cached.
    assert fake.post.call_count == 2


@pytest.mark.unit
def test_call_api_skips_cache_when_session_id_missing():
    """Without a session_id (e.g., local CLI use), the cache is bypassed."""
    import importlib.util
    from unittest.mock import MagicMock
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    server = module.LifeOSMCPServer()
    fake = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"events": []}
    fake_response.raise_for_status = MagicMock()
    fake.get.return_value = fake_response
    server.client = fake

    server._call_api("lifeos_calendar_upcoming", {"days": 7})
    server._call_api("lifeos_calendar_upcoming", {"days": 7})
    # Both calls round-trip — no session, no cache.
    assert fake.get.call_count == 2


# ---------------------------------------------------------------------------
# Investments snapshot tool (#447)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_investments_tool_curated():
    """lifeos_investments is a curated GET tool on the summary endpoint, with a
    description inside the 30-word cache budget."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = next((c for c in module.CURATED_ENDPOINTS.values()
                if c["name"] == "lifeos_investments"), None)
    assert cfg is not None, "lifeos_investments missing from CURATED_ENDPOINTS"
    assert cfg["method"] == "GET"
    assert len(cfg["description"].split()) <= 30


@pytest.mark.unit
def test_investments_format_digest():
    """_format_response renders a portfolio digest that surfaces synced_at."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LifeOSMCPServer()
    data = {
        "synced_at": "2026-07-09T03:26:00",
        "as_of": "2026-07-09",
        "totals": {
            "all_investments": 1234567, "schwab": 1000000, "external_retirement": 234567,
            "tax_buckets": {"pretax": 500000, "roth": 300000, "taxable": 434567},
        },
        "accounts": [
            {"key": "brokerage", "name": "Schwab Brokerage", "value": 1000000, "external": False},
            {"key": "401k", "name": "Guideline 401(k)", "value": 234567, "external": True},
        ],
        "positions": [
            {"symbol": "VTI", "value": 500000, "weight_pct": 40, "unrealized": 120000},
            {"symbol": "GFND", "value": 234567, "weight_pct": 19},  # external: no unrealized
        ],
        "taxable_unrealized": {"long_term": 80000, "short_term": 5000, "harvestable_losses": -2000},
    }
    out = server._format_response("lifeos_investments", data)
    assert "Investment portfolio" in out
    assert "2026-07-09T03:26:00" in out       # synced_at surfaced
    assert "$1,234,567" in out                 # total value
    assert "Schwab Brokerage" in out
    assert "(external)" in out                 # external account tagged
    assert "VTI" in out and "unrealized" in out


@pytest.mark.unit
def test_investments_not_synced_is_graceful():
    """A missing snapshot yields a friendly message, not an 'Error:' string."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LifeOSMCPServer()
    out = server._format_response(
        "lifeos_investments",
        {"error": 'API error 404: {"detail":"summary.json not synced yet — run the macbook refresh"}'},
    )
    assert not out.startswith("Error:")
    assert "not synced yet" in out.lower()


@pytest.mark.unit
def test_investments_format_tolerates_null_fields():
    """A partial snapshot — present-but-null numbers, or a whole null section —
    degrades to $0 instead of raising (external accounts can lack figures, and a
    partial write can null a section)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LifeOSMCPServer()
    # null numeric leaves within present sections
    data = {
        "synced_at": "2026-07-09T03:26:00", "as_of": "2026-07-09",
        "totals": {"all_investments": None, "schwab": None, "external_retirement": None,
                   "tax_buckets": {"pretax": None, "roth": None, "taxable": None}},
        "accounts": [{"key": "401k", "name": "Guideline 401(k)", "value": None, "external": True}],
        "positions": [{"symbol": "GFND", "value": None, "weight_pct": None}],
        "taxable_unrealized": {"long_term": None, "short_term": None, "harvestable_losses": None},
    }
    out = server._format_response("lifeos_investments", data)  # must not raise
    assert "Investment portfolio" in out
    assert "$0" in out
    assert "None" not in out
    # whole null sections (plausible on a partial write)
    out2 = server._format_response(
        "lifeos_investments",
        {"synced_at": "x", "as_of": "y", "totals": None, "accounts": None,
         "positions": None, "taxable_unrealized": None},
    )
    assert "Investment portfolio" in out2  # must not raise


@pytest.mark.unit
def test_investments_route_404_matches_mcp_not_synced_branch(tmp_path, monkeypatch):
    """The MCP graceful branch keys on 'not synced' in the route's 404 detail;
    guard that cross-file coupling so a reword of the route message can't
    silently drop the graceful handling."""
    from fastapi import HTTPException
    from api.routes import investments as inv_route
    monkeypatch.setattr(inv_route, "SYNC_DIR", str(tmp_path))  # empty dir → file missing
    with pytest.raises(HTTPException) as ei:
        inv_route._load("summary.json")
    assert "not synced" in str(ei.value.detail).lower()
