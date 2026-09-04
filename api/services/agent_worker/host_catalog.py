"""Host catalog for the board's assignment picker (#883).

`GET /api/agents/hosts` (`api/routes/agent_assignment.py`) needs "what
machines can a card be assigned to run on, and are they reachable" —
mirrors `model_catalog.py`'s injectable-everything TTL-cached shape, kept
intentionally simpler because there's only one probe (`tailscale status
--json`) instead of four provider fetches.

The list is always: the API host itself (`is_api_host: true`, always
`online: true` — the API process IS this host, no probe needed) plus
every entry in `settings.agent_hosts` (`LIFEOS_AGENT_HOSTS`), deduplicated
by name so a registry entry that happens to name the API host doesn't
produce two rows for it (that merged row still carries the registry's
`ssh_target` and stays `is_api_host: true`).

Reachability for every OTHER host comes from `tailscale status --json`,
run through an injectable `status_runner` so tests never shell out. A
registry host is matched (case-insensitively) against each Tailscale peer
— including `Self`, in case a registry entry happens to name the API host
under an ssh alias that doesn't match its own hostname — by comparing the
registry name and the ssh_target (with any `user@` prefix and `:port`
suffix stripped) against the peer's `HostName` and the first label of its
`DNSName`. `online` is `null` (never guessed `false`) whenever the signal
is inconclusive: `tailscale` isn't installed, the command fails, times
out, or returns unparseable JSON, or it ran fine but this particular host
simply isn't a known peer — a host can be reachable over plain LAN ssh
with no Tailscale involvement at all, so calling that `false` would be a
lie the picker has no business telling.

Cached for `_HOST_CATALOG_TTL_SECONDS` (30s — deliberately much shorter
than the model catalog's 24h; reachability is the kind of thing that
changes minute to minute, and the probe itself is cheap and bounded).
Unlike the model catalog, a cache MISS always re-reads
`settings.agent_hosts` fresh rather than reusing whatever names a
previous build saw — the registry is local config, not a network call,
so there's no reason to let it go stale between probes.

The probe itself is bounded well under the endpoint's 2-second budget:
`_TAILSCALE_TIMEOUT_SECONDS` (1.5s) is passed straight to
`subprocess.run(..., timeout=...)`, and `subprocess.TimeoutExpired` is
caught alongside every other probe failure — a hung or missing
`tailscale` binary degrades every non-API-host row to `online: null`,
never blocks the request past that ceiling, and never 500s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config.settings import settings


logger = logging.getLogger(__name__)

_TAILSCALE_TIMEOUT_SECONDS = 1.5
_HOST_CATALOG_TTL_SECONDS = 30

StatusRunner = Callable[[], "subprocess.CompletedProcess"]


def _default_status_runner() -> "subprocess.CompletedProcess":
    return subprocess.run(  # noqa: S603, S607 — fixed argv, no shell=True
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=_TAILSCALE_TIMEOUT_SECONDS,
        check=False,
    )


def _strip_target(value: str) -> str:
    """`user@host:port` -> `host`, lowercased — for matching a registry
    ssh_target against a Tailscale peer's HostName/DNSName."""
    value = (value or "").strip()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):  # bracketed IPv6 literal, optionally with :port
        value = value.split("]", 1)[0].lstrip("[")
    else:
        value = value.split(":", 1)[0]
    return value.lower()


def _peer_labels(peer: dict) -> set[str]:
    labels: set[str] = set()
    host_name = peer.get("HostName")
    if host_name:
        labels.add(str(host_name).lower())
    dns_name = peer.get("DNSName")
    if dns_name:
        first_label = str(dns_name).split(".", 1)[0]
        if first_label:
            labels.add(first_label.lower())
    return labels


def _online_for(name: str, ssh_target: Optional[str], peers: list[dict]) -> Optional[bool]:
    candidates = {(name or "").strip().lower()}
    if ssh_target:
        candidates.add(_strip_target(ssh_target))
    candidates.discard("")
    for peer in peers:
        if candidates & _peer_labels(peer):
            online = peer.get("Online")
            return bool(online) if isinstance(online, bool) else None
    return None


@dataclass
class HostCatalog:
    """Injectable-everything catalog builder (status_runner + clock test
    seams), mirroring `ModelCatalog`'s pattern. `probe_call_count` lets
    tests assert the TTL cache actually skips a second probe."""

    status_runner: Optional[StatusRunner] = None
    clock: Callable[[], float] = field(default=time.monotonic)

    probe_call_count: int = field(default=0, init=False)

    _cached: Optional[dict] = field(default=None, init=False, repr=False)
    _cached_at: Optional[float] = field(default=None, init=False, repr=False)

    async def get(self, *, ttl_seconds: Optional[int] = None) -> dict:
        ttl = ttl_seconds if ttl_seconds is not None else _HOST_CATALOG_TTL_SECONDS
        now = self.clock()
        if self._cached is not None and self._cached_at is not None and (now - self._cached_at) < ttl:
            return self._cached
        result = await asyncio.to_thread(self._build)
        self._cached = result
        self._cached_at = now
        return result

    def _build(self) -> dict:
        from api.services.agent_worker.remote_spawn import api_host_name

        api_name = api_host_name()
        registry = dict(settings.agent_hosts)  # read fresh on every cache miss
        peers = self._probe_peers()

        hosts: list[dict] = [{
            "name": api_name,
            "ssh_target": registry.get(api_name),
            "online": True,
            "is_api_host": True,
        }]
        seen = {api_name.strip().lower()}
        for name, target in registry.items():
            key = (name or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            hosts.append({
                "name": name,
                "ssh_target": target,
                "online": _online_for(name, target, peers),
                "is_api_host": False,
            })
        return {
            "hosts": hosts,
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _probe_peers(self) -> list[dict]:
        runner = self.status_runner or _default_status_runner
        self.probe_call_count += 1
        try:
            result = runner()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.info("tailscale status probe unavailable: %s", exc)
            return []
        returncode = getattr(result, "returncode", None)
        if returncode != 0:
            logger.info("tailscale status exited %s", returncode)
            return []
        try:
            data = json.loads(getattr(result, "stdout", "") or "")
        except (json.JSONDecodeError, TypeError):
            logger.info("tailscale status returned unparseable JSON")
            return []
        if not isinstance(data, dict):
            return []
        peers: list[dict] = []
        self_peer = data.get("Self")
        if isinstance(self_peer, dict):
            peers.append(self_peer)
        peer_map = data.get("Peer")
        if isinstance(peer_map, dict):
            peers.extend(p for p in peer_map.values() if isinstance(p, dict))
        return peers


# Process-wide singleton — mirrors model_catalog.py's pattern. Tests
# construct their own HostCatalog() with stub seams instead of touching
# this singleton (except the route-level shape test, which patches it).
_catalog: Optional[HostCatalog] = None


def get_host_catalog() -> HostCatalog:
    global _catalog
    if _catalog is None:
        _catalog = HostCatalog()
    return _catalog
