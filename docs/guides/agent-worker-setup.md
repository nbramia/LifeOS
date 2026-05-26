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

## Step 4b — Provision the Managed Agents preset, environment, and vault (Claude path)

`#agent` tasks (without `#local`) route to Claude on Anthropic's [Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) platform. The architecture has three reusable resources you set up once in the [Anthropic console](https://platform.claude.com):

- **Agent preset** (`agent_…`) — model, system prompt, MCP servers, tools, skills. Sessions reference it by ID.
- **Environment** (`env_…`) — where tool calls execute. Cloud container by default; self-hosted sandbox tracked in [#111](https://github.com/nbramia/LifeOS/issues/111).
- **Vault** (`vlt_…`) — OAuth credentials for MCP servers, matched to the MCP URLs declared in the agent preset.

> **Beta caveat:** Managed Agents launched April 2026 and the request/response schemas are still evolving. If a request fails with a 4xx, check the [Anthropic docs](https://platform.claude.com/docs/en/managed-agents) and update `api/services/agent_worker/managed_driver.py` accordingly.

### 1. Create the Vault

Workspace → Vaults → New Vault. Copy the `vlt_…` ID. Add credentials for whichever first-party connectors you want the agent to use (Gmail, Google Calendar, Google Drive, Superhuman) and any custom MCPs (Slack, Ramp, Granola, Asana, etc.).

> **URL byte-match is required.** Each Vault credential stores against an `mcp_server_url`. When the agent connects to an MCP at runtime, Anthropic matches the agent preset's `mcp_servers.url` against the Vault entry's `mcp_server_url` byte-for-byte (no trailing slash, exact subdomain, exact path). If the URLs differ at all, you'll see `MCP server '<name>' initialize failed: no credential is stored for this server URL` in the session-error stream. Copy URLs by paste rather than re-typing.

The LifeOS MCP needs a `static_bearer` credential. The console UI may default to OAuth flow; if you don't see a "static bearer" option, add it via the API:

```bash
curl -X POST "https://api.anthropic.com/v1/vaults/$VAULT_ID/credentials" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  -H "content-type: application/json" \
  -d "{
    \"display_name\": \"LifeOS MCP\",
    \"auth\": {
      \"type\": \"static_bearer\",
      \"mcp_server_url\": \"https://<your-mcp-hostname>/mcp\",
      \"token\": \"<LIFEOS_MCP_BEARER_TOKEN from Step 2>\"
    }
  }"
```

### 2. Create the cloud Environment

Workspace → Environments → New → Cloud.

> **Set `networking.type` to `unrestricted` at creation time.** The console UI defaults to `limited` mode with `allow_mcp_servers: false`, which blocks every MCP host and yields `400 MCP server host(s) blocked by environment network policy` on every session create. If you already created the environment with the default and want to fix it without recreating, `POST` (not PATCH — that returns 405) the config update.
>
> **Caveat:** the body below sends a complete `config` object that replaces the existing config wholesale. If you've also set `init_script`, package lists, or env vars, GET the current config first and merge before POSTing. For a fresh environment with only `networking` set, this single command is fine:
> ```bash
> curl -X POST "https://api.anthropic.com/v1/environments/$ENV_ID" \
>   -H "x-api-key: $ANTHROPIC_API_KEY" \
>   -H "anthropic-version: 2023-06-01" \
>   -H "anthropic-beta: managed-agents-2026-04-01" \
>   -H "content-type: application/json" \
>   -d '{"config":{"type":"cloud","networking":{"type":"unrestricted"}}}'
> ```

Copy the `env_…` ID.

### 3. Create the Agent preset

Workspace → Agents → New. Paste the YAML below (replace the LifeOS hostname with yours, replace each MCP `url` with the value your Vault uses for that integration — they must match exactly):

```yaml
name: LifeOS Worker
description: Autonomous executor for #agent-tagged tasks from LifeOS.
model: claude-sonnet-4-6
system: |
  <role>
  You are an autonomous task executor running outside the operator's
  LifeOS personal-assistant system. You receive tasks tagged #agent from
  the operator's task list and complete them end to end without further
  input.
  </role>

  <environment>
  You run inside an Anthropic-managed cloud container, not on the
  operator's machine. Your bash/read/write/edit/glob/grep tools operate
  on the container's ephemeral filesystem — use them as scratch space.
  Persistent data lives behind the attached MCP servers.
  </environment>

  <mcp_routing>
  Default to the `lifeos` MCP for any personal data: calendar, gmail,
  drive, photos, contacts, financial transactions, notes, tasks,
  reminders, person profiles, conversation history. The `lifeos` MCP
  wraps all of these and is faster than going through cloud-hosted
  alternatives.

  Use work-system MCPs (whichever are attached — slack, asana, ramp,
  granola, etc.) when a task explicitly names that system; default to
  `lifeos` otherwise. Use `web_search` / `web_fetch` for public web
  content.
  </mcp_routing>

  <output_format>
  Every task must end with a final assistant turn containing a text
  summary. Tool calls alone are not a complete response. After your last
  tool call, produce a text turn summarizing what you did and the key
  result. Be concrete: include specific names, counts, decisions, links.
  Skip filler phrases like "I have completed the task." Match the
  operator's voice when drafting on their behalf: direct, lowercase-
  leaning, no LinkedIn-speak.

  The summary is delivered to the operator via Telegram, which does NOT
  render Markdown tables, headings (`#`), or code-block borders nicely.
  Prefer prose, bullets, and bold/italic emphasis. Avoid pipe-table
  syntax — write a short list with "Title — date/time" lines instead.

  When the natural output is longer than a few short paragraphs, or is
  inherently tabular (a list of rows with multiple columns), or is a
  deliverable the operator will reuse (a report, draft, plan, comparison),
  create an artifact in a persistent store and link to it in your summary
  rather than dumping the body inline:
    - Your Bash/Write tools target the container's ephemeral filesystem
      and won't persist — never tell the operator to "see /tmp/foo.md"
      because that file is gone the moment your session ends.
    - **Reports / drafts / notes** → create a Google Doc via the
      `google_drive` MCP (or whatever doc-creation MCP is attached) and
      include the share URL in your summary.
    - **Tabular data** → create a Google Sheet via the same MCP. Never
      use Markdown pipe tables.
    - **Inline summary** → a 1–3 sentence Telegram message saying what
      you did and the artifact URL.
  </output_format>

  <ambiguity>
  Do not ask clarifying questions during execution. If a task is
  genuinely ambiguous, make a reasonable assumption, do the work, and
  note the assumption in your final summary.
  </ambiguity>

  <thinking>
  Respond directly on simple lookups. Reserve extended thinking for
  multi-step problems where it will meaningfully improve the answer.
  </thinking>

mcp_servers:
  - type: url
    name: lifeos
    url: https://<your-mcp-hostname>/mcp
  # …add `type: url, name, url` entries for each Vault MCP you want available

tools:
  - type: agent_toolset_20260401
  - type: mcp_toolset
    mcp_server_name: lifeos
  # …add a matching mcp_toolset entry for every mcp_servers entry above

skills: []
```

The system prompt is structured per Anthropic's [prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) for Claude 4.6/4.7: XML section tags (literal instruction-following works better with explicit structure), positive framing, an explicit requirement to produce a final text turn after tool use (the model otherwise sometimes idles after a tool call without summarizing), and adaptive-thinking guidance.

Every `mcp_servers` entry must have a matching `mcp_toolset` in `tools` (and vice versa) — the API rejects the agent definition otherwise. Save and copy the returned `agent_…` ID.

### 4. Write the IDs to `.env`

```bash
LIFEOS_AGENT_PRESET_ID=agent_<your_agent_id>
LIFEOS_AGENT_ENVIRONMENT_ID=env_<your_environment_id>
LIFEOS_AGENT_VAULT_ID=vlt_<your_vault_id>
LIFEOS_AGENT_MANAGED_MODEL=claude-sonnet-4-6   # informational; actual model lives in the preset
ANTHROPIC_API_KEY=sk-ant-...                    # already required for the Haiku preflight
```

### 5. Restart the worker

```bash
sudo systemctl restart lifeos-agent-worker
```

Without `LIFEOS_AGENT_PRESET_ID` or `LIFEOS_AGENT_ENVIRONMENT_ID`, Claude-routed tasks park at `#agent-blocked` with an explanatory Telegram notification — the worker does not call the API without both IDs configured.

The Vault holds **only credentials**. Live data (Obsidian, photos, monarch, calendar index, etc.) is read live from LifeOS MCP and the other connected MCPs on every agent call — nothing is snapshotted to Anthropic's side.

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

### Security model

By design, the local executor (Issue C) runs `Bash`, `Read`, `Write`, `Edit`, and `WebFetch` with **no sandbox** — the agent has the same filesystem and shell access as the operator. This is intentional (see [AGENTS.md § Design Principles](../../AGENTS.md)) and means an agent task can:

- Read or modify any file the operator can read or modify
- Execute arbitrary shell commands (including `rm`, `curl`, etc.)
- Fetch any URL the host can reach (SSRF surface — an internal HTTP service accessible from the host is reachable to the agent)

The Haiku preflight sanity-check is the only guard against destructive-shaped tasks. Operators should:
- Audit `#agent`-tagged tasks before they reach the worker (look at your task list)
- Keep the daily $-cap set so even a runaway loop can't burn unlimited budget
- Treat agent-touchable secrets the same as operator-touchable secrets

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `lifeos-mcp-http` won't start, log says "requires LIFEOS_MCP_BEARER_TOKEN" | Token not set in `.env`, or systemd didn't reload | Set the token, then `sudo systemctl daemon-reload && sudo systemctl restart lifeos-mcp-http` |
| 401 with a token that should work | Token in `.env` doesn't match the one the caller is sending | Re-read `.env`, ensure no quotes or trailing whitespace; restart `lifeos-mcp-http` after edits |
| 502/504 from the tunnel | The MCP HTTP service isn't running on the configured port | `systemctl status lifeos-mcp-http` and `curl http://127.0.0.1:8765/mcp` |
| Gemma loads but responses look garbled | Chat template mismatch | Add `--chat-template gemma3` to `lifeos-llm.service` `ExecStart` |
| `llama-server` crashes on Gemma | Insufficient VRAM, or stale cached model | Check `nvidia-smi`/`rocm-smi`; re-download with `llama-server -hf unsloth/gemma-4-26B-A4B-it-GGUF` once and confirm |
| `llama-server` starts but `/v1/models` returns `503 Loading model` or `404`, log shows "sha256 mismatch" + "HEAD failed, status: 404" | Upstream HF model was updated; local cache fails llama.cpp's integrity check and the redownload 404s, leaving the server in router mode with no model loaded | Point at the local GGUF directly: set `LIFEOS_LLM_MODEL_PATH=/absolute/path/to/cached.gguf` (and `LIFEOS_LLM_MMPROJ_PATH` for vision models) in `.env`, then re-run `sudo ./scripts/setup-systemd.sh` |
| Agent worker logs "daily spend cap reached" | `LIFEOS_AGENT_DAILY_CAP_DOLLARS` is 0 or already exceeded today | Raise the cap or wait until local midnight |
| Worker doesn't pick up a `#agent` task | Task isn't `status=todo`, or worker isn't enabled | `systemctl status lifeos-agent-worker`; `curl 'http://localhost:8000/api/tasks?status=todo&tag=agent'` |
| Managed Agents session stuck running | Worker can't reach the API, or the remote session is genuinely long | Find the `managed_agent_session_id` in `data/agent_sessions.db` (`SELECT task_id, session_id, managed_agent_session_id FROM sessions WHERE status='running'`). Cancel via the Anthropic console, or `curl -X DELETE -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" -H "anthropic-beta: managed-agents-2026-04-01" https://api.anthropic.com/v1/sessions/<remote_id>`. The worker's next poll sees a 404 (mapped to `cancelled`) and finalizes. |

---

## Related Documents

- [Installation](installation.md) — base LifeOS setup
- [Configuration](configuration.md) — environment variable reference
- [Scripts](scripts.md) — `setup-systemd.sh` and other operational scripts
- Epic [#98](https://github.com/nbramia/LifeOS/issues/98) — full agent-worker design
