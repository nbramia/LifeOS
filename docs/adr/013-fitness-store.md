# ADR-013: Self-Data Fitness Store (outside the two-tier CRM model)

**Status:** Complete
**Last Updated:** 2026-06-08
**Decision:** Accepted

## Context

LifeOS gained a fitness capability (issues #320–#323): a Telegram bot logs
workouts from text, tracks body metrics (morning weight, resting HR), holds a
training profile, and imports Apple Health/Fitness data. This data needs a home.

The established data model is the two-tier `SourceEntity` → `PersonEntity`
structure ([ADR-003](003-two-tier-data-model.md)) — built to resolve *people*
across sources (an email, a phone, a Slack id all map to one person). Its
fields are person-contact-centric (`observed_name`, `observed_email`,
`observed_phone`, `canonical_person_id`).

Workout and health data is **not about other people** — it's the user's own
time-series: sets/reps/loads, body weight, heart rate, sleep, Apple workouts.
It's also high-volume (a year of HealthKit samples is tens of thousands of
rows) and queried very differently (date-range scans, volume aggregation,
trend-over-time) than person resolution.

## Decision

Store fitness data in a **dedicated SQLite store, `data/fitness.db`**
(`api/services/fitness_store.py`), separate from the CRM model:

- Tables: `workout_sessions`, `workout_sets`, `health_metrics`,
  `training_profile`. A `source` column (`manual` | `apple_health`) unifies
  hand-logged and imported data.
- It is **self-referential** — there is no `person_id`; everything is the user's
  own data. No entity resolution applies.
- **Raw health metrics are never embedded into ChromaDB** (volume). They're
  queried by direct SQL (date-range scans). Only human-readable workout
  *summaries* are candidates for semantic indexing (deferred; see #323).
- The orchestrator reaches it through the `manage_workouts` tool, not the
  person-centric CRM tools.

## Consequences

- **Pro:** Clean separation — the CRM model stays about people; fitness queries
  are fast and purpose-built; high-volume health data doesn't bloat the entity
  tables or the vector index.
- **Pro:** Apple Health import ([ADR-014](014-apple-health-collection.md)) writes
  into the same store with `source=apple_health`, so manual and device data sit
  side by side.
- **Con:** A second storage model to maintain. Mitigated by following the same
  SQLite idioms as the other stores (`conversation_store.py`).
- **Con:** Fitness data won't appear in person timelines or cross-source entity
  views. That's intended — it isn't about other people.

## Related Documents

- [ADR-003 — Two-Tier Data Model](003-two-tier-data-model.md)
- [ADR-014 — Apple Health collection & import](014-apple-health-collection.md)
- [guides/apple-health.md](../guides/apple-health.md)
