# Pebble Capture Filing

**Status:** Complete
**Last Updated:** 2026-09-17
**Audience:** Operators

Pebble owns `LifeOS/Log/Pebble`, where each recording day is two files:
`YYYY-MM-DD-raw.md` holds the framed records and `YYYY-MM-DD.md` holds Pebble's
readable digest of them. LifeOS reads framed result records from that directory
and never rewrites the archive, transcripts, or Pebble's private SQLite/audio
spool. The digest carries no frames, so scanning it produces no effects. Only a
complete v1 `result` frame with `status: ready` is eligible for filing; partial,
invalid, uncertain, pending, and failed records remain evidence only.

## Enable and Validate

Start in classification-only mode after Pebble is publishing v1 frames:

```dotenv
LIFEOS_PEBBLE_CAPTURE_ENABLED=true
LIFEOS_PEBBLE_CAPTURE_APPLY=false
```

Restart with `./scripts/server.sh restart`, then inspect the local
`data/pebble_capture.db` receipt state and opaque API log statuses. No Tasks
or Scheduler entries are written in this mode. After a synthetic ready record
has been reviewed, set `LIFEOS_PEBBLE_CAPTURE_APPLY=true` and restart.

File events feed one bounded, coalescing queue and one serial consumer. The
watcher debounces events for two seconds, queues a startup scan without
blocking API startup, and rescans every 60 seconds. This recovers missed
events, older-day uploads, and restart gaps without creating a timer or worker
per file. Shutdown rejects new events, cancels queued work, and drains the one
active consumer before reporting stopped. A changed final revision is held
for review rather than silently reclassifying or recreating effects; a pending
raw revision creates no receipt, so a later ready reconciliation can proceed.

## Filing Rules

- Whether a plain (non-delegated) task gets filed is the classifier's
  judgment call under the filing policy prompt
  (`api/services/journal_filing_policy.py`): log-only is the strong default,
  and a task is filed only when the speaker actively asks for one, not for a
  bare imperative, an observation, a musing, a plan, a hedge, or a
  reminder-to-self in passing. When a capture asks for one thing and keeps
  talking, only the item actually asked for should file, not everything that
  follows. Application code applies no authority gate to a plain task; it
  files exactly what the classifier proposes. `scripts/eval_pebble_filing.py`
  scores the configured classifier against a worked TASK/LOG example table
  and is how this calibration gets re-checked after a model swap.
- Ordinary timed reminders create `notify` entries in `LifeOS/Scheduler/Inbox.md`.
- An `agent` schedule requires explicit quoted scheduled-execution evidence and
  a valid, visible non-empty `#executor`; blank, invented, and unknown
  executors are rejected.
- A task retains an execution tag only from an explicit positive delegation
  to a current registered executor. Negated, quoted, merely mentioned,
  reported, conditional, unknown, and model-invented tags cannot grant pickup
  authority, including execution sub-tags. Executable titles and scheduled
  agent messages use the exact source action span, so classifier paraphrasing
  cannot widen or redirect the delegated work. This authority gate is
  independent of the classifier's plain-task filing judgment above.
- A delegated task and every agent schedule require action_evidence and
  delegation_evidence: exact unquoted words copied verbatim from the
  transcript. A structurally incomplete classification (missing evidence, a
  malformed response) gets one bounded repair attempt; the replacement must
  supply exact source evidence and pass the same deterministic authority
  checks, and application code never fills missing delegation fields. A
  proposed task whose execution tag fails the delegation checks is corrected
  (the tag dropped, the task otherwise still filed) rather than granted
  pickup authority it never proved, and a second invalid response leaves the
  capture pending.
- Classifier-proposed titles and messages cannot inject task/schedule Markdown
  fields, routing tags, comments, or line separators. The captured text stays
  quoted producer evidence and never becomes parser metadata.
- Filing cannot authorize email, calendar, shell, endpoint, prompt, immediate
  agent work, or generic chat tools.
- An explicit list request ("make tasks to X, Y, and Z") files each requested
  item as its own unparented task, in transcript order; a plan asking for a
  parent to-do with sub-tasks ("a project to X with sub-tasks A and B") files
  X as the parent and links each other requested item to it via
  `parent_index`. Only a plain to-do gains these shapes -- a delegated task or
  a schedule still files one per capture, and hierarchy stays one level deep
  (a child action cannot itself be a parent). A capture never yields more
  than eight actions.

Each effect has an operation key from `(source.id, capture_id, action index)`.
The ledger claim plus the Markdown operation field lets a retry find a prior
task or schedule after a crash between the Markdown commit and ledger receipt.
If the object is absent after an expired claim, its outcome is ambiguous: the
consumer holds the capture instead of guessing whether the write never landed
or an operator deleted it. Completed objects are likewise never recreated
after operator edits or deletion. A persisted one-time schedule which elapses
before apply is held and creates no dead Scheduler entry.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LIFEOS_PEBBLE_CAPTURE_ENABLED` | `false` | Start the archive watcher and classifier. |
| `LIFEOS_PEBBLE_CAPTURE_APPLY` | `false` | Permit canonical task/schedule/card writes after dry-run validation. |
| `LIFEOS_PEBBLE_CAPTURE_DIR` | `LifeOS/Log/Pebble` | Producer-owned archive location; only this directory is accepted. |
| `LIFEOS_PEBBLE_CAPTURE_SCAN_SECONDS` | `60` | Periodic recovery scan interval (minimum 10). |
| `LIFEOS_PEBBLE_CLASSIFIER` | `llm` | Which classifier files a capture: `llm` (`PebbleJournalClassifier`, below) or `jev` (`JevPebbleClassifier`, TypeSafe's typed-judgment API; requires `TYPESAFE_API_KEY`). |

`PebbleJournalClassifier` (`api/services/pebble_capture.py`) routes to the
configured remote provider (`LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY`, see
[ADR-024](../adr/024-remote-llm-backend.md)) when configured, else the local
llama-server; a keyless install with no remote provider configured always
uses the local llama-server, which must be a loopback URL.

With `LIFEOS_PEBBLE_CLASSIFIER=jev` and `TYPESAFE_API_KEY` set,
`JevPebbleClassifier` segments the transcript in code and asks TypeSafe's
Jev API which disposition, item, work fragment, and executor apply, in one
call; its proposed actions pass through the same `validate_plan` authority
gate as the LLM classifier. This sends the capture's transcript to
TypeSafe instead of the configured remote provider -- an operator choice,
made by setting `LIFEOS_PEBBLE_CLASSIFIER=jev` explicitly. A missing key
falls back to `PebbleJournalClassifier` with a logged warning, never a
failed capture.

`JevPebbleClassifier` files a recurring reminder ("every morning", "weekly",
"on weekdays", ...) log-only rather than as a schedule: `parse_contextual_time`
only ever resolves a single instant, so it can't represent a recurrence, and
filing one anyway would silently collapse it into a single one-time reminder
at whatever hour happened to parse. `PebbleJournalClassifier` has no such
limit -- the model emits a `cron` schedule directly, so it still files
recurring reminders as schedules.

When the Jev classifier files a task, it also asks Jev whether the task is
software work; at 0.7 confidence or above the task carries the `software`
tag and, when Jev's location judgment is itself confident (>= 0.6) about a
concrete project rather than the vault or home, `fields.project` names it.

## Verification Matrix

| Concern | Evidence |
|---|---|
| Framed ready-only input; producer archive untouched | `tests/test_pebble_capture.py` frame, tamper, pending-to-ready, uncertain, and watcher cases |
| Startup/periodic/debounced recovery | `PebbleCaptureWatcher`; bounded queue, coalesced scan, atomic move-in, shutdown drain, health, and read-only watcher cases |
| Remote-preferred, local-fallback classification and dry run | `PebbleJournalClassifier`; remote-routing, local-client, and dry-run consumer cases |
| Journal/Pebble capability difference | shared note/task/reminder cases in `journal_filing_policy.py`, native clarification regression, and Pebble effect matrix |
| Plain-task filing is the classifier's judgment, re-checked by eval | `scripts/eval_pebble_filing.py`; index-based classifier/validated-action pairing regression; a log-only capture still completes |
| Relative time, timezone, elapsed trigger safety | `validate_plan`; local-time, DST gap/overlap, offset mismatch, and saved-plan elapsed cases |
| Explicit assignment and schedule action gate | source-scoped evidence validation; positive paraphrase, negated, quoted, reported, conditional, mentioned, unknown, and Markdown-rebuild pickup cases |
| Crash, restart, duplicate event, and revision conflict recovery | ledger consumer crash/replay and ambiguous-deletion cases; thread and process operation-key tests |
| Human queue lifecycle | stable-key filing through `human_queue.add_card`, replay deduplication, and existing resolve-by-key transition |
| Golden producer conformance | copied `tests/fixtures/pebble-result-*-v1.json` plus fixture-frame tests |

The full repository suite remains the merge gate. Run `./scripts/test.sh` from
the LifeOS worktree after any change to this consumer. Run
`~/.venvs/lifeos/bin/python scripts/eval_pebble_filing.py` after a classifier
model swap to re-check its filing judgment; it is a standalone tool, not a
test-suite gate, since it makes a real network call.

## Related Documents

- [Journal Ring Ingest](journal-ring-ingest.md) — The webhook a ring device posts to; this guide covers the file-watcher capture path and its filing controls.
- [Configuration](configuration.md) — Authoritative environment-variable reference.
