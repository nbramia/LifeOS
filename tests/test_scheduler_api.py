"""
Tests for the Scheduler API routes (/api/scheduler) and the agent-tools
manage_schedules wrapper — the renamed surface from #246.

CRUD is tested in-process via TestClient with a mocked store; the agent-tools
path is tested against a real store on a temp vault.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

    def test_create_propagates_execution_context(self, client, mock_store):
        response = client.post("/api/scheduler", json={
            "name": "Pinned", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "action": "agent", "message_content": "Draft the pinned update",
            "persona_id": "primary", "model_id": "gpt-synthetic",
            "effort": "high", "host": "server", "working_dir": "/tmp/synthetic",
        })
        assert response.status_code == 200
        kwargs = mock_store.create.call_args.kwargs
        assert {key: kwargs[key] for key in (
            "persona_id", "model_id", "effort", "host", "working_dir",
        )} == {
            "persona_id": "primary", "model_id": "gpt-synthetic", "effort": "high",
            "host": "server", "working_dir": "/tmp/synthetic",
        }

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

    def test_type_change_alone_validated_against_the_stored_value_and_rejected(self, client, mock_store):
        """A PUT that changes only schedule_type (no schedule_value in the
        same request) validates the entry's STORED value against the NEW
        type -- a bare type change that would leave the stored value
        unparsable under its own type is rejected before anything is
        written, rather than writing a schedule whose stored value no
        longer matches its type."""
        mock_store.get.return_value = _sample_entry(schedule_type="cron", schedule_value="0 9 * * *")
        resp = client.put("/api/scheduler/sch-1", json={"schedule_type": "once"})
        assert resp.status_code == 422
        assert "0 9 * * *" in resp.text
        mock_store.update.assert_not_called()

    def test_type_and_value_together_convert_in_one_write(self, client, mock_store):
        """The positive counterpart: submitting schedule_type and
        schedule_value together lets a conversion succeed in a single
        write, even though the entry's stored value doesn't parse under
        the new type on its own."""
        mock_store.get.return_value = _sample_entry(schedule_type="cron", schedule_value="0 9 * * *")
        resp = client.put("/api/scheduler/sch-1", json={
            "schedule_type": "once", "schedule_value": "2026-06-03T15:05:00",
        })
        assert resp.status_code == 200
        kwargs = mock_store.update.call_args.kwargs
        assert kwargs["schedule_type"] == "once"
        assert kwargs["schedule_value"] == "2026-06-03T15:05:00"

    def test_no_fields_present_skips_all_validation(self, client, mock_store):
        resp = client.put("/api/scheduler/sch-1", json={"name": "Renamed"})
        assert resp.status_code == 200
        mock_store.update.assert_called_once()


class TestActionInputValidation:
    """The resulting action's own required inputs are enforced on both
    create and update, in one shared function -- so a schedule
    whose action has nothing to fire with is rejected before it's ever
    saved, rather than failing silently at fire time."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_store(self):
        with patch("api.routes.scheduler.get_scheduler_store") as mock:
            store = mock.return_value
            entry = _sample_entry(action="agent", message_content="Draft my weekly review")
            store.create.return_value = entry
            store.get.return_value = entry
            store.update.return_value = entry
            yield store

    def _create_payload(self, **overrides):
        payload = {
            "name": "X", "schedule_type": "cron", "schedule_value": "0 9 * * *",
        }
        payload.update(overrides)
        return payload

    # -- create: endpoint action --

    def test_create_endpoint_action_without_config_rejects_on_method(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(action="endpoint"))
        assert resp.status_code == 422
        assert "endpoint_config.method" in resp.text
        mock_store.create.assert_not_called()

    def test_create_endpoint_action_with_bad_method_rejected(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "DELETE", "endpoint": "/api/tasks"},
        ))
        assert resp.status_code == 422
        assert "endpoint_config.method" in resp.text
        mock_store.create.assert_not_called()

    def test_create_endpoint_action_with_bad_path_rejected(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "GET", "endpoint": "tasks"},
        ))
        assert resp.status_code == 422
        assert "endpoint_config.endpoint" in resp.text
        mock_store.create.assert_not_called()

    def test_create_endpoint_action_with_non_object_params_rejected(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "GET", "endpoint": "/api/tasks", "params": "nope"},
        ))
        assert resp.status_code == 422
        assert "endpoint_config.params" in resp.text
        mock_store.create.assert_not_called()

    def test_create_endpoint_action_with_valid_config_normalizes_method_to_upper_case(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "get", "endpoint": "/api/tasks"},
        ))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["endpoint_config"]["method"] == "GET"

    def test_create_endpoint_action_with_absent_params_accepted(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "POST", "endpoint": "/api/tasks"},
        ))
        assert resp.status_code == 200

    def test_create_endpoint_action_with_object_params_accepted(self, client, mock_store):
        resp = client.post("/api/scheduler", json=self._create_payload(
            action="endpoint", endpoint_config={"method": "POST", "endpoint": "/api/tasks", "params": {"status": "todo"}},
        ))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["endpoint_config"]["params"] == {"status": "todo"}

    # -- create: notify / prompt / agent --

    @pytest.mark.parametrize("action", ["notify", "prompt", "agent"])
    def test_create_blank_message_content_rejected(self, client, mock_store, action):
        resp = client.post("/api/scheduler", json=self._create_payload(action=action))
        assert resp.status_code == 422
        assert "message_content" in resp.text
        mock_store.create.assert_not_called()

    @pytest.mark.parametrize("action", ["notify", "prompt", "agent"])
    def test_create_whitespace_only_message_content_rejected(self, client, mock_store, action):
        resp = client.post("/api/scheduler", json=self._create_payload(action=action, message_content="   "))
        assert resp.status_code == 422
        assert "message_content" in resp.text
        mock_store.create.assert_not_called()

    @pytest.mark.parametrize("action", ["notify", "prompt", "agent"])
    def test_create_non_blank_message_content_accepted(self, client, mock_store, action):
        resp = client.post("/api/scheduler", json=self._create_payload(action=action, message_content="hi"))
        assert resp.status_code == 200

    # -- update: only validated when the patch touches action/message_content/endpoint_config --

    def test_update_action_alone_validated_against_the_stored_message_content(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="notify", message_content="")
        resp = client.put("/api/scheduler/sch-1", json={"action": "prompt"})
        assert resp.status_code == 422
        assert "message_content" in resp.text
        mock_store.update.assert_not_called()

    def test_update_action_alone_accepted_against_the_stored_message_content(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="notify", message_content="hi")
        resp = client.put("/api/scheduler/sch-1", json={"action": "prompt"})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["action"] == "prompt"

    def test_update_action_to_endpoint_without_existing_config_rejected(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="notify", message_content="hi", endpoint_config=None)
        resp = client.put("/api/scheduler/sch-1", json={"action": "endpoint"})
        assert resp.status_code == 422
        assert "endpoint_config.method" in resp.text
        mock_store.update.assert_not_called()

    def test_update_endpoint_config_alone_validated_against_the_stored_action(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="endpoint", endpoint_config={"method": "GET", "endpoint": "/api/tasks"})
        resp = client.put("/api/scheduler/sch-1", json={"endpoint_config": {"method": "PATCH", "endpoint": "/api/tasks"}})
        assert resp.status_code == 422
        assert "endpoint_config.method" in resp.text
        mock_store.update.assert_not_called()

    def test_update_endpoint_config_normalizes_method_to_upper_case(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="endpoint", endpoint_config={"method": "GET", "endpoint": "/api/tasks"})
        resp = client.put("/api/scheduler/sch-1", json={"endpoint_config": {"method": "post", "endpoint": "/api/tasks"}})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["endpoint_config"]["method"] == "POST"

    def test_update_message_content_alone_rejected_when_blank_for_the_stored_action(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="prompt", message_content="the old prompt")
        resp = client.put("/api/scheduler/sch-1", json={"message_content": ""})
        assert resp.status_code == 422
        assert "message_content" in resp.text
        mock_store.update.assert_not_called()

    def test_update_unrelated_field_on_a_preexisting_invalid_entry_still_succeeds(self, client, mock_store):
        """An entry whose stored fields wouldn't pass this check on their
        own must not be permanently unable to accept an unrelated patch --
        only a patch that touches action,
        message_content, or endpoint_config is checked."""
        mock_store.get.return_value = _sample_entry(action="notify", message_content="")
        resp = client.put("/api/scheduler/sch-1", json={"enabled": False})
        assert resp.status_code == 200
        mock_store.update.assert_called_once()
        assert mock_store.update.call_args.kwargs.get("enabled") is False
        assert "action" not in mock_store.update.call_args.kwargs
        assert "message_content" not in mock_store.update.call_args.kwargs

    def test_update_endpoint_config_touching_only_params_validated_against_stored_method_and_path(self, client, mock_store):
        mock_store.get.return_value = _sample_entry(action="endpoint", endpoint_config={"method": "GET", "endpoint": "/api/tasks"})
        resp = client.put("/api/scheduler/sch-1", json={"endpoint_config": {"method": "GET", "endpoint": "/api/tasks", "params": {"status": "todo"}}})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["endpoint_config"]["params"] == {"status": "todo"}


class TestScheduleTypeConversionAgainstARealStore:
    """End-to-end proof of a cron<->once conversion against a real
    SchedulerStore (not a mock), so a passing test means the stored entry
    itself ends up correct -- not just that the mock received the right
    kwargs."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def store(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        return SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "idx.json")

    def test_cron_to_once_conversion_succeeds_with_a_real_next_fire_time(self, client, store):
        entry = store.create(
            name="Weekly review", schedule_type="cron", schedule_value="0 9 * * 6",
            action="notify", message_type="static", message_content="hi",
        )
        with patch("api.routes.scheduler.get_scheduler_store", return_value=store):
            resp = client.put(f"/api/scheduler/{entry.id}", json={
                "schedule_type": "once", "schedule_value": "2099-06-03T15:05:00",
            })
        assert resp.status_code == 200
        refreshed = store.get(entry.id)
        assert refreshed.schedule_type == "once"
        assert refreshed.schedule_value == "2099-06-03T15:05:00"
        assert refreshed.next_trigger_at is not None

    def test_once_to_cron_conversion_succeeds_with_a_real_next_fire_time(self, client, store):
        entry = store.create(
            name="One-off", schedule_type="once", schedule_value="2099-06-03T15:05:00",
            action="notify", message_type="static", message_content="hi",
        )
        with patch("api.routes.scheduler.get_scheduler_store", return_value=store):
            resp = client.put(f"/api/scheduler/{entry.id}", json={
                "schedule_type": "cron", "schedule_value": "0 9 * * 6",
            })
        assert resp.status_code == 200
        refreshed = store.get(entry.id)
        assert refreshed.schedule_type == "cron"
        assert refreshed.schedule_value == "0 9 * * 6"
        assert refreshed.next_trigger_at is not None

    def test_bare_type_change_that_would_strand_the_entry_leaves_it_completely_unchanged(self, client, store):
        """The regression this guards against: a cron entry whose type
        flips to "once" alone (schedule_value still the cron string) used
        to write successfully, recompute next_trigger_at as None via the
        swallowed parse error, and drop out of the active/Done bucketing.
        The entry must come back byte-for-byte identical after the
        rejected PUT."""
        entry = store.create(
            name="Weekly review", schedule_type="cron", schedule_value="0 9 * * 6",
            action="notify", message_type="static", message_content="hi",
        )
        before = store.get(entry.id)
        with patch("api.routes.scheduler.get_scheduler_store", return_value=store):
            resp = client.put(f"/api/scheduler/{entry.id}", json={"schedule_type": "once"})
        assert resp.status_code == 422
        after = store.get(entry.id)
        assert after.schedule_type == before.schedule_type == "cron"
        assert after.schedule_value == before.schedule_value == "0 9 * * 6"
        assert after.next_trigger_at == before.next_trigger_at
        assert after.next_trigger_at is not None


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


class TestPreviewSchedule:
    """`POST /api/scheduler/preview` — the create-schedule composer's live
    fire-time preview, computed with `compute_next_n_triggers` but never
    touching the store."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_cron_in_non_utc_timezone(self, client):
        """DST-safe: asserts the returned times land at 9am local and at a
        UTC offset America/New_York actually uses (-4 EDT or -5 EST) rather
        than depending on today's date landing on either side of a DST
        transition, or on a fixed offset a cron-evaluated-in-UTC bug would
        also happen to produce."""
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "timezone": "America/New_York",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["next"]) == 3
        parsed = [datetime.fromisoformat(t) for t in data["next"]]
        assert parsed == sorted(parsed)
        for dt in parsed:
            assert dt.tzinfo == timezone.utc
            local = dt.astimezone(ZoneInfo("America/New_York"))
            assert (local.hour, local.minute) == (9, 0)
            assert local.utcoffset() in (timedelta(hours=-4), timedelta(hours=-5))

    def test_once_in_future_returns_single_time(self, client):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "once", "schedule_value": future.isoformat(),
        })
        assert resp.status_code == 200
        assert resp.json()["next"] == [future.astimezone(timezone.utc).isoformat()]

    def test_once_in_past_returns_empty(self, client):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "once", "schedule_value": past,
        })
        assert resp.status_code == 200
        assert resp.json()["next"] == []

    def test_invalid_cron_returns_422_matching_create_wording(self, client):
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "cron", "schedule_value": "not a cron",
        })
        assert resp.status_code == 422
        assert "Invalid cron expression 'not a cron'" in resp.json()["detail"]

    def test_invalid_iso_datetime_returns_422_matching_create_wording(self, client):
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "once", "schedule_value": "not-a-date",
        })
        assert resp.status_code == 422
        assert "Invalid ISO datetime 'not-a-date'" in resp.json()["detail"]

    def test_invalid_timezone_returns_422(self, client):
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "timezone": "Nowhere/Fake",
        })
        assert resp.status_code == 422
        assert "Unknown timezone 'Nowhere/Fake'" in resp.json()["detail"]

    def test_bad_schedule_type_returns_400(self, client):
        resp = client.post("/api/scheduler/preview", json={
            "schedule_type": "weekly", "schedule_value": "0 9 * * *",
        })
        assert resp.status_code == 400

    def test_omitted_timezone_defaults_to_configured_timezone(self, client):
        with patch("api.routes.scheduler.settings.timezone", "America/Chicago"):
            resp = client.post("/api/scheduler/preview", json={
                "schedule_type": "cron", "schedule_value": "0 9 * * *",
            })
        assert resp.status_code == 200
        parsed = datetime.fromisoformat(resp.json()["next"][0])
        local = parsed.astimezone(ZoneInfo("America/Chicago"))
        assert local.hour == 9

    def test_not_captured_by_the_schedule_id_route(self, client):
        """POST /preview is declared before GET/PUT/DELETE /{schedule_id} —
        a store whose `get` would happily resolve "preview" as a schedule
        id must never be reached for this path."""
        with patch("api.routes.scheduler.get_scheduler_store") as mock:
            mock.return_value.get.return_value = _sample_entry()
            resp = client.post("/api/scheduler/preview", json={
                "schedule_type": "cron", "schedule_value": "0 9 * * *",
            })
        assert resp.status_code == 200
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

    def test_create_blank_message_rejected_and_writes_nothing(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "create",
                "name": "Silent",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "schedule_action": "notify",
                "message_content": "   ",
            })
        assert "Error" in out
        assert "message_content must not be blank" in out
        assert store.list_all() == []

    def test_create_endpoint_action_without_config_rejected_and_writes_nothing(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "create",
                "name": "Unconfigured endpoint",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "schedule_action": "endpoint",
            })
        assert "Error" in out
        assert "endpoint_config.method" in out
        assert store.list_all() == []

    def test_update_to_blank_message_rejected_and_leaves_entry_unchanged(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Keep me", schedule_type="cron",
                               schedule_value="0 9 * * *", action="notify",
                               message_content="Good morning")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "update", "schedule_id": created.id,
                "message_content": "   ",
            })
        assert "Error" in out
        assert "message_content must not be blank" in out
        refreshed = store.get(created.id)
        assert refreshed.message_content == "Good morning"

    def test_update_action_to_endpoint_without_config_rejected_and_leaves_entry_unchanged(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Notifier", schedule_type="cron",
                               schedule_value="0 9 * * *", action="notify",
                               message_content="Good morning")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "update", "schedule_id": created.id,
                "schedule_action": "endpoint",
            })
        assert "Error" in out
        assert "endpoint_config.method" in out
        refreshed = store.get(created.id)
        assert refreshed.action == "notify"

    def test_update_action_to_prompt_with_existing_message_accepted(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore
        from api.services import agent_tools

        store = SchedulerStore(vault_path=tmp_path / "vault",
                               index_path=tmp_path / "idx.json")
        created = store.create(name="Switches action", schedule_type="cron",
                               schedule_value="0 9 * * *", action="notify",
                               message_content="Good morning")
        with patch("api.services.scheduler_store.get_scheduler_store", return_value=store):
            out = agent_tools._tool_manage_schedules({
                "action": "update", "schedule_id": created.id,
                "schedule_action": "prompt",
            })
        assert "Schedule updated" in out
        refreshed = store.get(created.id)
        assert refreshed.action == "prompt"
        assert refreshed.message_content == "Good morning"

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
