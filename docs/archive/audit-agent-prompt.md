# LifeOS Audit Implementation — Orchestrator Prompt

You are an orchestrator agent responsible for implementing a series of improvements to LifeOS, a self-hosted AI life assistant. You will work through 8 phases sequentially, from Phase 0 through Phase 4b.

---

## Your Role

You are the single agent driving all implementation. For each phase, you will:

1. **Read the phase prompt** to understand what needs to happen
2. **Read prior phase state** from `docs/audit/audit-implementation-plan.md` ("What Changed" sections)
3. **Read CLAUDE.md** for project conventions (critical — follow these strictly)
4. **Explore the codebase** to understand current state before making changes
5. **Implement the phase** following the prompt's requirements and boundaries
6. **Verify the phase** using the verification steps in the prompt
7. **Update the implementation plan** — write a "What Changed" summary for the phase you just completed
8. **Commit the phase** with a descriptive commit message
9. **Move to the next phase**

---

## Key Documents

All documents are in `docs/audit/`. Read these as needed:

| Document | Purpose |
|----------|---------|
| `audit-implementation-plan.md` | **Source of truth.** Status, phase order, file ownership, state summaries. Update this after every phase. |
| `audit-vision-v2.md` | Master vision document. Read the relevant section for each phase. |
| `CLAUDE.md` | Project conventions. Follow these strictly — especially "Simplicity First" and "Surgical Changes". |

Phase-specific prompts (read the relevant one before starting each phase):

| Phase | Prompt File | What It Does |
|-------|-------------|-------------|
| Phase 0 | `audit-phase0-prompt.md` | Infrastructure: WAL mode, backups, launchd, log rotation |
| Phase 1 | `audit-phase1-prompt.md` | Migrate PersonEntity from JSON to SQLite |
| Phase 2a | `audit-phase2a-prompt.md` | Unify chat pipeline (single agentic entry point) |
| Phase 2b | `audit-phase2b-prompt.md` | Add MCP write tools (5.2-5.7) |
| Phase 2c | `audit-phase2c-prompt.md` | Agent memory (explicit save + semantic retrieval) |
| Phase 3 | `audit-phase3-prompt.md` | SQLite-backed task queue for background jobs |
| Phase 4a | `audit-phase4a-prompt.md` | Full-agentic reminder pipeline (parity with Telegram) |
| Phase 4b | `audit-phase4b-prompt.md` | Proactive intelligence modules (briefings, prep, nudges) |

PRD specs for Phase 2b sub-items:

| PRD | Tool |
|-----|------|
| `prd-mcp-update-person.md` | `lifeos_person_update` (5.2) |
| `prd-mcp-reminder-update.md` | `lifeos_reminder_update` (5.3) |
| `prd-mcp-trigger-sync.md` | `lifeos_sync_trigger` (5.4) |
| `prd-mcp-person-facts.md` | Person facts CRUD (5.5) |
| `prd-mcp-health-detailed.md` | Fix `lifeos_health` formatter (5.6) |

Archive audit docs (in `docs/audit/archive/`) — reference only when a phase prompt tells you to.

---

## Phase Execution Order

```
Phase 0 → Phase 1 → Phase 2a → Phase 2b → Phase 2c → Phase 3 → Phase 4a → Phase 4b
```

Phases 2a, 2b, and 2c touch different files and could theoretically run in parallel, but since you are a single agent, execute them sequentially in the order above.

### Dependencies

- **Phase 0**: No dependencies. Do first.
- **Phase 1**: Depends on Phase 0 (WAL mode must be in place).
- **Phase 2a/2b/2c**: Depend on Phase 1. No dependencies on each other.
- **Phase 3**: Depends on Phase 2 (benefits from unified chat pipeline).
- **Phase 4a**: Depends on Phase 2a (unified pipeline) and Phase 3 (task queue).
- **Phase 4b**: Depends on Phase 4a (hardened reminder pipeline).

---

## Workflow for Each Phase

### Before Starting a Phase

1. Read the phase prompt file (e.g., `docs/audit/audit-phase0-prompt.md`)
2. Read the "What Changed" sections in `docs/audit/audit-implementation-plan.md` for ALL prior completed phases
3. Read any PRDs or archive docs that the phase prompt references
4. Explore the files listed in the phase prompt's "Files to Explore" section
5. Understand the current state before writing any code

### During Implementation

6. Follow `CLAUDE.md` conventions strictly:
   - Simplicity first — minimum code that solves the problem
   - Surgical changes — touch only what you must
   - Restart server after code changes: `./scripts/server.sh restart`
   - Never run uvicorn directly
7. Respect the phase prompt's "Boundaries" section — do NOT touch files or systems outside scope
8. If something is unclear, explore the codebase to clarify rather than guessing

### After Completing a Phase

9. Run verification steps from the phase prompt
10. Run the standard verification checklist:
    ```bash
    # Tests pass
    ./scripts/test.sh

    # Server starts cleanly
    ./scripts/server.sh restart
    # Wait a few seconds, then:
    curl -s http://localhost:8000/health | jq .status
    ```
11. Update `docs/audit/audit-implementation-plan.md`:
    - Change the phase's status from `not started` to `completed` in the Status table
    - Write a concise "What Changed" summary under the appropriate section (files modified, new files created, key decisions made, anything the next phase needs to know)
12. Commit the phase:
    ```bash
    # Review changes first
    git diff
    git status
    # Commit with a descriptive message
    git add <specific files>
    git commit -m "Phase X: <description>"
    ```
13. Proceed to the next phase

---

## If Something Goes Wrong

- If tests fail after a phase, fix the issue before moving on. Do NOT proceed to the next phase with failing tests.
- If the server won't start, debug and fix before moving on.
- If you realize a phase's approach is wrong mid-implementation, revert uncommitted changes (`git checkout .`) and re-approach.
- If a phase seems to require changes outside its boundaries, note this but do NOT make those changes. Stick to the phase's scope.

---

## What Success Looks Like

When you're done with all 8 phases:

1. All SQLite databases use WAL mode, backups run nightly, launchd works, logs rotate
2. PersonEntity lives in SQLite with proper transactions
3. A single agentic chat pipeline handles all messages
4. MCP has write tools for person update, reminder update, sync trigger, facts CRUD, and a fixed health formatter
5. The agent has persistent memory across conversations
6. Long-running operations run in a background task queue
7. Prompt-type reminders have full agentic parity with direct Telegram messages
8. Pre-meeting prep, morning briefings, and communication gap nudges run as proactive reminders
9. All tests pass, server starts cleanly, and each phase has a clean commit
10. `audit-implementation-plan.md` has complete "What Changed" summaries for every phase

---

## Start Now

Begin with Phase 0. Read `docs/audit/audit-phase0-prompt.md` and `CLAUDE.md`, then proceed.
