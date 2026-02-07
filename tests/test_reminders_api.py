"""
Tests for the Reminders API routes.

Tests CRUD endpoints with mocked store and Telegram.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.unit


class TestRemindersAPI:
    """Tests for the /api/reminders endpoints."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_reminder_store(self):
        with patch("api.routes.reminders.get_reminder_store") as mock:
            from api.services.reminder_store import Reminder

            store = mock.return_value

            sample_reminder = Reminder(
                id="rem-123",
                name="Morning Briefing",
                schedule_type="cron",
                schedule_value="30 7 * * 1-5",
                message_type="prompt",
                message_content="Summarize my meetings today",
                enabled=True,
                created_at=datetime.now(timezone.utc).isoformat(),
                next_trigger_at=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            )

            store.create.return_value = sample_reminder
            store.list_all.return_value = [sample_reminder]
            store.get.return_value = sample_reminder
            store.update.return_value = sample_reminder
            store.delete.return_value = True
            yield store

    def test_create_reminder(self, client, mock_reminder_store):
        response = client.post("/api/reminders", json={
            "name": "Morning Briefing",
            "schedule_type": "cron",
            "schedule_value": "30 7 * * 1-5",
            "message_type": "prompt",
            "message_content": "Summarize my meetings today",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Morning Briefing"
        assert data["id"] == "rem-123"

    def test_create_reminder_invalid_schedule_type(self, client, mock_reminder_store):
        response = client.post("/api/reminders", json={
            "name": "Bad",
            "schedule_type": "weekly",
            "schedule_value": "monday",
            "message_type": "static",
            "message_content": "Hi",
        })
        assert response.status_code == 400

    def test_create_reminder_invalid_message_type(self, client, mock_reminder_store):
        response = client.post("/api/reminders", json={
            "name": "Bad",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
            "message_type": "webhook",
            "message_content": "Hi",
        })
        assert response.status_code == 400

    def test_list_reminders(self, client, mock_reminder_store):
        response = client.get("/api/reminders")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["reminders"]) == 1

    def test_get_reminder(self, client, mock_reminder_store):
        response = client.get("/api/reminders/rem-123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "rem-123"

    def test_get_reminder_not_found(self, client, mock_reminder_store):
        mock_reminder_store.get.return_value = None
        response = client.get("/api/reminders/nonexistent")
        assert response.status_code == 404

    def test_update_reminder(self, client, mock_reminder_store):
        response = client.put("/api/reminders/rem-123", json={
            "name": "Updated Name",
        })
        assert response.status_code == 200

    def test_update_reminder_not_found(self, client, mock_reminder_store):
        mock_reminder_store.update.return_value = None
        response = client.put("/api/reminders/nonexistent", json={
            "name": "X",
        })
        assert response.status_code == 404

    def test_delete_reminder(self, client, mock_reminder_store):
        response = client.delete("/api/reminders/rem-123")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"

    def test_delete_reminder_not_found(self, client, mock_reminder_store):
        mock_reminder_store.delete.return_value = False
        response = client.delete("/api/reminders/nonexistent")
        assert response.status_code == 404

    @patch("api.services.telegram.send_message_async", new_callable=AsyncMock, return_value=True)
    @patch("config.settings.settings")
    def test_send_adhoc_message(self, mock_settings, mock_send, client):
        mock_settings.telegram_enabled = True
        response = client.post("/api/reminders/send", json={
            "text": "Test message",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "sent"

    @patch("config.settings.settings")
    def test_send_adhoc_not_configured(self, mock_settings, client):
        mock_settings.telegram_enabled = False
        response = client.post("/api/reminders/send", json={
            "text": "Test message",
        })
        assert response.status_code == 400
