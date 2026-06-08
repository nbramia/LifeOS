# HealthBridge

An iOS app that reads Apple HealthKit (workouts + recovery metrics) and delivers
them to LifeOS, where the `/api/fitness/health/ingest` endpoint imports them into
`fitness.db`. This is the recommended collector for Apple Health data (it
supersedes the hand-built Shortcut from #323).

- **Authored as source** (XcodeGen `project.yml` + Swift). You generate the Xcode
  project, sign it with your Apple ID, and deploy to your iPhone.
- **Read-only** HealthKit access; never writes health data.
- **Incremental:** persisted `HKQueryAnchor`s mean each sync sends only new
  samples. Combined with the server's idempotency (workout `uuid`, metric
  `(type, start)`), overlap never duplicates.

> This is a working scaffold. HealthKit APIs vary slightly by SDK; open it in
> Xcode and resolve any version-specific compile notes before first run.

## Prerequisites (on a Mac)

- **Xcode** (App Store).
- **XcodeGen**: `brew install xcodegen`.
- An **iPhone** (HealthKit data lives on the phone; the Simulator has none) and an
  Apple ID for signing.
- LifeOS reachable from the phone over **Tailscale**, with
  `LIFEOS_HEALTH_INGEST_TOKEN` set on the server (`openssl rand -hex 32`).

## Build & run

```bash
cd apple/HealthBridge
xcodegen generate          # produces HealthBridge.xcodeproj (+ Info.plist, entitlements)
open HealthBridge.xcodeproj
```

In Xcode:
1. Select the **HealthBridge** target → **Signing & Capabilities**.
2. Set **Team** to your Apple ID (add it under Xcode → Settings → Accounts if
   needed). For a **free Apple ID**, also set a unique bundle id (e.g.
   `tech.lifeos.healthbridge.<yourname>`) — the prefilled one may be taken.
3. Confirm the **HealthKit** capability is present (with **Background Delivery**).
   XcodeGen adds it from `project.yml`; if Xcode prompts, accept.
4. Plug in / wirelessly pair your iPhone, select it as the run destination, and
   **Run** (⌘R). Trust the developer cert on the phone:
   *Settings → General → VPN & Device Management → (your Apple ID) → Trust.*
5. On first launch, **grant the Health permission prompt** (all requested types).

## Configure & sync

In the app:
- **Ingest URL** — your LifeOS ingest endpoint, e.g.
  `https://<your-machine>.tailXXXX.ts.net/api/fitness/health/ingest`.
- **Ingest token** — the value of `LIFEOS_HEALTH_INGEST_TOKEN`.
- Tap **Sync now**. The status line shows the server's import counts.

Background delivery is enabled automatically (hourly observer queries), so new
samples sync without launching the app — within the signing window (below).

Verify on the server:
```bash
curl http://localhost:8000/api/fitness/sessions   # or ask the fitness bot
```

## Signing reality (free Apple ID)

- Apps signed with a **free** Apple ID expire after **~7 days** and stop running
  in the background until re-deployed from Xcode (just **Run** again to refresh).
- A **paid Apple Developer** account ($99/yr) removes the expiry and makes
  background delivery reliable — optional upgrade.

## What it collects

- **Workouts** (`HKWorkout`): type, start/end, duration, distance, energy, avg HR.
- **Metrics**: body weight, resting HR, HRV (SDNN), steps, active energy, and
  **sleep** (asleep stages aggregated into nightly `sleep_hours`).

The payload matches the schema in [`docs/guides/apple-health.md`](../../docs/guides/apple-health.md)
and is consumed unchanged by `api/services/health_import.py`.
