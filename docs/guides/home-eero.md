# Home — eero

**Status:** Complete
**Last Updated:** 2026-09-15
**Audience:** Operator

Pause and resume a household profile or device's internet access via eero —
"off now, for the next hour" issued from Telegram, a persona, a scheduled
routine, or a phone shortcut, instead of unlocking a phone and tapping
through the eero app. Recurring bedtime cutoffs stay in eero's own native
per-profile scheduling; this integration is for ad-hoc overrides.

eero has no supported public API. This client talks to the same undocumented
consumer API the eero app uses, so it can break on vendor changes — see
[Failure alerts](#failure-alerts) below for how that's surfaced instead of
failing silently.

## Login

Login needs a verification code delivered out of band, so it's a two-step,
non-interactive script — run once by the operator, works from any shell
including Claude Code's `!` prefix (no TTY):

```bash
python scripts/eero_login.py --login you@example.com
# a code arrives by SMS or email
python scripts/eero_login.py --code 123456
```

This writes the session token to the gitignored state file
`data/home/eero_session.json` (mode 0600). The token is never printed.
`LIFEOS_EERO_SESSION_TOKEN` is a fallback, used only when that state file is
absent — the normal path is the login script, which the running service also
rewrites to whenever it auto-refreshes an expiring session.

Without a token in either place, every `/api/home/eero/*` endpoint returns
`503` naming this script.

## Target configuration

Targets are hand-maintained in the gitignored `config/home/eero_targets.json`
— there's no automatic device discovery. Copy the tracked example to get
started:

```bash
cp config/home/eero_targets.example.json config/home/eero_targets.json
```

Each entry is a name (matched case-insensitively) mapped to either a
**profile** or a **device**:

```json
{
  "Kid's iPad": {
    "type": "profile",
    "url": "/2.2/networks/12345/profiles/67890",
    "default_minutes": 60
  },
  "Guest Laptop": {
    "type": "device",
    "network_id": "12345",
    "mac": "AA:BB:CC:00:00:01"
  }
}
```

Prefer **profile** targets: they're the abstraction eero's own app is built
on, so they're the more stable identifier. Find a profile's `network_id`/
`profile_id` from the eero app's network settings, or from the vendor API
directly while logged in.

`default_minutes` (1-1440) is optional. It's the auto-resume duration a
pause uses when its request omits `minutes` — an explicit `minutes` always
overrides it, and an invalid `default_minutes` (not an integer, or out of
range) skips the whole target entry with a warning, the same as any other
malformed field.

A missing config file starts the service with zero configured targets, not
an error. A malformed entry (missing/invalid field, unrecognized `type`) is
skipped with a warning naming the target — the rest of the file still loads.

### Private Wi-Fi Address (device targets only)

A **device** target's identity is its MAC address. iOS's "Private Wi-Fi
Address" (enabled by default per network since iOS 14) rotates the MAC a
device presents, so a device target configured against today's MAC silently
targets nothing once the address rotates — pause/resume calls succeed
against eero but never touch the intended device. Disable Private Wi-Fi
Address for the household's home network on any device you configure as a
**device** target: Settings → Wi-Fi → (network) → Private Wi-Fi Address →
Off. **Profile** targets aren't affected — the profile is the pause point,
not any one device's MAC.

## Pause and resume

Three surfaces reach the same service (`api/services/home/eero.py`):

- **REST** — `GET /api/home/eero/status`, `POST /api/home/eero/{name}/pause`,
  `POST /api/home/eero/{name}/resume` (Tailscale-only, like the rest of the
  API). See [API Reference](../specs/product/api-reference.md#home-endpoints-eero).
- **Agent tools** — `pause_internet`, `resume_internet`, `internet_status`,
  callable from any persona or `/chat`.
- **MCP** — `lifeos_home_eero_pause`, `lifeos_home_eero_resume`,
  `lifeos_home_eero_status`, over stdio (Claude Code) and HTTP (Managed
  Agents, Hermes).

Every operation is idempotent and state-reconciling: it sets the value, then
reads it back from the vendor and reports what's actually there — a
`mismatch: true` flag (not an error) when the read-back state doesn't match
what was requested.

A pause given `minutes` (1-1440) schedules the resume as a one-off entry in
the scheduler (`LifeOS/Scheduler/Inbox.md`), so it survives a restart of
whatever process issued the pause. Calling pause again on a target with a
pending resume replaces it — exactly one pending resume exists per target.
Pausing without `minutes` uses the target's `default_minutes` if it has one;
otherwise it makes the pause indefinite and clears any pending resume.
Sending `{"indefinite": true}` forces an indefinite pause regardless of
`default_minutes` — combining it with `minutes` is a validation error.

### Silent scheduled calls

Pause and resume both accept `scheduled: true` in their body. A call that
succeeds with no `mismatch` then returns an empty `scheduler_message`, which
the scheduler's fire loop treats as nothing to send — so a scheduled pause
or resume that behaves as expected posts no Telegram line. A mismatch, or
any failure, still reports. The one-off auto-resume a timed pause creates
already posts `{"scheduled": true}` to `/resume`, so its ordinary success is
silent too.

Set `scheduled: true` yourself to build a **recurring** scheduled pause — a
plain cron `endpoint` schedule, no dedicated feature needed:

```bash
curl -X POST http://localhost:8000/api/scheduler \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Bedtime cutoff: Kid'\''s iPad",
    "schedule_type": "cron",
    "schedule_value": "0 21 * * *",
    "action": "endpoint",
    "endpoint_config": {
      "endpoint": "/api/home/eero/Kid'\''s iPad/pause",
      "method": "POST",
      "params": {"minutes": 480, "scheduled": true}
    }
  }'
```

This pauses "Kid's iPad" every night at 9pm for 8 hours, auto-resuming via
the same scheduler-backed mechanism as a manual timed pause, and — as long
as the vendor agrees with what was requested — silently.

## Failure alerts

Every failure mode surfaces loudly — Telegram plus a
[human-queue](human-queue.md) card, never a silent no-op:

| Failure | Human-queue key | When |
|---|---|---|
| Session dead (token rejected, refresh failed) | `eero-session` | Any request |
| Vendor rejected a write / unrecognized response shape | `eero-api-error` | Any request |
| Scheduled resume failed after 3 retries | `eero-resume-failed:<name>` | Only a scheduler-fired resume |

Repeated failures of the same kind update the existing card instead of
piling up duplicates (human-queue's key-based dedupe). A scheduled resume
that fails is the worst case — the device stays offline until someone acts —
which is why it gets its own retry-with-backoff and a per-target alert
naming the target explicitly still paused.

## Related Documents

### Specifications
- [API Reference — Home Endpoints (Eero)](../specs/product/api-reference.md#home-endpoints-eero) — REST contracts
- [MCP Tools](../specs/product/mcp-tools.md) — The `lifeos_home_eero_*` tools in the full MCP catalog

### Guides
- [Human Queue](human-queue.md) — The card mechanism failure alerts file into
- [Scheduler](scheduler.md) — The one-off `endpoint` action a timed pause's resume rides on
- [Configuration](configuration.md) — `LIFEOS_EERO_SESSION_TOKEN`

### Code References
- [`api/services/home/eero.py`](../../api/services/home/eero.py) — Vendor client, session/token persistence, target resolution, pause/resume/status
- [`api/routes/home.py`](../../api/routes/home.py) — REST routes
- [`scripts/eero_login.py`](../../scripts/eero_login.py) — Two-step login script
- [`config/home/eero_targets.example.json`](../../config/home/eero_targets.example.json) — Tracked target-map template
