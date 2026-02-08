"""
Tests for the Tasks API routes.

Tests CRUD endpoints with mocked TaskManager.
"""
import pytest
from unittest.mock import patch, MagicMock

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
            "context": "Finance",
            "tags": ["tax", "schwab"],
        })
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "Pull 1099 from Schwab"
        assert data["id"] == "abc12345"
        assert data["context"] == "Finance"

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
