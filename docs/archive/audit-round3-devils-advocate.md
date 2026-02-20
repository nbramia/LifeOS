# Round 3: Devil's Advocate -- Ruthless Pressure Test

**Auditor:** Claude Opus 4.6 (Engineering Director, 20 years of watching projects drown in ambition)
**Date:** 2026-02-13
**Input:** All 10 audit documents (5 Round 1 + 5 Round 2)
**Mandate:** Kill the weak ideas. Expose hidden complexity. Find the 20% that delivers 80%.

---

## Executive Summary: The Uncomfortable Truth

These audits are well-written, thorough, and deeply seductive. They paint a picture of LifeOS evolving from a capable personal assistant into a Star Trek computer. The problem? **They collectively propose 2-3 engineer-years of work for a single-person project.** Without brutal prioritization, this roadmap becomes a graveyard of half-implemented features.

The system today is impressive. It works. It has a user. The audits risk triggering the **Second System Effect** -- the temptation to redesign everything you learned from the first version into a beautiful, over-engineered second version that never ships.

Let me be clear about what I see: **about 15% of these proposals are genuinely high-value, 35% are reasonable but poorly timed, and 50% are aspirational complexity that will create more problems than they solve.**

---

## 1. Complexity Reality Check

### The "Simple" Task Queue That Isn't Simple

Every audit independently identifies a task queue as the top priority. The Round 2 infrastructure audit proposes Dramatiq + Redis with 5 queue types, 4 worker pools, progress tracking, retry policies, and a monitoring dashboard.

**Reality check:**
- Adding Redis is adding a new service to monitor, back up, and restart.
- Dramatiq workers need their own process management, logging, and health checks.
- The "SQLite-backed queue" alternative (Round 2 backend) sounds simpler but means you're building a custom task queue -- which is exactly the kind of thing that takes 3x longer than expected.
- Queue depth monitoring, dead letter handling, worker crash recovery, graceful shutdown during deployments -- none of this is mentioned but all of it is required.
- **Actual effort: 2-3 weeks, not "2-3 days."**

**The real question:** What SPECIFIC problem does the user hit TODAY that requires a task queue? The answer is: Claude Code blocks other messages for up to an hour. That's it. One specific pain point. You don't need a 5-queue Dramatiq architecture to solve "run Claude Code in a way that doesn't block Telegram messages."

**The 80/20 fix:** Run the Claude Code subprocess without blocking the Telegram listener. You already have a background thread for the bot. Add an `asyncio.create_task()` wrapper for Claude Code execution. Done. No Redis, no Dramatiq, no new infrastructure. Cost: half a day.

### The Event-Driven Architecture Fantasy

Round 2 backend proposes: webhook receivers for Gmail/Calendar/Slack, an internal event bus, SSE push channels, and proactive trigger hooks. Round 2 infrastructure proposes Redis Pub/Sub as a unified event bus. The Telegram audit proposes a ProactiveAgent running every 30 minutes.

**Reality check:**
- Gmail push notifications require a publicly accessible HTTPS endpoint, a Google Cloud Pub/Sub topic, and domain verification. On a Tailscale-only network, this means either Tailscale Funnel (exposing your server to the internet) or a cloud relay.
- Calendar webhooks have the same public endpoint requirement.
- Slack Events API requires a public endpoint with challenge-response verification.
- You're proposing to add 3 external webhook integrations, each with its own auth flow, retry semantics, and failure modes -- to replace a cron job that runs at 3 AM and works fine.

**The real question:** Does the user actually need real-time email sync? The current system has the agentic loop that calls `search_email` which hits the Gmail API directly. The email data IS real-time for queries. The interaction store (batch sync) is for historical tracking and relationship metrics, not for answering "what did John email me about?"

**The 80/20 fix:** Add a second sync run at noon or make sync triggerable via Telegram command (`/sync gmail`). Done. 95% of the freshness benefit, 2% of the complexity.

### The Hybrid Mac Mini + Workstation Architecture

Round 2 infrastructure proposes a split architecture: Mac Mini as data collector, Corsair Workstation as the brain. The Mac syncs iMessage, Photos, and Contacts; the workstation does everything else.

**Reality check:**
- You now have TWO machines to maintain, monitor, and keep in sync.
- Network connectivity between them becomes a dependency. If the Mac Mini's Tailscale connection drops, data collection stops.
- You need to build a "sync agent" that pushes data between machines -- another custom piece of infrastructure.
- Google OAuth tokens may not work across machines (different machine IDs).
- Debugging issues now requires checking logs on two machines.
- Power outages, OS updates, and restarts affect two machines instead of one.

**The real question:** Can the workstation run macOS? If so, skip the split entirely. If it must run Linux, is the added complexity of two machines worth it just for iMessage and Apple Photos?

**The 80/20 fix:** If the workstation runs Linux, keep the ENTIRE LifeOS stack on the workstation and use a scheduled `rsync` or `scp` to pull chat.db, Photos.sqlite, and AddressBook from the Mac Mini. A 10-line shell script, not a "sync agent."

---

## 2. YAGNI Alert: Solutions Looking for Problems

### Autonomy Levels (5-Level System)

The Telegram Round 2 audit proposes a 5-level autonomy system (L1-L5) with an AutonomyClassifier, confirmation gates, and per-level worker routing.

**YAGNI.** This is a classification problem being solved with a taxonomy before understanding if users even want variable autonomy. The current system has TWO effective modes: "agent answers immediately" and "Claude Code asks for plan approval." That's L1 and L4. Users understand this.

Adding 5 levels means: the classifier must be trained/tuned, edge cases between levels create confusion ("is 'draft an email' L2 or L3?"), and the confirmation UX varies per level, creating inconsistent behavior.

**What the user actually needs:** Confirmation before sending emails and creating calendar events. That's it. Hard-code confirmation for those two actions. Don't build a framework.

### Artifact System

The Telegram Round 2 audit proposes an Artifact system with types, vault paths, telegram message IDs, and web URLs.

**YAGNI.** The user asked "how do I get long responses through Telegram?" The answer is: split the message (already implemented) or send as a file attachment. You don't need a formal Artifact dataclass, persistence layer, and cross-channel rendering framework.

**What the user actually needs:** When a response exceeds 4096 characters, offer to send it as a `.md` file via Telegram. 20 lines of code.

### The "Personal API" / "Conversation as Operating System" Concepts

The Telegram Round 2 audit proposes treating LifeOS as a personal API where "the natural language interface IS the primary interface" and conversations become "persistent execution contexts" that "span days" and "have running tasks."

This is a beautiful vision paper. It is not a feature request. Building this would mean:
- Redesigning the conversation model from scratch
- Building a task-conversation association layer
- Creating branch/merge semantics for conversations
- Tracking conversation state across days with staleness handling

**Estimated effort: 4-8 weeks of full-time development.** For a feature that sounds cool but whose user value is unclear. Does the user actually return to conversations days later? Is the current "just ask again" model actually a problem?

### MCP Feature Parity with Frontend

Round 2 MCP proposes: "Every view, chart, and action available in the web UI should be achievable through MCP tools." This means exposing ~120 additional endpoints as MCP tools, including heatmap data, volume charts, network graphs, family dashboards, and tone analysis.

**Who is this for?** MCP tools are consumed by Claude Code. Claude Code cannot render heatmaps or D3.js graphs. Exposing raw heatmap data through MCP so Claude Code can... describe it in text? The web UI exists for visualization. MCP exists for data retrieval and actions. They serve different purposes.

**What actually matters for MCP:** The 5-8 missing write tools (person update, reminder update, vault read, calendar create). Not 120 data visualization endpoints.

### The Observation Layer / User Model

Round 2 Telegram proposes a background "observation layer" that continuously analyzes incoming data to learn tool selection patterns, detect life events, track goals, and build a user model.

**This is a research project, not a feature.** "Detect life events from three calendar entries mentioning 'moving'" requires: NLP entity extraction, event clustering, pattern matching against a taxonomy of life events, false positive filtering, and a notification UX. Each of these is its own engineering challenge.

**What the user actually needs:** The existing communication gap detection (already works) and birthday reminders (already works). Maybe add "tasks due tomorrow" to the morning brief. That's the 80%.

---

## 3. Dependency Hell

### The Redis Dependency Chain

Multiple proposals depend on Redis:
1. Task queue (Dramatiq + Redis)
2. Event bus (Redis Pub/Sub)
3. Caching layer (Redis as cache)
4. Progress tracking (Redis keys)
5. WebSocket state (Redis for connection tracking)

**If Redis goes down, you lose:** task execution, event delivery, caching, progress tracking, and real-time updates. You've created a new single point of failure that's arguably worse than the current architecture where everything runs in-process and "just works."

**The alternative nobody wants to hear:** SQLite handles all of this for a single-user system. SQLite WAL mode for concurrency. A simple `tasks` table for the queue. A `notifications` table for events. In-process Python caching for hot data. It's not sexy, but it has ZERO new dependencies and ZERO new services to monitor.

### The Docker Dependency Chain

Round 2 infrastructure proposes Docker Compose with: API, ChromaDB, Redis, workers, vLLM, scheduler, monitor, Prometheus, Grafana, nginx.

**That's 9 containers.** For a single-user personal assistant. Each container needs: a Dockerfile, health checks, log forwarding, resource limits, restart policies, volume mounts, network configuration, and coordinated upgrades.

**The question nobody is asking:** Is the current "bare metal" deployment actually causing problems? The server runs. The sync works. The bot responds. Docker adds operational overhead for benefits (reproducibility, isolation) that matter for team development and multi-machine deployment, but add pure friction for a solo developer on a single machine.

**When Docker makes sense:** When you migrate to the workstation (new machine setup). Create a Docker Compose file for THAT specific purpose. Don't containerize on the Mac Mini where it's already working.

### The vLLM Dependency

Round 2 infrastructure proposes replacing Ollama with vLLM for production inference, citing batched inference and PagedAttention.

**Who needs batched inference?** A single-user system makes one LLM request at a time. The "continuous batching" benefit of vLLM is for multi-user serving at scale. Ollama with a 72B model on a 32GB GPU will be perfectly adequate for single-user, single-request inference.

**Risk:** vLLM is a fast-moving project with frequent breaking changes. Ollama is simpler, has better model management (`ollama pull`), and works out of the box. The 5% throughput gain from vLLM isn't worth the operational overhead for a personal system.

---

## 4. Maintenance Burden: Features Are Forever

### Split Route Files (5670-line crm.py)

Every audit mentions this. "Split crm.py into 5 files." Sounds easy. But:
- Every IDE reference, every import, every URL route changes.
- Tests that import from `routes.crm` break.
- The MCP server's OpenAPI spec changes (route names may change).
- The frontend's API call paths may change.
- Merge conflicts with any in-flight work.

**Effort: 1-2 days.** But NOT zero risk. And the benefit is... the file is easier to navigate? Any modern IDE can jump to a function in a 5670-line file. The code works. The file size is ugly but not causing bugs.

**Do it when:** You're already making significant changes to the CRM routes. Bundle the split with a feature change. Don't do it as a standalone refactor -- that's pure risk for cosmetic benefit.

### Structured Logging with Correlation IDs

Round 2 backend proposes structlog + request correlation IDs. Round 2 infrastructure proposes JSON logging + Prometheus + Grafana.

**The maintenance burden nobody mentions:** Structured logging means every `logger.info("message")` in 75 service files becomes `logger.info("message", person_id=person_id, source=source, duration_ms=duration)`. That's a codebase-wide change. And it needs to be maintained -- every new log statement needs the right context fields.

**What the user actually needs to debug:** "Why didn't my query return results?" and "Why did the sync fail?" Both are answerable with the current logging + `tail -50 logs/server.log`.

**Do it when:** You hit an actual debugging wall where you can't trace a request. Not preemptively.

### Proactive Intelligence

The Telegram Round 2 audit proposes a ProactiveAgent with 7 check functions running every 30 minutes, smart delivery timing, suppression during focus time, and rate limiting.

**Maintenance burden:** Every check function needs to be maintained, tuned for false positive rates, and adapted as the user's life changes. "You haven't talked to Mom in 18 days" is a notification that gets annoying after the third time. Users will want to configure thresholds per person, suppress specific checks, adjust delivery timing -- and suddenly you're building a notification preferences system.

**The reality:** Prompt-type reminders already do this. "Every morning at 7am, check my calendar and tell me about meetings that need prep" is a one-liner that covers 80% of proactive intelligence. Don't build a framework when the primitive already exists.

---

## 5. The 80/20 Analysis: What Actually Matters

### Backend (80% of value in 20% of work)

1. **Enable WAL mode on all SQLite databases** -- 1 hour, prevents the concurrency time bomb.
2. **Fix launchd plist** -- 2 hours, server survives reboots.
3. **Add daily backup of crm.db** -- 2 hours, protects irreplaceable data.
4. **Add log rotation** -- 1 hour, stops disk fill.
5. **Migrate PersonEntity from JSON to SQLite** -- 1-2 days, eliminates corruption risk.

That's 3 days of work that addresses the 5 most dangerous reliability issues.

### Frontend (80% of value in 20% of work)

1. **Add markdown rendering in chat** -- 1 day with marked.js + highlight.js. Biggest daily UX improvement.
2. **Remove embedded CRM from index.html** -- 2 hours, removes 2500 lines of dead weight.
3. **Extract shared CSS into shared.css** -- 1 day, enables future page development without code duplication.
4. **Add a Tasks page** -- 2-3 days. Backend is complete. The most-requested missing UI.

That's about 1 week. Everything else (calendar views, constellation maps, session replay, relationship dossiers) is nice-to-have that can wait.

### Telegram (80% of value in 20% of work)

1. **Inline keyboards for confirmations** -- 1 day. Immediate UX improvement for approve/reject flows.
2. **Complete reminder agent tool** (add update/delete) -- half day. Removes a capability gap.
3. **Non-blocking Claude Code** -- half day. Wrap the subprocess in an asyncio task so the bot isn't blocked.
4. **Bot command menu** -- 1 hour. Register commands with BotFather for discoverability.

That's 3 days that addresses the top UX complaints.

### MCP (80% of value in 20% of work)

1. **Fix PUT/PATCH handling** -- 1 hour. Actual bug.
2. **Add `lifeos_vault_read`** -- half day. Closes the biggest capability gap.
3. **Add `lifeos_person_update`** -- half day. Enables CRM writes.
4. **Fix `lifeos_health` formatter** -- 30 minutes. Returns useful data instead of "healthy."
5. **Add chain guidance to descriptions** -- 1 day. Makes tools self-documenting.

That's 3 days that covers the critical gaps.

### Infrastructure (80% of value in 20% of work)

1. **Fix launchd plist** -- included above.
2. **Create `data/backups/` and verify backup functionality** -- 30 minutes.
3. **Add cron-based server watchdog** (like chromadb-watchdog.sh) -- 1 hour.
4. **Add log rotation via logrotate/newsyslog** -- 1 hour.
5. **Pin dependency versions in requirements.txt** -- 30 minutes.

That's 1 day. No Docker, no Redis, no systemd, no Prometheus.

### TOTAL: ~3 weeks of focused work addresses the top issues across ALL five domains.

---

## 6. Anti-Patterns Detected

### Second System Effect (Everywhere)

The audits describe a working system and then propose replacing large parts of it with more "elegant" architecture. The task queue proposal replaces working synchronous code with async workers. The event bus replaces working cron jobs. The Docker proposal replaces working bare-metal deployment. The vLLM proposal replaces working Ollama.

**The system works today.** Respect that. Improve it incrementally.

### Premature Abstraction (Autonomy Levels, Artifact System, Intelligence Layer)

Three separate abstraction layers proposed for problems that don't exist yet. Autonomy levels for a system where the user types text into Telegram. An artifact system for a system that returns text. An intelligence layer for three clients that work fine with the current code structure.

**Build the abstraction when you have 3+ concrete cases that need it.** Not before.

### Resume-Driven Development (Prometheus, Grafana, Docker, Redis, Kubernetes mention)

Some proposals read like "technologies I want on my resume" rather than "solutions to user problems." A single-user personal assistant does not need Prometheus, Grafana, Kubernetes, or an nginx reverse proxy. These are solutions for teams serving millions of users.

**Use the boring technology.** Cron, SQLite, Python scripts, shell watchdogs. They work. They're debuggable. They don't need their own monitoring.

### Bikeshedding (Color Inconsistency, Design System, Typography Scale)

The frontend audit spends significant attention on color token inconsistency (`#6366f1` vs `#e94560` vs `#00bcd4`) and font size scales. The user has been using this UI. If the colors were a problem, they would have fixed them. A shared design system is nice but it's a week of work that produces zero new capabilities.

---

## 7. What Would Actually Ship in 1 Month

If I were the engineering director and this person had exactly one month of focused development time, here's what I'd tell them to build:

### Week 1: Reliability Foundation
- [ ] WAL mode on all SQLite databases
- [ ] Fix launchd auto-start
- [ ] Daily backup of crm.db + interactions.db (SQLite `.backup()` to `data/backups/`)
- [ ] Log rotation (RotatingFileHandler or newsyslog)
- [ ] Server watchdog script (cron, every 5 minutes, like chromadb-watchdog)
- [ ] Pin all dependency versions in requirements.txt
- [ ] Migrate PersonEntity from JSON to SQLite-primary

### Week 2: Frontend Essentials
- [ ] Markdown rendering in chat (marked.js + highlight.js)
- [ ] Remove embedded CRM from index.html
- [ ] Extract shared.css (design tokens, nav, modals, buttons)
- [ ] Build Tasks page (list view with context groups, inline editing)

### Week 3: Telegram + MCP Polish
- [ ] Telegram inline keyboards for confirmations
- [ ] Complete reminder agent tool (update/delete)
- [ ] Non-blocking Claude Code execution
- [ ] Bot command menu registration
- [ ] MCP: fix PUT/PATCH, add vault_read, person_update, fix health formatter
- [ ] MCP: add chain guidance to all tool descriptions

### Week 4: High-Value Features
- [ ] Persistent agent memory (inject memories into system prompt)
- [ ] Consolidate compose intent into agentic loop (fixes compound compose)
- [ ] Add `GET /api/vault/file` endpoint (shared by MCP + agent)
- [ ] System health page in web UI (consolidate existing health endpoints)
- [ ] Add notifications aggregation endpoint

### What's NOT in the month:
- Task queue / Redis / Dramatiq
- Event-driven architecture / webhooks
- Docker / containerization
- Structured logging / Prometheus / Grafana
- Autonomy levels / artifact system / observation layer
- Calendar UI / email UI / constellation maps
- Voice messages / vision models
- Local 70B LLM
- Any hardware migration

Those are all post-month work AFTER the foundation is solid.

---

## 8. Risk Assessment for Major Proposals

| Proposal | Biggest Risk | Likelihood | Impact |
|----------|-------------|-----------|--------|
| Task queue (Dramatiq + Redis) | Introduces new failure mode; Redis becomes SPOF | Medium | High |
| Event-driven sync (webhooks) | Public endpoint exposure, complex auth flows, fragile integrations | High | Medium |
| Hybrid Mac + Workstation | Two-machine coordination failures; network dependency | Medium | High |
| Docker Compose (9 containers) | Operational overhead exceeds development velocity gain | High | Medium |
| vLLM replacing Ollama | Breaking changes in fast-moving project; complexity without benefit for single user | Medium | Low |
| 5-level autonomy system | Over-classification; user confusion about when system asks vs. acts | Medium | Medium |
| Proactive intelligence | Notification fatigue; false positives; maintenance of 7+ check functions | High | Medium |
| PersonEntity JSON -> SQLite | Migration bugs; data loss if done carelessly | Low | High |
| Frontend framework migration | Rewrite that never finishes; feature freeze during migration | Very High | Very High |
| Local 70B model | VRAM management; model quality regression for edge cases; serving complexity | Medium | Low |

---

## 9. The "Just Use Existing Tools" Test

| Proposal | Existing Alternative | Verdict |
|----------|---------------------|---------|
| Background task queue for Claude Code | `asyncio.create_task()` wrapper | Use the existing tool |
| Redis for caching | In-process Python dict with TTL (or `cachetools`) | Use the existing tool |
| Redis Pub/Sub event bus | SQLite `notifications` table + polling | Use the simpler approach first |
| Prometheus + Grafana | The existing `/health/services` + `/api/crm/sync/health` endpoints + a web UI page | Build on what exists |
| Structured logging (structlog) | Python's `logging` with a custom JSON formatter | Simpler version of the same thing |
| vLLM for LLM serving | Ollama (already works, simpler model management) | Keep Ollama |
| Docker Compose for deployment | `server.sh` + launchd (already works when plist is fixed) | Fix what's broken |
| nginx reverse proxy | FastAPI serves static files fine for a single user | Unnecessary |
| Command palette (Cmd+K) | Browser URL bar + `/crm?q=sarah` | Nice-to-have, not essential |
| Relationship constellation map | The existing D3.js network graph | Enhance, don't rebuild |

---

## 10. Single Point of Failure Analysis

### Proposals That Make Things MORE Fragile

1. **Redis as universal infrastructure (queue + cache + events + state):** Creates a massive SPOF. Current system has no Redis and works fine. Adding Redis and making 5 subsystems depend on it means Redis failure = total system failure.

2. **Hybrid Mac Mini + Workstation:** Doubles the number of machines that can fail. Network between them becomes a dependency. Currently one machine does everything -- simpler failure domain.

3. **vLLM replacing Ollama:** vLLM is less battle-tested for personal use. Ollama has been running reliably. Switching introduces regression risk for marginal throughput gains.

4. **Event-driven sync replacing batch sync:** Batch sync is simple and reliable -- it either runs or it doesn't. Event-driven sync has N failure modes (one per webhook source) plus the webhook endpoint itself. More moving parts = more failure modes.

5. **Docker Compose with 9 services:** Each service can crash independently. Container networking can fail. Docker daemon itself can fail. Volume mounts can corrupt on ungraceful shutdown. This is more fragile than bare-metal processes managed by launchd/cron.

### Proposals That REDUCE Fragility (Genuinely Good)

1. **WAL mode on SQLite:** Reduces corruption risk during concurrent access. Pure win.
2. **Daily database backups:** Reduces data loss risk. Pure win.
3. **Server watchdog:** Reduces downtime from crashes. Pure win.
4. **PersonEntity migration to SQLite:** Eliminates JSON file corruption risk. Pure win.
5. **Fix launchd plist:** Server survives reboots. Pure win.
6. **Log rotation:** Prevents disk full. Pure win.

Notice the pattern: the genuinely good proposals are all BORING. They don't require new technologies. They fix existing risks with simple, proven solutions.

---

## 11. What's Genuinely Brilliant

Not everything is bad. These ideas survived the pressure test and should be prioritized:

### 1. Prompt-Type Reminders (Already Exists)
The ability to schedule a natural language prompt that runs through the full chat pipeline on a cron schedule is a genuinely powerful primitive. "Every morning at 7am, summarize my calendar and suggest priorities" is a one-liner that provides enormous value. The audits propose building a ProactiveAgent framework around this -- but the primitive is already there and already works. Just create better prompts.

### 2. Agent Memory Integration
Injecting persistent memories into the agent system prompt is a small change (load top-K memories at prompt construction time) with outsized impact. The user can say "remember that I prefer bullet points" once and it persists forever. This is 1 day of work for a meaningfully smarter assistant.

### 3. Non-Blocking Claude Code
Making Claude Code execution non-blocking so the Telegram bot can still answer questions during a long code session. This is the single most impactful concurrency improvement and doesn't require a task queue.

### 4. MCP Vault Read Tool
Adding `lifeos_vault_read` closes the biggest capability gap in the MCP tool surface. The Telegram agent already has this. It's extracting and exposing existing code. Half day of work.

### 5. Telegram Inline Keyboards
This is a pure UX win. Buttons for approve/reject, task selection, and quick actions. Uses Telegram's native API. No backend changes needed. 1 day of work that makes every interactive flow better.

### 6. Consolidating Compose Intent into Agentic Loop
The compound compose problem ("email John using notes from our meeting") is a real user pain point. Routing these through the agent loop instead of the shortcut handler lets the agent gather context before composing. This is a backend refactor, not a new feature, and it makes a real user scenario work correctly.

### 7. The Tasks Web Page
Backend has full CRUD. The API is complete. There's no UI for a core "life OS" feature. A simple list page with context grouping would deliver immediate value. This is the highest-value missing page.

---

## 12. Final Verdict

The audits are thorough and technically excellent. But they suffer from a common ailment: **they optimize for architectural elegance over shipping velocity.** A solo developer maintaining a personal tool needs different advice than a team building a product for thousands of users.

**The system works.** The CRM is deep. The agentic pipeline is impressive. The Claude Code orchestrator is well-designed. The sync infrastructure is comprehensive. The search pipeline is solid.

**The risks are real but manageable.** No backups, no auto-start, no log rotation, potential JSON corruption. These are fixable in days, not months.

**The vision is inspiring but dangerous.** Event-driven architecture, multi-agent orchestration, autonomy levels, observation layers, constellation maps, artifact systems -- these are features for a funded startup with 5 engineers, not a solo developer who also has a day job and a family.

**My advice:** Spend 3 weeks on the boring reliability fixes. Then 1 week on the highest-value features (markdown in chat, tasks page, inline keyboards, MCP fixes). Then stop and USE the system for a month. Let actual usage drive the next round of priorities, not architectural audits.

The best code you can write is the code you don't write.

---

## Appendix: Proposal Scorecard

| Proposal | Value | Effort | Risk | Verdict |
|----------|-------|--------|------|---------|
| WAL mode on SQLite | Very High | Trivial | None | **DO NOW** |
| Fix launchd plist | Very High | Trivial | None | **DO NOW** |
| Daily DB backups | Very High | Low | None | **DO NOW** |
| Log rotation | High | Trivial | None | **DO NOW** |
| PersonEntity -> SQLite | High | Medium | Low | **DO NOW** |
| Markdown in chat | High | Low | None | **DO THIS MONTH** |
| Remove embedded CRM | Medium | Trivial | None | **DO THIS MONTH** |
| Extract shared.css | Medium | Low | None | **DO THIS MONTH** |
| Tasks page | High | Medium | None | **DO THIS MONTH** |
| Inline keyboards | High | Low | None | **DO THIS MONTH** |
| Non-blocking Claude Code | High | Low | Low | **DO THIS MONTH** |
| Agent memory integration | High | Low | Low | **DO THIS MONTH** |
| MCP vault read + person update | High | Low | None | **DO THIS MONTH** |
| Complete reminder agent tool | Medium | Trivial | None | **DO THIS MONTH** |
| Consolidate compose -> agent | High | Medium | Low | **DO THIS MONTH** |
| Bot command menu | Medium | Trivial | None | **DO THIS MONTH** |
| MCP chain guidance | Medium | Low | None | **DO THIS MONTH** |
| System health web page | Medium | Medium | None | **NEXT MONTH** |
| Shared.css for real | Medium | Medium | None | **NEXT MONTH** |
| Notifications endpoint | Medium | Low | None | **NEXT MONTH** |
| Calendar event creation | Medium | Medium | Low | **NEXT MONTH** |
| Task queue (lightweight) | Medium | High | Medium | **WHEN PAIN IS REAL** |
| Structured logging | Low | Medium | Low | **WHEN DEBUGGING FAILS** |
| Voice messages | Medium | Medium | Low | **WITH HARDWARE UPGRADE** |
| Local 70B LLM | Medium | Medium | Medium | **WITH HARDWARE UPGRADE** |
| GPU embeddings | High | Low | Low | **WITH HARDWARE UPGRADE** |
| Split crm.py | Low | Medium | Medium | **WHEN MAKING CRM CHANGES** |
| Docker Compose | Low | High | Medium | **FOR MACHINE MIGRATION ONLY** |
| Event-driven sync | Low | Very High | High | **PROBABLY NEVER** |
| Autonomy levels | Low | High | Medium | **DON'T BUILD** |
| Artifact system | Low | Medium | Low | **DON'T BUILD** |
| Observation layer | Low | Very High | High | **DON'T BUILD** |
| Prometheus + Grafana | Low | Medium | Low | **DON'T BUILD** |
| Redis event bus | Low | High | High | **DON'T BUILD** |
| Relationship constellation map | Low | High | None | **DON'T BUILD** |
| Session replay | Low | Very High | Medium | **DON'T BUILD** |
| Conversation as workspace | Low | Very High | High | **DON'T BUILD** |
| MCP feature parity with frontend | Low | Very High | Low | **DON'T BUILD** |
