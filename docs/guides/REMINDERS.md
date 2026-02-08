# Reminders Guide

LifeOS supports natural language reminders delivered via Telegram. You can create, edit, list, and delete reminders through conversation.

## Prerequisites

Reminders require Telegram to be configured. See [Configuration](../getting-started/CONFIGURATION.md#telegram-optional) for setup.

## Creating Reminders

Just ask naturally. If you don't specify a time, LifeOS picks a sensible default (tomorrow at 9am).

```
"remind me to return the library book"
"remind me to call mom tomorrow"
"remind me to check the oven in 30 minutes"
"remind me at 3pm to join the standup"
```

### Smart Time Defaults

| You say | Current time | Result |
|---------|--------------|--------|
| "remind me to X" (no time) | any | tomorrow 9am |
| "remind me later today" | 9am | 5pm today |
| "remind me later today" | 5pm | 8pm today |
| "remind me tonight" | any | 8pm today |
| "remind me tomorrow" | any | tomorrow 9am |
| "remind me tomorrow morning" | any | tomorrow 9am |
| "remind me tomorrow afternoon" | any | tomorrow 2pm |
| "remind me tomorrow evening" | any | tomorrow 6pm |
| "remind me next week" | any | next Monday 9am |
| "remind me in 2 hours" | any | now + 2 hours |

### Recurring Reminders

For recurring reminders, be explicit about the schedule:

```
"remind me every day at 6pm to fill out the form"
"remind me every weekday at 9am to check email"
"remind me every Monday at 10am about the standup"
```

## Listing Reminders

```
"what are my reminders"
"show my reminders"
"list reminders"
```

## Editing Reminders

You can edit reminders by name or by context (if you just created one).

**By name:**
```
"change the library book reminder to 3pm"
"move the standup reminder to tomorrow"
"reschedule my mom reminder to next week"
```

**By context (same conversation):**
```
User: "remind me to return the library book"
Bot: "Done! I've set a reminder for tomorrow at 9:00 AM..."

User: "change it to 7pm"
Bot: "I've updated "Library Book Reminder" to tomorrow at 7:00 PM."
```

## Deleting Reminders

**By name:**
```
"delete the library book reminder"
"cancel my standup reminder"
"remove the reminder about mom"
```

**By context:**
```
"cancel that reminder"
"delete that"
```

## Response Format

When you create a reminder, LifeOS confirms the exact time:

```
Done! I've set a reminder for tomorrow at 9:00 AM.

Library Book Reminder
Return the library book

Reply to change the time or say "cancel that reminder" to remove it.
```

## Task-Reminder Linking

You can create a task and reminder together:

```
"add a task to call the dentist and remind me Friday at 3pm"
```

This creates both a task in your vault and a timed reminder, linked via `reminder_id`. See [Task Management Guide](TASK-MANAGEMENT.md) for details.

## Obsidian Dashboard

Reminders are tracked in an auto-generated dashboard:

**Location:** `LifeOS/Reminders/Dashboard.md`

The dashboard includes three sections:
- **Recurring** — Active cron-based reminders with schedule and last triggered time
- **Upcoming** — One-time reminders not yet triggered
- **Past** — Completed or disabled reminders (last 20)

The dashboard regenerates automatically whenever a reminder is created, updated, triggered, or deleted.

## Technical Details

- Reminders are stored in `~/.lifeos/reminders.json`
- The scheduler checks for due reminders every 60 seconds
- Times are processed in Eastern time (America/New_York) by default
- One-time reminders auto-disable after triggering
- Dashboard auto-generated at `LifeOS/Reminders/Dashboard.md` in the vault
