"""Browser-origin check for requests that change state.

The API answers on loopback, the LAN and the tailnet, and its web UI is served
by the API itself, so every request the UI makes is same-origin. A request that
changes state (any method but GET, HEAD and OPTIONS) and that a browser marks
as coming from another site is refused with 403:

- `Sec-Fetch-Site: cross-site` is refused.
- `Sec-Fetch-Site: same-site`, or no `Sec-Fetch-Site` but an `Origin`, passes
  only when the `Origin` is one of this host's own addresses: loopback on any
  port, the tailnet HTTPS host on any port, the CORS origins, and
  `LIFEOS_API_TRUSTED_ORIGINS`. `Origin: null` is refused.
- `Sec-Fetch-Site: same-origin` or `none` passes.

A browser request (one carrying `Origin`, a `Sec-Fetch-*` header or a
`Mozilla/` user agent) of any method must also name this host in `Host`: an
IP address, loopback, the machine's hostname, the tailnet HTTPS host, or a
host from `LIFEOS_API_TRUSTED_ORIGINS`. A browser page cannot otherwise reach
the API under a name of its own choosing.

Clients that are not browsers (scripts, bots, the MCP bridge, the phone apps)
send none of these headers and pass unchanged. `GET /health/raw-state?clear=...`
clears state, so it gets the same check.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _origin_parts(origin: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower()


class TrustedOrigins:
    """The browser origins allowed to make state-changing requests."""

    def __init__(self, origins: Iterable[str], hosts_any_port: Iterable[str] = ()):
        self._exact = {o.strip().rstrip("/").lower() for o in origins if o and o.strip()}
        self._hosts = {h.strip().lower() for h in hosts_any_port if h and h.strip()} | LOOPBACK_HOSTS
        self._exact_hosts = {p[1] for p in map(_origin_parts, self._exact) if p}

    def allows_host(self, host_header: str) -> bool:
        """Whether a `Host` header names this machine (an IP literal always does)."""
        host = host_header.strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        return host.rstrip(".") in self._hosts or host.rstrip(".") in self._exact_hosts

    def allows(self, origin: str) -> bool:
        origin = origin.strip().rstrip("/").lower()
        if origin in self._exact:
            return True
        parts = _origin_parts(origin)
        return parts is not None and parts[1] in self._hosts


def _needs_check(scope) -> bool:
    if scope.get("method", "GET").upper() not in SAFE_METHODS:
        return True
    if scope.get("path") == "/health/raw-state":
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        return "clear" in query
    return False


def _is_browser(headers: dict[str, str]) -> bool:
    return (
        "origin" in headers
        or any(name.startswith("sec-fetch-") for name in headers)
        or headers.get("user-agent", "").startswith("Mozilla/")
    )


def refusal_reason(scope, trusted: TrustedOrigins) -> str | None:
    """Why this request is refused, or None when it may proceed."""
    headers = {}
    for name, value in scope.get("headers") or []:
        headers[name.decode("latin-1").lower()] = value.decode("latin-1")
    if _is_browser(headers) and not trusted.allows_host(headers.get("host", "")):
        return "request for another host name"
    if not _needs_check(scope):
        return None
    site = headers.get("sec-fetch-site", "").strip().lower()
    origin = headers.get("origin")
    if site == "cross-site":
        return "cross-site request"
    if site in ("same-origin", "none"):
        return None
    if origin is None:
        return None if not site else "same-site request without an origin"
    if origin.strip().lower() == "null" or not trusted.allows(origin):
        return "request from another origin"
    return None


class RequestOriginMiddleware:
    """ASGI middleware applying `refusal_reason` to every HTTP request."""

    def __init__(self, app, trusted: TrustedOrigins):
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and refusal_reason(scope, self.trusted) is not None:
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"detail":"cross-origin request refused"}'})
            return
        await self.app(scope, receive, send)
