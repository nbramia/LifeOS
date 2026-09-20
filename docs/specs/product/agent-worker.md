# Agent Worker

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-20

LifeOS includes an external **agent worker** that picks up engine-assigned tasks and completes them autonomously — running locally on a self-hosted LLM or on Anthropic's Managed Agents cloud, with budget caps you can specify in the task title and full audit transcripts on every run. When the agent finishes (or gets stuck), it notifies you on Telegram. If it has a question mid-run, it asks via Telegram and waits for your reply.

The point is hands-free task completion for the long tail of small chores that aren't worth a conversation but are worth doing: "draft a follow-up to last week's intro thread," "summarize my unread emails from the partnership channel," "find every meeting where we discussed the Q3 launch."

---

## Table of Contents

1. [Quick example](#quick-example)
2. [Task conventions](#task-conventions)
3. [Projects and independently assigned children](#projects-and-independently-assigned-children)
4. [Routing — local vs cloud](#routing--local-vs-cloud)
5. [Budgets](#budgets)
6. [Tag lifecycle](#tag-lifecycle)
7. [Telegram interactions](#telegram-interactions)
8. [Capability boundaries](#capability-boundaries)
9. [Safety model](#safety-model)
10. [Configuration knobs](#configuration-knobs)
11. [Related Documents](#related-documents)

---

## Quick example

You add this line anywhere in your Obsidian Tasks file:

```
- [ ] TODO Summarize my unread emails from the partnership channel and reply with the top 3 by importance #local
```

Within a poll cycle (default 60s), the worker:

1. Runs a Haiku preflight to parse the task — budget, routing, expected output, sanity check
2. Atomically adds `#agent-running` (so two workers can't claim the same task)
3. Routes the task — to your local Gemma model, a CLI engine, your configured remote provider, or Claude on Managed Agents — from your tags or an explicit request; when it can only *infer* that a cloud connector is needed, it asks you first
4. Lets the agent execute: tool calls, MCP servers, web search, file I/O, the full kit
5. On completion: marks the task done in your vault, swaps the tag to `#agent-completed`, writes the full result to an Agent Output note (`LifeOS/Tasks/Agent Output/`), records the run's outcome (the same completion summary, plus a coding session's branch and any pull request it opened) for the board's Review card to show, and sends you a one-paragraph Telegram summary with the actual result (linking the note)

Cost for that task: usually under $0.10 on Claude Sonnet 4.6, free on local Gemma. The full transcript (every tool call, every model turn) lands in `data/agent_transcripts/<session_id>.jsonl` for later review.

---

## Task conventions

The agent worker claims todo (`[ ]`) or urgent (`[!]`) tasks that carry an engine assignee (`#claude` / `#codex` / `#hermes` / `#local` / `#cloud`), a Managed Agents consent tag (`#cloud-haiku` / `#cloud-sonnet`), or the legacy bare `#agent` queue marker (no engine tag required). An engine assignee alone is the handoff — a separate `#agent` tag is not required. Marking a task as urgent in Obsidian doesn't skip the worker; it just signals high priority within your queue. Other statuses (`in_progress`, `done`, `cancelled`, `deferred`, `blocked`) are left alone.

Routing / handoff tags:

| Tag | Effect |
|-----|--------|
| `#local` | Forces routing to your local LLM (Gemma by default). No API spend. Subject to local model capability. Board assignee for local. |
| `#cloud` | Forces routing to your configured remote OpenAI-compatible provider (e.g. DeepSeek via Fireworks) — never the Anthropic API. Real per-token billing at that provider's rates. Requires the provider configured ([configuration.md](../../guides/configuration.md#openai-compatible-remote-provider)); an unconfigured install parks the task at `#agent-blocked` rather than falling back to Anthropic. Board assignee for the remote provider. |
| `#cloud-haiku` | Forces routing to Claude Haiku on Anthropic Managed Agents. Required for tasks that need Anthropic's cloud connectors. Per-token API billing. |
| `#cloud-sonnet` | Forces routing to Claude Sonnet on Anthropic Managed Agents. Same connector access and billing as `#cloud-haiku`. |
| `#claude` | Forces routing to Claude Code CLI (the same surface as `/claude`). Billed against your Claude Pro subscription rather than per-token. Good for code/filesystem/browser work where the cloud connectors aren't needed. |
| `#codex` | Forces routing to Codex CLI (the same surface as `/codex`). Billed against your ChatGPT subscription. Same caveat as `#claude`. |
| `#hermes` | Forces routing to the Hermes backend the persona bots use. Opens a Hermes conversation seeded with the card's title and notes; the task's cost is whatever Hermes reports, not a per-token Anthropic charge. The card's open endpoint returns a `/chat?conversation=<id>` deep link for such a task rather than spawning a terminal session; the board drawer's **Open** button is currently shown for `#claude`/`#codex` cards only. |

Without an explicit routing tag, the preflight reads the title. "With local agent" / "using gemma" force local, and naming an engine, model, or "anthropic"/"api" ("use claude", "with opus", "use the anthropic api") routes there — you asked, so it dispatches. A bare "cloud" in the title does not count on its own ("cloud" means the remote provider, not Anthropic) — it falls through to the confirmation question below like any other guess.

**Inference alone never spends API credits.** Phrases like "draft an email", "check my calendar", "search my gmail" still tell the preflight the task probably needs cloud connectors, but that is a guess, so the task pauses at `#agent-blocked` and asks instead of dispatching. The same happens when the title gives no signal at all. The question offers `claude code` (subscription), `codex` (subscription), `local` (on-box Gemma), `cloud` (your configured remote provider — costs credits), or `anthropic`/a Claude model name like `opus` (Anthropic API — costs credits); reply with whichever you want. A bare "claude" in your reply means the Claude Code CLI, not the API — name a model or say "anthropic" to reach the API.

To skip the question entirely for a task you know needs the remote provider, tag it `#cloud`; for one that needs Anthropic's own cloud connectors, tag it `#cloud-haiku` or `#cloud-sonnet`.

Tag precedence (first match wins): `#local` → `#claude` → `#codex` → `#hermes` → `#cloud-haiku` → `#cloud-sonnet` → `#cloud`. The CLI routes (`#claude`, `#codex`) skip the cost-confirmation gate because they're subscription-billed, and so does `#hermes` (billed however Hermes bills, not a per-token Anthropic charge) and `#cloud` (the remote provider is priced but isn't the confirmation ceremony's Anthropic "expensive exception"); per-session dollar rollups still appear in `/agents` via the rollout ingest (the `cc:` and `cx:` session rows).

## Projects and independently assigned children

A project is an ordinary vault task that has at least one incoming child
reference. Each child is another ordinary task with `fields.parent_id` set to
the parent's stable task ID. Completed and cancelled children still make the
parent a project while their links remain. Removing the final link restores
ordinary-task presentation without changing the parent's ID, notes, history,
or independent execution pause.

Projects are never claimed or opened as ordinary worker tasks. Their assignee
is the owner, while every child keeps its own assignment and normal execution
and review lifecycle. Creating or attaching a child does not inherit the
parent's engine tags. When the worker starts a child, its bounded execution
context contains that child's instructions plus the parent ID, title,
objective/acceptance notes, and a compact sibling-status summary; unrelated
tasks are not copied into the prompt. When the optional Jev destructive gate
is enabled, its safety judgment receives the child title/instructions and the
bounded parent title/objective notes that the child will execute under. The
ordinary route, model, preset and cloud-consent decisions still see only the
child title, fields and tags; sibling state is not sent to Jev.

Child location selection keeps an explicit `working_dir` authoritative, then
uses a recognized `fields.project` repository-affinity mapping, a compatible
parent directory or affinity, and finally the ordinary title-based fallback.
Affinity strings are catalog names, never raw paths. API-host mappings and
inferred title paths are not copied to a different remote execution host; a
remote child without an explicit or same-host parent path starts in that
host's default directory. In-process routes accept inferred locations only
when the directory already exists on the API host; their unset fallback stays
in effect rather than freezing an uncloned path into the execution snapshot.

An agent-owned project's explicit **Plan and delegate** action starts an
operator-origin coordination session. That session is separate from task
hierarchy and from any historical task-backed session: it receives the current
child IDs, assignments, states and outcomes, may create durable children
through validated task tools, and may assign only within the operator's
delegated scope and provider consent. A stable operation ID makes retries
recover the same coordination request. Each intended child also carries a
stable `operation_key` derived from that request and the child's role, so a
retried create recovers the existing task instead of duplicating child work.
The coordinator finishing or failing does not finish the project. An explicit
coordinator `working_dir` remains authoritative. Its optional project-affinity
fallback selects only an existing local directory on the API host; remote CLI
coordinators start in the remote host's default directory unless they have an
explicit path.

Project completion is explicit. All children must be done or cancelled, no
review may remain unaccepted, no coordinator may be live, and no cancellation
may be pending. Cancelled children require acknowledgement of reduced scope;
they are never counted as successful completion. Cancelling a project is also
explicit and two-step: the preview names unfinished, running and
awaiting-review work, then a confirmed operation stops controllable sessions,
cancels unfinished children, and records review output as abandoned rather
than accepted. A failed or unverifiable stop leaves cancellation pending and
reports the remaining session so the same operation can be retried. Cancelling
one child never cancels siblings or its parent.

An ordinary top-level task currently owned by an executor can be converted into
a project with `lifeos_agent_project_handoff`. This is a terminal action for
that exact executor turn, not ordinary child attachment: the caller submits a
stable operation ID and 1–20 uniquely keyed child requests, then stops. The
worker records the source turn's stop before activating the staged children and
bounded coordinator. A stop it cannot verify leaves the handoff pending; it
does not mark the original task complete or release any staged work. Existing
projects use **Plan and delegate**, not this conversion. Session-agent
delegation (`lifeos_agent_spawn`) remains separate from durable project
children. A child never becomes a project, and a coordinator is one bounded
run rather than an always-on monitor.

A pending handoff remains fenced through worker recovery. The source stays
paused and staged work stays blocked until its matching source turn is known
to have stopped; recovery never treats a missing or unverifiable stop as a
successful handoff. An operator can use the existing project cancellation flow
while a handoff is pending, but cancellation remains pending until its scoped
stop and teardown are verified. A staged intent with no children is still an
ordinary task, not a project, and is shown as a pending handoff rather than as
successful project work.

---

## Routing — local vs cloud

| | Local (Gemma) | Cloud (Claude) |
|---|---|---|
| **Best for** | Tasks against local files + LifeOS MCP. Privacy-sensitive work. | Tasks that touch Gmail / Calendar / Drive / Slack / Asana / etc. via cloud connectors. |
| **Speed** | ~50 tok/s on a workstation GPU; first-token latency dominated by load | ~70+ tok/s sustained, but session-create round-trip + container provisioning |
| **Cost** | Effectively free (electricity) | Sonnet 4.6: ~$3 / 1M input tokens, $15 / 1M output. Plus $0.08/hour session-hour overhead. |
| **Tools available** | Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, sleep + LifeOS MCP + inter-agent tools | Bash, Read, Write, Edit, Glob, Grep, web_search, web_fetch + LifeOS MCP + all your Vault-connected MCPs (cloud productivity, work tools) |
| **Filesystem reach** | Operator's actual machine — agent can touch your real files | Anthropic-managed ephemeral container |
| **Failure modes** | Model capability limits, GPU OOM | Per-task billing, MCP init failures, Anthropic rate limits |

---

## Budgets

You can put a budget in the task title. The preflight parses natural-language hints:

| Title fragment | Parsed as |
|---|---|
| `5 min` / `30s` / `1h` | `wall_seconds` |
| `max $0.50` / `budget $1.00` | `max_dollars` |
| `10k tokens` / `50000 tokens` | `max_tokens` |

If no budget appears in the title, defaults from `.env` apply: `$10.00` and `~4 hours wall`, and no token cap — the token cap only applies when a title names one explicitly (e.g. `50k tokens`). These are backstops sized so an ordinary task never approaches them, not quotas.

On an in-process route (local, the remote-forced route, or Managed Agents), a breach of the wall-clock, token, or dollar cap — or of the lineage-aggregate dollar cap a session with descendants shares with its family — **pauses and asks rather than failing the task**. The session yields (its conversation stays exactly as it was, nothing is lost), the card moves to the Human queue lane, and you get a question on Telegram (or Hermes) naming what was spent (dollars to two decimals and active minutes) and the cap:

- Reply **`yes`** to double the cap and resume from right where the session left off.
- Reply **`yes $12`** (or **`yes 90 min`** for a wall-clock breach) to set the cap to a specific value instead of doubling it.
- Reply **`stop`** to end the task at `#agent-budget-exceeded`.
- No reply at all leaves it parked indefinitely, at zero further cost — nothing re-dispatches it until you answer.

The board drawer offers **Continue** and **Stop** buttons alongside the free-text Answer box for exactly this question, so a reply doesn't require typing.

The **dollar cap is a real backstop only on the cloud Claude (Managed Agents / API) route and the remote-forced route**, the two with marginal per-task cost; on the local (free) and Claude Code / Codex CLI (subscription) routes a `max $…` hint is recorded but never breaches anything, and those two CLI routes carry no wall/token/dollar enforcement at all — no pause-and-ask either.

There's also a global daily $-cap (`LIFEOS_AGENT_DAILY_CAP_DOLLARS`, default `$100`). When the day's accumulated cost first crosses the cap, the worker stops claiming new tasks and sends one Telegram notice naming today's spend and the cap. Tasks already running aren't killed. Reply **`raise to $150`** to raise today's cap and resume claiming immediately — the raise applies to today only and the cap reverts to the configured default the next local day. Crossing a since-raised cap later the same day sends one more notice; repeatedly hitting the same cap value doesn't nag again.

A [scheduler](../../guides/scheduler.md) entry whose action hands work to the agent worker can carry its own budget (`[budget:: …]` / `[wall:: …]`), rendered into the created task's title in this same hint grammar on every fire.

---

## Tag lifecycle

An engine-assigned task transitions through these states:

```
#local / #claude / …      (engine assignee — the claim handoff)
   ↓ claimed
#agent-running            (worker has picked it up; assignee tag remains)
   ↓ terminal
#agent-completed          (success — task is also marked `done`)
   or
#agent-failed             (executor crashed / preflight rejected / runtime error)
   or
#agent-budget-exceeded    (hit a token / wall / dollar cap)
   or
#agent-blocked            (waiting on you via Telegram, or required setup is missing)
```

To re-run a terminal task, clear the terminal lifecycle tag and keep (or restore) an engine assignee so the worker can claim it again. The full prior transcript stays in `data/agent_transcripts/`.

---

## Telegram interactions

The agent worker uses your existing Telegram bot (no second bot needed). Three message types:

1. **Completion notifications** — one paragraph summarizing what the agent did, the key result, total tokens + cost + active seconds. If a tool failed mid-run, the summary includes a footer listing the affected MCPs.

2. **Clarification requests** — if the preflight can't determine routing OR if a task title is genuinely ambiguous (e.g., "reply to Alex" with no email reference), the worker pauses the task at `#agent-blocked` and asks one targeted question on Telegram. Reply by using Telegram's native reply feature (long-press the bot's message, hit Reply). The worker picks up your answer within the next poll cycle and resumes. A budget breach (see Budgets, above) parks the task at the same `#agent-blocked` tag and asks the same way — `yes` / `yes $12` / `yes 90 min` resumes it, `stop` ends it.

3. **Failure notifications** — short message naming the task and the failure reason, plus a transcript path so you can debug. Examples: "task X failed: managed_create_session 4xx" or "task Y hit its budget (max_dollars)" (the budget example is seen only after you reply `stop` to a budget question — a breach alone doesn't produce this notification).

**Replying to a thread.** Every terminal notification — completion, failure, or budget cut-off — is replyable: use Telegram's native reply on it (any chunk of a long message) and the agent reopens that thread as a follow-up turn with full prior context ("actually, also CC Jane"). The reply gesture is the *only* way to continue a thread on Telegram — a plain message is always a normal chat query, so unrelated questions are never mistaken for a thread continuation.

The immediate acknowledgment only ever confirms your note is queued — it never claims the session has already resumed, because a `/claude`/`/codex` session's actual resume happens on the worker's next poll cycle, not synchronously with your reply. A second, separate message confirms once that resumed run actually starts. If a resume doesn't start within a few minutes, a one-time alert names the stuck task/session rather than leaving it silently orphaned — check `#agent-running` on the card and, if it's still not moving, re-trigger it manually.

Every session message begins with the card's short title. After the session's first message, progress updates, questions, completion, failure, and budget notices appear as Telegram replies to that first message, keeping concurrent sessions visibly attributable. A completed Claude Code run sends one coherent final result; an unfinished trailing aside does not replace a summary that was already reported.

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

The agent runs with the operator's full filesystem and shell access — no sandbox. This is intentional and consistent with the rest of LifeOS (you trust it with your data); see the [Design Principles](../../../AGENTS.md#development-principles) section in the project AGENTS doc. Overlapping protections keep things sane:

1. **Preflight safety checks** — deterministic destructive-title checks fail closed. When explicitly configured, Jev additionally scores irreversible harm in `shadow` mode or parks threshold-crossing tasks for confirmation in `block` mode; project children are judged against their bounded child-plus-parent execution instructions, without allowing parent text to select an engine or grant cloud consent.
2. **Daily $-cap** — backstop against runaway loops; pauses all new claims when crossed.
3. **Per-task budgets** — enforced from outside the agent loop, so the model can't override them.
4. **Telegram notification on every terminal state** — you find out quickly if something runs that shouldn't have.
5. **Isolated worktree for coding sessions** — a Claude Code or Codex task that touches a git repository always runs in its own worktree on a fresh branch, off the current `main`, never in your primary checkout — the same working tree the production server runs from — even when the task is pinned to a remote host, where the worktree lives (and gets pushed/opened as a PR) on that host, not silently skipped. The session is told this and expected to commit its work; its completion summary becomes the public pull request description, so it's told that must carry no personal data — the worker also scrubs anything shaped like a bot token, API key, or other credential from it before publishing, as a backstop. When it reports the task fully done, the worker pushes the branch and opens a pull request for you — the completion notification carries its PR link, or says plainly if the push/PR failed or there was nothing to push. When it pauses to ask you something first — Claude Code's own question convention, or Codex's `[CLARIFY]` marker — the worker pushes what's committed so far (no pull request yet), the branch name rides along with the question, and the card moves to your Human queue until you answer. If the session itself leaves anything uncommitted when it stops for any reason, the worker commits and pushes that too, rather than letting it sit lost in a directory you'll never look at.
6. **Private, short-lived session files** — every board session receives its own private temporary directory instead of sharing a general-purpose temp location with other processes. Files created there, including copied authentication material, are removed when the session completes, fails, exceeds its budget, is killed or cancelled, or times out waiting for clarification. Coding worktrees are removed after their pull request merges or their card is accepted/cancelled. Cleanup commits and pushes any leftover work before removal, never removes the primary checkout or an operator-created worktree, and leaves remote branch deletion to the git host's policy.

Operators should still audit handed-off tasks before they reach the worker (your task list is the queue), keep budgets set, and treat agent-touchable secrets the same as operator-touchable secrets.

---

## Configuration knobs

All in `.env` — see [`agent-worker-setup.md`](../../guides/agent-worker-setup.md) for the full operator walkthrough. Most-used:

| Var | Purpose | Default |
|---|---|---|
| `LIFEOS_AGENT_WORKER_AUTOSTART` | Enable the worker on boot | `false` |
| `LIFEOS_AGENT_DAILY_CAP_DOLLARS` | Global daily $-cap (set to 0 to pause new claims) | `100.00` |
| `LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS` | Default per-task $-cap when title doesn't specify | `10.00` |
| `LIFEOS_AGENT_WORKER_POLL_SECONDS` | Polling interval | `60` |
| `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` | Telegram-clarification wait before abandoning | `72` |
| `LIFEOS_AGENT_STUCK_SESSION_TIMEOUT_MINUTES` | How long a reopened `/claude`/`/codex` session may sit unresumed before the stuck-session alert fires | `15` |
| `LIFEOS_AGENT_MANAGED_MODEL` | Informational; actual model lives in the cloud preset | `claude-sonnet-5` |

---

## Related Documents

- [ADR-008: Managed Agents Cloud Routing](../../adr/008-managed-agents-cloud-routing.md) — Why local + cloud, how routing is decided, cost model
- [ADR-026: A Budget Breach Asks; It Does Not Fail](../../adr/026-budget-breach-asks.md) — Why a budget breach pauses and asks instead of ending the task
- [Agent Worker — Technical](../technical/agent-worker.md) — Architecture, executors, prompts, state machine, restart resumability
- [Agent Worker — Setup](../../guides/agent-worker-setup.md) — Operator setup (Gemma swap, MCP HTTP transport, Vault provisioning, agent preset)
- [Claude Code Orchestration (product)](claude-code-orchestration.md) — The other autonomous-work system in LifeOS; triggered from Telegram `/claude` rather than `#agent` tags
- [Agent Viz](agent-viz.md) — Live `/agents` page showing in-flight and recently-finished worker sessions
- [Task Management](task-management.md) — How `#agent` tasks live alongside regular tasks in the Obsidian Tasks plugin
- [Scheduler Guide](../../guides/scheduler.md) — A schedule's `agent` action writes the `#agent` tasks this worker runs
- [MCP Tools](mcp-tools.md) — The `lifeos_agent_*` family for inter-agent coordination
- [API Reference](api-reference.md) — `POST /api/tasks/{id}/swap-tag` and other task endpoints the worker uses
- [Architecture](../technical/architecture.md) — Where the worker fits in the broader code structure
