"""
Tests for the pause_internet / resume_internet / internet_status agent
tools (#1081) — the native chat-tool surface over api/services/home/eero.py.

The eero service functions are monkeypatched directly; no vendor HTTP call
is made from this file (that coverage lives in tests/test_home_eero.py).
"""
import pytest

from api.services.agent_tools import (
    TOOL_DEFINITIONS,
    _TOOL_HANDLERS,
    _tool_pause_internet,
    _tool_resume_internet,
    _tool_internet_status,
)
from api.services.home import eero

pytestmark = pytest.mark.unit


class TestToolRegistration:
    @pytest.mark.parametrize("name", ["pause_internet", "resume_internet", "internet_status"])
    def test_registered_in_definitions_and_handlers(self, name):
        assert any(t["name"] == name for t in TOOL_DEFINITIONS)
        assert name in _TOOL_HANDLERS

    def test_pause_internet_requires_name(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "pause_internet")
        assert tool["input_schema"]["required"] == ["name"]

    def test_resume_internet_requires_name(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "resume_internet")
        assert tool["input_schema"]["required"] == ["name"]


@pytest.fixture(autouse=True)
def _default_configured(monkeypatch):
    """pause_internet/resume_internet/internet_status all gate on
    has_session_token() before doing anything else; default it to True so
    each test below only needs to override it for the unconfigured case."""
    monkeypatch.setattr(eero, "has_session_token", lambda: True)


class TestPauseInternet:
    @pytest.mark.asyncio
    async def test_pause_unconfigured(self, monkeypatch):
        monkeypatch.setattr(eero, "has_session_token", lambda: False)
        out = await _tool_pause_internet({"name": "kid's ipad"})
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_pause_reports_state(self, monkeypatch):
        async def fake_pause(name, minutes=None):
            return {
                "name": "Kid's iPad", "type": "profile", "requested_paused": True,
                "paused": True, "mismatch": False, "resume_at": None,
                "scheduler_message": "Kid's iPad: paused",
            }
        monkeypatch.setattr(eero, "pause", fake_pause)
        out = await _tool_pause_internet({"name": "kid's ipad"})
        assert "paused" in out
        assert "Kid's iPad" in out

    @pytest.mark.asyncio
    async def test_pause_reports_resume_at_when_scheduled(self, monkeypatch):
        async def fake_pause(name, minutes=None):
            return {
                "name": "Kid's iPad", "type": "profile", "requested_paused": True,
                "paused": True, "mismatch": False, "resume_at": "2026-09-15T20:00:00+00:00",
                "scheduler_message": "Kid's iPad: paused",
            }
        monkeypatch.setattr(eero, "pause", fake_pause)
        out = await _tool_pause_internet({"name": "kid's ipad", "minutes": 60})
        assert "auto-resumes" in out

    @pytest.mark.asyncio
    async def test_unknown_target_lists_configured_names(self, monkeypatch):
        async def fake_pause(name, minutes=None):
            raise eero.EeroUnknownTarget(name, "Kid's iPad, Guest Laptop")
        monkeypatch.setattr(eero, "pause", fake_pause)
        out = await _tool_pause_internet({"name": "nonexistent"})
        assert out.startswith("Error:")
        assert "Kid's iPad" in out
        assert "Guest Laptop" in out

    @pytest.mark.asyncio
    async def test_session_dead_is_a_friendly_error(self, monkeypatch):
        async def fake_pause(name, minutes=None):
            raise eero.EeroSessionDead()
        monkeypatch.setattr(eero, "pause", fake_pause)
        out = await _tool_pause_internet({"name": "kid's ipad"})
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_api_error_is_a_friendly_error(self, monkeypatch):
        async def fake_pause(name, minutes=None):
            raise eero.EeroAPIError("vendor rejected the write")
        monkeypatch.setattr(eero, "pause", fake_pause)
        out = await _tool_pause_internet({"name": "kid's ipad"})
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_minutes_as_string_is_rejected_without_vendor_call(self, monkeypatch):
        def fail_resolve(name):
            raise AssertionError("must not resolve target before validating minutes")
        monkeypatch.setattr(eero, "_resolve_target", fail_resolve)

        out = await _tool_pause_internet({"name": "kid's ipad", "minutes": "30"})
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_minutes_zero_is_rejected_without_vendor_call(self, monkeypatch):
        def fail_resolve(name):
            raise AssertionError("must not resolve target before validating minutes")
        monkeypatch.setattr(eero, "_resolve_target", fail_resolve)

        out = await _tool_pause_internet({"name": "kid's ipad", "minutes": 0})
        assert out.startswith("Error:")


class TestResumeInternet:
    @pytest.mark.asyncio
    async def test_resume_reports_state(self, monkeypatch):
        async def fake_resume(name, *, scheduled=False):
            return {
                "name": "Kid's iPad", "type": "profile", "requested_paused": False,
                "paused": False, "mismatch": False, "resume_at": None,
                "scheduler_message": "Kid's iPad: resumed",
            }
        monkeypatch.setattr(eero, "resume", fake_resume)
        out = await _tool_resume_internet({"name": "kid's ipad"})
        assert "resumed" in out

    @pytest.mark.asyncio
    async def test_unknown_target_lists_configured_names(self, monkeypatch):
        async def fake_resume(name, *, scheduled=False):
            raise eero.EeroUnknownTarget(name, "Kid's iPad")
        monkeypatch.setattr(eero, "resume", fake_resume)
        out = await _tool_resume_internet({"name": "nonexistent"})
        assert out.startswith("Error:")
        assert "Kid's iPad" in out


class TestInternetStatus:
    @pytest.mark.asyncio
    async def test_status_lists_targets(self, monkeypatch):
        monkeypatch.setattr(eero, "has_session_token", lambda: True)

        async def fake_list_status():
            return [
                {"name": "Kid's iPad", "type": "profile", "paused": True, "resume_at": "2026-09-15T20:00:00+00:00"},
                {"name": "Guest Laptop", "type": "device", "paused": False, "resume_at": None},
            ]
        monkeypatch.setattr(eero, "list_status", fake_list_status)
        out = await _tool_internet_status({})
        assert "Kid's iPad" in out and "paused" in out
        assert "Guest Laptop" in out and "resumed" in out

    @pytest.mark.asyncio
    async def test_status_unconfigured(self, monkeypatch):
        monkeypatch.setattr(eero, "has_session_token", lambda: False)
        out = await _tool_internet_status({})
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_status_zero_targets(self, monkeypatch):
        monkeypatch.setattr(eero, "has_session_token", lambda: True)

        async def fake_list_status():
            return []
        monkeypatch.setattr(eero, "list_status", fake_list_status)
        out = await _tool_internet_status({})
        assert "No eero targets configured" in out
