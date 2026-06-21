# Doctor Bot — Self-Repair Surface

**Status:** Complete
**Last Updated:** 2026-06-21
**Audience:** Operator

The **doctor** bot is a dedicated `@BotFather` Telegram bot whose only job is fixing LifeOS itself. You message it when you notice LifeOS misbehaving or missing something; it clarifies what you mean, files a GitHub issue, asks whether to implement, and — on your go-ahead — ships the fix end to end and reports back.

Unlike the `fitness` and `therapist` bots (pure chat surfaces), the doctor **orchestrates**: each message drives a real Claude Code session, and that session's progress, questions, and completion all come back on the doctor bot.

## The flow

1. **You report a problem** (a fresh message to the doctor bot), e.g. _"the calendar tool returns last week's events when I ask for this week."_
2. **Clarify.** The doctor asks questions only if intent is unclear; otherwise it proceeds.
3. **Issue.** It runs `/draft-issue` and replies with a clickable GitHub issue link.
4. **Gate.** It asks _"Implement this now?"_ — reply **yes** to ship, **no** to leave it as an issue. This is the single human gate.
5. **Ship.** On **yes** it creates a worktree off `origin/main`, runs `/implement` (plan → tests → PR → adversarial review → merge), brings the canonical checkout to merged `main`, restarts the server, and confirms: _"Shipped: PR #N merged, server restarted."_

Continue any step by **replying to the doctor's message** (swipe-to-reply in Telegram). A fresh, non-threaded message starts a **new** repair.

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

- **One gate.** Full-auto from your "yes" through merge; `/implement`'s built-in adversarial review is the quality bar.
- **Escalation respected.** If `/implement` can't resolve its review findings after 3 rounds, the doctor leaves the PR open and reports the escalation instead of merging.
- **Isolation.** Implementation happens in a throwaway worktree off `origin/main`, so the canonical checkout other agents share is never left on a feature branch.
- **Pick-up after merge.** The doctor pulls merged `main` into the canonical checkout before restarting, so the running server actually reflects the change.

## Adding another orchestration bot

The doctor's machinery is generic. To add another orchestration surface, add an entry to [`config/telegram_bots.json`](../../config/telegram_bots.json) with `"orchestrates": true`, write its contract in `config/personas/<name>.md`, and set its `*_BOT_TOKEN`. A pure-chat specialized bot is the same minus `orchestrates` (it routes to the chat pipeline with its persona).

## Related Documents

### Operational
- [Configuration](configuration.md) — `TELEGRAM_DOCTOR_*` and the specialized-bot env vars.
- [Claude Code Orchestration](claude-code-orchestration.md) — Claude Code install/auth on the server, shared by the doctor.
- [Agent Worker Setup](agent-worker-setup.md) — The worker that runs the doctor's sessions.

### Code References
- [`config/telegram_bots.json`](../../config/telegram_bots.json) — Bot registry (the `doctor` entry, `orchestrates: true`).
- [`config/personas/doctor.md`](../../config/personas/doctor.md) — The orchestration contract the session runs.
- [`api/services/telegram.py`](../../api/services/telegram.py) — Listener routing + bot-scoped reply hooks.
- [`api/services/agent_worker/worker.py`](../../api/services/agent_worker/worker.py) — Bot-bound notification routing for Claude Code sessions.
