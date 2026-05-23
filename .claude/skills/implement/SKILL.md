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

**Entry point — identify the target:**
1. If the leading token of `$ARGUMENTS` is a number or starts with `#`, it is a GitHub issue. Fetch the issue with `gh issue view <number> --comments` to get the full description and acceptance criteria.
2. Otherwise, run `gh pr list --head <current-branch> --json number,title --jq '.[0]'`. If a PR exists, verify it relates to the current task (check title/description alignment). If it does, record the PR number and treat the target as that PR. If it appears unrelated, ignore it.
3. Otherwise, treat the leading portion of `$ARGUMENTS` as a freeform task description.

**Trailing instructions — scope the lifecycle:**

Any text after the leading token controls which phases run. Parse it before bootstrapping tasks so the task list reflects only the phases you will execute.

| Instructions | Effect |
|--------------|--------|
| *(none)* | Full lifecycle (default): Phase 1 → 2 → 3 → 4 → 5 |
| `quick` or `no-review` | Skip Phase 4 entirely — Phase 1 → 2 → 3 → 5 |
| `no-plan` | Skip Phase 1 — go straight to Phase 2 |
| `review-only` | Run Phase 4 only on the target PR (target MUST be a PR number) |
| Any other text | Interpret intent. Do more rather than less — the full lifecycle is always safe. |

Modifiers compose (e.g. `no-plan quick` skips Phase 1 and Phase 4). If the target is an existing PR and no instructions are given, skip to Phase 4.

---

### Phase 1: Understand & Plan

1. **Explore.** Use Glob, Grep, Read to understand relevant modules, existing patterns, test structure. Read any specs or ADRs referenced by the task.
2. **Define done.** Write verifiable acceptance criteria. Add each as a task via TaskCreate.
3. **Identify test cases.** List tests that pass iff criteria are satisfied. Include edge cases.
4. **Plan implementation.** Identify files to create/modify, dependencies, sequencing. Minimum viable approach — AGENTS.md § Simplicity First.

### Phase 2: Implement

1. **Create a branch** if not already on a feature branch: `<type>/<short-description>` per AGENTS.md § Development Workflow.
2. **Write tests first** for identified test cases. They should fail until implementation is complete.
3. **Write production code** to make tests pass. Follow existing patterns. Surgical changes only.
4. **Run the full test suite** on the server: `./scripts/test.sh`. All tests must pass before proceeding.
5. **Self-review your diff.** Read every changed file. Check for: unused imports, style mismatches, missing error handling, changes that do not trace to the task.

### Phase 3: Create PR

1. **Commit** with `<type>: <summary>` format. Separate logical changes into distinct commits.
2. **Push** the branch.
3. **Create the PR:**
   ```
   gh pr create --title "<type>: <imperative summary>" --body "$(cat <<'EOF'
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

#### Step A: Spawn Reviewer Subagent

Use the **Skill tool** to invoke the existing `review-pr` skill as a subagent. Do NOT pre-load the skill content into the prompt — the subagent will load the methodology itself.

- `subagent_type`: `"general-purpose"`
- `description`: `"Review PR #<number> round <N>"`
- Use the **Task tool** with a short directive that instructs the subagent to invoke the `review-pr` skill with the PR number:

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

The reviewer subagent will post its findings to GitHub via `gh pr review` as part of the skill. That's the audit trail — let it happen.

#### Step B: Referee Evaluation (You — Main Context)

When the reviewer subagent returns, independently evaluate **every finding**. Read the relevant code yourself. Do not rubber-stamp and do not dismiss without checking.

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

Use the **Task tool** with a short directive that invokes the existing `address-review` skill. Do NOT pre-load the skill content.

- `subagent_type`: `"general-purpose"`
- `description`: `"Address review PR #<number> round <N>"`
- `prompt`:

```
You are addressing filtered review feedback on PR #<number>, round <N>. A referee has validated these findings — they are real issues. Address them all, but independently verify each suggested fix is correct before applying it.

Run the address-review skill on this PR:
  Skill tool → skill: "address-review", args: "<number>"

## Findings to Address (referee-filtered)
<paste the filtered action plan from Step B — only Accepted and Downgraded findings, with the referee's severity and reasoning>

## Key Rules
- Run the full test suite after ALL changes. Every test must pass. No exceptions.
- Commit with message format: `fix: address review round <N> — <description>`
- Keep fix commits separate when they address unrelated findings.
- Push to the PR branch when done.
- Do NOT re-request review or add reviewers — the orchestrator controls the review loop.
- Post your summary to the PR as a comment via: gh pr comment <number> --body "<summary>"

Return a summary table:

| # | Finding | Action | Details |
|---|---------|--------|---------|
| 1 | <brief description> | Applied / Partially applied / Rejected | <what was done and why> |
| ... | ... | ... | ... |

**Tests:** <command> — <result>
**Commits:** <list of fix commit messages>
```

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
2. **Merge and update issues.** Invoke the merge skill explicitly:
   ```
   Skill tool → skill: "merge-pr", args: "<pr-number>"
   ```
   This validates the PR against standards, squash-merges it, deletes the branch, and posts progress updates on all linked GitHub issues.
3. Report the result to the user.

---

## Escalation

Stop and flag the human directly (not as a PR comment) when encountering:

- Ambiguous requirements where you cannot proceed without clarification
- Architectural decisions that exceed the scope of the task
- A new third-party dependency is needed
- Changes touch auth, crypto, or PII handling beyond existing patterns
- Tests fail in ways unrelated to your changes

Provide: what you tried, evidence for/against options, your recommended path.
