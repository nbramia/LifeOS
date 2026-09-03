# Scheduler — Technical

> **Status:** Complete
> **Owner:** Scheduler
> **Last Updated:** 2026-09-03

Engineering view of the Scheduler — how a trigger bound to an action is stored,
reindexed, and fired. For the trigger/action model, line schema, and operator
how-to, see [guides/scheduler.md](../../guides/scheduler.md); for the public
surfaces see [api-reference.md](../product/api-reference.md#scheduler--telegram-endpoints)
and [mcp-tools.md](../product/mcp-tools.md). This spec does not restate those.

## Modules

| File | Role |
|------|------|
| `api/services/scheduler_store.py` | `ScheduleEntry`, `SchedulerStore` (CRUD + markdown round-trip + index cache + dashboard), `SchedulerScheduler` (tick + firing) |
| `api/services/scheduler_watcher.py` | `SchedulerWatcher` — watchdog observer that reindexes on external edits |
| `api/routes/scheduler.py` | `/api/scheduler` HTTP surface |
| `api/services/reminder_store.py` | Back-compat shim re-exporting the above under legacy `Reminder*` names |

`api/main.py` (lifespan, `api/main.py:95-102`, `:169-170`) rebuilds the index
from the vault on startup, then starts the scheduler thread and the watcher.

## Source of truth + cache

The vault markdown is authoritative; the JSON index is a rebuildable cache.

- **Source:** `LifeOS/Scheduler/Inbox.md` — one checkbox line per schedule (line schema in the guide). `_format_entry_line`/`_parse_entry_line` (`scheduler_store.py:198`, `:219`) are inverse for the definition fields, giving a lossless round-trip.
- **Cache:** `data/scheduler_index.json` — the full `ScheduleEntry` (including payload + computed `next_trigger_at`). Safe to delete; `rebuild_index` (`:538`) regenerates it from markdown.

Only a subset of fields lives in markdown (the user-editable definition). The
payload (`message_content`, `endpoint_config`) and run history
(`last_status`, `last_result`, `last_triggered_at`) live only in the cache and
are **merged back by ID** on every reindex via `_merge_prior`
(`scheduler_store.py:553`). This is why editing a line in Obsidian never wipes
a schedule's prompt text or history.

CRUD writes both sides: `create`/`update`/`mark_triggered`
(`:387`, `:415`, `:438`) edit the markdown line by ID and re-save the cache;
`record_run` (`:453`) updates history in the cache only (it is not part of the
markdown definition).

## Reindex on edit

`SchedulerWatcher` (`scheduler_watcher.py:76`) runs a watchdog `Observer` over
the Scheduler directory (non-recursive). `_SchedulerFileHandler` (`:25`)
coalesces rapid events per path behind a 2s debounce (`_DEBOUNCE_SECONDS`) and
calls `SchedulerStore.reindex_file`. The generated `Dashboard.md` and the
`Scheduler.md` control file are skipped, so regenerating them never triggers a
feedback loop.

## Firing

`SchedulerScheduler` (`scheduler_store.py:663`) runs a daemon thread:

- `_run` (`:721`) — crash-recovery loop: exponential backoff (5s→60s cap), Telegram alert per crash, gives up after 5 consecutive crashes, resets the counter after 10 healthy minutes.
- `_schedule_loop` (`:812`) — every 60s, reads the `Scheduler.md` control file (`_read_control_file`, `:774`; `enabled: false` pauses without killing the thread), then fires each due entry.
- `get_due_reminders` (`:467`) — returns enabled, past-due entries, skipping any fired within the last 90s (the cooldown that dedupes restarts). The **missed-fire policy** (run-once catch-up) is a consequence of this check; see the guide for the operator-facing statement.
- `_fire_entry` (`:849`) — advances the trigger first (so a crash mid-fire can't double-fire), then dispatches on `entry.action`, and records the outcome via `record_run`. For `notify`/`prompt` an empty or sentinel result is suppressed. The `agent` action calls `_hand_off_to_agent` (`:927`), which writes an `#agent` task tagged with the executor; the [agent worker](../product/agent-worker.md) discovers and routes it — no execution happens in the scheduler.

## Back-compat

`reminder_store.py` re-exports `ScheduleEntry`/`SchedulerStore`/`SchedulerScheduler`
(and `get_scheduler_store`/`get_scheduler`) under the legacy `Reminder*` names,
and `_fire_reminder` aliases `_fire_entry`. The legacy `/api/reminders*`,
`lifeos_reminder_*`, and `manage_reminders` surfaces forward to the same store.
`scripts/migrate_reminders_to_scheduler.py` is the one-shot import from the old
`~/.lifeos/reminders.json` (idempotent, keeps the JSON as a backup).

## Privacy Considerations

Schedules and their results live entirely in the local vault and `data/`; the
only outbound path is the Telegram delivery already covered by
[security-privacy.md](security-privacy.md). Prompt results are truncated to a
short snippet in `last_result` for the dashboard.

## Related Documents

### Specifications
- [Task Management — Technical](task-management.md) — Task store that mirrors this store's id-addressed block rewrite, `>` body, and merge-forward patterns
- [Scheduler Guide](../../guides/scheduler.md) — Operator how-to, line schema, trigger/action model (the consumer-facing counterpart to this spec)
- [API Reference](../product/api-reference.md#scheduler--telegram-endpoints) — `/api/scheduler` contracts
- [MCP Tools](../product/mcp-tools.md) — `lifeos_schedule_*` tool contracts
- [Architecture](architecture.md) — Where the scheduler modules sit in the code structure
- [Agent Worker — Product](../product/agent-worker.md) — Runs the `#agent` tasks the `agent` action writes
- [Data & Sync](data-and-sync.md) — Scheduler storage in the data-locations table
- [Agent Viz — Technical](agent-viz.md) — The `/agents` board's Scheduled/Done split, reading `list_all()` via `agent_board.is_schedule_active`

### Code References
- [scheduler_store.py](../../../api/services/scheduler_store.py) — Store, round-trip, scheduler
- [scheduler_watcher.py](../../../api/services/scheduler_watcher.py) — File watcher
- [tests/test_scheduler_store.py](../../../tests/test_scheduler_store.py) · [test_scheduler_watcher.py](../../../tests/test_scheduler_watcher.py) — Coverage
