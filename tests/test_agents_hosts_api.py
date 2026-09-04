"""Tests for the board's host catalog (#883): `HostCatalog`
(`api/services/agent_worker/host_catalog.py`) and the
`GET /api/agents/hosts` route that wraps it. Every `tailscale status`
call is a stub — no network call and no real subprocess is ever made.
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
import time

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


def test_default_status_runner_does_not_request_text_decoding(monkeypatch):
    """(#901 round 2, finding A2) `subprocess.run(text=True)` decodes
    stdout AND stderr eagerly and raises `UnicodeDecodeError` on invalid
    bytes *inside* subprocess.run itself, before `_probe_peers`'s except
    tuple ever gets a chance — that's a `ValueError`, which round 1's tuple
    didn't catch, so a `tailscale` writing non-UTF-8 (a non-UTF-8 locale,
    an accented device name in an error message, ...) wiped the whole
    registry from the response. The runner must request raw bytes and let
    `_probe_peers` decode defensively instead."""
    calls = []

    def _fake_run(*args, **kwargs):
        calls.append(kwargs)
        return _FakeResult(stdout="{}", returncode=0)

    monkeypatch.setattr(host_catalog_module.subprocess, "run", _fake_run)
    host_catalog_module._default_status_runner()

    assert len(calls) == 1
    assert calls[0].get("text") is not True


@pytest.mark.asyncio
async def test_online_null_on_non_utf8_stdout_bytes(registry, api_host):
    """(#901 round 2, finding A2) `tailscale` writing non-UTF-8 bytes on
    stdout must degrade every registry host to `online: null` — NOT vanish
    them from the response the way an unhandled `UnicodeDecodeError`
    (caught only by the route's blanket backstop, which used to ignore the
    registry entirely) did before this fix."""
    def _runner():
        return _FakeResult(stdout=b"\xff\xfe not valid utf-8 {", returncode=0)
    catalog = HostCatalog(status_runner=_runner, clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert set(by_name) == {"desktop-box", "studio-box", "laptop"}  # registry NOT dropped
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None
    assert by_name["desktop-box"]["online"] is True  # API host unaffected


@pytest.mark.asyncio
async def test_online_true_from_valid_bytes_stdout(registry, api_host):
    """Confirms `_probe_peers`'s bytes-decode path on the HAPPY path too —
    a real `subprocess.run` without `text=True` returns bytes on success,
    not just on failure."""
    peers = {"pubkey-1": {"HostName": "studio-box", "DNSName": "studio-box.tailnet.ts.net.", "Online": True}}
    payload = _status_json(peers).encode("utf-8")
    def _runner():
        return _FakeResult(stdout=payload, returncode=0)
    catalog = HostCatalog(status_runner=_runner, clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is True


@pytest.mark.asyncio
async def test_probe_degrades_on_value_error_from_runner(registry, api_host):
    """Belt-and-braces half of #901 round 2's A2 fix: even if a custom
    `status_runner` raises `UnicodeDecodeError` (a `ValueError` subclass)
    directly — rather than it surfacing from stdout/stderr decoding inside
    `_default_status_runner` — `_probe_peers` must degrade to `[]` rather
    than letting it escape `_build`."""
    def _raise():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    catalog = HostCatalog(status_runner=_raise, clock=_FrozenClock())
    result = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None
    assert by_name["desktop-box"]["online"] is True


def test_probe_peers_itself_catches_value_error(registry, api_host):
    """(#901 round 2, finding A2) Calls `_probe_peers()` DIRECTLY rather
    than going through `get()` — round 2's A4 fix also added a blanket
    `except Exception` around `_build()` in `get()`, which would degrade
    to the same `online: null` content regardless of whether
    `_probe_peers`'s OWN except tuple catches `ValueError` — a test that
    only asserts through `get()` can't tell the two guards apart. This
    pins the guard at the level the finding actually names."""
    def _raise():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    catalog = HostCatalog(status_runner=_raise, clock=_FrozenClock())
    assert catalog._probe_peers() == []


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
async def test_concurrent_cold_get_calls_with_a_raising_build_probe_exactly_once(registry, api_host):
    """(#901 round 2, finding A4) Round 1's lock only coalesced the SUCCESS
    path — when `_build` raises, `_cached` never gets set, so every queued
    waiter re-runs the double-check, misses, and executes its OWN probe
    under the lock: a concurrent-failure storm turned into a serialized
    queue of probes (measured: 30 concurrent failures went from 1.2s/30
    probes with no lock to 36s/30 probes with the naive lock). The fix
    handles a raising build INSIDE the lock and caches the degraded result,
    so N concurrent failing misses must still share ONE probe — same
    gating technique as the success-path test above, but the runner raises
    instead of returning. (`RuntimeError`, not any of `_probe_peers`'s own
    caught exception types, so this exercises `_build`/`get()`'s handling,
    not `_probe_peers`'s.)"""
    gate = threading.Event()

    def _gated_raiser():
        gate.wait(timeout=2.0)
        raise RuntimeError("tailscale exploded")

    catalog = HostCatalog(status_runner=_gated_raiser, clock=_FrozenClock())

    async def _get():
        return await catalog.get(ttl_seconds=86400)

    tasks = [asyncio.create_task(_get()) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert catalog.probe_call_count == 1
    assert all(r == results[0] for r in results)
    by_name = {h["name"]: h for h in results[0]["hosts"]}
    # Positive content, not just "it returned fast": the degraded result
    # must still list EVERY registry host, each at online: null — the
    # registry is preserved, unlike the pre-round-2 route backstop, which
    # dropped it entirely.
    assert set(by_name) == {"desktop-box", "studio-box", "laptop"}
    assert by_name["studio-box"]["online"] is None
    assert by_name["laptop"]["online"] is None
    assert by_name["desktop-box"]["online"] is True


@pytest.mark.asyncio
async def test_negative_ttl_expires_and_a_later_call_reprobes_and_succeeds(registry, api_host):
    """(#901 round 2, finding A4) A degraded result must not be cached
    forever — it sits behind a much shorter negative TTL so the catalog
    retries soon. This proves all three phases: (1) a failing build
    degrades and probes once, (2) a second call still inside the negative
    TTL is a cache hit (no second probe), (3) a call past the negative TTL
    re-probes for real and — critically — SUCCEEDS with fresh content, not
    just "attempted again"."""
    clock = _FrozenClock()
    attempts = {"n": 0}

    def _runner():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("tailscale exploded")
        peers = {"pubkey-1": {"HostName": "studio-box", "DNSName": "studio-box.tailnet.ts.net.", "Online": True}}
        return _FakeResult(stdout=_status_json(peers), returncode=0)

    catalog = HostCatalog(status_runner=_runner, clock=clock)

    first = await catalog.get(ttl_seconds=86400)
    by_name = {h["name"]: h for h in first["hosts"]}
    assert by_name["studio-box"]["online"] is None  # degraded
    assert catalog.probe_call_count == 1

    # Still inside the negative TTL -- must be a cache hit, not a re-probe.
    clock.now += 1
    second = await catalog.get(ttl_seconds=86400)
    assert catalog.probe_call_count == 1
    assert second == first

    # Past the negative TTL -- the next call re-probes, and this time
    # succeeds with real (non-degraded) content.
    clock.now += host_catalog_module._HOST_CATALOG_NEGATIVE_TTL_SECONDS + 1
    third = await catalog.get(ttl_seconds=86400)
    assert catalog.probe_call_count == 2
    by_name3 = {h["name"]: h for h in third["hosts"]}
    assert by_name3["studio-box"]["online"] is True  # a REAL probe succeeded this time


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
    """(#901 round 2, A2/A4/R5) `HostCatalog.get()` itself raising must
    still degrade the route to a 200 — this test forces that by
    monkeypatching `get` wholesale (bypassing HostCatalog's own internal
    degrade-on-build-failure) so it exercises the route's OWN backstop,
    `HostCatalog.degraded()`. `agent_hosts` is pinned to `{}` so the
    result is environment-independent — `degraded()` reads the real
    registry, unlike the pre-round-2 backstop, which ignored it."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

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


def test_get_hosts_endpoint_falls_back_to_bare_api_host_when_degraded_also_raises(client, monkeypatch):
    """(#901 round 3, finding M15) `test_get_hosts_endpoint_degrades_instead_of_500`
    above only exercises the OUTER `try` (`HostCatalog.get()` raising,
    `degraded()` succeeding) -- its assertion is satisfied by `degraded()`
    on an empty registry either way. This pins the route's INNERMOST
    fallback: `degraded()` itself raising too, which the route must still
    turn into a 200 with a single API-host row rather than a 500."""
    async def _raise(self, ttl_seconds=None):
        raise RuntimeError("catalog build blew up")

    def _raise_degraded(self):
        raise RuntimeError("degraded build blew up too")

    monkeypatch.setattr(host_catalog_module.HostCatalog, "get", _raise)
    monkeypatch.setattr(host_catalog_module.HostCatalog, "degraded", _raise_degraded)
    monkeypatch.setattr(host_catalog_module, "_catalog", None)

    from api.services.agent_worker import remote_spawn
    monkeypatch.setattr(remote_spawn, "api_host_name", lambda: "desktop-box")

    resp = client.get("/api/agents/hosts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hosts"] == [{"name": "desktop-box", "ssh_target": None, "online": True, "is_api_host": True}]


def test_get_hosts_route_times_out_and_degrades_with_registry_intact(client, monkeypatch):
    """(#901 round 2, finding R5) The route's "hard 2-second ceiling" was
    only ever an inference from the tailscale probe's own 1.5s timeout,
    which doesn't cover asyncio.Lock queueing or event-loop scheduling —
    measured up to 2.57s under concurrent load. This pins a REAL
    mechanism: `HostCatalog.get()` is forced to hang for 10s (far past any
    plausible ceiling), and the route must still respond well inside its
    own budget, with the DEGRADED registry-at-online-null CONTENT (not
    just "some 200") — proving the `asyncio.wait_for` wrapper is both
    present and wired to the registry-preserving backstop from A4, not the
    pre-round-2 one-row degenerate."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio-box": "operator@studio-box.example"}, raising=False)

    async def _hang(self, ttl_seconds=None):
        await asyncio.sleep(10)
        raise AssertionError("should never resolve — the route's wait_for must give up first")

    monkeypatch.setattr(host_catalog_module.HostCatalog, "get", _hang)
    monkeypatch.setattr(host_catalog_module, "_catalog", None)

    from api.services.agent_worker import remote_spawn
    monkeypatch.setattr(remote_spawn, "api_host_name", lambda: "desktop-box")

    from api.routes import agent_assignment as agent_assignment_module

    start = time.monotonic()
    resp = client.get("/api/agents/hosts")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    # (#901 round 3, finding R10) No bare wall-clock ceiling here — a
    # concurrent `-n 4` run measured this at 1.81s, only 0.19s under a
    # `< 2.0` assertion that would have been the only claim it made. The
    # `+ 0.5` assertion below already pins the MECHANISM (wait_for is
    # wired, not bypassed) with real slack; the product budget itself is
    # asserted separately as a timing-free constant check.
    assert agent_assignment_module._HOSTS_ROUTE_TIMEOUT_SECONDS < 2.0
    assert elapsed < agent_assignment_module._HOSTS_ROUTE_TIMEOUT_SECONDS + 0.5
    body = resp.json()
    names = {h["name"] for h in body["hosts"]}
    assert names == {"desktop-box", "studio-box"}  # registry preserved, not the one-row degenerate
    non_api = next(h for h in body["hosts"] if not h["is_api_host"])
    assert non_api["online"] is None
