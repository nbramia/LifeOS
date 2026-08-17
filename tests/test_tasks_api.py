"""
Tests for the Tasks API routes.

Tests CRUD endpoints with mocked TaskManager.
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class TestTasksAPI:
    """Tests for the /api/tasks endpoints."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_task_manager(self):
        with patch("api.routes.tasks.get_task_manager") as mock:
            from api.services.task_manager import Task

            manager = mock.return_value

            sample_task = Task(
                id="abc12345",
                description="Pull 1099 from Schwab",
                status="todo",
                context="Finance",
                priority="medium",
                due_date="2025-02-15",
                created_date="2025-02-08",
                tags=["tax", "schwab"],
                source_file="LifeOS/Tasks/Finance.md",
                line_number=7,
            )

            manager.create.return_value = sample_task
            manager.get.return_value = sample_task
            manager.list_tasks.return_value = [sample_task]
            manager.update.return_value = sample_task
            manager.complete.return_value = sample_task
            manager.delete.return_value = True
            yield manager

    # --- CREATE ---

    def test_create_task(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Pull 1099 from Schwab",
            "tags": ["tax", "schwab"],
        })
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "Pull 1099 from Schwab"
        assert data["id"] == "abc12345"
        # context comes from the fixture's sample_task; the API itself no
        # longer accepts a context on create — everything lands in Inbox.
        _, kwargs = mock_task_manager.create.call_args
        assert "context" not in kwargs

    def test_create_task_ignores_context_input(self, client, mock_task_manager):
        """Stray 'context' in the request body is dropped, not forwarded."""
        response = client.post("/api/tasks", json={
            "description": "Quick task",
            "context": "Work",  # silently dropped by Pydantic
        })
        assert response.status_code == 200
        _, kwargs = mock_task_manager.create.call_args
        assert "context" not in kwargs

    def test_create_task_minimal(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Quick task",
        })
        assert response.status_code == 200
        mock_task_manager.create.assert_called_once()

    def test_create_task_empty_description(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "",
        })
        assert response.status_code in (400, 422)  # Validation error

    # --- DRY RUN (#138) ---

    def test_dry_run_with_agent_tag_returns_preflight_preview(self, client, mock_task_manager):
        """dry_run=true on an #agent task runs preflight and returns the
        routing decision + cost estimate WITHOUT creating the task."""
        from api.services.agent_worker.preflight import (
            PreflightBudget,
            PreflightResult,
            ROUTE_CLAUDE,
        )
        fake_result = PreflightResult(
            budget=PreflightBudget(wall_seconds=600, max_tokens=20_000, max_dollars=2.0),
            routing=ROUTE_CLAUDE,
            routing_reason="multi-step task; needs cloud",
            expected_output="text",
            ambiguity=None,
            sane=True,
            sane_reason="",
        )
        with patch("api.services.agent_worker.preflight.run_preflight", return_value=fake_result):
            response = client.post("/api/tasks", json={
                "description": "draft my Q4 review",
                "tags": ["agent"],
                "dry_run": True,
            })
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["routing"] == "claude"
        assert data["budget"]["max_dollars"] == 2.0
        assert data["estimated_dollars"] > 0
        # The dry-run path does NOT touch the task manager.
        mock_task_manager.create.assert_not_called()

    def test_dry_run_without_agent_tag_falls_through_to_create(self, client, mock_task_manager):
        """dry_run is a no-op for non-#agent tasks — the task is still created."""
        response = client.post("/api/tasks", json={
            "description": "shopping list",
            "dry_run": True,
        })
        assert response.status_code == 200
        # Falls through to real task creation.
        mock_task_manager.create.assert_called_once()

    def test_dry_run_false_creates_task_normally(self, client, mock_task_manager):
        """dry_run=false on an #agent task still creates it (the worker
        picks it up later and does its own preflight)."""
        response = client.post("/api/tasks", json={
            "description": "dispatch this",
            "tags": ["agent"],
            "dry_run": False,
        })
        assert response.status_code == 200
        mock_task_manager.create.assert_called_once()

    # --- LIST ---

    def test_list_tasks(self, client, mock_task_manager):
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["description"] == "Pull 1099 from Schwab"

    def test_list_tasks_with_filters(self, client, mock_task_manager):
        response = client.get("/api/tasks?status=todo&context=Finance&tag=tax")
        assert response.status_code == 200
        mock_task_manager.list_tasks.assert_called_once_with(
            status="todo",
            context="Finance",
            tag="tax",
            due_before=None,
            query=None,
        )

    def test_list_tasks_with_query(self, client, mock_task_manager):
        response = client.get("/api/tasks?query=taxes")
        assert response.status_code == 200
        mock_task_manager.list_tasks.assert_called_once_with(
            status=None,
            context=None,
            tag=None,
            due_before=None,
            query="taxes",
        )

    def test_list_tasks_empty(self, client, mock_task_manager):
        mock_task_manager.list_tasks.return_value = []
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    # --- GET ---

    def test_get_task(self, client, mock_task_manager):
        response = client.get("/api/tasks/abc12345")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abc12345"
        assert data["description"] == "Pull 1099 from Schwab"

    def test_get_task_not_found(self, client, mock_task_manager):
        mock_task_manager.get.return_value = None
        response = client.get("/api/tasks/nonexistent")
        assert response.status_code == 404

    # --- UPDATE ---

    def test_update_task(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "priority": "high",
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_update_task_status(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "status": "in_progress",
        })
        assert response.status_code == 200

    def test_update_task_not_found(self, client, mock_task_manager):
        mock_task_manager.update.return_value = None
        response = client.put("/api/tasks/nonexistent", json={
            "priority": "low",
        })
        assert response.status_code == 404

    # --- COMPLETE ---

    def test_complete_task(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345/complete")
        assert response.status_code == 200
        mock_task_manager.complete.assert_called_once_with("abc12345")

    def test_complete_task_not_found(self, client, mock_task_manager):
        mock_task_manager.complete.return_value = None
        response = client.put("/api/tasks/nonexistent/complete")
        assert response.status_code == 404

    # --- TAGS ---

    def test_list_tags(self, client, mock_task_manager):
        mock_task_manager.list_tags.return_value = [
            {"tag": "work", "count": 3},
            {"tag": "urgent", "count": 1},
        ]
        response = client.get("/api/tasks/tags")
        assert response.status_code == 200
        assert response.json() == {
            "tags": [
                {"tag": "work", "count": 3},
                {"tag": "urgent", "count": 1},
            ]
        }

    def test_list_tags_empty(self, client, mock_task_manager):
        mock_task_manager.list_tags.return_value = []
        response = client.get("/api/tasks/tags")
        assert response.status_code == 200
        assert response.json() == {"tags": []}

    # --- DELETE ---

    def test_delete_task(self, client, mock_task_manager):
        response = client.delete("/api/tasks/abc12345")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        assert data["id"] == "abc12345"

    def test_delete_task_not_found(self, client, mock_task_manager):
        mock_task_manager.delete.return_value = False
        response = client.delete("/api/tasks/nonexistent")
        assert response.status_code == 404

    # --- RESPONSE SHAPE ---

    def test_task_response_shape(self, client, mock_task_manager):
        """Verify all expected fields are present in task response."""
        response = client.get("/api/tasks/abc12345")
        data = response.json()
        expected_fields = [
            "id", "description", "status", "context", "priority",
            "due_date", "created_date", "done_date", "cancelled_date",
            "tags", "reminder_id", "source_file", "line_number",
        ]
        for f in expected_fields:
            assert f in data, f"Missing field: {f}"


class TestListTasksParameterDocs:
    """The MCP tool schema for `lifeos_task_list` is generated from this route's
    OpenAPI spec (mcp_server._build_input_schema). Bare `Optional[str] = None`
    params produce the placeholder "Query parameter: <name>", which tells a model
    nothing about valid values — so it guesses a context (0 rows) or omits status
    (every status, mostly done/cancelled) and reports a partial list as complete.
    """

    @pytest.fixture
    def params(self):
        from api.main import app
        spec = app.openapi()["paths"]["/api/tasks"]["get"]["parameters"]
        return {p["name"]: p.get("description", "") for p in spec}

    def test_no_param_falls_back_to_placeholder_description(self, params):
        assert set(params) == {"status", "context", "tag", "due_before", "query"}
        for name, desc in params.items():
            assert desc, f"{name} has no description; MCP would emit a placeholder"
            assert "Query parameter:" not in desc

    def test_status_description_enumerates_valid_values(self, params):
        desc = params["status"]
        for value in ("todo", "done", "in_progress", "cancelled",
                      "deferred", "blocked", "urgent"):
            assert value in desc
        # The failure mode was omitting status entirely and summarising everything.
        assert "todo" in desc and "omit" in desc.lower()

    def test_context_description_warns_unused_values_return_nothing(self, params):
        desc = params["context"].lower()
        assert "zero" in desc or "no tasks" in desc
        assert "inbox" in desc

    def test_fallback_schema_does_not_invent_context_values(self):
        """The offline fallback must not advertise contexts that no vault has."""
        import mcp_server
        schema = mcp_server.LifeOSMCPServer._fallback_schemas(
            mcp_server.LifeOSMCPServer.__new__(mcp_server.LifeOSMCPServer)
        )["lifeos_task_list"]
        context_desc = schema["properties"]["context"]["description"]
        assert "Work, Personal" not in context_desc
        assert "Inbox" in context_desc
