# Telegram Setup

**Status:** Complete
**Last Updated:** 2026-07-09
**Audience:** Operators

The Telegram surface is a first-class client for LifeOS. It gives you:

- **Conversational access** to LifeOS from your phone — every message runs through the same orchestrator and tool catalog as the web `/chat` SPA (search, calendar, email, tasks, people, finances, and the rest).
- **Proactive notifications and briefings** — scheduled reminders and nightly summaries are delivered to your chat.
- **Alerting** — operational alerts (sync failures, auto-deploy failures) can land in Telegram as a backup to email.
- **Delegating autonomous work** — spawn `#agent` tasks and Claude Code / Codex sessions from the chat and get progress and completion notices back in the same thread.

This guide sets up the primary bot, explains its commands and reply threading, and points to the specialized persona bots.

## Create the primary bot

1. In Telegram, open a chat with [`@BotFather`](https://t.me/BotFather) and send `/newbot`.
2. Follow the prompts to name your bot (e.g. "LifeOS") and pick a username ending in `bot`.
3. BotFather replies with an HTTP API **token** that looks like `123456:ABC-fake`. Copy it.
4. Set it in `.env`:

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC-fake
   ```

## Find your chat ID

The bot only answers the one chat whose ID matches `TELEGRAM_CHAT_ID` — any other chat is ignored. To find yours:

1. Open a chat with your new bot in Telegram and send it any message (e.g. `hi`).
2. Fetch pending updates from the Bot API (substitute your token):

   ```bash
   curl "https://api.telegram.org/bot123456:ABC-fake/getUpdates" | jq '.result[].message.chat.id'
   ```

3. Read the numeric `chat.id` from the response and set it in `.env`:

   ```bash
   TELEGRAM_CHAT_ID=555000123
   ```

4. Restart the server so the listener picks up the new credentials:

   ```bash
   ./scripts/server.sh restart
   ```

Once both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the listener starts polling and the bot answers your messages. Send `/help` to confirm it is alive.

## Primary-bot commands

Any plain-text message runs through the chat pipeline. Slash commands are handled directly:

| Command | What it does |
|---|---|
| `/new` or `/clear` | Start a fresh conversation (drops the current thread's context). |
| `/status` | Report LifeOS server health (queries the local `/health` endpoint). |
| `/inspect` | Show the sources checked, tool actions taken, and timing for your last response. |
| `/help` | List the available commands. |
| `/agent [local|claude] <task>` | Spawn an autonomous agent for a task. With no model word it auto-routes (and asks if ambiguous); `local` forces the local Gemma model, `claude` forces cloud Claude. |
| `/claude <task>` | Run a task with a Claude Code session on the server. |
| `/claude_status` | Show active Claude Code sessions (aliased `/claudestatus`). |
| `/claude_cancel` | Cancel active Claude Code sessions (aliased `/claudecancel`). |
| `/codex <task>` | Run a task with a Codex CLI session on the server. |
| `/codex_status` | Show active Codex sessions (aliased `/codexstatus`). |
| `/codex_cancel` | Cancel active Codex sessions (aliased `/codexcancel`). |

The `/agent`, `/claude`, and `/codex` families (and their `_status` / `_cancel` variants) run on the **primary** bot only — their completion notices and clarification questions route back through the primary listener. A specialized bot that receives one of these commands redirects you to the primary bot.

An unrecognized `/command` falls through to the chat pipeline as an ordinary message.

## Quoted-reply threading

Replying to one of the bot's own messages (swipe-to-reply in the Telegram UI) changes how the next message is interpreted:

- **Reply to an ordinary bot message** (e.g. a nightly priorities summary or a "Logged: …" line) and the quoted text is passed to the orchestrator as context. This resolves deictic follow-ups like _"what about that?"_ or _"expand the second one"_ without re-stating what you mean. The quoted text is capped so a long summary is preserved but not unbounded.
- **Reply to an agent or Claude Code / Codex notification** (a clarification question or a completion notice) and your message continues that **specific** session — answering its question or sending a follow-up instruction — rather than starting a new chat.

A plain, non-threaded message is always a fresh chat query. It is never silently folded into a finished agent thread, so unrelated questions do not get swallowed.

## Specialized persona bots

Beyond the primary bot you can run **persona bots** — separate `@BotFather` bots that route to the same orchestrator with a domain-specific system-prompt preamble. Each is registered in [`config/telegram_bots.json`](../../config/telegram_bots.json) (mapping a `name` to a `persona_file`) and needs its own token and, optionally, its own chat ID:

| Bot | Env vars | Kind |
|---|---|---|
| `fitness` | `TELEGRAM_FITNESS_BOT_TOKEN` / `TELEGRAM_FITNESS_CHAT_ID` | Pure chat — training and nutrition surface. |
| `therapist` | `TELEGRAM_THERAPIST_BOT_TOKEN` / `TELEGRAM_THERAPIST_CHAT_ID` | Pure chat — advice-oriented surface. |
| `journal` | `TELEGRAM_JOURNAL_BOT_TOKEN` / `TELEGRAM_JOURNAL_CHAT_ID` | Pure chat — disjointed-fragment capture into `Personal/Log/`. |
| `doctor` | `TELEGRAM_DOCTOR_BOT_TOKEN` / `TELEGRAM_DOCTOR_CHAT_ID` | Orchestration — self-repair surface that files an issue and ships a fix. |

To add one:

1. Create a dedicated bot with `@BotFather` (as above) and copy its token.
2. Set `TELEGRAM_<NAME>_BOT_TOKEN` in `.env`. The `TELEGRAM_<NAME>_CHAT_ID` is optional and defaults to `TELEGRAM_CHAT_ID`.

   ```bash
   TELEGRAM_FITNESS_BOT_TOKEN=123456:ABC-fake
   # TELEGRAM_FITNESS_CHAT_ID=   # optional; defaults to TELEGRAM_CHAT_ID
   ```

3. Restart the server: `./scripts/server.sh restart`.

Leave a bot's token unset to not run it — a fresh clone with no extra tokens runs just the primary bot. The `fitness` and `therapist` bots are pure chat surfaces; the `doctor` bot **orchestrates** (each message drives a real Claude Code repair session). The persona layer is covered in [personas.md](personas.md), and the doctor's flow in [doctor-bot.md](doctor-bot.md).

## Alerting

LifeOS routes operational alerts (sync failures, auto-deploy failures) to email via `LIFEOS_ALERT_EMAIL`, with Telegram as a backup destination when `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set. Configure the primary bot as above and the alerts arrive in your chat. See [configuration.md](configuration.md) for the env vars and [operations.md](operations.md) for alerting severities.

## Troubleshooting

**Bot does not respond.**
Confirm `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are both set in `.env`, then restart with `./scripts/server.sh restart` — the listener only starts when both are present. Re-check the token against `@BotFather` (a revoked/regenerated token stops working) and verify the server is up with `/status`.

**`409 Conflict` in the logs, or messages arrive intermittently.**
A `409` from `getUpdates` means two pollers are long-polling the same bot token. Make sure only one LifeOS instance is running (a stray `./scripts/server.sh` process, or the same token reused by another service). Stop the duplicate; the surviving listener recovers on its next poll.

**Bot ignores your messages.**
The listener only answers the chat whose ID equals the configured `*_CHAT_ID` — every other chat is dropped with an "unauthorized chat" log line. Confirm you are messaging from the right account and that `TELEGRAM_CHAT_ID` matches the `chat.id` from `getUpdates`. For a persona bot, check its `TELEGRAM_<NAME>_CHAT_ID` (or that `TELEGRAM_CHAT_ID` is the fallback you expect).

## Related Documents

### Operational
- [README](../../README.md) — Architecture overview and client surfaces.
- [Configuration](configuration.md) — `TELEGRAM_*` env var reference and defaults.
- [Personas](personas.md) — The persona layer shared by web chat and the specialized bots.
- [Doctor Bot](doctor-bot.md) — The self-repair orchestration bot and its flow.
- [Operations](operations.md) — Alerting severities and operational procedures.

### Specifications
- [Agent Worker](../specs/product/agent-worker.md) — What `#agent` tasks do and how they run.

### Code References
- [`api/services/telegram.py`](../../api/services/telegram.py) — Listener, command handlers, and reply-threading logic.
- [`config/telegram_bots.json`](../../config/telegram_bots.json) — Specialized-bot registry (name → persona file).
