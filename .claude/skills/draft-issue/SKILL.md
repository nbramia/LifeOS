---
name: draft-issue
description: >
  Draft and create a GitHub issue from a description, investigation, or conversation context.
  Use when a problem is too large for a quick fix, when you want to propose a change for later,
  or when the user asks to "file an issue", "create an issue", "draft an issue", "open a ticket",
  or "log this for later". Also useful when another skill (like response-tuning) determines a
  change is too large and needs to be tracked as an issue instead.
argument-hint: <issue description or context>
---

# Draft Issue

Create a GitHub issue for: **$ARGUMENTS**

## Context

- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`
- Open issues: !`gh issue list --state=open --json number,title --limit=10 --jq '.[] | "#\(.number) \(.title)"' 2>/dev/null || echo "GH_ERROR"`

## Instructions

### Step 1: Understand the ask

Parse `$ARGUMENTS` to determine what the issue should cover. The input could be:
- A direct description ("add rate limiting to the search endpoint")
- A problem statement ("the sync pipeline sometimes misses new contacts")
- A reference to something discovered during other work ("response-tuning found this needs a larger refactor")
- A conversation context where the user said "file an issue for this"

If the description is too vague to write a useful issue, ask one clarifying question.

### Step 2: Research (if needed)

If the issue relates to specific code or behavior, quickly investigate:
- Read relevant files to understand the current state
- Check if a similar issue already exists (use the open issues list above)
- Identify which files/modules would be affected

Skip this if the user already provided enough detail.

### Step 3: Write and create the issue

Create the issue with a clear structure:

```bash
gh issue create \
  --title "<type>: <imperative summary>" \
  --label "<label>" \
  --body "$(cat <<'EOF'
## Problem

<What's wrong or what's missing — 2-3 sentences>

## Context

<How this was discovered, why it matters, any relevant background>

## Suggested approach

<High-level direction — not a full spec, just enough to orient whoever picks it up>

## Files involved

<List of files/modules likely affected>

## Acceptance criteria

- [ ] <Verifiable condition 1>
- [ ] <Verifiable condition 2>
EOF
)"
```

**Title format:** Use the same `<type>: <summary>` convention as commits (feat, fix, refactor, docs, etc.). Keep it under 70 characters.

**Labels:** Use existing labels. Common ones: `bug`, `enhancement`, `documentation`. Check what's available with `gh label list` if unsure.

**Body guidelines:**
- Problem section should be understandable by someone with no context
- Suggested approach should be directional, not prescriptive
- Acceptance criteria should be objectively verifiable
- Don't include implementation details unless they're critical constraints
- Use synthetic data in any examples

### Step 4: Report

Tell the user:
- The issue number and URL
- A one-line summary of what was filed

Keep it brief — the user can read the full issue on GitHub.
