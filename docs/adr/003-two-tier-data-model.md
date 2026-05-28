# ADR-003: Two-Tier Data Model (SourceEntity / PersonEntity)

**Status:** Complete
**Last Updated:** 2026-02-19
**Decision:** Accepted

## Context

LifeOS ingests data from 12+ sources: Gmail, Google Calendar, iMessage, WhatsApp, Slack, Apple Contacts, Apple Photos, phone/FaceTime call history, Obsidian vault, and more. The system's core value — being a personal AI assistant with full context — depends on unifying this data into a coherent model.

The same person appears across these sources with different identifiers: an email in Gmail, a phone number in iMessage and call history, a Slack user ID in Slack, a display name in Obsidian notes. A single contact might appear as `alex@example.com`, `+1-555-0100`, `U04ABCD`, and "Alex Chen" — the system must recognize these as the same person while preserving the raw data for debugging, re-processing, and audit.

Entity resolution — determining which observations refer to the same real-world person — is inherently imperfect. Heuristics will incorrectly merge distinct people who share a name, or fail to merge the same person across sources with no overlapping identifiers. The data model must support correcting these errors without re-ingesting from original sources, which may be slow, rate-limited, or unavailable.

## Decision

Two-tier model:

1. **SourceEntity** (raw observations): Immutable records from each data source. Each contains exactly what the source provided — an email, a phone number, a message, a calendar event. Never modified after creation.

2. **PersonEntity** (canonical records): Merged records representing a single real person. Created by entity resolution, which links SourceEntities via shared identifiers (email, phone, name). Contains consolidated contact info, facts, and interaction history.

Entity resolution runs as a batch process that maps SourceEntities to PersonEntities. The mapping is stored separately and can be re-computed without modifying source data.

## Rationale

- **Lossless data**: Raw observations are never modified. If resolution makes a mistake, the original data is intact for correction.
- **Re-runnable resolution**: Entity resolution can be re-executed with improved heuristics without re-ingesting from sources. This proved critical during development when the algorithm was refined multiple times.
- **Provenance tracking**: Every fact about a person traces back to a specific source observation with a timestamp, enabling "where did we learn this?" queries.
- **Merge/split support**: When resolution incorrectly merges two people, the PersonEntity can be split and the SourceEntities reassigned without data loss.

## Alternatives Considered

### Single-Entity Model

Merge data in-place as it arrives: when a new email is ingested, immediately add it to the matching person record, overwriting or appending fields.

**Rejected because:** Merging in-place is destructive. If resolution incorrectly links two people, untangling their merged data requires re-ingesting from original sources. Provenance is lost — there's no record of which source provided which fact. And re-running resolution with improved heuristics requires a full re-ingest, not just a re-computation. For a system that iterated through multiple resolution algorithms during development, this would have been prohibitively expensive.

### Graph-Only Model

A graph database (e.g., Neo4j) naturally represents people as nodes and relationships as edges, making complex social-network queries elegant.

**Rejected because:** Basic operations LifeOS performs constantly — list all contacts sorted by recency, search by name, count interactions — are more complex in graph queries than in relational SQL. A graph model is powerful for relationship analysis but adds query complexity for the primary use case (CRM-style contact management). The two-tier relational model is simpler, with graph-like queries possible through joins when needed.

### Per-Source Tables

One table per source (Gmail contacts, iMessage contacts, etc.) preserves source-specific schemas and avoids the abstraction overhead of a unified SourceEntity.

**Rejected because:** It duplicates schema definitions across tables and makes cross-source queries require complex unions. Entity resolution becomes table-specific rather than operating on a unified data model. Each new data source requires a new table, new ingestion code, and new resolution logic — a maintenance burden that scales linearly with source count. The unified SourceEntity model absorbs new sources with minimal schema changes.

## Consequences

### Positive

- Lossless data preservation — original observations are never modified.
- Entity resolution can be improved and re-run without re-ingesting source data.
- Clean separation between "what we observed" and "what we believe to be true."
- Supports merge/split corrections when resolution makes mistakes.
- Provenance for every piece of information about a person.

### Negative

- More complex queries — displaying a person's full profile requires joining across SourceEntity and PersonEntity tables.
- Entity resolution is a non-trivial subsystem that requires ongoing maintenance.
- Storage overhead from keeping both tiers (acceptable at current scale).
- Resolution quality directly affects user experience. Poor resolution (too many false merges or missed merges) creates confusion in the CRM. Ongoing heuristic refinement is necessary as new data sources are added.
- The two-tier model adds query complexity that could become a performance concern at scale. If PersonEntity profiles require joining across thousands of SourceEntities, query optimization or materialized views may be needed.
- New data sources may not fit cleanly into the SourceEntity schema, requiring schema extensions. The generic model trades source-specific richness for uniformity.

## Related Documents

### Design Context
- [ADR-004: Hybrid Search](004-hybrid-search.md) — How both tiers are indexed for search

### Specifications
- [Data Model](../specs/product/data-model.md) — Consumer-facing semantics of SourceEntity and PersonEntity
- [Entity Resolution](../specs/product/entity-resolution.md) — How people are matched and merged across sources
- [Data and Sync](../specs/technical/data-and-sync.md) — Sync pipeline that creates SourceEntities and triggers resolution

### Operational
- [Root AGENTS.md](../../AGENTS.md) — CRM API commands for searching and managing people

### Code References
- [`api/services/entity_resolver.py`](../../api/services/entity_resolver.py) — Resolution heuristics, scoring, merge logic
- [`api/services/people_aggregator.py`](../../api/services/people_aggregator.py) — SourceEntity → PersonEntity aggregation
- [`config/people_dictionary.example.json`](../../config/people_dictionary.example.json) — Template for the local-only `people_dictionary.json` used during resolution
