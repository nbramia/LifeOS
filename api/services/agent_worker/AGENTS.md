# Agent Worker

External long-running worker that picks up `#agent`-tagged tasks, executes them via Claude (Managed Agents), local Gemma (llama-server), or the Claude Code CLI, and notifies via Telegram. Lives outside the FastAPI process — talks to LifeOS over HTTP via `/api/tasks`.

## Files in this package

| File | Responsibility |
|------|---------------|
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal-safe stop, follow-up reply round-tripping |
| `session_store.py` | SQLite-backed `sessions` + `pending_questions` + `pending_messages` + `daily_spend` tables; `Session` dataclass |
| `transcript_store.py` | Append-only JSONL per `session_id` at `data/agent_transcripts/` |
| `spend_tracker.py` | Daily $-cap ledger (inclusive ceiling; cap ≤ 0 pauses claims) |
| `preflight.py` | Haiku preflight call: budget, routing (`local` / `claude` / `ask`), ambiguity, sanity |
| `router.py` | Thin dispatch helper used by the preflight-driven path |
| `local_executor.py` | Local-Gemma agent loop (synchronous, single in-process LLM client) |
| `managed_executor.py` | Anthropic Managed Agents driver — `start()` + `poll()` for remote sessions |
| `managed_driver.py` | HTTP client for the Managed Agents API |
| `code_executor.py` | Claude Code CLI driver (subprocess spawn, stream-json parse, `[NOTIFY]`/`[CLARIFY]` extraction) for `routing='code'` sessions |
| `code_spawn.py` | Helper that creates a parentless `routing='code'`, `origin='operator'` session row for a fresh `/code` task |
| `operator_spawn.py` | Same pattern for non-code operator-initiated agent spawns from Telegram or `/chat` |
| `inter_agent.py` | Tool surface that lets agents spawn / message / yield-until peers in the same lineage |
| `tools.py`, `tool_filter.py`, `tool_result_cache.py` | LifeOS-MCP tool catalog, per-preset filtering, repeat-call de-duplication |
| `pricing.py` | Per-model token rates + session-hour overhead for cost accounting |
| `capabilities_preamble.py` | Static `<lifeos>` capabilities block prepended to every managed-session user turn |

## Routing destinations

The `sessions.routing` column drives dispatch. Values come from preflight (for `#agent` tasks) or from the spawn helper (for operator-spawned sessions).

| `routing` | Executor | How sessions are created |
|-----------|----------|--------------------------|
| `local` | `LocalExecutor` | Preflight or `#local` tag |
| `claude` | `ManagedExecutor` | Preflight or `#cloud[-haiku\|-sonnet]` tag |
| `code` | `CodeExecutor` | `code_spawn.spawn_code_session()` — preflight is skipped, route is explicit |
| `ask` | — | Preflight couldn't decide; worker blocks the session and asks the operator |

## Lifecycle

```
poll → can_start_task(default_budget)?
     → list /api/tasks?status=todo&tag=agent   (preflight-driven path)
     → atomic swap #agent → #agent-running
     → session row + transcript "claim" event
     → preflight (Haiku) → routing + budget
     → dispatch to LocalExecutor / ManagedExecutor / block on routing-ask
     → handle terminal outcome + Telegram notify
```

Parallel to the `#agent` claim path, `_dispatch_spawned_sessions` picks up sessions that arrive already-claimed with an explicit routing — operator spawns (`/agent`, `/code`) and `lifeos_agent_spawn` children. `routing='code'` sessions are handled by a dedicated `_dispatch_code_session` that picks `execute()` vs `resume()` based on whether the CLI session UUID has been captured yet.

## Terminal tags (#agent path)

| Tag | When |
|-----|------|
| `#agent-completed` | Executor returned `STATUS_COMPLETED`; task `status=done` |
| `#agent-failed` | Executor returned `STATUS_FAILED` (incl. preflight sanity check) |
| `#agent-budget-exceeded` | Executor returned `STATUS_BUDGET_EXCEEDED` (token / wall / dollar cap hit) |
| `#agent-blocked` | Awaiting Telegram clarification, or Managed Agents not configured |

To re-run a terminal task, the operator must swap the tag back to `#agent` (Obsidian: edit the line; API: `POST /api/tasks/{id}/swap-tag?from=agent-failed&to=agent`).

On startup, `resume_pending()` scans non-terminal sessions and either marks them complete or rolls the tag back to `#agent` for retry. This makes the worker SIGKILL-safe.

## Follow-up replies

A Telegram reply to a session's completion message — or a reply from the web `/chat` thread view — resumes that session. The mechanism is uniform across `local`, `claude`, and `code`:

1. Worker registers a `pending_questions` row with `kind='followup'` keyed to the completion message's chunk ids.
2. The reply hook deposits the answer (Telegram listener) or enqueues it directly (`/chat` reply endpoint).
3. `_process_clarification_answers` picks up the answered row; `_resume_as_followup` dispatches to the executor branch for the session's routing.

For `routing='code'` sessions specifically, `_resume_as_followup` queues the reply as a `pending_message` and flips status back to `CLAIMED`, so the next tick's `_dispatch_code_session` calls `CodeExecutor.resume(message)` with the persisted CLI session UUID — resume survives worker restarts and arbitrary time gaps.

## Open-source guardrails

- Worker is **opt-in** via `LIFEOS_AGENT_WORKER_AUTOSTART=true` (default `false`). Fresh clones don't start polling.
- All operational knobs are env vars (see `config/settings.py` `agent_*` and `claude_*` fields, and `.env.example`).
- Public-internet exposure (the MCP HTTP transport) requires `LIFEOS_MCP_BEARER_TOKEN` — empty disables the HTTP unit entirely.
- Cap of 0 (or negative) pauses all new claims as a kill-switch.

## Related Documents

- [`docs/guides/agent-worker-setup.md`](../../../docs/guides/agent-worker-setup.md) — operator setup
- [`docs/guides/claude-code-orchestration.md`](../../../docs/guides/claude-code-orchestration.md) — operator how-to for `/code`
- [`api/services/AGENTS.md`](../AGENTS.md) — sibling services
