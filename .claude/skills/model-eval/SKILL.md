---
name: model-eval
description: Benchmark a candidate local model against the incumbent for LifeOS, each tuned to its best. Use when evaluating a new model (any family) as a replacement for the local orchestrator/router, or re-testing one after upstream fixes.
argument-hint: [candidate-gguf-or-hf-repo] [--routing-only|--execution-only]
---

# Model Evaluation

Arguments: `$ARGUMENTS`

Compare a candidate local model against the incumbent **with both tuned to their best**,
then judge fitness for LifeOS specifically. The point is not "which model is smarter" —
it is "which produces better grounded answers over *this* tool surface at acceptable
latency."

> **The one rule that matters.** In the Qwen3.8-27B evaluation, four separate "this model
> fails" verdicts were reached. **Three were misconfiguration** (`--mmproj`, thinking left
> on, a 32K context) and the fourth was **half a LifeOS tool-schema bug**. Budget your time
> accordingly: tuning and harness verification is most of the work. Benchmarking an
> untuned model produces confident, wrong conclusions.

---

## Phase 0 — Tune before you measure

Do all of this *before* recording a single number. Each item below cost a full wasted
benchmark round when skipped.

### Context window — check first, it silently voids results
The agentic loop accumulates tool results across rounds; a request that fits at round 1
will not at round 5. Symptom: `request (N tokens) exceeds the available context size`,
returning HTTP 400 mid-run. Two of five questions died this way at `-c 32768` and the
whole run was void.

Set context as large as VRAM allows and **verify by running the longest question**, not by
reading the model card. Fall back stepwise (`131072 → 65536 → …`) if the server refuses to
start — a too-large `-c` fails at load, which is loud and safe.

### Reasoning level — not a boolean, and per-path
Modern models expose graded effort (`xhigh`/`medium`/`low`, plus `--reasoning-budget N`)
and/or `chat_template_kwargs: {"enable_thinking": false}`. Only ever testing on/off hides
the setting that works.

**Reasoning is a prefix the answer competes with, not an addition to it.** A starved answer
looks like a model failure and is not.

The optimal level *differs by path* — do not assume one setting fits both:

| Path | Setting | Why |
|---|---|---|
| Router (`query_router._llm_route`) | thinking **off** | Classification; CoT adds nothing. 8x faster, correctness unchanged. |
| Agentic loop (`agent_loop.run_agent_loop`) | **some** reasoning | Needs it to terminate cleanly; fully off ran to the token cap. `low` gave 6/6 natural stops where the default gave 4-of-6 empty. |

### Speculative decoding / multi-token prediction
If the GGUF ships MTP/`nextn` tensors, they are **off unless requested**:
`--spec-type draft-mtp --spec-default --reasoning-preserve` (no separate draft model —
it drafts from the model's own head).

**Do not read `blk.N.nextn.* unused tensor -- ignoring` as "unsupported".** That warning
means *you did not pass the flag*. Reading it as an upstream gap cost a full round and a
wrong entry in the issue. With the flag the log reads `creating MTP draft context against
the target model`. Worth ~30% decode.

Diminishing returns are real: draft depth 6 measured *slower* than the default 3. Sweep,
don't assume.

### Projector (`--mmproj`) — load-bearing in a non-obvious way
On text-only work the projector inflated reasoning ~6x (1557 vs 245 tokens, identical
`-c`). Tempting to drop it. **Do not drop it without testing the agentic path** — the
incumbent without `--mmproj` runs away to the token cap on *execution* while looking fine
on routing. Validate any projector change on execution, not routing.

### Environment parity
`ROCBLAS_USE_HIPBLASLT=1` is set for the incumbent by systemd. A manual launch without it
is not a fair comparison. Check the unit file for anything else before hand-rolling a
server.

---

## Phase 1 — Verify the harness hits the surface you think it does

**LifeOS has two independent tool catalogs.** Fixing one does not affect the other:

| Surface | Source of tool schemas | Used by |
|---|---|---|
| Agentic chat | `api/services/agent_tools.py` → `TOOL_DEFINITIONS` | `run_agent_loop` — web chat, Telegram, voice, **and the execution benchmark** |
| MCP | route OpenAPI spec → `mcp_server._build_input_schema` | Claude Code, Managed Agents |

A fix to the MCP schema will not move an `agent_loop` benchmark by one character. Confirm
which catalog your harness exercises before running anything, and re-verify after any
"fix" you make between rounds.

### The tool surface confounds the model comparison
A misleading tool description **punishes the better instruction-follower**. Observed: the
schema advertised context values (`'Work'`, `'Personal'`) that no vault had, and said
nothing about what omitting `status` does. The model that trusted the schema filtered to
zero rows, fell back to an unfiltered call returning every task of every status, and
reported a partial list confidently. The model that ignored the hint guessed
`status='todo'` and was right.

**Before concluding model A retrieves worse than model B, read the tool descriptions both
of them read.** Retrieval failures are tool-surface bugs until proven otherwise.

---

## Phase 2 — Benchmark design

### Two axes, different currencies
- **Routing** — classification against labelled cases. Currency: *latency + correctness*.
  Use the real `config/prompts/query_router.txt`.
- **Execution** — multi-step agentic questions through `run_agent_loop`. Currency:
  *answer quality*. This is the one users wait on and the one that decides the verdict.

Gate on **both**. An early decision rule here gated only routing latency and would have
green-lit a model 3.4x slower on execution.

### Score qualitatively, never binary
Several questions "passed" a pass/fail check while being empty, off-topic, or confidently
wrong. Read the answers. Specifically check:

- **Grounding** — does it match reality? Anchor to a ground truth pulled straight from the
  API (`curl /api/tasks?status=todo` → N) rather than judging plausibility.
- **Completeness vs confidence** — the worst failure is a *partial* answer stated as
  complete ("only two open tasks left — everything else is done"). Rank that far below an
  honest "I couldn't find X."
- **Did it answer the question asked?** One model produced an excellent answer to a
  *different* question in the set.
- **Signal vs padding** — an answer listing calendar events and notification chatter as
  "waiting on a reply" is worse than a shorter, correct one. Length is not quality.

### Run discipline
- **Sequentially, one model resident at a time.** Concurrent runs contend for bandwidth
  and produced a 729.5s / 31-char artifact that nearly became a finding.
- **N ≥ 3 trials per question**, compare medians. These runs are stochastic: between two
  rounds the incumbent's answers dropped 1838→527 and 800→248 chars on questions the
  intervening change could not possibly affect. **A single run per cell cannot separate a
  real delta from variance** — with N=1, report direction and caveat it, never a scorecard.
- **Re-run both models** after any change to prompts or tool schemas. The incumbent's
  baseline moves too.

---

## Phase 3 — Red flags that mean "re-examine the setup", not "write the verdict"

| Signal | Almost always means |
|---|---|
| N queries landing at *exactly* the client timeout | timeout budget, not model speed |
| Different questions returning *identical* output lengths | hit a token cap |
| Empty answer with `reasoning_content` populated | reasoning starvation — raise budget or lower effort |
| HTTP 400 partway through a multi-round run | context window |
| A result contradicting a pre-registered estimate by >3x | harness bug |
| Model narrates a tool problem ("the filter matched literally") | **believe it** — it is describing a real schema/tool defect |

That last one is the cheapest signal available and was ignored once. Models often
diagnose the tool surface correctly in their own output.

---

## Phase 4 — Decision rule

Pre-register the bar **before** running, and write it down. Then:

1. Both paths tuned to their best, verified by the Phase 0 checks.
2. Routing within ~1.5x of incumbent latency, correctness ≥ incumbent.
3. Execution: quality ≥ incumbent on median-of-N, and latency within a factor the user has
   agreed to wait.
4. If ambiguous — **report, do not swap.** Ambiguity plus a large latency penalty is a no.

Do not swap on speed alone, and do not swap on prose quality alone. LifeOS's job is
grounded retrieval and synthesis over personal data; a model that writes better but
retrieves worse is the wrong trade.

---

## Phase 5 — Operational safety

The box is a single always-on host running the live API. Do not destabilize it.

- **Never run two models at once.** Stop the incumbent, run the candidate, restore.
- **Restore from an `EXIT` trap**, so a crash, a bad load, or a kill still puts the
  incumbent back. Verify `/health` after restore, and check `systemctl is-active`.
- **`kill -9` fallback** — a llama-server survived a plain `kill` and held ~17 GB of VRAM
  until reaped by hand. Wait on the PID, then escalate.
- **Test suites CPU-only:** `HIP_VISIBLE_DEVICES="" ROCR_VISIBLE_DEVICES="" CUDA_VISIBLE_DEVICES=""`.
  Embedding load alone has locked up the GPU and rebooted this host.
- **sudo:** `restart` is *not* allowlisted for `lifeos-llm` — use `stop` then `start`.
  Other units allow restart. Claude Code's `!` prefix has no TTY, so nothing interactive.
- **Never edit `.env`** for an experiment — it is a symlink into a Syncthing-synced dir.
  Pass settings as launch flags or monkeypatch in the harness.
- **Self-matching `pgrep`** — a waiter loop containing its own search string matches
  itself and never exits. Match on something the waiter does not contain.

## Scripts

`scripts/swap_and_run.sh` — stop incumbent → start candidate (with context fallback) →
run the question set → always restore. `scripts/qrun.py` — runs the question set through
the real `run_agent_loop`, patching per-path reasoning settings.

Both take the model path and label as arguments; neither hardcodes a model family.

## Reporting

Post measurements to a tracking issue, and **keep personal data out of it** — this repo is
public. Counts and latencies are fine; task text, contact names, and email subjects are
not. Score tables and mechanism descriptions carry the argument without the content.

State plainly which round each conclusion came from, and retract superseded ones by name —
several verdicts in the Qwen evaluation were reversed, and an issue thread that only
accumulates claims becomes unusable.

## Log of false starts (Qwen3.8-27B, Aug 2026)

Each of these produced a confident wrong conclusion. They are the reason Phase 0 exists.

| # | Concluded | Actually was | Cost |
|---|---|---|---|
| 1 | "Cannot meet the latency contract — 9/12 routing timeouts" | Thinking left ON. Off: 24.65s → 1.3-3.0s | full round |
| 2 | "llama.cpp drops the MTP head — wait for upstream" | Never passed `--spec-type draft-mtp`. The `unused tensor` warning means the flag is missing | full round + wrong issue entry |
| 3 | "Fails 2 of 5 execution questions" | `-c 32768` too small for 5 rounds of accumulated tool results; HTTP 400 mid-run | full round |
| 4 | "Removing `--mmproj` is a free 1.8x win" | True on routing, breaks the incumbent's agentic termination | retracted before shipping |
| 5 | "Retrieves worse — found 2 of 6 open tasks" | Substantially a tool-schema bug that punished the model for trusting it | skewed the verdict |
| 6 | "Quality is config-dependent, not bandwidth-dependent" | Ran both models concurrently; contention produced 729.5s / 31-char output | redone sequentially |
| 7 | Filed the schema bug against `_fallback_schemas()` | That path never reaches a model; the live schema comes from the OpenAPI spec | one wasted PR scope |
| 8 | Fixed the MCP schema, planned to re-benchmark | The benchmark uses `agent_tools.TOOL_DEFINITIONS`; MCP fix moves nothing | caught before re-running |

Two meta-lessons:

- **The candidate was blamed four times and was substantially the cause zero times.** When
  a well-reviewed model performs far below its reputation, the prior should be
  misconfiguration, not a bad model.
- **Fixing the confound does not automatically flip the verdict.** After the schema fix the
  candidate's retrieval measurably improved (0 → 4 of 6 open tasks named on one question),
  and it still lost on latency. Fix the confound so the comparison is *fair*, not to make a
  preferred model win.

## Related

- `docs/specs/technical/agent-worker.md`, `docs/specs/technical/architecture.md`
- Worked example, including every reversal: GitHub issue #567
