# Task Management — Technical

> **Status:** Complete
> **Owner:** Task Management
> **Last Updated:** 2026-09-03

Engineering view of the task store — how a task is located, written, and
reindexed. For the product-facing feature description, statuses, and API
usage examples, see [product/task-management.md](../product/task-management.md);
for endpoint contracts see
[api-reference.md](../product/api-reference.md#task-endpoints). This spec
does not restate those.

## Modules

| File | Role |
|------|------|
| `api/services/task_manager.py` | `Task`, `TaskManager` (CRUD + markdown round-trip + index cache + dashboard), module-level parse/format helpers |
| `api/services/task_watcher.py` | `TaskWatcher` — watchdog observer that reindexes on external edits |
| `api/services/atomic_write.py` | `atomic_write_text`/`atomic_write_lines` — shared temp-file-plus-rename helper, also used by `scheduler_store.py` |
| `api/routes/tasks.py` | `/api/tasks` HTTP surface |

`api/main.py` (lifespan, `api/main.py:185-192`) rebuilds the index from the
vault on startup, then starts the watcher.

## Source of truth + cache

The vault markdown is authoritative; `data/task_index.json` is a rebuildable
cache — deleting it just costs one `rebuild_index()` pass. Nothing about task
identity or content lives only in the cache except `reminder_id` (see
"Cache-only field merge-forward" below).

- **Source:** `LifeOS/Tasks/{Context}.md` — one checkbox line per task, with
  an optional indented `> ` notes body directly beneath it, exactly like a
  scheduler entry's `message_content` body
  ([scheduler.md](scheduler.md#source-of-truth--cache)). `_format_task_line`/
  `_parse_task_line` (`task_manager.py:898`, `:946`) are inverse for a task
  written by the API. A hand-authored line need not be — see "Parsing" below.
- **Cache:** `data/task_index.json` — the full `Task` (dataclass at
  `task_manager.py:76`), including fields with no markdown representation
  (`reminder_id`). `rebuild_index` (`:507`) regenerates it from the vault.

## Parsing

A task line is any `- [.] ...` checkbox line — `_CHECKBOX_RE` (`:892`) does
**not** require the literal `TODO` keyword. This is deliberate: it lets a
task typed by hand in Obsidian (`- [ ] Buy milk`) be recognized without the
operator learning LifeOS's own convention. `_format_task_line` still always
emits `TODO` on any line it writes, so the Obsidian Tasks dashboard queries
in `Dashboard.md` (which match on the checkbox status, not the word `TODO`)
and the existing convention both keep working unchanged.

Inline fields with a dedicated `Task` attribute (`due`, `priority`,
`created`, `done`, `cancelled`, `updated`) are parsed into that attribute;
every other `[key:: value]` — operator fields (`host`, `effort`, `model`,
`key`) and anything a later feature invents — lands in `Task.fields` (a
plain `dict[str, str]`) and round-trips through any rewrite untouched,
because `_format_task_line` re-emits `Task.fields` verbatim in the order it
holds them (`:898-934`). No parser change is needed to add a new field.

## Id write-back

A checkbox line with no `<!-- id:xxxx -->` comment gets one minted
(`uuid4().hex[:8]`, same scheme as before) the first time it's parsed. On the
next reindex, `_reparse_lines` (`:533`) appends that id comment to the raw
line — and changes nothing else about it: no reformatting, no inserted
`TODO`, no field reordering. Every other line in the file, task or not, is
copied through byte-for-byte. This matters because the parser no longer
requires `TODO`: on the first reindex after this feature ships, every
existing hand-written `- [ ]` checklist item in `LifeOS/Tasks/*.md` gets an
id comment appended, once, and is indexed as a task from then on. That is
the intended migration, not a bug.

## Id-addressed, compare-and-swap writes

Every write locates its task's line by id — `_find_task_block_span`
(`:1030`) scans for the block whose id comment matches, the same mechanism
`SchedulerStore._find_block_span` uses. A cached `line_number` is never used
to address a write; it is refreshed after every write purely as read-side
bookkeeping (`_reposition_file`, `:673`) so `GET` responses stay accurate.
This is what lets a `PUT` succeed correctly even when an external edit (via
Obsidian, delivered by Syncthing) has inserted lines above the task before
the watcher's 2s debounce catches up.

Each write goes through `_cas_rewrite` (`:597`) or `_cas_insert_at_top`
(`:640`): read the file's mtime, read and locate the block, compute the new
content, then re-check the mtime immediately before writing. A mismatch
means a concurrent external writer touched the file in between, so the
manager calls `reindex_file` (absorbing that change into `self._tasks`) and
retries — up to `_CAS_MAX_RETRIES` (`:61`, currently 3) times — before
raising `TaskConflictError`, which `api/routes/tasks.py` maps to HTTP 409.
The retry re-invokes the caller's `compute()` closure against the
just-refreshed `self._tasks[task_id]`, so an `update()` retry re-applies the
operator's requested changes on top of the latest known state rather than
blindly overwriting it with stale data — the same "recompute, don't just
retry the same bytes" discipline CAS requires anywhere.

`self._lock` is a `threading.RLock`, not a plain `Lock`: a CAS retry inside
a lock-held mutating call re-enters `reindex_file`, which also takes the
lock. A plain `Lock` would self-deadlock on the very first retry.

## Atomic writes

All file writes — task files, `data/task_index.json`, `Dashboard.md`, a
freshly created context file's template — go through
`atomic_write.atomic_write_text`/`atomic_write_lines`: write a temp file in
the same directory, `fsync`, then `os.replace` into place. A reader never
observes a partial file, because the destination path is never opened for
writing directly — only the temp file is, and the swap is one atomic
rename. `scheduler_store.py` shares this same helper (its own writes had the
same non-atomic gap; fixing it was a trivial swap with no behavior change,
verified by the unchanged scheduler test suite).

## Notes body

`Task.notes` is a multi-line string stored as `_BODY_INDENT` (four spaces)
plus `> ` per line, directly beneath the task's checkbox line —
`_format_task_block` (`:935`) emits it, `_match_task_block` (`:1005`) parses
it back, both mirroring the scheduler entry body pattern exactly
(`scheduler.md`'s `_format_entry_block`/`_iter_entry_blocks`). Deleting or
moving a task carries its body with it, because every write operates on the
whole block (main line plus body lines), never the main line alone.

## Cache-only field merge-forward

`reminder_id` has no markdown representation — it never appears in a
`[key:: value]` field, so a plain reindex would silently lose it (the
markdown, re-parsed, says nothing about it). `_reparse_lines` merges it
forward from `self._tasks` (the prior in-memory state, itself loaded from
the JSON cache) by id, the same pattern as `SchedulerStore._merge_prior` for
`message_content`/`endpoint_config`.

## External-edit detection

Whether a task's line changed externally since the API last wrote it is
decided by exact-string comparison, not by reformatting the prior `Task` and
hoping it matches. `TaskManager._last_written_line` (`dict[id, str]`, never
persisted) holds the literal text last written or observed for each task
this process's lifetime. `_reparse_lines` compares the current raw line
against that entry: a mismatch means an external edit — the parsed values
win (they already do, unconditionally) and the line is rewritten with a
fresh `[updated::]` stamp, scoped to that one task's line only; a match, or
no prior entry at all (first time this process has seen the id), leaves the
line untouched.

Reformatting the prior `Task` instead of storing the exact string was tried
first and rejected: it broke on any line the API didn't canonically format
— most notably a line that just had an id minted onto otherwise
hand-written text (no `TODO`, no `created` field). Reformatting that prior
`Task` via `_format_task_line` always re-inserts `TODO` and an (empty)
`created` field, so the comparison found a "difference" on every single
reindex and rewrote the line every time, defeating idempotency.

**Why no `data/kanban.db` sidecar.** A small sidecar database was one option
for this bookkeeping — `task_index.json` is rewritten whole on every
mutation, so it's the wrong place for it — but wasn't needed: `reminder_id`
merge-forward already works from the existing JSON-cache-backed
`self._tasks`, and external-edit detection only needs to hold up within a
single running process — after a restart, whatever's on disk simply becomes
the new baseline for comparison, and the in-memory `Task` always reflects
the file's actual current content regardless of whether that reset happened.
The cost of not persisting is purely cosmetic (one skipped `[updated::]`
restamp immediately after a restart, for a task that was genuinely edited
while the server was down) — never a correctness gap.

## Compare-and-swap retry vs. field-level merge

A CAS retry re-applies the caller's requested field changes on top of the
freshly reindexed task, but it does not attempt a field-level three-way
merge against a concurrent, unrelated edit to the *same* task (e.g. the
operator renames a task in Obsidian in the same instant the API is changing
its due date). The later writer's full requested change wins outright for
that task — an accepted simplification for a single-user vault, and a
narrower race window than the previous cached-line-number addressing had
for the sibling-line case this issue set out to fix.

## Conflict files

Syncthing conflict copies (`*.sync-conflict-YYYYMMDD-HHMMSS...`) and
in-progress temp files (`.syncthing.*`) are recognized by `is_conflict_file`
(`:135`) and skipped everywhere: `rebuild_index`'s glob, `reindex_file`
(early return, never indexed, never triggers a write), and `TaskWatcher`'s
event handler. `TaskManager.list_conflicts` (`:435`) surfaces them (name +
mtime) via `GET /api/tasks/conflicts`, registered before `GET
/{task_id}` in `api/routes/tasks.py` so FastAPI doesn't capture `"conflicts"`
as a task id. Resolving a conflict file is a manual, out-of-band operation
(Obsidian, or deleting the losing copy) — nothing here does it automatically.

## Reindex on edit

`TaskWatcher` (`task_watcher.py`) runs a watchdog `Observer` over the tasks
directory (non-recursive). `_TaskFileHandler` coalesces rapid events per path
behind a 2s debounce and calls `TaskManager.reindex_file`. `Dashboard.md` and
conflict/temp files are skipped so regenerating the dashboard, or a Syncthing
sync artifact landing mid-transfer, never triggers a feedback loop or a spurious
reindex.

## Privacy Considerations

Task descriptions, notes, and operator fields live entirely in the local
vault and `data/`; nothing here has an outbound path of its own (see
[security-privacy.md](security-privacy.md) for the surfaces, like Telegram
delivery, that do). `data/task_index.json` and the vault markdown carry the
same content — deleting one and rebuilding from the other never loses or
duplicates personal data, by construction.

## Related Documents

### Specifications
- [Task Management — Product](../product/task-management.md) — Feature description, statuses, API usage examples (the consumer-facing counterpart to this spec)
- [API Reference](../product/api-reference.md#task-endpoints) — `/api/tasks` contracts
- [Scheduler — Technical](scheduler.md) — The id-addressed block rewrite, notes-style body, and merge-forward patterns this store mirrors
- [Agent Worker — Product](../product/agent-worker.md#tag-lifecycle) — `POST /{id}/swap-tag` contract this store preserves unchanged
- [Architecture](architecture.md) — Where the task modules sit in the code structure

### Code References
- [task_manager.py](../../../api/services/task_manager.py) — Store, round-trip, CAS writes
- [task_watcher.py](../../../api/services/task_watcher.py) — File watcher
- [atomic_write.py](../../../api/services/atomic_write.py) — Shared atomic-write helper
- [tests/test_task_manager.py](../../../tests/test_task_manager.py) · [test_task_watcher.py](../../../tests/test_task_watcher.py) · [test_atomic_write.py](../../../tests/test_atomic_write.py) — Coverage
