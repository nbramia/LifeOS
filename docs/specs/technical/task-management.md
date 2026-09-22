# Task Management — Technical

> **Status:** Complete
> **Owner:** Task Management
> **Last Updated:** 2026-09-22

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
| `api/services/task_projects.py` | Derived hierarchy/read model, relationship validation, explicit project lifecycle and coordinator/cancellation recovery |
| `api/services/operation_lock.py` | Re-entrant cross-process boundary for relationship, claim, and durable-operation races |
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
copied through byte-for-byte. This matters because the parser does not
require `TODO`: the first time a vault reindexes under this parser, every
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

`status` is validated the same way, against `VALID_STATUSES` — without this,
an unrecognized status would write a blank checkbox (the symbol lookup
falls back to `todo`'s `" "`) and round-trip as the invalid string until
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
line text differs from the last line the API wrote or saw for this id,
or the on-disk notes body differs from the in-memory task's. This is
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
files. If the task's block is absent from the source entirely (an
external delete raced the move), the move is treated like any other
externally-deleted task — reconciled out of the index — rather than raising
a conflict.

## Lifecycle dates

`Task.done_date`/`Task.cancelled_date` are stamped and cleared at one
central choke point inside `update()`'s per-key `apply()` closure, the same
pattern `_clear_stale_snooze` uses for `snoozed_until`: a `status` write that
lands on `"done"` stamps `done_date` to today (skipped if the task is
already `"done"`, so a no-op status write never re-stamps it); a `status`
write that lands on `"cancelled"` stamps `cancelled_date` the same way. A
`status` write that *leaves* `"done"` clears `done_date`, and one that
leaves `"cancelled"` clears `cancelled_date` — so a task's lifecycle date
never survives a status change away from the status it belongs to. Moving
directly between the two terminal statuses stamps the new date and clears
the old one in the same write. This is what a board lane-move Undo (see
[agent-viz.md](../product/agent-viz.md)'s Undo behavior) relies on: writing
a card's prior status back through `PUT /api/tasks/{id}` — the general
task-update endpoint, not a lane-endpoint replay — leaves no stale
`done_date` behind on a card that only passed through Done briefly.

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
that task — an accepted simplification for a single-user vault. Addressing a
task by id keeps the race window narrow in the sibling-line case, because a
write never depends on a cached line number staying valid.

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

## Snoozed-until field

A card's snooze is a wake-up time, `[snoozed_until:: <ISO-8601 with offset>]`, stored the same generic way as `host`/`effort`/`model` — no parser change, no schema change (see [Parsing](#parsing)). `TaskManager` itself does not parse or validate the timestamp; `agent_board.parse_snoozed_until`/`is_snoozed` do (see [Agent Viz — Technical § Snooze](agent-viz.md#snooze)), and the write path (`PUT /api/agents/board/cards/{id}/snooze`) rejects a missing, unparseable, offset-less, or non-future value before ever calling `TaskManager.update`. A stored value that has since passed is left in place — nothing purges it, and it is simply ignored, the same as any other expired-but-present field.

`TaskManager.claim_for_agent`'s `is_claimable` check refuses a task whose `fields` make `agent_board.is_snoozed(task.fields)` true, re-evaluated on every compare-and-swap retry exactly like the existing status/pickup-tag/exclusion-tag checks — a claim attempt against a snoozed task fails the same way a claim against an ineligible status does, independent of whatever listing produced the candidate.

`TaskManager` also owns the one central write-time choke point that keeps a stale `snoozed_until` from lingering on a task whose write lands it somewhere the field is irrelevant: `_clear_stale_snooze(t)`, called at the tail of both `update()`'s `apply()` and `swap_tag()`'s `compute()`, drops the field whenever the write's own resulting status/tags land the task in a natural lane outside `agent_board.SNOOZABLE_LANES` (i.e. `in_progress` or `done`) — it checks only field presence and the natural lane, never parses the timestamp itself. This is what keeps the worker's `/swap-tag` resume (`agent-blocked` → `agent-running`) and a generic `PUT /api/tasks/{id}` status change to `in_progress`/`done`/`cancelled` from leaving a future wake-up time behind on a card that's now running or finished — see [Agent Viz — Technical § Snooze](agent-viz.md#snooze) for the full write-path picture, including the board-specific writes (lane move, Accept, Cancel, Reject/Reassign) that clear it explicitly. `human_queue.resolve_card` (`update(status="done", ...)`) is not one of the paths this clears for a `#human` card: the `human` tag alone keeps the natural lane at `human_queue`, which is still snooze-eligible, so a snoozed `#human` card resolved this way keeps its `snoozed_until` and stays hidden until it wakes.

## Shared lifecycle projection

`api/services/agent_worker/lifecycle.py` coordinates execution transitions
without replacing either authority. SessionStore records immutable
attempt/turn identity, typed waits, and a pending projection marker before
the projector applies an id-addressed `TaskManager.update`; the marker is
acknowledged only after the Markdown write succeeds. A stale `updated_at`
version leaves the marker conflicted and preserves the operator's edit for a
fresh retry. Provider and dependency waits keep the task in `in_progress` and
carry a `wait_reason` badge; operator waits use the existing blocked/Human
Queue behavior. Repeated events are keyed by `event_id` and are no-ops after
acknowledgement.

## Derived hierarchy and project lifecycle

`fields.parent_id` is the only persisted membership edge. `TaskHierarchy`
builds incoming-child and parent maps from the complete task set, including
terminal children, before any API filters are applied. It produces compact
read fields for task and board responses and a stable validation error for an
observed malformed edge. Malformed direct-vault edits stay indexed and visible;
they fail worker claim/open instead of being dropped or interpreted as safe
ordinary work.

`fields.project` is independent repository-affinity metadata and never enters
the hierarchy maps. An affinity-only task remains an ordinary task;
`is_project`, `child_count`, and the derived `project` summary depend only on
valid incoming `fields.parent_id` edges, while both persisted fields continue
to round-trip in the task's `fields` map. Worker dispatch may resolve the
affinity through its recognized location catalog, but an unknown string remains
opaque metadata and never becomes a raw path; hierarchy edits perform no
affinity migration or rewrite.

Relationship creation, mutation, deletion, and atomic worker claim share
`.task-operation.lock`. The re-entrant wrapper lets a first-child mutation
pause the parent through the ordinary TaskManager write path while retaining
one outer OS lock. Every path that needs both locks acquires the instance
`RLock` and then the process lock. Each process refreshes authoritative
Markdown after taking that boundary. Therefore a first-child attachment and a
worker claim serialize: the claim either records its live state first and
attachment refuses, or the attachment records the pause and child first and
claim refuses. Per-file CAS still protects the bytes written inside that
broader decision boundary.

API-mediated first attachment persists `execution_paused=true` before writing
the child. A valid relationship first observed during watcher/full reindex
also repairs the pause onto the parent; claim derives `is_project`
independently, so it fails closed even if that repair loses a CAS and waits for
the next watcher pass. A repair pass suppresses nested repair entry while its
bounded CAS retry reindexes competing external edits. Removing the final child
retains the pause. Invalid external relationships are not rewritten
automatically.

Interactive Open writes a short `execution_reservation_until` lease under the
same operation boundary before spawning a CLI. First-child attachment and
worker claim treat a live lease as execution, closing the hook-registration
gap; the first lifecycle status projection clears it, and an abandoned lease
expires without repair work.

Direct vault writes cannot be atomic with API decisions before the watcher has
observed them. The watcher debounce is the explicit observation window: an
unobserved edit can race an API request, while any observed relationship is
included in hierarchy validation, pause repair, and claim/open guards. This is
why classification and claim safety never rely on the pause field alone.

`TaskManager` enforces caller-independent guards for create/update/complete/
delete/claim and lifecycle-tag swaps. A swap cannot manufacture worker
lifecycle state on a task that fails project claim admission, while a card
that already carries `agent-running` or `agent-blocked` can still complete,
fail, block, or resume through the worker's atomic transition. The route maps
an admission conflict to HTTP 409.

Create/update on `/api/tasks` also read an optional, caller-asserted
`X-LifeOS-Agent-Session` header — the same trust model as `actor` and
`fields.assigned_by`, not cryptographic attestation. When present and the
write is, or would become, a project child, `_enforce_agent_child_tag_guard`
(`api/routes/tasks.py`) refuses `#hermes` outright and refuses a paid route
(`#cloud`/`#cloud-haiku`/`#cloud-sonnet`) unless the project's owner
(`project_coordinator_session_id`) already resolves to that same executor —
`inter_agent.metered_target_out_of_scope`, the same function the handoff
handler uses for its own source-turn scope check. A create that carries
`fields.parent_id` under that header stamps `project_child_origin=agent`
and `project_child_creator_session=<id>` on the new task; `stage_handoff`
stamps its children the same way, with the handoff's source session as the
creator. Both fields are in `TaskManager.create`'s and
`_guard_project_update`'s `internal_fields` sets, so neither create's raw
`fields` dict nor an ordinary update can set or clear them.

`pause_project`/`resume_project` set and clear three internal parent fields —
`project_paused`, `project_paused_at`, `project_pause_reason` (`operator`,
`owner_failed`, or `owner_budget`) — through the same `_project_operation`
path, so they land in `TaskManager.create`'s and `_guard_project_update`'s
`internal_fields` sets like every other project-lifecycle field. Pause
enforcement lives entirely in `_project_claim_allowed`: a truthy
`project_paused` on a child's parent refuses that child's claim and
interactive Open exactly like a pending cancellation or handoff, and a
`parent_project_paused` read field carries the same fact to task/board
consumers next to `parent_handoff_pending`. `claim_for_agent` additionally
raises `ProjectConflictError` (-> HTTP 409) the moment it observes a paused
parent, both before and inside its CAS retry closure, so a worker's claim
attempt gets an unambiguous refusal distinct from ordinary staleness — every
other `_project_claim_allowed` refusal reason still returns the softer
`(False, False)`. Pause does not touch `_guard_project_update`'s status/tags
guards, so a child mid-turn when the pause takes effect keeps transitioning
through its own lifecycle (running -> review) undisturbed, and Cancel and
operator Complete on the project itself stay available. `plan_and_delegate`
refuses a paused project outright, checked both before staging the
coordinator session and again in the linking CAS precondition.

`ProjectTaskService` composes the slower explicit actions:

- start and completion mutate the ordinary parent without forging agent tags;
- plan/delegate stages a separate operator-origin session as non-dispatchable,
  links its session and request IDs onto the parent, then makes it claimable;
- retry-safe child creation uses `TaskManager.create_or_find_by_operation()`;
  the coordinator derives one durable `operation_key` per intended child from
  the project ID, planning operation ID, and child role, and a later call that
  reuses the key with different task inputs recovers the original unchanged;
- cancellation writes its operation ID/timestamp first, releases task locks
  before session teardown, and re-reads current children on every pass;
- a failed or unverifiable stop leaves intent and affected work unresolved for
  a same-ID retry, including after process restart;
- cancellation preview returns that pending operation ID, and policy keeps the
  cancel action available so clients can issue the required same-ID retry;
- review cancellation removes the review lifecycle marker, adds
  `agent-result-abandoned`, and preserves SessionStore/transcript output rather
  than recording acceptance.

The coordinator uses a synthetic task ID derived from project ID plus a hash of
the caller's stable operation ID. Its canonical execution request comes from
the parent owner and assignment fields. The existing legacy-alias adapter maps
`#cloud-haiku` and `#cloud-sonnet` to the Managed Agents executor and their
explicit model consent while retaining configured effort and host assignment
fields. This gives restart recovery a direct lookup and prevents repeated
clicks from creating duplicate sessions without a new SQL table. Parent fields
link coordination directly; coordinator summaries scan the transcript through
its streaming iterator and retain only the latest 100 events in memory. The
scan remains linear in transcript length and does not add a cache. Capped board
snapshots and the task-backed session keyed by the parent ID are not used for
liveness.

## Related Documents

### Specifications
- [Task Management — Product](../product/task-management.md) — Feature description, statuses, API usage examples (the consumer-facing counterpart to this spec)
- [API Reference](../product/api-reference.md#task-endpoints) — `/api/tasks` contracts
- [Human Queue guide](../../guides/human-queue.md) — Fire-and-forget operator cards built on this store
- [Scheduler — Technical](scheduler.md) — The id-addressed block rewrite, notes-style body, and merge-forward patterns this store mirrors
- [Agent Worker — Product](../product/agent-worker.md#tag-lifecycle) — `POST /{id}/swap-tag` contract this store preserves unchanged
- [Architecture](architecture.md) — Where the task modules sit in the code structure
- [Agent Viz — Technical](agent-viz.md) — The `/agents` board's `GET /board`, reading `list_tasks()` and writing via `update()`

### Code References
- [task_manager.py](../../../api/services/task_manager.py) — Store, round-trip, CAS writes
- [task_projects.py](../../../api/services/task_projects.py) — Hierarchy and project lifecycle
- [task_watcher.py](../../../api/services/task_watcher.py) — File watcher
- [atomic_write.py](../../../api/services/atomic_write.py) — Shared atomic-write helper
- [tests/test_task_manager.py](../../../tests/test_task_manager.py) · [test_task_projects.py](../../../tests/test_task_projects.py) · [test_task_watcher.py](../../../tests/test_task_watcher.py) · [test_atomic_write.py](../../../tests/test_atomic_write.py) — Coverage
