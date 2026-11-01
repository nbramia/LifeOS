"""Single-operator OAuth 2.1 authorization server state for the MCP HTTP transport.

This module owns the SQLite store (`mcp_oauth.db`) behind the OAuth endpoints
that `api/services/mcp_oauth_http.py` mounts on the MCP HTTP app: registered
clients, pending consent requests, authorization codes, access and refresh
tokens, and the refresh chains that group them.

Invariants:

- Secrets (client secrets, authorization codes, access and refresh tokens,
  consent CSRF tokens) are stored only as SHA-256 hashes. Each is 256 bits of
  `secrets` randomness, so an unsalted hash is not brute-forceable.
- PKCE is S256 only, and every code is single use. Claiming a code (or
  rotating a refresh token) is one conditional `UPDATE`, so two concurrent
  redemptions cannot both succeed.
- A refresh chain is the unit of revocation: a code exchange starts one, each
  refresh rotates within it, and replaying a consumed code or refresh token
  revokes the whole chain (every access and refresh token issued from it).
- Nothing here logs a secret or the operator's identity header value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

from api.services.sqlite_connect import connect_closing

logger = logging.getLogger(__name__)

# The one tool tier an OAuth grant carries. `api/services/mcp_tool_tier.py`
# maps it to the allowlisted tools.
TIER_READ_SAFE_WRITES = "read_safe_writes"

# Headers a public edge adds to every request it relays and a client cannot
# strip: Tailscale Funnel's marker and Cloudflare Tunnel's client address.
# A client can add them, which only makes its own request stricter.
PUBLIC_EDGE_HEADERS = ("tailscale-funnel-request", "cf-connecting-ip")

# The scope string advertised in metadata and returned with tokens. Requested
# scopes do not widen a grant: every token carries exactly this tier.
OAUTH_SCOPE = "lifeos:read-safe-writes"

AUTH_CODE_TTL_SECONDS = 60
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
PENDING_CONSENT_TTL_SECONDS = 600
# Unauthenticated DCR must neither grow the store without bound nor lock out a
# real app. An unapproved registration older than UNAPPROVED_CLIENT_TTL_SECONDS
# is deleted on every registration and authorization request. At
# MAX_PENDING_CLIENTS unapproved registrations, a new one evicts the oldest;
# a registration with a live consent or an unexpired code is never deleted
# by either path.
UNAPPROVED_CLIENT_TTL_SECONDS = 15 * 60
MAX_PENDING_CLIENTS = 5000
# Hosts an https redirect URI may name; loopback hosts are always allowed.
DEFAULT_ALLOWED_REDIRECT_HOSTS = frozenset({"claude.ai", "claude.com", "chatgpt.com"})
# SQL predicate: this unapproved client is not protected by a live consent or
# an unexpired, unused code. Takes the current time as its only parameter.
_UNPROTECTED_CLIENT_SQL = (
    "NOT EXISTS (SELECT 1 FROM pending_consents p WHERE p.client_id = clients.client_id "
    "AND p.expires_at > :now) "
    "AND NOT EXISTS (SELECT 1 FROM auth_codes a WHERE a.client_id = clients.client_id "
    "AND a.used_at IS NULL AND a.expires_at > :now)"
)
MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 2048
MAX_CLIENT_NAME_LENGTH = 100

AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_ACCESS_PREFIX = "lfo_at_"
_REFRESH_PREFIX = "lfo_rt_"
_CODE_PREFIX = "lfo_ac_"
_SECRET_PREFIX = "lfo_cs_"


class OAuthError(Exception):
    """An RFC 6749 error: `error` is the wire code, `description` is safe to show."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(error)
        self.error = error
        self.description = description
        self.status = status


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def is_pkce_value(value: str) -> bool:
    """RFC 7636 §4.1 character set and 43..128 length (verifier and S256 challenge alike)."""
    if not 43 <= len(value) <= 128:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return all(ch in allowed for ch in value)


def validate_redirect_uri(uri: object, allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_REDIRECT_HOSTS) -> str:
    """Return `uri` if it is an https URI on an allowed host, or a loopback URI on any port."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        raise OAuthError("invalid_redirect_uri", "redirect_uris must be non-empty strings")
    parts = urlsplit(uri)
    if parts.fragment or "#" in uri:
        raise OAuthError("invalid_redirect_uri", "redirect URI must not contain a fragment")
    if any(ord(ch) < 0x21 for ch in uri):
        raise OAuthError("invalid_redirect_uri", "redirect URI contains whitespace or control characters")
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host in LOOPBACK_HOSTS:
        return uri
    if parts.scheme == "https" and host and host in allowed_hosts:
        return uri
    raise OAuthError(
        "invalid_redirect_uri",
        "redirect URIs must use https on an allowed host, or a loopback host",
    )


def redirect_uri_matches(registered: Iterable[str], requested: str) -> bool:
    """Exact match, except an http loopback URI matches on any port (RFC 8252 §7.3)."""
    for candidate in registered:
        if candidate == requested:
            return True
        reg, req = urlsplit(candidate), urlsplit(requested)
        if (
            reg.scheme == req.scheme == "http"
            and (reg.hostname or "").lower() in LOOPBACK_HOSTS
            and (reg.hostname or "").lower() == (req.hostname or "").lower()
            and reg.path == req.path
            and reg.query == req.query
        ):
            return True
    return False


@dataclass(frozen=True)
class OAuthConfig:
    """Runtime configuration for the OAuth endpoints on the MCP HTTP app."""

    issuer: str
    operator_logins: frozenset[str]
    store: "OAuthStore"
    authorize_url: str = ""
    allowed_redirect_hosts: frozenset[str] = DEFAULT_ALLOWED_REDIRECT_HOSTS

    def __post_init__(self):
        object.__setattr__(self, "issuer", self.issuer.rstrip("/"))
        object.__setattr__(
            self,
            "operator_logins",
            frozenset(login.strip().lower() for login in self.operator_logins if login.strip()),
        )
        if not self.authorize_url:
            object.__setattr__(self, "authorize_url", f"{self.issuer}/oauth/authorize")
        object.__setattr__(
            self,
            "allowed_redirect_hosts",
            frozenset(h.strip().lower() for h in self.allowed_redirect_hosts if h.strip()),
        )

    @property
    def resource(self) -> str:
        """Canonical URI of the protected MCP endpoint (RFC 8707 / RFC 9728 `resource`)."""
        return f"{self.issuer}/mcp"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource/mcp"

    def resource_matches(self, value: str) -> bool:
        """Whether a client's `resource` parameter names this server.

        Accepts the MCP endpoint URI or the bare origin, ignoring a trailing
        slash and scheme/host case.
        """
        def norm(uri: str) -> str:
            parts = urlsplit(uri.strip())
            netloc = parts.netloc.lower()
            return f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"

        if not value or "#" in value:
            return False
        wanted = norm(value)
        return wanted in {norm(self.resource), norm(self.issuer)}


@dataclass
class AccessGrant:
    """A validated access token's binding."""

    client_id: str
    chain_id: str
    tier: str


@dataclass
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = OAUTH_SCOPE
    extra: dict = field(default_factory=dict)

    def as_response(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL,
    client_secret_hash TEXT,
    created_at REAL NOT NULL,
    approved_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pending_consents (
    request_id TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    operator_login_hash TEXT NOT NULL,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    state TEXT,
    resource TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    tier TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    chain_id TEXT
);
CREATE TABLE IF NOT EXISTS chains (
    chain_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL,
    revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_chain ON tokens(chain_id);
CREATE INDEX IF NOT EXISTS idx_chains_client ON chains(client_id);
"""


class OAuthStore:
    """SQLite-backed OAuth state. Every public method opens and closes its own connection."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time):
        self.db_path = str(db_path)
        self._clock = clock
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with connect_closing(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self):
        return connect_closing(self.db_path, timeout=10)

    def now(self) -> float:
        return self._clock()

    # ── Clients ────────────────────────────────────────────────────────

    def register_client(
        self,
        metadata: dict,
        *,
        allowed_redirect_hosts: frozenset[str] = DEFAULT_ALLOWED_REDIRECT_HOSTS,
    ) -> dict:
        """RFC 7591 registration. Returns the client information response."""
        if not isinstance(metadata, dict):
            raise OAuthError("invalid_client_metadata", "registration body must be a JSON object")
        uris = metadata.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
            raise OAuthError(
                "invalid_redirect_uri",
                f"redirect_uris must list 1..{MAX_REDIRECT_URIS} URIs",
            )
        redirect_uris = [validate_redirect_uri(u, allowed_redirect_hosts) for u in uris]

        method = metadata.get("token_endpoint_auth_method") or "none"
        if method not in AUTH_METHODS:
            raise OAuthError(
                "invalid_client_metadata",
                f"token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}",
            )
        grant_types = metadata.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "unsupported grant_types")
        response_types = metadata.get("response_types") or ["code"]
        if not isinstance(response_types, list) or set(response_types) != {"code"}:
            raise OAuthError("invalid_client_metadata", "response_types must be ['code']")

        raw_name = metadata.get("client_name")
        name = raw_name if isinstance(raw_name, str) else ""
        name = "".join(ch for ch in name if ch.isprintable()).strip()[:MAX_CLIENT_NAME_LENGTH]
        name = name or "Unnamed app"

        now = self.now()
        client_id = "lfo_client_" + secrets.token_urlsafe(16)
        client_secret = _new_secret(_SECRET_PREFIX) if method != "none" else None
        with self._transaction() as conn:
            self._prune(conn, now)
            (unapproved,) = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE approved_at IS NULL"
            ).fetchone()
            if unapproved >= MAX_PENDING_CLIENTS:
                evicted = conn.execute(
                    "DELETE FROM clients WHERE client_id IN (SELECT client_id FROM clients "
                    f"WHERE approved_at IS NULL AND {_UNPROTECTED_CLIENT_SQL} "
                    "ORDER BY created_at LIMIT :n)",
                    {"now": now, "n": unapproved - MAX_PENDING_CLIENTS + 1},
                ).rowcount
                if unapproved - evicted >= MAX_PENDING_CLIENTS:
                    raise OAuthError(
                        "temporarily_unavailable",
                        "too many registrations awaiting approval; try again later",
                        status=429,
                    )
            conn.execute(
                "INSERT INTO clients (client_id, client_name, redirect_uris, "
                "token_endpoint_auth_method, client_secret_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    client_id,
                    name,
                    json.dumps(redirect_uris),
                    method,
                    hash_secret(client_secret) if client_secret else None,
                    now,
                ),
            )
        logger.info("mcp oauth: registered client %s", client_id)
        response = {
            "client_id": client_id,
            "client_id_issued_at": int(now),
            "client_name": name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if client_secret:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = 0
        return response

    @contextmanager
    def _transaction(self):
        """One `BEGIN IMMEDIATE` write transaction: committed on success, rolled back on error."""
        with connect_closing(self.db_path, timeout=10, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _prune(conn, now: float) -> int:
        conn.execute("DELETE FROM pending_consents WHERE expires_at <= ?", (now,))
        return conn.execute(
            "DELETE FROM clients WHERE approved_at IS NULL AND created_at < :cutoff "
            f"AND {_UNPROTECTED_CLIENT_SQL}",
            {"cutoff": now - UNAPPROVED_CLIENT_TTL_SECONDS, "now": now},
        ).rowcount

    def prune(self) -> int:
        """Delete expired unapproved registrations and expired pending consents. Returns clients deleted."""
        with self._transaction() as conn:
            return self._prune(conn, self.now())

    def get_client(self, client_id: str) -> dict | None:
        if not isinstance(client_id, str) or not client_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method, "
                "client_secret_hash, created_at, approved_at, revoked_at "
                "FROM clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row[0],
            "client_name": row[1],
            "redirect_uris": json.loads(row[2]),
            "token_endpoint_auth_method": row[3],
            "client_secret_hash": row[4],
            "created_at": row[5],
            "approved_at": row[6],
            "revoked_at": row[7],
        }

    def active_client(self, client_id: str) -> dict | None:
        client = self.get_client(client_id)
        if client is None or client["revoked_at"] is not None:
            return None
        return client

    def authenticate_client(self, client_id: str, client_secret: str | None) -> dict:
        """Token/revocation endpoint client authentication."""
        client = self.active_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client", status=401)
        if client["token_endpoint_auth_method"] == "none":
            return client
        if not client_secret or not hmac.compare_digest(
            hash_secret(client_secret), client["client_secret_hash"] or ""
        ):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
        return client

    def list_clients(self) -> list[dict]:
        now = self.now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.client_id, c.client_name, c.redirect_uris, c.created_at, "
                "c.approved_at, c.revoked_at, "
                "(SELECT COUNT(*) FROM chains h WHERE h.client_id = c.client_id "
                " AND h.revoked_at IS NULL AND EXISTS (SELECT 1 FROM tokens t "
                "  WHERE t.chain_id = h.chain_id AND t.kind = 'refresh' "
                "  AND t.used_at IS NULL AND t.revoked_at IS NULL AND t.expires_at > ?)), "
                "(SELECT MAX(h.last_used_at) FROM chains h WHERE h.client_id = c.client_id) "
                "FROM clients c ORDER BY c.created_at",
                (now,),
            ).fetchall()
        return [
            {
                "client_id": r[0],
                "client_name": r[1],
                "redirect_hosts": sorted({urlsplit(u).hostname or "" for u in json.loads(r[2])}),
                "created_at": r[3],
                "approved_at": r[4],
                "revoked_at": r[5],
                "active_grants": r[6],
                "last_used_at": r[7],
            }
            for r in rows
        ]

    def revoke_client(self, client_id: str) -> bool:
        """Revoke a client and every grant it holds. Returns False for an unknown client.

        A client that was never approved holds no grants and is deleted outright.
        """
        now = self.now()
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_consents WHERE client_id = ?", (client_id,))
            if conn.execute(
                "DELETE FROM clients WHERE client_id = ? AND approved_at IS NULL", (client_id,)
            ).rowcount:
                logger.info("mcp oauth: deleted unapproved client %s", client_id)
                return True
            cur = conn.execute(
                "UPDATE clients SET revoked_at = COALESCE(revoked_at, ?) WHERE client_id = ?",
                (now, client_id),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE chains SET revoked_at = COALESCE(revoked_at, ?), "
                "revoke_reason = COALESCE(revoke_reason, 'client_revoked') WHERE client_id = ?",
                (now, client_id),
            )
            conn.execute(
                "UPDATE tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE client_id = ?",
                (now, client_id),
            )
            conn.execute("DELETE FROM pending_consents WHERE client_id = ?", (client_id,))
            conn.execute(
                "UPDATE auth_codes SET used_at = COALESCE(used_at, ?) WHERE client_id = ?",
                (now, client_id),
            )
        logger.info("mcp oauth: revoked client %s", client_id)
        return True

    # ── Consent ────────────────────────────────────────────────────────

    def create_pending_consent(
        self,
        *,
        operator_login: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str | None,
        resource: str,
    ) -> tuple[str, str] | None:
        """Record an authorization request awaiting the operator.

        Returns (request_id, csrf_token), or None when the client is missing
        or revoked — the check and the insert share one transaction, so a
        concurrent prune cannot delete the client in between.
        """
        now = self.now()
        request_id = secrets.token_urlsafe(16)
        csrf = secrets.token_urlsafe(32)
        with self._transaction() as conn:
            conn.execute("DELETE FROM pending_consents WHERE expires_at <= ?", (now,))
            if conn.execute(
                "SELECT 1 FROM clients WHERE client_id = ? AND revoked_at IS NULL", (client_id,)
            ).fetchone() is None:
                return None
            conn.execute(
                "INSERT INTO pending_consents (request_id, csrf_hash, operator_login_hash, "
                "client_id, redirect_uri, code_challenge, state, resource, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    hash_secret(csrf),
                    hash_secret(operator_login.strip().lower()),
                    client_id,
                    redirect_uri,
                    code_challenge,
                    state,
                    resource,
                    now + PENDING_CONSENT_TTL_SECONDS,
                ),
            )
        return request_id, csrf

    # Test seam: called inside the decision transaction after the consent row
    # is consumed and before the client is checked.
    _after_consent_consumed = staticmethod(lambda: None)

    def decide_consent(
        self,
        *,
        request_id: str,
        csrf: str,
        operator_login: str,
        approve: bool,
        tier: str = TIER_READ_SAFE_WRITES,
    ) -> tuple[dict, str | None] | None:
        """Consume a pending consent and, on approval, issue its code — atomically.

        Consuming the consent, checking the client and issuing the code happen
        in one `BEGIN IMMEDIATE` transaction, so a concurrent prune can never
        delete the client between them. Returns (pending, code) — `code` is
        None for a denial — or None when the request is unknown, expired,
        forged, for another operator, or its client is gone.
        """
        if not request_id or not csrf:
            return None
        now = self.now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT csrf_hash, operator_login_hash, client_id, redirect_uri, "
                "code_challenge, state, resource, expires_at "
                "FROM pending_consents WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            if not hmac.compare_digest(hash_secret(csrf), row[0]):
                return None
            if not hmac.compare_digest(hash_secret(operator_login.strip().lower()), row[1]):
                return None
            conn.execute("DELETE FROM pending_consents WHERE request_id = ?", (request_id,))
            if row[7] <= now:
                return None
            self._after_consent_consumed()
            if conn.execute(
                "SELECT 1 FROM clients WHERE client_id = ? AND revoked_at IS NULL", (row[2],)
            ).fetchone() is None:
                return None
            pending = {
                "client_id": row[2],
                "redirect_uri": row[3],
                "code_challenge": row[4],
                "state": row[5],
                "resource": row[6],
            }
            if not approve:
                return pending, None
            code = _new_secret(_CODE_PREFIX)
            conn.execute(
                "INSERT INTO auth_codes (code_hash, client_id, redirect_uri, code_challenge, "
                "resource, tier, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hash_secret(code),
                    pending["client_id"],
                    pending["redirect_uri"],
                    pending["code_challenge"],
                    pending["resource"],
                    tier,
                    now + AUTH_CODE_TTL_SECONDS,
                ),
            )
            conn.execute(
                "UPDATE clients SET approved_at = COALESCE(approved_at, ?) WHERE client_id = ?",
                (now, pending["client_id"]),
            )
        logger.info("mcp oauth: operator approved client %s", pending["client_id"])
        return pending, code

    # ── Tokens ─────────────────────────────────────────────────────────

    def _mint(self, conn, client_id: str, chain_id: str) -> IssuedTokens:
        now = self.now()
        access = _new_secret(_ACCESS_PREFIX)
        refresh = _new_secret(_REFRESH_PREFIX)
        conn.executemany(
            "INSERT INTO tokens (token_hash, kind, client_id, chain_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (hash_secret(access), "access", client_id, chain_id, now + ACCESS_TOKEN_TTL_SECONDS),
                (hash_secret(refresh), "refresh", client_id, chain_id, now + REFRESH_TOKEN_TTL_SECONDS),
            ],
        )
        conn.execute("UPDATE chains SET last_used_at = ? WHERE chain_id = ?", (now, chain_id))
        return IssuedTokens(access, refresh, ACCESS_TOKEN_TTL_SECONDS)

    def _revoke_chain(self, conn, chain_id: str, reason: str) -> None:
        now = self.now()
        conn.execute(
            "UPDATE chains SET revoked_at = COALESCE(revoked_at, ?), "
            "revoke_reason = COALESCE(revoke_reason, ?) WHERE chain_id = ?",
            (now, reason, chain_id),
        )
        conn.execute(
            "UPDATE tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE chain_id = ?",
            (now, chain_id),
        )

    def exchange_code(
        self,
        *,
        client: dict,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        resource: str | None,
        resource_ok: Callable[[str], bool],
    ) -> IssuedTokens:
        """Redeem an authorization code (single use, PKCE S256)."""
        if not code:
            raise OAuthError("invalid_request", "code is required")
        code_hash = hash_secret(code)
        now = self.now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, redirect_uri, code_challenge, resource, tier, expires_at, "
                "used_at, chain_id FROM auth_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if row is None:
                raise OAuthError("invalid_grant", "unknown authorization code")
            claimed = conn.execute(
                "UPDATE auth_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (now, code_hash),
            ).rowcount
            if claimed != 1 and row[7]:
                # RFC 6749 §4.1.2: a replayed code revokes what it already issued.
                # Committed by leaving the block normally; the error is raised after.
                self._revoke_chain(conn, row[7], "code_reuse")
        if claimed != 1:
            logger.warning("mcp oauth: authorization code replay for client %s", row[0])
            raise OAuthError("invalid_grant", "authorization code already used")
        (code_client, code_redirect, challenge, code_resource, tier, expires_at, _, _) = row
        # The code is consumed from here on, whether or not the rest verifies.
        if code_client != client["client_id"]:
            raise OAuthError("invalid_grant", "code was issued to another client")
        if expires_at <= now:
            raise OAuthError("invalid_grant", "authorization code expired")
        if redirect_uri != code_redirect:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if (
            not isinstance(code_verifier, str)
            or not is_pkce_value(code_verifier)
            or not hmac.compare_digest(pkce_s256(code_verifier), challenge)
        ):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        if resource is not None and not resource_ok(resource):
            raise OAuthError("invalid_target", "resource does not name this server")

        chain_id = secrets.token_urlsafe(16)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chains (chain_id, client_id, tier, resource, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chain_id, code_client, tier, code_resource, now),
            )
            conn.execute(
                "UPDATE auth_codes SET chain_id = ? WHERE code_hash = ?", (chain_id, code_hash)
            )
            issued = self._mint(conn, code_client, chain_id)
        logger.info("mcp oauth: issued tokens to client %s", code_client)
        return issued

    def refresh(
        self,
        *,
        client: dict,
        refresh_token: str,
        resource: str | None,
        resource_ok: Callable[[str], bool],
    ) -> IssuedTokens:
        """Rotate a refresh token. Presenting a consumed one revokes its whole chain."""
        if not refresh_token:
            raise OAuthError("invalid_request", "refresh_token is required")
        if resource is not None and not resource_ok(resource):
            raise OAuthError("invalid_target", "resource does not name this server")
        token_hash = hash_secret(refresh_token)
        now = self.now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.client_id, t.chain_id, t.expires_at, t.used_at, t.revoked_at, "
                "h.revoked_at FROM tokens t JOIN chains h ON h.chain_id = t.chain_id "
                "WHERE t.token_hash = ? AND t.kind = 'refresh'",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise OAuthError("invalid_grant", "unknown refresh token")
            token_client, chain_id, expires_at, used_at, revoked_at, chain_revoked = row
            if token_client != client["client_id"]:
                raise OAuthError("invalid_grant", "refresh token was issued to another client")
            if revoked_at is not None or chain_revoked is not None:
                raise OAuthError("invalid_grant", "refresh token revoked")
            if expires_at <= now:
                raise OAuthError("invalid_grant", "refresh token expired")
            rotated = conn.execute(
                "UPDATE tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL "
                "AND revoked_at IS NULL",
                (now, token_hash),
            ).rowcount
            reused = used_at is not None or rotated != 1
            if reused:
                # Committed by leaving the block normally; the error is raised after.
                self._revoke_chain(conn, chain_id, "refresh_reuse")
            else:
                issued = self._mint(conn, token_client, chain_id)
        if reused:
            logger.warning(
                "mcp oauth: refresh token reuse for client %s; chain revoked", token_client
            )
            raise OAuthError("invalid_grant", "refresh token already used")
        return issued

    def revoke_token(self, *, client: dict, token: str) -> None:
        """RFC 7009 revocation: revoking either token kind revokes its chain."""
        if not token:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, chain_id FROM tokens WHERE token_hash = ?",
                (hash_secret(token),),
            ).fetchone()
            if row is None or row[0] != client["client_id"]:
                return
            self._revoke_chain(conn, row[1], "client_revocation")

    def validate_access_token(self, token: str, *, resource: str) -> AccessGrant | None:
        """Resolve a presented access token, or None if unknown, expired or revoked."""
        if not token or not token.startswith(_ACCESS_PREFIX):
            return None
        now = self.now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.client_id, t.chain_id, t.expires_at, t.revoked_at, h.revoked_at, "
                "h.tier, h.resource, c.revoked_at "
                "FROM tokens t JOIN chains h ON h.chain_id = t.chain_id "
                "JOIN clients c ON c.client_id = t.client_id "
                "WHERE t.token_hash = ? AND t.kind = 'access'",
                (hash_secret(token),),
            ).fetchone()
            if row is None:
                return None
            client_id, chain_id, expires_at, revoked, chain_revoked, tier, token_resource, client_revoked = row
            if revoked is not None or chain_revoked is not None or client_revoked is not None:
                return None
            if expires_at <= now or token_resource != resource:
                return None
            conn.execute("UPDATE chains SET last_used_at = ? WHERE chain_id = ?", (now, chain_id))
        return AccessGrant(client_id=client_id, chain_id=chain_id, tier=tier)
