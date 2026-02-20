# LifeOS: Project Vision

> **Status:** Complete
> **Last Updated:** 2026-02-19

## The Problem

Personal data is fragmented across dozens of silos. Emails live in Gmail, messages in iMessage and WhatsApp, notes in Obsidian, tasks in various apps, contacts scattered across platforms, photos in Apple Photos, financial data in banking apps. No single tool has the full picture of a person's life.

This fragmentation means every question requires manual detective work. "When did I last talk to Sarah?" requires checking email, messages, call history, and calendar. "What did we decide about the project?" requires searching email threads, meeting notes, and Slack channels. "Who introduced me to my dentist?" requires memory or digging through years of messages.

The information exists. It's just inaccessible — locked in separate apps with separate search systems, none of which understand context across boundaries.

## The Thesis

The missing piece is an AI assistant with full personal context. Not another app to organize data into — a layer that sits above all existing apps and unifies their data for search, synthesis, and proactive intelligence.

LifeOS indexes personal data from every source, resolves entities across platforms (recognizing that john@acme.com in Gmail and "+1-555-0100" in iMessage are the same person), and makes the unified corpus searchable through natural language. Claude provides synthesis — answering questions that span multiple sources, generating briefings, and proactively surfacing relevant context.

## What LifeOS Is

- **Self-hosted AI assistant** for personal data indexing, search, and synthesis.
- **Unifier** across 12+ data sources: notes, emails, messages, calendar, contacts, photos, financial data, call history.
- **Semantic search engine** combining vector similarity and keyword matching for natural language queries over personal data.
- **Personal CRM** tracking relationships, interactions, and facts about people across all communication channels.
- **Proactive intelligence** layer with reminders, briefings, and relationship tracking.

## What LifeOS Is Not

- **Not a cloud service.** Everything runs locally on a Mac Mini. Privacy is the foundation, not a feature.
- **Not a data platform or infrastructure.** LifeOS is an application — it consumes data from existing sources and makes it useful. It does not replace or abstract storage systems.
- **Not a replacement for individual apps.** Users continue using Gmail, iMessage, Obsidian, etc. LifeOS sits above them, indexing and unifying without interfering.
- **Not multi-user.** LifeOS is designed for a single user's personal data. There is no multi-tenant architecture, no user management, no access control beyond macOS permissions.

## Principles

### Privacy Is the Foundation

LifeOS handles the most sensitive data a person has: emails, therapy notes, financial transactions, private messages, personal photos. This data never leaves the local machine except for discrete query payloads sent to Claude for synthesis. There is no telemetry, no analytics, no cloud storage. Privacy is not a feature to be toggled — it is the architectural foundation.

### Local-First, Always

All data processing, indexing, and storage happens locally. External API calls (Claude for synthesis, Ollama for routing) send minimal context and receive only generated text. The system must function fully if the network is unavailable — only Claude synthesis requires connectivity.

### Intelligence Over Organization

LifeOS does not ask users to organize their data better. It meets data where it lives, in whatever format the source provides. The value comes from understanding and connecting data, not from imposing structure on it. Users should never need to change their workflow to accommodate LifeOS.

### Proactive Over Reactive

The most valuable personal assistant anticipates needs rather than waiting for questions. Reminders about people not contacted recently, briefings before meetings with relevant context, surfacing connections between unrelated-seeming information — these proactive capabilities differentiate LifeOS from a search engine.

## Scope

### Current

Communication (email, messages, calls), notes (Obsidian vault), calendar (Google Calendar), tasks (Obsidian Tasks), contacts (Apple Contacts, CRM), financial data (Monarch Money).

### Future

Deeper photo intelligence (face recognition, scene understanding), health data integration, location history, browser history, expanded financial analysis.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [ADR-001: Python/FastAPI](../adr/001-python-fastapi.md) — Backend framework choice
- [ADR-003: Two-Tier Data Model](../adr/003-two-tier-data-model.md) — Core data architecture
- [Data Model](../specs/product/data-model.md) — Entity semantics
- [Security & Privacy](../specs/technical/security-privacy.md) — How privacy principles are implemented
