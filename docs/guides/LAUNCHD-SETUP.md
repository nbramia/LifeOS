# Launchd Setup

Configure macOS launchd services for automatic startup and scheduled sync.

---

## Overview

LifeOS uses three launchd services:

| Service | Purpose | Schedule |
|---------|---------|----------|
| `com.lifeos.api` | API server | At boot, always running |
| `com.lifeos.chromadb` | ChromaDB server | At boot, always running |
| `com.lifeos.crm-sync` | Nightly data sync | Daily at 3 AM |

**Note**: ChromaDB has issues with launchd on macOS (exit code 78). Use a cron watchdog instead.

---

## Quick Setup

Run the setup script to configure all services:

```bash
./scripts/setup-launchd.sh
```

The script will:
1. Prompt for your vault path
2. Generate plist files from templates
3. Copy to `~/Library/LaunchAgents/`
4. Show commands to load services

---

## Manual Setup

### Step 1: Generate plist Files

Copy templates and replace placeholders:

```bash
cd config/launchd

# For each template
for template in *.plist.template; do
  output="${template%.template}"
  sed -e "s|__HOME__|$HOME|g" \
      -e "s|__LIFEOS_PATH__|$(pwd)/../..|g" \
      -e "s|__VAULT_PATH__|/path/to/your/vault|g" \
      "$template" > "$output"
done
```

### Step 2: Verify plist Files

Check the generated files:

```bash
# Validate XML
plutil -lint com.lifeos.api.plist
plutil -lint com.lifeos.crm-sync.plist
```

### Step 3: Install Services

```bash
# Copy to LaunchAgents
cp com.lifeos.api.plist ~/Library/LaunchAgents/
cp com.lifeos.crm-sync.plist ~/Library/LaunchAgents/
```

### Step 4: Load Services

```bash
# Load API server
launchctl load ~/Library/LaunchAgents/com.lifeos.api.plist

# Load sync service
launchctl load ~/Library/LaunchAgents/com.lifeos.crm-sync.plist
```

---

## ChromaDB Cron Watchdog

ChromaDB doesn't work well with launchd (exit code 78 issues). Use cron instead:

```bash
# Edit crontab
crontab -e

# Add watchdog (checks every minute, starts if not running)
* * * * * pgrep -f "chroma run" || (cd /path/to/LifeOS && ./scripts/chromadb.sh start >> /tmp/chromadb-watchdog.log 2>&1)
```

---

## Service Management

### Check Status

```bash
# List LifeOS services
launchctl list | grep lifeos

# Check specific service
launchctl list com.lifeos.api
```

### Start/Stop Services

```bash
# Stop
launchctl stop com.lifeos.api

# Start
launchctl start com.lifeos.api

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.lifeos.api.plist

# Reload (after changes)
launchctl unload ~/Library/LaunchAgents/com.lifeos.api.plist
launchctl load ~/Library/LaunchAgents/com.lifeos.api.plist
```

---

## Log Locations

| Service | stdout | stderr |
|---------|--------|--------|
| API | `logs/lifeos-api.log` | `logs/lifeos-api-error.log` |
| Sync | `logs/crm-sync.log` | `logs/crm-sync-error.log` |
| ChromaDB | `logs/chromadb.log` | `logs/chromadb-error.log` |

View logs:
```bash
# Tail API logs
tail -f logs/lifeos-api.log

# View recent errors
tail -100 logs/lifeos-api-error.log
```

---

## Troubleshooting

### Exit Code 78 (ChromaDB)

**Cause**: macOS sandbox restrictions on ChromaDB.

**Solution**: Use cron watchdog instead of launchd (see above).

### Service Won't Start

1. Check plist is valid:
   ```bash
   plutil -lint ~/Library/LaunchAgents/com.lifeos.api.plist
   ```

2. Check paths exist:
   ```bash
   ls -la ~/.venvs/lifeos/bin/uvicorn
   ```

3. Check logs for errors:
   ```bash
   tail -50 logs/lifeos-api-error.log
   ```

### Service Starts Then Stops

**Cause**: Application crashing on startup.

**Solution**:
1. Check error logs
2. Try running manually: `./scripts/server.sh start`
3. Verify ChromaDB is running

### Sync Not Running

1. Check service is loaded:
   ```bash
   launchctl list | grep crm-sync
   ```

2. Check last run time:
   ```bash
   launchctl list com.lifeos.crm-sync
   ```

3. Run manually to test:
   ```bash
   uv run python scripts/run_all_syncs.py --dry-run
   ```

### Environment Variables Not Found

**Cause**: launchd doesn't inherit shell environment.

**Solution**: Set variables via `launchctl setenv`:
```bash
launchctl setenv ANTHROPIC_API_KEY "sk-ant-..."
```

Or add to plist `EnvironmentVariables` dict.

---

## Templates Reference

### API Server Template

Key settings in `com.lifeos.api.plist.template`:

```xml
<key>RunAtLoad</key>
<true/>

<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
</dict>

<key>ThrottleInterval</key>
<integer>10</integer>
```

- `RunAtLoad`: Start on boot
- `KeepAlive`: Restart on crash
- `ThrottleInterval`: Wait 10s before restart

### Sync Template

Key settings in `com.lifeos.crm-sync.plist.template`:

```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
</dict>
```

Runs daily at 3:00 AM.

---

## Next Steps

- [First Run Guide](../getting-started/FIRST-RUN.md)
- [Troubleshooting](../reference/TROUBLESHOOTING.md)
