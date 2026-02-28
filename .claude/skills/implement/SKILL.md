---
name: implement
description: Full implementation lifecycle with adversarial review using subagents
argument-hint: <task-description or #issue-number>
---

# Implement

Orchestrate the full implementation lifecycle for: $ARGUMENTS

## Context

- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`
- Issue (if applicable): !`gh issue view $ARGUMENTS --comments 2>/dev/null || echo "NOT_AN_ISSUE"`

## Instructions

This skill runs five phases. You **MUST** use task tracking (TaskCreate/TaskUpdate) throughout to track progress and surface status to the user.

**Task tracking rules:**
1. **Bootstrap immediately.** Before starting work, create a task for each phase you will execute using TaskCreate. Each task needs a clear `subject` (imperative: "Explore codebase and plan implementation"), `activeForm` (continuous: "Exploring codebase and planning"), and `description`.
2. **One in_progress at a time.** Mark a task `in_progress` before starting it. Mark `completed` the moment it finishes — do not batch completions.
3. **Break down dynamically.** When entering a phase, expand it into granular sub-tasks. When unexpected work surfaces (failing test, unanticipated dependency, new requirement), add new tasks immediately.
4. **Keep the list truthful.** Delete irrelevant tasks. Update descriptions if scope changes. The list must reflect current reality.

**Entry point — determine where to start:**
1. If `$ARGUMENTS` starts with `#`, it is a GitHub issue. The issue body is shown in Context above — extract the task description and acceptance criteria. Then proceed to Phase 1.
2. Run `gh pr list --head <current-branch> --json number,title --jq '.[0]'`. If a PR exists, verify it relates to the current task (check title/description alignment). If it does, record the PR number and **skip to Phase 4**. If it appears unrelated, ignore it and proceed to Phase 1.
3. Otherwise, treat `$ARGUMENTS` as a freeform task description.

---

### Phase 1: Understand & Plan

1. **Explore.** Use Glob, Grep, Read to understand relevant modules, existing patterns, test structure. Read any specs or ADRs referenced by the task.
2. **Define done.** Write verifiable acceptance criteria. Add each as a task via TaskCreate.
3. **Identify test cases.** List tests that pass iff criteria are satisfied. Include edge cases.
4. **Plan implementation.** Identify files to create/modify, dependencies, sequencing. Minimum viable approach — AGENTS.md § Simplicity First.

### Phase 2: Implement

1. **Create a branch** if not already on a feature branch: `<type>/<short-description>` per `.claude/skills/pr-check/SKILL.md`.
2. **Write tests first** for identified test cases. They should fail until implementation is complete.
3. **Write production code** to make tests pass. Follow existing patterns. Surgical changes only.
4. **Run the full test suite** on the Mac Mini: `ssh nathanramia@100.95.233.70 "cd ~/Documents/Code/LifeOS && ./scripts/test.sh"`. All tests must pass before proceeding.
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

Runs up to **3 rounds**. Each round: Reviewer subagent -> Referee (you) -> Addresser subagent.

#### Before Round 1 — Prepare Injection Context

These steps gather everything needed to construct subagent prompts. Do them once; re-fetch the diff each round.

1. **Read the review skill:** Use the Read tool on `.claude/skills/review-pr/SKILL.md`. Store the full content. Strip the YAML frontmatter block (lines between `---` markers) and strip lines that contain `` !`...` `` shell escape sequences (those inject data at skill load time — you will replace them with actual data in the prompt).
2. **Read the address skill:** Use the Read tool on `.claude/skills/address-review/SKILL.md`. Strip frontmatter and `` !`...` `` shell escape lines the same way. Also strip Step 6 (the "Re-request Review" step) — the orchestrator controls the review loop, so re-requesting review from within the addresser subagent would trigger unwanted notifications.
3. **Fetch PR data:** Run `gh pr view <number>` and `gh pr diff <number>`. Store both outputs. You will re-fetch the diff before each subsequent round since it changes after addressing.

#### Step A: Spawn Reviewer Subagent

Use the **Task tool** with these exact parameters:

- `subagent_type`: `"general-purpose"`
- `description`: `"Review PR #<number> round <N>"`
- `prompt`: Construct by concatenating the blocks below. Copy the actual data into the prompt — the subagent has no access to the PR or skill files otherwise.

**Prompt template for reviewer:**

```
You are an adversarial code reviewer. Your job is to find real problems — bugs, security issues, spec violations, missing tests. Do not nitpick style unless it violates project conventions.

## PR Metadata
<paste output of: gh pr view <number>>

## PR Diff
<paste output of: gh pr diff <number>>

## PR Comments
<If N > 1, paste the PR comments from: gh pr view <number> --comments. This gives the reviewer access to previous referee decisions and addresser summaries for verification. If N == 1, omit this section.>

## Review Methodology
<paste the stripped contents of review-pr/SKILL.md here — this gives the reviewer the full review methodology including specialist agent spawning, severity categories, verification steps, and anti-patterns>

## Round Context
Round <N> of 3.
<If N > 1, include: "Previous round referee decisions: <paste the referee decision table from the prior round>. Do NOT repeat findings that were already addressed or explicitly rejected with justification. Focus on: new issues introduced by fixes, issues missed in prior rounds, and whether previously-addressed findings were actually fixed correctly.">

## Output Format
Return findings in exactly this structure:

### Action Required
- **[Category]** Description with specific file:line references

### Recommended
- **[Category]** Description with specific file:line references

### Minor
- **[Category]** Description with specific file:line references

### Summary
<1-2 sentence overall assessment: merge-ready, needs changes, or needs discussion>

If a category has no findings, omit it entirely.
```

The reviewer subagent will also post its findings to GitHub using `gh pr review`. This is fine — let it. The GitHub comment provides an audit trail.

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

Use the **Task tool** with these exact parameters:

- `subagent_type`: `"general-purpose"`
- `description`: `"Address review PR #<number> round <N>"`
- `prompt`: Construct by concatenating the blocks below.

**Prompt template for addresser:**

```
You are addressing filtered review feedback on PR #<number>. A referee has validated these findings — they are real issues. Address them all, but independently verify that each suggested fix is correct before applying it.

## PR Number
<number>

## Branch
<branch-name from gh pr view --json headRefName>

## Findings to Address
<paste the filtered action plan from Step B — only Accepted and Downgraded findings, with the referee's severity and reasoning>

## Address Methodology
<paste the stripped contents of address-review/SKILL.md here — this gives the addresser the full methodology for verifying findings, the Apply/Partially-apply/Reject/Escalate framework, testing requirements, and commit conventions>

## Key Rules
- Run the full test suite after ALL changes. Every test must pass. No exceptions.
- Commit with message format: `fix: address review round <N> — <description>`
- Keep fix commits separate when they address unrelated findings.
- Push to the PR branch when done.
- Do NOT re-request review or add reviewers — the orchestrator controls the review loop.
- Post your summary to the PR as a comment using: gh pr comment <number> --body "<summary>"

## Output Format
Return a summary table:

| # | Finding | Action | Details |
|---|---------|--------|---------|
| 1 | <brief description> | Applied / Partially applied / Rejected | <what was done and why> |
| ... | ... | ... | ... |

**Tests:** <command> — <result>
**Commits:** <list of fix commit messages>
```

#### Step E: Evaluate Continuation

After the addresser subagent returns:

1. **Post the addresser's summary** as a PR comment (if the addresser didn't already).
2. **Decide whether to continue:**
   - **Stop** if: this was round 3, OR zero findings were accepted by the referee in this round.
   - **Continue** if: Any findings were accepted and addressed — run a verification round to confirm fixes are clean and no regressions were introduced. Re-fetch `gh pr diff <number>` and return to Step A.
3. **Escalate** if round 3 ends with unresolved Action Required items:

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
