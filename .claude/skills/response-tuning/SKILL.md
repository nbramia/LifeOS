---
name: response-tuning
description: >
  Tune LifeOS orchestrator behavior based on feedback about a bad response.
  Use when the user describes a problem with how LifeOS answered a query — wrong sources,
  too verbose, not persistent enough, poor tool selection, bad source integration,
  missing detail, gave up too early, etc. Also covers Claude Code task execution issues
  (scope, persistence, notification style). Makes a surgical prompt edit, branches, commits, and pushes.
  Trigger on: "response-tuning", "fix the prompt", "tune the response", "it didn't search enough",
  "too verbose", "improve the orchestrator", "the response was bad", or any feedback about
  LifeOS chat/orchestrator quality.
argument-hint: <feedback about what went wrong>
---

# Response Tuning

Improve LifeOS orchestrator behavior based on this feedback: **$ARGUMENTS**

## Context

- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -3`
- Recent conversations: !`sqlite3 data/conversations.db "SELECT m.role, substr(m.content, 1, 200), m.created_at FROM messages m JOIN conversations c ON m.conversation_id = c.id ORDER BY m.created_at DESC LIMIT 10;" 2>/dev/null || echo "NO_CONVERSATION_DB"`
- Recent perf traces: !`curl -s http://localhost:8000/api/perf/traces?limit=3 2>/dev/null | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f'{t.get(\"question\",\"\")[:100]} | tools: {[s[\"name\"] for s in t.get(\"spans\",[]) if s[\"name\"].startswith(\"tool_\")]}') for t in data.get('traces',[])]" 2>/dev/null || echo "NO_PERF_DATA"`

## How this works

You are fixing the orchestrator's behavior — the instructions that tell LifeOS how to search,
pick tools, integrate sources, and format responses. The user got a bad result and is telling
you what went wrong so you can prevent it from happening again.

The fix is almost always a targeted edit to an instruction in a system prompt. Think of it like
tuning a knob — you're adjusting the model's tendencies, not rewriting the system.

## Step 1: Diagnose

Read the feedback and the recent conversation context above. Identify:

1. **What went wrong** — Map the feedback to a specific failure mode:
   - Didn't search enough sources → tool persistence / multi-tool patterns
   - Too verbose / too terse → response format section
   - Gave up without trying → give-up detection / self-correction
   - Wrong tool chosen → tool descriptions / when-to-use guidance
   - Poor source integration → synthesis instructions
   - Didn't follow up on partial results → search persistence rules
   - Bad Claude Code task execution → orchestrator scope/persistence/notification rules

2. **Which file owns this behavior:**

   | Behavior | File | Key section |
   |----------|------|-------------|
   | Chat tool selection & search strategy | `api/services/agent_system_prompt.py` | `_STATIC_PROMPT_TEMPLATE` |
   | Chat response format & conciseness | `api/services/agent_system_prompt.py` | Response format section |
   | Chat tool round limits | `api/services/agent_loop.py` | `max_tool_rounds` param |
   | Give-up detection patterns | `api/services/agent_loop.py` | `_GIVE_UP_PATTERNS` |
   | Self-correction nudge | `api/services/agent_loop.py` | `SELF_CORRECTION_NUDGE` |
   | Claude Code task scope & persistence | `api/services/claude_orchestrator.py` | `_SYSTEM_PROMPT` |
   | Claude Code notifications | `api/services/claude_orchestrator.py` | NOTIFICATIONS section |

3. **Read the target file(s)** — Read the actual current content of the file you plan to edit.
   Do not work from memory. The prompt may have changed since you last saw it.

## Step 2: Assess scope

This skill is for **small, surgical changes**. Before editing, ask yourself:

- Am I changing fewer than ~20 lines of prompt text? → Proceed.
- Am I adding/adjusting a single behavioral rule or instruction? → Proceed.
- Am I tweaking a parameter (like max_tool_rounds)? → Proceed.
- Would this require restructuring the prompt, adding new tools, or changing multiple
  modules? → **Stop.** Create a GitHub issue instead:

  ```bash
  gh issue create \
    --title "Orchestrator: <summary of needed change>" \
    --body "## Problem
  <what went wrong — the user's feedback>

  ## Analysis
  <your diagnosis of the root cause>

  ## Suggested approach
  <what you think needs to change and why it's too large for a prompt tweak>

  ## Files involved
  <list of files that would need changes>"
  ```

  Then notify the user that an issue was created and stop.

## Step 3: Edit

Make the change. Guidelines:

- **Match the existing style.** The prompts use a specific tone and structure — imperative,
  concise, example-driven. Don't introduce a different voice.
- **Be specific, not vague.** "Search at least 3 different sources" is better than
  "Be more thorough." The model responds to concrete instructions.
- **Explain why when it's not obvious.** A brief parenthetical like "(the user can't easily
  follow up from their phone)" helps the model internalize the intent.
- **Don't bloat the prompt.** If you're adding a new rule, check if an existing rule already
  covers a similar case and can be strengthened instead. Every line in the prompt costs
  attention — keep it lean.
- **Don't weaken other behaviors.** Read the surrounding context to make sure your edit
  doesn't contradict or undermine an existing instruction.
- **Test your edit mentally.** Imagine the same query that produced the bad response going
  through the updated prompt. Would the model now behave differently? If not, your edit
  isn't targeted enough.

## Step 4: Branch, commit, push

```bash
# Ensure we branch from main
git checkout main
git pull --ff-only

# Create branch
git checkout -b fix/response-tuning-<short-description>

# Stage only the changed files
git add <changed-files>

# Commit
git commit -m "fix: tune orchestrator — <what was adjusted>

Feedback: <brief user feedback>
Change: <one-line description of what was edited>"

# Push
git push -u origin fix/response-tuning-<short-description>
```

## Step 5: Notify

Report back with:
- What you diagnosed
- What you changed (quote the before/after of the edited lines)
- The branch name so the user can review

Keep it to 3-5 sentences. The user is on their phone.
