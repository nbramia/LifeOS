---
name: standup
description: Personal daily summary — what you shipped, what's in progress, what needs attention
argument-hint: [hours]
---

# Standup: Personal Daily Summary

Arguments: `$ARGUMENTS`

Parameters (all optional):
- **hours** — numeric, defaults to 24

## My Data

Merged PRs by me in the window:

!`H=$(echo "${ARGUMENTS:-}" | grep -oE '[0-9]+' | head -1); H=${H:-24}; gh pr list --author=@me --state=merged --search="merged:>=$(date -u -d "${H} hours ago" +%Y-%m-%dT%H:%M:%S)Z" --json number,title,mergedAt,additions,deletions,changedFiles --limit 50 2>/dev/null || echo "GH_ERROR"`

Open PRs by me with review status:

!`gh pr list --author=@me --state=open --json number,title,createdAt,updatedAt,isDraft,reviewDecision,additions,deletions,changedFiles --limit 50 2>/dev/null || echo "GH_ERROR"`

Issues assigned to me:

!`gh issue list --assignee=@me --state=open --json number,title,createdAt,updatedAt,labels --limit 50 2>/dev/null || echo "GH_ERROR"`

My commits on main in the window:

!`H=$(echo "${ARGUMENTS:-}" | grep -oE '[0-9]+' | head -1); H=${H:-24}; EMAIL=$(git config user.email); git log --author="$EMAIL" --since="$(date -u -d "${H} hours ago" +%Y-%m-%dT%H:%M:%S)" --oneline origin/main 2>/dev/null || echo "No commits found"`

My git stats for the window:

!`H=$(echo "${ARGUMENTS:-}" | grep -oE '[0-9]+' | head -1); H=${H:-24}; EMAIL=$(git config user.email); SINCE=$(date -u -d "${H} hours ago" +%Y-%m-%dT%H:%M:%S); COMMITS=$(git log --author="$EMAIL" --since="$SINCE" --oneline origin/main 2>/dev/null | wc -l | tr -d ' '); STATS=$(git log --author="$EMAIL" --since="$SINCE" --shortstat origin/main 2>/dev/null | grep "insertion\|deletion" | awk '{for(i=1;i<=NF;i++){if($i~/insertion/)ins+=$(i-1);if($i~/deletion/)del+=$(i-1)}} END {printf "+%d / -%d lines", ins+0, del+0}'); echo "commits=$COMMITS $STATS"`

## Instructions

You are producing a **personal standup summary** — a quick status report of what *I* shipped, what's in progress, and what needs my attention.

### Step 1: Triage

If any data block shows `GH_ERROR`, report that `gh` failed (likely auth or network) and stop.

Parse the hours parameter from the arguments (default 24). Note the time window in the output header.

### Step 2: Write the standup

Produce the following sections:

---

#### Stats

A single compact line summarizing activity:

> **3 commits, 1 PR merged, 2 PRs open, +713 / -28 lines (last 24h)**

Derive values from the git stats and PR data above. Adjust the "(last Nh)" to match the actual window.

#### Done

List merged PRs and landed commits in the window. For each:
- PR number and title
- Brief description of what it accomplished (one line)

If no merged PRs or commits, say "Nothing landed in this window."

#### In Progress

List open PRs by me. For each:
- PR number and title
- Status label derived from the data:
  - `DRAFT` — if `isDraft` is true
  - `CHANGES REQUESTED` — if `reviewDecision` is `CHANGES_REQUESTED`
  - `APPROVED` — if `reviewDecision` is `APPROVED`
  - `AWAITING REVIEW` — otherwise (not draft, no decision yet)
- Lines changed (`+N / -M`)

If no open PRs, say "No open PRs."

#### Needs Attention

Flag items that require action. Include only items that apply:
- PRs with `CHANGES REQUESTED` status — need to address feedback
- Stale open PRs (updated 7+ days ago) — need to push forward or close
- Assigned issues — list with age and labels

If nothing needs attention, omit this section entirely.

---

### Formatting rules

- Use markdown headers and bullets
- Reference PR numbers as `#N`
- Reference issues as `#N`
- Keep it scannable — this should take under 1 minute to read
- Don't editorialize or add motivational commentary

## Escalation

Stop and tell the user when:
- `gh` commands fail
- Any PR list returns exactly 50 results — suggest a shorter time window
