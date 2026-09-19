# Instinct MCP Access

**Status:** Complete
**Last Updated:** 2026-09-19
**Audience:** Operators

How to expose a restricted subset of the LifeOS MCP tool surface to Instinct — a separate HTTP transport process, its own bearer credential, and a required tool allowlist, so Instinct's access can be rotated or revoked without touching any other client.

This is the LifeOS-side half of the setup. Instinct's own remote-MCP connector (registering the URL, storing the bearer token as a secret, attaching it to an agent) is configured on the Instinct side and isn't covered here.

---

## What this sets up

- **A second MCP HTTP process** (`lifeos-mcp-instinct.service`), on its own port, alongside the existing `lifeos-mcp-http.service`. Both bind to `127.0.0.1` only.
- **A distinct bearer credential** (`LIFEOS_MCP_INSTINCT_BEARER_TOKEN`), separate from `LIFEOS_MCP_BEARER_TOKEN`. Rotating or revoking it never affects Managed Agents or any other MCP client.
- **A required tool allowlist** (`LIFEOS_MCP_INSTINCT_ALLOWED_TOOLS`). The dedicated instance refuses to start without one — an unset allowlist is only valid on the default (`:8765`) instance, where it means "every tool."
- **A stable HTTPS `/mcp` URL** via the same outbound-only Cloudflare Tunnel pattern used for Managed Agents — see [agent-worker-setup.md Step 4](agent-worker-setup.md#step-4--expose-via-cloudflare-tunnel).
- **A disabled-by-default systemd unit** — `setup-systemd.sh` only enables it once both the credential and the allowlist are configured.

---

## Step 1 — Choose the tool allowlist

Start read-only. A suggested starter set — verified against their route handlers as read-only (`GET`, or a `POST` that only queries and never writes):

```
lifeos_health,lifeos_search,lifeos_calendar_upcoming,lifeos_calendar_search,lifeos_people_search,lifeos_person_profile,lifeos_task_list
```

Exclude, deliberately:
- Every write/create/update/delete tool (`lifeos_*_create`, `*_update`, `*_delete`, `*_complete`, `lifeos_person_update`, `lifeos_vault_write`, …).
- Every send tool (`lifeos_gmail_send`, `lifeos_gmail_draft`, `lifeos_telegram_send`).
- The `lifeos_agent_*` inter-agent tools — they derive HMAC caller proofs from the transport secret, which the Instinct instance's own credential would let an external client forge; a named instance's allowlist containing any of them fails to start (`_load_http_config` exits with an error rather than accepting it).
- Home/eero controls (`lifeos_home_eero_*`).
- Financial tools (`lifeos_monarch_*`, `lifeos_investments`).

Expand the list deliberately, one tool at a time, once Instinct's actual needs are known — the current MCP catalog includes tools that change LifeOS state.

## Step 2 — Generate the credential

```bash
openssl rand -hex 32
```

```bash
# .env
LIFEOS_MCP_INSTINCT_BEARER_TOKEN=<paste the generated hex string>
LIFEOS_MCP_INSTINCT_ALLOWED_TOOLS=lifeos_health,lifeos_search,lifeos_calendar_upcoming,lifeos_calendar_search,lifeos_people_search,lifeos_person_profile,lifeos_task_list
```

`.env` is already gitignored. Never commit a real token.

Optional override (default shown):

```bash
# The Instinct unit's ExecStart passes --host 127.0.0.1 and --port 8766
# directly, not through env vars, so it isn't affected by LIFEOS_MCP_HTTP_HOST
# (which only applies to the default instance) — see
# config/systemd/lifeos-mcp-instinct.service.
```

## Step 3 — Enable the systemd unit

```bash
sudo ./scripts/setup-systemd.sh
sudo systemctl status lifeos-mcp-instinct
```

`setup-systemd.sh` enables `lifeos-mcp-instinct.service` only when both `LIFEOS_MCP_INSTINCT_BEARER_TOKEN` and `LIFEOS_MCP_INSTINCT_ALLOWED_TOOLS` are set in `.env`; with either missing, it disables and stops the unit. The process itself enforces the same rule independently at startup — missing either one exits immediately with a `sys.exit(2)` and a log line naming which is missing — so a hand-started process (bypassing `setup-systemd.sh`) fails closed too.

Smoke-test locally:

```bash
curl -sS -X POST http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer $LIFEOS_MCP_INSTINCT_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
# Should print exactly the allowlisted tool names — nothing else.
```

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8766/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 401 — no credential
```

A call to a tool outside the allowlist is rejected without reaching the LifeOS API:

```bash
curl -sS -X POST http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer $LIFEOS_MCP_INSTINCT_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"lifeos_monarch_accounts","arguments":{}}}' | jq
# result.isError: true, result.content[0].text: "Error: Unknown tool: lifeos_monarch_accounts"
```

## Step 4 — Expose via Cloudflare Tunnel

Add a second ingress rule to the same tunnel used for the default MCP instance (or Managed Agents), pointing at the Instinct instance's port. Follow [agent-worker-setup.md Step 4](agent-worker-setup.md#step-4--expose-via-cloudflare-tunnel) for the full tunnel setup; only the `ingress` entry differs:

```yaml
tunnel: <your-tunnel-uuid>
credentials-file: /home/<your-user>/.cloudflared/<tunnel-uuid>.json

ingress:
  # ... your other routes ...
  - hostname: instinct-mcp.example.com
    service: http://127.0.0.1:8766
  - service: http_status:404
```

Add a DNS record (CNAME `instinct-mcp.example.com` → `<tunnel-uuid>.cfargotunnel.com`), reload `cloudflared`, and verify from outside the host:

```bash
curl -sS -X POST https://instinct-mcp.example.com/mcp \
  -H "Authorization: Bearer $LIFEOS_MCP_INSTINCT_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

The LifeOS process itself never binds beyond `127.0.0.1`; only the tunnel's outbound connection makes it reachable. No inbound firewall rule is added.

## Step 5 — Register with Instinct

On the Instinct side, register the tunnel's `https://.../mcp` URL as a custom remote MCP server and store `LIFEOS_MCP_INSTINCT_BEARER_TOKEN`'s value as its secret, through whatever secure secret flow Instinct's connector provides. Never paste the token into a chat message or a shared document.

---

## Rotation

Rotating the Instinct credential never affects the default MCP instance, Managed Agents, or any other client — it's a distinct env var read by a distinct process.

```bash
openssl rand -hex 32
```

1. Update `LIFEOS_MCP_INSTINCT_BEARER_TOKEN` in `.env` with the new value.
2. Restart the unit: `sudo systemctl restart lifeos-mcp-instinct`.
3. Update the stored secret on the Instinct side to match.

Calls using the old token 401 as soon as the restart completes.

## Revocation / disabling

To stop Instinct from calling LifeOS and keep it stopped:

```bash
sudo systemctl disable --now lifeos-mcp-instinct
```

Then remove `LIFEOS_MCP_INSTINCT_BEARER_TOKEN` from `.env` (and revoke or rotate it). `stop` alone isn't enough for a real revocation: the unit stays enabled, so a reboot (this host reboots itself via its network watchdog) or a `setup-systemd.sh` re-run restarts it with the same credential still valid. `disable` is not in the passwordless sudoers allowlist that `setup-systemd.sh` installs (`start`/`stop`/`restart`/`reset-failed` only), so this command prompts for a password, unlike the `stop`/`start`/`restart` commands elsewhere in this guide.

The process isn't listening at all once stopped, so every call — including ones that already have the correct token — fails (connection refused locally, and a tunnel/gateway error through Cloudflare). To also remove it from Instinct's side, delete or disable the registered connection there.

## Teardown

To remove the dedicated instance entirely:

1. `sudo systemctl disable --now lifeos-mcp-instinct`
2. Remove `LIFEOS_MCP_INSTINCT_BEARER_TOKEN` and `LIFEOS_MCP_INSTINCT_ALLOWED_TOOLS` from `.env`.
3. Remove the tunnel's ingress rule and DNS record for the Instinct hostname; reload `cloudflared`.
4. Remove the registered connection on the Instinct side.

The default MCP instance (`lifeos-mcp-http`) and every other client are unaffected — they never shared a credential, a port, or a systemd unit with the Instinct instance.

---

## Related Documents

### Design Context
- [Root AGENTS.md](../../AGENTS.md) — Development principles this setup follows (ask-first on MCP tool definitions, minimal surface area)

### Operational
- [agent-worker-setup.md](agent-worker-setup.md#step-4--expose-via-cloudflare-tunnel) — The Cloudflare Tunnel pattern this guide reuses
- [Configuration](configuration.md#mcp-http-transport) — `LIFEOS_MCP_*` and `LIFEOS_MCP_INSTINCT_*` env var reference

### Code References
- [mcp_server.py](../../mcp_server.py) — `LifeOSMCPServer.__init__`'s `allowed_tools` kwarg, `_apply_tool_allowlist`, `_load_http_config`, and `dispatch()`'s tools/call allowlist check
- [api/services/log_redaction.py](../../api/services/log_redaction.py) — `BearerTokenRedactionFilter`, the log-redaction backstop this transport installs
- [config/systemd/lifeos-mcp-instinct.service](../../config/systemd/lifeos-mcp-instinct.service) — The dedicated instance's unit file
- [scripts/setup-systemd.sh](../../scripts/setup-systemd.sh) — Installs and conditionally enables the unit
- [tests/test_mcp_http_transport.py](../../tests/test_mcp_http_transport.py) — Allowlist and redaction coverage
- [tests/test_mcp_server.py](../../tests/test_mcp_server.py) — `_load_http_config` and construction-time allowlist coverage
