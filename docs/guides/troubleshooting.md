# Troubleshooting

> **Status:** Complete
> **Last Updated:** 2026-08-28
> **Audience:** Operators

Common issues and solutions organized by category.

---

## Server Issues

### Ghost Server Process

**Symptom**: Code changes don't take effect, or conflicting behavior between localhost and Tailscale.

**Cause**: Multiple server processes running on different interfaces.

**Solution**:
```bash
# Kill all uvicorn processes
pkill -f uvicorn

# Start fresh
./scripts/server.sh start
```

**Prevention**: Always use `./scripts/server.sh` - never run `uvicorn` directly.

---

### Port 8000 Already in Use

**Symptom**: `Address already in use` error.

**Solution**:
```bash
# Find process on port 8000
lsof -i :8000

# Kill it
kill -9 <PID>

# Or use the script
./scripts/server.sh stop
./scripts/server.sh start
```

---

### Server Won't Start

**Symptom**: `./scripts/server.sh start` exits immediately.

**Diagnosis**:
```bash
# Check status
./scripts/server.sh status

# Check error logs
tail -50 logs/lifeos-api-error.log

# Run in the foreground to watch startup errors live (Ctrl+C to stop)
./scripts/server.sh foreground
```

Never run `uvicorn api.main:app` directly — it binds only localhost and creates a ghost server on a different interface than the script's `0.0.0.0` bind.

**Common Causes**:
- ChromaDB not running
- Missing environment variables
- Python dependency errors

---

### Changes Not Taking Effect

**Symptom**: Modified Python code doesn't change behavior.

**Cause**: Server not restarted.

**Solution**:
```bash
./scripts/server.sh restart
```

The server does NOT auto-reload.

---

## ChromaDB Issues

### Exit Code 78 (macOS only — launchd)

**Symptom**: ChromaDB fails with exit code 78 when run via launchd.

**Cause**: macOS sandbox/TCC restrictions.

**Solution**: Use cron watchdog instead:
```bash
# Add to crontab
crontab -e

# Watchdog line
* * * * * pgrep -f "chroma run" || (cd /path/to/LifeOS && ./scripts/chromadb.sh start)
```

---

### Connection Refused (Port 8001)

**Symptom**: `Connection refused` when accessing ChromaDB.

**Solution**:
```bash
# Check if running
pgrep -f "chroma run"

# Start if not
./scripts/chromadb.sh start

# Verify
curl http://localhost:8001/api/v2/heartbeat
```

---

### ChromaDB Won't Start

**Diagnosis**:
```bash
# Check port
lsof -i :8001

# Check logs
tail -50 logs/chromadb-error.log

# Try manual start
source ~/.venvs/lifeos/bin/activate
chroma run --path ./data/chromadb --port 8001
```

---

## LLM Backend Issues

### Server won't start: "LIFEOS_LLM_BACKEND is ... but ... is not set"

**Symptom**: `LLMBackendNotConfiguredError` at startup.

**Cause**: `LIFEOS_LLM_BACKEND=anthropic` (the default) with no `ANTHROPIC_API_KEY`, or `LIFEOS_LLM_BACKEND=remote` with `LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY` incomplete. Both backends fail fast rather than silently falling back — see [ADR-024](../adr/024-remote-llm-backend.md).

**Solution**: Set the missing variable(s), or switch `LIFEOS_LLM_BACKEND` to a backend you have configured (`local` needs only a reachable `LIFEOS_LOCAL_LLM_URL`). See [Configuration → LLM Backend](configuration.md#llm-backend--synthesis-and-orchestration).

---

### Remote provider requests fail (401/404) or hang

**Symptom**: Chat turns on the "Remote" model picker option, or with `LIFEOS_LLM_BACKEND=remote`, error out or time out.

**Causes**: Wrong bearer token; a model id the provider doesn't serve; a base URL with an extra or missing `/v1` (either convention is accepted — see [Configuration](configuration.md#openai-compatible-remote-provider)); the provider is genuinely down.

**Solution**: Confirm `LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY` against the provider's own docs, and raise `LIFEOS_REMOTE_LLM_TIMEOUT` (default 90s) if requests are timing out rather than erroring.

---

## Hermes / Front-Door Issues

### `/chat` shows "Hermes" as unavailable, or a Telegram persona bot falls back to native replies

**Symptom**: The Hermes option is missing from the backend picker, or a persona bot that should route through Hermes answers natively instead (sometimes with a one-time in-channel notice).

**Cause**: `LIFEOS_HERMES_BACKEND_URL` is unset (`configured: false`), or set but the gateway isn't reachable (`configured: true, reachable: false`) — see `GET /api/hermes/status` in [api-reference.md](../specs/product/api-reference.md).

**Solution**: This is a safe degraded mode, not a bug — LifeOS answers natively either way. To use Hermes, set `LIFEOS_HERMES_BACKEND_URL` (and `LIFEOS_HERMES_BACKEND_TOKEN` if the gateway requires one) and confirm the gateway itself is up. A bot can also be opted out of Hermes permanently with `"backend": "lifeos"` in `config/telegram_bots.local.json` — see [personas.md](personas.md).

---

## Google OAuth Issues

### "Access blocked: This app's request is invalid"

**Cause**: OAuth consent screen not configured or you're not a test user.

**Solution**:
1. Go to Google Cloud Console → OAuth consent screen
2. Add your email as a test user
3. Make sure you're signing in with that email

---

### Token Expired

**Symptom**: `Token has been expired or revoked`

**Solution**:
```bash
# Re-authenticate
~/.venvs/lifeos/bin/python scripts/google_auth.py --account personal
```

---

### Invalid Credentials

**Symptom**: `Invalid client` or credentials errors.

**Solution**:
1. Re-download credentials from Google Cloud Console
2. Save to correct path (`config/credentials-personal.json`)
3. Verify JSON is valid: `cat config/credentials-personal.json | jq`

---

## macOS Permission Issues (Apple Data Agent only)

These issues apply only to macOS machines running the Apple Data Agent for iMessage, Contacts, and Photos sync.

### Full Disk Access Required

**Symptom**: Can't access iMessage, Contacts, or Photos databases.

**Solution**:
1. Open System Settings → Privacy & Security → Full Disk Access
2. Add Terminal (or your terminal app)
3. Add Python if running directly

---

### Contacts Access Denied

**Symptom**: Contacts sync returns empty results.

**Solution**:
1. System Settings → Privacy & Security → Contacts
2. Add Terminal or the app running LifeOS

---

### Photos Access Denied

**Symptom**: Photos sync fails or returns no data.

**Solution**:
1. System Settings → Privacy & Security → Photos
2. Add Terminal or the app running LifeOS

---

## Sync Issues

### Sync Timeout

**Symptom**: Sync script times out or hangs.

**Causes**:
- Large data volume
- API rate limiting
- Network issues

**Solution**:
```bash
# Run single source
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --source gmail --force

# Check status
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status
```

---

### Sync Errors in Vault

**Location**: Check `~/Notes/LifeOS/sync_errors.md` (or your vault's LifeOS folder).

---

### Entity Not Linking

**Symptom**: Source entities not linking to PersonEntity.

**Causes**:
- Email/phone doesn't match exactly
- Missing identifier

**Solution**:
1. Check source entity has email/phone
2. Verify PersonEntity has matching identifier
3. Run entity linking manually:
   ```bash
   ~/.venvs/lifeos/bin/python scripts/link_slack_entities.py --execute
   ```

---

## Performance Issues

### High Memory Usage

**Symptom**: Process using excessive RAM.

**Solution**:
1. Check which process: `top -o %MEM` (Linux) or `top -o MEM` (macOS)
2. Restart services: `./scripts/server.sh restart`
3. For sync scripts, they have memory monitoring built in

---

### Slow Search

**Symptom**: Search queries take several seconds.

**Causes**:
- First query (model loading)
- Large result set
- ChromaDB not optimized

**Solution**:
1. Wait for first query to complete (model loading)
2. Use filters to reduce result set
3. Check ChromaDB is running locally (not remote)

---

### Reindex Taking Too Long

**Symptom**: Vault reindex takes hours.

**Solution**:
```bash
# Run incremental instead of full
curl -X POST http://localhost:8000/api/admin/reindex

# Full reindex (blocking) - only when needed
curl -X POST http://localhost:8000/api/admin/reindex/sync
```

---

## General Debugging

### Check All Service Status

```bash
# Server
./scripts/server.sh status

# ChromaDB
./scripts/chromadb.sh status

# Full health check
curl http://localhost:8000/health/full | jq
```

### View Logs

```bash
# API server
tail -f logs/lifeos-api.log
tail -f logs/lifeos-api-error.log

# ChromaDB
tail -f logs/chromadb.log

# Sync
tail -f logs/crm-sync.log
```

### Run Tests

```bash
# Quick unit tests
./scripts/test.sh

# Full test suite
./scripts/test.sh all
```

---

## Getting Help

If these solutions don't work:

1. Check the error logs for specific messages
2. Search existing issues: https://github.com/<your-fork>/LifeOS/issues
3. Open a new issue with:
   - Error message
   - Steps to reproduce
   - Environment (OS version, Python version)
   - Relevant log output

## Related Documents

- [Scripts Reference](scripts.md) -- All LifeOS scripts with usage examples
- [Configuration](configuration.md) -- Environment variables and config files
- [API Reference](../specs/product/api-reference.md) -- `GET /api/hermes/status` and other backend status fields
