---
name: remove-worktree
description: >
  Tear down a Claude Code worktree created via scripts/create-worktree.sh.
  Use when the user says "close the worktree", "remove this worktree", "tear
  down the worktree", "I'm done with this worktree", or runs /remove-worktree.
  Handles cd-out if invoked from inside the worktree, runs the removal script,
  and optionally deletes the branch when merged into origin/main.
argument-hint: [worktree-name] [--delete-branch]
---

# Remove Worktree

Tear down a worktree from `~/.claude/worktrees/LifeOS/`.

## Context

- Current directory: !`pwd`
- Worktree info (if inside one): !`cat .worktree-info 2>/dev/null || echo "(not in a worktree)"`
- Main repo checkout: !`dirname "$(git rev-parse --path-format=absolute --git-common-dir)"`
- Active worktrees: !`"$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/scripts/list-worktrees.sh" 2>/dev/null || echo "(none)"`
- Arguments: **$ARGUMENTS**

## Instructions

### Step 1: Resolve the worktree name

In order of preference:
1. If `$ARGUMENTS` contains a name (first non-flag token), use it.
2. Else if cwd is inside a worktree and `.worktree-info` exists, read `NAME=` from it.
3. Else if exactly one worktree is active in the list above, use that.
4. Else ask the user which worktree to remove.

Flatten any slashes (`feat/foo` → `feat-foo`) — the script expects the flat dir name.

### Step 2: Check the delete-branch flag

If `$ARGUMENTS` contains `--delete-branch`, prepare to set `LIFEOS_WORKTREE_DELETE_BRANCH=1`. The script only deletes the branch if it's already merged into `origin/main`, so this is safe by default — but mention to the user that you're going to attempt branch deletion.

### Step 3: Get out of the worktree if necessary

If `pwd` is under `~/.claude/worktrees/LifeOS/`, `cd` to the main repo checkout (shown in Context above) first. `git worktree remove --force` will delete the directory out from under the shell otherwise.

### Step 4: Run the removal script

From the main repo:

```bash
cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
LIFEOS_WORKTREE_DELETE_BRANCH=<0 or 1> ./scripts/remove-worktree.sh <name>
```

### Step 5: Report

In one or two sentences:
- Whether the worktree was removed
- Whether the branch was deleted (or why not — e.g., "not merged to origin/main")
- If anything looks orphaned, suggest `./scripts/cleanup-worktrees.sh`

Do not summarize what the script printed line-by-line; the user already sees the output.
