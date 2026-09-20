# Jev orchestrator offline experiments (issue #1158)

Reproducible scripts for E0–E5 of #1158: measuring whether typed judgments
from TypeSafe's Jev model would help the `/chat` orchestrator, against
recorded real turns, before any control-flow change ships. Full results and
verdicts are in `~/.claude/lifeos-jev/gate5-report.md` (not in this repo —
it may reference aggregate counts derived from personal data and has no
reason to be checked in).

## Vendored client vs. the real one

`_jev.py` is a small vendored client, written because `api/services/
jev_client.py` (#1157) hadn't landed on `feat/jev` when these experiments
started. It has since landed (#1159/#1160, plus a first production caller
in #1161). These scripts still use the vendored client — all six
experiments were run and the report finalized against it before the branch
was fast-forwarded, and it exercises the same public API contract
(`POST /v1/systemone`, same question/answer shapes) so the results aren't
affected. Swapping to `api.services.jev_client.JevClient` is a reasonable
follow-up if this suite is rerun (e.g. for E6 drift comparison), but wasn't
done here to avoid re-spending Jev budget re-verifying already-measured
results for a change with no effect on them.

## Privacy

All datasets are written to `data/jev_eval/` under the checkout each
script's own `__file__` resolves to (`Path(__file__).resolve().parents[2]`
-- no hardcoded path, so this works from any clone). `data/` is gitignored,
so writing here never lands a dataset somewhere that could get committed.
Note that `perf_traces.db`/`conversations.db` only exist where LifeOS
actually runs with real data -- run these scripts from that checkout (a
fresh worktree's `data/` directory is empty). Never commit anything under
`data/`. Never print message text to your own terminal/log beyond what a
specific hand-labeling step requires.

## Running

Requires `~/.venvs/lifeos/bin/python` (repo venv — has numpy/scipy/sklearn)
and `TYPESAFE_API_KEY` (env var, or falls back to
`~/Code/Sync/envs/LifeOS/.env`). Run in order; each is idempotent (reuses
its cached `*_raw.jsonl`/results unless you pass `--force`):

```
python scripts/jev_eval/e0_extract.py     # local only, no Jev calls, free
python scripts/jev_eval/e1_preturn.py     # ~981 Jev calls, ~$0.03
python scripts/jev_eval/e2_inloop.py      # ~637 Jev calls; first run writes
                                           # e2_gold_review.jsonl and exits --
                                           # hand-label it into e2_gold.jsonl,
                                           # then re-run to get metrics
python scripts/jev_eval/e3_bundles.py     # local only, free
python scripts/jev_eval/e4_policy.py      # local only, free (reads e1-e3 results)
python scripts/jev_eval/e5_latency.py     # 500 Jev calls, ~$0.02
```

## Shared dataset (E0)

`e0_extract.py` joins `data/perf_traces.db` (span-level latency/tool
timing) with `data/conversations.db` (message text, tool names+args via the
`routing`/`sources` columns) into `data/jev_eval/e0_dataset.jsonl`, one row
per agentic chat turn. Read its module docstring before trusting any field
— it documents exactly what's available, what isn't (tool RESULT content
doesn't exist anywhere queryable for historical turns), and the join logic
including two bugs it works around (a round-span rename from
`claude_api_round_N` to `llm_api_round_N`, and trace-vs-message timestamp
ordering).

## Tool → family map (used by E1, E3)

The issue names 9 families: comm, calendar, people/crm, vault, finance,
tasks/reminders/schedules, fitness, web, home. LifeOS's 25 tools
(`api/services/agent_tools.py`'s `TOOL_DEFINITIONS`) map onto them as:

| Family | Tools |
|---|---|
| comm | `search_email`, `search_slack`, `get_message_history`, `create_email_draft`, `send_email_draft` |
| calendar | `search_calendar`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` |
| people_crm | `person_info` |
| vault | `search_vault`, `read_vault_file`, `save_memory`, `search_memories`, `search_drive` |
| finance | `search_finances` |
| tasks | `manage_tasks`, `manage_reminders`, `manage_schedules`, `manage_human_queue` |
| fitness | `manage_workouts` |
| web | `search_web` |
| home | `pause_internet`, `resume_internet`, `internet_status` |

`search_drive` has no obvious home in the issue's 9 names; bucketed under
`vault` (closest fit — a documents/knowledge source) rather than given its
own family. This is a judgment call, not a fact from the issue.

## Per-tool token sizes (chars/4 approximation, per the issue's own method)

Full 25-tool schema: **~7322 tokens** (issue claimed ~7300 — confirmed).
Individual sizes range from `internet_status` (~61 tokens) to
`manage_workouts` (~1213 tokens) — the consolidated multi-action tools
(`manage_workouts`, `manage_tasks`, `manage_schedules`) are the largest
since they carry every sub-action's schema in one definition. See
`e3_bundles.py`'s `TOOL_TOKENS` dict for the exact per-tool figures, or
`data/jev_eval/e3_results.json` after running it.

## `tools_for_persona` / bundle pattern precedent

`api/services/agent_worker/tool_filter.py` already does per-class tool
filtering for the agent worker (not `/chat`): a fixed set of "cross-cutting"
tools merged into every class, plus per-class specialties. E3's bundles
follow the same shape (a fixed floor — `search_web`, `search_vault`,
`person_info`, per the issue — merged into every bundle) but are derived
from measured `/chat` tool co-occurrence rather than hand-authored.

## Orchestrator facts (verified against code, not taken on faith)

- `api/routes/chat.py:1240` calls `run_agent_loop(..., max_tool_rounds=5,
  ...)`. The pre-turn pipeline (intent classification, query expansion)
  runs at `chat.py:1068-1255` — `E1`'s `is_followup` reproduces
  `expand_followup_query` from `api/services/chat_helpers.py:118` exactly
  (the primary substring-based path; a secondary
  `conversation_context`-based expansion for person references at
  `chat.py:1126-1136` is not reproduced).
- `api/services/agent_loop.py:693` (`tools = tools_for_persona(persona_id)`)
  fixes the tool list for the whole turn — confirmed unchanged across
  rounds. Tool execution and result logging is at `:815-846`
  (`tool_calls_log`, in-memory only — see the data-gap note above).
- `api/services/agent_tools.py:999` puts the cache breakpoint
  (`cache_control: {type: ephemeral}`) on the LAST tool definition,
  meaning the entire 25-tool block is one cached unit today. This is why
  E4 treats `needs_tools` pruning as cache-neutral (removing tools for one
  turn doesn't touch the shared cached block other turns still use) but
  flags bundle-switching (E3/E4 policy b) as a real cache-miss risk (a
  bundle's tool list is a different byte sequence than the full 25-tool
  block, so a turn using a bundle can't hit that cache at all, and
  switching bundles between turns can't hit each other's cache either).
