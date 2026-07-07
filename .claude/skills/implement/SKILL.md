---
name: implement
description: Full implementation lifecycle with adversarial review using subagents
argument-hint: <task-description or #issue-number> [instructions]
---

# Implement

Orchestrate the full implementation lifecycle for: $ARGUMENTS

## Context

- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`
- Arguments: $ARGUMENTS

## Instructions

This skill runs five phases. You **MUST** use task tracking (TaskCreate/TaskUpdate) throughout to track progress and surface status to the user.

**Task tracking rules:**
1. **Bootstrap immediately.** Before starting work, create a task for each phase you will execute using TaskCreate. Each task needs a clear `subject` (imperative: "Explore codebase and plan implementation"), `activeForm` (continuous: "Exploring codebase and planning"), and `description`.
2. **One in_progress at a time.** Mark a task `in_progress` before starting it. Mark `completed` the moment it finishes — do not batch completions.
3. **Break down dynamically.** When entering a phase, expand it into granular sub-tasks. When unexpected work surfaces (failing test, unanticipated dependency, new requirement), add new tasks immediately.
4. **Keep the list truthful.** Delete irrelevant tasks. Update descriptions if scope changes. The list must reflect current reality.

**Working with subagents (applies to every spawn below):**
1. **Results must be relayed.** A subagent's plain-text output is invisible to you. Every spawn prompt must end with: "Load SendMessage first (`ToolSearch: \"select:SendMessage\"`), then deliver your full report via `SendMessage({to: \"main\", summary: \"<5-10 words>\", message: \"<full report>\"})` before going idle."
2. **Idle ≠ done.** Idle notifications fire on any pause — a subagent may still have queued work. Treat a subagent as finished only when its SendMessage report arrives. Never edit its worktree or re-dispatch based on an idle ping alone.
3. **Silence ≠ stalled.** Before concluding a subagent stalled, check its branch: `git fetch origin <branch> && git log --oneline -3 origin/<branch>`. Take over only if the branch hasn't moved across two checks AND the subagent confirms it is blocked.
4. **Parallel agents need isolated worktrees** — never two active agents in one working directory (see Phase 2).
5. **Name spawns for auditability:** set `description` to `"<role> PR #<number> round <N> — <lens>"`.

**Entry point — identify the target:**
1. If the leading token of `$ARGUMENTS` is a number or starts with `#`, it is a GitHub issue. Fetch the issue with `gh issue view <number> --comments` to get the full description and acceptance criteria. If the output is empty (a gh quirk on some issues), retry without `--comments`.
2. Otherwise, run `gh pr list --head <current-branch> --json number,title --jq '.[0]'`. If a PR exists, verify it relates to the current task (check title/description alignment). If it does, record the PR number and treat the target as that PR. If it appears unrelated, ignore it.
3. Otherwise, treat the leading portion of `$ARGUMENTS` as a freeform task description.
4. **Scope stays on the named target.** If the issue is part of an epic (linked issues, or the body lists dependencies/follow-ups), implement only the named issue by default — surface the full set and ask the user before expanding to sibling issues.

**Trailing instructions — scope the lifecycle:**

Any text after the leading token controls which phases run. Parse it before bootstrapping tasks so the task list reflects only the phases you will execute.

| Instructions | Effect |
|--------------|--------|
| *(none)* | Full lifecycle (default): Phase 1 → 2 → 3 → 4 → 5 |
| `quick` or `no-review` | Skip Phase 4 entirely — Phase 1 → 2 → 3 → 5 |
| `no-plan` | Skip Phase 1 — go straight to Phase 2 |
| `review-only` | Run Phase 4 only on the target PR (target MUST be a PR number) |
| `--base <branch>` | Open and merge the PR against `<branch>` instead of `main`, and diff adversarial review against it (see below) |
| Any other text | Interpret intent. Do more rather than less — the full lifecycle is always safe. |

Modifiers compose (e.g. `no-plan quick` skips Phase 1 and Phase 4). If the target is an existing PR and no instructions are given, skip to Phase 4.

**Base branch.** Parse `--base <branch>` from the trailing instructions; if absent, the base is `main`. The base is the branch the PR targets and merges into. Record it as `<base>` and thread it through Phase 2 (branching point + review diff), Phase 3 (`gh pr create --base`), and Phase 5 (`merge-pr`). With no `--base`, every step below is byte-identical to targeting `main`. This exists so stacked sub-PRs can land on a long-lived integration branch (off `main`) before a single PR merges that branch to `main`.

---

### Phase 1: Understand & Plan

1. **Explore.** Use Glob, Grep, Read to understand relevant modules, existing patterns, test structure. Read any specs or ADRs referenced by the task.
   - **Credit existing context.** If this conversation already contains substantial exploration of the target modules, treat Phase 1 as substantially complete — bootstrap tasks reflecting that and do not repeat the reads.
   - **Fan-out exploration (large cross-subsystem tasks).** You may delegate: spawn narrow parallel reader subagents (one subsystem each, returning file:line-cited findings via SendMessage per the subagent rules above), optionally feeding all readings to one synthesizer subagent that writes the briefing from the provided material with no further tool calls.
2. **Define done.** Write verifiable acceptance criteria. Add each as a task via TaskCreate.
3. **Identify test cases.** List tests that pass iff criteria are satisfied. Include edge cases.
   - **Unreproducible flakes:** when the issue frames the fix as hardening ("no reliance on X") and 3–4 reproduction attempts don't trigger the failure, accept the non-reproduction, state it in the PR description, and proceed with the hardening fix — don't keep looping on reproduction.
4. **Plan implementation.** Identify files to create/modify, dependencies, sequencing. Minimum viable approach — AGENTS.md § Simplicity First.

### Phase 2: Implement

1. **Create a branch** if not already on a feature branch: `<type>/<short-description>` per AGENTS.md § Development Workflow. Branch off `<base>` (default `main`): `git fetch origin <base> && git checkout -b <type>/<desc> origin/<base>`. **Do this before opening any file for editing** — working-tree edits made on `main` risk landing on the wrong base or being lost to a concurrent push.
2. **Write tests first** for identified test cases. They should fail until implementation is complete.
3. **Write production code** to make tests pass. Follow existing patterns. Surgical changes only. Read every file before editing it (issue the Reads in parallel; Edit errors on unread files). After an edit that reshapes code, re-read before composing the next edit's old_string — prior edits invalidate stale snippets.
4. **Run the test suite** on the server: `./scripts/test.sh auto` — picks scope (unit/browser/slow/skip) from the git diff and runs it in parallel. All tests must pass before proceeding. **Hard gate: never commit until a green run is in hand — pushing "so tests can run elsewhere" is not a substitute.**
   - **No local venv (the MacBook):** run tests on nathan-linux in an isolated copy — never the live checkout at `~/Code/lifeos`, which may be stale. Push the branch, then: `ssh nathan-linux-ts 'cd ~/Code/lifeos && git fetch origin <branch> && git worktree add /tmp/wt-<branch> origin/<branch> && cd /tmp/wt-<branch> && ~/.venvs/lifeos/bin/python -m pytest -m unit -q'`.
   - **Waiting on long runs:** one blocking call with a long timeout, or one background job waited on with `until grep -qE "passed|failed|error" $OUT; do sleep 5; done`. Never repeatedly Read an unchanged output file; never start a second run before the first finishes. (`grep -c` exits 1 on zero matches — don't chain it with `&&`.)
5. **Self-review your diff.** Read every changed file. Check for: unused imports, style mismatches, missing error handling, changes that do not trace to the task.

**Delegating implementation (3+ files, or parallel issues).** Phase 2 may be delegated to an implementation subagent:
- Create the worktree yourself first: `git fetch origin <base> && git worktree add <path> -b <branch> origin/<base>`. Pass the worktree path, branch, and a precise spec (design decisions, files to modify, test patterns to mirror, test command).
- **Every parallel agent gets its own worktree — never two active agents in one directory.** All the agent's Edit/Write calls must use the worktree's absolute paths; verify a changed symbol appears in the worktree copy, not the main checkout.
- The subagent owns its worktree exclusively until its SendMessage report arrives (idle pings don't count — see subagent rules).
- When it reports, self-verify its diff before Phase 3: read the riskiest hunks against the spec, confirm nothing fails to trace to the task.
- Record the worktree path — Phase 4 reviewer and addresser prompts need it.
- Clean up with `git worktree remove --force <path> && git worktree prune` (not `rm -rf`, which leaves a dangling ref).

### Phase 3: Create PR

1. **Commit** with `<type>: <summary>` format. Separate logical changes into distinct commits.
2. **Push** the branch.
3. **Create the PR** against `<base>` (default `main`). Pass `--base <base>` explicitly — when `<base>` is `main` this is identical to the repo default:
   ```
   gh pr create --base <base> --title "<type>: <imperative summary>" --body "$(cat <<'EOF'
   ## Summary
   <1-3 sentences: what and why>

   <Closes #N / Relates to #N if applicable>

   ## Test evidence
   <test command and result summary>

   ## Review focus
   <areas where review attention is most valuable>
   EOF
   )"
   ```
4. Record the PR number for subsequent phases.
5. **Update linked issues.** If the original task was a GitHub issue (`$ARGUMENTS` started with `#`), post a progress comment on the issue:
   ```
   gh issue comment <N> --body "$(cat <<'EOF'
   ## In Progress

   Implementation PR created: #<pr-number> — <PR title>
   Entering adversarial review phase.
   EOF
   )"
   ```

---

### Phase 4: Review/Address Loop

**Default: 1 round.** Spawn a reviewer subagent, referee findings, address them, then **self-verify** the fixes from the main context. Spawn a Round-2 reviewer subagent **only if** self-verification surfaces something concrete.

Subagent spawns are not free — each costs context and wall time. Reading the diff yourself is the cheaper verification path for most PRs. **Hard limit: 3 rounds total.**

#### Step A: Spawn Reviewer Subagent(s)

Spawn via the **Agent tool** (`subagent_type: "general-purpose"`) — the prompt directs the subagent to invoke the `review-pr` skill itself. Do NOT pre-load the skill content into the prompt — the subagent will load the methodology itself.

**Before spawning:**
- Verify the number is a PR: `gh pr view <number>` must succeed. If it resolves to an issue instead, find the real PR with `gh pr list --head <branch>`.
- Check no round-<N> review already exists on the PR (look for a "Review Round <N>" comment) — if one does, skip straight to Step B using its content. Never spawn a duplicate reviewer for the same round.
- Record which worktree/branch has the PR checked out (`git worktree list`) and note any uncommitted changes (`git status --short`) — if a parallel address pass is in flight, say so in the spawn prompt and instruct reviewers to review the committed diff, not the working tree.

**Choose the review shape:**
- **Focused PRs (< ~200 changed lines):** one reviewer subagent invoking `review-pr` (template below).
- **Large or multi-module PRs:** spawn parallel lens reviewers (correctness, standards, design — as relevant), each with a rich prompt: PR number, worktree path, a summary of the change, the diff (capture once with `gh pr diff <number> > /tmp/pr-<number>.diff`; over ~20KB pass just `--name-only` and let reviewers fetch files selectively), and the specific functions/lines to scrutinize. Lens reviewers return findings via SendMessage; **you post the consolidated raw findings as `gh pr review <number> --comment` in Step C** — that is the audit trail, and it does not happen otherwise.

**Every reviewer prompt must include:**
- Git state: "The PR branch is checked out at `<worktree-path>` — read source there, not the main checkout. Compare to base with `git diff origin/<base>...HEAD`."
- "`Read` takes an integer `offset` and `limit` — never `offset: [start, end]`."
- "If review-pr spawns specialist subagents, review the diff yourself in parallel — do not sleep-poll waiting for them. Send what you have when your own analysis is done; incorporate specialist findings only if they arrive in time."
- The SendMessage relay instruction (see "Working with subagents").

- `description`: `"Review PR #<number> round <N> — <lens>"`
- For the single-reviewer shape, use a short directive that instructs the subagent to invoke the `review-pr` skill with the PR number:

```
You are an adversarial code reviewer for PR #<number>, round <N>.

Run the review-pr skill on this PR:
  Skill tool → skill: "review-pr", args: "<number>"

If round <N> > 1, also fetch `gh pr view <number> --comments` first so you can see previous referee decisions and avoid repeating addressed/rejected findings. Focus on: new issues introduced by fixes, issues missed in prior rounds, and whether previously-addressed findings were actually fixed correctly.

Return findings in this structure:

### Action Required
- **[Category]** Description with specific file:line references

### Recommended
- **[Category]** Description with specific file:line references

### Minor
- **[Category]** Description with specific file:line references

### Summary
<1-2 sentence overall assessment: merge-ready, needs changes, or needs discussion>

Omit any category that has no findings.
```

Findings arrive via the reviewer's SendMessage. If they haven't arrived after one nudge, fall back immediately: `gh pr view <number> --json reviews,comments` for anything posted, then self-review the diff yourself — do not hold indefinitely. Ensure the raw findings land on GitHub as the audit trail: the `review-pr` path posts them itself; for lens reviewers, you post the consolidated findings in Step C.

#### Step B: Referee Evaluation (You — Main Context)

When the reviewer findings arrive, independently evaluate **every finding**. Read the relevant code yourself. Do not rubber-stamp and do not dismiss without checking.

**First, deduplicate:** if multiple reviewers/lenses flagged the same issue, merge them into one entry — note the independent confirmation, keep the most detailed version. Never list the same finding twice in the action plan.

For each finding, decide:

| Decision | When to use | Effect |
|----------|-------------|--------|
| **Accept** | Finding is valid — you verified by reading the code | Include in addresser action plan at reviewer's severity |
| **Downgrade** | Finding has merit but severity is overstated | Include at lower severity with your reasoning |
| **Reject** | Finding is incorrect, irrelevant, or pure style preference | Exclude from action plan; record your reasoning |

**Default postures** (err on the side of accepting):
- **Action Required findings:** Accept unless you can demonstrate the code is correct by reading it.
- **Security findings:** Accept by default. Reject only with concrete evidence that the concern does not apply.
- **Convention findings:** Accept if the code violates a documented standard in AGENTS.md. Reject if it is personal preference not backed by a standard.
- **Vague "consider" / "might" language:** Downgrade to Minor unless you independently agree it matters.

Produce a **filtered action plan** containing only Accepted and Downgraded findings, each with your reasoning.

**Referee mindset:** Think like a principal engineer. Good review isn't just about catching bugs — it's about raising the bar. When the reviewer identifies a legitimate improvement (consolidating duplication, using a more idiomatic API, improving test structure), accept it if it's in scope and doesn't incur technical debt. "Recommended" doesn't mean "optional" — it means "the code would be better for it." Embrace going the extra mile on quality; reject only what is truly out of scope, incorrect, or adds unnecessary complexity.

**If zero findings survive filtering**, post a brief PR comment for the audit trail — `"Review Round <N>: no actionable findings — review loop complete."` — then skip to Phase 5.

#### Step C: Post Referee Decisions to GitHub

**Lens-split rounds:** first post the consolidated, deduplicated raw findings as `gh pr review <number> --comment` (the audit trail the lens reviewers didn't post), then the referee table below as a separate comment.

```
gh pr comment <number> --body "$(cat <<'EOF'
## Review Round <N> — Referee Decisions

| # | Finding | Reviewer Severity | Decision | Reasoning |
|---|---------|-------------------|----------|-----------|
| 1 | <brief description> | Action Required / Recommended / Minor | Accept / Downgrade to X / Reject | <why> |
| ... | ... | ... | ... | ... |

**Findings forwarded to addresser:** <count>
EOF
)"
```

#### Step D: Spawn Addresser Subagent

**Fast path:** if every forwarded finding is Minor or doc-only and the fixes are mechanical (< ~5 lines total, no logic risk), apply them yourself from main context, commit `fix: address review round <N> — <description>`, push, and go straight to Step E. State your reasoning. This exception never applies to Action Required or Recommended findings with behavior impact.

Otherwise spawn via the **Agent tool** with a short directive that invokes the existing `address-review` skill. Do NOT pre-load the skill content.

- `subagent_type`: `"general-purpose"`
- `description`: `"Address review PR #<number> round <N>"`
- `prompt`:

```
You are addressing filtered review feedback on PR #<number>, round <N>. A referee has validated these findings — they are real issues. Address them all, but independently verify each suggested fix is correct before applying it. When a finding asserts a quantitative default or behavior ("timeout is 0", "this runs synchronously"), verify the claim against the code first; if the premise is wrong but the fix still helps, apply it with a corrected justification and flag the discrepancy in your summary.

<If Phase 2 used a worktree:> Work in the existing worktree at <path> (branch <branch>) — never the main checkout.

Run the address-review skill on this PR:
  Skill tool → skill: "address-review", args: "<number>"

## Findings to Address (referee-filtered)
<paste the filtered action plan from Step B — only Accepted and Downgraded findings, with the referee's severity and reasoning>

## Key Rules
- Run the full test suite ONCE, after ALL findings are committed. Every test must pass. No exceptions. Do not run the suite between findings. (Remote-wait pattern: Phase 2 step 4.)
- Before browser tests, kill any stale process holding the target port and reuse it — don't switch ports.
- Commit with message format: `fix: address review round <N> — <description>`
- Keep fix commits separate when they address unrelated findings.
- Push to the PR branch when done.
- Do NOT re-request review or add reviewers — the orchestrator controls the review loop.
- Post your summary to the PR as a comment via: gh pr comment <number> --body "<summary>"
- When done, relay your summary table via SendMessage (see spawn instructions) — your plain-text output is invisible to the orchestrator.

Return a summary table:

| # | Finding | Action | Details |
|---|---------|--------|---------|
| 1 | <brief description> | Applied / Partially applied / Rejected | <what was done and why> |
| ... | ... | ... | ... |

**Tests:** <command> — <result>
**Commits:** <list of fix commit messages>
```

**No mid-flight injection.** If new findings surface while the addresser is running (from self-verification, a late specialist, anywhere), queue them — do not message them into the in-flight addresser; every injected wave restarts its commit-test-verify cycle. When its report arrives: fold minor extras in yourself at Step E, or spawn a second addresser with one consolidated batch. If you must forward supplements after a report, label them collision-proof (`SUPPLEMENT-A`, `SUPPLEMENT-B`, …) and state explicitly that they are new items, not rows from the referee table.

#### Step E: Self-Verify, Then Decide Continuation

After the addresser subagent returns, **verify the fixes from your own context** before deciding whether to spawn another reviewer subagent.

1. **Post the addresser's summary** as a PR comment (if the addresser didn't already).
2. **Self-verify** by re-fetching the diff and reading the touched files yourself:
   ```bash
   gh pr diff <number> --name-only
   ```
   Read each touched file. Cross-check against the addresser's summary and the round's filtered action plan. You are looking for **concrete** evidence that a Round-N+1 reviewer would help:
   - A new file appeared in the diff that the original reviewer never saw.
   - An addresser commit changed code that doesn't trace back to any forwarded finding.
   - A forwarded finding looks unfixed or partially fixed (the change doesn't actually resolve the issue).
3. **Decide:**
   - **Stop and go to Phase 5** if: self-verification found nothing concerning, OR this was round 3.
   - **Dispatch an addresser directly (no new reviewer)** if self-verification found a concrete, precisely-scoped bug touching ≤3 files: send the fix specification, document your reasoning in a PR comment. Larger or architectural finds get a Round-N+1 reviewer instead.
   - **Spawn Round N+1 reviewer (Step A)** only if self-verification surfaced one of the concrete triggers above. If you spawn Round 2 and it finds nothing, stop — do not spawn Round 3 speculatively.
4. **Escalate** if round 3 ends with unresolved Action Required items:

```
gh pr comment <number> --body "$(cat <<'EOF'
## Escalation — Review Loop Limit

3 review rounds completed with unresolved Action Required items:

<list each unresolved item with context on what was attempted>

Requesting human review.
EOF
)"
```

Then stop and inform the user directly with the escalation details.

---

### Phase 5: Merge & Finalize

1. **Final test run.** Confirm all tests pass.
   - **Untestable behavior** (OS signals, IPC, external services): make at most 1–2 live verification attempts. If the window is structurally too narrow, stop and state the gap plainly in your report ("X is covered by mocked unit tests; live verification was limited by Y") instead of burning attempts or claiming success.
2. **Merge and update issues — use the Skill tool, never `gh pr merge` directly.** Inline merges have broken `Closes #N` auto-close (amended commits) and fail branch deletion from worktree checkouts; merge-pr handles validation, issue linkage, and cleanup. Pass the base after the PR number when it is not `main`:
   ```
   Skill tool → skill: "merge-pr", args: "<pr-number> [--base <base>]"
   ```
   `merge-pr` reads the PR's actual base from GitHub, so the `--base` arg is optional; pass it for clarity when threading a non-`main` base. This validates the PR against standards, squash-merges it into its base, deletes the branch, and posts progress updates on all linked GitHub issues.
3. **Optional doc audit.** For PRs that shipped new protocol tags, API endpoints, or persona behavior, consider spawning a doc-audit subagent: give it the doc paths, the shipped PRs, and staleness grep patterns (`"deferred to #N"`, `"TODO"`, `"follow-up"`); it reports via SendMessage. It catches stale roadmap language and cross-doc drift that code reviewers won't.
4. Report the result to the user.

---

## Escalation

Stop and flag the human directly (not as a PR comment) when encountering:

- Ambiguous requirements where you cannot proceed without clarification
- Architectural decisions that exceed the scope of the task
- A new third-party dependency is needed
- Changes touch auth, crypto, or PII handling beyond existing patterns
- Tests fail in ways unrelated to your changes
- A working-tree or branch collision between parallel agents corrupted (or nearly corrupted) a branch — report what must be verified or redone
- A session authentication failure interrupted work mid-task — report the state reached before the interruption

Provide: what you tried, evidence for/against options, your recommended path.
