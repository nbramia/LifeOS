# Round 2: MCP Cross-Pollination Analysis

**Perspective:** MCP Integration Specialist
**Date:** 2026-02-13
**Inputs:** All five Round 1 audits (Backend, Frontend, MCP, Telegram/Chat, Infrastructure)

---

## 1. The Tool Ecosystem: Mapping User Journeys to MCP Tools

The current 35 MCP tools cover ~22% of the 156+ API endpoints. But the real gap is not endpoint coverage -- it is **journey coverage**. Here is what it takes to complete real user journeys end-to-end through MCP tools alone.

### Journey: "Prepare me for my meeting with John at 2pm"

| Step | Needed Tool | Status |
|------|-------------|--------|
| Find meeting details | `lifeos_calendar_upcoming` | Exists |
| Look up John | `lifeos_people_search` | Exists |
| Get John's profile | `lifeos_person_profile` | Exists |
| Get recent interactions | `lifeos_person_timeline` | Exists |
| Get extracted facts | `lifeos_person_facts` | Exists |
| Read relevant vault notes | `lifeos_read_vault_file` | **MISSING** |
| Full meeting prep bundle | `lifeos_meeting_prep` | Exists |

**Verdict:** 6/7 tools exist. Only missing `read_vault_file`. This is the strongest journey.

### Journey: "Draft an email to Sarah about the project update using our last meeting notes"

| Step | Needed Tool | Status |
|------|-------------|--------|
| Find Sarah | `lifeos_people_search` | Exists |
| Get Sarah's email | `lifeos_person_profile` | Exists |
| Search meeting notes | `lifeos_search` | Exists |
| Read specific meeting note | `lifeos_read_vault_file` | **MISSING** |
| Create email draft | `lifeos_gmail_draft` | Exists |
| Send the email | `lifeos_gmail_send` | **MISSING (only drafts)** |

**Verdict:** 4/6 exist. The Telegram auditor flagged that compound compose intents bypass the agentic loop -- MCP tools could enable a better flow here since agents naturally chain tools.

### Journey: "Research flights to Tokyo next month and remind me to book"

| Step | Needed Tool | Status |
|------|-------------|--------|
| Web search for flights | `lifeos_web_search` | **MISSING** |
| Browse flight sites | Browser automation tools | **MISSING (requires Claude Code)** |
| Summarize findings | (Claude's native ability) | N/A |
| Save summary to vault | `lifeos_save_to_vault` | **MISSING** |
| Create reminder to book | `lifeos_reminder_create` | Exists |
| Create task to track | `lifeos_task_create` | Exists |

**Verdict:** 2/6 exist. The Telegram auditor noted this scenario is limited by the single web search call and the inability to visit specific sites. MCP tools alone cannot do this -- it requires Claude Code's browser automation.

### Journey: "Check my CRM data health and clean up duplicates"

| Step | Needed Tool | Status |
|------|-------------|--------|
| Check data health | `lifeos_data_health` | **MISSING** |
| View cleanup queue | `lifeos_cleanup_queue` | **MISSING** |
| Review suggested merges | `lifeos_review_queue` | **MISSING** |
| Merge duplicates | `lifeos_person_merge` | **MISSING** |
| Confirm entity links | `lifeos_confirm_link` | **MISSING** |

**Verdict:** 0/5 exist. The frontend audit revealed these are all functional in the web UI (19,500-line CRM page), and the backend has the endpoints. The MCP layer simply does not expose them.

### Journey: "What happened with my family this week? Who should I reach out to?"

| Step | Needed Tool | Status |
|------|-------------|--------|
| Get family members | `lifeos_family_members` | **MISSING** |
| Check communication gaps | `lifeos_communication_gaps` | Exists |
| View family stats | `lifeos_family_stats` | **MISSING** |
| See channel breakdown | `lifeos_family_channels` | **MISSING** |
| Send a message | iMessage/Slack send | **MISSING** |

**Verdict:** 1/5 exist. The frontend has a full family dashboard with streaks, gaps, and channel mix. None of this is available through MCP.

---

## 2. Tool Chains for Complex Tasks

The Telegram auditor identified that the agentic pipeline handles multi-step reasoning within a single query via its 5-round tool loop. But MCP tools are used by external agents (Claude Code, third-party clients) that orchestrate their own tool chains. The key insight: **MCP tool chains need to be self-documenting**.

### Current Problem: Tool Discovery

The `lifeos_people_search` description is exemplary -- it tells agents what to call next:
```
FOLLOW-UP TOOLS (use entity_id):
- lifeos_person_profile(entity_id)
- lifeos_person_timeline(entity_id)
```

Most other tools lack this guidance. An agent calling `lifeos_search` doesn't know it should then call `lifeos_read_vault_file` to get full content.

### Proposed Tool Chains (with description guidance)

**Research Chain:**
```
lifeos_search → lifeos_read_vault_file → lifeos_save_to_vault
"After finding relevant documents, use lifeos_read_vault_file to read full content.
 Use lifeos_save_to_vault to save synthesized findings."
```

**People Chain (exists, well-documented):**
```
lifeos_people_search → lifeos_person_profile → lifeos_person_timeline
                     → lifeos_person_facts
                     → lifeos_person_connections
```

**Communication Chain (mostly missing):**
```
lifeos_people_search → lifeos_person_profile (get email/phone)
                     → lifeos_gmail_draft (compose email)
                     → lifeos_gmail_send (MISSING)
                     OR → lifeos_imessage_send (MISSING)
                     OR → lifeos_slack_send (MISSING)
```

**Meeting Prep Chain:**
```
lifeos_calendar_upcoming → lifeos_meeting_prep
                         → lifeos_people_search (per attendee)
                         → lifeos_person_facts (conversation starters)
```

**CRM Maintenance Chain (all missing):**
```
lifeos_data_health → lifeos_cleanup_queue → lifeos_person_merge
lifeos_review_queue → lifeos_confirm_link / lifeos_reject_link
```

**Scheduling Chain:**
```
lifeos_calendar_upcoming (check availability)
→ lifeos_calendar_create (MISSING - create event)
→ lifeos_gmail_draft (send invite details)
→ lifeos_reminder_create (set prep reminder)
```

### What the Telegram Auditor Wants: Multi-Step Reasoning

The Telegram audit identified a workflow engine as Tier 2 priority. From the MCP perspective, tool chains ARE the workflow engine. If tools are well-described with chain guidance, any LLM agent can orchestrate multi-step workflows without a dedicated engine. The key is:

1. Each tool description should include "NEXT STEPS" guidance
2. Tools should return enough context for the agent to decide what to do next
3. Error responses should suggest alternative tools

---

## 3. Write/Action Tools Gap

This is the most critical cross-cutting finding. The backend audit reveals 120+ endpoints. The MCP audit found 35 exposed tools. But the write/action imbalance is stark:

### Current Read vs. Write Balance

| Category | Read Tools | Write Tools |
|----------|-----------|-------------|
| Search/Knowledge | 3 | 0 |
| Email | 1 (search) | 1 (draft only) |
| Calendar | 2 (upcoming, search) | 0 |
| Messages | 2 (iMessage, Slack search) | 0 |
| CRM/People | 8 (search, profile, timeline, facts, connections, insights, gaps, meeting prep) | 0 |
| Tasks | 1 (list) | 4 (create, update, complete, delete) |
| Reminders | 1 (list) | 2 (create, delete) |
| Photos | 3 | 0 |
| Memories | 1 (search) | 1 (create) |
| System | 1 (health) | 0 |
| Telegram | 0 | 1 (send) |
| **Total** | **23** | **9** |

**Read:Write ratio is 2.5:1.** For a "do anything" assistant, it should be closer to 1:1.

### Missing Write Tools (prioritized by user impact)

**Tier 1 -- High frequency, existing endpoints:**
| Tool | Endpoint | Why |
|------|----------|-----|
| `lifeos_person_update` | PATCH `/api/crm/people/{id}` | "Remember John's birthday is March 15" / "Tag Sarah as VIP" |
| `lifeos_reminder_update` | PUT `/api/reminders/{id}` | "Change my morning briefing to 6:30am" |
| `lifeos_save_to_vault` | POST `/api/chat/save-to-vault` | Save research, notes, meeting summaries |
| `lifeos_memory_update` | PUT `/api/memories/{id}` | Correct or update saved memories |
| `lifeos_memory_delete` | DELETE `/api/memories/{id}` | Remove outdated memories |

**Tier 2 -- High impact, requires new or adapted endpoints:**
| Tool | Status | Why |
|------|--------|-----|
| `lifeos_calendar_create` | No endpoint exists | "Schedule a meeting with John next Tuesday" |
| `lifeos_gmail_send` | Only drafts exist | "Send that email" (after drafting) |
| `lifeos_imessage_send` | No endpoint (needs AppleScript bridge) | "Text Mom I'll be late" |
| `lifeos_slack_send` | Read-only integration | "Post in #general that I'm OOO" |

**Tier 3 -- System operations:**
| Tool | Endpoint | Why |
|------|----------|-----|
| `lifeos_reindex` | POST `/api/admin/reindex` | After vault edits |
| `lifeos_sync_trigger` | Would need new endpoint | "Sync my email now" |
| `lifeos_person_merge` | POST `/api/crm/people/merge` | CRM cleanup via chat |
| `lifeos_fact_extract` | POST `/api/crm/people/{id}/facts/extract` | On-demand enrichment |
| `lifeos_confirm_fact` | POST `/api/crm/people/{id}/facts/{id}/confirm` | Validate AI-extracted facts |

### The "Send" Problem

The Telegram auditor identified cross-platform communication as Tier 4 (aspirational). But from the MCP perspective, sending messages is the single most impactful write capability. Every "do anything" scenario eventually needs to communicate the result:

- **Email:** Backend has draft creation. Adding a "send draft" endpoint is trivial (Gmail API supports it).
- **iMessage:** Requires macOS AppleScript/Shortcuts bridge. The infrastructure audit confirms Terminal.app has FDA. An AppleScript-based send via `osascript` is feasible.
- **Slack:** The backend already has full Slack integration for reading. The `slack_integration.py` service has the OAuth tokens needed for posting.

---

## 4. MCP as the Universal Interface

If MCP tools are how AI agents interact with LifeOS, they need to cover every capability. Here is the gap between current tools and "anything on a computer":

### Coverage Map

| Domain | Current Coverage | Gap |
|--------|-----------------|-----|
| **Personal knowledge** | Good (search, ask) | Missing: read specific files, write to vault |
| **Email** | Good read, weak write | Missing: send, reply, forward, label |
| **Calendar** | Good read, no write | Missing: create, update, delete events |
| **Messaging** | Good read, no write | Missing: send iMessage, Slack, WhatsApp |
| **Tasks** | Excellent (full CRUD) | Complete |
| **Reminders** | Good (create, list, delete) | Missing: update |
| **CRM** | Good read, no write | Missing: update, merge, review queue |
| **Photos** | Basic read | Missing: thumbnails, search by date/location |
| **Files** | Google Drive search | Missing: local file operations, vault management |
| **System admin** | Basic health only | Missing: reindex, sync, usage, logs |
| **Browser** | None | Claude Code handles this |
| **Home automation** | None | No backend exists |
| **Financial** | None | No backend exists |
| **Health data** | None | No backend exists |
| **Music/Media** | None | No backend exists |
| **Location** | None | No backend exists |

### What "Anything on a Computer" Requires

The infrastructure audit reveals the hardware upgrade (Corsair AI Workstation 300) will dramatically expand local compute. Combined with the backend's existing 75-service architecture, MCP tools could expose:

**1. System Administration Tools:**
```
lifeos_service_status    -- All services health (not just API)
lifeos_sync_trigger      -- Trigger specific sync sources
lifeos_sync_status       -- Current sync progress
lifeos_reindex           -- Trigger vault reindex
lifeos_usage_stats       -- API costs and usage
lifeos_logs_search       -- Search recent logs
```

**2. Vault/File Management Tools:**
```
lifeos_vault_read        -- Read a specific vault file
lifeos_vault_write       -- Create/update a vault note
lifeos_vault_list        -- List files in a vault folder
lifeos_vault_move        -- Move/rename vault files
```

**3. Full Communication Suite:**
```
lifeos_gmail_send        -- Send email (or send draft)
lifeos_gmail_reply       -- Reply to an email thread
lifeos_imessage_send     -- Send iMessage via AppleScript bridge
lifeos_slack_post        -- Post to Slack channel
lifeos_slack_dm          -- Send Slack DM
```

**4. Calendar Management:**
```
lifeos_calendar_create   -- Create calendar event
lifeos_calendar_update   -- Update event details
lifeos_calendar_delete   -- Cancel event
lifeos_calendar_rsvp     -- Accept/decline invitations
```

**5. Automation/Script Tools (new backend needed):**
```
lifeos_shortcut_run      -- Run macOS Shortcut by name
lifeos_shell_run         -- Execute whitelisted shell commands
lifeos_applescript_run   -- Run AppleScript (with safety constraints)
```

### The Claude Code Bridge

The backend audit reveals the Claude Code Orchestrator already provides "do anything" capability via spawning headless CLI sessions. The Telegram auditor rated it 9/10. But it has a fundamental limitation: **single session, blocking, no queue**.

From the MCP perspective, Claude Code is a "super tool" -- an escape hatch when structured tools are insufficient. The architecture should be:

```
Structured MCP Tools (fast, predictable, safe)
       |
       v  (when structured tools can't do it)
Claude Code Orchestrator (slow, powerful, flexible)
       |
       v  (when even Claude Code needs help)
Browser Automation / Shell Execution
```

The infrastructure audit recommends a task queue (Redis + Celery/Dramatiq). This directly enables:
- `lifeos_code_task_submit` -- Queue a Claude Code task
- `lifeos_code_task_status` -- Check task progress
- `lifeos_code_task_cancel` -- Cancel running task
- `lifeos_code_task_list` -- List queued/running/completed tasks

---

## 5. Tool Quality: Making Agents Effective

### Description Quality Audit

The Round 1 MCP audit rated tool descriptions. Cross-referencing with how the Telegram agentic pipeline uses similar tools reveals important patterns:

**What the Telegram agent does well that MCP tools should copy:**
- `agent_tools.py` consolidates related operations into single tools (e.g., `manage_tasks` handles create/list/complete). MCP splits these into 5 separate tools. Both approaches work, but MCP's approach requires better chain documentation.
- The Telegram agent's `person_info` tool has a `mode` parameter (`lookup` vs `briefing`). The MCP equivalent requires calling 3-4 separate tools. Consider adding a `lifeos_person_briefing` mega-tool that returns profile + facts + recent timeline in one call.

**Specific description improvements needed:**

1. **`lifeos_search`** -- Add: "Returns document chunks, not full files. To read a complete file, use lifeos_read_vault_file with the filename from results."

2. **`lifeos_health`** -- The formatter currently drops per-service detail (backend audit confirms `/health/full` returns comprehensive data). Fix the formatter AND improve the description: "Returns per-service status (chromadb, ollama, gmail, calendar, etc.), degradation events, and critical issues."

3. **`lifeos_task_update`** -- The MCP audit flagged that the PUT body schema may not include updatable fields. The description should enumerate: "Updatable fields: description, status (todo/done/in_progress/cancelled/deferred/blocked/urgent), context, priority (high/medium/low), due_date, tags."

4. **`lifeos_gmail_draft`** -- Falls through to default JSON formatter. Add a custom formatter that returns: "Draft created. Draft ID: {id}. Open in Gmail: {url}. To send, open the link and click Send."

5. **`lifeos_communication_gaps`** -- Description says "Requires comma-separated person_ids" but the typical workflow is searching family members first. Add: "TIP: Use lifeos_people_search to find family member IDs, or use lifeos_family_members to get all family IDs at once."

### Schema Quality Fixes

From the MCP audit, critical issues:

1. **PUT/PATCH handling in `_call_api`**: The backend audit confirms PATCH is used for person updates, PUT for task and reminder updates. The MCP server's `_call_api` routes these through the POST/else branch, which sends JSON body. This works but is fragile. Add explicit method handling.

2. **Timeout per tool**: `lifeos_ask` calls Claude for synthesis (15-30s). `lifeos_search` is sub-second. `lifeos_meeting_prep` aggregates multiple sources (5-10s). Per-tool timeouts would prevent false failures.

3. **Response truncation**: The infrastructure audit shows `crm.db` has 556 MB of data. A `lifeos_task_list` with no filters could return thousands of results. Add: `max_results` defaults, truncation with "Showing N of M" footers, and explicit guidance in descriptions.

---

## 6. Infrastructure-Aware Tools

The infrastructure audit identified major new capabilities coming with the hardware upgrade. Here is what MCP tools should expose:

### GPU-Enabled Capabilities

| New Capability | MCP Tool | Description |
|---------------|----------|-------------|
| Local LLM inference | `lifeos_local_llm` | Run queries through local 70B model (free, private, fast) |
| Speech-to-text | `lifeos_transcribe` | Transcribe audio files via local Whisper |
| Image analysis | `lifeos_image_analyze` | Describe/OCR images via local vision model |
| Fast reindexing | `lifeos_reindex` (improved) | GPU-accelerated, completes in minutes not hours |

### Task Queue Integration

The infrastructure audit's #1 recommendation is a task queue. MCP tools should integrate directly:

```
lifeos_job_submit       -- Submit a background job (sync, reindex, research)
lifeos_job_status       -- Check job progress
lifeos_job_list         -- List active/queued/completed jobs
lifeos_job_cancel       -- Cancel a queued or running job
```

This solves the Telegram auditor's critical gap #1 (no long-running task queue) and enables:
- "Reindex my vault" without blocking the API
- "Run my nightly sync now" on demand
- "Research flights to Tokyo" as a background job with progress updates

### Monitoring & Observability Tools

The infrastructure audit found no external monitoring and in-memory failure tracking. MCP tools could provide the observability layer:

```
lifeos_service_status   -- All services (API, ChromaDB, Ollama, Telegram, sync)
lifeos_sync_health      -- Per-source sync freshness and error counts
lifeos_usage_report     -- API costs, token counts, model usage breakdown
lifeos_disk_usage       -- Database sizes, log sizes, vault size
lifeos_recent_errors    -- Last N errors across all services
```

### Backup & Recovery Tools

The infrastructure audit found critical backup gaps (no backups of crm.db, ChromaDB, or config). MCP tools for backup operations:

```
lifeos_backup_create    -- Trigger manual backup of all databases
lifeos_backup_list      -- List available backups with sizes and dates
lifeos_backup_status    -- Last backup time, next scheduled, any failures
```

---

## 7. New Ideas from the Full Picture

Reading all five audits together reveals insights none of them could surface individually:

### 7.1 The Frontend-MCP Parity Problem

The frontend audit reveals the CRM page has 19,500 lines implementing features like:
- Dunbar circle management
- 365-day interaction heatmaps
- Communication channel breakdown
- Relationship tone analysis
- Entity merge/split/cleanup workflows
- Birthday calendar

**None of this is available through MCP.** The CRM frontend and the MCP tool surface are two completely separate interfaces to the same backend, with wildly different coverage. A user asking Claude Code "show me my interaction heatmap with John" gets nothing, while the web UI has it built in.

**Proposal: MCP tools should have feature parity with the frontend.** Every view, chart, and action available in the web UI should be achievable through MCP tools. This does not mean replicating every pixel -- it means the data behind every visualization should be accessible:

```
lifeos_person_heatmap     -- Interaction heatmap data (the web UI renders it as SVG)
lifeos_person_volume      -- Interaction volume over time
lifeos_network_graph      -- Social network graph data
lifeos_me_dashboard       -- Owner's stats, trends, neglected contacts
lifeos_family_dashboard   -- Family health, streaks, gaps, channel mix
lifeos_relationship_tone  -- Tone analysis over time
lifeos_birthday_calendar  -- All birthdays by month
```

### 7.2 The Memory Gap is Universal

The Telegram auditor identified "no persistent memory" as limitation #6. The backend audit confirmed no cross-conversation memory. The MCP audit found `lifeos_memories_create` and `lifeos_memories_search` exist but are disconnected from the agentic pipeline.

**The cross-cutting insight:** Memories exist as a tool but are not part of any agent's core loop. They are a storage mechanism without integration.

**Proposal:** Every MCP client (Claude Code, Telegram, web) should:
1. Automatically load relevant memories at session start
2. Have the ability to save new memories from interactions
3. Share memories across all interfaces

This requires a new approach: `lifeos_memories_context` -- returns the top N most relevant memories for a given query, designed to be injected into system prompts.

### 7.3 The "Proactive Agent" Needs MCP Tools Too

The Telegram auditor proposed proactive intelligence (communication gap detection, calendar prep, task deadlines). The infrastructure audit confirmed prompt-type reminders already enable scheduled queries.

**The MCP angle:** Proactive behaviors are really just scheduled MCP tool chains. A "proactive agent" is a cron job that:
1. Calls `lifeos_calendar_upcoming` to check tomorrow's meetings
2. Calls `lifeos_meeting_prep` for each meeting
3. Calls `lifeos_communication_gaps` for family/VIPs
4. Calls `lifeos_task_list` with `due_before=tomorrow`
5. Synthesizes everything and sends via `lifeos_telegram_send`

This is already possible with prompt-type reminders. The missing piece is a **meta-tool** that composes other tools:

```
lifeos_daily_briefing    -- Runs the full briefing pipeline (calendar + prep + gaps + tasks)
lifeos_weekly_review     -- Summarizes the week (interactions, tasks completed, relationship trends)
```

These are not new endpoints -- they are orchestration patterns that call existing tools in sequence.

### 7.4 The Two-Agent Architecture

The system currently has two agent runtimes:
1. **Telegram agentic loop** -- 12 tools, 5 rounds, fast, focused
2. **Claude Code orchestrator** -- full system access, slow, powerful

MCP is the third runtime, used by Claude Code and potentially other clients.

**The cross-cutting insight:** These three runtimes have different tool sets, different capabilities, and different trade-offs. They should converge on a single tool surface with capability tiers:

| Tier | Tools | Speed | Safety | Use Case |
|------|-------|-------|--------|----------|
| 1: Read | Search, lookup, list | Fast (<2s) | Safe | Information retrieval |
| 2: Write (reversible) | Create task, draft email, save memory | Medium | Safe (undo-able) | Lightweight actions |
| 3: Write (significant) | Send email, post to Slack, merge people | Medium | Needs confirmation | Communication & data changes |
| 4: Execute | Shell commands, browser automation, file management | Slow | Needs authorization | System operations |

MCP tools should be tagged with their tier, so agents can make appropriate safety decisions.

### 7.5 Cross-Platform Notification as a First-Class Tool

Right now `lifeos_telegram_send` is the only notification tool. But the system interacts with users through multiple channels (Telegram, web UI, email). A unified notification tool would be more powerful:

```
lifeos_notify
  channel: "telegram" | "email" | "web_push"
  content: "Your backup completed successfully"
  priority: "normal" | "urgent"
  action_url: "/admin/backups"  (optional deep link)
```

This becomes critical when the task queue is implemented. Background jobs need to notify users on completion, and the notification channel should be configurable.

### 7.6 The Vault as a Write Destination

The backend audit reveals `POST /api/chat/save-to-vault` exists for saving chat content. The infrastructure audit shows the Obsidian vault is the primary knowledge store. But MCP has zero vault write tools.

For a "life OS," the vault is the long-term memory. Every significant interaction should be saveable:
- Research summaries
- Meeting prep documents
- Email drafts for review
- Decision logs
- Weekly reviews

**Proposed vault tools:**
```
lifeos_vault_create     -- Create a new note (title, folder, content, tags)
lifeos_vault_append     -- Append to an existing note
lifeos_vault_read       -- Read a specific file (already proposed)
lifeos_vault_search     -- Already exists as lifeos_search
lifeos_vault_template   -- Create a note from a template (meeting notes, daily review, etc.)
```

### 7.7 Bridging the Frontend's Missing Pages

The frontend audit found that backend APIs exist for tasks, calendar, reminders, admin, and briefings -- but have no web UI. The MCP tools could serve as the "backend for frontend" that enables rapid UI development:

If every backend capability is exposed through well-documented MCP tools, a lightweight frontend (or even a Telegram Mini App) could be built by having Claude Code generate it, using MCP tools as the API layer. The MCP tool descriptions become the API documentation.

---

## 8. Priority Recommendations

### Immediate (fix what exists)

1. **Fix PUT/PATCH handling** in MCP server `_call_api`
2. **Add custom formatters** for photos and gmail_draft tools
3. **Fix `lifeos_health` formatter** to show per-service details
4. **Add response truncation** with "showing X of Y" summaries
5. **Add chain guidance** to all tool descriptions ("NEXT STEPS" section)

### Short-term (new tools from existing endpoints, no new backend work)

6. `lifeos_vault_read` -- Adapt from Telegram agent's `read_vault_file`
7. `lifeos_person_update` -- PATCH people (notes, tags, category, birthday)
8. `lifeos_reminder_update` -- PUT reminders
9. `lifeos_save_to_vault` -- Save content to Obsidian
10. `lifeos_birthdays` -- Birthday list/calendar
11. `lifeos_crm_statistics` -- CRM overview stats
12. `lifeos_data_health` -- Data quality diagnostics
13. `lifeos_sync_health` -- Sync freshness per source
14. `lifeos_usage_stats` -- API cost tracking
15. `lifeos_family_members` -- Quick family list
16. `lifeos_me_stats` -- Owner's dashboard data
17. `lifeos_relationship_detail` -- Relationship between two specific people
18. `lifeos_tone_analysis` -- Sentiment over time

### Medium-term (new endpoints needed)

19. `lifeos_calendar_create` -- Create calendar events
20. `lifeos_gmail_send` -- Send a draft or compose+send
21. `lifeos_vault_create` / `lifeos_vault_append` -- Write to vault
22. `lifeos_imessage_send` -- AppleScript bridge for iMessage
23. `lifeos_slack_post` -- Post to Slack channels
24. `lifeos_reindex` -- Trigger vault reindex
25. `lifeos_sync_trigger` -- On-demand sync for specific sources
26. `lifeos_person_merge` / `lifeos_review_queue_action` -- CRM maintenance

### Long-term (new infrastructure)

27. `lifeos_job_submit` / `lifeos_job_status` -- Background task queue integration
28. `lifeos_code_task_submit` / `lifeos_code_task_status` -- Queued Claude Code sessions
29. `lifeos_transcribe` -- Local Whisper STT (post hardware upgrade)
30. `lifeos_image_analyze` -- Local vision model (post hardware upgrade)
31. `lifeos_local_llm` -- Local inference for privacy-sensitive queries
32. `lifeos_shortcut_run` -- macOS Shortcuts bridge
33. `lifeos_notify` -- Unified cross-channel notifications
34. `lifeos_daily_briefing` / `lifeos_weekly_review` -- Orchestrated meta-tools

---

## Summary

The five audits reveal a system with deep capabilities trapped behind incomplete interfaces. The backend has 120+ endpoints; MCP exposes 35. The web UI has 25,000 lines of rich features; MCP can replicate maybe 40% of the user experience. The Telegram pipeline has tools the MCP server lacks, and vice versa.

The single most important architectural shift is recognizing that **MCP is becoming the universal API layer**. As AI agents become the primary way users interact with LifeOS (via Telegram, Claude Code, and future clients), the MCP tool surface IS the product's capability surface. Every feature that exists only in the web UI or only in the Telegram pipeline is a feature that AI agents cannot access.

The path forward:
1. **Parity first**: Every existing endpoint should be exposable through MCP
2. **Write tools second**: Break the 2.5:1 read:write imbalance
3. **Chain documentation third**: Make tool sequences self-discoverable
4. **New capabilities last**: Calendar creation, message sending, system administration

With the hardware upgrade on the horizon, the MCP tool surface should be designed to accommodate GPU-accelerated local inference, background job queues, and multi-agent orchestration from day one. The tools built today should have the extensibility to serve as the interface for a dramatically more capable system tomorrow.
