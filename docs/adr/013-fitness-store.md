# ADR-013: Self-Data Fitness Store (outside the two-tier CRM model)

**Status:** Complete
**Last Updated:** 2026-06-08
**Decision:** Accepted

## Context

LifeOS gained a fitness capability (issues #320–#323): a Telegram bot logs
workouts from text, tracks body metrics (morning weight, resting HR), holds a
training profile, and imports Apple Health/Fitness data. This data needs a home.

The established data model is the two-tier `SourceEntity` → `PersonEntity`
structure ([ADR-003](003-two-tier-data-model.md)), built to resolve *people*
across sources. Its fields are person-contact-centric (`observed_name`,
`observed_email`, `observed_phone`, `canonical_person_id`).

Workout and health data is **not about other people** — it is the user's own
time-series: sets/reps/loads, body weight, heart rate, sleep, Apple workouts.
It is also high-volume (a year of HealthKit samples is tens of thousands of
rows) and queried differently (date-range scans, volume aggregation,
trend-over-time) than person resolution.

## Decision

Store fitness data in a **dedicated SQLite store, `data/fitness.db`**
(`api/services/fitness_store.py`), separate from the CRM model. Tables:
`workout_sessions`, `workout_sets`, `health_metrics`, `training_profile`, with a
`source` column (`manual` | `apple_health`) unifying hand-logged and imported
data. The store is self-referential — no `person_id`, no entity resolution.
Raw health metrics are queried by direct SQL and are **never embedded into
ChromaDB**; only human-readable workout *summaries* are candidates for semantic
indexing. The orchestrator reaches it through the `manage_workouts` tool.

## Rationale

The CRM model exists to answer "who is this and how do they connect to other
people." Fitness data has no such question — it is single-subject time-series.
Forcing it into `SourceEntity` would mean abusing person-contact fields for
metric data, bloating the entity tables and the vector index with tens of
thousands of low-value rows, and inheriting entity-resolution machinery that
does nothing useful here. A purpose-built store keeps queries fast
(date-range/aggregation) and keeps the CRM model clean.

## Alternatives Considered

### Extend `SourceEntity` with health fields

Add `source_type="health_*"` rows and stuff metrics into the generic `metadata`
JSON, pointing `canonical_person_id` at a "self" person.

**Rejected because:** the dataclass is contact-centric (name/email/phone), the
`metadata` blob is unindexed and unvalidated for metric queries, and a year of
samples would bloat the shared entity tables. Entity resolution would run over
data that has no person to resolve.

### Index all health data into ChromaDB

Treat each metric/workout as a document for semantic search.

**Rejected because:** raw metrics are high-volume and numeric — semantic search
over "step count = 8123" is useless and expensive. Direct SQL is the right
query path; only workout summaries benefit from embedding.

## Consequences

### Positive

- Clean separation — the CRM model stays about people; fitness queries are fast
  and purpose-built; high-volume data doesn't bloat the entity tables or vector
  index.
- Apple Health import ([ADR-014](014-apple-health-collection.md)) writes into the
  same store with `source=apple_health`, so manual and device data sit together.
- Follows the existing SQLite idioms (`conversation_store.py`), so it's familiar.

### Negative

- A second storage model to maintain alongside the CRM stores.
- Fitness data won't appear in person timelines or cross-source entity views —
  intended, since it isn't about other people, but a boundary to remember.

## Related Documents

### Design Context
- [ADR-003: Two-Tier Data Model](003-two-tier-data-model.md) — The person-centric model this store deliberately sits outside of
- [ADR-014: Apple Health collection & import](014-apple-health-collection.md) — Writes into this store with `source=apple_health`

### Operational
- [guides/apple-health.md](../guides/apple-health.md) — How Apple data lands in this store

### Code References
- [`api/services/fitness_store.py`](../../api/services/fitness_store.py) — The store
- [`api/services/agent_tools.py`](../../api/services/agent_tools.py) — `manage_workouts` tool over it
