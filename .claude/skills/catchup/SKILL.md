---
name: catchup
description: Synthesize recent PR activity into a cross-PR net-effects briefing
argument-hint: [hours] [me|others]
---

# Catchup: Recent PR Synthesis

Arguments: `$ARGUMENTS`

Parameters (order-independent, all optional):
- **hours** — numeric, defaults to 24
- **author filter** — `me` (only your PRs), `others` (everyone except you), or omit for all

## PR Data

Merged PRs in the window:

!`A="${ARGUMENTS:-}"; H=$(echo "$A" | grep -oE '[0-9]+' | head -1); H=${H:-24}; F=$(echo "$A" | grep -oiE '\b(me|others)\b' | head -1 | tr A-Z a-z); case "$F" in me) AF="author:@me";; others) AF="-author:@me";; *) AF="";; esac; gh pr list --state=merged --search="merged:>=$(date -u -v-${H}H +%Y-%m-%dT%H:%M:%S)Z ${AF}" --json number,title,author,mergedAt,body,additions,deletions,changedFiles --limit 50 2>/dev/null || echo "GH_ERROR"`

Open/changed PRs in the window (created or updated):

!`A="${ARGUMENTS:-}"; H=$(echo "$A" | grep -oE '[0-9]+' | head -1); H=${H:-24}; F=$(echo "$A" | grep -oiE '\b(me|others)\b' | head -1 | tr A-Z a-z); case "$F" in me) AF="author:@me";; others) AF="-author:@me";; *) AF="";; esac; gh pr list --state=open --search="updated:>=$(date -u -v-${H}H +%Y-%m-%dT%H:%M:%S)Z ${AF}" --json number,title,author,createdAt,updatedAt,body,additions,deletions,changedFiles,labels --limit 50 2>/dev/null || echo "GH_ERROR"`

Git stats for the window:

!`A="${ARGUMENTS:-}"; H=$(echo "$A" | grep -oE '[0-9]+' | head -1); H=${H:-24}; SINCE=$(date -u -v-${H}H +%Y-%m-%dT%H:%M:%S); COMMITS=$(git log --since="$SINCE" --oneline origin/main 2>/dev/null | wc -l | tr -d ' '); STATS=$(git log --since="$SINCE" --shortstat origin/main 2>/dev/null | grep "insertion\|deletion" | awk '{for(i=1;i<=NF;i++){if($i~/insertion/)ins+=$(i-1);if($i~/deletion/)del+=$(i-1)}} END {printf "%d added, %d removed", ins+0, del+0}'); echo "commits=$COMMITS $STATS"`

Project context for understanding what changed:

!`cat AGENTS.md`

## Instructions

You are producing a **net-effects briefing** — not a PR-by-PR changelog. The reader wants to understand what's different about the codebase now compared to N hours ago.

Check the arguments above to determine the author filter. If `others` was specified, note in your briefing header that this covers changes by other contributors (not the person running the skill). If `me` was specified, note it covers only the runner's own changes. If no filter, cover all contributors.

### Step 1: Triage

If either PR list shows `GH_ERROR`, report that `gh` failed (likely auth or network) and stop.

Scan both PR lists. If there are zero PRs in both lists, say so and stop.

Group PRs into two buckets:
- **Landed** — merged in the window
- **In flight** — open PRs created or updated in the window

### Step 2: Understand the changes

For each merged PR, read its description carefully. Extract:
- What area of the codebase it touches
- What capability it adds, changes, or removes
- Whether it introduces schema/migration changes, API surface changes, new dependencies, or test infrastructure changes

If a PR description is too terse to understand the scope, fetch the diff summary:
```
gh pr diff <number> --stat
```

For particularly important or large PRs, fetch the full diff to understand the actual changes:
```
gh pr diff <number>
```

Use your judgment — not every PR needs a full diff read. Focus effort on PRs with large insertion counts, schema changes, or architectural implications.

### Step 3: Write the briefing

Produce the following sections. Use concise, direct language. No filler.

---

#### By the Numbers

A compact stats line at the top of the briefing. Use the git stats and PR data provided above to produce a single line like:

> **12 commits, 6 PRs merged, +4,713 / -128 lines**

Include: commit count, merged PR count, lines added/removed. If an author filter is active, note it (e.g., "by @nbramia" or "by others").

#### Landed

*Group changes by theme/area, not by PR.* Merge related PRs into a single narrative. For each theme:
- What changed (capabilities added, removed, or modified)
- Relevant stats if notable (e.g., "new sync phase", "15 new API tests")
- Which PRs contributed and who authored them (parenthetical references, e.g., "#23, #24 — @nbramia")

Themes should be whatever emerges naturally from the changes — examples: "Sync pipeline", "Search & indexing", "CRM / entity resolution", "API endpoints", "MCP tools", "Documentation", "Tooling", "Test coverage". Don't force categories that don't exist.

#### System Impact

For each theme above, explain in plain language **how the system's behavior or capabilities changed at a high level**. Write this for someone who hasn't looked at the code — they want to know what the system can do now that it couldn't before, what works differently, or what's more reliable/faster/safer.

Examples of the right level of abstraction:
- "The nightly sync pipeline now halts on critical failures instead of continuing with stale data. If ChromaDB goes down mid-sync, downstream phases won't run with incomplete indexes."
- "Hybrid search now weights BM25 keyword matches more heavily for short queries, improving precision on exact-name lookups without degrading semantic recall for longer questions."
- "The CRM person timeline now includes iMessage and WhatsApp data alongside email and calendar, giving a unified interaction history per person."

Avoid restating the "what changed" section — focus on the **so what**.

#### In Flight

For each open PR updated in the window:
- Title and author
- What it's adding/changing
- Current state (draft? review requested? CI passing?)

If there are no open PRs, omit this section.

#### Review Attention

Optional section — include only if warranted. Flag items like:
- Unusually large PRs that may not have received proportional review depth
- Architectural decisions that are now load-bearing and hard to reverse
- Areas with complex logic that could benefit from a closer look
- Cross-cutting changes that affect many files
- New dependencies introduced

If nothing warrants attention, omit this section entirely.

---

### Formatting rules

- Use markdown headers and bullets
- Reference PR numbers as `#N` (GitHub will auto-link them)
- Don't include full PR descriptions — synthesize
- Keep the total output scannable — aim for a briefing someone can read in 2-3 minutes
- Don't editorialize on code quality or contributor velocity

## Escalation

Stop and tell the user when:
- `gh` commands fail (auth, network, or unexpected errors)
- Either PR list returns exactly 50 results (the limit cap) — suggest a shorter time window
- The date command fails (this skill uses macOS `date -v` syntax; it won't work on Linux)
