"""Single-operator OAuth 2.1 on the MCP HTTP transport, and its tool tier.

Every identity, host and document here is synthetic. Tool calls never reach a
LifeOS API: `_call_api` is replaced with a recorder, and the tool catalog is
the offline fallback so the set of tools is deterministic.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

import mcp_server
from api.services import mcp_oauth
from api.services.mcp_oauth import OAuthConfig, OAuthStore, pkce_s256
from api.services.mcp_tool_tier import OAUTH_ALLOWED_TOOLS, OAUTH_TOOL_TIER

pytestmark = pytest.mark.unit

BEARER = "test-bearer-secret"
ISSUER = "https://lifeos.example.ts.net:8443"
RESOURCE = f"{ISSUER}/mcp"
OPERATOR = "operator@example.com"
OTHER_OPERATOR = "second.operator@example.com"
CLAUDE_REDIRECT = "https://claude.example.com/api/mcp/auth_callback"
OPERATOR_HEADERS = {"Tailscale-User-Login": OPERATOR}
LOOPBACK = ("127.0.0.1", 51000)
ALLOWED_HOSTS = frozenset({"claude.example.com", "app.example.com", "spam.example.com"})


class Clock:
    def __init__(self) -> None:
        self.now = 1_900_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> OAuthStore:
    return OAuthStore(tmp_path / "mcp_oauth.db", clock=clock)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "SyntheticVault"
    (root / "Work").mkdir(parents=True)
    (root / "Work" / "Planning Notes.md").write_text("# Planning\nSynthetic roadmap body.\n")
    (root / "Private.md").write_text("never surfaced by search\n")
    (tmp_path / "outside.md").write_text("outside the vault\n")
    tasks = root / "LifeOS" / "Tasks"
    tasks.mkdir(parents=True)
    for context in ("Inbox", "Personal", "Dashboard"):
        (tasks / f"{context}.md").write_text(f"# {context} Tasks\n")
    (tasks / "Linked.md").symlink_to(tmp_path / "outside.md")
    return root


@pytest.fixture
def calls() -> list[tuple[str, dict]]:
    return []


_ORIGINAL_CALL_API = mcp_server.LifeOSMCPServer._call_api


@pytest.fixture
def server(monkeypatch, calls, vault) -> mcp_server.LifeOSMCPServer:
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_load_openapi_spec",
        lambda self: self._build_tools_fallback(),
    )

    def fake_call(self, tool_name: str, arguments: dict[str, Any], *a, **kw) -> dict[str, Any]:
        calls.append((tool_name, dict(arguments)))
        if tool_name == "lifeos_search":
            return {"results": [
                {"file_path": str(vault / "Work" / "Planning Notes.md"), "file_name": "Planning Notes",
                 "content": "Synthetic roadmap body."},
                {"file_path": str(vault / "Work" / "Planning Notes.md"), "file_name": "Planning Notes",
                 "content": "second chunk"},
                {"file_path": str(vault.parent / "outside.md"), "file_name": "outside", "content": "x"},
            ]}
        return {"echo": tool_name}

    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", fake_call)
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_format_response",
        lambda self, name, data, arguments=None: json.dumps(data),
    )
    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_vault_root", lambda self: vault.resolve())
    return mcp_server.LifeOSMCPServer()


@pytest.fixture
def config(store) -> OAuthConfig:
    return OAuthConfig(issuer=ISSUER, operator_logins=frozenset({OPERATOR, OTHER_OPERATOR}), store=store,
                       allowed_redirect_hosts=ALLOWED_HOSTS)


@pytest.fixture
def app(server, config):
    return mcp_server.build_http_app(server, bearer_token=BEARER, oauth=config)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, client=LOOPBACK, follow_redirects=False)


# ── helpers ──────────────────────────────────────────────────────────────


def _register(client: TestClient, **overrides) -> dict:
    body = {"client_name": "Synthetic App", "redirect_uris": [CLAUDE_REDIRECT],
            "token_endpoint_auth_method": "none", **overrides}
    resp = client.post("/oauth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, pkce_s256(verifier)


def _authorize_params(client_id: str, challenge: str, **overrides) -> dict:
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "synthetic-state", "resource": RESOURCE,
    }
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def _consent_form(resp) -> dict:
    assert resp.status_code == 200, resp.text
    request_id = re.search(r'name="request_id" value="([^"]+)"', resp.text).group(1)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', resp.text).group(1)
    return {"request_id": request_id, "csrf_token": csrf}


def _post_form(client: TestClient, path: str, data: dict, headers: dict | None = None):
    return client.post(
        path, content=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
    )


def _get_code(client: TestClient, client_id: str, challenge: str) -> str:
    page = client.get("/oauth/authorize", params=_authorize_params(client_id, challenge), headers=OPERATOR_HEADERS)
    form = _consent_form(page)
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    assert resp.status_code == 303
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["state"] == ["synthetic-state"]
    assert query["iss"] == [ISSUER]
    return query["code"][0]


def _exchange(client: TestClient, client_id: str, code: str, verifier: str, **overrides):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": verifier, "client_id": client_id, "resource": RESOURCE, **overrides}
    return _post_form(client, "/oauth/token", {k: v for k, v in data.items() if v is not None})


def _connect(client: TestClient) -> tuple[str, dict, str]:
    """Register, consent and exchange. Returns (client_id, token response, verifier)."""
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    resp = _exchange(client, client_id, code, verifier)
    assert resp.status_code == 200, resp.text
    return client_id, resp.json(), verifier


def _rpc(client: TestClient, token: str, method: str, params: dict | None = None, req_id: int = 1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


def _refresh(client: TestClient, client_id: str, refresh_token: str):
    return _post_form(client, "/oauth/token", {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
    })


def _assert_401_challenge(resp) -> None:
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge


# ── metadata and registration ────────────────────────────────────────────


def test_protected_resource_metadata_names_the_issuer(client):
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        body = client.get(path).json()
        assert body["resource"] == RESOURCE
        assert body["authorization_servers"] == [ISSUER]


def test_authorization_server_metadata_advertises_s256_only(client):
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["issuer"] == ISSUER
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert body["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert body["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert body["revocation_endpoint"] == f"{ISSUER}/oauth/revoke"
    assert body["authorization_response_iss_parameter_supported"] is True
    assert "client_id_metadata_document_supported" not in body


def test_authorize_url_override_is_advertised(server, store):
    cfg = OAuthConfig(issuer=ISSUER, operator_logins=frozenset({OPERATOR}), store=store,
                      authorize_url="https://lifeos.example.ts.net/oauth/authorize")
    c = TestClient(mcp_server.build_http_app(server, bearer_token=BEARER, oauth=cfg), client=LOOPBACK)
    body = c.get("/.well-known/oauth-authorization-server").json()
    assert body["authorization_endpoint"] == "https://lifeos.example.ts.net/oauth/authorize"


@pytest.mark.parametrize("uri", [
    "https://app.example.com/callback",
    "http://127.0.0.1/callback",
    "http://localhost:3118/callback",
    "http://[::1]:9000/cb",
])
def test_registration_accepts_https_and_loopback_redirects(client, uri):
    info = _register(client, redirect_uris=[uri])
    assert info["client_id"].startswith("lfo_client_")
    assert info["redirect_uris"] == [uri]
    assert "client_secret" not in info


@pytest.mark.parametrize("uri", [
    "http://app.example.com/callback",
    "custom-scheme://callback",
    "https://app.example.com/callback#frag",
    "javascript:alert(1)",
    "",
])
def test_registration_rejects_other_redirects(client, uri):
    resp = client.post("/oauth/register", json={"redirect_uris": [uri]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_registration_rejects_unsupported_metadata(client):
    for body in (
        {"redirect_uris": [CLAUDE_REDIRECT], "grant_types": ["client_credentials"]},
        {"redirect_uris": [CLAUDE_REDIRECT], "token_endpoint_auth_method": "private_key_jwt"},
        {"redirect_uris": [CLAUDE_REDIRECT], "response_types": ["token"]},
        {"redirect_uris": []},
        ["not", "an", "object"],
    ):
        assert client.post("/oauth/register", json=body).status_code == 400


def test_registration_spam_does_not_lock_out_a_real_app(client, store):
    spam = [
        client.post("/oauth/register", json={"redirect_uris": ["https://spam.example.com/cb"]})
        for _ in range(60)
    ]
    assert all(r.status_code == 201 for r in spam)
    real = _register(client)
    _, challenge = _pkce()
    page = client.get("/oauth/authorize", params=_authorize_params(real["client_id"], challenge),
                      headers=OPERATOR_HEADERS)
    assert page.status_code == 200
    assert "spam.example.com" not in page.text


def _spam(client: TestClient, n: int) -> None:
    for _ in range(n):
        resp = client.post("/oauth/register", json={"redirect_uris": ["https://spam.example.com/cb"]})
        assert resp.status_code == 201


def test_registration_at_the_ceiling_evicts_the_oldest_unprotected(client, store, monkeypatch, calls):
    monkeypatch.setattr(mcp_oauth, "MAX_PENDING_CLIENTS", 5)
    _spam(client, 5)
    real = _register(client)["client_id"]
    verifier, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(real, challenge),
                                    headers=OPERATOR_HEADERS))
    _spam(client, 12)  # keeps evicting; the consent-protected registration survives
    assert sum(1 for c in store.list_clients() if c["approved_at"] is None) == 5
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    assert resp.status_code == 303
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    _spam(client, 6)
    tokens = _exchange(client, real, code, verifier).json()
    body = _rpc(client, tokens["access_token"], "tools/call", {"name": "lifeos_health", "arguments": {}}).json()
    assert "result" in body
    assert calls == [("lifeos_health", {})]


def test_registration_is_refused_only_when_every_slot_is_consent_protected(client, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "MAX_PENDING_CLIENTS", 2)
    for _ in range(2):
        client_id = _register(client)["client_id"]
        _, challenge = _pkce()
        assert client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                          headers=OPERATOR_HEADERS).status_code == 200
    resp = client.post("/oauth/register", json={"redirect_uris": [CLAUDE_REDIRECT]})
    assert resp.status_code == 429


@pytest.mark.parametrize("uri", [
    "https://attacker.example.net/cb",
    "https://claude.example.com.attacker.example.net/cb",
    "https://claude.example.com@attacker.example.net/cb",
    "http://claude.example.com/cb",
])
def test_registration_rejects_a_redirect_host_outside_the_allowlist(client, uri):
    resp = client.post("/oauth/register", json={"redirect_uris": [uri]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_default_redirect_allowlist_is_the_claude_and_chatgpt_hosts(server, store):
    cfg = OAuthConfig(issuer=ISSUER, operator_logins=frozenset({OPERATOR}), store=store)
    c = TestClient(mcp_server.build_http_app(server, bearer_token=BEARER, oauth=cfg), client=LOOPBACK)
    for uri in ("https://claude.ai/api/mcp/auth_callback", "https://chatgpt.com/connector_platform_oauth_redirect",
                "https://claude.com/cb", "https://127.0.0.1:8443/cb", "http://localhost:3118/callback"):
        assert c.post("/oauth/register", json={"redirect_uris": [uri]}).status_code == 201, uri
    resp = c.post("/oauth/register", json={"redirect_uris": [CLAUDE_REDIRECT]})
    assert resp.status_code == 400


def test_allowed_redirect_hosts_setting_reaches_the_config(monkeypatch, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "oauth_operator_logins", OPERATOR)
    monkeypatch.setattr(settings, "oauth_issuer_url", ISSUER)
    monkeypatch.setattr(settings, "oauth_allowed_redirect_hosts", " Claude.AI , chatgpt.com ,")
    monkeypatch.setattr(settings, "chroma_path", str(tmp_path / "chromadb"))
    cfg = mcp_server._oauth_config_from_settings()
    assert cfg.allowed_redirect_hosts == frozenset({"claude.ai", "chatgpt.com"})


def test_approval_survives_a_prune_at_the_ttl_boundary(client, store, clock):
    """A prune that lands mid-approval cannot delete the client being approved."""
    import threading

    client_id = _register(client)["client_id"]
    clock.now += 14 * 60
    verifier, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    clock.now += 2 * 60  # 16 minutes after registration
    pruner = threading.Thread(target=store.prune)

    def interleave_prune():
        pruner.start()
        pruner.join(timeout=1.0)  # a prune that can run now, runs to completion here

    store._after_consent_consumed = interleave_prune
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    pruner.join(timeout=15)
    assert not pruner.is_alive()
    assert resp.status_code == 303, resp.text
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    assert _exchange(client, client_id, code, verifier).status_code == 200


def test_prune_spares_a_client_holding_an_unexpired_code(store, clock):
    info = store.register_client({"redirect_uris": [CLAUDE_REDIRECT]}, allowed_redirect_hosts=ALLOWED_HOSTS)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO auth_codes (code_hash, client_id, redirect_uri, code_challenge, resource, tier, "
            "expires_at) VALUES ('h', ?, ?, 'c', ?, 't', ?)",
            (info["client_id"], CLAUDE_REDIRECT, RESOURCE, clock.now + 20 * 60),
        )
    conn.close()
    clock.now += 16 * 60
    store.prune()
    assert store.get_client(info["client_id"]) is not None
    clock.now += 5 * 60
    store.prune()
    assert store.get_client(info["client_id"]) is None


def test_unapproved_registrations_expire_after_fifteen_minutes(client, clock, store):
    stale = _register(client)["client_id"]
    clock.now += 15 * 60 + 1
    fresh = _register(client)["client_id"]
    assert store.get_client(stale) is None
    assert store.get_client(fresh) is not None
    clock.now += 15 * 60 + 1
    _, challenge = _pkce()
    resp = client.get("/oauth/authorize", params=_authorize_params(fresh, challenge), headers=OPERATOR_HEADERS)
    assert resp.status_code == 400  # pruned on the authorize request itself
    assert store.get_client(fresh) is None


def test_a_pending_consent_keeps_its_registration(client, clock, store):
    client_id = _register(client)["client_id"]
    clock.now += 14 * 60
    verifier, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    clock.now += 2 * 60
    store.prune()
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    assert resp.status_code == 303


def test_revoking_an_unapproved_client_deletes_it(client, store):
    client_id = _register(client)["client_id"]
    assert store.revoke_client(client_id) is True
    assert store.get_client(client_id) is None
    approved_id, _, _ = _connect(client)
    assert store.revoke_client(approved_id) is True
    assert store.get_client(approved_id)["revoked_at"] is not None


# ── happy path and tier ──────────────────────────────────────────────────


def test_full_flow_lists_and_calls_only_the_tier(client, calls):
    _, tokens, _ = _connect(client)
    assert tokens["token_type"] == "Bearer"
    assert 0 < tokens["expires_in"] <= 3600
    access = tokens["access_token"]

    init = _rpc(client, access, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    assert init.status_code == 200

    listed = _rpc(client, access, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in listed}
    assert names <= OAUTH_ALLOWED_TOOLS
    assert {"search", "fetch", "lifeos_search", "lifeos_task_create"} <= names
    assert not names & {"lifeos_gmail_send", "lifeos_task_delete", "lifeos_agent_spawn", "lifeos_home_eero_pause"}
    by_name = {t["name"]: t for t in listed}
    assert by_name["lifeos_search"]["annotations"]["readOnlyHint"] is True
    assert by_name["lifeos_task_create"]["annotations"]["readOnlyHint"] is False
    assert by_name["lifeos_task_create"]["annotations"]["destructiveHint"] is False

    resp = _rpc(client, access, "tools/call", {"name": "lifeos_search", "arguments": {"query": "roadmap"}})
    result = resp.json()["result"]
    assert "isError" not in result
    assert calls == [("lifeos_search", {"query": "roadmap"})]


def test_bearer_listing_is_unchanged_by_oauth(client, server):
    listed = _rpc(client, BEARER, "tools/list").json()["result"]["tools"]
    assert listed == server.tools
    assert not {"search", "fetch"} & {t["name"] for t in listed}


def test_forbidden_tool_call_is_unknown_and_never_reaches_the_api(client, calls):
    _, tokens, _ = _connect(client)
    for name in ("lifeos_gmail_send", "lifeos_task_delete", "lifeos_agent_spawn", "lifeos_does_not_exist"):
        body = _rpc(client, tokens["access_token"], "tools/call",
                    {"name": name, "arguments": {"draft_id": "d1"}}).json()
        assert body["error"]["code"] == -32602
        assert body["error"]["message"] == f"Unknown tool: {name}"
    assert calls == []


def test_vault_read_is_in_the_tier_as_a_read_only_tool(client, calls):
    _, tokens, _ = _connect(client)
    listed = _rpc(client, tokens["access_token"], "tools/list").json()["result"]["tools"]
    by_name = {t["name"]: t for t in listed}
    assert by_name["lifeos_vault_read"]["annotations"]["readOnlyHint"] is True
    assert OAUTH_TOOL_TIER["lifeos_vault_read"].read_only is True
    _rpc(client, tokens["access_token"], "tools/call",
         {"name": "lifeos_vault_read", "arguments": {"path": "Work/Planning Notes.md"}})
    assert calls == [("lifeos_vault_read", {"path": "Work/Planning Notes.md"})]


def test_bearer_can_still_call_a_tool_outside_the_tier(client, calls):
    body = _rpc(client, BEARER, "tools/call", {"name": "lifeos_gmail_send", "arguments": {"draft_id": "d1"}}).json()
    assert "result" in body
    assert calls == [("lifeos_gmail_send", {"draft_id": "d1"})]


def test_a_newly_added_tool_is_not_exposed_to_oauth(client, server, calls):
    server.tools.append({"name": "lifeos_brand_new", "description": "x", "inputSchema": {"type": "object"}})
    _, tokens, _ = _connect(client)
    listed = {t["name"] for t in _rpc(client, tokens["access_token"], "tools/list").json()["result"]["tools"]}
    assert "lifeos_brand_new" not in listed
    body = _rpc(client, tokens["access_token"], "tools/call", {"name": "lifeos_brand_new", "arguments": {}}).json()
    assert body["error"]["code"] == -32602
    assert calls == []
    assert "lifeos_brand_new" in {t["name"] for t in _rpc(client, BEARER, "tools/list").json()["result"]["tools"]}


def test_batch_requests_are_tier_enforced(client, calls):
    _, tokens, _ = _connect(client)
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lifeos_gmail_send", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lifeos_health", "arguments": {}}},
    ]
    resp = client.post("/mcp", json=batch, headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert resp[0]["error"]["code"] == -32602
    assert "result" in resp[1]
    assert calls == [("lifeos_health", {})]


@pytest.mark.parametrize("arguments", [
    {"description": "Synthetic task", "tags": ["claude"]},
    {"description": "Synthetic task", "tags": ["#Cloud-Sonnet"]},
    {"description": "Synthetic task", "tags": ["agent-running"]},
    {"description": "Synthetic task", "tags": ["human"]},
    {"description": "Synthetic task #codex please"},
    {"description": "Synthetic task", "notes": "run with #local"},
    {"description": "Synthetic task", "fields": {"parent_id": "t-1"}},
    {"description": "Synthetic task", "dry_run": True},
    {"description": "Synthetic task", "tags": "claude"},
])
def test_task_create_cannot_hand_work_to_an_agent(client, calls, arguments):
    _, tokens, _ = _connect(client)
    result = _rpc(client, tokens["access_token"], "tools/call",
                  {"name": "lifeos_task_create", "arguments": arguments}).json()["result"]
    assert result["isError"] is True
    assert calls == []


def test_plain_task_create_is_allowed(client, calls):
    _, tokens, _ = _connect(client)
    args = {"description": "Buy synthetic milk", "tags": ["errand"], "context": "Personal"}
    result = _rpc(client, tokens["access_token"], "tools/call",
                  {"name": "lifeos_task_create", "arguments": args}).json()["result"]
    assert "isError" not in result
    assert calls == [("lifeos_task_create", args)]


@pytest.mark.parametrize("arguments", [
    {"description": "Synthetic task", "tags": ["ordinary #codex"]},
    {"description": "Synthetic task", "tags": ["a,#claude"]},
    {"description": "Synthetic task", "tags": ["\uff03claude"]},
    {"description": "Synthetic task", "tags": ["ok\n#claude"]},
    {"description": "Synthetic task", "tags": ["ok #errand"]},
    {"description": "Synthetic task", "tags": ["errand\n"]},
    {"description": "Synthetic task", "tags": ["errand\r"]},
    {"description": "Synthetic task", "tags": ["\uff43odex"]},
    {"description": "Synthetic task \uff03claude"},
    {"description": "Synthetic task #\uff43laude"},
    {"description": "Synthetic task\n- [ ] TODO #claude"},
    {"description": "Synthetic task", "notes": "line one\n- [ ] TODO #hermes"},
    {"description": "Synthetic task", "priority": "#cloud"},
    {"description": "Synthetic task", "operation_key": "k #agent"},
], ids=["embedded-tag", "comma-tag", "fullwidth-hash-tag", "newline-tag", "spaced-tag",
        "trailing-newline-tag", "trailing-cr-tag", "fullwidth-letter-tag",
        "fullwidth-hash-description", "fullwidth-letters-description", "newline-description",
        "notes-checkbox", "priority-tag", "operation-key-tag"])
def test_task_create_refuses_smuggled_tags(client, calls, arguments):
    _, tokens, _ = _connect(client)
    result = _rpc(client, tokens["access_token"], "tools/call",
                  {"name": "lifeos_task_create", "arguments": arguments}).json()["result"]
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("context", [
    "/tmp/synthetic-outside", "../outside", "Linked", "Dashboard", "Brand New", "Inbox/../../x",
])
def test_task_create_context_must_already_exist(client, calls, context):
    _, tokens, _ = _connect(client)
    result = _rpc(client, tokens["access_token"], "tools/call", {
        "name": "lifeos_task_create",
        "arguments": {"description": "Synthetic task", "context": context},
    }).json()["result"]
    assert result["isError"] is True
    assert calls == []


def test_task_create_default_context_requires_an_existing_inbox(client, calls, vault):
    _, tokens, _ = _connect(client)
    args = {"description": "Synthetic task"}
    ok = _rpc(client, tokens["access_token"], "tools/call",
              {"name": "lifeos_task_create", "arguments": args}).json()["result"]
    assert "isError" not in ok
    (vault / "LifeOS" / "Tasks" / "Inbox.md").unlink()
    refused = _rpc(client, tokens["access_token"], "tools/call",
                   {"name": "lifeos_task_create", "arguments": args}).json()["result"]
    assert refused["isError"] is True
    assert calls == [("lifeos_task_create", args)]


@pytest.fixture
def real_tasks(server, vault, tmp_path, monkeypatch):
    """The real task route and TaskManager behind the MCP server's API calls."""
    from fastapi import FastAPI

    from api.routes import tasks as task_routes
    from api.services.task_manager import TaskManager

    manager = TaskManager(vault_path=vault, index_path=tmp_path / "index.json",
                          live_session_checker=lambda *a: False, live_coordinator_checker=lambda *a: False)
    backend = FastAPI()
    backend.include_router(task_routes.router)
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(mcp_server.LifeOSMCPServer, "_call_api", _ORIGINAL_CALL_API)
    monkeypatch.setattr(mcp_server, "API_BASE", "http://testserver")
    server.client = TestClient(backend)
    return manager


def _create_task(client: TestClient, token: str, arguments: dict) -> dict:
    return _rpc(client, token, "tools/call",
                {"name": "lifeos_task_create", "arguments": arguments}).json()["result"]


FORBIDDEN = {"agent", "claude", "codex", "hermes", "local", "cloud", "cloud-haiku", "cloud-sonnet", "human"}


def test_oauth_task_create_end_to_end_never_yields_an_engine_tag(client, real_tasks, tmp_path):
    _, tokens, _ = _connect(client)
    for arguments in (
        {"description": "Synthetic execution request", "tags": ["ordinary #codex"]},
        {"description": "Synthetic task #codex"},
        {"description": "Synthetic task", "context": str(tmp_path / "outside")},
        {"description": "Synthetic task", "tags": ["errand\n"]},
    ):
        assert _create_task(client, tokens["access_token"], arguments)["isError"] is True, arguments
    ok = _create_task(client, tokens["access_token"],
                      {"description": "Synthetic errand", "tags": ["errand"], "context": "Personal"})
    assert "isError" not in ok, ok
    real_tasks.rebuild_index()
    assert [set(t.tags) for t in real_tasks._tasks.values()] == [{"errand"}]
    assert (tmp_path / "outside.md").read_text() == "outside the vault\n"


def test_cyrillic_lookalike_tag_never_becomes_an_engine_tag(client, real_tasks):
    _, tokens, _ = _connect(client)
    lookalike = "c\u043edex"  # Cyrillic small o
    result = _create_task(client, tokens["access_token"], {"description": f"Synthetic #{lookalike} note"})
    assert "isError" not in result, result
    real_tasks.rebuild_index()
    tags = {tag for t in real_tasks._tasks.values() for tag in t.tags}
    assert tags == {lookalike}
    assert not {t.lower() for t in tags} & FORBIDDEN


def test_fullwidth_engine_tag_is_refused_end_to_end(client, real_tasks):
    _, tokens, _ = _connect(client)
    for arguments in ({"description": "Synthetic \uff03codex"}, {"description": "Synthetic #\uff43odex"},
                      {"description": "Synthetic", "tags": ["\uff43odex"]}):
        assert _create_task(client, tokens["access_token"], arguments)["isError"] is True, arguments
    assert real_tasks._tasks == {}


def test_watcher_reindex_of_oauth_created_tasks_yields_no_forbidden_tag(client, real_tasks):
    _, tokens, _ = _connect(client)
    attempts = [
        {"description": "Synthetic errand", "tags": ["errand"], "context": "Personal"},
        {"description": "Synthetic plain task"},
        {"description": "Synthetic", "notes": "first\n- [ ] TODO #claude\n#hermes", "context": "Personal"},
        {"description": "Synthetic #c\u043edex", "context": "Personal"},
        {"description": "Synthetic", "tags": ["ok #codex"], "context": "Personal"},
    ]
    for arguments in attempts:
        _create_task(client, tokens["access_token"], arguments)
    tasks_dir = real_tasks.tasks_dir
    for name in ("Personal.md", "Inbox.md"):
        real_tasks.reindex_file(str(tasks_dir / name))
    tags = {tag.lower() for t in real_tasks._tasks.values() for tag in t.tags}
    assert {"errand"} <= tags
    assert not tags & FORBIDDEN
    assert not any(t.startswith("agent") for t in tags)


@pytest.mark.parametrize("arguments", [
    {"message_type": "prompt", "message_content": "summarize", "schedule_type": "once"},
    {"message_type": "endpoint", "endpoint_config": {"endpoint": "/api/gmail/send"}},
    {"message_type": "static", "endpoint_config": {"endpoint": "/api/x"}},
    {"message_content": "no type"},
])
def test_reminder_create_is_static_only(client, calls, arguments):
    _, tokens, _ = _connect(client)
    result = _rpc(client, tokens["access_token"], "tools/call",
                  {"name": "lifeos_reminder_create", "arguments": arguments}).json()["result"]
    assert result["isError"] is True
    assert calls == []


def test_static_reminder_create_is_allowed(client, calls):
    _, tokens, _ = _connect(client)
    args = {"name": "Water", "message_type": "static", "message_content": "Water the plants",
            "schedule_type": "once"}
    result = _rpc(client, tokens["access_token"], "tools/call",
                  {"name": "lifeos_reminder_create", "arguments": args}).json()["result"]
    assert "isError" not in result
    assert calls == [("lifeos_reminder_create", args)]


# ── ChatGPT search / fetch ───────────────────────────────────────────────


def test_search_and_fetch_return_chatgpt_shapes(client):
    _, tokens, _ = _connect(client)
    access = tokens["access_token"]
    result = _rpc(client, access, "tools/call", {"name": "search", "arguments": {"query": "roadmap"}}).json()["result"]
    payload = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == payload
    assert [r["id"] for r in payload["results"]] == ["Work/Planning Notes.md"]
    hit = payload["results"][0]
    assert hit["title"] == "Planning Notes"
    assert hit["url"].startswith("obsidian://open?vault=SyntheticVault&file=Work%2FPlanning")

    fetched = _rpc(client, access, "tools/call",
                   {"name": "fetch", "arguments": {"id": hit["id"]}}).json()["result"]["structuredContent"]
    assert fetched["id"] == hit["id"]
    assert fetched["title"] == "Planning Notes"
    assert "Synthetic roadmap body." in fetched["text"]
    assert fetched["url"] == hit["url"]


@pytest.mark.parametrize("doc_id", ["Private.md", "../outside.md", "/etc/hostname", ""])
def test_fetch_serves_only_documents_search_returned(client, doc_id):
    _, tokens, _ = _connect(client)
    access = tokens["access_token"]
    _rpc(client, access, "tools/call", {"name": "search", "arguments": {"query": "roadmap"}})
    result = _rpc(client, access, "tools/call", {"name": "fetch", "arguments": {"id": doc_id}}).json()["result"]
    assert result["isError"] is True


def test_fetch_is_bound_to_the_client_that_searched(client):
    _, first, _ = _connect(client)
    _, second, _ = _connect(client)
    found = _rpc(client, first["access_token"], "tools/call",
                 {"name": "search", "arguments": {"query": "roadmap"}}).json()["result"]["structuredContent"]
    doc_id = found["results"][0]["id"]
    other = _rpc(client, second["access_token"], "tools/call",
                 {"name": "fetch", "arguments": {"id": doc_id}}).json()["result"]
    assert other["isError"] is True
    own = _rpc(client, first["access_token"], "tools/call",
               {"name": "fetch", "arguments": {"id": doc_id}}).json()["result"]
    assert "isError" not in own


def test_search_and_fetch_are_not_on_the_bearer_path(client, calls):
    body = _rpc(client, BEARER, "tools/call", {"name": "search", "arguments": {"query": "x"}}).json()
    assert "structuredContent" not in body["result"]
    assert calls == [("search", {"query": "x"})]  # the unchanged curated dispatcher


# ── consent gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers,peer", [
    ({}, LOOPBACK),
    ({"Tailscale-User-Login": "intruder@example.com"}, LOOPBACK),
    ({"Tailscale-User-Login": OPERATOR, "Tailscale-Funnel-Request": "?1"}, LOOPBACK),
    (OPERATOR_HEADERS, ("100.101.102.103", 40000)),
], ids=["missing-identity", "non-operator", "funnel-marker", "non-loopback-peer"])
def test_consent_is_refused_outside_the_operator_tailnet_path(app, client, headers, peer):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    c = TestClient(app, client=peer, follow_redirects=False)
    resp = c.get("/oauth/authorize", params=_authorize_params(client_id, challenge), headers=headers)
    assert resp.status_code == 403
    assert "csrf_token" not in resp.text


@pytest.mark.parametrize("headers,peer", [
    ({}, LOOPBACK),
    ({"Tailscale-User-Login": "intruder@example.com"}, LOOPBACK),
    ({"Tailscale-User-Login": OPERATOR, "Tailscale-Funnel-Request": "?1"}, LOOPBACK),
    (OPERATOR_HEADERS, ("100.101.102.103", 40000)),
    ({**OPERATOR_HEADERS, "Sec-Fetch-Site": "cross-site"}, LOOPBACK),
    ({**OPERATOR_HEADERS, "Origin": "https://attacker.example.com"}, LOOPBACK),
], ids=["missing-identity", "non-operator", "funnel-marker", "non-loopback-peer", "cross-site", "foreign-origin"])
def test_approval_post_is_refused_outside_the_operator_tailnet_path(app, client, headers, peer):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    c = TestClient(app, client=peer, follow_redirects=False)
    resp = _post_form(c, "/oauth/authorize", {**form, "decision": "approve"}, headers)
    assert resp.status_code == 403
    assert "location" not in resp.headers


def test_approval_accepts_a_same_origin_post_through_the_proxy(client):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    headers = {**OPERATOR_HEADERS, "Origin": "https://lifeos.example.ts.net", "Host": "127.0.0.1:8765",
               "X-Forwarded-Host": "lifeos.example.ts.net", "Sec-Fetch-Site": "same-origin"}
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, headers)
    assert resp.status_code == 303


def test_approval_requires_the_matching_csrf_token_and_operator(client):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    wrong = _post_form(client, "/oauth/authorize", {**form, "csrf_token": "forged", "decision": "approve"},
                       OPERATOR_HEADERS)
    assert wrong.status_code == 400
    other = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"},
                       {"Tailscale-User-Login": OTHER_OPERATOR})
    assert other.status_code == 400
    ok = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    assert ok.status_code == 303
    replay = _post_form(client, "/oauth/authorize", {**form, "decision": "approve"}, OPERATOR_HEADERS)
    assert replay.status_code == 400


def test_consent_page_names_app_redirect_host_and_tier_and_escapes(client):
    client_id = _register(client, client_name="<script>alert(1)</script> App")["client_id"]
    _, challenge = _pkce()
    resp = client.get("/oauth/authorize", params=_authorize_params(client_id, challenge), headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert "&lt;script&gt;" in resp.text and "<script>" not in resp.text
    assert "claude.example.com" in resp.text
    assert "Read + safe writes" in resp.text
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_deny_redirects_with_access_denied(client):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    form = _consent_form(client.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                                    headers=OPERATOR_HEADERS))
    resp = _post_form(client, "/oauth/authorize", {**form, "decision": "deny"}, OPERATOR_HEADERS)
    assert resp.status_code == 303
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert "code" not in query


@pytest.mark.parametrize("method", ["plain", None, "s256"])
def test_authorize_refuses_non_s256_pkce(client, method):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    params = _authorize_params(client_id, verifier if method == "plain" else challenge, code_challenge_method=method)
    resp = client.get("/oauth/authorize", params=params, headers=OPERATOR_HEADERS)
    assert resp.status_code == 303
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["error"] == ["invalid_request"]
    assert "code" not in query


def test_authorize_refuses_missing_challenge(client):
    client_id = _register(client)["client_id"]
    params = _authorize_params(client_id, "")
    resp = client.get("/oauth/authorize", params=params, headers=OPERATOR_HEADERS)
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["invalid_request"]


def test_authorize_refuses_an_unregistered_redirect_without_redirecting(client):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    params = _authorize_params(client_id, challenge, redirect_uri="https://attacker.example.com/cb")
    resp = client.get("/oauth/authorize", params=params, headers=OPERATOR_HEADERS)
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_authorize_refuses_an_unknown_client(client):
    _, challenge = _pkce()
    resp = client.get("/oauth/authorize", params=_authorize_params("lfo_client_nope", challenge),
                      headers=OPERATOR_HEADERS)
    assert resp.status_code == 400


def test_authorize_refuses_a_foreign_resource(client):
    client_id = _register(client)["client_id"]
    _, challenge = _pkce()
    params = _authorize_params(client_id, challenge, resource="https://other.example.com/mcp")
    resp = client.get("/oauth/authorize", params=params, headers=OPERATOR_HEADERS)
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["invalid_target"]


def test_loopback_redirect_matches_on_any_port(client):
    client_id = _register(client, redirect_uris=["http://localhost/callback"])["client_id"]
    _, challenge = _pkce()
    params = _authorize_params(client_id, challenge, redirect_uri="http://localhost:43123/callback")
    resp = client.get("/oauth/authorize", params=params, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert "Local redirect" in resp.text


# ── code exchange ────────────────────────────────────────────────────────


def test_pkce_mismatch_is_refused_and_burns_the_code(client):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    other_verifier, _ = _pkce()
    bad = _exchange(client, client_id, code, other_verifier)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert _exchange(client, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_plain_verifier_equal_to_challenge_is_refused(client):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    assert _exchange(client, client_id, code, challenge).json()["error"] == "invalid_grant"


def test_code_reuse_is_refused_and_revokes_issued_tokens(client):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    first = _exchange(client, client_id, code, verifier)
    assert first.status_code == 200
    access = first.json()["access_token"]
    assert _rpc(client, access, "tools/list").status_code == 200
    second = _exchange(client, client_id, code, verifier)
    assert second.json()["error"] == "invalid_grant"
    _assert_401_challenge(_rpc(client, access, "tools/list"))
    assert _refresh(client, client_id, first.json()["refresh_token"]).json()["error"] == "invalid_grant"


def test_code_expires_after_sixty_seconds(client, clock):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    clock.now += 61
    assert _exchange(client, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_token_redirect_uri_must_match_the_authorization_request(client):
    second = "https://claude.example.com/other_callback"
    client_id = _register(client, redirect_uris=[CLAUDE_REDIRECT, second])["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    assert _exchange(client, client_id, code, verifier, redirect_uri=second).json()["error"] == "invalid_grant"


def test_code_is_bound_to_its_client(client):
    client_id = _register(client)["client_id"]
    other_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    assert _exchange(client, other_id, code, verifier).json()["error"] == "invalid_grant"


def test_token_refuses_a_foreign_resource(client):
    client_id = _register(client)["client_id"]
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    resp = _exchange(client, client_id, code, verifier, resource="https://other.example.com/mcp")
    assert resp.json()["error"] == "invalid_target"


def test_confidential_client_must_authenticate(client):
    info = _register(client, token_endpoint_auth_method="client_secret_basic")
    secret = info["client_secret"]
    verifier, challenge = _pkce()
    code = _get_code(client, info["client_id"], challenge)
    no_secret = _exchange(client, info["client_id"], code, verifier)
    assert no_secret.status_code == 401 and no_secret.json()["error"] == "invalid_client"

    verifier, challenge = _pkce()
    code = _get_code(client, info["client_id"], challenge)
    basic = base64.b64encode(f"{info['client_id']}:{secret}".encode()).decode()
    resp = client.post(
        "/oauth/token",
        content=urlencode({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": CLAUDE_REDIRECT, "code_verifier": verifier}),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
    )
    assert resp.status_code == 200, resp.text


def test_token_endpoint_requires_form_encoding(client):
    resp = client.post("/oauth/token", json={"grant_type": "authorization_code"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# ── refresh and revocation ───────────────────────────────────────────────


def test_refresh_rotates_and_reuse_revokes_the_chain(client):
    client_id, tokens, _ = _connect(client)
    rotated = _refresh(client, client_id, tokens["refresh_token"])
    assert rotated.status_code == 200
    new = rotated.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert new["access_token"] != tokens["access_token"]
    assert _rpc(client, new["access_token"], "tools/list").status_code == 200

    replay = _refresh(client, client_id, tokens["refresh_token"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    _assert_401_challenge(_rpc(client, new["access_token"], "tools/list"))
    _assert_401_challenge(_rpc(client, tokens["access_token"], "tools/list"))
    assert _refresh(client, client_id, new["refresh_token"]).json()["error"] == "invalid_grant"


def test_refresh_token_is_bound_to_its_client(client):
    client_id, tokens, _ = _connect(client)
    other_id = _register(client)["client_id"]
    assert _refresh(client, other_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 200


def test_expired_access_token_gets_401_with_challenge(client, clock):
    client_id, tokens, _ = _connect(client)
    clock.now += 3601
    resp = _rpc(client, tokens["access_token"], "tools/list")
    _assert_401_challenge(resp)
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 200


def test_revoked_access_token_gets_401_with_challenge(client):
    client_id, tokens, _ = _connect(client)
    resp = _post_form(client, "/oauth/revoke", {"token": tokens["access_token"], "client_id": client_id})
    assert resp.status_code == 200
    _assert_401_challenge(_rpc(client, tokens["access_token"], "tools/list"))
    assert _refresh(client, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_missing_token_gets_401_with_challenge(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _assert_401_challenge(resp)
    assert "error=" not in resp.headers["www-authenticate"]


def test_get_on_the_mcp_endpoint_challenges_then_refuses_the_stream(client):
    resp = client.get("/mcp", headers={"Accept": "text/event-stream"})
    _assert_401_challenge(resp)
    _, tokens, _ = _connect(client)
    authed = client.get("/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert authed.status_code == 405
    assert authed.headers["allow"] == "POST"
    assert client.get("/mcp", headers={"Authorization": f"Bearer {BEARER}"}).status_code == 405


def test_token_for_another_resource_is_rejected(client, store):
    _, tokens, _ = _connect(client)
    assert store.validate_access_token(tokens["access_token"], resource="https://other.example.com/mcp") is None


# ── connected apps ───────────────────────────────────────────────────────


def test_connected_apps_list_and_revoke_are_local_and_bearer_only(app, client):
    client_id, tokens, _ = _connect(client)
    auth = {"Authorization": f"Bearer {BEARER}"}
    assert client.get("/oauth/clients").status_code == 403
    assert client.get("/oauth/clients", headers={"Authorization": f"Bearer {tokens['access_token']}"}).status_code == 403
    assert client.get("/oauth/clients", headers={**auth, "Tailscale-Funnel-Request": "?1"}).status_code == 403
    remote = TestClient(app, client=("100.101.102.103", 40000))
    assert remote.get("/oauth/clients", headers=auth).status_code == 403

    listed = client.get("/oauth/clients", headers=auth).json()["clients"]
    row = next(c for c in listed if c["client_id"] == client_id)
    assert row["client_name"] == "Synthetic App"
    assert row["redirect_hosts"] == ["claude.example.com"]
    assert row["active_grants"] == 1
    assert row["approved_at"] is not None
    assert "secret" not in json.dumps(listed).lower()

    assert client.delete(f"/oauth/clients/{client_id}").status_code == 403
    assert client.delete(f"/oauth/clients/{client_id}", headers=auth).status_code == 200
    _assert_401_challenge(_rpc(client, tokens["access_token"], "tools/list"))
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 401
    assert client.delete("/oauth/clients/lfo_client_nope", headers=auth).status_code == 404


def test_cli_lists_and_revokes(client, store, capsys):
    from scripts import mcp_oauth as cli

    client_id, tokens, _ = _connect(client)
    assert cli.main(["--db", store.db_path, "list"]) == 0
    out = capsys.readouterr().out
    assert client_id in out and "approved" in out
    assert cli.main(["--db", store.db_path, "revoke", client_id]) == 0
    _assert_401_challenge(_rpc(client, tokens["access_token"], "tools/list"))
    assert cli.main(["--db", store.db_path, "revoke", "lfo_client_nope"]) == 1


def test_cli_prune_deletes_expired_registrations(client, store, clock, capsys):
    from scripts import mcp_oauth as cli

    import time

    clock.now = time.time() - (15 * 60 + 1)  # the CLI's store runs on the real clock
    stale = _register(client)["client_id"]
    assert cli.main(["--db", store.db_path, "prune"]) == 0
    assert "Deleted 1" in capsys.readouterr().out
    assert store.get_client(stale) is None


# ── disabled, storage and logging ────────────────────────────────────────


def test_oauth_disabled_leaves_the_bearer_transport_as_it_was(server):
    c = TestClient(mcp_server.build_http_app(server, bearer_token=BEARER), client=LOOPBACK)
    assert c.get("/.well-known/oauth-protected-resource").status_code == 404
    assert c.get("/oauth/authorize", headers=OPERATOR_HEADERS).status_code in (404, 405)
    resp = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code == 401
    assert "www-authenticate" not in resp.headers


def test_empty_operator_logins_disables_oauth(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "oauth_operator_logins", "")
    monkeypatch.setattr(settings, "oauth_issuer_url", ISSUER)
    assert mcp_server._oauth_config_from_settings() is None
    monkeypatch.setattr(settings, "oauth_operator_logins", OPERATOR)
    monkeypatch.setattr(settings, "oauth_issuer_url", "http://insecure.example.com")
    assert mcp_server._oauth_config_from_settings() is None


def test_empty_login_set_refuses_every_consent(server, store):
    cfg = OAuthConfig(issuer=ISSUER, operator_logins=frozenset(), store=store,
                      allowed_redirect_hosts=ALLOWED_HOSTS)
    c = TestClient(mcp_server.build_http_app(server, bearer_token=BEARER, oauth=cfg),
                   client=LOOPBACK, follow_redirects=False)
    client_id = _register(c)["client_id"]
    _, challenge = _pkce()
    assert c.get("/oauth/authorize", params=_authorize_params(client_id, challenge),
                 headers=OPERATOR_HEADERS).status_code == 403


def _issued_secrets(client: TestClient) -> list[str]:
    info = _register(client, token_endpoint_auth_method="client_secret_post")
    verifier, challenge = _pkce()
    code = _get_code(client, info["client_id"], challenge)
    tokens = _exchange(client, info["client_id"], code, verifier, client_secret=info["client_secret"]).json()
    rotated = _post_form(client, "/oauth/token", {
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": info["client_id"], "client_secret": info["client_secret"],
    }).json()
    _rpc(client, rotated["access_token"], "tools/list")
    # Reuse: the old refresh token again, which revokes the chain and logs a warning.
    _post_form(client, "/oauth/token", {
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": info["client_id"], "client_secret": info["client_secret"],
    })
    _exchange(client, info["client_id"], code, verifier, client_secret=info["client_secret"])
    return [info["client_secret"], code, verifier, tokens["access_token"], tokens["refresh_token"],
            rotated["access_token"], rotated["refresh_token"]]


def test_store_holds_only_hashes(client, store):
    issued = _issued_secrets(client)
    with sqlite3.connect(store.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    conn.close()
    for value in issued:
        assert value not in dump
    assert mcp_oauth.hash_secret(issued[3]) in dump


def test_no_secret_or_identity_reaches_the_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    issued = _issued_secrets(client)
    text = caplog.text
    assert "refresh token reuse" in text  # the capture is live
    for value in [*issued, OPERATOR, BEARER]:
        assert value not in text


# ── tier classification ──────────────────────────────────────────────────


def _every_buildable_tool() -> set[str]:
    from api.services.agent_worker.inter_agent import INTER_AGENT_TOOL_SCHEMAS

    names = {c["name"] for c in mcp_server.CURATED_ENDPOINTS.values()}
    names |= {s["name"] for s in INTER_AGENT_TOOL_SCHEMAS}
    names |= set(mcp_server.CHATGPT_TOOL_NAMES)
    return names


def test_every_tool_is_classified_with_a_reason():
    buildable = _every_buildable_tool()
    unclassified = sorted(buildable - set(OAUTH_TOOL_TIER))
    assert not unclassified, (
        "Classify these tools in api/services/mcp_tool_tier.OAUTH_TOOL_TIER as allowed "
        f"or denied for OAuth-connected apps: {unclassified}"
    )
    stale = sorted(set(OAUTH_TOOL_TIER) - buildable)
    assert not stale, f"OAUTH_TOOL_TIER names tools that no longer exist: {stale}"
    for name, cls in OAUTH_TOOL_TIER.items():
        assert cls.reason.strip(), name


SAFE_WRITES = {"lifeos_task_create", "lifeos_reminder_create", "lifeos_memories_create", "lifeos_gmail_draft"}
POST_READS = {"lifeos_ask", "lifeos_search", "lifeos_slack_search"}


def test_allowed_tools_are_reads_or_the_named_safe_writes():
    methods = {c["name"]: c["method"] for c in mcp_server.CURATED_ENDPOINTS.values()}
    allowed_writes = {n for n in OAUTH_ALLOWED_TOOLS if not OAUTH_TOOL_TIER[n].read_only}
    assert allowed_writes == SAFE_WRITES
    for name in OAUTH_ALLOWED_TOOLS - SAFE_WRITES - set(mcp_server.CHATGPT_TOOL_NAMES):
        assert methods[name] == "GET" or name in POST_READS, name
    assert not any(n.startswith("lifeos_agent_") for n in OAUTH_ALLOWED_TOOLS)


def test_served_transport_sees_the_proxy_peer_not_x_forwarded_for(server, config, monkeypatch):
    """Through uvicorn as run_http configures it, a Serve-proxied request
    carrying the tailnet client's X-Forwarded-For still reaches consent as a
    loopback peer, so the operator's identity header is honoured."""
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    # A permissive trusted-proxy setting must not bring X-Forwarded-For back.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, kw=kw))
    mcp_server.run_http(server, "127.0.0.1", 0, BEARER, oauth=config)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    kw = {**captured["kw"], "host": "127.0.0.1", "port": port, "log_level": "warning"}
    srv = uvicorn.Server(uvicorn.Config(captured["app"], **kw))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not srv.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert srv.started

        def status(headers: dict[str, str]) -> int:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/oauth/authorize", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status
            except urllib.error.HTTPError as err:
                return err.code

        forwarded = {"X-Forwarded-For": "100.64.0.7", **OPERATOR_HEADERS}
        # Past the identity check: refused only for the missing client (400).
        assert status(forwarded) == 400
        assert status({"X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "guest@example.com"}) == 403
        assert status({**forwarded, "Tailscale-Funnel-Request": "?1"}) == 403

        mcp = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", method="POST",
            data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            headers={"Content-Type": "application/json", "Tailscale-Funnel-Request": "?1"},
        )
        with pytest.raises(urllib.error.HTTPError) as unauthenticated:
            urllib.request.urlopen(mcp, timeout=10)
        assert unauthenticated.value.code == 401
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def test_initialize_returns_server_instructions(client):
    resp = client.post("/mcp", headers={"Authorization": f"Bearer {BEARER}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    instructions = resp.json()["result"]["instructions"]
    assert instructions == mcp_server.SERVER_INSTRUCTIONS
    assert "lifeos_ask" in instructions and "lifeos_search" in instructions


def _socket_status(uds: str, path: str, method: str = "GET", headers: dict | None = None) -> int:
    import http.client
    import socket as socket_mod

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            self.sock.connect(uds)

    conn = UnixConnection("lifeos-mcp.example.ts.net")
    conn.request(method, path, body=b"{}" if method == "POST" else None,
                 headers={"Content-Type": "application/json", **(headers or {})})
    status = conn.getresponse().status
    conn.close()
    return status


def test_public_socket_listener_serves_only_public_paths_and_never_trusts_identity(
    server, config, monkeypatch, tmp_path,
):
    """run_http's unix-socket listener: the public paths answer, while consent,
    connected apps and anything else are 404 there, even with an operator
    identity header and no Funnel marker."""
    import threading
    import time

    import uvicorn

    captured: list = []

    class Recorder:
        def __init__(self, config):
            captured.append(config)
            self.config, self.should_exit, self.started = config, False, True

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", Recorder)
    uds = str(tmp_path / "mcp.sock")
    mcp_server.run_http(server, "127.0.0.1", 0, BEARER, oauth=config, uds=uds)
    tcp_config, sock_config = captured
    assert tcp_config.proxy_headers is False and sock_config.uds == uds
    monkeypatch.undo()

    srv = uvicorn.Server(sock_config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not srv.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert srv.started
        assert _socket_status(uds, "/.well-known/oauth-authorization-server") == 200
        assert _socket_status(uds, "/.well-known/oauth-protected-resource/mcp") == 200
        assert _socket_status(uds, "/mcp", "POST") == 401
        for path in ("/oauth/authorize", "/oauth/clients", "/", "/oauth/authorize/", "/%6Fauth/authorize"):
            assert _socket_status(uds, path, headers=OPERATOR_HEADERS) == 404, path
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


@pytest.mark.parametrize("failing", ["tcp", "socket"])
def test_a_listener_that_fails_to_start_stops_the_other_and_exits_nonzero(
    server, config, monkeypatch, tmp_path, failing,
):
    import asyncio

    import uvicorn

    servers: list = []

    class FakeServer:
        def __init__(self, config):
            self.config, self.should_exit, self.started = config, False, False
            self.is_socket = config.uds is not None
            servers.append(self)

        async def serve(self):
            if (failing == "socket") == self.is_socket:
                return None  # bind failed: returns without starting
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    uds = tmp_path / "missing-dir" / "mcp.sock"
    with pytest.raises(SystemExit) as exited:
        mcp_server.run_http(server, "127.0.0.1", 0, BEARER, oauth=config, uds=str(uds))
    assert exited.value.code == 3
    assert all(s.should_exit for s in servers)
    assert uds.parent.is_dir() and (uds.parent.stat().st_mode & 0o777) == 0o700
