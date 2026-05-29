"""
Tests for the Scheduler API routes (/api/scheduler) and the agent-tools
manage_schedules wrapper — the renamed surface from #246.

CRUD is tested in-process via TestClient with a mocked store; the agent-tools
path is tested against a real store on a temp vault.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

pytestmark = pytest.mark.unit


def _sample_entry(**overrides):
    from api.services.scheduler_store import ScheduleEntry
    fields = dict(
        id="sch-1", name="Weekly review", schedule_type="cron",
        schedule_value="0 9 * * 6", action="agent", message_type="prompt",
        message_content="Draft my weekly review", executor="cloud", enabled=True,
        created_at=datetime.now(timezone.utc).isoformat(),
        next_trigger_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        last_status="",
    )
    fields.update(overrides)
    return ScheduleEntry(**fields)


class TestSchedulerAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_store(self):
        with patch("api.routes.scheduler.get_scheduler_store") as mock:
            store = mock.return_value
            entry = _sample_entry()
            store.create.return_value = entry
            store.list_all.return_value = [entry]
            store.get.return_value = entry
            store.update.return_value = entry
            store.delete.return_value = True
            yield store

    def test_create_schedule_with_action(self, client, mock_store):
        resp = client.post("/api/scheduler", json={
            "name": "Weekly review", "schedule_type": "cron",
            "schedule_value": "0 9 * * 6", "action": "agent",
            "executor": "cloud", "message_content": "Draft my weekly review",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "agent"
        assert data["executor"] == "cloud"
        # The store received the action + executor.
        kwargs = mock_store.create.call_args.kwargs
        assert kwargs["action"] == "agent"
        assert kwargs["executor"] == "cloud"

    def test_create_defaults_action_from_message_type(self, client, mock_store):
        client.post("/api/scheduler", json={
            "name": "Ping", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "message_type": "static", "message_content": "hi",
        })
        assert mock_store.create.call_args.kwargs["action"] == "notify"

    def test_create_invalid_schedule_type(self, client, mock_store):
        resp = client.post("/api/scheduler", json={
            "name": "X", "schedule_type": "weekly", "schedule_value": "x", "action": "notify",
        })
        assert resp.status_code == 400

    def test_create_invalid_action(self, client, mock_store):
        resp = client.post("/api/scheduler", json={
            "name": "X", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "action": "explode",
        })
        assert resp.status_code == 400

    def test_list_schedules(self, client, mock_store):
        resp = client.get("/api/scheduler")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["schedules"][0]["action"] == "agent"

    def test_get_schedule(self, client, mock_store):
        resp = client.get("/api/scheduler/sch-1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "sch-1"

    def test_get_schedule_not_found(self, client, mock_store):
        mock_store.get.return_value = None
        assert client.get("/api/scheduler/nope").status_code == 404

    def test_update_schedule(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"name": "Renamed"})
        assert resp.status_code == 200

    def test_update_not_found(self, client, mock_store):
        mock_store.update.return_value = None
        assert client.put("/api/scheduler/nope", json={"name": "x"}).status_code == 404

    def test_delete_schedule(self, client, mock_store):
        resp = client.delete("/api/scheduler/sch-1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_delete_not_found(self, client, mock_store):
        mock_store.delete.return_value = False
        assert client.delete("/api/scheduler/nope").status_code == 404


class TestReminderAliasStillWorks:
    """The legacy /api/reminders surface must keep functioning."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_reminders_create_still_works(self, client):
        with patch("api.routes.reminders.get_reminder_store") as mock:
            mock.return_value.create.return_value = _sample_entry(action="notify", message_type="static")
            resp = client.post("/api/reminders", json={
                "name": "Legacy", "schedule_type": "cron", "schedule_value": "0 9 * * *",
                "message_type": "static", "message_content": "hi",
            })
        assert resp.status_code == 200


class TestManageSchedulesAgentTool:
    """The chat orchestrator creates schedules (incl. action:agent) via manage_schedules."""

    def test_create_agent_schedule_end_to_end(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "create",
                "name": "Weekly review",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * 6",
                "schedule_action": "agent",
                "executor": "cloud",
                "message_content": "Draft my weekly review",
            })
        assert "Schedule created" in out
        created = store.list_all()
        assert len(created) == 1
        assert created[0].action == "agent"
        assert created[0].executor == "cloud"

    def test_list_schedules_tool(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        store.create(name="N", schedule_type="cron", schedule_value="0 9 * * *",
                     action="notify", message_type="static", message_content="x")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({"action": "list"})
        assert "\"N\"" in out
        assert "notify" in out

    def test_manage_reminders_alias_still_works(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        # _reminder_create imports get_reminder_store from the shim at call time.
        with patch("api.services.reminder_store.get_reminder_store", return_value=store):
            out = agent_tools._tool_manage_reminders({
                "action": "create", "name": "Legacy", "schedule_type": "cron",
                "schedule_value": "0 9 * * *", "message_content": "hi",
            })
        assert "created" in out.lower()
        assert len(store.list_all()) == 1
