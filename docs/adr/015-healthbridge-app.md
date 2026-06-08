# ADR-015: HealthBridge App as the Recommended Apple Health Collector

**Status:** Complete
**Last Updated:** 2026-06-08
**Decision:** Accepted
**Amends:** ADR-014

## Context

[ADR-014](014-apple-health-collection.md) decided Apple Health is collected on
iOS (not a Mac, which has no HealthKit store) via an **iOS Shortcut** that emits
`health.json` into a synced folder. That works, but a hand-built Shortcut has
real limits: HealthKit "Find Samples" actions are slow and fiddly to assemble,
incremental selection is awkward, sleep-stage aggregation is hard, and the
file-sync hop adds latency and a moving part.

A small native app can do all of this better — anchored incremental queries,
proper sleep aggregation, and direct delivery — while keeping ADR-014's core
decision (collection happens on iOS) intact.

## Decision

Make a native iOS app, **HealthBridge** (`apple/HealthBridge/`), the
**recommended** Apple Health collector. It:

- reads HealthKit with **`HKAnchoredObjectQuery`** + persisted anchors, so each
  sync emits only new samples;
- **POSTs** the payload to the authenticated `POST /api/fitness/health/ingest`
  endpoint over Tailscale (the primary delivery mode), bearer-gated by
  `LIFEOS_HEALTH_INGEST_TOKEN`;
- aggregates sleep stages into nightly `sleep_hours`;
- runs in the background via HealthKit background delivery.

It emits the **same `health.json` schema** as the Shortcut, consumed unchanged by
`api/services/health_import.py`. The **iOS Shortcut remains a documented fallback**
(file mode), and the server-side import is unchanged. The app is authored as
source (XcodeGen `project.yml` + Swift) and built/signed/deployed by the user on
a Mac + iPhone with a free Apple ID.

This **amends** ADR-014 (it refines the recommended iOS producer); it does not
reverse it — collection is still iOS-only and the import is identical.

## Rationale

Anchored queries are the native primitive for "only what's new," which the
Shortcut approximates poorly. A direct POST removes the file-sync hop and makes
data land immediately. Sleep aggregation and unit handling are trivial in Swift
and painful in Shortcuts. Keeping the schema and import unchanged means the app
is a drop-in producer — no server rework, and the Shortcut still works for anyone
who doesn't want to build an app.

## Alternatives Considered

### Keep the iOS Shortcut as the only collector

**Rejected because:** brittle to build/maintain, weak incremental support, and no
clean sleep aggregation. Retained as a fallback, not the recommendation.

### File delivery from the app (instead of POST)

Have the app write `health.json` into the synced folder like the Shortcut.

**Rejected as primary because:** it reintroduces the file-sync latency and a
moving part the app can avoid. Kept available as the secondary mode.

### Paid Apple Developer account requirement

**Rejected because:** a free Apple ID is sufficient (≈7-day re-sign window). Paid
membership is an optional upgrade for permanent background delivery, not a
prerequisite.

## Consequences

### Positive

- Robust incremental sync, immediate delivery, proper sleep/units — all native.
- No server changes beyond the ingest endpoint (already shipped); the Shortcut
  path keeps working.

### Negative

- The app must be built and signed by the user on a Mac+iPhone (can't be done
  from the server), and a free Apple ID needs re-deploy ~weekly.
- A second collector to document, though the Shortcut is now just a fallback.

## Related Documents

### Design Context
- [ADR-014: Apple Health collection & import](014-apple-health-collection.md) — The decision this amends (iOS collection; the import pipeline)
- [ADR-013: Self-data fitness store](013-fitness-store.md) — Where the data lands

### Operational
- [apple/HealthBridge/README.md](../../apple/HealthBridge/README.md) — Build/sign/run the app
- [guides/apple-health.md](../guides/apple-health.md) — Collector options + schema

### Code References
- [`api/routes/fitness.py`](../../api/routes/fitness.py) — The ingest endpoint the app POSTs to
- [`api/services/health_import.py`](../../api/services/health_import.py) — The shared import core
