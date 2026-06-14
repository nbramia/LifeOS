---
name: implement
description: "Full Codex implementation lifecycle for GitHub issues, pull requests, or freeform coding tasks: understand scope, plan, create a branch, write tests, implement changes, run verification, create a PR, perform adversarial review/address rounds, merge, and update linked issues. Use when the user invokes $implement, asks to implement an issue such as '#123', says 'implement this', asks for end-to-end delivery from task to PR/merge, or wants the former Claude /implement workflow in Codex."
---

# Implement

Run the full implementation lifecycle for the request you were given.

## Core Rules

- Use the repo's `AGENTS.md` and nested guidance as binding instructions.
- Use `update_plan` when available. If it is unavailable, maintain a concise visible checklist in normal messages.
- Keep exactly one plan item `in_progress` at a time and update status as phases complete.
- Keep changes surgical. Do not refactor adjacent code unless needed for the task.
- Prefer tests that fail before implementation and pass after it.
- Never skip, weaken, delete, or mark failing tests as skipped just to pass.
- Do not overwrite or revert user changes. Work with a dirty tree carefully.
- If the task requires a public API change, database schema change, sync-phase change, MCP tool definition change, or new dependency, follow the repo's "Ask first" boundary before editing.

## Entry Point

Identify the target before planning:

1. Run `git branch --show-current` and `git log --oneline -5`.
2. If the leading token of the user's request is a number or starts with `#`, treat it as a GitHub issue. Fetch it with `gh issue view <number> --comments`.
3. Otherwise, check for a PR on the current branch:
   `gh pr list --head "$(git branch --show-current)" --json number,title,body --jq '.[0]'`.
   If the PR matches the user's request, treat it as the target PR.
4. Otherwise, treat the request as a freeform task.

Parse trailing modifiers:

| Modifier | Effect |
| --- | --- |
| none | Run all phases. |
| `quick` or `no-review` | Skip the adversarial review/address loop. |
| `no-plan` | Skip the initial planning phase only when the target is already precise. |
| `review-only` | Run only the review/address loop for the target PR. |
| `no-merge` | Stop after PR creation/review and leave merge to the user. |

When in doubt, run more of the lifecycle, not less.

## Phase 1: Understand And Plan

Skip only for `no-plan` or `review-only`.

1. Explore relevant files with `rg`, `rg --files`, and targeted reads.
2. Read any referenced specs, ADRs, issue comments, or failing test output.
3. Define concrete acceptance criteria.
4. Identify the test cases that prove the criteria.
5. Plan the smallest viable implementation: files to edit, tests to add/update, and verification commands.
6. Present the plan briefly if the task is substantial or if tradeoffs matter.

Update the plan with granular items before editing.

## Phase 2: Implement

Skip for `review-only`.

1. Create a branch if currently on `main` or another non-feature branch:
   `<type>/<short-description>`, lowercase and hyphen-separated.
2. Write focused tests first when practical.
3. Implement production changes.
4. Run targeted tests as soon as the changed area is testable.
5. Run the repo's normal verifier before PR creation. For LifeOS, prefer:
   `./scripts/test.sh auto`
6. Read the diff yourself:
   - `git diff --stat`
   - `git diff`
   - touched files directly if needed
7. Fix unused imports, style mismatches, missing docs, or behavior gaps that trace to the task.

If Python files changed in LifeOS, restart through `./scripts/server.sh restart` when the task requires a running server or manual API verification. Never run `uvicorn` directly.

## Phase 3: Create PR

Skip for `review-only` when the target PR already exists.

1. Commit with `<type>: <summary>`.
2. Push the branch.
3. Create the PR:

```bash
gh pr create --title "<type>: <imperative summary>" --body "$(cat <<'EOF'
## Summary
<1-3 sentences: what changed and why>

<Closes #N / Relates to #N if applicable>

## Test evidence
<commands and result summary>

## Review focus
<areas where reviewer attention is useful>
EOF
)"
```

4. If the target was a GitHub issue, comment on it:

```bash
gh issue comment <N> --body "$(cat <<'EOF'
## In Progress

Implementation PR created: #<pr-number> — <PR title>
Entering review/verification phase.
EOF
)"
```

## Phase 4: Review And Address Loop

Skip for `quick` or `no-review`. Run for existing PRs by default.

Default to one round. Run at most three rounds.

### 4A. Review

Use a Codex subagent only when the user or current session policy explicitly allows subagents/delegation. Otherwise, perform the same adversarial review yourself in the main context.

For a subagent review, ask for a bounded PR review:

```text
Review PR #<number> adversarially. Read the PR description, linked issue/specs, and diff.
Prioritize correctness bugs, regressions, privacy/security risks, missing tests, and repo-standard violations.
Return findings only, grouped as Action Required, Recommended, Minor, then a brief summary.
Use file:line references.
```

For local review, fetch and inspect:

```bash
gh pr view <number> --json number,title,body,comments,reviews
gh pr diff <number> --name-only
gh pr diff <number>
```

### 4B. Referee Findings

Evaluate every finding before acting:

| Decision | Use when |
| --- | --- |
| Accept | You verified the finding is valid. |
| Downgrade | The concern is real but severity is overstated. |
| Reject | The finding is incorrect, irrelevant, unsupported, or out of scope. |

Accept Action Required and security findings by default unless the code clearly proves them wrong.

Post referee decisions to the PR:

```bash
gh pr comment <number> --body "$(cat <<'EOF'
## Review Round <N> — Referee Decisions

| # | Finding | Reviewer Severity | Decision | Reasoning |
|---|---------|-------------------|----------|-----------|
| 1 | <brief description> | Action Required / Recommended / Minor | Accept / Downgrade / Reject | <why> |

**Findings forwarded to addresser:** <count>
EOF
)"
```

If no findings survive, post:

```bash
gh pr comment <number> --body "Review Round <N>: no actionable findings — review loop complete."
```

### 4C. Address

Address accepted and downgraded findings either yourself or with a Codex worker subagent when allowed and when the write scope is clear.

Rules:

- Read the relevant code before changing it.
- Keep fixes scoped to forwarded findings.
- Add or adjust tests when a finding exposes untested behavior.
- Run targeted tests and then the repo verifier again.
- Commit fixes separately when they address unrelated findings.
- Push to the PR branch.
- Comment with what changed and the test evidence.

### 4D. Self-Verify Continuation

After addressing, inspect the new diff yourself. Spawn or perform another review round only if there is concrete reason:

- A forwarded finding is not fixed.
- A fix introduced a new touched area not previously reviewed.
- A change does not trace to the task or review findings.
- Tests changed in a way that weakens coverage.

Stop after round 3. If unresolved Action Required findings remain, comment on the PR with an escalation summary and report that human review is needed.

## Phase 5: Merge And Finalize

Skip for `no-merge`.

1. Run the final verifier. In LifeOS, use `./scripts/test.sh auto` unless a broader suite is warranted.
2. Use the native `$pr-check` skill if installed, or manually verify PR standards.
3. Use the native `$merge-pr` skill if installed. It should validate, merge, delete the branch, and update linked issues.
4. If `$merge-pr` is not available, run the equivalent non-interactive GitHub flow:
   - Confirm tests passed.
   - Confirm PR has no unresolved required checks or blocking comments.
   - Squash merge with `gh pr merge --squash --delete-branch`.
   - Comment on linked issues with the merged PR and outcome.
5. Report the PR/merge result and test evidence to the user.

## Output Shape

During work, keep user updates short and concrete.

Final response:

- State what changed.
- Link the PR or merged commit.
- Summarize verification.
- Mention any skipped phase, failed command, or residual risk.
