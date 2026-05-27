# LifeOS: Project Vision

> **Status:** Complete
> **Last Updated:** 2026-05-27

## The Problem

Personal data is fragmented across dozens of silos. Emails live in Gmail, messages in iMessage and WhatsApp, notes in Obsidian, tasks in various apps, contacts scattered across platforms, photos in Apple Photos, financial data in banking apps. No single tool has the full picture of a person's life.

This fragmentation means every question requires manual detective work, and every action requires switching apps. "When did I last talk to Sarah?" means checking email, messages, call history, and calendar. "Draft a reply to that thread using what we discussed at lunch" means stitching context together by hand. "Who introduced me to my dentist?" means memory or digging through years of messages.

The information exists. It's just inaccessible — locked in separate apps with separate search systems, none of which understand context across boundaries, and none of which can act on your behalf with that context in mind.

## The Thesis

A personal AI assistant needs two halves working together:

1. **A context layer** that ingests every silo of personal data and resolves entities across them — so any question or action can be grounded in the full picture of your life. This is the *aggregator/maintainer* half: indexes, entity resolution, nightly syncs, semantic + keyword search.
2. **An agentic layer** that uses that context to *do things* on your behalf — drafting, scheduling, researching, prepping, following up — communicating progress as it works rather than only replying when prompted. This is the *autonomous assistant* half: an agent worker that owns multi-step tasks, reports back through Telegram or chat, and surfaces what you need before you ask.

Most personal assistants pick one half. LifeOS does both because they're inseparable — context without action is a search engine; action without context is a generic LLM. The combination is what makes a system useful as a *personal* assistant rather than a generic one.

Both halves run on your own machine. LLM inference is the only thing that ever leaves the box, and only as discrete query payloads — never wholesale data uploads.

## What LifeOS Is

- **A unified personal context layer.** 12+ data sources (notes, email, messages, calendar, contacts, photos, financial data, call history) indexed into a single semantically searchable corpus, with cross-source entity resolution so `john@acme.com`, `+1-555-0100`, and "John from the conference" all resolve to the same person.
- **An agentic assistant that completes tasks autonomously.** An agent worker takes on `#agent`-tagged tasks (or work delegated from chat / Telegram), runs multi-round tool-using sessions backed by the full LifeOS context, and reports progress and results as it goes. Long-running work happens without you having to babysit it.
- **A proactive intelligence layer.** Reminders, briefings before meetings, surfacing of relevant context, nudges about people you haven't talked to in a while — things the assistant decides to tell you rather than waiting to be asked.
- **A personal CRM.** Canonical people, interactions, facts, and relationships, kept in sync with every communication channel.
- **A natural-language interface to all of it.** Chat UI, Telegram, MCP for Claude Code — same context, same tools, same agent capabilities everywhere.

## What LifeOS Is Not

- **Not a cloud service.** Everything runs locally on the user's own machine. Privacy is the foundation, not a feature. LLM inference is the only external call, and it's per-query, payload-scoped, and toggleable to fully local.
- **Not a data platform or infrastructure.** LifeOS is an application — it consumes data from existing sources and makes it useful. It does not replace or abstract storage systems.
- **Not a replacement for individual apps.** Users continue using Gmail, iMessage, Obsidian, etc. LifeOS sits above them, indexing and acting without interfering.
- **Not multi-user.** LifeOS is designed for a single user's personal data. There is no multi-tenant architecture, no user management, no access control beyond the host OS's permissions.
- **Not a chatbot wrapper.** The agentic half does real work — drafting, scheduling, researching across hours and tool calls — not just question-answering.

## Principles

### Privacy Is the Foundation

LifeOS handles the most sensitive data a person has: emails, therapy notes, financial transactions, private messages, personal photos. This data never leaves the local machine except for discrete query payloads sent to Claude for synthesis. There is no telemetry, no analytics, no cloud storage. Privacy is not a feature to be toggled — it is the architectural foundation.

### Local-First, Always

All data ingestion, indexing, and storage happen locally and remain on the machine. The orchestrator LLM is configurable: the default (`LIFEOS_LLM_BACKEND=anthropic`) sends discrete query payloads — the user's query plus the snippets of context the agent decided to retrieve — to the Claude API; `LIFEOS_LLM_BACKEND=local` keeps every LLM call on a local llama-server. Either way, indexes, raw data, and historical content stay on disk and are never wholesale uploaded.

### Intelligence Over Organization

LifeOS does not ask users to organize their data better. It meets data where it lives, in whatever format the source provides. The value comes from understanding and connecting data, not from imposing structure on it. Users should never need to change their workflow to accommodate LifeOS.

### Proactive Over Reactive

The most valuable personal assistant anticipates needs rather than waiting for questions. Reminders about people not contacted recently, briefings before meetings with relevant context, surfacing connections between unrelated-seeming information — these proactive capabilities differentiate LifeOS from a search engine.

### Action, Not Just Answers

A query-response loop is the floor, not the ceiling. The agent worker takes on multi-step tasks — drafting an email and waiting for the user's edits, researching a topic across sources before producing a summary, prepping a meeting and posting the brief to the right channel — and reports back as it works. The system should be able to *finish* things, not just describe what should be done.

### Same Context Everywhere

The same indexed corpus, the same canonical people, and the same tool catalog back every interface: the web chat, Telegram, the agent worker, and MCP for Claude Code. Switching channel never means losing context or capability.

## Scope

### Current

**Context sources:** Communication (email, messages, calls), notes (Obsidian vault), calendar (Google Calendar), tasks (Obsidian Tasks), contacts (Apple Contacts, CRM), financial data (Monarch Money), photos.

**Agentic surfaces:** Web chat UI, Telegram bot, agent worker (autonomous `#agent`-tagged task execution with progress reporting), MCP server (Claude Code and other agent clients).

### Future

Deeper photo intelligence (face recognition, scene understanding), health data integration, location history, browser history, expanded financial analysis.

## Related Documents

### Design Context
- [ADR-003: Two-Tier Data Model](../adr/003-two-tier-data-model.md) — Core data architecture enabling the context layer
- [ADR-004: Hybrid Search](../adr/004-hybrid-search.md) — Vector + BM25 retrieval that powers grounded answers
- [ADR-007: Linux Migration & Local LLM](../adr/007-linux-migration.md) — Foundation for the agentic half running locally
- [ADR-008: Managed Agents Cloud Routing](../adr/008-managed-agents-cloud-routing.md) — How the agent worker delegates to cloud Claude
- [ADR-011: External Agent Ingest](../adr/011-external-agent-ingest.md) — Read-only direct-access path for external agents

### Specifications
- [Data Model](../specs/product/data-model.md) — Entity semantics for the context layer
- [Agent Worker](../specs/product/agent-worker.md) — The autonomous task surface
- [MCP Tools](../specs/product/mcp-tools.md) — Tool catalog shared across all surfaces
- [Security & Privacy](../specs/technical/security-privacy.md) — How privacy principles are implemented

### Operational
- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [Installation](../guides/installation.md) — How to stand up both halves locally
