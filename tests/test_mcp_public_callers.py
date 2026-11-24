"""The public MCP listener's caller filter: classification, modes, the OpenAI list, alerts."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import mcp_server
from api.services import mcp_public_callers as mpc

pytestmark = pytest.mark.unit

ANTHROPIC_IP = "160.79.104.5"
OPENAI_NET = "203.0.113.0/24"
OPENAI_IP = "203.0.113.9"
OUTSIDE_IP = "198.51.100.7"


def openai_doc(n: int = 12, v6: bool = True) -> bytes:
    prefixes = [{"ipv4Prefix": OPENAI_NET}]
    prefixes += [{"ipv4Prefix": f"192.0.2.{i * 8}/29"} for i in range(n - 2)]
    if v6:
        prefixes.append({"ipv6Prefix": "2001:db8:abcd::/48"})
    return json.dumps({"creationTime": "2026-11-01T00:00:00Z", "prefixes": prefixes}).encode()


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def notices():
    return []


@pytest.fixture
def clock():
    return Clock()


def cache_file(tmp_path):
    return tmp_path / "cache" / mpc.CACHE_FILENAME


def make_filter(mode, tmp_path, notices, clock, *, extra=None, fetch=None, preload=True):
    cache = cache_file(tmp_path)
    if preload:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(openai_doc())
    return mpc.PublicCallerFilter(
        mode,
        extra_ranges=mpc.parse_networks(extra or [], source="test"),
        cache_path=cache,
        fetch=fetch or (lambda url: openai_doc()),
        notify=notices.append,
        clock=clock,
    )


async def _ok(request):
    return PlainTextResponse("ok")


def inner_app():
    return Starlette(routes=[
        Route("/mcp", _ok, methods=["GET", "POST"]),
        Route("/oauth/token", _ok, methods=["POST"]),
    ])


def client(filt):
    return TestClient(mcp_server.public_socket_app(inner_app(), filt))


def xff(value):
    return {"X-Forwarded-For": value}


# -- off ------------------------------------------------------------------

def test_off_admits_everything_without_logging(tmp_path, notices, clock, caplog):
    c = client(make_filter("off", tmp_path, notices, clock))
    with caplog.at_level(logging.INFO):
        assert c.get("/mcp").status_code == 200
        assert c.get("/mcp", headers=xff(OUTSIDE_IP)).status_code == 200
    assert "mcp public caller" not in caplog.text


def test_no_filter_object_is_unchanged():
    c = TestClient(mcp_server.public_socket_app(inner_app()))
    assert c.get("/mcp").status_code == 200


# -- report -----------------------------------------------------------------

def test_report_passes_all_and_logs_provider(tmp_path, notices, clock, caplog):
    c = client(make_filter("report", tmp_path, notices, clock))
    with caplog.at_level(logging.INFO):
        assert c.get("/mcp", headers=xff(ANTHROPIC_IP)).status_code == 200
        assert c.get("/mcp", headers=xff(OPENAI_IP)).status_code == 200
        assert c.get("/mcp", headers=xff(OUTSIDE_IP)).status_code == 200
        assert c.get("/mcp").status_code == 200
    lines = [r.getMessage() for r in caplog.records if "mcp public caller" in r.getMessage()]
    assert "mcp public caller: mode=report provider=anthropic path=/mcp" in lines
    assert "mcp public caller: mode=report provider=openai path=/mcp" in lines
    assert f"mcp public caller: mode=report provider=outside path=/mcp addr={OUTSIDE_IP}" in lines
    assert "mcp public caller: mode=report provider=unknown path=/mcp addr=unknown" in lines
    assert not any(ANTHROPIC_IP in line or OPENAI_IP in line for line in lines)


# -- enforce ----------------------------------------------------------------

def test_enforce_admits_providers(tmp_path, notices, clock, caplog):
    c = client(make_filter("enforce", tmp_path, notices, clock, extra=["192.0.2.128/25"]))
    with caplog.at_level(logging.INFO):
        for ip in (ANTHROPIC_IP, OPENAI_IP, "2001:db8:abcd::1", "192.0.2.200"):
            assert c.get("/mcp", headers=xff(ip)).status_code == 200, ip
    assert "mcp public caller" not in caplog.text


@pytest.mark.parametrize("headers", [
    xff(OUTSIDE_IP), {}, xff("garbage"), xff(""), xff("2001:db8:ffff::1"),
])
def test_enforce_refuses(headers, tmp_path, notices, clock, caplog):
    c = client(make_filter("enforce", tmp_path, notices, clock))
    with caplog.at_level(logging.WARNING):
        r = c.get("/mcp", headers=headers)
    assert r.status_code == 403
    assert r.text == "Forbidden"
    assert r.headers["content-type"].startswith("text/plain")
    assert "mcp public caller refused: provider=" in caplog.text


def test_refusal_log_format(tmp_path, notices, clock, caplog):
    c = client(make_filter("enforce", tmp_path, notices, clock))
    with caplog.at_level(logging.WARNING):
        c.get("/mcp", headers=xff(OUTSIDE_IP))
    assert (f"mcp public caller refused: provider=outside addr={OUTSIDE_IP} path=/mcp"
            in caplog.text)


def test_last_xff_entry_wins(tmp_path, notices, clock):
    c = client(make_filter("enforce", tmp_path, notices, clock))
    assert c.get("/mcp", headers=xff(f"1.2.3.4, {ANTHROPIC_IP}")).status_code == 200
    assert c.get("/mcp", headers=xff(f"{ANTHROPIC_IP}, 1.2.3.4")).status_code == 403


def test_extra_ranges_and_invalid_entries(tmp_path, notices, clock, caplog):
    with caplog.at_level(logging.WARNING):
        filt = make_filter("enforce", tmp_path, notices, clock, extra=["10.9.0.0/16", "nonsense"])
    assert "skipping invalid network" in caplog.text
    assert filt.classify("10.9.4.4") == "extra"
    assert filt.classify("10.8.4.4") == "outside"


def test_ipv6_classification(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock)
    assert filt.classify("2001:db8:abcd:1::5") == "openai"
    assert filt.classify("2001:db8:1::5") == "outside"
    assert filt.classify(None) == "unknown"
    assert filt.classify("not-an-ip") == "unknown"


def test_websocket_is_closed_when_refused(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock)
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    app = mcp_server.public_socket_app(inner, filt)
    sent = []

    async def send(msg):
        sent.append(msg)

    import asyncio
    scope = {"type": "websocket", "path": "/mcp", "headers": [(b"x-forwarded-for", b"198.51.100.7")]}
    asyncio.run(app(scope, None, send))
    assert reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]
    scope["headers"] = [(b"x-forwarded-for", ANTHROPIC_IP.encode())]
    asyncio.run(app(scope, None, send))
    assert reached == ["websocket"]


def test_path_allowlist_404_comes_first(tmp_path, notices, clock):
    c = client(make_filter("enforce", tmp_path, notices, clock))
    assert c.get("/consent", headers=xff(OUTSIDE_IP)).status_code == 404
    assert c.get("/consent", headers=xff(ANTHROPIC_IP)).status_code == 404


def test_run_http_filters_only_the_public_listener(tmp_path, notices, clock, monkeypatch):
    import uvicorn

    filt = make_filter("enforce", tmp_path, notices, clock)
    monkeypatch.setattr(filt, "start", lambda: None)
    monkeypatch.setattr(mpc.PublicCallerFilter, "from_settings", classmethod(lambda cls, **kw: filt))
    configs = []

    class FakeServer:
        def __init__(self, config):
            configs.append(config)
            self.started = True
            self.should_exit = False

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kw: {"app": app, **kw})
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    mcp_server.run_http(
        mcp_server.LifeOSMCPServer(), "127.0.0.1", 0, "tok", uds=str(tmp_path / "s" / "mcp.sock"),
    )
    tcp = next(c["app"] for c in configs if not c.get("uds"))
    public = next(c["app"] for c in configs if c.get("uds"))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    bearer = {"Authorization": "Bearer tok"}
    # The TCP listener (local and tailnet clients) never consults the filter.
    assert TestClient(tcp).post("/mcp", headers={**bearer, **xff(OUTSIDE_IP)}, json=ping).status_code == 200
    assert TestClient(tcp).post("/mcp", headers=bearer, json=ping).status_code == 200
    # The public listener does.
    assert TestClient(public).post("/mcp", headers={**bearer, **xff(OUTSIDE_IP)}, json=ping).status_code == 403


def test_ipv4_mapped_ipv6_is_classified_as_its_ipv4(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock)
    assert filt.classify(f"::ffff:{ANTHROPIC_IP}") == "anthropic"
    assert filt.classify(f"::ffff:{OUTSIDE_IP}") == "outside"


def test_empty_openai_list_is_retried_soon(tmp_path, notices, clock):
    calls = []

    def failing(url):
        calls.append(url)
        raise OSError("down")

    filt = mpc.PublicCallerFilter("enforce", cache_path=tmp_path / "c.json", fetch=failing,
                                  notify=notices.append, clock=clock)
    assert filt.next_tick_seconds() == mpc.EMPTY_RETRY_SECONDS
    filt.tick()
    assert len(calls) == 1 and filt.next_tick_seconds() == mpc.EMPTY_RETRY_SECONDS


# -- alerts -------------------------------------------------------------------

def test_credentials_alert_throttled_hourly(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock)
    c = client(filt)
    c.get("/mcp", headers={**xff(OUTSIDE_IP), "Authorization": "Bearer s3cret"})
    c.post("/oauth/token", headers=xff(OUTSIDE_IP))
    c.get("/mcp", headers=xff(OUTSIDE_IP))  # no credentials: no alert
    filt.flush()
    assert notices == [
        f"LifeOS connector: refused a call carrying app credentials from {OUTSIDE_IP}, "
        "outside the Anthropic and OpenAI ranges. If Claude or ChatGPT stopped reaching "
        "LifeOS, set LIFEOS_MCP_PUBLIC_CALLERS=report and restart lifeos-mcp-http."
    ]
    assert "s3cret" not in notices[0]
    clock.now += 3601
    c.post("/oauth/token", headers=xff(OUTSIDE_IP))
    filt.flush()
    assert len(notices) == 2


def test_no_credentials_alert_without_credentials_or_in_report(tmp_path, notices, clock):
    filt = make_filter("report", tmp_path, notices, clock)
    client(filt).get("/mcp", headers={**xff(OUTSIDE_IP), "Authorization": "Bearer x"})
    filt.flush()
    assert notices == []


def test_notifier_exception_is_swallowed(tmp_path, clock, caplog):
    def boom(text):
        raise RuntimeError("telegram down")

    filt = mpc.PublicCallerFilter("enforce", cache_path=tmp_path / "c.json",
                                  fetch=lambda u: openai_doc(), notify=boom, clock=clock)
    with caplog.at_level(logging.ERROR):
        filt.credentials_refused("198.51.100.7")
        filt.flush()
    assert "public caller notice failed" in caplog.text


def test_stale_alert_once_per_day(tmp_path, notices, clock):
    def failing(url):
        raise OSError("offline")

    filt = make_filter("enforce", tmp_path, notices, clock, fetch=failing, preload=False)
    filt.tick()
    filt.flush()
    assert notices == [
        "LifeOS connector: the ChatGPT address list has never loaded; "
        "ChatGPT calls may be refused if its addresses changed."
    ]
    clock.now += 3600
    filt.tick()
    filt.flush()
    assert len(notices) == 1
    clock.now += 86400
    filt.tick()
    filt.flush()
    assert len(notices) == 2


def test_stale_alert_reports_days_and_off_mode_is_silent(tmp_path, notices, clock):
    fetched = {"ok": True}

    def fetch(url):
        if not fetched["ok"]:
            raise OSError("offline")
        return openai_doc()

    filt = make_filter("enforce", tmp_path, notices, clock, fetch=fetch, preload=False)
    filt.tick()
    filt.flush()
    assert notices == []
    fetched["ok"] = False
    clock.now += 4 * 86400
    filt.tick()
    filt.flush()
    assert notices == [
        "LifeOS connector: the ChatGPT address list has not refreshed for 4 days; "
        "ChatGPT calls may be refused if its addresses changed."
    ]
    off = make_filter("off", tmp_path, [], clock, fetch=fetch, preload=False)
    off.tick()
    off.flush()


# -- OpenAI list loader ---------------------------------------------------------

def test_good_file_loads_and_is_cached_atomically(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock, preload=False)
    assert filt.classify(OPENAI_IP) == "outside"
    assert filt.refresh() is True
    assert filt.classify(OPENAI_IP) == "openai"
    assert (cache_file(tmp_path)).read_bytes() == openai_doc()
    assert [p.name for p in cache_file(tmp_path).parent.iterdir()] == [mpc.CACHE_FILENAME]


def test_cache_is_loaded_at_start(tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock)
    assert filt.classify(OPENAI_IP) == "openai"
    assert filt._last_refresh is not None


def test_too_small_file_rejected_and_last_good_kept(tmp_path, notices, clock, caplog):
    docs = [openai_doc(), openai_doc(3, v6=False)]
    filt = make_filter("enforce", tmp_path, notices, clock, fetch=lambda u: docs.pop(), preload=False)
    assert filt.refresh() is False  # the small one pops first
    assert filt.classify(OPENAI_IP) == "outside"
    assert not (cache_file(tmp_path)).exists()
    assert filt.refresh() is True
    docs.append(openai_doc(3, v6=False))
    cached = (cache_file(tmp_path)).read_bytes()
    with caplog.at_level(logging.WARNING):
        assert filt.refresh() is False
    assert "unusable" in caplog.text
    assert filt.classify(OPENAI_IP) == "openai"
    assert (cache_file(tmp_path)).read_bytes() == cached


@pytest.mark.parametrize("raw", [b"<html>nope</html>", b"[]", b'{"prefixes": 4}', b'{"prefixes": ["x"]}'])
def test_unparsable_file_rejected(raw, tmp_path, notices, clock):
    filt = make_filter("enforce", tmp_path, notices, clock, fetch=lambda u: raw)
    assert filt.refresh() is False
    assert filt.classify(OPENAI_IP) == "openai"  # last good list from the cache


def test_refresh_interval_honoured(tmp_path, notices, clock):
    calls = []

    def fetch(url):
        calls.append(url)
        return openai_doc()

    filt = make_filter("enforce", tmp_path, notices, clock, fetch=fetch, preload=False)
    filt.tick()
    assert len(calls) == 1
    clock.now += 11 * 3600
    filt.tick()
    assert len(calls) == 1
    clock.now += 2 * 3600
    filt.tick()
    assert len(calls) == 2


def test_mode_normalization(caplog):
    assert mpc.normalize_mode(None) == "off"
    assert mpc.normalize_mode(" Enforce ") == "enforce"
    with caplog.at_level(logging.WARNING):
        assert mpc.normalize_mode("block") == "off"
    assert "LIFEOS_MCP_PUBLIC_CALLERS" in caplog.text
