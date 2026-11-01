"""OAuth 2.1 endpoints mounted on the MCP HTTP app (`mcp_server.build_http_app`).

Public endpoints (reachable wherever the MCP endpoint is):

- `GET /.well-known/oauth-protected-resource[/mcp]` — RFC 9728 metadata.
- `GET /.well-known/oauth-authorization-server` — RFC 8414 metadata.
- `POST /oauth/register` — RFC 7591 dynamic client registration.
- `POST /oauth/token` — authorization-code (PKCE S256) and refresh grants.
- `POST /oauth/revoke` — RFC 7009 revocation.

Operator-only endpoints:

- `GET`/`POST /oauth/authorize` — the consent page. It answers only a request
  that Tailscale Serve proxied from the tailnet: the direct peer is loopback,
  `Tailscale-User-Login` names a configured operator, and the
  `Tailscale-Funnel-Request` marker is absent. Serve strips client-supplied
  identity headers and never adds them to Funnel traffic.
- `GET /oauth/clients`, `DELETE /oauth/clients/{client_id}` — connected apps,
  for a loopback, non-Funnel caller holding the MCP bearer token.
"""
from __future__ import annotations

import base64
import hmac
import html
import logging
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from api.services.mcp_oauth import (
    PUBLIC_EDGE_HEADERS,
    OAUTH_SCOPE,
    OAuthConfig,
    OAuthError,
    is_pkce_value,
    redirect_uri_matches,
)

logger = logging.getLogger(__name__)

IDENTITY_HEADER = "Tailscale-User-Login"
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PAGE_HEADERS = {
    **_NO_STORE,
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
TIER_DESCRIPTION = (
    "Read + safe writes: read-only LifeOS tools, plus creating tasks, static "
    "reminders, memories and unsent email drafts. No sending, deleting, "
    "home-network control, agents, schedules, syncs or person edits."
)


def is_loopback_peer(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOOPBACK_PEERS


def is_public_edge_request(request: Request) -> bool:
    """True when a public edge (Tailscale Funnel, Cloudflare Tunnel) relayed the request."""
    return any(header in request.headers for header in PUBLIC_EDGE_HEADERS)


def operator_identity(request: Request, config: OAuthConfig) -> str | None:
    """The verified operator login for a tailnet-proxied request, else None.

    All three conditions are required: a loopback peer (the request came
    through Tailscale Serve's local proxy, not straight to the port), no
    Funnel marker, and an identity header naming a configured operator.
    """
    if not config.operator_logins:
        return None
    if not is_loopback_peer(request):
        return None
    if is_public_edge_request(request):
        return None
    login = request.headers.get(IDENTITY_HEADER, "").strip().lower()
    if not login or login not in config.operator_logins:
        return None
    return login


def _same_origin_post(request: Request) -> bool:
    """Refuse a form post the browser marks as cross-site or from a foreign origin.

    The CSRF token bound to the pending request is the primary defense; this
    is a second check. The proxy may forward the public host as `Host` or as
    `X-Forwarded-Host`, so a match with either is same-origin.
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlsplit(origin).netloc.lower()
        hosts = {
            request.headers.get("host", "").lower(),
            request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower(),
        }
        if origin_host not in hosts - {""}:
            return False
    return True


def _with_params(uri: str, params: dict) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((k, v) for k, v in params.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _oauth_error_json(err: OAuthError) -> JSONResponse:
    body = {"error": err.error}
    if err.description:
        body["error_description"] = err.description
    headers = dict(_NO_STORE)
    if err.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="lifeos-oauth"'
    return JSONResponse(body, status_code=err.status, headers=headers)


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;"
        "line-height:1.45;color:#1b1b1b;background:#fafafa}"
        "@media (prefers-color-scheme:dark){body{color:#eee;background:#151515}}"
        ".box{border:1px solid #8884;border-radius:8px;padding:1rem;margin:1rem 0}"
        ".warn{border-color:#c80}code{word-break:break-all}"
        "button{font-size:1rem;padding:.6rem 1.2rem;margin-right:.6rem;border-radius:6px;"
        "border:1px solid #8888;cursor:pointer}button.approve{background:#1f6f43;color:#fff}"
        "</style></head><body>"
        f"{body_html}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_PAGE_HEADERS)


def _refusal_page(status: int, message: str) -> HTMLResponse:
    return _page("LifeOS authorization", f"<h1>Not authorized</h1><p>{html.escape(message)}</p>", status)


def _client_credentials(request: Request, form: dict) -> tuple[str, str | None]:
    """client_id/secret from HTTP Basic (RFC 6749 §2.3.1) or the form body."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "basic" and value.strip():
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except Exception as exc:
            raise OAuthError("invalid_client", "malformed Basic credentials", status=401) from exc
        user, sep, password = decoded.partition(":")
        if not sep:
            raise OAuthError("invalid_client", "malformed Basic credentials", status=401)
        return unquote(user), unquote(password)
    client_id = form.get("client_id") or ""
    if not client_id:
        raise OAuthError("invalid_client", "client authentication required", status=401)
    return client_id, form.get("client_secret")


async def _form(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        raise OAuthError("invalid_request", "body must be application/x-www-form-urlencoded")
    raw = (await request.body()).decode("utf-8", errors="replace")
    form: dict[str, str] = {}
    for key, value in parse_qsl(raw, keep_blank_values=True):
        if key in form:
            raise OAuthError("invalid_request", f"duplicate parameter: {key}")
        form[key] = value
    return form


def install_oauth_routes(app: FastAPI, config: OAuthConfig, *, bearer_token: str) -> None:
    store = config.store

    def protected_resource_metadata() -> dict:
        return {
            "resource": config.resource,
            "authorization_servers": [config.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [OAUTH_SCOPE],
            "resource_name": "LifeOS",
        }

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def oauth_protected_resource() -> JSONResponse:
        return JSONResponse(protected_resource_metadata())

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server() -> JSONResponse:
        return JSONResponse({
            "issuer": config.issuer,
            "authorization_endpoint": config.authorize_url,
            "token_endpoint": f"{config.issuer}/oauth/token",
            "registration_endpoint": f"{config.issuer}/oauth/register",
            "revocation_endpoint": f"{config.issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": [OAUTH_SCOPE],
            "authorization_response_iss_parameter_supported": True,
        })

    @app.post("/oauth/register")
    async def oauth_register(request: Request) -> JSONResponse:
        try:
            metadata = await request.json()
        except Exception:
            return _oauth_error_json(OAuthError("invalid_client_metadata", "body must be JSON"))
        try:
            info = store.register_client(metadata, allowed_redirect_hosts=config.allowed_redirect_hosts)
        except OAuthError as err:
            return _oauth_error_json(err)
        return JSONResponse(info, status_code=201, headers=_NO_STORE)

    def _redirect_error(redirect_uri: str, state: str | None, error: str, description: str) -> RedirectResponse:
        target = _with_params(redirect_uri, {
            "error": error,
            "error_description": description,
            "state": state,
            "iss": config.issuer,
        })
        return RedirectResponse(target, status_code=303, headers=_NO_STORE)

    @app.get("/oauth/authorize")
    async def oauth_authorize(request: Request) -> Response:
        login = operator_identity(request, config)
        if login is None:
            return _refusal_page(
                403,
                "Approval is available only to the LifeOS operator, from the tailnet.",
            )
        store.prune()
        q = request.query_params
        client = store.active_client(q.get("client_id", ""))
        if client is None:
            return _refusal_page(400, "Unknown or revoked client.")
        redirect_uri = q.get("redirect_uri", "")
        if not redirect_uri or not redirect_uri_matches(client["redirect_uris"], redirect_uri):
            return _refusal_page(400, "redirect_uri is not registered for this client.")
        state = q.get("state")
        if q.get("response_type") != "code":
            return _redirect_error(redirect_uri, state, "unsupported_response_type", "response_type must be code")
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not is_pkce_value(challenge):
            return _redirect_error(
                redirect_uri, state, "invalid_request", "PKCE with code_challenge_method=S256 is required",
            )
        resource = q.get("resource")
        if resource is not None and not config.resource_matches(resource):
            return _redirect_error(redirect_uri, state, "invalid_target", "resource does not name this server")

        created = store.create_pending_consent(
            operator_login=login,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=state,
            resource=config.resource,
        )
        if created is None:
            return _refusal_page(400, "Unknown or revoked client.")
        request_id, csrf = created
        host = urlsplit(redirect_uri).hostname or ""
        loopback_warning = ""
        if host.lower() in {"127.0.0.1", "localhost", "::1"}:
            loopback_warning = (
                "<div class=\"box warn\"><strong>Local redirect.</strong> This app returns to "
                "a program on the device you approve from. Approve only if you started this "
                "connection yourself.</div>"
            )
        body = (
            "<h1>Connect an app to LifeOS</h1>"
            f"<div class=\"box\"><p><strong>App:</strong> {html.escape(client['client_name'])}</p>"
            f"<p><strong>Returns to:</strong> <code>{html.escape(host)}</code></p>"
            f"<p><strong>Access granted:</strong> {html.escape(TIER_DESCRIPTION)}</p></div>"
            f"{loopback_warning}"
            "<form method=\"post\">"
            f"<input type=\"hidden\" name=\"request_id\" value=\"{html.escape(request_id)}\">"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{html.escape(csrf)}\">"
            "<button class=\"approve\" type=\"submit\" name=\"decision\" value=\"approve\">Approve</button>"
            "<button type=\"submit\" name=\"decision\" value=\"deny\">Deny</button>"
            "</form>"
        )
        return _page("Connect an app to LifeOS", body)

    @app.post("/oauth/authorize")
    async def oauth_authorize_decision(request: Request) -> Response:
        login = operator_identity(request, config)
        if login is None or not _same_origin_post(request):
            return _refusal_page(
                403,
                "Approval is available only to the LifeOS operator, from the tailnet.",
            )
        try:
            form = await _form(request)
        except OAuthError:
            return _refusal_page(400, "Malformed approval request.")
        decided = store.decide_consent(
            request_id=form.get("request_id", ""),
            csrf=form.get("csrf_token", ""),
            operator_login=login,
            approve=form.get("decision") == "approve",
        )
        if decided is None:
            return _refusal_page(400, "This approval request is invalid or has expired. Start again from the app.")
        pending, code = decided
        if code is None:
            return _redirect_error(pending["redirect_uri"], pending["state"], "access_denied", "the operator denied access")
        target = _with_params(pending["redirect_uri"], {
            "code": code,
            "state": pending["state"],
            "iss": config.issuer,
        })
        return RedirectResponse(target, status_code=303, headers=_NO_STORE)

    @app.post("/oauth/token")
    async def oauth_token(request: Request) -> JSONResponse:
        try:
            form = await _form(request)
            client_id, client_secret = _client_credentials(request, form)
            client = store.authenticate_client(client_id, client_secret)
            grant_type = form.get("grant_type")
            resource = form.get("resource")
            if grant_type == "authorization_code":
                issued = store.exchange_code(
                    client=client,
                    code=form.get("code", ""),
                    redirect_uri=form.get("redirect_uri", ""),
                    code_verifier=form.get("code_verifier", ""),
                    resource=resource,
                    resource_ok=config.resource_matches,
                )
            elif grant_type == "refresh_token":
                issued = store.refresh(
                    client=client,
                    refresh_token=form.get("refresh_token", ""),
                    resource=resource,
                    resource_ok=config.resource_matches,
                )
            else:
                raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")
        except OAuthError as err:
            return _oauth_error_json(err)
        return JSONResponse(issued.as_response(), headers=_NO_STORE)

    @app.post("/oauth/revoke")
    async def oauth_revoke(request: Request) -> Response:
        try:
            form = await _form(request)
            client_id, client_secret = _client_credentials(request, form)
            client = store.authenticate_client(client_id, client_secret)
        except OAuthError as err:
            return _oauth_error_json(err)
        store.revoke_token(client=client, token=form.get("token", ""))
        return Response(status_code=200, headers=_NO_STORE)

    def _local_admin(request: Request) -> bool:
        if not is_loopback_peer(request) or is_public_edge_request(request):
            return False
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        return (
            scheme.lower() == "bearer"
            and bool(token)
            and hmac.compare_digest(token.encode("utf-8"), bearer_token.encode("utf-8"))
        )

    @app.get("/oauth/clients")
    async def oauth_clients(request: Request) -> JSONResponse:
        if not _local_admin(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return JSONResponse({"clients": store.list_clients()}, headers=_NO_STORE)

    @app.delete("/oauth/clients/{client_id}")
    async def oauth_client_revoke(client_id: str, request: Request) -> JSONResponse:
        if not _local_admin(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if not store.revoke_client(client_id):
            return JSONResponse({"detail": "unknown client"}, status_code=404)
        return JSONResponse({"client_id": client_id, "revoked": True})
