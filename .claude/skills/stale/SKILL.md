---
name: stale
description: Housekeeping report — find stale PRs, orphan branches, stale issues, and stale plans
argument-hint: [days]
---

# Stale: Housekeeping Report

Arguments: `$ARGUMENTS`

Parameters (all optional):
- **days** — staleness threshold in days, defaults to 14

## Repository Data

Open PRs with metadata:

!`gh pr list --state=open --json number,title,author,createdAt,updatedAt,isDraft,reviewDecision,headRefName,additions,deletions --limit 100 2>/dev/null || echo "GH_ERROR"`

Open issues:

!`gh issue list --state=open --json number,title,assignees,createdAt,updatedAt,labels --limit 100 2>/dev/null || echo "GH_ERROR"`

Local branches with last commit date:

!`git branch --format='%(refname:short) %(committerdate:iso8601)' 2>/dev/null | grep -v '^main$' | grep -v '^master$'`

Branch-to-PR mapping:

!`git branch --format='%(refname:short)' | grep -v '^main$' | grep -v '^master$' | while IFS= read -r b; do PR=$(gh pr list --head "$b" --state=open --json number --jq '.[0].number' 2>/dev/null); echo "$b -> ${PR:-none}"; done`

Plan files (if docs/plans/ exists):

!`if [ -d docs/plans ]; then find docs/plans -name '*.md' -not -path '*/archive/*' 2>/dev/null; else echo "NO_PLANS_DIR"; fi`

Plan file contents (frontmatter only, for status/date checking):

!`if [ -d docs/plans ]; then find docs/plans -name '*.md' -not -path '*/archive/*' 2>/dev/null | while IFS= read -r f; do echo "=== $f ==="; head -20 "$f" 2>/dev/null; echo; done; else echo "NO_PLANS_DIR"; fi`

## Instructions

You are producing a **housekeeping report** — identifying stale items that need cleanup, closure, or attention.

### Step 1: Triage

If any data block shows `GH_ERROR`, report that `gh` failed (likely auth or network) and stop.

Parse the days threshold from arguments (default 14). An item is "stale" if it hasn't been updated in more than this many days.

### Step 2: Analyze

For each category, identify stale or orphaned items:

**Stale PRs** — Open PRs where `updatedAt` is older than the threshold.

**Orphan branches** — Local branches that have no open PR (mapped to `none` in the branch-to-PR data). For each orphan, check if it's been merged into main:
```
git branch --merged main
```
An orphan that's already merged into main is a safe delete candidate. An unmerged orphan needs investigation.

**Stale issues** — Open issues where `updatedAt` is older than the threshold.

**Stale plans** — Plan files in `docs/plans/` (not in `archive/`) that appear completed, have a past target date, or haven't been updated recently. Check frontmatter for `Status` and date fields.

### Step 3: Write the report

Produce the following sections:

---

#### Summary

A single compact line:

> **2 stale PRs, 3 orphan branches, 1 stale issue, 0 stale plans**

#### Stale PRs

A table for each stale PR:

| PR | Title | Author | Age | Status | Suggested Action |
|----|-------|--------|-----|--------|-----------------|

Status should reflect: `DRAFT`, `AWAITING REVIEW`, `CHANGES REQUESTED`, `APPROVED`.
Suggested actions: "Close — appears abandoned", "Ping author for update", "Needs rebase", "Review and merge — approved but stale", etc.

If no stale PRs, say "None — all PRs are active."

#### Orphan Branches

List each orphan branch with:
- Branch name
- Last commit date
- Whether it's merged into main
- Suggested action: "Safe to delete (merged)" or "Investigate — unmerged and has no PR"

If no orphan branches, say "None — all branches have open PRs."

#### Stale Issues

A table for each stale issue:

| Issue | Title | Assignee | Age | Labels | Suggested Action |
|-------|-------|----------|-----|--------|-----------------|

Suggested actions: "Close — likely resolved", "Needs triage", "Reassign — assignee may be unavailable", etc.

If no stale issues, say "None — all issues are active."

#### Stale Plans

List plan files that appear stale:
- File path
- Status from frontmatter (if present)
- Why it's flagged (completed, past target date, no recent updates)
- Suggested action: "Archive to docs/plans/archive/", "Update status", "Delete if superseded"

If `docs/plans/` doesn't exist or has no stale plans, say "None."

#### Quick Wins

List exact commands the user can copy-paste to clean up safe-to-delete items. **Do not execute these — list them only.**

Examples:
```
git branch -d feature/old-branch    # merged, safe to delete
git push origin --delete feature/old-branch  # clean up remote
gh pr close 42 --comment "Closing stale PR — superseded by #57"
```

If there are no quick wins, omit this section.

---

### Formatting rules

- Use markdown tables for PRs and issues
- Reference PR/issue numbers as `#N`
- Show ages as "X days" (compute from current date vs `updatedAt` or commit date)
- Keep it scannable — this is a cleanup checklist, not a deep analysis
- Be specific in suggested actions — generic "review this" isn't helpful

## Escalation

Stop and tell the user when:
- `gh` commands fail (auth, network, or unexpected errors)
- Any list returns at its limit cap (100 results) — the repo may have more items than shown
