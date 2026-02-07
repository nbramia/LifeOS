# Troubleshooting

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

# Try running directly for better error output
source ~/.venvs/lifeos/bin/activate
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

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

### Exit Code 78 (launchd)

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
curl http://localhost:8001/api/v1/heartbeat
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

## Ollama Issues

### Model Not Found

**Symptom**: `model 'qwen2.5:7b-instruct' not found`

**Solution**:
```bash
# Pull the model
ollama pull qwen2.5:7b-instruct

# Verify
ollama list
```

---

### Ollama Not Running

**Symptom**: `connection refused` on port 11434.

**Solution**:
```bash
# Start Ollama
ollama serve &

# Verify
curl http://localhost:11434/api/tags | jq
```

---

### Slow Query Routing

**Symptom**: First query takes 30+ seconds.

**Cause**: Ollama loading model into memory.

**Solution**: This is normal on first query. Subsequent queries will be faster. Keep Ollama running.

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
uv run python scripts/google_auth.py --account personal
```

---

### Invalid Credentials

**Symptom**: `Invalid client` or credentials errors.

**Solution**:
1. Re-download credentials from Google Cloud Console
2. Save to correct path (`config/credentials-personal.json`)
3. Verify JSON is valid: `cat config/credentials-personal.json | jq`

---

## macOS Permission Issues

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
uv run python scripts/run_all_syncs.py --source gmail --force

# Check status
uv run python scripts/run_all_syncs.py --status
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
   uv run python scripts/link_slack_entities.py --execute
   ```

---

## Performance Issues

### High Memory Usage

**Symptom**: Process using excessive RAM.

**Solution**:
1. Check which process: `top -o MEM`
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

# Ollama
curl -s http://localhost:11434/api/tags | jq '.models[].name'

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
2. Search existing issues: https://github.com/yourusername/LifeOS/issues
3. Open a new issue with:
   - Error message
   - Steps to reproduce
   - Environment (macOS version, Python version)
   - Relevant log output
