"""
Tests for the two-step, non-interactive eero login script (#1081).

All vendor HTTP calls are mocked (no test contacts the real eero service).
"""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "eero_login.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("eero_login", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch, tmp_path):
    mod = _load_script()
    monkeypatch.setattr(mod.eero, "STATE_PATH", tmp_path / "eero_session.json")
    return mod


class TestLoginStep:
    @pytest.mark.asyncio
    async def test_login_stores_pending_token(self, script, monkeypatch, capsys):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/2.2/login"
            body = json.loads(request.content)
            assert body == {"login": "you@example.com"}
            return httpx.Response(200, json={"data": {"user_token": "temp-token"}})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_login("you@example.com")
        assert rc == 0
        assert json.loads(script.eero.STATE_PATH.read_text()) == {"token": "temp-token"}
        out = capsys.readouterr().out
        assert "temp-token" not in out

    @pytest.mark.asyncio
    async def test_login_failure_returns_nonzero(self, script, monkeypatch, capsys):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "nope"})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_login("you@example.com")
        assert rc == 1
        assert not script.eero.STATE_PATH.exists()

    @pytest.mark.asyncio
    async def test_login_missing_user_token_returns_nonzero(self, script, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {}})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_login("you@example.com")
        assert rc == 1


class TestVerifyStep:
    @pytest.mark.asyncio
    async def test_verify_without_pending_token_returns_nonzero(self, script):
        rc = await script.do_verify("123456")
        assert rc == 1

    @pytest.mark.asyncio
    async def test_verify_promotes_pending_token(self, script, monkeypatch, capsys):
        script.eero._save_token("temp-token")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/2.2/login/verify"
            assert request.headers.get("cookie") == "s=temp-token"
            body = json.loads(request.content)
            assert body == {"code": "123456"}
            return httpx.Response(200, json={"data": {}})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_verify("123456")
        assert rc == 0
        # No new token in the response -> the pending token is kept as persistent.
        assert json.loads(script.eero.STATE_PATH.read_text()) == {"token": "temp-token"}
        out = capsys.readouterr().out
        assert "temp-token" not in out

    @pytest.mark.asyncio
    async def test_verify_uses_a_new_token_when_the_vendor_returns_one(self, script, monkeypatch):
        script.eero._save_token("temp-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"user_token": "persistent-token"}})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_verify("123456")
        assert rc == 0
        assert json.loads(script.eero.STATE_PATH.read_text()) == {"token": "persistent-token"}

    @pytest.mark.asyncio
    async def test_verify_failure_returns_nonzero(self, script, monkeypatch):
        script.eero._save_token("temp-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad code"})

        monkeypatch.setattr(script.eero, "_new_http_client", _make_factory(script, handler))
        rc = await script.do_verify("000000")
        assert rc == 1


def _make_factory(script, handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=script.eero.BASE_URL)
    return factory
