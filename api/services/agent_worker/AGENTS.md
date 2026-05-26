# Agent Worker

External long-running worker that picks up `#agent`-tagged tasks, executes them via Claude Opus (Managed Agents) or local Gemma (llama-server), and notifies via Telegram. Lives outside the FastAPI process — talks to LifeOS over HTTP via `/api/tasks`.

> **Status:** Scaffolding only (Issue B / #100). No LLM execution yet.
> **Owning epic:** [#98](https://github.com/nbramia/LifeOS/issues/98)

## Files in this package

| File | Responsibility |
|------|---------------|
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal-safe stop |
| `session_store.py` | SQLite-backed `sessions` + `daily_spend` tables; `Session` dataclass |
| `transcript_store.py` | Append-only JSONL per `session_id` at `data/agent_transcripts/` |
| `spend_tracker.py` | Daily $-cap ledger (inclusive ceiling; cap ≤ 0 pauses claims) |

## Lifecycle (current scope)

```
poll → can_start_task(default_budget)?
     → list /api/tasks?status=todo&tag=agent
     → atomic swap #agent → #agent-running
     → session row + transcript "claim" event
     → no-op dispatcher (replaced in Issues C/D)
     → mark task complete
     → transcript "noop_complete" + Telegram notify
```

On startup, `resume_pending()` scans non-terminal sessions and either marks them complete or rolls the tag back to `#agent` for retry. This makes the worker SIGKILL-safe.

## Where future issues plug in

| Issue | Adds |
|-------|------|
| C ([#101](https://github.com/nbramia/LifeOS/issues/101)) | Haiku preflight + local Gemma executor → replaces `_dispatch_noop` for `#local`-tagged tasks |
| D ([#102](https://github.com/nbramia/LifeOS/issues/102)) | Managed Agents driver → replaces `_dispatch_noop` for Claude-routed tasks |
| E ([#103](https://github.com/nbramia/LifeOS/issues/103)) | `lifeos_agent_*` MCP tools, lineage budgets, yield-and-resume |
| F ([#104](https://github.com/nbramia/LifeOS/issues/104)) | Telegram clarification round-trip via reply-threading |

## Open-source guardrails

- Worker is **opt-in** via `LIFEOS_AGENT_WORKER_AUTOSTART=true` (default `false`). Fresh clones don't start polling.
- All operational knobs are env vars (see `config/settings.py` `agent_*` fields and `.env.example`).
- Public-internet exposure (the MCP HTTP transport) requires `LIFEOS_MCP_BEARER_TOKEN` — empty disables the HTTP unit entirely.
- Cap of 0 (or negative) pauses all new claims as a kill-switch.

## Related Documents

- [`docs/guides/agent-worker-setup.md`](../../../docs/guides/agent-worker-setup.md) — operator setup
- [`api/services/AGENTS.md`](../AGENTS.md) — sibling services
- [Epic #98](https://github.com/nbramia/LifeOS/issues/98) — full design
