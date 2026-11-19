# Pebble Capture Filing

**Status:** Complete
**Last Updated:** 2026-11-19
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

## Addressed Captures (Hand-off)

A capture that opens with the operator's configured phrase is a message for
whoever the phrase names, not a journal entry. With
`LIFEOS_PEBBLE_HANDOFF_PHRASE` and `LIFEOS_PEBBLE_HANDOFF_COMMAND` both set,
such a capture skips classification and the rest of its text is piped, once,
to the command on stdin. It files no task, schedule, or human-queue card. Pebble's
own log and digest are untouched, as for every capture.

- **Matching** (`api/services/pebble_handoff.py`): the phrase must open the
  capture, in any capitalization, with hyphens or spaces between its words,
  and any punctuation after it; up to three leading filler words ("um",
  "okay so", "hey", ...) are skipped. A capture that only mentions the phrase
  later, uses it as a possessive ("<phrase>'s ..."), or has nothing after it
  is an ordinary capture.
- **Command contract**: the command is split into words shell-style (no
  shell runs), gets the message on stdin plus `LIFEOS_HANDOFF_RECORDED_AT`
  (the capture's UTC timestamp) and `LIFEOS_HANDOFF_KEY` (stable per
  capture) in its environment, and must exit 0 within 60 seconds. Its output
  is never logged.
- **Failure**: a non-zero exit, timeout, or missing command leaves the capture
  pending; the next recovery scan (every `LIFEOS_PEBBLE_CAPTURE_SCAN_SECONDS`)
  retries it until the command succeeds. It is never filed as a task instead.
- **At most once**: the send is a ledger effect like a task. A crash after the
  command returned is recovered as sent; a crash while it was running leaves
  the outcome unknown, so the capture is held (`ambiguous_outcome`) rather
  than sent twice.
- In classification-only mode (`LIFEOS_PEBBLE_CAPTURE_APPLY=false`) the
  hand-off is planned in the ledger but not sent.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LIFEOS_PEBBLE_CAPTURE_ENABLED` | `false` | Start the archive watcher and classifier. |
| `LIFEOS_PEBBLE_CAPTURE_APPLY` | `false` | Permit canonical task/schedule/card writes after dry-run validation. |
| `LIFEOS_PEBBLE_CAPTURE_DIR` | `LifeOS/Log/Pebble` | Producer-owned archive location; only this directory is accepted. |
| `LIFEOS_PEBBLE_CAPTURE_SCAN_SECONDS` | `60` | Periodic recovery scan interval (minimum 10). |
| `LIFEOS_PEBBLE_HANDOFF_PHRASE` | empty | Phrase that addresses a capture to the hand-off command (above). Empty disables the hand-off. |
| `LIFEOS_PEBBLE_HANDOFF_COMMAND` | empty | Command that receives an addressed capture's message on stdin. Empty disables the hand-off. |
| `LIFEOS_PEBBLE_CLASSIFIER` | `llm` | Which classifier files a capture: `llm` (`PebbleJournalClassifier`, below) or `jev` (`JevPebbleClassifier`, TypeSafe's typed-judgment API; requires `TYPESAFE_API_KEY`). |

`PebbleJournalClassifier` (`api/services/pebble_capture.py`) routes to the
configured remote provider (`LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY`, see
[ADR-024](../adr/024-remote-llm-backend.md)) when configured, else the local
llama-server; a keyless install with no remote provider configured always
uses the local llama-server, which must be a loopback URL.

With `LIFEOS_PEBBLE_CLASSIFIER=jev` and `TYPESAFE_API_KEY` set,
`JevPebbleClassifier` segments the transcript in code and asks TypeSafe's
Jev API which disposition, item, work fragment, executor, and assignee
apply, plus which title each fragment carries, in one call; its proposed
actions pass through the same `validate_plan` authority gate as the LLM
classifier. This sends the capture's transcript to
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

Whether a capture files at all rests on two Jev signals from the same call.
The `disposition` choice decides when a filing disposition reaches 0.5
confidence. Otherwise a `filing_request` probability (the speaker asked for
something to be recorded as a to-do or reminder) of 0.7 or above rescues the
capture -- but only while the disposition's own log-only probability is
present and below 0.5, and only ever as a plain to-do, the speaker's or
unassigned: never a reminder or agent work. This catches terse requests
("put this on my plate: ...") without filing reflections or thinking aloud.

Who a Jev-filed task is assigned to is Jev's judgment, not a phrasing
match: Jev chooses among nobody, the speaker, and each AI agent. At 0.7
confidence or above the judgment decides -- the speaker files the task
tagged `me`, and the plan records `assignee_source="jev"` with Jev's
confidence as the assignment's evidence. Below the floor, or on a missing or
malformed answer, the explicit "assign it to me" wording and the `executor`
answer decide instead.

Every agent-executable outcome of a Jev plan -- a delegated task on any
path, including the `executor` fallback, and an agent schedule -- needs two
independent locks, both enforced by `validate_plan` for a Jev-classified
capture: the agent's name (any alias in `EXECUTOR_ALIASES`) in the same
sentence as the to-do and outside the to-do's own span, and a second,
targeted Jev call -- made only when the first proposes agent work --
confirming at 0.8 or above that the speaker themself asks that agent to do
it (not negated, imagined, or reported), whose probability the plan records
as `agent_confirmation`. These sit on top of the literal delegation-evidence
gates. Failing either, the work files as an unassigned to-do. A capture
therefore makes one Jev call, or two when agent work is proposed. The
accepted residual: reported or negated speech that binds the agent's name
beside the to-do still becomes agent work if both independent Jev judgments
-- the assignee choice and the targeted confirmation -- are wrong about it.

A Jev-filed title is Jev's choice among literal cuts of the chosen fragment:
every span starting within its first 12 words and ending at most 8 words
before its end, so a leading filing request ("make a task assigned to me
to") and a trailing assignee remark ("that one's mine") can each be cut
away. Titles therefore stay exact transcript spans. Below 0.5 confidence the
fragment with its filing request stripped is the title.
`scripts/jev_eval/e7_pebble_filing.py` scores both judgments against a
committed, synthetic-only labeled set of varied phrasings
(`scripts/jev_eval/cases/pebble_filing_cases.jsonl`) with real Jev.

A Pebble note is automatic speech-recognition output, which can mishear the
filing request itself ("at a desk to ..." for "add a task to ..."). Every Jev
call's state therefore carries a `source` field naming the note as a
speech-recognition transcript whose opening request wording may be misheard,
and the disposition, filing-request, item, assignee and title questions and
the targeted agent confirmation say the same in their own wording, while
reported speech stays log-only. The assignee question also counts the speaker
claiming a task ("I'll take this one", "I'm doing it myself") as theirs. Jev
judges what the speaker most likely said; no substitution rule rewrites the
transcript, and a misheard request is simply left out of the literal title
span. The eval set's `asr` and `asr_journal` categories cover misheard
requests and the same words used in their ordinary meaning.

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
| Jev-judged filing, assignee and title | mocked-Jev plain-task-only rescue and log-only guard, agent schedule and executor-fallback locks, self-assignment, unnamed/unbound/unconfirmed agent, Jev-sourced `validate_plan` evidence, literal-title property, speech-recognition state and question wording, and fallback cases; `scripts/jev_eval/e7_pebble_filing.py` against real Jev |
| Crash, restart, duplicate event, and revision conflict recovery | ledger consumer crash/replay and ambiguous-deletion cases; thread and process operation-key tests |
| Human queue lifecycle | stable-key filing through `human_queue.add_card`, replay deduplication, and existing resolve-by-key transition |
| Addressed-capture hand-off | `tests/test_pebble_handoff.py`: phrase variants and non-matches, command contract, send-once with no task, failure retry, dry run, crash recovery, watcher end to end with a real command |
| Golden producer conformance | copied `tests/fixtures/pebble-result-*-v1.json` plus fixture-frame tests |

The full repository suite remains the merge gate. Run `./scripts/test.sh` from
the LifeOS worktree after any change to this consumer. Run
`~/.venvs/lifeos/bin/python scripts/eval_pebble_filing.py` after a classifier
model swap to re-check its filing judgment; it is a standalone tool, not a
test-suite gate, since it makes a real network call.

## Related Documents

- [Journal Ring Ingest](journal-ring-ingest.md) — The webhook a ring device posts to; this guide covers the file-watcher capture path and its filing controls.
- [Configuration](configuration.md) — Authoritative environment-variable reference.
