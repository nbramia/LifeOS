"""Tests for the board's host catalog (#883): `HostCatalog`
(`api/services/agent_worker/host_catalog.py`) and the
`GET /api/agents/hosts` route that wraps it. Every `tailscale status`
call is a stub — no network call and no real subprocess is ever made.
"""
from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.services.agent_worker import host_catalog as host_catalog_module
from api.services.agent_worker.host_catalog import HostCatalog


pytestmark = pytest.mark.unit


class _FrozenClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _status_json(peers: dict | None = None, self_peer: dict | None = None) -> str:
    import json
    payload = {"Peer": peers or {}}
    if self_peer is not None:
        payload["Self"] = self_peer
    return json.dumps(payload)


def _runner(stdout: str = "", returncode: int = 0):
    def _run():
        return _FakeResult(stdout=stdout, returncode=returncode)
    return _run


@pytest.fixture
def registry(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {
        "studio-box": "operator@studio-box.example",
        "laptop": "operator@laptop.example:2222",
    }, raising=False)


@pytest.fixture
def api_host(monkeypatch):
    from api.services.agent_worker import remote_spawn
    monkeypatch.setattr(remote_spawn, "api_host_name", lambda: "desktop-box")


# ---------------------------------------------------------------------------
# HostCatalog — direct unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_host_entry_always_present_and_online(registry, api_host):
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    api_entries = [h for h in result["hosts"] if h["is_api_host"]]
    assert len(api_entries) == 1
    assert api_entries[0]["name"] == "desktop-box"
    assert api_entries[0]["online"] is True
    assert api_entries[0]["ssh_target"] is None  # not named in the registry in this fixture


@pytest.mark.asyncio
async def test_registry_hosts_appear_with_synthetic_names(registry, api_host):
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    names = {h["name"] for h in result["hosts"]}
    assert names == {"desktop-box", "studio-box", "laptop"}
    non_api = {h["name"]: h for h in result["hosts"] if not h["is_api_host"]}
    assert non_api["studio-box"]["ssh_target"] == "operator@studio-box.example"
    assert non_api["laptop"]["ssh_target"] == "operator@laptop.example:2222"


@pytest.mark.asyncio
async def test_online_true_and_false_from_tailscale_payload(registry, api_host):
    peers = {
        "pubkey-1": {"HostName": "studio-box", "DNSName": "studio-box.tailnet.ts.net.", "Online": True},
        "pubkey-2": {"HostName": "laptop", "DNSName": "laptop.tailnet.ts.net.", "Online": False},
    }
    catalog = HostCatalog(status_runner=_runner(_status_json(peers)), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is True
    assert by_name["laptop"]["online"] is False


@pytest.mark.asyncio
async def test_online_null_when_tailscale_binary_missing(registry, api_host):
    def _raise():
        raise FileNotFoundError("no such file: tailscale")
    catalog = HostCatalog(status_runner=_raise, clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None
    assert by_name["desktop-box"]["online"] is True  # API host unaffected


@pytest.mark.asyncio
async def test_online_null_on_nonzero_exit(registry, api_host):
    catalog = HostCatalog(status_runner=_runner("", returncode=1), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None


@pytest.mark.asyncio
async def test_online_null_on_unparseable_json(registry, api_host):
    catalog = HostCatalog(status_runner=_runner("not json{{{", returncode=0), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None


@pytest.mark.asyncio
async def test_online_null_on_timeout_expired(registry, api_host):
    """Simulates the real production runner's `subprocess.run(timeout=...)`
    raising `TimeoutExpired` on a hung `tailscale status` — the catalog
    must catch it and degrade to `online: null`, not propagate.

    (round 1, finding R3) This test does NOT prove the 2-second budget —
    the fake runner raises `TimeoutExpired` immediately rather than
    actually blocking, so a wall-clock assertion here would be vacuous:
    it could not fail for any implementation, timeout-bounded or not. The
    bound itself is proven by
    `test_default_status_runner_invokes_tailscale_with_bounded_timeout`
    below, which asserts the real runner passes `timeout=` to
    `subprocess.run` at all.
    """
    def _hang():
        raise subprocess.TimeoutExpired(cmd=["tailscale", "status", "--json"], timeout=1.5)
    catalog = HostCatalog(status_runner=_hang, clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None
    assert by_name["desktop-box"]["online"] is True


def test_default_status_runner_invokes_tailscale_with_bounded_timeout(monkeypatch):
    """(round 1, finding R3) The two-second endpoint budget has exactly one
    enforcement mechanism: `_default_status_runner` passing `timeout=` to
    `subprocess.run`. Nothing else in this suite touches
    `_default_status_runner` or the timeout constant directly — deleting
    the `timeout=` argument left the rest of this file's 14 tests green
    (verified as this finding's mutation proof), because every other test
    injects its own `status_runner` stub instead of using the real one."""
    calls = []

    def _fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResult(stdout="{}", returncode=0)

    monkeypatch.setattr(host_catalog_module.subprocess, "run", _fake_run)
    result = host_catalog_module._default_status_runner()

    assert isinstance(result, _FakeResult)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == ["tailscale", "status", "--json"]
    assert kwargs.get("timeout") == host_catalog_module._TAILSCALE_TIMEOUT_SECONDS
    # The constant itself must stay comfortably inside the endpoint's
    # 2-second budget (a regression here would silently blow that budget).
    assert host_catalog_module._TAILSCALE_TIMEOUT_SECONDS < 2.0


@pytest.mark.asyncio
async def test_online_null_for_registry_host_absent_from_peer_list(registry, api_host):
    # tailscale runs fine, but only reports one of the two registry hosts.
    peers = {"pubkey-1": {"HostName": "studio-box", "DNSName": "studio-box.tailnet.ts.net.", "Online": True}}
    catalog = HostCatalog(status_runner=_runner(_status_json(peers)), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is True
    assert by_name["laptop"]["online"] is None  # ran fine, just no matching peer — not `false`


@pytest.mark.asyncio
async def test_no_duplicate_entry_when_registry_names_the_api_host(api_host, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {
        "desktop-box": "operator@desktop-box.example",
        "laptop": "operator@laptop.example",
    }, raising=False)
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    api_entries = [h for h in result["hosts"] if h["name"].lower() == "desktop-box"]
    assert len(api_entries) == 1
    assert api_entries[0]["is_api_host"] is True
    assert api_entries[0]["ssh_target"] == "operator@desktop-box.example"
    assert {h["name"] for h in result["hosts"]} == {"desktop-box", "laptop"}


@pytest.mark.asyncio
async def test_api_host_ssh_target_found_despite_registry_key_case_difference(api_host, monkeypatch):
    """(round 1, finding M1) The dedup guard above already matches a
    registry key against `api_host_name()` case-insensitively — a
    registry entry named e.g. `DESKTOP-BOX` for an API host reported as
    `desktop-box` gets merged into the single `is_api_host: true` row, not
    a duplicate. But merging isn't enough on its own: the `ssh_target`
    lookup for that merged row must use the SAME lowercased key, or the
    entry that WAS found for dedup purposes gets its target dropped on
    the floor by a case-sensitive `registry.get(api_name)`. Synthetic
    names throughout — never this machine's real hostname."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {
        "DESKTOP-BOX": "operator@desktop-box.example",
    }, raising=False)
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    assert {h["name"] for h in result["hosts"]} == {"desktop-box"}  # no duplicate
    api_entry = next(h for h in result["hosts"] if h["is_api_host"])
    assert api_entry["ssh_target"] == "operator@desktop-box.example"


@pytest.mark.asyncio
async def test_ttl_cache_hit_avoids_a_second_probe(registry, api_host):
    clock = _FrozenClock()
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=clock)
    await catalog.get(ttl_seconds=86400)
    assert catalog.probe_call_count == 1
    clock.now += 5  # well within the TTL
    await catalog.get(ttl_seconds=86400)
    assert catalog.probe_call_count == 1  # no second probe


@pytest.mark.asyncio
async def test_ttl_expiry_triggers_a_fresh_probe(registry, api_host):
    clock = _FrozenClock()
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=clock)
    await catalog.get(ttl_seconds=10)
    clock.now += 11
    await catalog.get(ttl_seconds=10)
    assert catalog.probe_call_count == 2


@pytest.mark.asyncio
async def test_concurrent_cold_get_calls_probe_exactly_once(registry, api_host):
    """(round 1, finding R4) N concurrent `get()` calls that all miss the
    cache at once must coalesce into ONE probe, not one per caller — the
    `asyncio.Lock` + double-checked-locking in `get()` is what makes that
    true. `_gated_runner` blocks (on a worker thread, via
    `asyncio.to_thread` — never the event loop thread) until every
    concurrent caller has had a chance to reach `get()` and either win the
    lock race or start waiting on it, so this can't pass by accident of
    scheduling."""
    gate = threading.Event()

    def _gated_runner():
        gate.wait(timeout=2.0)
        return _FakeResult(stdout=_status_json(), returncode=0)

    catalog = HostCatalog(status_runner=_gated_runner, clock=_FrozenClock())

    async def _get():
        return await catalog.get(ttl_seconds=86400)

    tasks = [asyncio.create_task(_get()) for _ in range(5)]
    # Let every task run up to its first suspend point: the lock winner
    # blocks inside asyncio.to_thread (a worker thread, not this loop);
    # the other four suspend waiting on the lock. Neither can progress
    # further until `gate` is set below.
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert catalog.probe_call_count == 1
    assert all(r == results[0] for r in results)


@pytest.mark.asyncio
async def test_registry_read_fresh_on_cache_miss(api_host, monkeypatch):
    from config.settings import settings
    clock = _FrozenClock()
    catalog = HostCatalog(status_runner=_runner(_status_json()), clock=clock)

    monkeypatch.setattr(settings, "agent_hosts", {"studio-box": "operator@studio-box.example"}, raising=False)
    first = await catalog.get(ttl_seconds=1)
    assert {h["name"] for h in first["hosts"]} == {"desktop-box", "studio-box"}

    # TTL expires (frozen clock advances past it), and the registry
    # changes in between — the next build must see the new registry, not
    # a stale snapshot from the first build.
    clock.now += 2
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "operator@laptop.example"}, raising=False)
    second = await catalog.get(ttl_seconds=1)
    assert {h["name"] for h in second["hosts"]} == {"desktop-box", "laptop"}


# ---------------------------------------------------------------------------
# GET /api/agents/hosts — route shape
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_get_hosts_endpoint_shape(client, monkeypatch):
    async def _fake_get(self, ttl_seconds=None):
        return {
            "hosts": [
                {"name": "desktop-box", "ssh_target": None, "online": True, "is_api_host": True},
                {"name": "studio-box", "ssh_target": "operator@studio-box.example", "online": False, "is_api_host": False},
            ],
            "refreshed_at": "2026-01-01T00:00:00Z",
        }
    monkeypatch.setattr(host_catalog_module.HostCatalog, "get", _fake_get)
    monkeypatch.setattr(host_catalog_module, "_catalog", None)

    resp = client.get("/api/agents/hosts")
    assert resp.status_code == 200
    body = resp.json()
    assert "hosts" in body and "refreshed_at" in body
    names = {h["name"] for h in body["hosts"]}
    assert names == {"desktop-box", "studio-box"}
    api_entry = next(h for h in body["hosts"] if h["is_api_host"])
    assert api_entry["online"] is True
    other = next(h for h in body["hosts"] if not h["is_api_host"])
    assert other["online"] is False
    assert other["ssh_target"] == "operator@studio-box.example"


def test_get_hosts_endpoint_degrades_instead_of_500(client, monkeypatch):
    async def _raise(self, ttl_seconds=None):
        raise RuntimeError("catalog build blew up")
    monkeypatch.setattr(host_catalog_module.HostCatalog, "get", _raise)
    monkeypatch.setattr(host_catalog_module, "_catalog", None)

    from api.services.agent_worker import remote_spawn
    monkeypatch.setattr(remote_spawn, "api_host_name", lambda: "desktop-box")

    resp = client.get("/api/agents/hosts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hosts"] == [{"name": "desktop-box", "ssh_target": None, "online": True, "is_api_host": True}]
