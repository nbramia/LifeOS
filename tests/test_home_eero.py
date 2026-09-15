"""
Tests for the eero home-network integration (#1081) — pause/resume a
household profile or device's internet access.

Every vendor HTTP call is mocked via httpx.MockTransport (no test contacts
the real eero service). Telegram and the human queue are monkeypatched so
no test writes to a real vault Scheduler Inbox outside a tmp_path-isolated
SchedulerStore, and no test sends a real Telegram message.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.home as home_route
import api.services.home.eero as eero
from api.services.scheduler_store import SchedulerStore, _format_endpoint_result

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _client_factory(handler):
    """A callable matching eero._new_http_client's signature, wired to a
    MockTransport so no test call ever reaches the real eero API."""
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=eero.BASE_URL)
    return factory


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(eero, "STATE_PATH", tmp_path / "data" / "home" / "eero_session.json")
    monkeypatch.setattr(eero, "TARGETS_PATH", tmp_path / "config" / "home" / "eero_targets.json")
    monkeypatch.setattr(eero.settings, "eero_session_token", "")

    store = SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "scheduler_index.json")
    monkeypatch.setattr(eero, "get_scheduler_store", lambda: store)

    telegram_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("api.services.telegram.send_message_async", telegram_mock)

    human_queue_mock = MagicMock()
    monkeypatch.setattr("api.services.human_queue.add_card", human_queue_mock)

    monkeypatch.setattr(eero, "_RESUME_RETRY_BACKOFF", (0.0, 0.0))

    return {
        "tmp_path": tmp_path,
        "store": store,
        "telegram": telegram_mock,
        "human_queue": human_queue_mock,
    }


def _write_targets(env, targets: dict):
    eero.TARGETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eero.TARGETS_PATH.write_text(json.dumps(targets))


def _write_token(env, token: str):
    eero.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    eero.STATE_PATH.write_text(json.dumps({"token": token}))


PROFILE_TARGETS = {
    "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890"},
}
DEVICE_TARGETS = {
    "Guest Laptop": {"type": "device", "network_id": "12345", "mac": "AA:BB:CC:00:00:01"},
}


def _ok_handler(*, paused: bool):
    """Every vendor request (write or read) succeeds; reads report `paused`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"paused": paused}})
        return httpx.Response(200, json={"data": {"paused": paused}})
    return handler


def _minutes_until(resume_at: str) -> float:
    dt = datetime.fromisoformat(resume_at)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 60


# ---------------------------------------------------------------------------
# Session / token resolution
# ---------------------------------------------------------------------------

class TestTokenResolution:
    def test_no_token_anywhere_is_unconfigured(self, env):
        assert eero.has_session_token() is False
        assert eero._load_token() is None

    def test_state_file_wins_over_env(self, env):
        monkeypatch_env_token = "env-token"
        eero.settings.eero_session_token = monkeypatch_env_token
        _write_token(env, "state-token")
        assert eero._load_token() == "state-token"

    def test_env_var_is_fallback(self, env):
        eero.settings.eero_session_token = "env-token"
        assert eero._load_token() == "env-token"

    def test_state_file_write_is_mode_0600(self, env):
        eero._save_token("some-token")
        mode = eero.STATE_PATH.stat().st_mode & 0o777
        assert mode == 0o600
        assert json.loads(eero.STATE_PATH.read_text()) == {"token": "some-token"}


# ---------------------------------------------------------------------------
# Vendor request shape
# ---------------------------------------------------------------------------

class TestCookieHeader:
    @pytest.mark.asyncio
    async def test_cookie_header_carries_the_token(self, env, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["cookie"] = request.headers.get("cookie")
            return httpx.Response(200, json={"data": {"paused": True}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = await eero._vendor_request("GET", "/x", "abc123")
        assert resp.status_code == 200
        assert seen["cookie"] == "s=abc123"


# ---------------------------------------------------------------------------
# Refresh-and-retry
# ---------------------------------------------------------------------------

class TestRefreshAndRetry:
    @pytest.mark.asyncio
    async def test_refresh_success_retries_and_persists_new_token(self, env, monkeypatch):
        _write_token(env, "old-token")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            cookie = request.headers.get("cookie", "")
            calls.append((request.method, str(request.url.path), cookie))
            if request.url.path == "/2.2/login/refresh":
                assert cookie == "s=old-token"
                return httpx.Response(200, json={"data": {"user_token": "new-token"}})
            if cookie == "s=old-token":
                return httpx.Response(401, json={"error": "unauthorized"})
            assert cookie == "s=new-token"
            return httpx.Response(200, json={"data": {"paused": True}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = await eero._authed_request("GET", "/some/path")
        assert resp.status_code == 200
        assert json.loads(eero.STATE_PATH.read_text())["token"] == "new-token"
        # original request, refresh, retried request
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_refresh_failure_alerts_and_raises_session_dead(self, env, monkeypatch):
        _write_token(env, "old-token")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/2.2/login/refresh":
                return httpx.Response(500, json={"error": "nope"})
            return httpx.Response(401, json={"error": "unauthorized"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroSessionDead):
            await eero._authed_request("GET", "/some/path")

        env["telegram"].assert_called_once()
        env["human_queue"].assert_called_once()
        _, kwargs = env["human_queue"].call_args
        assert kwargs["key"] == "eero-session"

    @pytest.mark.asyncio
    async def test_refresh_success_but_retry_still_401_is_dead(self, env, monkeypatch):
        _write_token(env, "old-token")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/2.2/login/refresh":
                return httpx.Response(200, json={"data": {"user_token": "new-token"}})
            return httpx.Response(401, json={"error": "unauthorized"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroSessionDead):
            await eero._authed_request("GET", "/some/path")
        env["human_queue"].assert_called_once()


# ---------------------------------------------------------------------------
# Target config loading
# ---------------------------------------------------------------------------

class TestTargetConfig:
    def test_missing_config_file_is_zero_targets(self, env):
        assert eero._load_targets() == {}
        assert eero.configured_target_names() == []

    def test_malformed_json_is_zero_targets(self, env):
        eero.TARGETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        eero.TARGETS_PATH.write_text("{not valid json")
        assert eero._load_targets() == {}

    def test_case_insensitive_lookup(self, env):
        _write_targets(env, PROFILE_TARGETS)
        target = eero._resolve_target("kid's ipad")
        assert target.name == "Kid's iPad"
        target2 = eero._resolve_target("KID'S IPAD")
        assert target2.name == "Kid's iPad"

    def test_device_target_resolves_vendor_path(self, env):
        _write_targets(env, DEVICE_TARGETS)
        target = eero._resolve_target("guest laptop")
        assert target.vendor_path == "/2.3/networks/12345/devices/AA:BB:CC:00:00:01"

    def test_profile_target_vendor_path_is_its_url(self, env):
        _write_targets(env, PROFILE_TARGETS)
        target = eero._resolve_target("kid's ipad")
        assert target.vendor_path == "/2.2/networks/12345/profiles/67890"

    def test_entry_missing_type_is_skipped_others_still_load(self, env):
        _write_targets(env, {
            **PROFILE_TARGETS,
            "Broken": {"url": "/2.2/networks/1/profiles/2"},
        })
        names = eero.configured_target_names()
        assert names == ["Kid's iPad"]

    def test_device_entry_missing_mac_is_skipped(self, env):
        _write_targets(env, {"Bad Device": {"type": "device", "network_id": "12345"}})
        assert eero.configured_target_names() == []

    def test_profile_entry_bad_url_is_skipped(self, env):
        _write_targets(env, {"Bad Profile": {"type": "profile", "url": "not-a-path"}})
        assert eero.configured_target_names() == []

    def test_unrecognized_type_is_skipped(self, env):
        _write_targets(env, {"Weird": {"type": "router", "url": "/x"}})
        assert eero.configured_target_names() == []

    def test_unknown_target_raises_with_configured_names(self, env):
        _write_targets(env, PROFILE_TARGETS)
        with pytest.raises(eero.EeroUnknownTarget) as exc_info:
            eero._resolve_target("nonexistent")
        assert "Kid's iPad" in exc_info.value.configured_names


# ---------------------------------------------------------------------------
# Pause / resume — idempotency, mismatch, vendor errors
# ---------------------------------------------------------------------------

class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_sets_and_reports_paused(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad")
        assert result["paused"] is True
        assert result["requested_paused"] is True
        assert result["mismatch"] is False
        assert result["resume_at"] is None
        assert result["scheduler_message"] == "Kid's iPad: paused"

    @pytest.mark.asyncio
    async def test_pause_is_idempotent_on_already_paused_target(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        first = await eero.pause("kid's ipad")
        second = await eero.pause("kid's ipad")
        assert first["paused"] is True
        assert second["paused"] is True

    @pytest.mark.asyncio
    async def test_resume_is_idempotent_on_already_resumed_target(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=False)))

        result = await eero.resume("kid's ipad")
        assert result["paused"] is False
        assert result["requested_paused"] is False

    @pytest.mark.asyncio
    async def test_unknown_target_never_contacts_vendor(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("vendor must not be contacted for an unknown target")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroUnknownTarget):
            await eero.pause("no such target")
        with pytest.raises(eero.EeroUnknownTarget):
            await eero.resume("no such target")

    @pytest.mark.asyncio
    async def test_mismatch_flagged_when_readback_disagrees(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                return httpx.Response(200, json={"data": {"paused": True}})
            # Vendor reports the opposite of what was requested.
            return httpx.Response(200, json={"data": {"paused": False}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        result = await eero.pause("kid's ipad")
        assert result["paused"] is False
        assert result["requested_paused"] is True
        assert result["mismatch"] is True

    @pytest.mark.asyncio
    async def test_vendor_write_rejection_alerts_and_raises_api_error(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "nope"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroAPIError):
            await eero.pause("kid's ipad")

        env["telegram"].assert_called_once()
        env["human_queue"].assert_called_once()
        _, kwargs = env["human_queue"].call_args
        assert kwargs["key"] == "eero-api-error"

    @pytest.mark.asyncio
    async def test_vendor_unexpected_shape_is_api_error(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroAPIError):
            await eero.pause("kid's ipad")


# ---------------------------------------------------------------------------
# Scheduler-backed timed pause
# ---------------------------------------------------------------------------

class TestScheduledResumeEntry:
    @pytest.mark.asyncio
    async def test_pause_with_minutes_creates_pending_entry(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad", minutes=30)
        assert result["resume_at"] is not None

        op_key = eero._operation_key("Kid's iPad")
        pending = eero._pending_entry(env["store"], op_key)
        assert pending is not None
        assert pending.action == "endpoint"
        assert pending.endpoint_config["method"] == "POST"
        assert pending.endpoint_config["params"] == {"scheduled": True}
        assert "/pause" not in pending.endpoint_config["endpoint"]
        assert pending.endpoint_config["endpoint"].endswith("/resume")

    @pytest.mark.asyncio
    async def test_second_timed_pause_replaces_pending_entry(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        await eero.pause("kid's ipad", minutes=30)
        op_key = eero._operation_key("Kid's iPad")
        first_id = eero._pending_entry(env["store"], op_key).id

        await eero.pause("kid's ipad", minutes=15)
        entries = eero._entries_for_operation(env["store"], op_key)
        assert len(entries) == 1
        assert entries[0].enabled is True
        assert entries[0].id != first_id

    @pytest.mark.asyncio
    async def test_indefinite_pause_clears_pending_entry(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        await eero.pause("kid's ipad", minutes=30)
        result = await eero.pause("kid's ipad")
        assert result["resume_at"] is None

        op_key = eero._operation_key("Kid's iPad")
        assert eero._pending_entry(env["store"], op_key) is None

    @pytest.mark.asyncio
    async def test_manual_resume_deletes_pending_entry(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))
        await eero.pause("kid's ipad", minutes=30)

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=False)))
        await eero.resume("kid's ipad")

        op_key = eero._operation_key("Kid's iPad")
        assert eero._pending_entry(env["store"], op_key) is None

    @pytest.mark.asyncio
    async def test_finished_entries_are_cleaned_up_before_creating_a_new_one(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        await eero.pause("kid's ipad", minutes=30)
        op_key = eero._operation_key("Kid's iPad")
        fired = eero._pending_entry(env["store"], op_key)
        env["store"].mark_triggered(fired.id)  # simulate the scheduler firing it
        assert eero._pending_entry(env["store"], op_key) is None
        assert len(eero._entries_for_operation(env["store"], op_key)) == 1  # disabled, still present

        await eero.pause("kid's ipad", minutes=10)
        entries = eero._entries_for_operation(env["store"], op_key)
        assert len(entries) == 1
        assert entries[0].enabled is True
        assert entries[0].id != fired.id

    @pytest.mark.asyncio
    async def test_readback_failure_after_successful_write_keeps_pending_resume(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                return httpx.Response(200, json={"data": {"paused": True}})
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroAPIError):
            await eero.pause("kid's ipad", minutes=60)

        op_key = eero._operation_key("Kid's iPad")
        entries = eero._entries_for_operation(env["store"], op_key)
        assert len(entries) == 1
        assert entries[0].enabled is True
        assert entries[0].operation_key == op_key


# ---------------------------------------------------------------------------
# Per-target default duration
# ---------------------------------------------------------------------------

class TestDefaultMinutes:
    @pytest.mark.asyncio
    async def test_pause_without_minutes_uses_target_default(self, env, monkeypatch):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": 45},
        })
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad")
        assert result["resume_at"] is not None
        assert 44 <= _minutes_until(result["resume_at"]) <= 45

        op_key = eero._operation_key("Kid's iPad")
        assert eero._pending_entry(env["store"], op_key) is not None

    @pytest.mark.asyncio
    async def test_explicit_minutes_overrides_default(self, env, monkeypatch):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": 45},
        })
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad", minutes=10)
        assert 9 <= _minutes_until(result["resume_at"]) <= 10

    @pytest.mark.asyncio
    async def test_no_default_no_minutes_stays_indefinite(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)  # no default_minutes configured
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad")
        assert result["resume_at"] is None

    def test_invalid_default_minutes_type_skips_entry(self, env):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": "soon"},
            "Guest Laptop": {"type": "device", "network_id": "12345", "mac": "AA:BB:CC:00:00:01"},
        })
        assert eero.configured_target_names() == ["Guest Laptop"]

    def test_default_minutes_bool_is_invalid(self, env):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": True},
        })
        assert eero.configured_target_names() == []

    def test_default_minutes_out_of_range_is_invalid(self, env):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": 1500},
        })
        assert eero.configured_target_names() == []


# ---------------------------------------------------------------------------
# Indefinite pause
# ---------------------------------------------------------------------------

class TestIndefinitePause:
    @pytest.mark.asyncio
    async def test_indefinite_overrides_default_and_clears_pending(self, env, monkeypatch):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": 45},
        })
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        await eero.pause("kid's ipad", minutes=30)
        op_key = eero._operation_key("Kid's iPad")
        assert eero._pending_entry(env["store"], op_key) is not None

        result = await eero.pause("kid's ipad", indefinite=True)
        assert result["resume_at"] is None
        assert eero._pending_entry(env["store"], op_key) is None

    @pytest.mark.asyncio
    async def test_indefinite_with_minutes_is_rejected_without_vendor_call(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("vendor must not be contacted when validation fails")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(ValueError):
            await eero.pause("kid's ipad", minutes=30, indefinite=True)


# ---------------------------------------------------------------------------
# Silent scheduled calls
# ---------------------------------------------------------------------------

class TestScheduledMessage:
    @pytest.mark.asyncio
    async def test_scheduled_pause_success_has_empty_message(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad", scheduled=True)
        assert result["mismatch"] is False
        assert result["scheduler_message"] == ""

    @pytest.mark.asyncio
    async def test_scheduled_resume_success_has_empty_message(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=False)))

        result = await eero.resume("kid's ipad", scheduled=True)
        assert result["mismatch"] is False
        assert result["scheduler_message"] == ""

    @pytest.mark.asyncio
    async def test_scheduled_with_mismatch_is_not_empty(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                return httpx.Response(200, json={"data": {"paused": True}})
            # Vendor reports the opposite of what was requested.
            return httpx.Response(200, json={"data": {"paused": False}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        result = await eero.pause("kid's ipad", scheduled=True)
        assert result["mismatch"] is True
        assert result["scheduler_message"] != ""

    @pytest.mark.asyncio
    async def test_non_scheduled_keeps_message_even_without_mismatch(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        result = await eero.pause("kid's ipad")
        assert result["scheduler_message"] == "Kid's iPad: paused"

    def test_format_endpoint_result_of_scheduled_success_is_empty(self):
        """End-to-end check of the scheduler's own formatting function
        (api/services/scheduler_store.py) against a pause/resume-shaped
        scheduled-success response — this is what makes the fire loop send
        nothing for it."""
        data = {
            "name": "Kid's iPad", "type": "profile", "requested_paused": True,
            "paused": True, "mismatch": False, "resume_at": None,
            "scheduler_message": "",
        }
        assert _format_endpoint_result(data) == ""


# ---------------------------------------------------------------------------
# Scheduled resume — retry and failure alert
# ---------------------------------------------------------------------------

class TestScheduledResumeRetry:
    @pytest.mark.asyncio
    async def test_scheduled_resume_succeeds_after_transient_failures(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                attempts["n"] += 1
                if attempts["n"] < 3:
                    return httpx.Response(500, json={"error": "transient"})
                return httpx.Response(200, json={"data": {"paused": False}})
            return httpx.Response(200, json={"data": {"paused": False}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        result = await eero.resume("kid's ipad", scheduled=True)
        assert result["paused"] is False
        assert attempts["n"] == 3
        env["human_queue"].assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_resume_exhausts_retries_and_alerts(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroResumeFailed):
            await eero.resume("kid's ipad", scheduled=True)

        env["human_queue"].assert_called_once()
        _, kwargs = env["human_queue"].call_args
        target_name = "Kid's iPad"
        assert kwargs["key"] == f"eero-resume-failed:{eero._normalize(target_name)}"

    @pytest.mark.asyncio
    async def test_scheduled_resume_transport_error_retries_and_alerts(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                attempts["n"] += 1
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"data": {"paused": False}})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroResumeFailed):
            await eero.resume("kid's ipad", scheduled=True)

        assert attempts["n"] == eero._RESUME_RETRY_ATTEMPTS
        env["human_queue"].assert_called_once()
        _, kwargs = env["human_queue"].call_args
        target_name = "Kid's iPad"
        assert kwargs["key"] == f"eero-resume-failed:{eero._normalize(target_name)}"

    @pytest.mark.asyncio
    async def test_scheduled_resume_session_dead_stops_after_one_attempt(self, env, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                attempts["n"] += 1
            # Every call (including the refresh attempt) is rejected, so the
            # session is decided dead on the very first attempt.
            return httpx.Response(401, json={"error": "unauthorized"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        with pytest.raises(eero.EeroResumeFailed):
            await eero.resume("kid's ipad", scheduled=True)

        assert attempts["n"] == 1
        target_name = "Kid's iPad"
        keys = [kwargs["key"] for _, kwargs in env["human_queue"].call_args_list]
        assert keys.count("eero-session") == 1
        assert keys.count(f"eero-resume-failed:{eero._normalize(target_name)}") == 1
        assert len(keys) == 2


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    @pytest.mark.asyncio
    async def test_status_lists_every_target_with_pending_resume(self, env, monkeypatch):
        _write_targets(env, {**PROFILE_TARGETS, **DEVICE_TARGETS})
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        await eero.pause("kid's ipad", minutes=20)
        statuses = await eero.list_status()
        assert len(statuses) == 2
        by_name = {s["name"]: s for s in statuses}
        assert by_name["Kid's iPad"]["resume_at"] is not None
        assert by_name["Guest Laptop"]["resume_at"] is None
        assert all(s["paused"] is True for s in statuses)

    @pytest.mark.asyncio
    async def test_status_with_zero_targets_is_empty_list(self, env):
        assert await eero.list_status() == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(home_route.router)
    return TestClient(app)


class TestRoutes:
    def test_status_503_when_unconfigured(self, env, app_client):
        resp = app_client.get("/api/home/eero/status")
        assert resp.status_code == 503
        assert "LIFEOS_EERO_SESSION_TOKEN" in resp.json()["detail"]

    def test_pause_503_when_unconfigured(self, env, app_client):
        resp = app_client.post("/api/home/eero/anything/pause")
        assert resp.status_code == 503

    def test_pause_404_for_unknown_target(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        resp = app_client.post("/api/home/eero/nope/pause")
        assert resp.status_code == 404
        assert "Kid's iPad" in resp.json()["detail"]

    def test_pause_minutes_zero_is_validation_error(self, env, app_client):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        resp = app_client.post("/api/home/eero/kid's ipad/pause", json={"minutes": 0})
        assert resp.status_code == 422

    def test_pause_minutes_over_1440_is_validation_error(self, env, app_client):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        resp = app_client.post("/api/home/eero/kid's ipad/pause", json={"minutes": 1441})
        assert resp.status_code == 422

    def test_pause_indefinite_with_minutes_is_422(self, env, app_client):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        resp = app_client.post(
            "/api/home/eero/kid's ipad/pause", json={"minutes": 30, "indefinite": True},
        )
        assert resp.status_code == 422

    def test_pause_indefinite_ignores_target_default(self, env, app_client, monkeypatch):
        _write_targets(env, {
            "Kid's iPad": {"type": "profile", "url": "/2.2/networks/12345/profiles/67890", "default_minutes": 45},
        })
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))
        resp = app_client.post("/api/home/eero/kid's ipad/pause", json={"indefinite": True})
        assert resp.status_code == 200
        assert resp.json()["resume_at"] is None

    def test_pause_scheduled_success_has_empty_scheduler_message(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))
        resp = app_client.post("/api/home/eero/kid's ipad/pause", json={"scheduled": True})
        assert resp.status_code == 200
        assert resp.json()["scheduler_message"] == ""

    def test_pause_success_returns_200_with_scheduler_message(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=True)))

        resp = app_client.post("/api/home/eero/kid's ipad/pause")
        assert resp.status_code == 200
        body = resp.json()
        assert body["paused"] is True
        assert "scheduler_message" in body and body["scheduler_message"]

    def test_pause_vendor_error_returns_502(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "nope"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = app_client.post("/api/home/eero/kid's ipad/pause")
        assert resp.status_code == 502

    def test_pause_transport_error_returns_502(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = app_client.post("/api/home/eero/kid's ipad/pause")
        assert resp.status_code == 502

    def test_pause_transport_error_files_eero_api_error_card(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = app_client.post("/api/home/eero/kid's ipad/pause")
        assert resp.status_code == 502

        env["human_queue"].assert_called_once()
        _, kwargs = env["human_queue"].call_args
        assert kwargs["key"] == "eero-api-error"
        env["telegram"].assert_called_once()

    def test_resume_404_for_unknown_target(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("vendor must not be contacted for an unknown target")

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = app_client.post("/api/home/eero/nope/resume")
        assert resp.status_code == 404
        assert "Kid's iPad" in resp.json()["detail"]

    def test_resume_scheduled_failure_returns_502(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        monkeypatch.setattr(eero, "_new_http_client", _client_factory(handler))
        resp = app_client.post("/api/home/eero/kid's ipad/resume", json={"scheduled": True})
        assert resp.status_code == 502

    def test_status_route_returns_targets(self, env, app_client, monkeypatch):
        _write_targets(env, PROFILE_TARGETS)
        _write_token(env, "tok")
        monkeypatch.setattr(eero, "_new_http_client", _client_factory(_ok_handler(paused=False)))
        resp = app_client.get("/api/home/eero/status")
        assert resp.status_code == 200
        assert resp.json()["targets"][0]["name"] == "Kid's iPad"
