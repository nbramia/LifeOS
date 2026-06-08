# ADR-014: Apple Health Collection via iOS Shortcut

**Status:** Complete
**Last Updated:** 2026-06-08
**Decision:** Accepted
**Amended by:** ADR-015

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

Collect Apple Health data with an **iOS Shortcut** on the iPhone. The Shortcut
reads HealthKit and writes a `health.json` (schema in
[guides/apple-health.md](../guides/apple-health.md)) into a synced folder (the
Syncthing `~/Code/Sync/` share, already mirrored to the server); a daily iOS
Automation runs it. The **server-side import is collection-agnostic**:
`import_health()` in `scripts/apple_data_import.py` reads the JSON from
`LIFEOS_HEALTH_EXPORT_PATH` and upserts into `data/fitness.db`, running as a
source in the existing nightly `apple_import` step. Idempotency: workouts dedupe
on the HKWorkout `uuid`, metrics on `(type, start)`; timestamps normalize to UTC.

## Rationale

Collection must run where the data actually lives — iOS. Decoupling collection
(the Shortcut) from import (the server) means the producer can change without
touching the importer, and the importer can be unit-tested against a fixture.
Routing through the existing Sync share avoids any new transport. Idempotent,
windowed imports make a missed automation run self-healing.

## Alternatives Considered

### Swift HealthKit CLI on the Mac Mini

Mirror the existing Apple Data Agent — a compiled Swift binary reading
`HKHealthStore` under the Mini's FDA grant.

**Rejected because:** macOS does not hold the iPhone/Watch HealthKit store, so
the CLI would read an empty store. This was the original plan and is the reason
the issue was spiked before building.

### Manual Health-app XML export only

The iPhone Health app can export a full XML archive.

**Rejected because:** it is manual and not automatable for ongoing sync. Kept as
a **fallback for one-time history backfill** (same import target), not the
primary path.

## Consequences

### Positive

- Works with Apple's actual data residency; no fragile attempt to read iPhone
  data from a Mac, and no new macOS/Swift build dependency.
- Import is decoupled from collection — a better future export only changes the
  producer; the importer is unchanged and independently testable.

### Negative

- The Shortcut is built and maintained on the phone (can't be authored from the
  server), and HealthKit "Find Samples" actions are slow over long ranges —
  mitigated by a small daily window plus idempotent overlap.
- Collection depends on the iOS Automation firing; a missed day self-heals on
  the next windowed run.

## Related Documents

### Design Context
- [ADR-010: Apple Data Agent](010-apple-data-agent.md) — The Mac-side pipeline this parallels; explains the FDA grant the rejected Swift-CLI alternative would have used
- [ADR-013: Self-data fitness store](013-fitness-store.md) — Where imported data lands
- [ADR-015: HealthBridge app](015-healthbridge-app.md) — Amends this: the app is now the recommended collector, the Shortcut a fallback

### Operational
- [guides/apple-health.md](../guides/apple-health.md) — iOS Shortcut build steps + `health.json` schema

### Code References
- [`scripts/apple_data_import.py`](../../scripts/apple_data_import.py) — `import_health()`
- [`tests/test_apple_health_import.py`](../../tests/test_apple_health_import.py) — Import coverage
