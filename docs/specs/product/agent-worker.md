# Agent Worker

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-03

LifeOS includes an external **agent worker** that picks up tasks you've tagged `#agent` and completes them autonomously — running locally on a self-hosted LLM or on Anthropic's Managed Agents cloud, with budget caps you can specify in the task title and full audit transcripts on every run. When the agent finishes (or gets stuck), it notifies you on Telegram. If it has a question mid-run, it asks via Telegram and waits for your reply.

The point is hands-free task completion for the long tail of small chores that aren't worth a conversation but are worth doing: "draft a follow-up to last week's intro thread," "summarize my unread emails from the partnership channel," "find every meeting where we discussed the Q3 launch."

---

## Table of Contents

1. [Quick example](#quick-example)
2. [Task conventions](#task-conventions)
3. [Routing — local vs cloud](#routing--local-vs-cloud)
4. [Budgets](#budgets)
5. [Tag lifecycle](#tag-lifecycle)
6. [Telegram interactions](#telegram-interactions)
7. [Capability boundaries](#capability-boundaries)
8. [Safety model](#safety-model)
9. [Configuration knobs](#configuration-knobs)
10. [Related Documents](#related-documents)

---

## Quick example

You add this line anywhere in your Obsidian Tasks file:

```
- [ ] TODO Summarize my unread emails from the partnership channel and reply with the top 3 by importance #agent
```

Within a poll cycle (default 60s), the worker:

1. Runs a Haiku preflight to parse the task — budget, routing, expected output, sanity check
2. Atomically swaps the tag to `#agent-running` (so two workers can't claim the same task)
3. Routes the task — to your local Gemma model, a CLI engine, your configured remote provider, or Claude on Managed Agents — from your tags or an explicit request; when it can only *infer* that a cloud connector is needed, it asks you first
4. Lets the agent execute: tool calls, MCP servers, web search, file I/O, the full kit
5. On completion: marks the task done in your vault, swaps the tag to `#agent-completed`, writes the full result to an Agent Output note (`LifeOS/Tasks/Agent Output/`), and sends you a one-paragraph Telegram summary with the actual result (linking the note)

Cost for that task: usually under $0.10 on Claude Sonnet 4.6, free on local Gemma. The full transcript (every tool call, every model turn) lands in `data/agent_transcripts/<session_id>.jsonl` for later review.

---

## Task conventions

The agent worker triggers on tasks that have the `#agent` tag and a pickup-eligible status — `todo` (`[ ]`) or `urgent` (`[!]`). Marking a `#agent` task as urgent in Obsidian doesn't skip the worker; it just signals high priority within your queue. Other statuses (`in_progress`, `done`, `cancelled`, `deferred`, `blocked`) are left alone.

Optional sub-tags steer routing:

| Tag | Effect |
|-----|--------|
| `#agent` | Required. Marks the task as eligible for autonomous execution. |
| `#local` | Forces routing to your local LLM (Gemma by default). No API spend. Subject to local model capability. |
| `#cloud` | Forces routing to your configured remote OpenAI-compatible provider (e.g. DeepSeek via Fireworks) — never the Anthropic API. Real per-token billing at that provider's rates. Requires the provider configured ([configuration.md](../../guides/configuration.md#openai-compatible-remote-provider)); an unconfigured install parks the task at `#agent-blocked` rather than falling back to Anthropic. |
| `#cloud-haiku` | Forces routing to Claude Haiku on Anthropic Managed Agents. Required for tasks that need Anthropic's cloud connectors. Per-token API billing. |
| `#cloud-sonnet` | Forces routing to Claude Sonnet on Anthropic Managed Agents. Same connector access and billing as `#cloud-haiku`. |
| `#claude` | Forces routing to Claude Code CLI (the same surface as `/claude`). Billed against your Claude Pro subscription rather than per-token. Good for code/filesystem/browser work where the cloud connectors aren't needed. |
| `#codex` | Forces routing to Codex CLI (the same surface as `/codex`). Billed against your ChatGPT subscription. Same caveat as `#claude`. |
| `#hermes` | Forces routing to the Hermes backend the persona bots use. Opens a Hermes conversation seeded with the card's title and notes; the task's cost is whatever Hermes reports, not a per-token Anthropic charge. The card's open endpoint returns a `/chat?conversation=<id>` deep link for such a task rather than spawning a terminal session; the board drawer's **Open** button is currently shown for `#claude`/`#codex` cards only. |

Without an explicit routing tag, the preflight reads the title. "With local agent" / "using gemma" force local, and naming an engine, model, or "anthropic"/"api" ("use claude", "with opus", "use the anthropic api") routes there — you asked, so it dispatches. A bare "cloud" in the title no longer counts (since #809, "cloud" means the remote provider, not Anthropic) — it falls through to the confirmation question below like any other guess.

**Inference alone never spends API credits.** Phrases like "draft an email", "check my calendar", "search my gmail" still tell the preflight the task probably needs cloud connectors, but that is a guess, so the task pauses at `#agent-blocked` and asks instead of dispatching. The same happens when the title gives no signal at all. The question offers `claude code` (subscription), `codex` (subscription), `local` (on-box Gemma), `cloud` (your configured remote provider — costs credits), or `anthropic`/a Claude model name like `opus` (Anthropic API — costs credits); reply with whichever you want. A bare "claude" in your reply means the Claude Code CLI, not the API — name a model or say "anthropic" to reach the API.

To skip the question entirely for a task you know needs the remote provider, tag it `#cloud`; for one that needs Anthropic's own cloud connectors, tag it `#cloud-haiku` or `#cloud-sonnet`.

Tag precedence (first match wins): `#local` → `#claude` → `#codex` → `#hermes` → `#cloud-haiku` → `#cloud-sonnet` → `#cloud`. The CLI routes (`#claude`, `#codex`) skip the cost-confirmation gate because they're subscription-billed, and so does `#hermes` (billed however Hermes bills, not a per-token Anthropic charge) and `#cloud` (the remote provider is priced but isn't the confirmation ceremony's Anthropic "expensive exception"); per-session dollar rollups still appear in `/agents` via the rollout ingest (the `cc:` and `cx:` session rows).

---

## Routing — local vs cloud

| | Local (Gemma) | Cloud (Claude) |
|---|---|---|
| **Best for** | Tasks against local files + LifeOS MCP. Privacy-sensitive work. | Tasks that touch Gmail / Calendar / Drive / Slack / Asana / etc. via cloud connectors. |
| **Speed** | ~50 tok/s on a workstation GPU; first-token latency dominated by load | ~70+ tok/s sustained, but session-create round-trip + container provisioning |
| **Cost** | Effectively free (electricity) | Sonnet 4.6: ~$3 / 1M input tokens, $15 / 1M output. Plus $0.08/hour session-hour overhead. |
| **Tools available** | Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, sleep + LifeOS MCP + inter-agent tools | Bash, Read, Write, Edit, Glob, Grep, web_search, web_fetch + LifeOS MCP + all your Vault-connected MCPs (cloud productivity, work tools) |
| **Filesystem reach** | Operator's actual machine — agent can touch your real files | Anthropic-managed ephemeral container by default; self-hosted sandbox planned for future |
| **Failure modes** | Model capability limits, GPU OOM | Per-task billing, MCP init failures, Anthropic rate limits |

---

## Budgets

You can put a budget in the task title. The preflight parses natural-language hints:

| Title fragment | Parsed as |
|---|---|
| `5 min` / `30s` / `1h` | `wall_seconds` |
| `max $0.50` / `budget $1.00` | `max_dollars` |
| `10k tokens` / `50000 tokens` | `max_tokens` |

If no budget appears in the title, defaults from `.env` apply (default `$5.00`, `~4 hours wall`, `500k tokens`). The worker enforces the wall and token caps externally for every route — it kills the session when either is breached and the task lands at `#agent-budget-exceeded`. The **dollar cap is enforced only on the cloud Claude (Managed Agents / API) route**, the only one with marginal per-task cost; on the local (free) and Claude Code / Codex CLI (subscription) routes a `max $…` hint is recorded but never stops the task.

There's also a global daily $-cap (`LIFEOS_AGENT_DAILY_CAP_DOLLARS`, default `$100`). When the day's accumulated cost crosses the cap, the worker stops claiming new tasks until the next local midnight. Tasks already running aren't killed.

---

## Tag lifecycle

A `#agent` task transitions through these states:

```
#agent                    (your starting tag)
   ↓ claimed
#agent-running            (worker has picked it up)
   ↓ terminal
#agent-completed          (success — task is also marked `done`)
   or
#agent-failed             (executor crashed / preflight rejected / runtime error)
   or
#agent-budget-exceeded    (hit a token / wall / dollar cap)
   or
#agent-blocked            (waiting on you via Telegram, or required setup is missing)
```

To re-run a terminal task, swap the tag back to `#agent` (Obsidian: edit the line; API: `POST /api/tasks/{id}/swap-tag?from=agent-failed&to=agent`). The full prior transcript stays in `data/agent_transcripts/`.

---

## Telegram interactions

The agent worker uses your existing Telegram bot (no second bot needed). Three message types:

1. **Completion notifications** — one paragraph summarizing what the agent did, the key result, total tokens + cost + active seconds. If a tool failed mid-run, the summary includes a footer listing the affected MCPs.

2. **Clarification requests** — if the preflight can't determine routing OR if a task title is genuinely ambiguous (e.g., "reply to Alex" with no email reference), the worker pauses the task at `#agent-blocked` and asks one targeted question on Telegram. Reply by using Telegram's native reply feature (long-press the bot's message, hit Reply). The worker picks up your answer within the next poll cycle and resumes.

3. **Failure notifications** — short message naming the task and the failure reason, plus a transcript path so you can debug. Examples: "task X failed: managed_create_session 4xx" or "task Y hit its budget (max_dollars)".

**Replying to a thread.** Every terminal notification — completion, failure, or budget cut-off — is replyable: use Telegram's native reply on it (any chunk of a long message) and the agent reopens that thread as a follow-up turn with full prior context ("actually, also CC Jane"). The reply gesture is the *only* way to continue a thread on Telegram — a plain message is always a normal chat query, so unrelated questions are never mistaken for a thread continuation.

**Starting an agent on demand.** You don't have to create a `#agent` task — send `/agent <task>` to spawn one immediately. The model is auto-routed by preflight; force it with `/agent local <task>` or `/agent claude <task>`. If routing is ambiguous — or the cloud route was only inferred — the bot asks which engine before starting. The same `/agent` command works in web chat. The resulting thread notifies and is replyable exactly like a `#agent` task.

Default clarification timeout is 72 hours (`LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS`). After that the task is abandoned permanently and you get a Telegram heads-up. The transcript is preserved.

---

## Capability boundaries

### What the agent can do

- Read or write any file the operator can (filesystem, vault, scratch space)
- Run any shell command the operator can
- Call any MCP server attached to the agent — for the cloud path, that includes whatever you've configured in your Anthropic Vault (LifeOS MCP, Gmail, Calendar, Drive, Slack, etc.); for local, that's whatever the local MCP exposes
- Search the web, fetch URLs
- Spawn child agent sessions, message them, wait for them — see [Inter-agent coordination](../technical/agent-worker.md#inter-agent-coordination) in the technical spec
- Sleep / yield — pause and resume later without burning idle compute

### What the agent can't do

- Make decisions you didn't authorize — every task starts from a tag you wrote
- Charge you beyond your configured budgets — both per-task and daily caps enforced externally
- Run without your knowing — every run lands a Telegram message
- Persist state beyond the worker's SQLite (sessions, transcripts, daily spend ledger)

---

## Safety model

The agent runs with the operator's full filesystem and shell access — no sandbox. This is intentional and consistent with the rest of LifeOS (you trust it with your data); see the [Design Principles](../../../AGENTS.md#development-principles) section in the project AGENTS doc. Four overlapping protections keep things sane:

1. **Haiku preflight sanity check** — flags obviously destructive titles (`rm -rf /`, "delete all my data") and parks them at `#agent-failed` before the executor sees them.
2. **Daily $-cap** — backstop against runaway loops; pauses all new claims when crossed.
3. **Per-task budgets** — enforced from outside the agent loop, so the model can't override them.
4. **Telegram notification on every terminal state** — you find out quickly if something runs that shouldn't have.

Operators should still audit `#agent`-tagged tasks before they reach the worker (your task list is the queue), keep budgets set, and treat agent-touchable secrets the same as operator-touchable secrets.

---

## Configuration knobs

All in `.env` — see [`agent-worker-setup.md`](../../guides/agent-worker-setup.md) for the full operator walkthrough. Most-used:

| Var | Purpose | Default |
|---|---|---|
| `LIFEOS_AGENT_WORKER_AUTOSTART` | Enable the worker on boot | `false` |
| `LIFEOS_AGENT_DAILY_CAP_DOLLARS` | Global daily $-cap (set to 0 to pause new claims) | `100.00` |
| `LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS` | Default per-task $-cap when title doesn't specify | `5.00` |
| `LIFEOS_AGENT_WORKER_POLL_SECONDS` | Polling interval | `60` |
| `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` | Telegram-clarification wait before abandoning | `72` |
| `LIFEOS_AGENT_MANAGED_MODEL` | Informational; actual model lives in the cloud preset | `claude-sonnet-5` |

---

## Related Documents

- [ADR-008: Managed Agents Cloud Routing](../../adr/008-managed-agents-cloud-routing.md) — Why local + cloud, how routing is decided, cost model
- [Agent Worker — Technical](../technical/agent-worker.md) — Architecture, executors, prompts, state machine, restart resumability
- [Agent Worker — Setup](../../guides/agent-worker-setup.md) — Operator setup (Gemma swap, MCP HTTP transport, Vault provisioning, agent preset)
- [Claude Code Orchestration (product)](claude-code-orchestration.md) — The other autonomous-work system in LifeOS; triggered from Telegram `/claude` rather than `#agent` tags
- [Agent Viz](agent-viz.md) — Live `/agents` page showing in-flight and recently-finished worker sessions
- [Task Management](task-management.md) — How `#agent` tasks live alongside regular tasks in the Obsidian Tasks plugin
- [Scheduler Guide](../../guides/scheduler.md) — A schedule's `agent` action writes the `#agent` tasks this worker runs
- [MCP Tools](mcp-tools.md) — The `lifeos_agent_*` family for inter-agent coordination
- [API Reference](api-reference.md) — `POST /api/tasks/{id}/swap-tag` and other task endpoints the worker uses
- [Architecture](../technical/architecture.md) — Where the worker fits in the broader code structure
