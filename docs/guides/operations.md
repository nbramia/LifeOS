# Operations Reference

> **Status:** Complete
> **Owner:** Operations
> **Last Updated:** 2026-07-08
> **Audience:** Operators

Operational procedures that don't belong in the day-to-day coding reference: the Apple Data Agent, Monarch Money auth, and quick observability commands. Moved here from `AGENTS.md` to keep the agent-facing file lean.

---

## Apple Data Agent (optional, macOS only)

If you have a Mac with iMessage/phone data, it can export Apple ecosystem data and sync it to the Linux server nightly via `scripts/apple_data_agent.sh`.

### macOS FDA (Full Disk Access)

`/Applications/LifeOS.app` is a bash-script-based .app bundle with **Full Disk Access**. macOS cron cannot access `~/Library/Messages/` without FDA, so the Apple Data Agent cron job routes through this wrapper.

If adding new cron jobs or scripts on macOS that need to access protected directories, route them through `LifeOS exec`.

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

---

## Related Documents

- [AGENTS.md](../../AGENTS.md) — Agent-facing project reference (points here for operational detail)
- [Apple Health Import](apple-health.md) — Health/Fitness data import specifics
- [Observability — Technical](../specs/technical/observability.md) — Perf tracing and monitoring design
- [Data & Sync](../specs/technical/data-and-sync.md) — Nightly sync pipeline phases
- [Troubleshooting](troubleshooting.md) — General operational troubleshooting
