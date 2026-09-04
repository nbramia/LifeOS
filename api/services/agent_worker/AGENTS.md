# Agent Worker

External long-running worker that picks up `#agent`-tagged tasks, executes them via Claude (Managed Agents), local Gemma (llama-server), the Claude Code CLI, or the Codex CLI, and notifies via Telegram. Lives outside the FastAPI process — talks to LifeOS over HTTP via `/api/tasks`.

## Files in this package

| File | Responsibility |
|------|---------------|
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal-safe stop, follow-up reply round-tripping |
| `session_store.py` | SQLite-backed `sessions` + `pending_questions` + `pending_messages` + `daily_spend` tables; `Session` dataclass |
| `transcript_store.py` | Append-only JSONL per `session_id` at `data/agent_transcripts/` |
| `spend_tracker.py` | Daily $-cap ledger (inclusive ceiling; cap ≤ 0 pauses claims) |
| `preflight.py` | Haiku preflight call: budget, routing (`local` / `claude` / `ask`), ambiguity, sanity |
| `router.py` | Thin dispatch helper used by the preflight-driven path |
| `local_executor.py` | Local-Gemma agent loop (synchronous, single in-process LLM client); also serves the `#cloud` remote route. Validates a task's optional `working_dir` field before any model call and threads it into every tool dispatch (#925) |
| `managed_executor.py` | Anthropic Managed Agents driver — `start()` + `poll()` for remote sessions |
| `managed_driver.py` | HTTP client for the Managed Agents API |
| `claude_code_executor.py` | Claude Code CLI driver (subprocess spawn, stream-json parse, `[NOTIFY]`/`[CLARIFY]` extraction) for `routing='claude_code'` sessions |
| `claude_code_spawn.py` | Helper that creates a parentless `routing='claude_code'`, `origin='operator'` session row for a fresh `/claude` task |
| `codex_executor.py` | Codex CLI driver (subprocess spawn, `--json` stream parse, `-o` final-message capture) for `routing='codex'` sessions. Prepends `CAPABILITIES_PREAMBLE` on the opening turn; suppresses intermediate-message streaming so only heartbeats + the final result reach Telegram |
| `codex_spawn.py` | Helper that creates a parentless `routing='codex'`, `origin='operator'` session row for a fresh `/codex` task |
| `operator_spawn.py` | Same pattern for non-code operator-initiated agent spawns from Telegram or `/chat` |
| `inter_agent.py` | Tool surface that lets agents spawn / message / yield-until peers in the same lineage. `lifeos_agent_spawn` accepts `claude`/`local`/`claude_code`/`codex` — the CLI routes enable cross-engine capability fallback (e.g. delegate browser work to a `claude_code` child) |
| `codex_skill_sync.py` | Converts engine-agnostic `.claude/skills/` into Codex's `SKILL.md` format; installed into `~/.codex/skills/` by `scripts/install_codex_skills.py` |
| `tools.py`, `tool_filter.py`, `tool_result_cache.py` | LifeOS-MCP tool catalog, per-preset filtering, repeat-call de-duplication. Read/Write/Edit/Bash accept an optional `base_dir` (#925) — a path escaping it via `..` or a symlink is rejected |
| `pricing.py` | Per-model token rates + session-hour overhead for cost accounting |
| `capabilities_preamble.py` | Static LifeOS capabilities briefing prepended to the opening user turn on the managed, local, and `codex` routes (Claude Code gets the equivalent via `--append-system-prompt`) |

## Routing destinations

The `sessions.routing` column drives dispatch. Values come from preflight (for `#agent` tasks) or from the spawn helper (for operator-spawned sessions).

| `routing` | Executor | How sessions are created |
|-----------|----------|--------------------------|
| `local` | `LocalExecutor` | Preflight or `#local` tag |
| `remote` | `LocalExecutor` | Preflight or bare `#cloud` tag (configured remote provider, e.g. Fireworks) |
| `claude` | `ManagedExecutor` | Preflight or `#cloud-haiku`/`#cloud-sonnet` tag (Anthropic API, per-token) |
| `claude_code` | `ClaudeCodeExecutor` | `claude_code_spawn.spawn_claude_code_session()` (`/claude`) or `#claude` tag (Claude Code CLI, subscription-billed) |
| `codex` | `CodexExecutor` | `codex_spawn.spawn_codex_session()` (`/codex`) or `#codex` tag (Codex CLI, subscription-billed) |
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

Parallel to the `#agent` claim path, `_dispatch_spawned_sessions` picks up sessions that arrive already-claimed with an explicit routing — operator spawns (`/agent`, `/claude`) and `lifeos_agent_spawn` children. `routing='claude_code'` sessions are handled by a dedicated `_dispatch_claude_code_session` that picks `execute()` vs `resume()` based on whether the CLI session UUID has been captured yet.

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

For `routing='claude_code'` sessions specifically, `_resume_as_followup` queues the reply as a `pending_message` and flips status back to `CLAIMED`, so the next tick's `_dispatch_claude_code_session` calls `CodeExecutor.resume(message)` with the persisted CLI session UUID — resume survives worker restarts and arbitrary time gaps. `routing='codex'` works the same way via `_dispatch_codex_session` → `CodexExecutor.resume()`. The anchor is registered only on the **final** completion message; because `CodexExecutor` no longer streams intermediate narration to Telegram, the operator's reply lands on that registered message rather than an un-anchored mid-run chunk.

## Open-source guardrails

- Worker is **opt-in** via `LIFEOS_AGENT_WORKER_AUTOSTART=true` (default `false`). Fresh clones don't start polling.
- All operational knobs are env vars (see `config/settings.py` `agent_*` and `claude_*` fields, and `.env.example`).
- Public-internet exposure (the MCP HTTP transport) requires `LIFEOS_MCP_BEARER_TOKEN` — empty disables the HTTP unit entirely.
- Cap of 0 (or negative) pauses all new claims as a kill-switch.

## Related Documents

- [`docs/guides/agent-worker-setup.md`](../../../docs/guides/agent-worker-setup.md) — operator setup
- [`docs/guides/claude-code-orchestration.md`](../../../docs/guides/claude-code-orchestration.md) — operator how-to for `/claude`
- [`api/services/AGENTS.md`](../AGENTS.md) — sibling services
