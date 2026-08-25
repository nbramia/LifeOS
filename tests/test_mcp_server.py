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


@pytest.mark.requires_server
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


@pytest.mark.unit
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

    def test_task_create_tags_advertised_as_array(self, openapi_spec):
        """Regression for #609: the incident that prompted this issue traced
        back to `lifeos_task_create`'s `tags` field being advertised as
        `"type": "string"` before #603's `_unwrap_optional` fix. A
        schema-following model sent a string, the write 400'd, and (per the
        incident) the failure wasn't surfaced as such. `_unwrap_optional` is
        exactly the code that could regress and reintroduce the mistyping —
        pin the schema shape directly so a regression is caught here instead
        of by another incident.
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

        task_create_tool = next(t for t in server.tools if t["name"] == "lifeos_task_create")
        tags_schema = task_create_tool["inputSchema"]["properties"]["tags"]
        assert tags_schema.get("type") == "array", (
            f"lifeos_task_create's 'tags' field is advertised as "
            f"{tags_schema.get('type')!r}, not 'array' — a schema-following "
            f"model will send the wrong shape and the write will 400"
        )
        assert "items" in tags_schema, "'tags' array schema is missing 'items'"


@pytest.mark.requires_server
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


@pytest.mark.unit
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


@pytest.mark.unit
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
def test_investments_format_lists_all_positions():
    """The MCP digest must include a beyond-top-15 holding (regression for #452,
    where SPCX at rank 44 was silently dropped by a [:15] cap). Mirrors the
    search_finances 'investments' digest, so header/format stay consistent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LifeOSMCPServer()
    positions = [
        {"symbol": f"FIL{i:02d}", "value": 100000 - i * 1000, "weight_pct": 5,
         "unrealized": 1000}
        for i in range(19)
    ]
    positions.append({"symbol": "SPCX", "desc": "SpaceX Class A (SPV)",
                      "value": 3050, "weight_pct": 0.37})
    data = {
        "synced_at": "2026-07-09T03:26:00", "as_of": "2026-07-09",
        "totals": {"all_investments": 1234567, "schwab": 1000000,
                   "external_retirement": 234567,
                   "tax_buckets": {"pretax": 500000, "roth": 300000, "taxable": 434567}},
        "accounts": [{"key": "brokerage", "name": "Synthetic Brokerage",
                      "value": 1000000, "external": False}],
        "positions": positions,
        "taxable_unrealized": {"long_term": 80000, "short_term": 5000,
                               "harvestable_losses": -2000},
    }
    out = server._format_response("lifeos_investments", data)
    assert "SPCX" in out                 # beyond-top-15 holding is present
    assert "Positions (20):" in out      # header reflects the full count
    assert "Top positions:" not in out   # old truncating header is gone
    # Ticker + security name, so company-name questions are a text match
    # (stale world knowledge otherwise overrides an unrecognized ticker).
    assert "SPCX — SpaceX Class A (SPV)" in out
    assert "FIL01:" in out               # desc-less positions keep bare ticker


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


# ---------------------------------------------------------------------------
# Turn-context tool (#591)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_turn_context_tool_curated_and_registered(openapi_spec, monkeypatch):
    """lifeos_turn_context is a curated GET tool exposing the #591 per-turn
    context endpoint, with a description telling clients to read it at the
    start of a turn, and it appears on the built tool surface.

    Builds the server against the in-process OpenAPI spec (this checkout's
    code), not whatever's live on localhost:8000 — a running server may
    still be on pre-#591 code and wouldn't have this path yet.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = next((c for c in module.CURATED_ENDPOINTS.values()
                if c["name"] == "lifeos_turn_context"), None)
    assert cfg is not None, "lifeos_turn_context missing from CURATED_ENDPOINTS"
    assert cfg["method"] == "GET"
    assert len(cfg["description"].split()) <= 30
    assert "start of" in cfg["description"].lower()

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return openapi_spec

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(module.httpx, "Client", _FakeClient)
    server = module.LifeOSMCPServer()
    tool = next((t for t in server.tools if t["name"] == "lifeos_turn_context"), None)
    assert tool is not None, "lifeos_turn_context not built onto the MCP tool surface"


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


# ---------------------------------------------------------------------------
# _handle_sync_trigger (#609) — the custom_handler for lifeos_sync_trigger.
# It bypasses the generic _call_api HTTP wrapper, so its own error-signaling
# needs its own coverage rather than inheriting the generic tests above.
# ---------------------------------------------------------------------------

def _fresh_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LifeOSMCPServer()


@pytest.mark.unit
def test_sync_trigger_missing_source_is_an_error():
    result = _fresh_server()._handle_sync_trigger({})
    assert "error" in result


@pytest.mark.unit
def test_sync_trigger_unknown_source_is_an_error():
    result = _fresh_server()._handle_sync_trigger({"source": "not-a-real-source"})
    assert "error" in result


@pytest.mark.unit
def test_sync_trigger_known_source_routes_to_the_right_endpoint():
    from unittest.mock import MagicMock
    server = _fresh_server()
    fake = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"status": "started"}
    fake_response.raise_for_status = MagicMock()
    fake.post.return_value = fake_response
    server.client = fake

    result = server._handle_sync_trigger({"source": "vault"})
    assert result == {"status": "started"}
    called_url = fake.post.call_args[0][0]
    assert called_url.endswith("/api/admin/reindex")


@pytest.mark.unit
def test_sync_trigger_downstream_non_2xx_is_an_error():
    """A downstream route that correctly raises on failure (the safe
    pattern every other curated write endpoint follows) must still surface
    as {"error": ...} here — this is what lets dispatch() set `isError`
    generically for every sync source without source-specific handling."""
    import httpx
    from unittest.mock import MagicMock
    server = _fresh_server()
    fake = MagicMock()
    fake_response = MagicMock(status_code=500)
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=fake_response
    )
    fake.post.return_value = fake_response
    server.client = fake

    result = server._handle_sync_trigger({"source": "vault"})
    assert "error" in result


@pytest.mark.unit
def test_sync_trigger_2xx_with_embedded_error_sets_is_error():
    """Defense in depth, generically: even if some future sync source ever
    reported a failure as a 200 carrying only a top-level `error` key (as
    `POST /api/admin/calendar/sync` and `POST /api/photos/sync` did before
    #614 additionally made a total failure non-2xx), `_handle_sync_trigger`
    passes a 2xx body through unmodified and needs no source-specific
    handling — a 200 whose body already carries `error` must still flip
    `dispatch()`'s `isError`, exactly like
    `test_tools_call_sets_is_error_on_tool_failure` in
    test_mcp_http_transport.py proves for the generic (non-sync-trigger)
    case."""
    import importlib.util
    from unittest.mock import MagicMock
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LifeOSMCPServer.__new__(module.LifeOSMCPServer)
    fake = MagicMock()
    fake_response = MagicMock(status_code=200)
    fake_response.raise_for_status = MagicMock()  # 2xx: does not raise
    fake_response.json.return_value = {
        "status": "error",
        "events_indexed": 0,
        "errors": ["calendar API unreachable"],
        "elapsed_seconds": 0,
        "last_sync": "",
        "error": "calendar API unreachable",
    }
    fake.post.return_value = fake_response
    server.client = fake

    result = module.dispatch(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lifeos_sync_trigger", "arguments": {"source": "calendar"}},
        },
    )
    assert result["result"]["isError"] is True


# ---------------------------------------------------------------------------
# #609: generic, code-driven safety net over every curated write endpoint.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestWriteEndpointNeverReturnsSuccessShapedFailure:
    """Enumerates write endpoints from the code — CURATED_ENDPOINTS plus the
    sub-routes `_handle_sync_trigger` fans `lifeos_sync_trigger` out to,
    since that tool has no direct path of its own — rather than hardcoding
    today's list, so a newly added write endpoint that returns a failure
    inside a 2xx gets caught here without needing a new test.

    Flags any `except` block in a route handler that returns normally
    (implicit or explicit 2xx) instead of raising or setting an explicit
    non-2xx status — the exact shape #603 fixed in `fitness.py` and #609
    found in two sync routes. This is a static approximation of the real
    guarantee, not a substitute for the failure-injection tests elsewhere in
    this suite (e.g. `test_create_task_failure_is_never_success_shaped`) —
    it exists so *future* endpoints get some coverage by construction.
    """

    # No current exemptions: `POST /api/admin/calendar/sync` and
    # `POST /api/photos/sync` were fixed for #609 by adding a top-level
    # `error` key to their failure body, and #614 additionally made a total
    # failure return a non-2xx status (see docs/specs/technical/
    # architecture.md, "Write Endpoint Failure Contract") — both signals are
    # present now, which this scanner recognizes as safe either way.
    # Add an entry here (with a tracking issue and a doc note) only for a
    # deliberately accepted gap — never to silence a finding.
    _KNOWN_EXEMPTIONS: set[tuple[str, str]] = set()

    @staticmethod
    def _load_mcp_module():
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _curated_write_paths(self, module):
        """(method, path) pairs for every write endpoint reachable through
        the MCP surface."""
        from unittest.mock import MagicMock

        pairs = set()
        for key, config in module.CURATED_ENDPOINTS.items():
            if config["method"] == "GET" or config.get("custom_handler"):
                continue
            pairs.add((config["method"], config.get("path", key.split(":")[0])))

        # lifeos_sync_trigger has no direct path — _handle_sync_trigger fans
        # it out at runtime based on `source`. Drive the real routing logic
        # for every valid source instead of re-typing its route_map, so a
        # change there is picked up automatically.
        server = module.LifeOSMCPServer.__new__(module.LifeOSMCPServer)
        for source in (
            "vault", "calendar", "contacts", "slack", "photos",
            "gmail", "imessage", "phone", "facetime", "linkedin",
        ):
            fake_client = MagicMock()
            server.client = fake_client
            server._handle_sync_trigger({"source": source})
            url = fake_client.post.call_args[0][0]
            path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
            pairs.add(("POST", path))
        return pairs

    @staticmethod
    def _resolve_route(method: str, curated_path: str):
        """Find the FastAPI route matching `curated_path`, treating any
        route path parameter as a wildcard — curated paths sometimes name a
        param differently than the route itself (e.g. `{entity_id}` in
        CURATED_ENDPOINTS vs `{person_id}` on the actual CRM route), and a
        sync-trigger URL carries a concrete value (e.g. `gmail`) where the
        route has a param (`{source_type}`)."""
        from api.main import app

        curated_segments = curated_path.strip("/").split("/")
        for route in app.routes:
            if method not in getattr(route, "methods", set()):
                continue
            route_segments = route.path.strip("/").split("/")
            if len(route_segments) != len(curated_segments):
                continue
            if all(
                a == b or b.startswith("{")
                for a, b in zip(curated_segments, route_segments)
            ):
                return route
        return None

    @staticmethod
    def _except_blocks_return_success_shaped(func) -> list[str]:
        """Describe every `except` block in `func` that returns normally
        without either raising, setting an explicit non-2xx status, or
        carrying a top-level `error` key in the response body — the AC's
        "non-2xx status *or* top-level error" (#609 review discussion),
        which is what let #609 fix the two sync-trigger routes by adding an
        additive `error` key instead of changing their status code."""
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(func))
        func_node = ast.parse(src).body[0]

        def is_explicit_error_status(value_node) -> bool:
            if not isinstance(value_node, ast.Call):
                return False
            callee = value_node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
            if name == "HTTPException":
                return True
            if name in ("JSONResponse", "Response", "PlainTextResponse", "ORJSONResponse"):
                return any(kw.arg == "status_code" for kw in value_node.keywords)
            return False

        def dict_has_top_level_error_key(d) -> bool:
            return isinstance(d, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "error"
                for k in d.keys if k is not None
            )

        def has_top_level_error_key(value_node) -> bool:
            """True for a bare `{"error": ...}` dict literal, or one passed
            as `content=`/positionally to a Response-constructing call (e.g.
            `JSONResponse(content={"error": ..., ...})`). Deliberately
            top-level only — a dict literal nested inside another kwarg
            (e.g. the old `SyncResponse(stats={"error": ...})` shape) does
            NOT count, since that key wasn't visible to a caller checking
            the body's own top level."""
            if dict_has_top_level_error_key(value_node):
                return True
            if isinstance(value_node, ast.Call):
                candidates = list(value_node.args) + [
                    kw.value for kw in value_node.keywords if kw.arg in (None, "content")
                ]
                return any(dict_has_top_level_error_key(c) for c in candidates)
            return False

        findings = []
        for node in ast.walk(func_node):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if any(isinstance(n, ast.Raise) for n in ast.walk(node)):
                continue  # some branch of this handler raises — treat as safe
            for n in ast.walk(node):
                if isinstance(n, ast.Return) and n.value is not None:
                    if not is_explicit_error_status(n.value) and not has_top_level_error_key(n.value):
                        findings.append(ast.dump(n.value)[:120])
        return findings

    def test_no_curated_write_endpoint_swallows_a_failure_into_a_2xx(self):
        module = self._load_mcp_module()
        pairs = self._curated_write_paths(module)
        assert pairs, "no curated write endpoints discovered — enumeration is broken"

        unexpected_unsafe = []
        for method, path in pairs:
            route = self._resolve_route(method, path)
            if route is None:
                continue
            findings = self._except_blocks_return_success_shaped(route.endpoint)
            if findings and (method, path) not in self._KNOWN_EXEMPTIONS \
                    and (method, route.path) not in self._KNOWN_EXEMPTIONS:
                unexpected_unsafe.append((method, route.path, findings))

        assert not unexpected_unsafe, (
            "curated write endpoint(s) return a failure inside a 2xx (the "
            f"#603/#609 anti-pattern): {unexpected_unsafe}. If this is "
            "intentional, add it to _KNOWN_EXEMPTIONS with a tracking issue "
            "and document the exemption in "
            "docs/specs/technical/architecture.md."
        )

    def test_known_exemptions_are_still_exempt_and_documented(self):
        """Guards the exemption list itself: once a listed exemption's route
        is fixed to raise instead of returning success-shaped, this fails —
        so the entry (and the doc's exemption note) gets removed instead of
        silently going stale."""
        for method, path in self._KNOWN_EXEMPTIONS:
            route = self._resolve_route(method, path)
            assert route is not None, (
                f"exempted route {method} {path} no longer exists — remove "
                "the stale exemption"
            )
            findings = self._except_blocks_return_success_shaped(route.endpoint)
            assert findings, (
                f"{method} {path} no longer matches the exempted anti-pattern "
                "— remove it from _KNOWN_EXEMPTIONS and its note in "
                "docs/specs/technical/architecture.md"
            )
