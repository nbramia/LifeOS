# Doctor Bot — Self-Repair Surface

**Status:** Complete
**Last Updated:** 2026-06-26
**Audience:** Operator

The **doctor** bot's only job is fixing LifeOS itself. You message it when you notice LifeOS misbehaving or missing something; it converses with you to define the **goal**, locks that goal, then — on your approval — orchestrates the work end to end and reports back. It is a **goal-first orchestrator**: it supervises the implementation (subagents do the coding via `/implement`) rather than hand-coding inline, and every change lands through a reviewed, tested PR — never a direct push to `main`.

Unlike the `fitness` and `therapist` bots (pure chat surfaces), the doctor **orchestrates**: each message drives a real Claude Code session, and that session's progress, questions, and completion all come back to you.

## Surfaces

- **Telegram** (primary): a dedicated `@BotFather` bot. Full round-trip — reply to a `[CLARIFY]`/`[GOAL]` message (swipe-to-reply) to answer; a fresh, non-threaded message starts a **new** repair.
- **Web / voice**: selecting the `doctor` persona in web chat (or via whisper-relay) spawns the same orchestrating session. You can answer its `[CLARIFY]`/`[GOAL]` from the web conversation too (`POST /api/conversations/{id}/answer`); notices also surface on the doctor's Telegram thread and the `/agents` page.

## The flow

This is the operator's-eye summary; the exact contract the session runs lives in [`config/personas/doctor.md`](../../config/personas/doctor.md) — treat that as authoritative if the two ever drift.

1. **You report a problem**, e.g. _"the calendar tool returns last week's events when I ask for this week."_
2. **Clarify the goal.** The doctor investigates the code and asks the minimum `[CLARIFY]` questions needed to pin down what "done" means. No work starts yet.
3. **Propose + approve the goal.** It emits a `[GOAL]` — a crisp success condition (issue filed, tested PR merged to `main`, restarted, confirmed). **This is the single human gate.** Reply **yes** to lock it (which arms the session's `/goal`), or send changes to refine it.
4. **Execute.** On approval it files the issue(s) via `/draft-issue`, then ships — adaptive to size:
   - **Small goal:** one branch → `/implement` → one PR → `main`.
   - **Multi-part goal:** an **integration branch** off `main`, each part landed as a sub-PR onto it (`/implement <issue> --base <integration-branch>`), then one PR integration→`main`, then cleanup.
5. **Confirm.** It brings the canonical checkout to merged `main`, restarts (the right way — see Autonomy and safety), and confirms with the revert handle: _"Shipped: PR #N merged, server restarted. Revert with: gh pr revert N."_

## Setup

1. **Create the bot.** In Telegram, message `@BotFather` → `/newbot`, name it (e.g. "LifeOS Doctor"), and copy the token.
2. **Set the token** in `.env`:
   ```bash
   TELEGRAM_DOCTOR_BOT_TOKEN=<token-from-botfather>
   # TELEGRAM_DOCTOR_CHAT_ID=   # optional; defaults to TELEGRAM_CHAT_ID
   ```
   The bot is registered in [`config/telegram_bots.json`](../../config/telegram_bots.json) and its behavior lives in [`config/personas/doctor.md`](../../config/personas/doctor.md). Leave the token unset to not run it.
3. **Prerequisites on the server:**
   - **Claude Code** installed and authenticated (`claude setup-token`) — same requirement as `/claude`. See [Claude Code Orchestration](claude-code-orchestration.md).
   - **`gh`** authenticated (`gh auth login`) so it can file issues and open/merge PRs.
   - The **agent worker** running (`lifeos-agent-worker`) — it runs the doctor's Claude Code sessions.
4. **Restart** to pick up the new bot: `./scripts/server.sh restart`.

## Autonomy and safety

- **One gate.** Full-auto from your goal approval through merge; `/implement`'s built-in adversarial review is the quality bar.
- **Always PR-gated, always revertable.** Even the smallest fix goes branch → PR → `main` with tests + docs + review; the doctor never pushes to `main`, and each change lands as a single revertable merge commit whose `gh pr revert` handle it reports.
- **Escalation respected.** If `/implement` can't resolve its review findings after its rounds, the doctor leaves the PR open and reports the escalation instead of merging.
- **Isolation.** Implementation happens in throwaway worktrees off `origin/main` (pre-flight-cleaned via `scripts/cleanup-worktrees.sh` so a prior crashed run can't block the `add`), so the canonical checkout other agents share is never left on a feature branch.
- **Pick-up after merge.** The doctor pulls merged `main` into the canonical checkout, then runs `./scripts/server.sh verify-deployed` to confirm the checkout is a real work tree on the merged commit **before** restarting — so a silently-failed pull (e.g. a bare/misconfigured checkout) is reported as a deploy failure instead of a false "Shipped" (#419). Only on success does it restart, so the running server actually reflects the change.
- **Safe self-restart.** The doctor runs *inside* `lifeos-agent-worker`. It uses `./scripts/server.sh classify-change` to tell an API-only change (plain `lifeos-api` restart; its session survives) from an agent-worker change (which would kill it mid-run). For the latter it uses `./scripts/server.sh restart-worker-detached`, which flushes the final notice and marks the restart deliberate **before** bouncing the worker in a detached process — so the "Shipped" notice always lands and the run isn't surfaced as a spurious failure.

## Adding another orchestration bot

The doctor's machinery is generic. To add another orchestration surface, add an entry to [`config/telegram_bots.json`](../../config/telegram_bots.json) with `"orchestrates": true`, write its contract in `config/personas/<name>.md`, and set its `*_BOT_TOKEN`. A pure-chat specialized bot is the same minus `orchestrates` (it routes to the chat pipeline with its persona).

## Related Documents

### Operational
- [Configuration](configuration.md) — `TELEGRAM_DOCTOR_*` and the specialized-bot env vars.
- [Claude Code Orchestration](claude-code-orchestration.md) — Claude Code install/auth on the server, shared by the doctor.
- [Agent Worker Setup](agent-worker-setup.md) — The worker that runs the doctor's sessions.

### Code References
- [`config/telegram_bots.json`](../../config/telegram_bots.json) — Bot registry (the `doctor` entry, `orchestrates: true`).
- [`config/personas/doctor.md`](../../config/personas/doctor.md) — **The authoritative orchestration contract** the session runs; this guide is its operator-facing summary.
- [`api/services/telegram.py`](../../api/services/telegram.py) — Listener routing + bot-scoped reply hooks.
- [`api/services/agent_worker/worker.py`](../../api/services/agent_worker/worker.py) — Bot-bound notification routing for Claude Code sessions.
