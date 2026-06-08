# Apple Health Import → LifeOS

> **Audience:** Operators setting up Apple Health/Fitness ingestion
> **Status:** Complete
> **Last Updated:** 2026-06-08

LifeOS imports Apple Health/Fitness data (workouts, body weight, resting heart
rate, HRV, sleep, energy) into the fitness store so the fitness bot can
cross-reference training with recovery.

**Why on iOS:** macOS has no Health app and a Mac does not hold the iPhone/Watch
HealthKit store, so the data can only be read **on iOS** ([ADR-014](../adr/014-apple-health-collection.md)).
The data lands in the **self-data fitness store** (`data/fitness.db`), not the
person-centric CRM model ([ADR-013](../adr/013-fitness-store.md)).

## Collectors

Two ways to get the data off the phone, both emitting the same `health.json`
schema (below):

| Collector | Delivery | When |
|-----------|----------|------|
| **HealthBridge app (recommended)** | **POST** to `/api/fitness/health/ingest` over Tailscale | Best — incremental anchored sync + background delivery ([ADR-015](../adr/015-healthbridge-app.md)). |
| iOS Shortcut (fallback) | **file** into the synced path, imported nightly | No Xcode needed; manual to build, less robust. |
| Health-app XML export | file, one-shot | History backfill only. |

### HealthBridge app (recommended)

Build and deploy the app per **[`apple/HealthBridge/README.md`](../../apple/HealthBridge/README.md)**
(Xcode + a free Apple ID). In the app, set the ingest URL and token and tap
**Sync now**; background delivery handles the rest.

Server setup for POST mode — set a dedicated bearer token in `.env`:
```
LIFEOS_HEALTH_INGEST_TOKEN=<openssl rand -hex 32>
```
Empty disables the endpoint (it returns 503). The app POSTs to
`https://<your-machine>.<tailnet>.ts.net/api/fitness/health/ingest`; rows land in
`fitness.db` immediately, queryable via the fitness bot.

---

## Fallback: iOS Shortcut (file mode)

If you'd rather not build the app, an iOS Shortcut can write `health.json` into a
synced folder that the nightly importer reads.

### 1. Where the file goes

The importer reads `LIFEOS_HEALTH_EXPORT_PATH` (default
`data/apple-imports/health.json`). The simplest automated path is the Syncthing
share that's already mirrored to every machine:

1. Pick a synced location, e.g. `~/Code/Sync/health/health.json`.
2. Set it in `.env` on the server (use your own home path):
   ```
   LIFEOS_HEALTH_EXPORT_PATH=/home/<your-username>/Code/Sync/health/health.json
   ```
   (No "mover" step needed — the Shortcut writes into the synced folder on the
   phone via iCloud Drive/Working Copy, or you point the path at wherever your
   sync tool lands the file.)

---

## 2. The `health.json` schema

The Shortcut must emit this shape (extra keys are ignored):

```json
{
  "generated_at": "2026-06-08T06:00:00-04:00",
  "watermark": "2026-06-07T00:00:00-04:00",
  "workouts": [
    {
      "uuid": "F1A2…",                 // HKWorkout UUID — REQUIRED, dedupe key
      "type": "Running",               // workout type (friendly or HK identifier)
      "start": "2026-06-07T08:00:00-04:00",
      "end": "2026-06-07T08:55:00-04:00",
      "duration_s": 3300,
      "distance_m": 10000,             // optional
      "energy_kcal": 620,              // optional
      "avg_hr": 145                    // optional
    }
  ],
  "metrics": [
    { "type": "body_weight", "value": 178.4, "unit": "lb",
      "start": "2026-06-07T07:00:00-04:00" },
    { "type": "resting_hr", "value": 54, "unit": "bpm",
      "start": "2026-06-07T07:00:00-04:00" },
    { "type": "sleep_hours", "value": 7.2, "unit": "h",
      "start": "2026-06-07T00:00:00-04:00", "end": "2026-06-07T07:00:00-04:00" }
  ]
}
```

Notes:
- **`uuid` is required** on workouts — it's the idempotency key, so re-running
  the Shortcut never duplicates a session. Metrics dedupe on `(type, start)`.
- Timestamps may carry any offset or a trailing `Z`; the importer normalizes to
  UTC.
- Suggested `metrics.type` values the fitness bot recognizes as recovery
  signals: `body_weight`, `resting_hr`, `hrv`, `sleep_hours`. Any other type is
  imported and queryable too.

---

## 3. Building the iOS Shortcut (on the iPhone)

> Built once on the phone. This can't be done from the Linux/Mac side.

1. **Shortcuts app → +** (new shortcut). Name it "LifeOS Health Export".
2. For each data type, add a **Find Health Samples** action, filter
   **Start Date is in the last 1 day** (or since your watermark), then build a
   dictionary entry per sample:
   - **Workouts:** Find Workouts → for each, read UUID, type, start/end,
     duration, distance, active energy, average heart rate.
   - **Body weight / resting HR / HRV / sleep / energy:** Find Health Samples
     for each quantity type → value, unit, start (+ end for sleep).
3. Assemble a **Dictionary** matching the schema above (`workouts` and `metrics`
   arrays), then **Get Text from Dictionary** (JSON).
4. **Save File** → overwrite to your synced location (iCloud Drive folder that
   maps to `~/Code/Sync/health/health.json`, or use the Working Copy/Syncthing
   app's save target).
5. **Automation:** Shortcuts → Automation → **Time of Day → 6:00 AM, daily** →
   run "LifeOS Health Export". (Optionally also run it manually after a workout.)

> Tip: HealthKit "Find Samples" actions can be slow over long ranges. Keep the
> daily window small (last 1–2 days); the importer's idempotency means overlap
> is harmless. For a one-time history backfill, run a wider window once.

---

## 4. Import & verify (server)

The import runs automatically as part of the nightly **`apple_import`** sync
step (it's one of the Apple import sources). To run it on demand:

```bash
~/.venvs/lifeos/bin/python scripts/apple_data_import.py --execute --source health
```

Expected output includes `workouts +N` / `metrics +N`. Then query via the
fitness bot ("what's my body weight trend", "readiness") or directly — health
metrics and Apple workouts live in `data/fitness.db` alongside your manual log.

---

## Related Documents

- [ADR-013 — Self-data fitness store](../adr/013-fitness-store.md)
- [ADR-014 — Apple Health collection & import](../adr/014-apple-health-collection.md)
- [ADR-015 — HealthBridge app (recommended collector)](../adr/015-healthbridge-app.md)
- [apple/HealthBridge/README.md](../../apple/HealthBridge/README.md) — Build/sign/run the app
- [ADR-010 — Apple Data Agent](../adr/010-apple-data-agent.md)
- [guides/scheduler.md](scheduler.md) — proactive check-ins (e.g. morning weight)
