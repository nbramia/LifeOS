---
name: merge-pr
description: Merge a PR and update upstream GitHub issues with progress
argument-hint: <pr-number>
---

# Merge PR and Update Issues

Merge PR #$ARGUMENTS and update all linked GitHub issues with what was delivered.

## PR Context

- PR metadata: !`gh pr view $ARGUMENTS --json number,title,body,state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,headRefName,baseRefName,additions,deletions,changedFiles`
- PR checks: !`gh pr checks $ARGUMENTS 2>/dev/null || echo "NO_CHECKS"`

## Instructions

### Step 1: Validate Readiness

Check that the PR is safe to merge. For each check, determine pass/fail:

1. **State** — PR must be `OPEN`. If already merged or closed, report and stop.
2. **Merge conflicts** — `mergeable` must not be `CONFLICTING`. If conflicts exist, report and stop.
3. **CI status** — All status checks must pass. If any check is failing, report which ones and stop.
4. **PR standards** — Read `.Codex/skills/pr-check/SKILL.md` and execute its validation checks against the PR. All checks must pass (WARN is acceptable, FAIL is not). Fix any failures if possible; otherwise report what needs to be fixed and stop.
5. **Review decision** — Check `reviewDecision` from the PR metadata. If `CHANGES_REQUESTED`, stop and report. If `REVIEW_REQUIRED` or empty/null (no reviews submitted), escalate to the user. Only proceed if `APPROVED` or if the user explicitly confirms.

**If validation fails**, stop and report exactly what needs to be fixed. Do not merge.

**If validation requires human judgment** (e.g., a check is flaky, the PR has unresolved conversations), stop and consult the user with the evidence.

### Step 2: Merge

Squash-merge the PR and delete the remote branch:

```
gh pr merge $ARGUMENTS --squash --delete-branch
```

If the merge fails, report the error and stop.

**Normalize commit timestamp** (privacy: hides work schedule):

After the merge succeeds, pull the merge commit and normalize its timestamp to 12:00 UTC on the same date. This matches the post-commit hook behavior, which doesn't run for GitHub-side merges.

```bash
git checkout main && git pull origin main
NORM_DATE="$(date -u -d "$(git log -1 --format='%ad' --date=short) 12:00" '+%Y-%m-%dT12:00:00+00:00')"
GIT_AUTHOR_DATE="$NORM_DATE" GIT_COMMITTER_DATE="$NORM_DATE" \
    git commit --amend --no-edit --no-verify --date="$NORM_DATE"
git push --force-with-lease origin main
```

If the force-push fails (e.g., another commit landed), warn the user but do not retry. The merge itself already succeeded.

### Step 3: Update Linked Issues

1. **Extract issue references** from the PR title and description. Look for:
   - **Closing references:** `Closes #N`, `Fixes #N`, `Resolves #N` (case-insensitive) — the PR fully addresses the issue
   - **Partial references:** `Relates to #N`, `Part of #N` — the PR partially addresses or relates to the issue

2. **For each referenced issue**, fetch the issue with `gh issue view <N> --json state,title` and post an update:

   **For closing references** (issue should be auto-closed by GitHub):
   ```
   gh issue comment <N> --body "$(cat <<'EOF'
   ## Delivered

   **PR:** #<pr-number> — <PR title>

   ### Changes delivered
   - <bullet summary extracted from PR description and diff>

   This PR fully addresses this issue.
   EOF
   )"
   ```

   **For partial references:**
   ```
   gh issue comment <N> --body "$(cat <<'EOF'
   ## Progress Update

   **PR:** #<pr-number> — <PR title>

   ### Changes delivered
   - <bullet summary extracted from PR description>

   ### Remaining work
   <What this issue still needs, based on reading the issue body vs. what was delivered. If unclear, state "See issue description for remaining scope.">
   EOF
   )"
   ```

3. **If no issues are referenced**, skip this step.

### Step 4: Report

Output a summary:

```
## Merge Complete

**PR:** #<number> — <title>
**Merged to:** <base branch>
**Issues updated:** <list of issue numbers, or "none">

### Changes
- <bullet summary>
```

## Escalation

Stop and consult the user when:

- PR has failing CI checks that may be flaky (unclear if real failure)
- PR has unresolved review conversations
- Merge fails for an unexpected reason
- An issue referenced by the PR is already closed and the update seems redundant
- Any situation requiring human judgment about whether to proceed
