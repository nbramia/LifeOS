# Scheduler Guide

> **Status:** Complete
> **Owner:** Scheduler
> **Last Updated:** 2026-08-17
> **Audience:** Operators

The Scheduler runs work on a timer. A **schedule** binds a **trigger** (one-off
or recurring) to an **action** (notify, prompt, endpoint, or hand off to the
agent worker). Schedules are Obsidian-native: the markdown is the source of
truth, editable in your vault, and reindexed automatically when you change it.

> The Scheduler replaces the older "reminders" system. The old `/api/reminders`
> endpoints, `lifeos_reminder_*` MCP tools, and `manage_reminders` chat tool
> still work as deprecated aliases over the same store.

## Storage Format

Schedules live in `LifeOS/Scheduler/Inbox.md` as checkbox lines with stable
`<!-- id:xxxx -->` IDs and Dataview-style `[key:: value]` inline fields — the
same pattern as [Tasks](../specs/product/task-management.md). Markdown is the
source of truth; `data/scheduler_index.json` is a rebuildable cache.

```markdown
- [ ] Morning Briefing [cron:: 0 9 * * *] [tz:: America/New_York] [action:: prompt] [mtype:: prompt] <!-- id:a1b2c3 -->
- [ ] Weekly Review [cron:: 0 9 * * 6] [action:: agent] [mtype:: prompt] #cloud <!-- id:d4e5f6 -->
- [x] Pay rent [at:: 2026-06-01T09:00:00] [action:: notify] [mtype:: static] <!-- id:0a9b8c -->
```

- **Checkbox** — `[ ]` enabled, `[x]` disabled (toggle it in Obsidian to pause).
- **Trigger** — `[cron:: <expr>]` for recurring, `[at:: <ISO datetime>]` for one-off.
- **`[tz:: <IANA zone>]`** — interprets the trigger in that timezone (defaults to the configured timezone).
- **`[action:: …]`** — what fires (see below).
- **`#executor` tag** — for `action:: agent`, the executor: `#local`, `#cloud`, `#cloud-haiku`, or `#cloud-sonnet`.
- **`[bot:: <name>]`** — which Telegram bot delivers the notification (see below); omitted means the primary bot.

Editing a line in Obsidian (changing the cron, toggling the checkbox) is picked
up within ~2s by the file watcher. Markdown edits are **not** validated — a
`[bot:: <name>]` typed here is accepted as-is, and the fire-time routing warning
below is the only safety net.

## Triggers

| Type | Field | Example | Fires |
|------|-------|---------|-------|
| Recurring | `[cron:: …]` | `0 9 * * 1-5` | Every weekday at 9am |
| One-off | `[at:: …]` | `2026-06-03T15:05:00` | Once, then auto-disables |

Cron expressions are interpreted in the schedule's timezone and converted to UTC
internally, so "daily at 6pm" means 6pm local.

## Actions

| Action | What happens |
|--------|--------------|
| `notify` | Sends the static `message_content` via Telegram |
| `prompt` | Runs `message_content` through the full chat pipeline (with retry) and sends the result |
| `endpoint` | Calls a LifeOS API endpoint and sends the formatted result |
| `agent` | Writes an `#agent` task into `LifeOS/Tasks/Inbox.md` so the agent worker runs it autonomously |

For `notify` and `prompt`, the Telegram message is **suppressed** when the
result is empty or a sentinel (`NO_MEETING`, `NOTHING_TO_REPORT`, …) — so
high-frequency schedules like pre-meeting prep stay quiet when there's nothing
to say.

### Notification bot

Any schedule except `action:: agent` can name the Telegram bot that delivers it,
so finance, health, or therapy content lands in its own channel instead of the
general feed. The valid names are `primary` plus whatever is *configured* —
`config/telegram_bots.json` is the registry, but an entry there counts only once
the env var named by its `token_env` is set, so a listed bot with no token is not
an accepted name. Both `POST /api/scheduler` and `PUT /api/scheduler/{id}` reject
any other name with a 422 that lists the accepted ones. Leaving the field unset
means the primary bot, which is what an installation with no specialized bots
configured gets.

If a stored schedule names a bot the registry no longer has — usually because
the bot was renamed after the schedule was written — the notification is still
delivered from the primary bot rather than dropped, but the message carries a
routing warning naming the unresolvable bot so the misroute is visible in the
channel it lands in.

### Agent hand-off

An `action:: agent` schedule spawns autonomous work without any new executor:
when it fires it writes an `#agent` task carrying the prompt as the description
and the executor tag (`#local` / `#cloud` / `#cloud-haiku` / `#cloud-sonnet`).
The existing [agent worker](../specs/product/agent-worker.md) discovers the
task and routes it via preflight. Progress is reported through the worker's own
channel, not the scheduler.

For **cron** (recurring) schedules the hand-off also stamps a `#sched-<id>` tag
on the task. The worker reads it on completion and appends every fire's output
to **one shared Agent Output note per schedule** (`LifeOS/Tasks/Agent Output/<schedule-slug>-<id>.md`),
newest run on top under a dated heading — rather than a new note per fire. A
one-time (`once`) agent schedule gets no such tag and produces its own one-off
note like any other `#agent` task.

```markdown
- [ ] Weekly Review [cron:: 0 9 * * 6] [action:: agent] #cloud <!-- id:d4e5f6 -->
```

## Creating Schedules

### Via Chat or Telegram

Just ask naturally — the orchestrator calls `manage_schedules`:

```
"every weekday at 9am, brief me on my calendar"
"every Saturday at 9am, have the cloud agent draft my weekly review"
"remind me to pay rent on the 1st at 9am"
```

### Via API

```bash
curl -X POST http://localhost:8000/api/scheduler \
  -H "Content-Type: application/json" \
  -d '{"name": "Weekly Review", "schedule_type": "cron", "schedule_value": "0 9 * * 6",
       "action": "agent", "executor": "cloud", "message_content": "Draft my weekly review"}'
```

### Via MCP Tools

`lifeos_schedule_create` / `lifeos_schedule_list` / `lifeos_schedule_update` /
`lifeos_schedule_delete` — each accepts the `action` and (for agent schedules)
`executor` parameters.

## Managing Schedules

- **List:** `GET /api/scheduler`, `lifeos_schedule_list`, or "list my schedules"
- **Update:** `PUT /api/scheduler/{id}`, or edit the line in Obsidian
- **Delete:** `DELETE /api/scheduler/{id}`, or "delete the … schedule"
- **Test-fire:** `POST /api/scheduler/{id}/trigger`
- **Pause all:** set `enabled: false` in `LifeOS/Scheduler/Scheduler.md`

## Obsidian Dashboard

An auto-generated, read-only dashboard tracks every schedule.

**Location:** `LifeOS/Scheduler/Dashboard.md`

Sections:
- **Recurring** — active cron schedules with action, next fire, and last fired
- **Upcoming** — one-off schedules not yet fired
- **Recently Fired** — the last N fires with their **outcome** (sent / suppressed / handed-off / failed) and a short result snippet

The dashboard regenerates whenever a schedule is created, updated, fired, or
deleted, and is never reindexed as a source.

## Missed-fire Policy

If the server is down through a fire window, the schedule is past-due on the
next tick after startup and fires **once** (run-once catch-up; the 90s cooldown
dedupes overlapping restarts). Cron schedules then advance to their next future
slot; a missed one-off fires once and disables. Windows missed during a longer
outage are **not** replayed — at most one catch-up per schedule.

## Migration

Existing `~/.lifeos/reminders.json` entries migrate into `Scheduler/Inbox.md`
(message types map `static→notify`, `prompt→prompt`, `endpoint→endpoint`):

```bash
~/.venvs/lifeos/bin/python scripts/migrate_reminders_to_scheduler.py
```

The migration is idempotent (a `reminders.json.migrated` marker guards re-runs)
and keeps `reminders.json` as a backup.

## API Reference

See [API Reference § Scheduler & Telegram Endpoints](../specs/product/api-reference.md#scheduler--telegram-endpoints).

## Technical Details

- Schedules are defined in `LifeOS/Scheduler/Inbox.md` (source of truth); `data/scheduler_index.json` is a rebuildable cache.
- The scheduler checks for due schedules every 60 seconds.
- A watchdog watcher reindexes the Scheduler directory on external edits (~2s debounce).
- Times default to the configured timezone (`America/New_York`).
- One-off schedules auto-disable after firing.
- Implementation: `api/services/scheduler_store.py`, `api/services/scheduler_watcher.py`.

## Related Documents

- [Scheduler — Technical](../specs/technical/scheduler.md) — Engineering view: store, cache, watcher, firing internals
- [Task Management](../specs/product/task-management.md) — Tasks system (the `agent` action writes `#agent` tasks)
- [Agent Worker](../specs/product/agent-worker.md) — Runs `action:: agent` schedules
- [API Reference](../specs/product/api-reference.md) — Scheduler API endpoint contracts
- [MCP Tools](../specs/product/mcp-tools.md) — `lifeos_schedule_*` tool contracts
