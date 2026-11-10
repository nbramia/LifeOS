# HealthBridge

An iOS app that reads Apple HealthKit (workouts + recovery metrics) and delivers
them to LifeOS, where the `/api/fitness/health/ingest` endpoint imports them into
`fitness.db`. This is the recommended collector for Apple Health data (it
supersedes the hand-built Shortcut from #323).

- **Authored as source** (XcodeGen `project.yml` + Swift), built and installed from
  the MacBook.
- **Read-only** HealthKit access; never writes health data.
- **Incremental:** persisted `HKQueryAnchor`s mean each sync sends only new
  samples. Combined with the server's idempotency (workout `uuid`, metric
  `(type, start)`), overlap never duplicates.

## Prerequisites (on a Mac)

- **Xcode** (App Store).
- **XcodeGen**: `brew install xcodegen`.
- The **iPhone**, paired with the Mac (HealthKit data lives on the phone; the
  Simulator has none).
- LifeOS reachable from the phone over **Tailscale**, with
  `LIFEOS_HEALTH_INGEST_TOKEN` set on the server (`openssl rand -hex 32`).

## Build & run

The project signs with Nathan's paid team (`872CQP9C37`, bundle id
`com.nbramia.healthbridge`), so installs do not expire. The ingest URL and token are
baked in at build time from a gitignored `Secrets.xcconfig`:

```bash
cd apple/HealthBridge
cp Secrets.xcconfig.example Secrets.xcconfig   # then set HB_INGEST_TOKEN to LIFEOS_HEALTH_INGEST_TOKEN
xcodegen generate                              # produces HealthBridge.xcodeproj (+ Info.plist, entitlements)
xcodebuild -project HealthBridge.xcodeproj -scheme HealthBridge -destination 'id=<iPhone UDID>' \
  -allowProvisioningUpdates build
xcrun devicectl device install app --device <iPhone UDID> \
  ~/Library/Developer/Xcode/DerivedData/HealthBridge-*/Build/Products/Debug-iphoneos/HealthBridge.app
```

Codesigning fails over SSH (the login keychain is locked there); run the build in the
Mac's GUI session, e.g. `osascript -e 'tell application "Terminal" to do script "..."'`.

On the phone, the only steps are: open **HealthBridge**, and allow every type in the
Health permission sheet. The app then syncs at once and keeps syncing through HealthKit
background delivery, which it registers at every launch.

## Configure & sync

The URL and token come preset from the build; they stay editable in the app (an edit
overrides the preset). **Sync now** runs a sync by hand, and the status line shows the
server's import counts.

Verify on the server:
```bash
curl http://localhost:8000/api/fitness/sessions   # or ask the fitness bot
```

## What it collects

- **Workouts** (`HKWorkout`): type, start/end, duration, distance, energy, avg HR.
- **Metrics**: body weight, resting HR, HRV (SDNN), steps, active energy, and
  **sleep** (asleep stages aggregated into nightly `sleep_hours`).

The payload matches the schema in [`docs/guides/apple-health.md`](../../docs/guides/apple-health.md)
and is consumed unchanged by `api/services/health_import.py`.
