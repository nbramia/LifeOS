# Operations Reference

> **Status:** Complete
> **Owner:** Operations
> **Last Updated:** 2026-08-27
> **Audience:** Operators

Operational procedures that don't belong in the day-to-day coding reference: the Apple Data Agent, Monarch Money auth, and quick observability commands. Moved here from `AGENTS.md` to keep the agent-facing file lean.

---

## Apple Data Agent (optional, macOS only)

If you have a Mac with iMessage/phone data, it can export Apple ecosystem data and sync it to the Linux server nightly via `scripts/apple_data_agent.sh`.

### Before your first import: check Messages retention

Messages has its own history-retention setting (Messages → Settings → General → "Keep Messages"), and it silently prunes the same local database this system reads from. If it isn't set to keep history forever, older messages are already gone from the source by the time an import ever runs — no amount of re-syncing recovers them. Check this setting on your own device before the first import, and decide whether to change it: a shorter-than-expected import is very likely this setting, not a limitation of the export pipeline or of iMessage itself.

### macOS FDA (Full Disk Access)

`/Applications/LifeOS.app` is a bash-script-based .app bundle with **Full Disk Access**. macOS cron cannot access `~/Library/Messages/` without FDA, so the Apple Data Agent cron job routes through this wrapper. The grant must be added to `/Applications/LifeOS.app` itself in System Settings → Privacy & Security → Full Disk Access — never to `/bin/bash`, `/bin/zsh`, or the Python interpreter it eventually invokes. TCC grants are per-bundle, and neither a bare shell nor a venv's `python3` has a stable enough identity to hold a persistent grant on (recreating the venv would silently break the export).

**Use `LifeOS run`, not `LifeOS exec`, for anything that touches protected data.** macOS TCC attributes FDA to the *responsible* (parent) process, not to whichever binary happens to be running. `LifeOS.app`'s `run` subcommand runs the command as a real subprocess with the wrapper script staying alive as its parent, so the child inherits FDA. Its `exec` subcommand replaces the wrapper's own process image via shell `exec`, which breaks the parent chain and silently loses FDA — the child just reads empty or protected-looking data, with no error. `scripts/apple_data_agent.sh` (the working export path) has always used `run`; if adding new cron jobs or scripts on macOS that need to access protected directories, route them through `LifeOS run` the same way. See [ADR-022](../adr/022-macos-fda-inheritance-and-restart.md) for the full mechanism and why [ADR-010](../adr/010-apple-data-agent.md) originally described this backward.

**Restarting the API service (or anything routed through LifeOS.app) on macOS:** never force-restart with `launchctl kickstart` — it has wedged the service rather than cleanly restarting it. Use a clean stop-then-start instead: `./scripts/service.sh stop` then `./scripts/service.sh start` (or, for a harder reset, `./scripts/service.sh uninstall` then `./scripts/service.sh install` to fully tear down and rebootstrap the launchd job). If that bootstrap/load step fails with a bare `Input/output error`, that's a known-transient launchd/TCC hiccup — retry the identical command before assuming anything is actually misconfigured.

### Self-update (issue #509)

The Apple Data Agent Mac runs `apple_data_agent.sh` from its own separate checkout, which nothing else ever pulls — a fix to the export pipeline on `main` would otherwise silently never reach it. Step 0 of the agent script self-updates that checkout before exporting, using the same guards as `auto-deploy.sh`: only on the `main` branch, only with a clean working tree, and only via `git pull --ff-only` (never force, never reset). If any guard trips or the pull fails, the agent logs a warning (and, on an actual pull failure, sends a Telegram alert) and **continues with the existing checkout** — a slightly stale export is better than a skipped one, as long as the staleness is visible.

The before/after SHA is logged in the agent's own log, and the export's `manifest.json` records the SHA it ran from as `agent_sha`. On the Linux side, `apple_data_import.py`'s `check_manifest()` compares `agent_sha` against the host's own `main` SHA and logs a warning on mismatch — so a stuck self-update is diagnosable from the import log without SSHing to the Apple Data Agent Mac. Manifests written before this change simply lack `agent_sha`, which is treated as "nothing to compare" rather than a warning.

## Monarch Money (Financial Data)

Auth uses a cached session token at `data/monarch_session.pickle`. Monthly sync runs on the 1st via `run_all_syncs.py` (phase 5). Live queries at `/api/monarch/*`.

Re-authenticate when the token expires (401/525), or when the nightly sync warns
that the session is old. Run from the project root — the session path is relative.

**Preferred (works in any shell, including agent/non-TTY sessions):** reads
`MONARCH_EMAIL` / `MONARCH_PASSWORD` from `.env` and takes the MFA code as an
argument. TOTP codes expire in ~30s, so read the code and run promptly.

```bash
~/.venvs/lifeos/bin/python scripts/monarch_reauth.py <6-digit-code>
```

It verifies the new session with a live authenticated call before reporting
success — a saved pickle alone does not prove the session works. On success it
prints how many accounts are reachable.

**Interactive alternative (requires a real TTY):** prompts for email, password,
and MFA code. This fails with `EOFError: EOF when reading a line` in any
non-interactive shell, including Claude Code's `!` prefix.

```bash
~/.venvs/lifeos/bin/python -c "
import asyncio
from monarchmoney import MonarchMoney
mm = MonarchMoney()
asyncio.run(mm.interactive_login())
mm.save_session('data/monarch_session.pickle')
print('Session saved!')
"
```

The running API server picks up the refreshed session without a restart; verify
with `curl -s localhost:8000/api/monarch/accounts | head -c 200`.

## Performance Tracing — Quick Commands

Every chat request is traced with per-stage timing (SQLite, `data/perf_traces.db`). Full design in [Observability](../specs/technical/observability.md).

```bash
curl http://localhost:8000/api/perf/stats | jq                    # Aggregate stats
curl "http://localhost:8000/api/perf/traces?limit=10" | jq        # Recent traces
curl http://localhost:8000/api/perf/traces/{trace_id} | jq        # Single trace
```

## GPU Watchdog

`scripts/gpu-watchdog.sh` runs every 5 minutes (`lifeos-gpu-watchdog.timer`, Linux only) and watches for two independent GPU failure signals on the gfx1151 iGPU:

- **VRAM saturation** — reads usage from AMDGPU sysfs and alerts above `LIFEOS_VRAM_ALERT_PCT` (default 80%). Guards against the 2026-05-28 incident where VRAM exhaustion during model load locked up the GPU.
- **SDMA-queue exhaustion (#521)** — the iGPU has only 8 SDMA queues. Concurrent GPU embedders (e.g. the API server and a manual reindex both loading/encoding on GPU at once) can exhaust them with VRAM still healthy — VRAM% alone can't see this. Each tick scans the kernel log (`journalctl -k --since <last tick>`) for `No more SDMA queue to allocate`, the signature that preceded the 2026-07-10 host freeze, and alerts on it with its own cooldown (`LIFEOS_SDMA_ALERT_COOLDOWN_MIN`, defaults to the VRAM cooldown) so it can't spam independently of the VRAM alert.

Both signals alert via Telegram and log to `logs/gpu-watchdog.log`. See `api/services/embeddings.py`'s cross-process `flock` (settings `embedding_gpu_lock_*`) for the mitigation that serializes GPU embedding across processes to prevent SDMA exhaustion in the first place.

## Alerting Severities

| Severity | When Sent | Examples |
|----------|-----------|----------|
| **CRITICAL** | Immediately (rate-limited) | ChromaDB down, embedding model failed, vault inaccessible |
| **WARNING** | Batched nightly (7 AM ET) | LLM API errors, backup failed, >5 degradation events |
| **INFO** | Log only | Telegram retry, config defaults used |

Set `LIFEOS_ALERT_EMAIL` in `.env` for alerts. Telegram backup via `telegram_bot_token` + `telegram_chat_id`.

Services are tracked on-use, not by polling. Degradation events (fallback usage) are collected and reported in the nightly health check if there are 5+ in 24 hours.

---

## Auto-Deploy (self-hosted redeploy on push to main)

When you push to `main` from another machine, the self-hosted host can pull and redeploy itself instead of you doing it by hand. This is the `lifeos-autodeploy.timer` — a polling systemd timer, off by default.

**How it works:** every 10 minutes `scripts/auto-deploy.sh` runs `git fetch`; if `origin/main` advanced, it fast-forward pulls and restarts the currently-active code services (`lifeos-api`, and if running, `lifeos-agent-worker` / `lifeos-mcp-http`). Docs/tests/frontend-only changes pull without a restart. If `requirements.txt` changed it `pip install`s first. It runs as the repo owner (git pull uses the user's SSH key; restarts use the passwordless sudoers rule).

Pull-based on purpose: it needs no inbound network, so it works on a WiFi-only host and just catches up on the next tick after any outage. Latency is up to the poll interval (~10 min).

**Guards** (any tripped → the run changes nothing): must be on `main`, working tree must be clean (never clobbers local edits on the host), and the pull is `--ff-only` (a diverged `main` alerts instead of resetting).

**Enable it:**

```bash
# in .env
LIFEOS_AUTODEPLOY_ENABLED=true
LIFEOS_AUTODEPLOY_NOTIFY=failure   # failure (default) | always | never

sudo ./scripts/setup-systemd.sh    # installs + enables the timer
systemctl list-timers lifeos-autodeploy.timer
```

**Watch it:** `tail -f logs/auto-deploy.log`. Failures (fetch, diverged pull, pip, restart, or `/health` not recovering post-deploy) send a Telegram alert.

**Caveat:** it deploys whatever lands on `main` *without* re-running the test suite — safe only because merged `main` is expected to be green. It does not gate on tests by design (running the suite would take the API down and thrash the shared server).

**Drift detection:** on every tick (pull or no pull), each active service's start time is compared against two independent signals: the newest mtime among tracked source files, and `.env`'s own mtime (a config-only edit — a rotated token, a new backend flag — needs a restart too, even with no code change). A unit whose start time predates either signal is restarted. Per-unit, per-signal "applied" markers under `data/` remember the exact `.env` mtime value each unit was last restarted for, so a **future-dated** mtime (clock skew, `rsync -t`, a bad manual `touch`) can only trigger one restart, never an infinite loop — and a manual `sudo systemctl restart <unit>` after an operator edits `.env` directly is still correctly recognized as "already current" on the next tick, since the check requires both signals (start time *and* marker mismatch), not the marker alone.

**Sync-in-progress lock:** never restarts a service while the nightly sync is running (a mid-sync restart can OOM-freeze the host). A systemd-launched sync is detected via `lifeos-sync.service`'s own `ActiveState`; a manually-launched one via a kernel-held advisory lock (`flock`) that `scripts/run_all_syncs.py` holds (shared mode) for its entire run and `auto-deploy.sh` takes exclusively around its whole restart section — not just a point-in-time check, closing the race where a sync could start in the gap between checking and restarting. The lock path is **host-wide** (`$HOME/.lifeos/sync.lock`, fixed — not configurable via `.env`, since `run_all_syncs.py` and the deploy scripts would otherwise resolve an override differently and silently lock two different files), not tied to any one git checkout: this repo is routinely worked in multiple worktrees, each with its own `data/` directory, so a sync launched from one worktree must still be visible to a deploy running from a different one.

---

## Auto-Deploy on macOS (self-hosted redeploy)

The macOS analog of the section above, for a launchd-managed API service: `scripts/auto-update-macos.sh`, same opt-in flag (`LIFEOS_AUTODEPLOY_ENABLED`), same drift signals (code mtime, `.env` mtime with the same future-mtime-safe applied-marker logic), and the same host-wide sync lock (shared with the Linux side and with `run_all_syncs.py` — a sync launched on either platform is visible to a deploy check on either). It is **not** installed as a timer by any script in this repo; an operator who wants it to run periodically adds their own cron entry.

**Why this exists as a separate script, not a shared one:** launchd has no equivalent of systemd's `ActiveEnterTimestamp`, so drift detection reads the running API process's actual start time from the OS (`ps -o lstart=`) instead of a self-tracked marker — accurate from the very first run, with no bootstrap gap. The restart sequence itself is the one a real field deployment converged on after taking the API down twice in one night:

1. **Unload, then poll for the previous process to actually be gone** before loading the next one — `launchctl unload` returns once it has *asked* the process to exit, not once it has; starting immediately after let the new process's `bind()` collide with the old one still tearing down. The pid polled is the one captured *before* `unload` runs, not re-derived from `launchctl list` afterward (which can report empty the instant `unload` is called, well before the process is actually gone).
2. **Retry the start once** on a transient-looking `launchctl load` failure (a bare I/O-type error has been observed to be transient here).
3. **Poll `/health` with a timeout scaled to the on-disk vault size**, not a fixed short window — a larger vault takes longer to embed/reindex on boot.
4. **One more full restart cycle** if `/health` still isn't up, then alert a human (Telegram) and stop — never restart indefinitely, never silently stay down.

Never uses `launchctl kickstart` — see the kickstart-avoidance guidance under Apple Data Agent above; the same wedging risk applies here.

---

## Shared Credentials Across Tools (#658)

LifeOS sometimes shares a paid provider account with another tool running on the
same box (e.g. `LIFEOS_REMOTE_LLM_API_KEY` in `config/settings.py` — a vendor-neutral
credential for whichever OpenAI-compatible remote provider is configured, #654).
When another local tool authenticates to that *same* provider account, the two
tools end up with two independent copies of one secret: two rotation targets,
and two places that silently drift when only one gets updated.

**The fix is never a config-sharing mechanism between the two codebases** —
LifeOS reading the other tool's config file (or vice versa) creates exactly the
kind of cross-repo coupling and stale-snapshot risk this section exists to avoid
(see the model readout below for what that failure mode looks like in practice).
Instead: store the literal secret in exactly **one** file, outside either tool's
own config tree (e.g. `~/.credentials/<provider>.env`, mode 600), and have each
tool's own config *reference* that file rather than embed the value:

- If a tool's env-loading already supports `${VAR}` expansion against the
  process environment (LifeOS's own `.env` does, via `python-dotenv`/
  `pydantic-settings` — confirmed: `dotenv_values()` resolves `${OTHER_VAR}`
  against anything already in `os.environ` when it parses a file), export the
  one shared variable into the environment ahead of that tool's own env-file
  load (e.g. a systemd `EnvironmentFile=` pointed at the shared file), then
  have each tool's own `.env` set its own variable name to `${THE_SHARED_VAR}`.
- If a tool's env-loading is a plain line-by-line reader with no expansion
  support, this trick doesn't work for it — check before relying on it. The
  fallback for such a tool is whatever file-reference mechanism its own config
  format supports (an `api_key_file`-style setting, systemd's own
  `EnvironmentFile=`, etc.); embedding the literal value is what this section
  is trying to eliminate.

Either way, each tool keeps its own variable name — there's no requirement (or
benefit) to renaming across tools, only to stop duplicating the value.

## Model Readout (#658)

`GET /health/full` includes a `models` key reporting, per chat surface, which
model is **actually serving it right now** — `api/services/model_readout.py`.
This exists because a configured value and a live value can silently diverge
(a local LLM server restarted against a different model than its own config
still names; an external tool's config snapshot going stale relative to its
running process). The readout never re-reads a config file to answer "what's
live" — each surface uses whichever live signal actually exists for it:

- **LifeOS native picker:** in-memory settings for the Anthropic backend
  (that setting IS the live value — nothing else can run a different
  Anthropic model out from under it), or a live `/v1/models` probe against
  the local backend's llama-server.
- **Hermes chat:** observed from the last real turn's own `usage` event,
  not probed. `LIFEOS_HERMES_BACKEND_URL` points at LifeOS's own adapter in
  front of the Hermes gateway, which has no capability endpoint to probe —
  and even a live probe against the gateway itself would answer "what
  could serve a turn," not "what did," since the adapter can pick a
  different model per turn. Reports `"unknown"` until this process has
  relayed at least one Hermes chat turn.
- **Hermes Telegram:** Hermes's Telegram bot talks to the gateway directly,
  bypassing LifeOS by design (its independence from LifeOS uptime is a
  property worth keeping — see client-surfaces.md), so LifeOS has no
  channel to observe or probe it at all. Reports `"not_observable"` — a
  status distinct from `"unknown"` (an attempt that failed) — and is never
  assumed to match Hermes chat's observed value, since the two can
  genuinely be answered by different models per turn.

A surface that can't be confirmed reports `"status": "unknown"` (or
`"not_observable"` where there's no attempt to make) — never the configured
value dressed up as confirmed-live.

---

## Related Documents

- [AGENTS.md](../../AGENTS.md) — Agent-facing project reference (points here for operational detail)
- [ADR-022: macOS FDA Inheritance and Safe Service Restart](../adr/022-macos-fda-inheritance-and-restart.md) — Design rationale for the `run`-not-`exec` and restart guidance in the FDA section above
- [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md) — The `.app` wrapper design; amended by ADR-022
- [Apple Health Import](apple-health.md) — Health/Fitness data import specifics
- [Observability — Technical](../specs/technical/observability.md) — Perf tracing and monitoring design
- [Data & Sync](../specs/technical/data-and-sync.md) — Nightly sync pipeline phases
- [Troubleshooting](troubleshooting.md) — General operational troubleshooting
- [Scripts Reference](scripts.md) — `auto-deploy.sh` / `auto-update-macos.sh` / `setup-launchd.sh` usage and flags
