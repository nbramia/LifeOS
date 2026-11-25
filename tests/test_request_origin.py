"""State-changing requests from another site's browser page are refused."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.request_origin import RequestOriginMiddleware, TrustedOrigins

pytestmark = pytest.mark.unit

TAILNET = "box.example-tailnet.ts.net"


@pytest.fixture
def client():
    app = FastAPI()

    @app.post("/act")
    def act():
        return {"ok": True}

    @app.get("/read")
    def read():
        return {"ok": True}

    @app.get("/health/raw-state")
    def raw_state(clear: bool = False):
        return {"cleared": clear}

    app.add_middleware(
        RequestOriginMiddleware,
        trusted=TrustedOrigins(
            ["http://localhost:8000", "https://extra.example"], hosts_any_port=[TAILNET, "testserver"],
        ),
    )
    return TestClient(app)


@pytest.mark.parametrize("headers", [
    {},  # scripts, bots, the MCP bridge: no browser headers
    {"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "http://127.0.0.1:8000"},
    {"Origin": "http://localhost:9788"},
    {"Origin": f"https://{TAILNET}"},
    {"Origin": f"https://{TAILNET}:8443", "Sec-Fetch-Site": "same-site"},
    {"Origin": "https://extra.example"},
])
def test_state_change_allowed(client, headers):
    assert client.post("/act", headers=headers).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1:8000"},
    {"Origin": "https://evil.example"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-site"},
    {"Origin": "null"},
    {"Origin": f"https://{TAILNET}.evil.example"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_state_change_from_another_site_refused(client, headers):
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        r = client.post("/act", headers={**headers, "Content-Type": content_type}, content=b"")
        assert r.status_code == 403


def test_reads_and_navigation_unaffected(client):
    assert client.get("/read", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}).status_code == 200


def test_raw_state_clear_needs_same_origin(client):
    assert client.get("/health/raw-state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    r = client.get("/health/raw-state?clear=true", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert client.get("/health/raw-state?clear=true").json() == {"cleared": True}


def test_real_app_refuses_cross_site_state_change():
    from api.main import app

    c = TestClient(app)
    r = c.post("/api/admin/maintenance?duration_seconds=1", headers={"Host": "localhost:8000", "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/hermes/ask/stream", headers={"Host": "localhost:8000", "Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"},
               content=b'{"question":"synthetic"}')
    assert r.status_code == 403
    r = c.post("/api/voice/turn/stream", headers={"Host": "localhost:8000", "Sec-Fetch-Site": "cross-site"},
               files={"transcript": (None, "synthetic")})
    assert r.status_code == 403


@pytest.mark.parametrize("host,ok", [
    ("localhost:8000", True), ("127.0.0.1:8000", True), ("100.64.0.9:8000", True),
    ("[::1]:8000", True), (TAILNET, True), ("extra.example", True),
    ("rebind.evil.example", False), (f"{TAILNET}.evil.example", False), ("", False),
])
def test_browser_requests_must_name_this_host(client, host, ok):
    browser = {"Host": host, "Sec-Fetch-Site": "same-origin", "User-Agent": "Mozilla/5.0"}
    assert (client.get("/read", headers=browser).status_code == 200) is ok
    assert (client.post("/act", headers=browser).status_code == 200) is ok


def test_non_browser_clients_any_host(client):
    assert client.get("/read", headers={"Host": "nathan-box:8000", "User-Agent": "python-httpx/0.28"}).status_code == 200
    assert client.post("/act", headers={"Host": "nathan-box:8000"}).status_code == 200
