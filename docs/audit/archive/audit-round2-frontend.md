# Round 2: Frontend/UX Cross-Pollination Analysis

**Auditor:** Claude Opus 4.6 (Senior Frontend Engineer & UX Designer)
**Date:** 2026-02-13
**Input:** All five Round 1 audits (Backend, Frontend, MCP, Telegram/Chat, Infrastructure)

---

## Table of Contents

1. [The Command Center Vision](#1-the-command-center-vision)
2. [UI for Agentic Operations](#2-ui-for-agentic-operations)
3. [Missing Interfaces](#3-missing-interfaces)
4. [Mobile-First Rethink](#4-mobile-first-rethink)
5. [Data Visualization Opportunities](#5-data-visualization-opportunities)
6. [New Ideas from the Full Picture](#6-new-ideas-from-the-full-picture)
7. [Concrete Implementation Priorities](#7-concrete-implementation-priorities)

---

## 1. The Command Center Vision

### The Problem Today

The web UI is split across 3 monolithic HTML files with no shared design system. Chat lives in `index.html` (5,267 lines). CRM lives in `crm.html` (19,566 lines). A simple landing page in `home.html` (164 lines) shows two cards. Meanwhile, the backend has ~120+ API endpoints, 75 service files, 9 tracked services, 20 sync sources, a task queue system, a reminder scheduler, Claude Code orchestration, and cost tracking -- almost none of which is surfaced in the web UI.

The MCP audit reveals 35 tools exposed to Claude Code (only 22% of available endpoints). The Telegram audit shows a capable agentic pipeline with 13 tools. The infrastructure audit reveals 5 running services, 6 databases totaling 2.3 GB, and a daily sync pipeline with 20 sources across 6 phases. All of this operational complexity is invisible to the user.

### The Unified Dashboard

The command center should be a single-page application with a persistent sidebar and a main content area. Think of it as the bridge of a starship -- every system at a glance, deep dive on demand.

**Primary Navigation (Left Sidebar)**:
- **Home / Dashboard** -- system pulse, today's priorities
- **Chat** -- conversational AI interface
- **People** -- CRM with all existing depth
- **Calendar** -- week/month view with meeting prep
- **Tasks** -- Kanban or list view with context grouping
- **Reminders** -- schedule management
- **Search** -- unified cross-source search
- **System** -- health, syncs, costs, admin

**Dashboard Home (the "Pulse" view)**:

This is the screen you see when you open LifeOS. It answers: "What do I need to know right now?"

```
+---------------------------------------------------------------+
| LifeOS                                    [Search] [N alerts]  |
+---------------------------------------------------------------+
|          |                                                     |
| Nav      |  Good morning, Nathan.            [System: Healthy] |
|          |                                                     |
|  Home    |  +-- TODAY'S AGENDA ------+  +-- OPEN TASKS ------+ |
|  Chat    |  | 9:00 AM  1:1 w/ Sarah |  | [!] Review Q4 plan | |
|  People  |  |   [Prep Ready]        |  | [ ] Call dentist    | |
|  Calendar|  | 11:00 AM Team standup |  | [ ] Email John re:  | |
|  Tasks   |  | 2:00 PM  Client call  |  |     contract        | |
|  Reminders  |   [3 prep notes]      |  +--------------------+ |
|  Search  |  +------------------------+                         |
|  System  |                                                     |
|          |  +-- NEED ATTENTION ------+  +-- ACTIVE JOBS -----+ |
|          |  | Mom (18 days)          |  | Morning brief [done]| |
|          |  | Kevin (22 days)        |  | Sync: 3:00 AM [ok] | |
|          |  | Alex (31 days) [!]     |  | Claude Code: idle   | |
|          |  +------------------------+  +--------------------+ |
|          |                                                     |
|          |  +-- RECENT CONVERSATIONS --------------------------+|
|          |  | "Tell me about the Tokyo trip..." (2h ago)       ||
|          |  | "Draft email to Sarah about..." (yesterday)     ||
|          |  +--------------------------------------------------+|
+---------------------------------------------------------------+
```

**Key design principles**:
- Every widget links to its detail view (click "Open Tasks" heading to go to Tasks page)
- "Need Attention" comes from the communication gaps API (`/api/crm/family/communication-gaps`)
- "Active Jobs" shows Claude Code session status, last sync result, and next scheduled reminder
- Calendar items link to meeting prep (`/api/calendar/meeting-prep`)
- Everything is real-time via SSE push (not just polling)

---

## 2. UI for Agentic Operations

### The Gap

The Telegram audit reveals a powerful agentic pipeline: 5 tool rounds, parallel tool execution, Claude Code orchestration with plan-approve-implement workflows, and scheduled prompt-type reminders. But the web UI shows none of this. When you ask a question in chat, you see "Thinking..." dots and then a response. The rich intermediate process -- tool selection, parallel execution, source gathering, synthesis -- is invisible.

The backend audit confirms: the SSE stream emits `status`, `routing`, `sources`, and `usage` events that the web UI mostly ignores.

### Agentic Pipeline Visualization

When the chat initiates an agentic query, the UI should show a step-by-step visualization:

```
+-- Your question: "Prep me for my 2pm meeting with Sarah" -----+
|                                                                 |
| Step 1: Understanding intent...                         [done]  |
|   Route: calendar + people + vault                              |
|                                                                 |
| Step 2: Gathering data (3 tools in parallel)           [done]  |
|   [calendar] Fetching today's events............... 420ms       |
|   [people]   Looking up Sarah Chen................. 380ms       |
|   [vault]    Searching meeting notes............... 650ms       |
|                                                                 |
| Step 3: Deep context (2 tools)                         [done]  |
|   [email]    Recent emails with Sarah.............. 1.2s        |
|   [vault]    Reading "Project Alpha Notes.md"...... 180ms       |
|                                                                 |
| Step 4: Synthesizing with Claude Sonnet 4.5            [active] |
|   Tokens: 3,200 in / ~800 out | Est. cost: $0.02              |
|                                                                 |
+-- Response streaming below -------- [Collapse pipeline view] --+
```

This is collapsible -- power users see it, casual users can ignore it. The key insight from the Telegram audit is that tool execution already emits `status` SSE events. The web UI just needs to render them.

### Claude Code Session Monitor

The Telegram audit describes Claude Code orchestration with plan mode, clarification, heartbeat, and cost tracking. The web UI should have a dedicated panel for this:

```
+-- Claude Code Session: "Refactor sync pipeline" ---------------+
|                                                                 |
| Status: IMPLEMENTING        Elapsed: 4m 32s       Cost: $0.89  |
|                                                                 |
| [====================>            ] 60% (est.)                  |
|                                                                 |
| Plan (approved):                                                |
|   1. Extract phase runner from run_all_syncs.py        [done]   |
|   2. Create async task wrappers                        [active] |
|   3. Add parallel execution for Phase 1                [pending]|
|   4. Update sync health tracking                       [pending]|
|                                                                 |
| Recent activity:                                                |
|   [4:31] Editing api/services/sync_runner.py                    |
|   [4:28] Reading scripts/run_all_syncs.py                       |
|   [4:25] [NOTIFY] Phase runner extracted, 3 tests passing       |
|                                                                 |
| [Cancel Session]  [Send Message]                                |
+----------------------------------------------------------------+
```

The infrastructure audit confirms Claude Code sessions track cost, elapsed time, turn count, and activity. The `[NOTIFY]` messages and heartbeats provide the data. The web UI provides a richer canvas than Telegram's 4096-character text limit.

### Approval Workflows

Both the Telegram and backend audits highlight that Claude Code has plan-approve-implement and clarification workflows. The web UI should make these first-class:

- **Plan approval**: Show the plan as a checklist with "Approve" / "Reject" / "Edit Plan" buttons
- **Clarification**: Inline chat bubble within the session panel
- **Destructive action warning**: The backend audit notes `--dangerously-skip-permissions`. The web UI could add a confirmation layer for file deletions, force pushes, etc. -- something Telegram can't easily do

---

## 3. Missing Interfaces

### Backend Capabilities with Zero UI

Cross-referencing the backend API map (120+ endpoints) against the frontend audit (3 HTML files):

| Capability | Backend Endpoints | Web UI | Priority |
|-----------|------------------|--------|----------|
| **Task Management** | Full CRUD (`/api/tasks`) | None | Critical |
| **Reminder Management** | Full CRUD + trigger (`/api/reminders`) | None | Critical |
| **System Health** | `/health/full`, `/health/services`, sync health | None (scattered modals) | High |
| **Calendar View** | `/api/calendar/upcoming`, `/search`, `/meeting-prep` | None | High |
| **API Cost Tracking** | `/api/admin/usage`, per-conversation costs | Hidden in chat modal | High |
| **Sync Dashboard** | `/api/crm/sync/health/summary`, 20 sync sources | None | High |
| **Email View** | `/api/gmail/search`, `/drafts` | None | Medium |
| **Unified Search** | `/api/search`, all source-specific search endpoints | Chat-only | Medium |
| **Briefing Generator** | `/api/briefings`, `/api/briefings/{person}` | None | Medium |
| **Admin Panel** | `/api/admin/reindex`, processor management, calendar sync triggers | None | Medium |
| **Slack Browser** | `/api/slack/search`, conversations | None | Lower |
| **iMessage Browser** | `/api/imessage/search`, conversations, stats | None | Lower |
| **Drive File Browser** | `/api/drive/search` | None | Lower |

### Highest-Value Missing Interfaces

**1. Task Management Page**

The MCP audit confirms task tools have full CRUD coverage and Obsidian-compatible markdown storage. This is a core "life OS" feature with rich backend support and zero UI.

Design: A list view grouped by context (Work, Personal, Finance, Inbox) with inline editing, priority indicators, due date badges, and tag chips. Not a Kanban board -- that adds complexity without value for a personal system. Keep it simple: a smart list with filters.

**2. System Dashboard**

The infrastructure audit reveals: 5 running services, 9 tracked health services, 6 databases (2.3 GB total), daily sync with 20 sources, log files accumulating without rotation, and a broken launchd auto-start. None of this is visible.

Design: A single "System" page showing:
- Service status grid (healthy/degraded/down with last-check timestamp)
- Sync timeline (Gantt-like bar chart showing last sync per source with freshness color coding)
- Database sizes and growth trends
- API cost chart (daily/weekly/monthly spend by model)
- Log tail (last 50 lines, filterable by level)
- Action buttons: trigger sync, trigger reindex, restart service

**3. Calendar Integration**

The backend has calendar upcoming, search, and the powerful meeting prep endpoint. The meeting prep endpoint aggregates CRM data, past meetings, vault notes, and related documents -- this is a unique value proposition that deserves a dedicated view.

Design: A week view with event cards. Click an event to see the meeting prep panel: attendees with CRM links, related notes, past meetings with these people, suggested talking points.

---

## 4. Mobile-First Rethink

### The Current Split

- **Telegram**: Primary mobile interface. Text-only input, 4096-char message limit, no inline keyboards, no voice/image support. But it's always in your pocket.
- **Web UI**: Desktop-oriented. No PWA manifest, no service worker, no push notifications. Rich CRM visualization. Monolithic files.

### The Complementary Model

Telegram and the web UI should serve fundamentally different purposes:

| Dimension | Telegram | Web UI |
|-----------|----------|--------|
| **Primary use** | Quick questions, actions, notifications | Deep analysis, visualization, management |
| **Interaction model** | Conversational (text in, text out) | Direct manipulation (click, drag, browse) |
| **Session length** | Seconds to minutes | Minutes to hours |
| **Context** | On the go, interruption-driven | Focused, desktop, deliberate |
| **Strength** | Ubiquity, speed, push notifications | Rich visualization, multi-panel layouts |

### What This Means for the Web UI

The web UI should NOT try to be a mobile chat app. That's Telegram's job. Instead, the web UI should be:

1. **The monitoring dashboard** -- see everything at a glance (system health, upcoming events, neglected relationships, active tasks)
2. **The power-user CRM** -- this already exists and is strong. Keep investing here.
3. **The session reviewer** -- review what Claude Code did, what the morning briefing contained, what happened in overnight syncs
4. **The configuration interface** -- manage reminders, set up scheduled briefings, configure sync preferences, review costs
5. **The visualization engine** -- relationship graphs, interaction heatmaps, communication patterns, cost trends

### What This Means for Telegram

The Telegram audit's top recommendations (inline keyboards, voice support, background task queue) are exactly right. Telegram should get:
- Button-based confirmations for actions
- Voice message transcription for hands-free use
- Progress notifications for long-running tasks
- Rich message formatting (within Telegram's limits)

The web UI then becomes where you go to see the FULL picture of what Telegram summarized.

### PWA: Bridging the Gap

A PWA with service worker would let the web UI work offline (cached people, cached conversations) and receive push notifications. This bridges the gap between "always in your pocket" (Telegram) and "rich desktop experience" (web):
- Push notification: "You haven't talked to Mom in 3 weeks" -> tap -> opens CRM person view
- Push notification: "Claude Code finished refactoring sync" -> tap -> opens session review
- Offline: Browse cached CRM data, read past conversations, review tasks

---

## 5. Data Visualization Opportunities

### What Exists

The CRM already has impressive visualizations:
- 365-day interaction heatmap (GitHub-style)
- D3.js force-directed network graph
- Relationship strength ring (SVG)
- Volume line chart
- Family communication gaps timeline
- Tone timeline
- Depth bubbles
- Weekly rhythm chart

### What's Missing

Cross-referencing the backend data model with visualization potential:

**1. Relationship Constellation Map**

The backend's `relationship_discovery.py` (53KB) discovers connections via co-occurrence. The CRM has Dunbar circle classification. Combine these into a zoomable constellation:

- Center: you
- Inner ring: Dunbar circle 1 (5 people)
- Middle ring: Dunbar circle 2 (15 people)
- Outer ring: Dunbar circle 3 (50 people)
- Lines between nodes show shared context (shared meetings, email threads)
- Node color = relationship trend (warming = green, cooling = red, stable = blue)
- Node size = interaction frequency
- Pulsing glow = "needs attention" (communication gap exceeded threshold)
- Click a node: slide-in panel with person detail
- Click a line between two nodes: shows the relationship detail (`/api/crm/relationship/{a}/{b}`)

This replaces the current flat force-directed graph with something that tells a story.

**2. Life Timeline**

The interaction store has every email, meeting, message, call, and vault note with timestamps. The person timeline exists per-person but there's no global timeline. Imagine:

- Horizontal scrollable timeline (weeks/months/years)
- Swim lanes per communication channel (email, iMessage, Slack, calendar, phone)
- Density heat: color intensity shows volume
- Overlay: life events from vault notes (birthdays, milestones, trips)
- Click any cluster: expand to see individual interactions
- Filter by person or group

This turns years of communication data into a visual autobiography.

**3. Sync Pipeline Visualization**

The infrastructure audit describes a 6-phase, 20-source sync pipeline. Visualize it as a pipeline diagram:

```
Phase 1 (Data)          Phase 2 (Entity)      Phase 3 (Relations)
+--------+  +--------+  +--------+            +--------+
| Gmail  |  | Slack  |  | Link   |            | Discover|
| [2.3s] |  | [4.1s] |  | Source |            | Rels   |
| 142 new|  | 89 new |  | [12s]  |            | [45s]  |
+--------+  +--------+  +--------+            +--------+
+--------+  +--------+  +--------+            +--------+
| Cal    |  | iMsg   |  | Merge  |            | Compute|
| [1.8s] |  | [FDA]  |  | Entities            | Strength
| 23 new |  | [OK]   |  | [8s]   |            | [30s]  |
+--------+  +--------+  +--------+            +--------+
```

Each box shows: source name, last run time, record count, status color (green/yellow/red). Click to see detailed logs.

**4. Cost Attribution Dashboard**

The backend tracks per-conversation API costs. The cost tracker has model pricing. Visualize:

- Daily spend bar chart (stacked by model: Haiku/Sonnet/Opus)
- Cost per conversation (top 10 most expensive)
- Claude Code session costs (separate budget visualization)
- Trend line: cost trajectory vs. usage growth
- Budget alert: configurable daily/weekly/monthly budget with visual threshold

**5. Communication Balance Radar**

For any person, show a radar chart of communication dimensions:
- Frequency (how often)
- Recency (how recently)
- Diversity (how many channels)
- Reciprocity (sent vs. received ratio)
- Depth (average message length / meeting duration)
- Consistency (variance in contact frequency)

This gives an instant "shape" to each relationship that numbers alone can't convey.

---

## 6. New Ideas from the Full Picture

These ideas only emerge from reading all five audits together:

### 6.1 The "Ghost in the Machine" Notification Layer

**Insight**: The infrastructure audit shows a health check at 2:30 AM and 7 AM. The Telegram audit shows prompt-type reminders can run any query on schedule. The MCP audit shows communication gap detection. The backend has relationship strength scoring with warming/cooling trends.

**Idea**: A proactive notification engine that combines all of these into a unified "ambient awareness" system:

- **Morning brief** (already possible via prompt reminder, but deserves a web UI view): Today's meetings with prep, overdue tasks, relationship alerts, system health
- **Relationship nudges**: "You and Sarah used to talk weekly. It's been 3 weeks." (Communication gap + historical pattern)
- **Calendar prep**: 30 minutes before a meeting, push a notification with the meeting prep data
- **Sync anomalies**: "Gmail sync found 0 emails today. Usually there are 40+. Check if sync is working."
- **Cost alerts**: "You've spent $5.40 on Claude API today, which is 2x your daily average."

The web UI shows a notification center. Telegram delivers the push. Both link to the same deep-dive view.

### 6.2 "Replay" Mode for Agentic Sessions

**Insight**: The backend audit notes Claude Code sessions have no output capture. The Telegram audit notes no persistent session history beyond 5 minutes. The MCP audit shows conversation history is available but not leveraged.

**Idea**: Record and replay agentic sessions. When the chat pipeline or Claude Code runs a complex query, capture the full trace: every tool call, every intermediate result, every model decision. Store this as a "session recording."

The web UI provides a replay viewer:
- Step through the agent's decisions
- See what data each tool returned
- Understand why the agent chose certain tools
- Compare: "last time I asked this, the agent took a different path"
- Learn: "the agent called person_info before search_email -- that's a good pattern"

This turns the opaque AI into a transparent reasoning system. It also provides debugging data when the agent gives wrong answers.

### 6.3 "Relationship Dossier" -- One-Page Briefing

**Insight**: The MCP audit shows the multi-tool workflow (people_search -> profile -> timeline -> facts -> connections). The Telegram audit shows meeting prep aggregates multiple sources. The backend has relationship insights from therapy notes and tone analysis.

**Idea**: A single-page, printable "dossier" for any person, generated on demand:

```
+-- SARAH CHEN -- Relationship Dossier -- Feb 2026 -----------------+
|                                                                     |
| VITALS                          | RELATIONSHIP SHAPE               |
| Company: Acme Corp              |    [Radar chart: frequency,      |
| Role: VP Engineering            |     recency, diversity,          |
| Last contact: 3 days ago        |     reciprocity, depth]          |
| Strength: 78/100 (warming)      |                                  |
| Dunbar circle: 2                |                                  |
|                                 |                                  |
| FACTS                           | COMMUNICATION PATTERN            |
| Birthday: March 15              |   [Heatmap: day-of-week x       |
| Kids: 2 (ages 8, 11)           |    hour-of-day interaction]      |
| Interests: hiking, photography  |                                  |
| Met: 2023 at tech conference    |                                  |
|                                 |                                  |
| RECENT INTERACTIONS (30 days)   | SHARED CONNECTIONS               |
| 4 emails, 2 meetings,          |   Kevin (12 shared meetings)     |
| 15 Slack messages               |   Alex (8 shared threads)        |
|                                 |   Maria (5 shared events)        |
| KEY TOPICS                      |                                  |
| - Project Alpha launch date     | NOTES                            |
| - Q1 budget approval            | "Prefers morning meetings.       |
| - Team offsite planning         |  Responsive on Slack."           |
+-------------------------------------------------------------------+
```

Generates from: person_profile + person_facts + person_timeline + person_connections + relationship_insights + tone_analysis. All endpoints exist. Just needs a UI to compose them.

### 6.4 Cross-Platform Command Palette

**Insight**: The frontend audit notes no keyboard shortcuts beyond Enter/Shift+Enter. The MCP audit shows 35 tools. The Telegram audit shows `/code`, `/new`, `/clear`, `/status` commands.

**Idea**: A universal command palette (Cmd+K) that works everywhere in the web UI:

```
+-- Command Palette (Cmd+K) ------------------------------------+
| > search people sarah                                          |
|                                                                |
|   [People]    Search for "sarah" in CRM                        |
|   [Chat]      Ask about Sarah                                  |
|   [Calendar]  Find meetings with Sarah                         |
|   [Email]     Search emails from Sarah                         |
|   [Task]      Create task about Sarah                          |
|   [Claude]    Run Claude Code task about Sarah                 |
|   [Reminder]  Set reminder about Sarah                         |
|                                                                |
| Recent:                                                        |
|   [Chat]      "Prep me for tomorrow's meetings"                |
|   [System]    Check sync health                                |
|   [Task]      Mark "Review Q4 plan" complete                   |
+----------------------------------------------------------------+
```

This is the keyboard-driven entry point for everything. It queries multiple backends simultaneously and presents unified results. It also doubles as the "universal search" that's missing from the home page.

### 6.5 The "What Happened While I Was Away" View

**Insight**: The sync runs at 3 AM. Prompt reminders can generate briefings. The interaction store captures all data. But there's no view that says "here's what changed since you last looked."

**Idea**: A changelog-style view for your life data:

```
Since you last checked in (8 hours ago):

NEW EMAILS (12)
  3 from Sarah Chen (Project Alpha)
  2 from Kevin (meeting reschedule)
  7 others

NEW MESSAGES (8)
  Mom: "Call me when you get a chance"
  Alex: 3 messages about weekend plans

CALENDAR CHANGES
  Tomorrow 2pm: "Client Review" moved to 3pm
  New event: "Team Lunch" Thursday 12:30

CRM UPDATES (from overnight sync)
  2 new source entities linked
  1 relationship strength change: Kevin (+5 to 72)

SYSTEM
  All syncs completed at 3:42 AM (18 sources OK, 2 skipped)
  API cost yesterday: $1.23
```

This is the "morning brief" as a web page instead of a Telegram message. It provides the full context that Telegram's 4096-character limit can't.

### 6.6 MCP Tool Playground

**Insight**: The MCP audit reveals 35 tools with varying description quality, missing formatters, and schema issues. Testing these requires running Claude Code. There's no way to manually invoke a tool and see the raw result.

**Idea**: A developer-facing "Tool Playground" in the System/Admin section:

- List all 35 MCP tools with their schemas
- Fill in parameters and execute
- See raw JSON response + formatted response
- Compare MCP tool output vs. direct API response
- Test tool chains: output of tool A becomes input of tool B

This accelerates development and debugging of the MCP layer. It also helps validate that tool descriptions match actual behavior.

---

## 7. Concrete Implementation Priorities

### Phase 1: Foundation (Weeks 1-3)

**Goal**: Shared design system + missing critical pages

1. **Extract shared CSS/JS** -- Create `shared.css` and `shared.js` with design tokens, reset styles, nav component, modal system, and utility functions. Import from all pages. This is the single highest-leverage change for maintainability.

2. **Proper markdown rendering in chat** -- Replace the 6-line regex `formatContent()` with marked.js + highlight.js. This affects every AI response and is the biggest day-to-day UX gap.

3. **Task management page** -- List view with context grouping, priority badges, due dates, inline editing. Backend is complete.

4. **Command palette (Cmd+K)** -- Universal search + navigation. Start simple: search people, search conversations, navigate pages. Extend later.

### Phase 2: Operational Visibility (Weeks 4-6)

**Goal**: See what the system is doing

5. **System dashboard** -- Service health grid, sync timeline, cost chart, action buttons. Pull from `/health/services`, `/api/crm/sync/health/summary`, `/api/admin/usage`.

6. **Agentic pipeline visualization in chat** -- Render the `status` SSE events as a collapsible step-by-step view above the response.

7. **Reminder management page** -- List all reminders with schedule visualization, next fire time, enable/disable toggles, inline editing. Backend has full CRUD.

8. **Notification center** -- Aggregate birthdays, communication gaps, sync failures, cost alerts into a persistent notification area with badge count in the nav.

### Phase 3: Rich Visualizations (Weeks 7-10)

**Goal**: Make data come alive

9. **Calendar view** -- Week view with meeting prep integration. Click event to see attendees with CRM links, related notes, past meetings.

10. **Relationship constellation map** -- Zoomable Dunbar-circle visualization replacing the flat force-directed graph.

11. **Cost attribution dashboard** -- Daily spend chart, per-conversation costs, Claude Code session budgets, trend lines.

12. **"What happened while I was away" view** -- Changelog of new emails, messages, calendar changes, CRM updates, sync results since last visit.

### Phase 4: Power Features (Weeks 11+)

13. **Session replay for agentic queries** -- Full trace capture and step-through viewer.
14. **Relationship dossier generator** -- One-page printable briefing from all CRM data.
15. **PWA manifest + service worker** -- Offline support, push notifications, home screen install.
16. **Claude Code session monitor** -- Real-time web UI for watching and interacting with running sessions.

---

## Summary

The fundamental insight from reading all five audits is this: **LifeOS has a remarkably capable backend (120+ endpoints, 75 services, 20 sync sources, 9 tracked services) behind a remarkably thin frontend (3 HTML files, 2 real pages)**. The CRM is deep but everything else -- tasks, reminders, calendar, system health, cost tracking, sync monitoring, Claude Code sessions -- is invisible or accessible only through Telegram.

The web UI's role is not to compete with Telegram for quick interactions. Its role is to be the **observatory** -- the place where you see the full picture of your digital life, the operational state of the system that manages it, and the reasoning behind the AI decisions that assist you. Telegram is where you talk to LifeOS. The web UI is where you understand it.

The biggest architectural change needed is extracting a shared design system from the monolithic files. Without that, every new page means duplicating thousands of lines of CSS and JS. With it, new pages become composable from existing components -- and the 19,566-line CRM file can finally be tamed.

The single most impactful new feature is the **System Dashboard**. Right now, the infrastructure audit reveals a broken launchd auto-start, missing backup directories, and accumulating log files -- all invisible. Making operational health visible is the precondition for operational excellence.
