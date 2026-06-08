# ADR-014: Apple Health Collection via iOS Shortcut

**Status:** Complete
**Last Updated:** 2026-06-08
**Decision:** Accepted

## Context

LifeOS imports Apple Health/Fitness data into the fitness store
([ADR-013](013-fitness-store.md)). The open question (issue #323) was **how to
get the data off Apple devices.**

The existing Apple Data Agent ([ADR-010](010-apple-data-agent.md)) runs on the
Mac Mini and reads protected macOS databases (Messages, Contacts, CallHistory,
Photos) under a Full Disk Access grant. The initial assumption was that Health
would slot in the same way — a Swift HealthKit CLI on the Mini.

That assumption is wrong: **macOS has no Health app, and a Mac does not hold the
iPhone/Watch HealthKit store.** HealthKit data is iOS/watchOS-resident and does
not sync to Macs for third-party reads. A Mac-side reader would find an empty
store. The viable readers are all on iOS.

## Decision

Collect Apple Health data with an **iOS Shortcut** running on the iPhone:

- The Shortcut reads HealthKit (workouts + selected quantity samples) and writes
  a `health.json` (schema in [guides/apple-health.md](../guides/apple-health.md))
  into a **synced folder** (the Syncthing `~/Code/Sync/` share, already mirrored
  to the server). A daily iOS Automation runs it.
- The **server-side import is collection-agnostic**: `import_health()` in
  `scripts/apple_data_import.py` reads the JSON from
  `LIFEOS_HEALTH_EXPORT_PATH` and upserts into `data/fitness.db`. It runs as one
  of the sources in the existing nightly `apple_import` step.
- **Idempotency:** workouts dedupe on the HKWorkout `uuid`, metrics on
  `(type, start)`. Timestamps are normalized to UTC on import.
- A manual **Health-app XML export** remains a fallback for one-time history
  backfill (same import target).

The originally-considered **Swift HealthKit CLI on the Mac Mini is rejected** —
the Mini doesn't have the data.

## Consequences

- **Pro:** Works with Apple's actual data residency; no fragile attempt to read
  iPhone data from a Mac. No new macOS/Swift build dependency.
- **Pro:** The import is decoupled from collection — if Apple ever offers a
  better export, only the producer changes; the importer is unchanged.
- **Con:** The Shortcut is built and maintained on the phone (can't be authored
  from the server), and HealthKit "Find Samples" actions are slow over long
  ranges — mitigated by a small daily window plus idempotent overlap.
- **Con:** Collection depends on the iOS Automation actually firing; a missed
  day is self-healing on the next run (idempotent, windowed).

## Related Documents

- [ADR-010 — Apple Data Agent](010-apple-data-agent.md)
- [ADR-013 — Self-data fitness store](013-fitness-store.md)
- [guides/apple-health.md](../guides/apple-health.md)
