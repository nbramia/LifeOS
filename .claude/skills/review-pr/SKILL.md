---
name: review-pr
description: Run adversarial PR review with specialist subagents
argument-hint: <pr-number>
---

# Adversarial PR Review

Review PR #$ARGUMENTS using specialist agents. Be adversarial — verify claims, don't trust assertions.

## PR Context

- PR metadata: !`gh pr view $ARGUMENTS`
- PR diff: !`gh pr diff $ARGUMENTS`
- PR comments: !`gh pr view $ARGUMENTS --comments`

## Instructions

### Step 1: Gather Context

Before spawning any review agents, gather context yourself:

1. Read the PR description above. Identify the **intent**, **test evidence**, and any **review focus** areas the author flagged.
2. Check referenced issues or specs — if the description links to issues (`#N`) or spec documents, read them to understand what the PR is supposed to accomplish.
3. Scan the diff file list. Identify which modules are touched. Does the scope match the stated intent, or are there unrelated changes?
4. If the change touches architecture (new modules, new dependencies, schema changes), read related ADRs in `docs/adr/` to confirm alignment.
5. If this is part of a stacked PR series, note the stack position and ensure you're reviewing in order.

### Step 2: Verify Claims

Do not trust the PR description at face value. Independently verify:

- **Test evidence** — If the author says "all tests pass," check out the branch and run the tests yourself. Agent work is cheap; broken merges are expensive.
- **Scope claims** — If the description says "only touches X," confirm via the diff that nothing else was changed.
- **Spec conformance** — If the description says "implements spec Y," read the spec and verify the implementation actually matches.

### Step 3: Select Specialist Agents

Based on the change type, determine which specialists to spawn:

- **Docs-only change** → Standards only
- **Code change (typical)** → Correctness + Security + Standards
- **New API endpoint** → Correctness + Security + Requirements + Standards
- **Performance-sensitive path** → All five specialists

### Step 4: Spawn Specialist Agents

Use the Task tool to spawn the selected agents **in parallel**. Each agent should be a `general-purpose` subagent. Provide each agent with:

- The full PR diff (copy it into the prompt)
- The specific focus area and what to look for (see table below)
- The relevant standards document(s) to check against
- Instructions to categorize every finding by severity: **Action Required**, **Recommended**, or **Minor**

| Agent | Focus | Reference Docs |
|-------|-------|---------------|
| Correctness | Logic bugs, edge cases, error handling gaps, race conditions | `AGENTS.md` (development principles) |
| Security | AuthZ/AuthN, injection risks, secrets exposure, PII handling | `AGENTS.md` (privacy principles) |
| Performance | Hot paths, N+1 queries, async bottlenecks, SQLite locking, connection pool exhaustion | `AGENTS.md` (tech stack), `docs/specs/technical/architecture.md` |
| Requirements | Validates code against acceptance criteria and referenced specs | Referenced issues/specs from PR description |
| Standards | Project conventions, naming, structure, test coverage, PR formatting | `AGENTS.md`, `.claude/skills/pr-check/SKILL.md` |

### Step 5: Consolidate Review

After all specialist agents return findings:

1. Merge all findings into a single list
2. Deduplicate — if multiple agents flagged the same issue, keep the most detailed version
3. Resolve conflicts — if agents disagree, note the disagreement and recommend the safer option
4. Sort by severity: **Action Required** first, then **Recommended**, then **Minor**

### Step 6: Post Review

Format the consolidated review and post it as a GitHub PR review comment using:

```
gh pr review $ARGUMENTS --comment --body "<review content>"
```

Use this output format:

```markdown
## PR Review: <PR title>

### Action Required
- **[Agent]** Finding description with file:line references

### Recommended
- **[Agent]** Finding description with file:line references

### Minor
- **[Agent]** Finding description with file:line references

### Summary
<1-2 sentence overall assessment: merge-ready, needs changes, or needs discussion>
```

If there are no Action Required items, state that explicitly. If there are no findings at all in a category, omit that category.

## Escalation

Stop the review and flag the human directly (do not post as a PR comment) when encountering:

- Ambiguous requirements or missing acceptance criteria
- Failing tests where root cause is unclear
- Architectural decisions that affect multiple modules
- New third-party dependency introduction
- Changes touching auth, crypto, or PII handling

When escalating, provide: what you tried, evidence, options with tradeoffs, and your recommended path.

## Anti-Patterns

Do not fall into these traps during review:

- **Blind trust** — Merging because "it passed CI" without understanding the changes
- **Review theater** — Approving without reading or comprehending
- **Skipping verification** — Trusting the author's claims about test results or scope
- **Test modification** — Suggesting test changes to make them pass instead of fixing the code (see AGENTS.md § "Tests Are Sacred")
