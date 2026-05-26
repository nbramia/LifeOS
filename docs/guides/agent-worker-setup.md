# Agent Worker Setup

> **Status:** Stub — expanded by later issues in the agent-worker series (#98)
> **Last Updated:** 2026-05-26
> **Audience:** Operators

One-time setup for the external agent worker that picks up `#agent`-tagged tasks and executes them via Claude Opus (Anthropic Managed Agents) or a local Gemma model. This guide covers **prerequisites only** — the worker itself ships in later issues.

---

## What this issue (#99) sets up

- **Local LLM** swapped from `gpt-oss-120b` to `unsloth/gemma-4-26B-A4B-it-GGUF`. Smaller VRAM footprint, leaves headroom for the embedding model to coexist.
- **MCP HTTP transport** on `mcp_server.py` so a remote agent platform (Anthropic Managed Agents) can call LifeOS tools without stdio access to the host.
- **Bearer-token auth** required by the HTTP transport. The stdio transport (used by local Claude Code) is unchanged and has no token check.
- **Cloudflare Tunnel** exposes the bearer-protected HTTP endpoint to the public internet.

---

## Step 1 — Swap the local LLM to Gemma

The `LIFEOS_LLM_MODEL` env var controls which GGUF `llama-server` loads. The default in this repo is now Gemma:

```bash
# .env (or .env.example to see the documented options)
LIFEOS_LLM_MODEL=unsloth/gemma-4-26B-A4B-it-GGUF
```

After setting it, re-install the systemd unit and restart:

```bash
sudo ./scripts/setup-systemd.sh
sudo systemctl restart lifeos-llm
```

Verify Gemma is loaded:

```bash
curl http://localhost:8080/v1/models | jq
# Should report the Gemma model id, not gpt-oss.
```

If chat formatting looks broken on `LIFEOS_LLM_BACKEND=local` requests, try adding `--chat-template gemma3` to the `ExecStart` line of `config/systemd/lifeos-llm.service` — Gemma 4 generally works with `--jinja` (the embedded Jinja template) but some llama.cpp builds prefer the explicit template flag.

---

## Step 2 — Generate a bearer token

The MCP HTTP transport refuses to start without a bearer token. Generate one and add it to `.env`:

```bash
openssl rand -hex 32
```

```bash
# .env
LIFEOS_MCP_BEARER_TOKEN=<paste the generated hex string>
```

Keep `.env` out of version control (it already is via `.gitignore`). Never commit a real token.

Optional overrides (defaults shown):

```bash
# LIFEOS_MCP_HTTP_HOST=127.0.0.1   # bind localhost only; Cloudflare Tunnel handles public exposure
# LIFEOS_MCP_HTTP_PORT=8765
```

---

## Step 3 — Enable the MCP HTTP systemd unit

`setup-systemd.sh` enables `lifeos-mcp-http.service` automatically when it detects a bearer token in `.env`:

```bash
sudo ./scripts/setup-systemd.sh
sudo systemctl status lifeos-mcp-http
```

Smoke-test locally (still 401 from outside because the bind is localhost — that's expected):

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $LIFEOS_MCP_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# Should print a positive integer (the number of registered tools).
```

A request without the header should return 401:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 401
```

---

## Step 4 — Expose via Cloudflare Tunnel

Anthropic Managed Agents (and any other remote MCP caller) need to reach `http://127.0.0.1:8765/mcp` from the public internet. Use [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — outbound-only, no inbound firewall rules.

If you already run `cloudflared` for another service (e.g., QuickStage), add a route to the existing tunnel config. Otherwise, follow the Cloudflare Zero Trust quickstart to create a tunnel for your account.

Example `~/.cloudflared/config.yml` snippet (replace placeholders):

```yaml
tunnel: <your-tunnel-uuid>
credentials-file: /home/<your-user>/.cloudflared/<tunnel-uuid>.json

ingress:
  # ... your other routes ...
  - hostname: mcp.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Add a DNS record (CNAME `mcp.example.com` → `<tunnel-uuid>.cfargotunnel.com`) and reload `cloudflared`.

Verify from outside the host:

```bash
curl -sS -X POST https://mcp.example.com/mcp \
  -H "Authorization: Bearer $LIFEOS_MCP_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

Hardening upgrade (deferred to a later issue): swap the bearer-token check for a [Cloudflare Access service token](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/) for revocable, per-token audit logging. Bearer token is fine for v1.

---

## Step 5 — Enable the agent worker (Issue B)

Issue B installs `lifeos-agent-worker.service`, which polls `/api/tasks` for `#agent`-tagged tasks. It's **off by default** so a fresh clone doesn't start consuming tasks before later issues add real execution.

To enable:

```bash
# .env
LIFEOS_AGENT_WORKER_AUTOSTART=true

# Optional knobs (defaults shown)
# LIFEOS_AGENT_WORKER_POLL_SECONDS=60
# LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS=5.00
# LIFEOS_AGENT_DAILY_CAP_DOLLARS=100.00
```

```bash
sudo ./scripts/setup-systemd.sh
sudo systemctl status lifeos-agent-worker
tail -f logs/agent-worker.log
```

At Issue B's scope, claiming a task does nothing except mark it complete with a placeholder Telegram notification ("no-op completion"). Real execution arrives in Issue C (#101) for the local Gemma path and Issue D (#102) for the Claude managed-agents path.

To smoke-test the claim path:

```bash
# Create a task with the #agent tag
curl -X POST http://localhost:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description":"scaffolding smoke test","tags":["agent"]}'

# Within ~60s, the worker should:
#   1. swap #agent → #agent-running on the task
#   2. record a session row in data/agent_sessions.db
#   3. append events to data/agent_transcripts/<session_id>.jsonl
#   4. mark the task complete
#   5. send a Telegram notification (if TELEGRAM_BOT_TOKEN is configured)
```

To pause new claims without stopping the worker, set `LIFEOS_AGENT_DAILY_CAP_DOLLARS=0` and restart — the worker keeps polling but refuses to claim anything new.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `lifeos-mcp-http` won't start, log says "requires LIFEOS_MCP_BEARER_TOKEN" | Token not set in `.env`, or systemd didn't reload | Set the token, then `sudo systemctl daemon-reload && sudo systemctl restart lifeos-mcp-http` |
| 401 with a token that should work | Token in `.env` doesn't match the one the caller is sending | Re-read `.env`, ensure no quotes or trailing whitespace; restart `lifeos-mcp-http` after edits |
| 502/504 from the tunnel | The MCP HTTP service isn't running on the configured port | `systemctl status lifeos-mcp-http` and `curl http://127.0.0.1:8765/mcp` |
| Gemma loads but responses look garbled | Chat template mismatch | Add `--chat-template gemma3` to `lifeos-llm.service` `ExecStart` |
| `llama-server` crashes on Gemma | Insufficient VRAM, or stale cached model | Check `nvidia-smi`/`rocm-smi`; re-download with `llama-server -hf unsloth/gemma-4-26B-A4B-it-GGUF` once and confirm |
| Agent worker logs "daily spend cap reached" | `LIFEOS_AGENT_DAILY_CAP_DOLLARS` is 0 or already exceeded today | Raise the cap or wait until local midnight |
| Worker doesn't pick up a `#agent` task | Task isn't `status=todo`, or worker isn't enabled | `systemctl status lifeos-agent-worker`; `curl 'http://localhost:8000/api/tasks?status=todo&tag=agent'` |

---

## Related Documents

- [Installation](installation.md) — base LifeOS setup
- [Configuration](configuration.md) — environment variable reference
- [Scripts](scripts.md) — `setup-systemd.sh` and other operational scripts
- Epic [#98](https://github.com/nbramia/LifeOS/issues/98) — full agent-worker design
