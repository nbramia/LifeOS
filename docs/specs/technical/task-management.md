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
  `_parse_task_line` (`task_manager.py:1224`, `:1272`) are inverse for a task
  written by the API. A hand-authored line need not be — see "Parsing" below.
- **Cache:** `data/task_index.json` — the full `Task` (dataclass at
  `task_manager.py:129`), including fields with no markdown representation
  (`reminder_id`). `rebuild_index` (`:671`) regenerates it from the vault.

## Parsing

A task line is any `- [.] ...` checkbox line — `_CHECKBOX_RE` (`:1188`) does
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
holds them (`:1224-1258`). No parser change is needed to add a new field.

A checkbox line inside a fenced (` ``` ` or `~~~`) code block is never
parsed as a task, even though `_CHECKBOX_RE` would otherwise match it — a
line like `- [ ] example` in a documentation snippet is example text, not a
real task. `_iter_lines_with_fence_state` toggles fence state on any line
whose stripped text starts with a fence marker; every scanner that walks
task lines (`_reparse_lines`, `_cas_insert_at_top`, `_find_task_block_span`,
`_reposition_file`) skips a line it marks as fenced before handing it to
`_match_task_block`.

**Duplicate ids.** Two lines sharing the same `<!-- id:xxxx -->` comment
(most often a hand-copied line) are not left to fight over that id forever.
`_reparse_lines` keeps the first occurrence's id as-is; a later occurrence
in the same file carrying an id already claimed earlier in the same parse
is treated exactly like a line with no id comment at all — its
stale/duplicated comment is replaced with a freshly minted one on that
pass, and both lines are indexed under distinct ids from then on.

A duplicate that spans two *different* files is resolved only on the next
full `rebuild_index`, not by a single-file `reindex_file` call: `rebuild_index`
threads one `seen_ids` set across every file it parses, so a task whose id
was already claimed by an earlier file in the same rebuild is treated the
same way as a same-file duplicate. `reindex_file` deliberately does not do
this — it has no way to distinguish a genuine cross-file duplicate from an
operator cutting a task line out of one file and pasting it into another,
so it keeps ids intact on an external cross-file move and leaves any true
cross-file duplicate for the next full rebuild to resolve.

## Id write-back

A checkbox line with no `<!-- id:xxxx -->` comment gets one minted
(`uuid4().hex[:8]`, same scheme as before) the first time it's parsed. On the
next reindex, `_reparse_lines` (`:713`) appends that id comment to the raw
line — and changes nothing else about it: no reformatting, no inserted
`TODO`, no field reordering. Every other line in the file, task or not, is
copied through byte-for-byte. This matters because the parser no longer
requires `TODO`: on the first reindex after this feature ships, every
existing hand-written `- [ ]` checklist item in `LifeOS/Tasks/*.md` gets an
id comment appended, once, and is indexed as a task from then on. That is
the intended migration, not a bug.

## Input validation

`description`, `notes`, and `fields` are validated by
`_validate_text_fields` before `create`/`update` do anything else, because
they're interpolated directly into the checkbox line's text rather than
going through a serializer that could escape them. Rejected (`ValueError`,
mapped to HTTP 422 by `api/routes/tasks.py`) rather than silently
sanitized, since truncating or stripping would save something other than
what the caller sent:

- A newline (`\n` or `\r`) in `description` or a `fields` value would split
  the single checkbox line into two.
- A `]` in `description` or a `fields` value would truncate an inline
  field's closing bracket and leak the rest of the value into the
  description (or the next field).
- An HTML comment opener (`<!--`) anywhere in `description`, a `fields`
  value, or a `notes` line could forge a new `<!-- id:.. -->`, hijacking
  another task's id on the next reindex.
- `notes` lines are allowed to be multi-line — that's their whole point —
  but not `\r` (would desync the `\n`-joined body from what
  `Task.notes.split("\n")` expects) or `<!--`.
- A `fields` key must be a bare word (`^\w+$`), because `_format_task_line`
  interpolates it directly as `[key:: value]`.
- A `fields` key may not shadow a reserved key: any inline field with a
  dedicated `Task` attribute (`due`, `priority`, `created`, `done`,
  `cancelled`, `updated`) or `id` itself. Accepting one through the
  free-form `fields` dict would let a caller write a second, conflicting
  `[key:: value]` onto the line, or forge the id comment outright —
  `fields={"updated": "SPOOFED"}` would otherwise write two `[updated::]`
  fields and, after the next reindex re-parses the line, `updated_at` would
  read back as `"SPOOFED"`.

`status` is validated the same way, against `VALID_STATUSES` — an
unrecognized status previously wrote a blank checkbox (the symbol lookup
falls back to `todo`'s `" "`) and round-tripped as the invalid string until
the next reindex silently flipped it back to `todo`.

## Id-addressed, compare-and-swap writes

Every write locates its task's line by id — `_find_task_block_span`
(`:1356`) scans for the block whose id comment matches, the same mechanism
`SchedulerStore._find_block_span` uses. A cached `line_number` is never used
to address a write; it is refreshed after every write purely as read-side
bookkeeping (`_reposition_file`, `:917`) so `GET` responses stay accurate.
This is what lets a `PUT` succeed correctly even when an external edit (via
Obsidian, delivered by Syncthing) has inserted lines above the task before
the watcher's 2s debounce catches up.

Each write goes through `_cas_rewrite` (`:802`) or `_cas_insert_at_top`
(`:881`): read the file's mtime, read and locate the block, compute the new
content, then re-check the mtime immediately before writing. A mismatch
means a concurrent external writer touched the file in between, so the
manager calls `reindex_file` (absorbing that change into `self._tasks`) and
retries — up to `_CAS_MAX_RETRIES` (`:70`, currently 3) times — before
raising `TaskConflictError`, which `api/routes/tasks.py` maps to HTTP 409.
The retry re-invokes the caller's `compute()` closure against the
just-refreshed `self._tasks[task_id]`, so an `update()` retry re-applies the
operator's requested changes on top of the latest known state rather than
blindly overwriting it with stale data — the same "recompute, don't just
retry the same bytes" discipline CAS requires anywhere. `compute()` always
builds a *new* `Task` from a copy of the current one rather than mutating it
in place, so a losing attempt's edits are never visible through `get()` —
`self._tasks[task_id]` is rebound only once a write actually succeeds.

`_cas_rewrite` also checks, before calling `compute()`, whether the on-disk
block already reflects an edit `reindex_file` hasn't absorbed yet — the raw
line text no longer matches the last line the API wrote or saw for this id,
or the on-disk notes body no longer matches the in-memory task's. This is
the *normal* case for an edit that just landed, not a rare race: the
watcher's 2s debounce means a `PUT` can easily arrive after an external
edit has hit disk but before `reindex_file` has run. Without this check,
`compute()` would build its replacement from the stale in-memory task and
silently revert the edit — an operator retitling a task and adding a body
line, followed a moment later by an unrelated `PUT` that only changes
`priority`, would otherwise lose the retitle and the added line. On a
mismatch the manager absorbs it via `reindex_file` and retries (counting
toward `_CAS_MAX_RETRIES`), the same as an mtime conflict.

`self._lock` is a `threading.RLock`, not a plain `Lock`: a CAS retry inside
a lock-held mutating call re-enters `reindex_file`, which also takes the
lock. A plain `Lock` would self-deadlock on the very first retry.

**Context-change moves.** `update(..., context=...)` moves a task's block
between files via `_move_task_between_files`. The destination insert
happens *before* the source removal: if the destination's CAS insert
raises, the source is untouched — the task never disappears. If the source
removal then fails (a conflict there, after the destination insert already
succeeded), the manager best-effort removes the just-inserted destination
block before re-raising, rather than leaving the task duplicated in both
files. If the task's block is no longer present in the source at all (an
external delete raced the move), the move is treated like any other
externally-deleted task — reconciled out of the index — rather than raising
a conflict.

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

A task-file rewrite preserves the file's original line terminator (`\r\n`
vs. `\n`) and whether it ended with a trailing terminator, rather than
normalizing wholesale to `\n`-joined-plus-trailing-newline. Every call site
that reads a task file for a possible rewrite (`reindex_file`,
`rebuild_index`, `_cas_rewrite`, `_cas_insert_at_top`) uses
`_read_lines_with_terminator`, which reads raw bytes (not `Path.read_text`,
whose universal-newline translation would silently turn CRLF into LF before
the terminator could even be inspected) and detects both properties; the
matching write passes them through as `atomic_write_lines`'s `newline=`/
`trailing_newline=` keyword arguments. `scheduler_store.py` never calls
`atomic_write_lines` (it writes whole files via `atomic_write_text`), so
these defaults don't change its behavior.

## Notes body

`Task.notes` is a multi-line string stored as `_BODY_INDENT` (four spaces)
plus `> ` per line, directly beneath the task's checkbox line —
`_format_task_block` (`:1261`) emits it, `_match_task_block` (`:1331`) parses
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
The cost of not persisting is usually cosmetic: one skipped `[updated::]`
restamp immediately after a restart, for a task that was genuinely edited
while the server was down. It is a correctness gap in one specific,
narrow case — a task's block is inside a fenced code block, or shares a
duplicate id with another block, at the moment of a restart — where the
in-memory bookkeeping that would otherwise have resolved it cleanly is
reset along with everything else in `self._tasks`; the next parse simply
treats it as a fresh id with no history, same as it would for any other
process-lifetime-only piece of state. That's an accepted, bounded cost, not
a claim that no correctness gap exists at all.

`reindex_file`'s own write-back (minting an id, restamping an external
edit) is itself CAS-protected on the file's mtime, the same discipline as
`_cas_rewrite`: it re-reads and re-parses (bounded to `_CAS_MAX_RETRIES`
attempts) if the file changed between its read and its write, rather than
blindly overwriting whatever landed in between. On persistent conflict it
logs a warning and skips the write for that pass instead of raising —
`reindex_file` has no caller to hand a `TaskConflictError` to that would do
anything useful with it, and the watcher will fire again for whatever
caused the conflict. That skip is total, not partial: on a persistent
conflict `reindex_file` returns without merging the abandoned attempt's
parse into `self._tasks` or touching the index file or dashboard, since
that parse may have minted ids that never reached disk.

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
(`:209`) and skipped everywhere: `rebuild_index`'s glob, `reindex_file`
(early return, never indexed, never triggers a write), and `TaskWatcher`'s
event handler. `TaskManager.list_conflicts` (`:555`) surfaces them (name +
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

## Human queue

The Human queue (`api/services/human_queue.py`) is a thin layer on top of
this store, not a separate one: a card is a task with tag `human` and status
`blocked`, using the `fields` free-form dict (`key`, `source_host`,
`source_cwd`, `source_session`, `done_when`) documented above — no new
persistence, no schema change. See the
[Human Queue guide](../../guides/human-queue.md) for the tool/endpoint
contract and the `done_when` reference.

## Related Documents

### Specifications
- [Task Management — Product](../product/task-management.md) — Feature description, statuses, API usage examples (the consumer-facing counterpart to this spec)
- [API Reference](../product/api-reference.md#task-endpoints) — `/api/tasks` contracts
- [Human Queue guide](../../guides/human-queue.md) — Fire-and-forget operator cards built on this store
- [Scheduler — Technical](scheduler.md) — The id-addressed block rewrite, notes-style body, and merge-forward patterns this store mirrors
- [Agent Worker — Product](../product/agent-worker.md#tag-lifecycle) — `POST /{id}/swap-tag` contract this store preserves unchanged
- [Architecture](architecture.md) — Where the task modules sit in the code structure

### Code References
- [task_manager.py](../../../api/services/task_manager.py) — Store, round-trip, CAS writes
- [task_watcher.py](../../../api/services/task_watcher.py) — File watcher
- [atomic_write.py](../../../api/services/atomic_write.py) — Shared atomic-write helper
- [tests/test_task_manager.py](../../../tests/test_task_manager.py) · [test_task_watcher.py](../../../tests/test_task_watcher.py) · [test_atomic_write.py](../../../tests/test_atomic_write.py) — Coverage
