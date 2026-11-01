# MCP OAuth and the Connected-App Tool Tier

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-11-01

The MCP HTTP transport (`mcp_server.py --transport http`) accepts two credentials on its MCP endpoint (`POST /mcp`, `POST /`):

- **The bearer token** (`LIFEOS_MCP_BEARER_TOKEN`) — full tool catalog, for local and internal clients such as Managed Agents.
- **An OAuth 2.1 access token** issued by the transport's own single-operator authorization server — a restricted tool tier, for apps such as Claude and ChatGPT connectors.

OAuth is off unless `LIFEOS_OAUTH_OPERATOR_LOGINS` is non-empty and `LIFEOS_OAUTH_ISSUER_URL` is an `https` URL (see [Configuration](../../guides/configuration.md#mcp-http-transport)). Code: `api/services/mcp_oauth.py` (store), `api/services/mcp_oauth_http.py` (endpoints), `api/services/mcp_tool_tier.py` (tier).

---

## Endpoints

| Endpoint | Who can reach it | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource[/mcp]` | anyone | RFC 9728 metadata; `resource` is `<issuer>/mcp` |
| `GET /.well-known/oauth-authorization-server` | anyone | RFC 8414 metadata; `code_challenge_methods_supported: ["S256"]`, `authorization_response_iss_parameter_supported: true` |
| `POST /oauth/register` | anyone | RFC 7591 dynamic client registration (JSON) |
| `GET`/`POST /oauth/authorize` | the operator, from the tailnet | Consent page and the approve/deny decision |
| `POST /oauth/token` | registered clients | `authorization_code` and `refresh_token` grants (form-encoded) |
| `POST /oauth/revoke` | registered clients | RFC 7009 revocation |
| `GET /oauth/clients`, `DELETE /oauth/clients/{client_id}` | loopback, not via a public edge, bearer token | Connected apps and per-app revoke |

The bearer-authenticated `/mcp` may also be served to the tailnet only (see [Connected Apps](../../guides/mcp-connected-apps.md#tailnet-route-for-your-own-clients)). `/mcp` has no loopback exemption: a peer on `127.0.0.1`, which is what Tailscale Serve presents, needs a valid token like any other. The full-access bearer token is accepted only from the host and the tailnet; a request that came through a public edge needs an OAuth token: the public unix-socket listener (marked on the ASGI scope, which a client cannot set), or a request carrying `Tailscale-Funnel-Request` or `Cf-Connecting-Ip`, headers those edges add and a client cannot strip. The consent page and the connected-apps endpoints apply the same public-edge test. Off the tailnet only OAuth tokens, and so only the tier below, reach LifeOS unless `LIFEOS_MCP_ALLOW_PUBLIC_BEARER` is set.

An MCP request with no token, or an unknown, expired or revoked one, gets `401` with `WWW-Authenticate: Bearer resource_metadata="<issuer>/.well-known/oauth-protected-resource/mcp"` (plus `error="invalid_token"` when a token was presented).

`scripts/mcp_oauth.py list|revoke <client_id>|prune` reads, revokes and prunes the store directly.

## Registration

- Redirect URIs must be `https` on a host in `LIFEOS_OAUTH_ALLOWED_REDIRECT_HOSTS` (default `claude.ai`, `claude.com`, `chatgpt.com`; exact host match), or `http`/`https` on a loopback host (`127.0.0.1`, `localhost`, `::1`) on any port, with no fragment. They are pinned at registration; an `http` loopback URI matches on any port (RFC 8252 §7.3), every other URI matches exactly.
- `token_endpoint_auth_method` is `none` (public client, the default when omitted), `client_secret_post` or `client_secret_basic`.
- Client ID Metadata Documents are not supported, and the metadata does not advertise them, so Claude and ChatGPT fall back to registration.
- Registration is unauthenticated. An unapproved registration is deleted 15 minutes after it was made (pruned on every registration and authorization request, and by `prune`); revoking an unapproved client deletes it. At 5,000 unapproved registrations, a new one evicts the oldest. A registration with a live consent or an unexpired, unused code is never pruned or evicted, and registration is refused (`429`) only when every slot is protected that way. The consent page names only the client of its own authorization request, so registration spam neither locks out nor confuses a real app.

## Consent

`/oauth/authorize` answers only when all of these hold, else `403`:

1. The direct peer is loopback — the request arrived through Tailscale Serve's local proxy, since the transport binds `127.0.0.1`.
2. The request carries no public-edge header (`Tailscale-Funnel-Request` or `Cf-Connecting-Ip`).
3. `Tailscale-User-Login` names one of `LIFEOS_OAUTH_OPERATOR_LOGINS` (case-insensitive). Serve strips client-supplied identity headers, and adds none to Funnel or tagged-device traffic.

The page names the app (`client_name`, escaped), the redirect host and the tier, and warns when the redirect is a loopback address. It loads no external assets and refuses framing. Approve and Deny post back with a CSRF token bound to a server-side pending request (10 minutes, single use, same operator login). Consuming the pending request, checking the client and issuing the code happen in one `BEGIN IMMEDIATE` transaction, so a concurrent prune cannot remove the client mid-approval. A post marked `Sec-Fetch-Site: cross-site`, or whose `Origin` matches neither `Host` nor `X-Forwarded-Host`, is refused.

The authorization request must carry `response_type=code`, an `S256` `code_challenge`, and a registered `redirect_uri`; a `resource` parameter, when present, must name this server. Approval redirects with `code`, `state` and `iss`.

When the public issuer port is Funnel-exposed, the consent page needs a tailnet-only origin; `LIFEOS_OAUTH_AUTHORIZE_URL` advertises it as the authorization endpoint.

## Tokens

| Artifact | Lifetime | Rules |
|---|---|---|
| Authorization code | 60 s | Single use; bound to client, redirect URI, PKCE challenge and resource. Redemption consumes it even when verification fails. Replaying a redeemed code revokes the tokens it issued. |
| Access token | 1 h | Bound to client, chain, tier and resource. |
| Refresh token | 30 days | Rotated on every use. Presenting a consumed refresh token revokes its whole chain. |

A **chain** is everything issued from one code exchange. Revoking a token (RFC 7009), replaying a code or refresh token, or revoking the client revokes the chain. The store (`mcp_oauth.db` in the data directory) holds only SHA-256 hashes of client secrets, codes, tokens, CSRF tokens and operator logins. No secret and no identity-header value is logged.

## Tool tier: read + safe writes

`OAUTH_TOOL_TIER` in `api/services/mcp_tool_tier.py` classifies every tool the server can build as allowed or denied, with a reason; a tool absent from it is denied, and `tests/test_mcp_oauth.py` fails until a new tool is classified. For an OAuth request, `tools/list` returns only allowed tools, each with `annotations` (`readOnlyHint`, `destructiveHint`), and `tools/call` on any other name returns the same `Unknown tool` error (`-32602`) as a nonexistent tool.

- **Allowed reads:** every read-only curated tool (including `lifeos_vault_read`, which returns one note by vault-relative path and is confined to the vault root), plus `search` and `fetch`.
- **Allowed writes:** `lifeos_task_create`, `lifeos_reminder_create`, `lifeos_memories_create`, `lifeos_gmail_draft`, with argument guards. A task:
  - takes each `tags` entry as one plain tag (the whole string matches `[A-Za-z0-9][A-Za-z0-9_/-]{0,63}`, so no `#`, whitespace or trailing newline);
  - keeps every text field except `notes` on one line without control characters;
  - is rendered with the task store's own formatter and re-parsed with its own parser (each rendered line also NFKC-normalized), and is refused if any resulting tag is an engine, consent, protected-lifecycle or `#human` tag;
  - may name only a context whose file already exists (an omitted context means `Inbox`; `Dashboard` and symlinked files do not count), so a connected app never creates a file;
  - may not set the `fields` map or `dry_run`.

  A reminder must be `message_type: "static"` with no `endpoint_config`. Independently of OAuth, the task store accepts only a plain context name (the whole string matches `[A-Za-z0-9][A-Za-z0-9 _&'-]{0,63}`) whose file resolves directly inside the tasks directory.
- **Denied:** sending (email, Telegram), deletes, person and fact edits, vault writes, task lifecycle actions, projects, schedules, sync, calendar writes (they email attendees), home-network control, Human-queue writes, workout logging, and the `lifeos_agent_*` family.
- **Operator grant only:** the host tools (`lifeos_fs_list`, `lifeos_fs_read`, `lifeos_fs_write`, `lifeos_shell`, `lifeos_logs`) are denied in the tier. An OAuth client whose id is listed in `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS`, while `LIFEOS_HOST_TOOLS_ENABLED` is on, also gets those five, and nothing else beyond the tier. `lifeos_fs_write` and `lifeos_shell` carry `destructiveHint: true`. Their own path and command limits are in [Host Tools](../../guides/host-tools.md).
- **Operator grant, per tool:** `LIFEOS_MCP_OAUTH_TOOL_GRANTS` lists `client_id:tool_name` pairs; the named client's view gains each listed tool that is in `OAUTH_TOOL_TIER`. Host tools, `lifeos_agent_*`, `lifeos_task_update` and unknown names grant nothing, and agent tokens never receive it. Granted tools are annotated as writes (never read-only; deletes and cancels also destructive), dispatch through the same path as tier tools, and are audited identically.
- **Operator grant, per agent:** `lifeos_task_update` is denied in the tier. An OAuth client mapped to an external agent in `LIFEOS_TASK_AGENT_OAUTH_CLIENTS` (`duet=<client_id>`) also gets it, with a trimmed schema: `task_id`, `status`, `assignee`, `notes`, `priority`, `due_date`, `description`. The argument guard refuses any other key, non-string values, non-printable characters outside `notes`, a rendered forbidden tag in `description` or `notes`, and any `assignee` other than the agent itself or `none`. The server then adds `acting_agent` for the mapped agent, so the API applies the agent assignee rules under the task store's lock ([Task Management](../product/task-management.md#external-agents)). Tags and fields cannot be set this way. A bearer token whose SHA-256 is listed in `LIFEOS_MCP_AGENT_TOKENS` (`muse=<sha256>`) gets the same view as such a client: the tier plus `lifeos_task_update` as that agent, never the host tools. It is refused through a public edge unless `LIFEOS_MCP_ALLOW_PUBLIC_BEARER` is set, like the full-access token.

`search` and `fetch` exist only for OAuth requests. `search` (`{query}`) runs vault search and returns `{results: [{id, title, url, text}]}`, one per vault document, where `id` is the vault-relative path and `url` an `obsidian://` link. `fetch` (`{id}`) returns `{id, title, text, url, metadata}` for a document an earlier search by the same OAuth client returned in the same process, re-checked to resolve inside the vault. Both results carry the JSON as text content and as `structuredContent`.

## Residual risks

- **Local-writer races.** The task-context containment check and `fetch`'s inside-the-vault check resolve paths and then open them. A process on the host that swaps a context file or vault document for a symlink between the check and the open can redirect that one write or read. That needs a concurrent local filesystem writer; an OAuth caller cannot cause one.
- **Shared fetch-cache budget.** `fetch` entries are bound to the OAuth client, but the 2,048-entry budget is shared, so one client's searches can evict another's entries. That client then has to search again; it never gains access to another client's documents.
- **Case-insensitive filesystems.** The context rule treats names case-sensitively, which only matches the filesystem on a case-sensitive one. Case-insensitive filesystems (default macOS APFS, Windows) are unsupported for it: two spellings of one context map to one file.

## Related Documents

- [Connected Apps](../../guides/mcp-connected-apps.md) -- Publishing the transport and connecting Claude and ChatGPT
- [Client Surfaces](client-surfaces.md) -- The MCP transport among LifeOS's other client surfaces
- [Configuration](../../guides/configuration.md#mcp-http-transport) -- `LIFEOS_OAUTH_*` and `LIFEOS_MCP_*` settings
- [Security & Privacy](security-privacy.md) -- Broader security posture this tier fits into
- [Agent Worker Setup](../../guides/agent-worker-setup.md#step-3--enable-the-mcp-http-systemd-unit) -- Bearer-token transport setup for Managed Agents
- [Host Tools](../../guides/host-tools.md) -- The operator-granted host tools and their limits
