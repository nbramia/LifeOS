"""
Tests for the workout MCP surface (#603).

The fitness persona's core instruction — log first, report after, never ask
for confirmation before logging — was previously uncarryable through the MCP
surface: `manage_workouts` had no exposed equivalent under any name. This
covers the new `POST /api/fitness/workouts` endpoint and its curated MCP
tool `lifeos_workout_manage`:

1. The tool is registered and discoverable on the MCP surface.
2. A workout logged through the MCP/REST path lands in the exact same
   FitnessStore a natively-logged one does (asserting the stored row, not
   just a success response — the failure mode at issue is a false "Logged").
3. No other curated tool or persona's tool availability changed.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.services.fitness_store as fs
from api.services.fitness_store import FitnessStore
from api.routes import fitness as fitness_route

pytestmark = pytest.mark.unit

MCP_SERVER_PATH = Path(__file__).parent.parent / "mcp_server.py"


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _server_built_from_live_spec():
    """A LifeOSMCPServer with `.tools` built from this checkout's live
    OpenAPI spec, in-process — no HTTP round-trip, no running server."""
    from api.main import app
    openapi_spec = app.openapi()
    module = _load_mcp_module()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    server.openapi_spec = openapi_spec
    server._build_tools_from_spec()
    return server


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    monkeypatch.setattr(fs, "_store_instance", store)  # get_fitness_store() -> this
    app = FastAPI()
    app.include_router(fitness_route.router)
    client = TestClient(app)
    return client, store


# ---------------------------------------------------------------------------
# 1. Registered and discoverable on the MCP surface
# ---------------------------------------------------------------------------

class TestMCPRegistration:
    def test_curated_endpoint_present(self):
        module = _load_mcp_module()
        cfg = next(
            (c for c in module.CURATED_ENDPOINTS.values() if c["name"] == "lifeos_workout_manage"),
            None,
        )
        assert cfg is not None, "lifeos_workout_manage missing from CURATED_ENDPOINTS"
        assert cfg["method"] == "POST"
        assert cfg.get("path", "").endswith("/api/fitness/workouts")

    def test_tool_discoverable_with_action_required(self):
        """Built from this checkout's live OpenAPI spec (in-process, no server)."""
        from api.main import app
        openapi_spec = app.openapi()

        module = _load_mcp_module()
        with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
            server = module.LifeOSMCPServer()
        server.openapi_spec = openapi_spec
        server._build_tools_from_spec()

        tool = next((t for t in server.tools if t["name"] == "lifeos_workout_manage"), None)
        assert tool is not None, "lifeos_workout_manage not discovered from the OpenAPI spec"
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert schema.get("required") == ["action"]

    def test_format_response_passes_result_through_as_plain_text(self):
        """The formatter should echo the tool's own confirmation/error string
        rather than re-wrapping it as JSON, matching what the native
        orchestrator sees from the same dispatcher."""
        module = _load_mcp_module()
        server = module.LifeOSMCPServer()
        formatted = server._format_response(
            "lifeos_workout_manage",
            {"result": "Logged — 2026-06-07: Bench Press 135×8 (session id: abc123)"},
        )
        assert formatted == "Logged — 2026-06-07: Bench Press 135×8 (session id: abc123)"


# ---------------------------------------------------------------------------
# 2. Same store as the native path
# ---------------------------------------------------------------------------

class TestSharedStore:
    def test_log_via_route_writes_a_real_row(self, env):
        """The write must be real, not just a success string — check the
        store directly rather than trusting the response body."""
        client, store = env
        resp = client.post(
            "/api/fitness/workouts",
            json={"action": "log", "sets": [{"exercise": "bench", "reps": 8, "weight": 135}]},
        )
        assert resp.status_code == 200
        assert resp.json()["result"].startswith("Logged")

        session = store.get_latest_session()
        assert session is not None
        assert session.sets[0].exercise == "Bench Press"
        assert session.sets[0].reps == 8
        assert session.sets[0].weight == 135

    def test_lands_in_same_store_the_native_tool_reads(self, env):
        """A session logged via the route must be visible to the exact
        dispatcher the native orchestrator calls — proving both paths share
        one store rather than two independent ones."""
        client, store = env
        client.post(
            "/api/fitness/workouts",
            json={"action": "log", "sets": [{"exercise": "squats", "reps": 5, "weight": 185}]},
        )
        from api.services.agent_tools import _tool_manage_workouts
        out = _tool_manage_workouts({"action": "history", "exercise": "squats"})
        assert store.get_latest_session().id in out

    def test_log_without_sets_is_a_real_error_not_a_false_confirmation(self, env):
        """No path may report a workout as logged without a write. An empty
        `sets` must come back as a 4xx, not a structurally-successful 200
        with an 'Error: ...' string buried in the body (#603 review MAJOR —
        a 200 there is legible only to a caller that reads the prose, which
        is exactly the false-confirmation shape this issue exists to close)."""
        client, store = env
        resp = client.post("/api/fitness/workouts", json={"action": "log", "sets": []})
        assert 400 <= resp.status_code < 500
        assert "Error" in resp.json()["detail"]
        assert store.list_sessions() == []

    def test_update_targets_the_session_the_route_just_logged(self, env):
        client, store = env
        client.post(
            "/api/fitness/workouts",
            json={"action": "log", "sets": [{"exercise": "bench", "reps": 8, "weight": 135}]},
        )
        resp = client.post(
            "/api/fitness/workouts",
            json={"action": "update", "sets": [{"exercise": "bench", "reps": 8, "weight": 145}]},
        )
        assert resp.json()["result"].startswith("Updated")
        assert store.get_latest_session().sets[0].weight == 145

    def test_readiness_action_reachable_over_the_route(self, env):
        """The persona's recommendation flow also needs `readiness`, not
        just `log` — confirm the full action set is reachable, not a
        logging-only subset."""
        client, _ = env
        resp = client.post("/api/fitness/workouts", json={"action": "readiness"})
        assert resp.status_code == 200
        assert "Readiness snapshot" in resp.json()["result"]


# ---------------------------------------------------------------------------
# 3. Nothing else moved
# ---------------------------------------------------------------------------

class TestNoUnrelatedChange:
    def test_curated_endpoint_count(self):
        """Pins the new total (59 = the pre-existing 56 [55 + this one] plus
        the 3 Human-queue tools added by #852) so a future change to this
        count is a deliberate, reviewed edit."""
        module = _load_mcp_module()
        assert len(module.CURATED_ENDPOINTS) == 59

    def test_other_tools_unaffected(self):
        module = _load_mcp_module()
        names = {c["name"] for c in module.CURATED_ENDPOINTS.values()}
        # Spot-check a sample from unrelated categories — none of this PR's
        # changes should have touched their registration.
        for expected in ("lifeos_task_create", "lifeos_calendar_upcoming", "lifeos_monarch_accounts"):
            assert expected in names

    def test_agentic_loop_tool_count_unchanged(self):
        """manage_workouts already existed in the native agentic-loop tool
        set (#320) — this PR only adds an MCP-side route to it, so the
        agentic-loop tool count must not move.

        Pinned total: 22 = the pre-existing 21 + `manage_human_queue`,
        added by #852. A future change to this count should be deliberate
        and reviewed, same as the CURATED_ENDPOINTS count above."""
        from api.services.agent_tools import TOOL_DEFINITIONS
        assert len(TOOL_DEFINITIONS) == 22


# ---------------------------------------------------------------------------
# 4. Generated schema reports real types, not the anyOf-null fallback
# ---------------------------------------------------------------------------

class TestSchemaTypeAccuracy:
    """#603 review (MAJOR): FastAPI/Pydantic v2 renders every Optional field
    as `anyOf: [<real schema>, {"type": "null"}]` with no top-level `type`.
    The old generator read only the top-level `type` and silently defaulted
    to "string", so `sets` (a JSON array of objects) and `limit` (an integer)
    were both advertised to MCP clients as `"type": "string"`. A schema-
    following external client could not construct the array a workout log
    needs — the capability looked present on the tool list and was actually
    unusable, which alone defeats the point of this issue."""

    def test_sets_reports_array_with_items(self):
        server = _server_built_from_live_spec()
        tool = next(t for t in server.tools if t["name"] == "lifeos_workout_manage")
        sets_schema = tool["inputSchema"]["properties"]["sets"]
        assert sets_schema["type"] == "array"
        assert "items" in sets_schema  # the per-entry shape must survive, not just the outer type

    def test_limit_reports_integer(self):
        server = _server_built_from_live_spec()
        tool = next(t for t in server.tools if t["name"] == "lifeos_workout_manage")
        assert tool["inputSchema"]["properties"]["limit"]["type"] == "integer"

    def test_required_action_field_still_reports_string(self):
        """`action` has no Optional wrapper (it's required) — confirm the
        anyOf-unwrap didn't regress the already-correct, non-nullable case."""
        server = _server_built_from_live_spec()
        tool = next(t for t in server.tools if t["name"] == "lifeos_workout_manage")
        assert tool["inputSchema"]["properties"]["action"]["type"] == "string"

    def test_other_curated_tool_optional_array_also_fixed(self):
        """The same anyOf-null shape affects other curated tools' Optional
        array fields (e.g. task tags) — confirm the generator fix is general,
        not special-cased to fitness."""
        server = _server_built_from_live_spec()
        tool = next(t for t in server.tools if t["name"] == "lifeos_task_create")
        tags_schema = tool["inputSchema"]["properties"].get("tags")
        assert tags_schema is not None
        assert tags_schema["type"] == "array"


# ---------------------------------------------------------------------------
# 5. No persona instruction leaked into the tool description
# ---------------------------------------------------------------------------

class TestNoPersonaLeakInDescription:
    """#603 review (MAJOR): `ToolRegistry` exposes the full curated catalog
    to every persona, so a persona-specific instruction ("log first, never
    ask for confirmation") baked into this tool's *description* would change
    behavior for every non-fitness persona too. That instruction already
    lives in config/personas/fitness.md, where it applies to the right
    surface — the MCP description must stay capability-only."""

    def test_description_has_no_confirmation_instruction(self):
        module = _load_mcp_module()
        cfg = next(c for c in module.CURATED_ENDPOINTS.values() if c["name"] == "lifeos_workout_manage")
        desc = cfg["description"].lower()
        assert "confirm" not in desc
        assert "never ask" not in desc

    def test_instruction_still_lives_in_the_fitness_persona(self):
        persona_path = Path(__file__).parent.parent / "config" / "personas" / "fitness.md"
        text = persona_path.read_text().lower()
        assert "never ask for confirmation" in text
