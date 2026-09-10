#!/usr/bin/env python3
"""
Child-process entry point for an isolated candidate test instance.

Installs a best-effort outbound-network guard *before* importing the
candidate application at all, then runs it via uvicorn. This is an
accidental-access guard for well-behaved application code — it monkeypatches
`socket` to reject any outbound TCP/UDP connection whose destination isn't
loopback — not a security sandbox boundary. A real OS-level network
namespace was checked and found unavailable in this environment (`bwrap
--unshare-net` fails without additional host privileges this process
doesn't have); untrusted/adversarial code needs a real hosted isolation
boundary, not this guard.

Only inet (AF_INET/AF_INET6) destinations are checked; AF_UNIX socket paths
are left alone since they can't reach off-host resources by construction.

Loopback is allowed by host, but NOT unconditionally: this is exactly where
the operator's real production services live (the shared `lifeos-api`,
ChromaDB, the local LLM server, ...), so a set of well-known LifeOS
production ports is blocked on loopback too, regardless of host. This is
still a fixed, best-effort denylist, not a discovery of what a given
instance's own dependencies happen to use — an instance's own assigned
ports (its API port, its LLM stub) are untouched since they're never in
this set.
"""
import argparse
import os
import signal
import socket
import sys
import threading

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Known LifeOS production ports (see config/settings.py defaults and
# AGENTS.md): lifeos-api, ChromaDB, the local LLM server, the voice
# gateway, lifedb, and the MCP HTTP bridge. Blocked on loopback even though
# the host itself is otherwise allowed.
_BLOCKED_PRODUCTION_PORTS = {8000, 8001, 8080, 8081, 8765, 9788, 11434}


def _install_network_guard() -> None:
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def _check(address) -> None:
        if not isinstance(address, tuple):
            return  # AF_UNIX path or similar — not a network destination
        host = address[0]
        if host not in _ALLOWED_HOSTS:
            raise PermissionError(
                f"test instance network guard: outbound connection to "
                f"{host!r} blocked (only {sorted(_ALLOWED_HOSTS)} allowed)"
            )
        port = address[1] if len(address) > 1 else None
        if port in _BLOCKED_PRODUCTION_PORTS:
            raise PermissionError(
                f"test instance network guard: outbound connection to "
                f"{host!r}:{port} blocked — that port is a known LifeOS "
                f"production service, never an instance's own dependency"
            )

    def guarded_connect(self, address, *args, **kwargs):
        _check(address)
        return real_connect(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.create_connection = guarded_create_connection


def _watch_owner_liveness(fd: int, _instance_dir: str) -> None:
    """Block until the owner-liveness pipe's write end closes — which the
    kernel does automatically when every process holding it exits, for any
    reason, including a SIGKILL nothing in that process could react to —
    then terminate this API group. The owned Chroma supervisor is the single
    shared-directory owner: it waits/reaps API and Chroma before deleting the
    instance directory, so this child must never race it by deleting shared
    vault/data/log paths itself.
    """
    try:
        os.read(fd, 1)  # returns b'' (empty) on EOF; blocks until then
    except OSError:
        pass
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(1)  # reached only if killpg itself failed to kill this process


def _install_perf_provider_fixtures(instance_dir: str) -> None:
    """Deterministic stand-ins for the two external-provider boundaries the
    perf benchmark's candidate-lane mode exercises through real tool
    dispatch, plus a storage redirect no candidate should skip regardless
    of perf usage. Unconditional, like the network guard above: non-perf
    tests never call these tools, so this is a no-op for them.

    - CRM storage: ``PersonEntityStore.CRM_DB_PATH`` resolves relative to
      its own source file, which lands inside this instance's read-only
      source snapshot (``candidate_snapshot.py`` excludes ``data/`` from
      the snapshot, so it would create a *fresh* db there, but still
      inside it). Redirected to this instance's own external data
      directory instead — created by ``TestInstance.start()`` already.
    - Calendar/web search: real Google Calendar and DuckDuckGo are non-loopback
      destinations the network guard above already blocks. Replacing the
      client-construction boundary (matching ``tests/test_calendar.py``'s
      own mocking convention) rather than any higher-level tool logic keeps
      date-range handling, per-account error reporting, result formatting,
      and synthesis all running for real.
    """
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from unittest.mock import MagicMock

    import api.services.calendar as calendar_mod
    import api.services.person_entity as person_entity_mod
    import api.services.web_search as web_search_mod

    person_entity_mod.PersonEntityStore.CRM_DB_PATH = Path(instance_dir) / "data" / "crm.db"

    class _FakeGoogleAuth:
        def get_credentials(self):
            return None

    def _fake_get_google_auth(account_type, credentials_path=None, token_path=None):
        return _FakeGoogleAuth()

    def _fake_build(service_name, version, credentials=None, **kwargs):
        now = datetime.now(timezone.utc)
        events = {"items": [{
            "id": "synthetic-perf-calendar-event",
            "summary": "[synthetic-perf-calendar] Quarterly sync",
            "start": {"dateTime": (now + timedelta(days=1)).isoformat()},
            "end": {"dateTime": (now + timedelta(days=1, hours=1)).isoformat()},
        }]}
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = events
        return service

    calendar_mod.get_google_auth = _fake_get_google_auth
    calendar_mod.build = _fake_build

    def _fake_ddg_search(query, max_results=5):
        return [{
            "title": "Synthetic weather result",
            "url": "https://example.com/synthetic-weather",
            "snippet": "[synthetic-perf-web] Sunny, high of 72F, for the synthetic test location.",
        }]

    web_search_mod._ddg_search = _fake_ddg_search


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--instance-dir", required=True)
    parser.add_argument("--owner-liveness-fd", type=int, default=None)
    args = parser.parse_args(argv)

    _install_network_guard()
    _install_perf_provider_fixtures(args.instance_dir)

    if args.owner_liveness_fd is not None:
        threading.Thread(
            target=_watch_owner_liveness,
            args=(args.owner_liveness_fd, args.instance_dir),
            daemon=True, name="lifeos-test-instance-owner-liveness",
        ).start()

    import uvicorn
    uvicorn.run("api.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main(sys.argv[1:])
