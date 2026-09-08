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

    def test_create_failure_is_never_success_shaped(self, mock_store):
        """#609: a store write failure must be a non-2xx, never a 200 with
        the created schedule's own shape."""
        from fastapi.testclient import TestClient
        from api.main import app

        mock_store.create.side_effect = OSError("disk write failed")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/scheduler", json={
            "name": "X", "schedule_type": "cron", "schedule_value": "0 9 * * *", "action": "notify",
        })
        assert not (200 <= resp.status_code < 300)

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


class TestUpdateScheduleValidation:
    """PUT /api/scheduler/{id} validates schedule_type, action, timezone,
    and schedule_value before writing anything, mirroring the checks
    POST "" already applies at creation time."""

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
            store.get.return_value = entry
            store.update.return_value = entry
            yield store

    def test_rejects_invalid_schedule_type(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"schedule_type": "weekly"})
        assert resp.status_code == 400
        assert "schedule_type" in resp.text
        mock_store.update.assert_not_called()

    def test_rejects_invalid_action(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"action": "explode"})
        assert resp.status_code == 400
        assert "action" in resp.text
        mock_store.update.assert_not_called()

    def test_rejects_unknown_timezone(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"timezone": "Nowhere/Fake"})
        assert resp.status_code == 422
        assert "Nowhere/Fake" in resp.text
        mock_store.update.assert_not_called()

    def test_accepts_known_timezone(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"timezone": "America/Chicago"})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["timezone"] == "America/Chicago"

    def test_rejects_invalid_cron_expression_against_explicit_type(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={
            "schedule_type": "cron", "schedule_value": "not a cron string",
        })
        assert resp.status_code == 422
        assert "cron" in resp.text.lower()
        assert "not a cron string" in resp.text
        mock_store.update.assert_not_called()

    def test_accepts_valid_cron_expression(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={
            "schedule_type": "cron", "schedule_value": "0 8 * * 1-5",
        })
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["schedule_value"] == "0 8 * * 1-5"

    def test_rejects_invalid_once_datetime(self, client, mock_store):
        # A valid CRON expression, so a validator that ignores the
        # requested "once" type and always parses as cron would let this
        # through instead of rejecting it as a bad ISO datetime.
        resp = client.put("/api/scheduler/sch-1", json={
            "schedule_type": "once", "schedule_value": "0 9 * * *",
        })
        assert resp.status_code == 422
        assert "0 9 * * *" in resp.text
        mock_store.update.assert_not_called()

    def test_accepts_valid_once_datetime(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={
            "schedule_type": "once", "schedule_value": "2026-06-03T15:05:00",
        })
        assert resp.status_code == 200

    def test_schedule_value_alone_is_validated_against_the_stored_type(self, client, mock_store):
        """A PUT that changes only schedule_value (no schedule_type in the
        same request) must be validated against the ENTRY's stored type,
        not assumed to be cron."""
        mock_store.get.return_value = _sample_entry(schedule_type="once")
        # A valid CRON expression but not a valid ISO datetime -- a
        # validator that defaults to (or ignores the fetch and assumes)
        # cron would accept this instead of rejecting it against the
        # entry's actual stored "once" type.
        resp = client.put("/api/scheduler/sch-1", json={"schedule_value": "0 9 * * *"})
        assert resp.status_code == 422
        assert "0 9 * * *" in resp.text
        mock_store.update.assert_not_called()

    def test_schedule_value_alone_accepted_against_the_stored_cron_type(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(schedule_type="cron")
        resp = client.put("/api/scheduler/sch-1", json={"schedule_value": "0 7 * * *"})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["schedule_value"] == "0 7 * * *"

    def test_no_fields_present_skips_all_validation(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"name": "Renamed"})
        assert resp.status_code == 200
        mock_store.update.assert_called_once()


class TestListBots:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_returns_registry_names(self, client):
        with patch("api.services.telegram.valid_bot_names", return_value=["primary", "alerts", "ledger"]):
            resp = client.get("/api/scheduler/bots")
        assert resp.status_code == 200
        assert resp.json() == {"bots": ["primary", "alerts", "ledger"]}

    def test_not_captured_by_the_schedule_id_route(self, client):
        """GET /bots is declared before GET /{schedule_id} — a store whose
        `get` would happily resolve "bots" as a schedule id must never be
        reached for this path."""
        with patch("api.routes.scheduler.get_scheduler_store") as mock:
            mock.return_value.get.return_value = _sample_entry()
            with patch("api.services.telegram.valid_bot_names", return_value=["primary"]):
                resp = client.get("/api/scheduler/bots")
        assert resp.status_code == 200
        assert resp.json() == {"bots": ["primary"]}
        mock.return_value.get.assert_not_called()


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

    def test_update_schedule_tool(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Old name", schedule_type="cron",
                               schedule_value="0 9 * * *", action="prompt",
                               message_content="old prompt")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "update",
                "schedule_id": created.id,
                "name": "New name",
                "message_content": "new prompt",
                "enabled": False,
            })
        assert "Schedule updated" in out
        refreshed = store.get(created.id)
        assert refreshed.name == "New name"
        assert refreshed.message_content == "new prompt"
        assert refreshed.enabled is False

    def test_update_only_changes_supplied_fields(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Keep", schedule_type="cron",
                               schedule_value="0 9 * * *", action="prompt",
                               message_content="keep me")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            agent_tools._tool_manage_schedules({
                "action": "update", "schedule_id": created.id,
                "schedule_value": "0 10 * * *",
            })
        refreshed = store.get(created.id)
        assert refreshed.schedule_value == "0 10 * * *"
        assert refreshed.name == "Keep"
        assert refreshed.message_content == "keep me"

    def test_update_missing_id_errors(self, tmp_path):
        from api.services import agent_tools
        out = agent_tools._tool_manage_schedules({"action": "update", "name": "X"})
        assert "Error" in out and "schedule_id" in out

    def test_update_unknown_id_errors(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "update", "schedule_id": "nope", "name": "X"})
        assert "Error" in out and "nope" in out

    def test_delete_schedule_tool(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Doomed", schedule_type="cron",
                               schedule_value="0 9 * * *", action="notify",
                               message_content="bye")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "delete", "schedule_id": created.id})
        assert "Schedule deleted" in out and "Doomed" in out
        assert store.get(created.id) is None

    def test_delete_missing_id_errors(self, tmp_path):
        from api.services import agent_tools
        out = agent_tools._tool_manage_schedules({"action": "delete"})
        assert "Error" in out and "schedule_id" in out

    def test_delete_unknown_id_errors(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "delete", "schedule_id": "ghost"})
        assert "Error" in out and "ghost" in out

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
