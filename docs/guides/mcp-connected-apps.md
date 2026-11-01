# Connected Apps (Claude, ChatGPT)

**Status:** Complete
**Last Updated:** 2026-10-04
**Audience:** Operator

The Claude and ChatGPT apps can use LifeOS as a custom connector. Their servers reach the MCP HTTP transport through a small, separate Tailscale node, running in its own container, whose port 443 is public (Funnel). Each app is approved once, on a consent page that only the operator's own tailnet devices can open. A connected app gets the restricted "read + safe writes" tool tier, never the full catalog.

What the tier allows, the token rules and the residual risks are in [MCP OAuth](../specs/technical/mcp-oauth.md).

---

## How the pieces fit

| Address | Exposure | Paths | Serves |
|---|---|---|---|
| `https://<host>.<tailnet>.ts.net` | tailnet only | `/` | LifeOS itself; never public |
| `https://<host>.<tailnet>.ts.net:8443` | tailnet only | `/oauth/authorize` | The consent page |
| `https://<host>.<tailnet>.ts.net:8443` | tailnet only | `/mcp` (optional) | The bearer-authenticated MCP endpoint for the operator's own clients; see [Tailnet route](#tailnet-route-for-your-own-clients) |
| `https://<mcp-node>.<tailnet>.ts.net` | public (Funnel) | `/mcp`, `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/oauth/register`, `/oauth/token`, `/oauth/revoke` | What the app's servers call |

Connector platforms connect out only on port 443, and Funnel exposure is port-wide. LifeOS's own 443 must stay private, so the public paths live on a second node with its own hostname. It runs in a Docker container (`tailscale/tailscale`), so its network namespace holds no host service: a tailnet device connecting to it reaches nothing but what it publishes. Its only way into LifeOS is the transport's public unix-socket listener (`LIFEOS_MCP_HTTP_UDS`), mounted read-only. That listener serves only the paths in the table above, and a request through it never counts as a loopback peer, so it can never reach the consent page or the connected-apps list.

The app's servers discover the authorization server on the public node, register themselves, and send your browser to the consent page. That page loads only on a device that is on your tailnet and signed in as an operator login. After approval the app talks only to the public node, with a short-lived access token that it refreshes on its own.

## Set up the host

The MCP HTTP transport must already run: `LIFEOS_MCP_BEARER_TOKEN` set and the `lifeos-mcp-http` unit enabled, as in [Agent Worker Setup](agent-worker-setup.md#step-3--enable-the-mcp-http-systemd-unit). Without the bearer token the transport does not start. Docker must be installed with your user in the `docker` group, and the tailnet must allow Funnel for your devices (the `funnel` node attribute in the Tailscale admin console).

1. Add to `.env`, with your hostnames and your Tailscale login (`tailscale status --json` shows it under `User`; list only your own — anyone listed can approve an app):

   ```bash
   LIFEOS_OAUTH_OPERATOR_LOGINS=operator@example.com
   LIFEOS_OAUTH_ISSUER_URL=https://lifeos-mcp.<tailnet>.ts.net
   LIFEOS_OAUTH_AUTHORIZE_URL=https://<host>.<tailnet>.ts.net:8443/oauth/authorize
   LIFEOS_MCP_HTTP_UDS=/home/<user>/.local/state/lifeos-mcp/mcp.sock
   LIFEOS_MCP_FUNNEL_NODE=true
   ```

2. Add the consent route to `config/tailscale-routes.local` (private, no `funnel=on`):

   ```
   https=8443 path=/oauth/authorize target=http://127.0.0.1:8765/oauth/authorize
   ```

3. Restart the transport so it opens the socket, then apply the routes:

   ```bash
   sudo systemctl restart lifeos-mcp-http
   ./scripts/setup-tailscale.sh
   ```

4. Start the node and log it in once, as yourself. `login` prints a URL; open it and approve the node, then publish:

   ```bash
   ./scripts/mcp-funnel-node.sh up       # exits 3 until the node is logged in
   ./scripts/mcp-funnel-node.sh login
   ./scripts/mcp-funnel-node.sh apply
   ```

5. Check: `./scripts/mcp-funnel-node.sh check` exits 0, and `docker exec lifeos-mcp-funnel tailscale funnel status` shows `/` proxying to `unix:/sock/mcp.sock` under `Funnel on`.

The container restarts with Docker, and its login lives in `LIFEOS_MCP_FUNNEL_STATE_DIR`, so the node comes back by itself after a reboot; the infra watchdog repairs anything else. A newly published node can take several minutes before public connections succeed.

## Connect an app

The connector URL is `https://lifeos-mcp.<tailnet>.ts.net/mcp`.

- **Claude** (web, desktop or mobile): Settings → Connectors → Add custom connector. Paste the URL; leave the OAuth client fields empty.
- **ChatGPT** (on chatgpt.com): turn on Developer mode (Settings → Security and login, or Settings → Apps → Advanced settings). Then on the Plugins page, the **+** button → Create MCP App, with the URL as the connection. Add it to a conversation from the tools menu.

The app opens the consent page. Open it on a device that is on the tailnet and signed in as the operator login; it names the app, its redirect host and the tier. Approve. The connector then lists LifeOS's allowed tools.

Apps decide on their own when to call a tool, from the tool descriptions and the short server instructions LifeOS sends when an app connects. Saying "check LifeOS" in a message, or a line in the app's custom instructions, makes it reliable.

## See and revoke connected apps

```bash
~/.venvs/lifeos/bin/python scripts/mcp_oauth.py list
~/.venvs/lifeos/bin/python scripts/mcp_oauth.py revoke <client_id>
```

Revoking deletes the app's registration and tokens at once; to reconnect, the app registers again and is approved again. Removing the connector inside the app does not revoke anything on the LifeOS side.

## Turn it off

- **Close public access:** set `LIFEOS_MCP_FUNNEL_NODE=false`, then `docker rm -f lifeos-mcp-funnel`. The node goes offline, so nothing is public. Running `docker exec lifeos-mcp-funnel tailscale logout` first also removes it from the tailnet.
- **Disable OAuth entirely:** empty `LIFEOS_OAUTH_OPERATOR_LOGINS` and restart `lifeos-mcp-http`. Every OAuth endpoint then returns `404`, and issued tokens stop working.

## Tailnet route for your own clients

The OAuth connected-apps endpoint above remains the route for the Claude and ChatGPT apps. For the operator's own tailnet clients (for example Muse), the same MCP transport can also be served to the tailnet only, authenticated by the full-access bearer token (`LIFEOS_MCP_BEARER_TOKEN`). A bearer client lists and calls the whole curated catalog, not the connected-app tier.

1. Add the route to `config/tailscale-routes.local` (private, no `funnel=on`, `/mcp` only):

   ```
   https=8443 path=/mcp target=http://127.0.0.1:8765/mcp
   ```

   Do not mount `/` or `/oauth/*` on this port. The public node's path allowlist is unchanged.

2. Apply it: `./scripts/setup-tailscale.sh` (or wait for the infra watchdog, which also keeps the port private as declared).

3. Point the client at `https://<host>.<tailnet>.ts.net:8443/mcp` and configure it to send `Authorization: Bearer <token>`.

Tailscale Serve relays tailnet requests from `127.0.0.1`, so a loopback peer proves nothing: `/mcp` requires a valid bearer (or OAuth) token from every peer, and an unauthenticated or wrong-token request gets `401`.

Hand the token to the client through its secret or credential store (a password manager entry, or the client's own secret field). Never paste it into a chat, and never write it to a file in a repository. To rotate it, generate a new value with `openssl rand -hex 32`, update `.env`, restart `lifeos-mcp-http`, and update each client.

Check from a tailnet machine, reading the token from your secret store into the environment:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>.<tailnet>.ts.net:8443/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # 401
curl -s -X POST https://<host>.<tailnet>.ts.net:8443/mcp -H "Authorization: Bearer $LIFEOS_MCP_BEARER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # full catalog
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| The app says no server responds at the URL | The URL is not on port 443 of the public node, or the node is down; `./scripts/mcp-funnel-node.sh check` and `docker ps -a --filter name=lifeos-mcp-funnel` tell which. |
| A public request from the LifeOS host itself fails the TLS handshake | A machine cannot reach its own Funnel address. Test from a device off the tailnet. |
| Consent page returns `403` | The device is off the tailnet, signed in as another login, or reached the page through a public address. |
| The app says the server needs authentication but never shows the consent page | `LIFEOS_OAUTH_ISSUER_URL` or `LIFEOS_OAUTH_AUTHORIZE_URL` names the wrong host or port; check `/.well-known/oauth-authorization-server`. |
| Telegram alert: the public MCP node is logged out | Run `./scripts/mcp-funnel-node.sh login`, approve, then `apply`. `logs/infra-watchdog.log` shows each check. |

## Related Documents

- [MCP OAuth](../specs/technical/mcp-oauth.md) — Endpoints, consent rules, tokens, the tool tier, residual risks
- [Operations — Host routes and dependencies](operations.md#host-routes-and-dependencies) — Declared routes, the infra watchdog
- [Configuration](configuration.md#mcp-http-transport) — `LIFEOS_OAUTH_*` and `LIFEOS_MCP_*` settings
- [Agent Worker Setup](agent-worker-setup.md) — The bearer-token path for Managed Agents
