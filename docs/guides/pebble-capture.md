# Pebble Capture Filing

**Status:** Complete
**Last Updated:** 2026-09-09
**Audience:** Operators

Pebble owns `LifeOS/Log/Pebble/YYYY-MM-DD.md`. LifeOS reads its framed result
records and never rewrites the archive, transcripts, or Pebble's private
SQLite/audio spool. Only a complete v1 `result` frame with `status: ready` is
eligible for filing; partial, invalid, uncertain, pending, and failed records
remain evidence only.

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

- Clear to-dos create canonical `LifeOS/Tasks/Inbox.md` entries.
- Ordinary timed reminders create `notify` entries in `LifeOS/Scheduler/Inbox.md`.
- An `agent` schedule requires explicit quoted scheduled-execution evidence and
  a valid, visible non-empty `#executor`; blank, invented, and unknown
  executors are rejected.
- A task retains an execution tag only from an explicit positive delegation
  to a current registered executor. Negated, quoted, merely mentioned,
  reported, conditional, unknown, and model-invented tags cannot grant pickup
  authority, including execution sub-tags. Executable titles and scheduled
  agent messages use the exact source action span, so classifier paraphrasing
  cannot widen or redirect the delegated work.
- A structurally incomplete local classification gets one bounded local repair
  attempt. The replacement must supply exact source evidence and pass the same
  deterministic authority checks; application code never fills missing
  delegation fields, and a second invalid response leaves the capture pending.
- Classifier-proposed titles and messages cannot inject task/schedule Markdown
  fields, routing tags, comments, or line separators. The captured text stays
  quoted producer evidence and never becomes parser metadata.
- Filing cannot authorize email, calendar, shell, endpoint, prompt, immediate
  agent work, or generic chat tools.

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
| `LIFEOS_PEBBLE_CAPTURE_ENABLED` | `false` | Start the archive watcher and local classifier. |
| `LIFEOS_PEBBLE_CAPTURE_APPLY` | `false` | Permit canonical task/schedule/card writes after dry-run validation. |
| `LIFEOS_PEBBLE_CAPTURE_DIR` | `LifeOS/Log/Pebble` | Producer-owned archive location; only this directory is accepted. |
| `LIFEOS_PEBBLE_CAPTURE_SCAN_SECONDS` | `60` | Periodic recovery scan interval (minimum 10). |

## Verification Matrix

| Concern | Evidence |
|---|---|
| Framed ready-only input; producer archive untouched | `tests/test_pebble_capture.py` frame, tamper, pending-to-ready, uncertain, and watcher cases |
| Startup/periodic/debounced recovery | `PebbleCaptureWatcher`; bounded queue, coalesced scan, atomic move-in, shutdown drain, health, and read-only watcher cases |
| Local-only bounded classification and dry run | `LocalOnlyJournalClassifier`; dry-run consumer case |
| Journal/Pebble capability difference | shared note/task/reminder cases in `journal_filing_policy.py`, native clarification regression, and Pebble effect matrix |
| Relative time, timezone, elapsed trigger safety | `validate_plan`; local-time, DST gap/overlap, offset mismatch, and saved-plan elapsed cases |
| Explicit assignment and schedule action gate | source-scoped evidence validation; positive paraphrase, negated, quoted, reported, conditional, mentioned, unknown, and Markdown-rebuild pickup cases |
| Crash, restart, duplicate event, and revision conflict recovery | ledger consumer crash/replay and ambiguous-deletion cases; thread and process operation-key tests |
| Human queue lifecycle | stable-key filing through `human_queue.add_card`, replay deduplication, and existing resolve-by-key transition |
| Local privacy boundary | direct `LocalLLMClient` use, opaque watcher logs, no remote fallback |
| Golden producer conformance | copied `tests/fixtures/pebble-result-*-v1.json` plus fixture-frame tests |

The full repository suite remains the merge gate. Run `./scripts/test.sh` from
the LifeOS worktree after any change to this consumer.

## Related Documents

- [Configuration](configuration.md) — Authoritative environment-variable reference.
