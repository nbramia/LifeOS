# Configuration Guide

**Status:** Complete
**Last Updated:** 2026-09-04
**Audience:** Operators

**This is the single authoritative reference for every `LIFEOS_*` environment variable and the third-party service variables (`ANTHROPIC_API_KEY`, `OLLAMA_*`, `SLACK_*`, `TELEGRAM_*`, `MONARCH_*`) that LifeOS reads.** Other guides reference this file rather than restating defaults — when documentation conflicts, this file wins (and `config/settings.py` wins over both, since the code is the source of truth).

Each section corresponds roughly to a section in [`config/settings.py`](../../config/settings.py). Pydantic Settings reads these from `.env` at startup; changes require a server restart (`./scripts/server.sh restart` or `sudo systemctl restart lifeos-api`).

## Required

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_VAULT_PATH` | path | — | Absolute or `~`-prefixed path to your Obsidian vault. Every sync, every search, and every chat reads from here. |

## Server

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_HOST` | str | `0.0.0.0` | API server bind address. Keep `0.0.0.0` for Tailscale access; `127.0.0.1` to restrict to localhost only. |
| `LIFEOS_PORT` | int | `8000` | API server port. |
| `LIFEOS_SLOW_REQUEST_MS` | int | `500` | Threshold (ms) above which `RouteTimingMiddleware` logs one WARNING for a request and counts it toward `slow_count` in `GET /api/perf/routes`. See [Observability](../specs/technical/observability.md#route-timing). |
| `LIFEOS_SERVER_HOSTNAME` | str | — | Hostname of the one machine designated to run the LifeOS API server (e.g. `<your-host>`, matching `hostname`/`socket.gethostname()` there). Empty (default) disables the guard so a fresh clone is never blocked. When set, `api/main.py` and `scripts/server.sh` refuse to start on any other machine — other machines should point at the designated host via `LIFEOS_API_URL` instead of running their own server (#506). |
| `LIFEOS_CHROMA_URL` | str | `http://localhost:8001` | ChromaDB server endpoint the API connects to. |
| `LIFEOS_CHROMA_PATH` | path | `./data/chromadb` | Where ChromaDB persists its data. |
| `LIFEOS_CODE_DIR` | path | `~/Code` | Parent directory containing LifeOS and (optionally) other projects. Used by `/claude` orchestrator path resolution. |
| `LIFEOS_BACKUP_PATH` | path | `./data/backups` | Where backup archives are written. |
| `LIFEOS_BACKUP_KEEP` | int | `2` | Nightly snapshots retained per database. Older ones are pruned only after a fully successful sync whose newest snapshot passes an integrity check, so repeated failures cannot rotate away the last good copy. |
| `TAILNET_HTTPS_URL` | str | — | Your machine's Tailscale HTTPS URL (no port), e.g. `https://<your-machine>.<tailnet>.ts.net`. Used by `scripts/setup-tailscale.sh` status output, and returned as `secure_url` by `GET /api/chat/config` so `/chat` can offer a one-tap link here when the mic is blocked by an insecure context. **Open `/chat` on this URL for voice** — the mic requires HTTPS. |
| `LIFEOS_VOICE_GATEWAY_URL` | str | `http://127.0.0.1:9788` | whisper-relay base URL; LifeOS reverse-proxies `/api/voice/*` here (ADR-016). |
| `LIFEOS_AGENT_BACKEND_URL` | str | *(empty)* | Agent text backend base URL. LifeOS proxies it at `/api/agent/ask/stream`, adding a bearer server-side. Empty disables the `/chat` Agent option entirely. Deliberately absent from `.env.example` — see [voice-setup.md](voice-setup.md#optional-agent-and-hermes-text-backends). |
| `LIFEOS_AGENT_BACKEND_TOKEN` | str | *(empty)* | Optional bearer token for the Agent text backend, added server-side (never exposed to the browser). |
| `LIFEOS_HERMES_BACKEND_URL` | str | *(empty)* | Hermes text backend base URL, proxied the same way at `/api/hermes/ask/stream` (#587). Empty disables the `/chat` Hermes option; with no stored backend preference, `/chat` defaults to Hermes when it's configured and reachable, else LifeOS. Deliberately absent from `.env.example`. |
| `LIFEOS_HERMES_BACKEND_TOKEN` | str | *(empty)* | Optional bearer token for the Hermes text backend, added server-side. |
| `LIFEOS_DETACHED_TURN_TIMEOUT_SECONDS` | float | `300.0` | How long a chat turn (native or Hermes-relayed) may keep running after its client disconnects before it's cancelled (#611). The clock starts at disconnect, not at turn start, so a turn that stays watched is never affected by it. Matches the proxy's own upstream read timeout (`api/routes/_proxy.py`'s `TIMEOUT`), so a detached turn isn't cut off any earlier than a connected one already tolerates. |

**Tailscale Serve (phone /chat + voice):** run once after install, then enable the user unit so it survives reboot:

```bash
./scripts/install-systemd-tailscale.sh
systemctl --user enable --now lifeos-tailscale.service
# Disable whisper-relay claiming :443 if you still have it:
systemctl --user disable --now whisper-relay-tailscale.service
```

This binds Tailscale **HTTPS :443 → LifeOS :8000**. whisper-relay stays on localhost; voice calls go through LifeOS same-origin.

## Auto-Deploy

Optional pull-based deploy loop: `lifeos-autodeploy.timer` polls `origin/main` every 10 minutes and, when it advances, fast-forward pulls and restarts the code services that changed. Guarded — it only acts on the `main` branch, with a clean working tree, and only `--ff-only`. Off by default so a fresh clone never silently pulls and restarts. Run `sudo ./scripts/setup-systemd.sh` after changing these. Read by `scripts/auto-deploy.sh` and `setup-systemd.sh` (not Pydantic Settings).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AUTODEPLOY_ENABLED` | bool | `false` | Enable the `lifeos-autodeploy.timer`. |
| `LIFEOS_AUTODEPLOY_NOTIFY` | str | `failure` | Telegram notifications from auto-deploy: `failure` (default), `always`, or `never`. |

## LLM Backend — Synthesis and Orchestration

Governs chat synthesis, intent classification, and agentic orchestration. The toggle decision is recorded in [ADR-009](../adr/009-llm-backend-toggle.md), extended to a third value by [ADR-024](../adr/024-remote-llm-backend.md).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_LLM_BACKEND` | str | `anthropic` | `anthropic` (Claude API), `local` (llama-server on `LIFEOS_LOCAL_LLM_URL`), or `remote` (the configured paid provider below, as the standing default rather than a per-turn pick — #771/ADR-024). `anthropic` with no `ANTHROPIC_API_KEY`, or `remote` without the provider fully configured, fails fast with a named error rather than silently falling back. |
| `LIFEOS_ANTHROPIC_MODEL` | str | `claude-haiku-4-5` | **Base** Claude model for chat orchestration when `LIFEOS_LLM_BACKEND=anthropic`. Per-query escalation can override it for a turn (see below). |
| `LIFEOS_ANTHROPIC_SPECIALIST_MODEL` | str | `claude-sonnet-5` | Sonnet-tier model for specialist calls (relationship insights, fact extraction, tone analysis) — independent of the orchestrator model above. Pin aliases here, never dated snapshots (`claude-*-20YYMMDD`): snapshots retire and 404, silently breaking specialist features (#470). |
| `LIFEOS_AGENT_ESCALATION_MODEL` | str | — (off) | Switches per-query escalation **on**; empty disables it. Anthropic backend only. Despite the name it no longer names the rung an automatic escalation climbs to — that is limited to non-API engines (see below). A model named here is still what "escalate to opus"-style *user-directed* escalation resolves against. |
| `LIFEOS_AGENT_ESCALATION_LADDER` | str | `claude_code,codex` | Comma-separated rungs climbed on each successive refusal+pushback. Rungs must cost nothing per token: `claude_code`, `codex` (subscription CLIs) or `local` (on-box Gemma). Anthropic model ids are accepted but **dropped from the climb** with a log line — LifeOS never puts a turn on the API unless you ask. Override e.g. `local,claude_code,codex`. |
| `ANTHROPIC_API_KEY` | str | — | Required when `LIFEOS_LLM_BACKEND=anthropic`. Also the preferred client for specialized calls (relationship insights, fact extraction, tone analysis — [ADR-025](../adr/025-specialist-call-fallback.md)) regardless of `LIFEOS_LLM_BACKEND`; when unset, those calls fall back to the local llama-server if reachable, else the remote provider below, instead of silently producing nothing (#772). Web search has no local equivalent and is unaffected — see below. |
| `LIFEOS_LOCAL_LLM_URL` | str | `http://localhost:8080` | Local llama-server endpoint. |
| `LIFEOS_LOCAL_LLM_TIMEOUT` | int | `90` | Local LLM HTTP request timeout, seconds. |
| `LIFEOS_LLM_MODEL` | str | — | Optional override for the GGUF model the `lifeos-llm` systemd unit loads. When unset, the unit uses its bundled `-hf` default; when set, the setup script substitutes a `-m`/`--mmproj` form. |
| `LIFEOS_LOCAL_LLM_AUTOSTART` | bool | `false` | When `true`, the API service brings up `lifeos-llm` on its `Wants=` chain. Default `false` so a missing local model doesn't break the API. |

**When to change `LIFEOS_LLM_BACKEND`:** the default (`anthropic`) is right for operators without a high-VRAM GPU. Switch to `local` if you have a workstation that can run `llama-server` and want zero marginal cost / no data transit to Anthropic.

### OpenAI-compatible Remote Provider

A paid OpenAI-compatible endpoint — e.g. Fireworks running DeepSeek or Qwen. Reachable two ways: an explicit per-turn model pick from the chat model picker (`model_override="remote"`), or as the process-wide default via `LIFEOS_LLM_BACKEND=remote` above (#771). Never a rung the escalation ladder can reach on its own (ADR-018), regardless of which of those two ways selects it — it only ever runs when named explicitly, by an operator or by a user's per-turn pick. Also what the agent worker's local route can fall back to when the local llama-server is unreachable — see [agent-worker.md § Local executor](../specs/technical/agent-worker.md#local-executor-gemma-path).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_REMOTE_LLM_URL` | str | *(empty)* | Base URL of the provider (e.g. `https://api.fireworks.ai/inference/v1`). Empty disables the provider entirely — hidden from the chat model picker, and `model_override="remote"` is ignored. |
| `LIFEOS_REMOTE_LLM_MODEL` | str | *(empty)* | Model id to send in the request body. |
| `LIFEOS_REMOTE_LLM_API_KEY` | str | *(empty)* | Bearer token for the provider. |
| `LIFEOS_REMOTE_LLM_LABEL` | str | `Remote` | Display label for the chat model picker option. |
| `LIFEOS_REMOTE_LLM_TIMEOUT` | int | `90` | Request timeout, seconds. |
| `LIFEOS_REMOTE_LLM_INPUT_PRICE_PER_MTOK` | float | — (unset) | USD per million input tokens. Unset (distinct from `0.0`) means the rate isn't known — a turn on this provider records as unpriced rather than a guessed cost. |
| `LIFEOS_REMOTE_LLM_OUTPUT_PRICE_PER_MTOK` | float | — (unset) | USD per million output tokens. Same unset/`0.0` distinction as the input rate. |

All three of URL, model, and API key must be set for the provider to be considered configured; pricing is independent and can be added later without affecting whether turns run.

### Routing Target and Reasoning Control (#566/#773)

Query routing, conversation titling, agent-activity summaries, and person-fact filtering never use the Claude API, regardless of `LIFEOS_LLM_BACKEND` — these are cheap, auxiliary, non-user-facing calls that shouldn't carry API cost. The local llama-server (`_get_local_routing_client`) is the preferred target; when it's unreachable, these calls fall back to the configured remote provider (below) instead of silently doing nothing, still never to Anthropic (#773). These settings let the local routing target — and whether it's asked to reason — be configured independently of the main local LLM used for chat synthesis.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_LOCAL_ROUTING_LLM_URL` | str | — (falls back to `LIFEOS_LOCAL_LLM_URL`) | Dedicated llama-server endpoint for routing/validation calls. `llama-server` serves one model per process and ignores the request's `model` field, so this URL — pointing at a second `llama-server` with a different model loaded — is the actual mechanism for giving routing a distinct model from chat synthesis. Set only once that second server is actually running. |
| `LIFEOS_ROUTER_ENABLE_THINKING` | bool | `true` | Whether `query_router`'s LLM routing call requests reasoning from the local model. Measured on the live host: 23-32s/call with thinking on vs. 2-9s with it off, with substantively identical routing decisions. Left `true` (unchanged behaviour) pending a broader correctness A/B — flipping to `false` is a one-line default change once confirmed. |

`LocalLLMClient.create`/`acreate`/`astream` (and the `generate_text`/`generate_json` routing helpers) also accept per-request `enable_thinking` (bool) and `reasoning_effort` (str) keyword arguments, sent as `chat_template_kwargs: {"enable_thinking": ...}` and `reasoning_effort` on the request body. Leaving both unset adds no new keys to the request — existing callers are unaffected.

### Document Summarization Target (#742/#775)

The indexer's document-summary generation (`api/services/summarizer.py`) talks to an OpenAI-compatible endpoint directly, independent of `LIFEOS_LLM_BACKEND` and the routing target above. `llama-server` ignores the request's `model` field (one model per process); Ollama does not and rejects a request naming a model it isn't serving — this pair exists so an Ollama-only host (no `llama-server`) can still get document summaries.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_SUMMARIZER_LLM_URL` | str | — (falls back to `LIFEOS_LOCAL_LLM_URL`) | Dedicated endpoint for summarization requests. Point it at an Ollama server (e.g. `http://localhost:11434`) on a host with no `llama-server`. |
| `LIFEOS_SUMMARIZER_MODEL` | str | `local` | Model name sent in the summarization request body. The default is a placeholder `llama-server` ignores; an Ollama endpoint requires a real tag (e.g. `qwen2.5:3b-instruct`). Also backs the chat-answer synthesizer's usage-event `model` label (`api/services/synthesizer.py`) — a cosmetic reuse for consistency, not a second wire-protocol call site. |

## Embedding & Search

Encoder model selection and search-pipeline knobs. Decision recorded in [ADR-012](../adr/012-embedding-pipeline.md).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_EMBEDDING_MODEL` | str | `mixedbread-ai/mxbai-embed-large-v1` | Sentence-transformers encoder (1024-dim, ~335M params — the fresh-clone default). Changing this requires reindexing the ChromaDB collection (dimension changes). |
| `LIFEOS_EMBEDDING_CACHE` | path | — | Embedding cache directory (empty = HuggingFace default `~/.cache/huggingface`). |
| `LIFEOS_RERANKER_MODEL` | str | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder for the rerank stage. |
| `LIFEOS_RERANKER_ENABLED` | bool | `true` | Disable to skip the rerank pass (faster, lower precision). |
| `LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB` | int | `28000` | Pre-flight free-RAM gate before phase 4 (embedding). Below this threshold the phase is skipped to avoid kernel OOM. Read directly from the environment by `scripts/run_all_syncs.py` (not a Pydantic Setting). |
| `LIFEOS_EMBEDDING_BATCH_SIZE` | int | `8` | Max texts per `model.encode()` batch. Bounds peak VRAM per embedding call so one large document's chunks can't spike GPU memory and exhaust a unified-memory iGPU's SDMA queues, freezing the host (#483). Semantically neutral — only affects peak memory. |
| `LIFEOS_EMBEDDING_GPU_LOCK_ENABLED` | bool | `true` | Serializes GPU embedding across processes (API server, agent worker, nightly sync, ad-hoc scripts) via a cross-process file lock, so they can't all grab GPU compute queues at once (#521). |
| `LIFEOS_EMBEDDING_GPU_LOCK_PATH` | str | `./data/gpu_embed.lock` | `flock()` path for the cross-process GPU embedding lock (#521). Relative paths resolve against the process cwd, which every LifeOS process shares. Set to empty to disable the lock. |
| `LIFEOS_EMBEDDING_GPU_LOCK_TIMEOUT` | float | `300.0` | Max seconds to wait for the cross-process GPU embedding lock before giving up. |
| `HF_HUB_OFFLINE` | bool | `1` | Standard HuggingFace flag. `1` forces the embedding loader to use the local model cache only, skipping the huggingface.co etag round-trip on every model load. Set in `.env.example` to avoid DNS-failure retry storms during the nightly sync window (model files are pinned by `requirements.txt`). Honored directly by the HuggingFace libraries, not a Pydantic Setting. |
| `TRANSFORMERS_OFFLINE` | bool | `1` | Companion to `HF_HUB_OFFLINE` for the `transformers` library. Same rationale. |

**When to change `LIFEOS_EMBEDDING_MODEL`:** the default `mxbai-embed-large-v1` (1024-dim) is a stable, well-tested encoder that fits modest hardware. On a high-VRAM machine, `Alibaba-NLP/gte-Qwen2-1.5B-instruct` (1536-dim) is a recommended quality upgrade (larger transient footprint, ~15-22 GB). On constrained hardware, drop to `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80 MB). Any change requires a full reindex.

## Ollama — Legacy Timeout Aliases

**These `OLLAMA_*` variables are legacy aliases.** LifeOS no longer runs Ollama — routing and summarization go through the unified LLM client (the Anthropic API or a local `llama-server`, per `LIFEOS_LLM_BACKEND`). The variable names are retained only so existing operator `.env` files don't break. Of the four, only the two timeout vars still have any effect (read as generic request timeouts); `OLLAMA_HOST` and `OLLAMA_MODEL` are vestigial and read by nothing. Historical decision: [ADR-006](../adr/006-ollama-query-routing.md).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `OLLAMA_TIMEOUT` | int | `45` | Per-request timeout, seconds (still read). |
| `OLLAMA_RETRY_TIMEOUT` | int | `60` | Retry timeout, seconds (still read). |
| `OLLAMA_HOST` | str | `http://localhost:11434` | Vestigial — read by nothing. |
| `OLLAMA_MODEL` | str | `gemma4:26b` | Vestigial — read by nothing. |

## MCP HTTP Transport

The HTTP MCP transport exposes LifeOS tools to remote agents (primarily Anthropic Managed Agents — see [ADR-008](../adr/008-managed-agents-cloud-routing.md)). Bearer-token gated. See [agent-worker-setup.md](agent-worker-setup.md#mcp-http-transport) for the operator setup.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_MCP_HTTP_HOST` | str | `127.0.0.1` | Bind address. Keep `127.0.0.1` and front with Cloudflare Tunnel / Tailscale Funnel. |
| `LIFEOS_MCP_HTTP_PORT` | int | `8765` | Port. |
| `LIFEOS_MCP_BEARER_TOKEN` | str | — | Required for any non-loopback request. Generate with `openssl rand -hex 32`. Treat as a secret. |
| `LIFEOS_MCP_HTTP_URL` | str | — | Public URL Managed Agents uses to reach the MCP server. |

## Agent Worker — Defaults and Budgets

`#agent`-tagged task worker. Product spec: [agent-worker.md](../specs/product/agent-worker.md). Operator setup: [agent-worker-setup.md](agent-worker-setup.md).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AGENT_WORKER_AUTOSTART` | bool | `false` | When `true`, the worker starts on boot. Default off to require explicit opt-in. |
| `LIFEOS_AGENT_WORKER_POLL_SECONDS` | float | `60` | Poll interval for new `#agent`-tagged tasks. |
| `LIFEOS_HUMAN_QUEUE_POLL_SECONDS` | float | `300` | Poll interval for Human-queue `done_when` checks. See [human-queue.md](human-queue.md). |
| `LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS` | float | `5.00` | Per-task $-cap when the task title doesn't specify one. |
| `LIFEOS_AGENT_DEFAULT_WALL_SECONDS` | int | `14400` (4 h) | Per-task wall-time cap when title doesn't specify. |
| `LIFEOS_AGENT_DEFAULT_MAX_TOKENS` | int | `500000` | Per-task token cap when title doesn't specify. |
| `LIFEOS_AGENT_DAILY_CAP_DOLLARS` | float | `100.00` | Global daily $-cap. When crossed, the worker stops claiming new tasks until next local midnight. Set to `0` to pause new claims entirely. |
| `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` | int | `72` | How long to wait for a Telegram clarification before abandoning the task. |
| `LIFEOS_AGENT_COST_CONFIRM_THRESHOLD_DOLLARS` | float | varies | Threshold above which preflight requires Telegram confirmation before running a task. |
| `LIFEOS_AGENT_OUTPUT_DIR` | path | `LifeOS/Tasks/Agent Output` | Vault-relative folder where the worker writes an Agent Output note on every successful task completion (one note per one-off task; one shared, prepended note per recurring cron schedule). |
| `LIFEOS_AGENT_PREFLIGHT_MODEL` | str | `claude-haiku-4-5` | Anthropic model used for preflight (budget parsing, routing, ambiguity, sanity) when the preflight call runs on the Anthropic branch. |
| `LIFEOS_AGENT_PREFLIGHT_ENGINE` | str | `auto` | Which LLM client preflight's classifier call runs on. `auto` (default) is the pre-existing fallback order — Anthropic-if-key → local llama → [remote provider](#openai-compatible-remote-provider), byte-identical to every install today. `remote` runs preflight on the remote provider first (unprobed), when it's fully configured; if not configured, raises rather than silently falling back to another engine (never to the Anthropic API); `run_preflight()` degrades that to `routing=ask`. `anthropic` forces the Anthropic branch (raises the same way if no key is set). `local` forces the local llama-server client (still probed for reachability; raises if unreachable — nothing to fall back to for a forced value). An unrecognized value is treated as `auto` with a logged warning, never a crash. This only changes which client classifies a task, not which engine the task itself dispatches to. |
| `LIFEOS_AGENT_MANAGED_MODEL` | str | `claude-sonnet-5` | Informational — actual model lives in the Anthropic Console preset. |
| `LIFEOS_AGENT_MANAGED_MODEL_FOR_TESTS` | str | — | Override for test runs. |
| `LIFEOS_AGENT_MAX_SPAWN_DEPTH` | int | varies | Hard cap on nested spawn depth (parent → child → grandchild). |
| `LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT` | int | varies | Total descendants per root session. |
| `LIFEOS_AGENT_MAX_CONCURRENT_LOCAL` | int | varies | Concurrent local-executor sessions. |
| `LIFEOS_AGENT_MAX_CONCURRENT_MANAGED` | int | varies | Concurrent Managed-Agents sessions. |
| `LIFEOS_AGENT_REMOTE_EXECUTOR` | bool | `false` | Opt-in: when the [OpenAI-compatible remote provider](#openai-compatible-remote-provider) is fully configured and the local llama-server is unreachable at session start, the local route runs the session on the remote provider instead of failing. No-op unless the remote provider is fully configured. |
| `LIFEOS_AGENT_DEFAULT_ROUTE` | str | *(empty)* | Route preflight dispatches to instead of `ask` when a task has no routing cues at all — for a single-executor install there's nothing useful to ask about. Applies only when lack of cues, not a sanity failure, is why preflight would otherwise ask. Tag overrides (`#local`, `#cloud`, etc.) always win. When set to a valid route, also demotes any preflight `ambiguity` to advisory (logged, not blocking) instead of parking the task on the question — see [agent-worker.md](../specs/technical/agent-worker.md#preflight) for the full precedence. |
| `LIFEOS_LOCAL_AGENT_ENABLE_THINKING` | bool | `false` | Whether `run_agent_loop`'s tool-round and synthesis calls request reasoning/thinking from a **local** model (Anthropic backend ignores this). Default `false` (#567): measured on the real orchestrator with Gemma 4 26B-A4B across 6 multi-step questions — thinking on averaged 233.0s/1032 chars, off 72.6s, with no answer-quality regression. |

## Agent Worker — Managed Agents (Cloud)

Anthropic Console artifacts the cloud path needs. See [agent-worker-setup.md](agent-worker-setup.md#anthropic-console-setup) for provisioning.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AGENT_VAULT_ID` | str | — | Anthropic Vault holding cloud-connector OAuth credentials (Gmail, Calendar, Drive, Slack…). |
| `LIFEOS_AGENT_PRESET_ID` | str | — | Anthropic Console agent preset id (bundles Vault, model, system prompt). |
| `LIFEOS_AGENT_ENVIRONMENT_ID` | str | — | Anthropic Console environment id binding the preset to settings. |
| `LIFEOS_AGENT_CONNECTORS` | str | — | Comma-separated connector list pulled from the Vault. |
| `LIFEOS_AGENT_EXTRA_MCP_SERVERS` | str | — | Additional MCP server URLs to attach to Managed Agents sessions (advanced). |

## Claude Code Viz (`/agents` ingest of Claude Code sessions)

Read-only ingest of Claude Code's per-session JSONL transcripts. Decision: [ADR-011](../adr/011-external-agent-ingest.md).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AGENT_VIZ_PREFETCH_ENABLED` | bool | `true` | When true, a background loop walks the `/agents` snapshot between user actions and pre-computes Gemma summaries for any session without one cached, yielding to the agent worker when it's running. `false` makes summaries strictly click-on-demand. Applies to every session type shown on `/agents`, not just Claude Code. |
| `LIFEOS_CLAUDE_CODE_VIZ_ENABLED` | bool | `true` | Master switch. `false` disables the entire Claude Code ingest path. |
| `LIFEOS_CLAUDE_CODE_PROJECTS_DIR` | path | `~/.claude/projects` | Where to read Claude Code JSONLs from. |
| `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS` | int | varies | How far back to scan transcripts. |

## Claude Code Resume (`/agents` operator-controlled re-launch)

Operator-side controls for re-opening a Claude Code session from the `/agents` UI. Used in [agent-viz product spec § Operator controls — resume and Go To](../specs/product/agent-viz.md#graph-tab--operator-controls--resume-and-go-to).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_CC_RESUME_ENABLED` | bool | varies | Gates the resume UI and the `POST /api/agents/sessions/{id}/resume` route. |
| `LIFEOS_CC_RESUME_CMD` | str | — | Outer command template the server runs to relaunch a terminal (e.g., a `warp-terminal` launcher). |
| `LIFEOS_CC_RESUME_INNER_CMD` | str | — | Inner command run inside the relaunched terminal (the `claude --resume <id>` invocation). |
| `LIFEOS_CC_RESUME_ENV_FILE` | path | — | Optional `key=value` env file pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS` for the spawned terminal. |

## Codex Viz (`/agents` ingest of Codex sessions)

Read-only ingest of the Codex CLI's per-session JSONL rollouts. Same model as the Claude Code adapter above, but rooted at `~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl`. Sessions appear in `/agents` with `cx:`-prefixed ids.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_CODEX_VIZ_ENABLED` | bool | `true` | Master switch. `false` disables the entire Codex ingest path. |
| `LIFEOS_CODEX_SESSIONS_DIR` | path | `~/.codex/sessions` | Where to read Codex rollout JSONLs from. |
| `LIFEOS_CODEX_LOOKBACK_DAYS` | int | `7` | How far back to scan rollouts. |

## Codex Resume (`/agents` operator-controlled re-launch)

Mirror of the Claude Code resume controls for Codex sessions. Drives `POST /api/agents/sessions/cx:<id>/resume` (which spawns `codex resume <id>` in a wezterm pane by default).

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_CODEX_RESUME_ENABLED` | bool | `false` | Gates the resume UI and route for `cx:` sessions. |
| `LIFEOS_CODEX_RESUME_CMD` | str | `wezterm cli spawn --cwd {cwd} -- {inner_command}` | Outer launcher template. Same substitution surface as `LIFEOS_CC_RESUME_CMD`. |
| `LIFEOS_CODEX_RESUME_INNER_CMD` | str | `codex resume {session_id}` | Inner command run inside the spawned terminal. |

## Cross-Machine CLI Session Registration (`/agents` from any host)

Lets a Claude Code or Codex session running on any machine — not just the one hosting the API — register itself with `/agents`. See [agent-viz technical spec § Cross-machine CLI session registration](../specs/technical/agent-viz.md#cross-machine-cli-session-registration) and [guides/agents-go-to.md § 4](agents-go-to.md#4-cross-machine-session-registration) for setup.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AGENT_HOOK_TOKEN` | str | *(empty)* | Bearer token required from `scripts/lifeos-agent-hook.sh` on `POST /api/agents/cli-sessions/events`. Empty disables the endpoint (503). |

The token above is set on the machine hosting the API. Each machine *posting* session events (the API host included, if you want its own sessions to carry a `host`) additionally needs a local env file the hook script reads — not a LifeOS setting, since it's per-machine and lives outside `.env`:

| File | Purpose |
|---|---|
| `~/.config/lifeos/agent-hook.env` (override path via `$LIFEOS_AGENT_HOOK_ENV`) | Contains `LIFEOS_API_URL` and `LIFEOS_AGENT_HOOK_TOKEN` (the same token as above). Values already set in the environment take precedence over this file. Absent → the hook exits silently without posting. |

## Card Assignment (`#851`, host / model / effort routing)

See [guides/agent-worker-setup.md § Card assignment](agent-worker-setup.md#card-assignment-running-a-card-on-another-machine-851) for the ssh-prerequisites walkthrough and [specs/technical/agent-worker.md § Card assignment](../specs/technical/agent-worker.md#card-assignment-851) for the mechanism.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_AGENT_HOSTS` | JSON object | `{}` | `{name: ssh_target}` — maps a board-facing host name to the ssh target the worker/API connects to for it. Empty disables every remote host: a task naming a host not in this map lands at `#agent-failed`. Invalid JSON logs a warning and is treated as `{}`. Operator configuration — never committed with real values. |
| `LIFEOS_AGENT_SSH_CONNECT_TIMEOUT` | int (seconds) | `10` | How long ssh may spend establishing a connection to a remote host before giving up. Applies to remote spawn, remote kill, and remote resume/focus alike. |
| `LIFEOS_AGENT_MODEL_CATALOG_TTL_SECONDS` | int (seconds) | `86400` | How long `GET /api/agents/models` caches each engine's model list before re-querying providers. |
| `LIFEOS_CODEX_MODELS_CACHE_PATH` | str | `~/.codex/models_cache.json` | Path to the Codex CLI's own model-catalog cache, read by the model catalog endpoint for the codex engine's picker list. |
| `LIFEOS_OPENAI_API_KEY` | str | *(empty)* | Optional OpenAI API key, used ONLY as the model-catalog fallback when `LIFEOS_CODEX_MODELS_CACHE_PATH` is missing/unreadable. Never used to run turns — Codex sessions are subscription-billed through the CLI itself, never the API. |

## Claude Code Orchestration (`/claude` Telegram command)

Subprocess orchestration triggered from Telegram. See [claude-code-orchestration.md](claude-code-orchestration.md) for the operator flow.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_CLAUDE_BINARY` | str | `claude` | Path to the Claude CLI binary. |
| `LIFEOS_CLAUDE_TIMEOUT` | int | `3600` | Safety-net wall-time per session, seconds. Heartbeats keep you informed; this is a backstop. |
| `LIFEOS_CLAUDE_MAX_TURNS` | int | `50` | Max turns per session. |
| `LIFEOS_CLAUDE_MAX_COST` | float | `2.0` | Max cost per session, USD. |

## User Identity

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_USER_NAME` | str | — | Your first name. Appears in AI prompts. |
| `LIFEOS_MY_PERSON_ID` | str | — | Your CRM PersonEntity UUID. Set after first sync — find it via `curl "localhost:8000/api/crm/people?q=<your-name>" \| jq '.people[0].id'`. |
| `LIFEOS_WORK_DOMAIN` | str | — | Your primary work email domain. |
| `LIFEOS_WORK_DOMAIN_2` | str | — | Second work email domain if you have one. |
| `LIFEOS_WORK_DOMAINS_EXTRA` | str | — | Comma-separated list of any further work email domains beyond the first two (e.g. `thirdco.com,fourthco.com`). |
| `LIFEOS_TIMEZONE` | str | — | IANA timezone (e.g., `America/New_York`). |

## Relationships

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_PARTNER_NAME` | str | — | Partner's first name. |
| `LIFEOS_THERAPIST_PATTERNS` | str | — | Therapist name patterns, pipe-separated (e.g., `Dr. Example\|Example Therapist`). |
| `LIFEOS_PERSONAL_RELATIONSHIP_PATTERNS` | str | — | Pipe-separated patterns identifying personal meetings. |
| `LIFEOS_CONTACT_PERSON_MIN_MESSAGES` | int | `5` | Minimum iMessage count on a contact's phone/email handle before `scripts/create_contact_persons.py` creates a PersonEntity for an otherwise person-less Apple Contacts record — filters out one-off wrong-number contacts. |

## Vault Structure

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_CURRENT_WORK_PATH` | str | `Work/` | Work folder prefix in the vault. |
| `LIFEOS_PERSONAL_ARCHIVE_PATH` | str | `Personal/zArchive/` | Personal archive folder prefix. |
| `LIFEOS_RELATIONSHIP_FOLDER` | str | `Relationship` | Relationship folder name. |
| `LIFEOS_VAULT_MTIME_TRUSTED_AFTER` | date | — | For undated notes, trust the file's mtime as the interaction date only when the mtime is strictly later than this `YYYY-MM-DD` cutoff; otherwise the note falls back to the 1970 "undated" sentinel. Set to a date after your last bulk migration (clone/restore/mass-rename) so migration-era mtimes don't show up as recent activity. Leave unset to keep all undated notes on the sentinel. Read directly from the environment by `api/services/indexer.py` (not a Pydantic Setting). |

## Gmail Send Safety

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_GMAIL_DRAFT_SEND_COOLDOWN_SECONDS` | int | `300` | Cooling-off window for LifeOS-created Gmail drafts when the caller does not provide an exact different `X-LifeOS-Turn-ID` on send. During this window, `POST /api/gmail/send` refuses the send with HTTP 409 and asks the caller to obtain user confirmation. |
| `LIFEOS_GMAIL_DRAFT_LEDGER_MAX_TURN_TAGGED_ROWS` | int | `10000` | Cap on turn-tagged rows kept in the Gmail draft send-gate ledger (`data/gmail_draft_ledger.db`). A same-turn-id send is refused regardless of age, so these rows are never pruned by time — only the oldest are evicted once this count is exceeded, bounding the ledger's size without letting the guarantee expire on a timer. |

The ledger also keeps a sibling marker file, `data/gmail_draft_ledger.db.initialized`, next to the database. If the `.db` file goes missing while the marker survives (e.g. it was deleted or a partial backup restored it without the ledger), LifeOS treats that as lost safety-gate data and refuses every Gmail send with HTTP 409 for one `LIFEOS_GMAIL_DRAFT_SEND_COOLDOWN_SECONDS` window, then resumes normal behavior automatically. Deleting both files together (or neither existing yet, e.g. a fresh install) is read as "nothing to distrust" and is not restricted.

## Multi-Account Sync

All work toggles default to `false` — work data is not indexed unless explicitly enabled.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_SYNC_WORK_GMAIL` | bool | `false` | Enable work Gmail sync. |
| `LIFEOS_SYNC_WORK_CALENDAR` | bool | `false` | Enable work Calendar sync. |
| `LIFEOS_SYNC_WORK2_GMAIL` | bool | `false` | Enable second work Gmail. |
| `LIFEOS_SYNC_WORK2_CALENDAR` | bool | `false` | Enable second work Calendar. |
| `LIFEOS_SYNC_SLACK` | bool | `false` | Enable Slack sync. |

## Photos

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_PHOTOS_PATH` | path | `~/Pictures/Photos Library.photoslibrary` | Apple Photos library path. Read by the Apple Data Agent on macOS (see [ADR-010](../adr/010-apple-data-agent.md)). |
| `LIFEOS_APPLE_EXPORT_AGENT_LABEL` | str | `the export agent` | Name for the Apple Data Agent machine used in staleness/failure alerts from `scripts/apple_data_import.py` (e.g. `Mac Mini`, `my MacBook`). Generic default since the export agent's hardware is installer-specific (#770). |

## Fitness & Health

Apple Health/Fitness ingestion. See [apple-health.md](apple-health.md) for the end-to-end flow.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_HEALTH_EXPORT_PATH` | path | `data/apple-imports/health.json` | Path to the Apple Health export JSON written by the iOS Shortcut. Imported nightly. Point at a synced location (e.g. `~/Code/Sync/health/health.json`) for automation. |
| `LIFEOS_HEALTH_INGEST_TOKEN` | str | — | Bearer token for `POST /api/fitness/health/ingest` (the HealthBridge app's POST delivery mode). Empty disables the endpoint (503). Generate with `openssl rand -hex 32`. |
| `LIFEOS_FITNESS_SHEET_ID` | str | — | Google Sheet ID to mirror the workout log into (optional; mirror is off if unset). Requires the read-write Sheets OAuth scope — re-run the Google auth flow after enabling. |

## Journal Ring Ingest

A capture device (e.g. the Pebble Index ring) posts transcriptions here. See [journal-ring-ingest.md](journal-ring-ingest.md) for the endpoint contract.

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_JOURNAL_INGEST_TOKEN` | str | — | Bearer token for `POST /api/journal/ingest` (a capture device's transcription webhook). Empty disables the endpoint (503). Generate with `openssl rand -hex 32`. |

## Notifications

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_ALERT_EMAIL` | str | — | Destination for CRITICAL alerts (immediate) and the nightly health digest. |

## Third-party Services

### Slack

| Variable | Type | Default | Sets |
|---|---|---|---|
| `SLACK_CLIENT_ID` | str | — | Slack OAuth app client ID (full OAuth flow). |
| `SLACK_CLIENT_SECRET` | str | — | Slack OAuth app client secret. |
| `SLACK_REDIRECT_URI` | str | — | Slack OAuth redirect URL (e.g., `http://localhost:8000/api/crm/slack/callback`). |
| `SLACK_USER_TOKEN` | str | — | Direct user OAuth token (`xoxp-...`) — alternative to the full OAuth flow. |
| `SLACK_TEAM_ID` | str | — | Workspace ID. |

### Telegram

| Variable | Type | Default | Sets |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | str | — | Bot token from `@BotFather`. |
| `TELEGRAM_CHAT_ID` | str | — | Your chat ID (find via `/getUpdates`). |
| `TELEGRAM_PRIMARY_LISTENER_ENABLED` | bool | `true` | `false` stops LifeOS polling this bot's updates (send-only) while the scheduler and alerting keep delivering into it — for when another process already owns the one `getUpdates` poller this token allows. See [telegram-setup.md](telegram-setup.md#create-the-primary-bot). |

When both are set, Telegram becomes a conversational client (full chat pipeline), the scheduled-reminder delivery channel, and the alerting destination.

**Telegram bot commands** (`@your-bot`):

| Command | Description |
|---|---|
| `/new` | Start a new conversation (clears context). |
| `/status` | Check LifeOS server health. |
| `/claude <task>` (alias `/claude`) | Run a task with Claude Code orchestrator (see [claude-code-orchestration.md](claude-code-orchestration.md)). |
| `/claude_status` | Check active Claude Code session. |
| `/claude_cancel` | Cancel active Claude Code session. |
| `/codex <task>` | Run a task with the Codex CLI (sibling surface, ChatGPT-plan billed). |
| `/codex_status` | Check active Codex session. |
| `/codex_cancel` | Cancel active Codex session. |
| `/help` | Show available commands. |

Natural-language messages run through the chat pipeline (search, synthesis, tools).

**Specialized bots** (optional) are separate `@BotFather` bots registered in [`config/telegram_bots.json`](../../config/telegram_bots.json), each with a persona in `config/personas/`. Leave a bot's token unset to not run it. `*_CHAT_ID` is optional and defaults to `TELEGRAM_CHAT_ID`.

| Variable | Bot | Kind |
|---|---|---|
| `TELEGRAM_FITNESS_BOT_TOKEN` / `TELEGRAM_FITNESS_CHAT_ID` | `fitness` | Pure chat — clinical training/nutrition logging surface. |
| `TELEGRAM_THERAPIST_BOT_TOKEN` / `TELEGRAM_THERAPIST_CHAT_ID` | `therapist` | Pure chat — advice-oriented surface grounded in therapy notes. |
| `TELEGRAM_DOCTOR_BOT_TOKEN` / `TELEGRAM_DOCTOR_CHAT_ID` | `doctor` | **Orchestration** — self-repair surface that files an issue and ships a fix. See [doctor-bot.md](doctor-bot.md). |
| `TELEGRAM_FINANCE_BOT_TOKEN` / `TELEGRAM_FINANCE_CHAT_ID` | `finance` | Pure chat — financial-planning surface grounded in the investments snapshot + Monarch. |
| `TELEGRAM_JOURNAL_BOT_TOKEN` / `TELEGRAM_JOURNAL_CHAT_ID` | `journal` | Pure chat — captures disjointed fragments into `Personal/Log/`, never `Personal/Journal/`. |

### Monarch Money

| Variable | Type | Default | Sets |
|---|---|---|---|
| `MONARCH_EMAIL` | str | — | Monarch Money login email. |
| `MONARCH_PASSWORD` | str | — | Monarch Money password. |
| `LIFEOS_MONARCH_VAULT_DIR` | path | `Personal/Finance/Monarch` | Vault-relative folder where the monthly Monarch summary (`YYYY-MM.md`) lands. |

Auth tokens are cached at `data/monarch_session.pickle` after first login. Re-authenticate when the token expires (401/525) per the steps in the root [AGENTS.md § Monarch Money](../../AGENTS.md#monarch-money-financial-data).

Both `MONARCH_EMAIL`/`MONARCH_PASSWORD` unset AND no cached session at `data/monarch_session.pickle` means Monarch isn't configured — the nightly sync records it as skipped rather than failed. Any other failure (bad session, wrong password, network) still fails the run.

### Investments Snapshot

| Variable | Type | Default | Sets |
|---|---|---|---|
| `LIFEOS_INVESTMENTS_SYNC_DIR` | path | `~/Code/Sync/investments` | Directory `summary.json`/`portfolio.json` are read from by the investments API route and the `search_finances` "investments" chat tool action. |

This directory is populated by a separate Schwab export pipeline outside this repo, not by LifeOS itself. Without that pipeline (or with this unset and the default directory absent), the API route and chat tool both report a clean "not synced yet" — there is no error and nothing else to configure.

## Configuration Files

A handful of operator-tunable files live alongside the env vars. All are gitignored.

| File | Purpose |
|---|---|
| `config/people_dictionary.json` | Nickname/alias lookup (e.g., `"Al": "Alex"`). Restart server after edits. Template at `config/people_dictionary.example.json`. |
| `config/relationship_overrides.json` | Force relationship strength/circle for specific person UUIDs. Template at `config/relationship_overrides.example.json`. |
| `config/family_members.json` | Family member person UUIDs for special handling. Template at `config/family_members.example.json`. |
| `config/crm_settings.yaml` | CRM-side tunables (filters, dashboards). |
| `config/gdoc_sync.example.yaml`, `config/gsheet_sync.example.yaml` | Templates for Google Docs/Sheets sync configs. |
| `config/telegram_bots.local.json` | Optional per-install override of the tracked `config/telegram_bots.json` template — when present, it *replaces* the template entirely (not a merge), so this install's persona-bot selection is a local choice, not a change to a shared repo file. See [telegram-setup.md](telegram-setup.md#specialized-persona-bots). |

## Data Directory

Default: `data/` (gitignored). Holds personal data — back it up regularly, never commit. See [data-and-sync.md](../specs/technical/data-and-sync.md#data-stores) for the full store layout.

## Person ID Durability

When configuring overrides by person (strength, circle, tags), use **PersonEntity UUIDs**, not names — names change (renames, typos, merges); UUIDs are immutable. Find a UUID via:

```bash
curl "http://localhost:8000/api/crm/people?q=PersonName" | jq '.people[0].id'
```

## Example `.env`

A minimal `.env` that boots LifeOS in its `anthropic`-backend default mode:

```bash
# Required
LIFEOS_VAULT_PATH=~/Notes

# LLM Backend (default is `anthropic` — switch to `local` once you have llama-server running)
# LIFEOS_LLM_BACKEND=local
# ANTHROPIC_API_KEY=sk-ant-your-key-here       # required for anthropic backend

# Identity
LIFEOS_USER_NAME=YourName
LIFEOS_WORK_DOMAIN=yourcompany.com
LIFEOS_TIMEZONE=America/New_York

# Notifications
LIFEOS_ALERT_EMAIL=you@example.com

# Slack (optional)
# SLACK_USER_TOKEN=xoxp-your-token
# SLACK_TEAM_ID=T02XXXXX

# Telegram (optional, enables conversational client)
# TELEGRAM_BOT_TOKEN=your-bot-token
# TELEGRAM_CHAT_ID=your-chat-id

# Monarch Money (optional)
# MONARCH_EMAIL=you@example.com
# MONARCH_PASSWORD=your-password
```

## Related Documents

- [Installation](installation.md) — Initial setup; points back here for env-var reference.
- [First Run](first-run.md) — Post-install verification.
- [Voice Setup](voice-setup.md) — The `/chat` Agent/Hermes text-backend toggle and voice dock that the vars above configure.
- [Agent Worker Setup](agent-worker-setup.md) — Operator setup for the `#agent` worker; references many of the `LIFEOS_AGENT_*` vars above in operator-flow context.
- [Claude Code Orchestration](claude-code-orchestration.md) — `/claude` setup; references the `LIFEOS_CLAUDE_*` vars in operator-flow context.
- [Journal Ring Ingest](journal-ring-ingest.md) — `LIFEOS_JOURNAL_INGEST_TOKEN` in operator-flow context.
- [Doctor Bot](doctor-bot.md) — The self-repair orchestration bot; setup of its `TELEGRAM_DOCTOR_*` vars and the repair flow.
- [ADR-009: LIFEOS_LLM_BACKEND toggle](../adr/009-llm-backend-toggle.md) — Why the synthesis backend is operator-configurable.
- [ADR-024: Remote provider as a third backend value](../adr/024-remote-llm-backend.md) — Why `remote` can be the standing default, not just a per-turn pick.
- [ADR-019: A Turn's Lifetime Is Owned by the Server](../adr/019-turn-owned-by-server.md) — Why `LIFEOS_DETACHED_TURN_TIMEOUT_SECONDS` exists.
- [ADR-012: Embedding Pipeline](../adr/012-embedding-pipeline.md) — Why `LIFEOS_EMBEDDING_MODEL` is overridable; the OOM-protection knobs.
- [API Reference](../specs/product/api-reference.md) — Gmail send endpoint behavior controlled by the draft send cooldown.
- [Observability](../specs/technical/observability.md) — Perf tracing, route timing, and alerting behavior controlled by the vars documented above
- [`config/settings.py`](../../config/settings.py) — The source of truth; this guide should track it.
