#!/bin/bash
# Create the LifeOS.app FDA wrapper bundle
#
# This creates a macOS .app bundle at /Applications/LifeOS.app that serves as
# a Full Disk Access container. Cron jobs and launchd agents route through it
# to access protected databases (iMessage, Phone calls, Photos).
#
# After creation, grant Full Disk Access:
#   System Settings → Privacy & Security → Full Disk Access → Add LifeOS.app
#
# Usage: ./scripts/create-lifeos-app.sh

set -euo pipefail

APP_PATH="/Applications/LifeOS.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FORCE=false

for arg in "$@"; do
    if [ "$arg" = "--force" ] || [ "$arg" = "-f" ]; then
        FORCE=true
    fi
done

echo "LifeOS.app Creator"
echo "==================="
echo ""
echo "Project directory: $PROJECT_DIR"
echo "App location:      $APP_PATH"
echo ""

# Check for existing app
if [ -d "$APP_PATH" ]; then
    if [ "$FORCE" = true ]; then
        echo "Overwriting existing $APP_PATH (--force)"
    else
        echo "WARNING: $APP_PATH already exists."
        read -p "Overwrite? (y/N) " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            echo "Aborted."
            exit 0
        fi
    fi
    rm -rf "$APP_PATH"
fi

# Create bundle structure
mkdir -p "$APP_PATH/Contents/MacOS"

# Create Info.plist
cat > "$APP_PATH/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.lifeos.app</string>
    <key>CFBundleName</key>
    <string>LifeOS</string>
    <key>CFBundleExecutable</key>
    <string>LifeOS</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
</dict>
</plist>
PLIST

# Create the main executable script
cat > "$APP_PATH/Contents/MacOS/LifeOS" << SCRIPT
#!/bin/bash
# LifeOS App Wrapper
# This .app bundle has Full Disk Access granted in macOS Privacy settings.
# All scripts that need FDA or access to ~/Documents/ should route through here.
#
# Usage:
#   LifeOS                  - Prints this usage and exits 1 (no default action)
#   LifeOS server           - Start the API server (must be explicit)
#   LifeOS fda-sync         - Run FDA sync (phone/iMessage via Terminal.app)
#   LifeOS watchdog         - ChromaDB health check and auto-restart
#   LifeOS server-watchdog  - Server health check and auto-restart
#   LifeOS exec <cmd>       - Run arbitrary command with FDA permissions
#   LifeOS run <cmd>        - Run command as subprocess, keeping LifeOS.app as parent (preserves FDA)
#
# IMPORTANT: no-arg invocation must NEVER default to 'server'. This app is
# often installed as a login item on machines that only run the export agent
# (the API server lives on the Linux host per the architecture) — an
# implicit 'server' default means every login silently launches a rogue
# uvicorn on :8000 via Terminal.app. 'server' requires an explicit argument.

LIFEOS_DIR="$PROJECT_DIR"

case "\${1:-}" in
    "")
        echo "Usage: LifeOS <command> [args]" >&2
        echo "" >&2
        echo "Commands:" >&2
        echo "  server           - Start the API server (must be explicit)" >&2
        echo "  fda-sync         - Run FDA sync (phone/iMessage via Terminal.app)" >&2
        echo "  watchdog         - ChromaDB health check and auto-restart" >&2
        echo "  server-watchdog  - Server health check and auto-restart" >&2
        echo "  exec <cmd>       - Run arbitrary command with FDA permissions" >&2
        echo "  run <cmd>        - Run command as subprocess, keeping LifeOS.app as parent" >&2
        exit 1
        ;;

    server)
        osascript <<'EOF'
tell application "Terminal"
    set serverRunning to false
    try
        do shell script "lsof -i :8000 | grep -q LISTEN"
        set serverRunning to true
    end try
    if not serverRunning then
        do script "cd $PROJECT_DIR && ./scripts/server.sh start"
        activate
    end if
end tell
EOF
        ;;

    fda-sync)
        # Wake NVMe before running Python
        ls /opt/homebrew/bin > /dev/null 2>&1
        sleep 2
        osascript -e "tell application \"Terminal\" to do script \"cd \$LIFEOS_DIR && ~/.venvs/lifeos/bin/python scripts/run_fda_syncs.py && sleep 2 && exit\""
        ;;

    server-watchdog)
        exec "\$LIFEOS_DIR/scripts/server-watchdog.sh"
        ;;

    watchdog)
        HEALTH_URL="http://localhost:8001/api/v2/heartbeat"
        LOG="\$LIFEOS_DIR/logs/chromadb-watchdog.log"
        if curl -s --max-time 5 "\$HEALTH_URL" > /dev/null 2>&1; then
            exit 0
        fi
        echo "\$(date '+%Y-%m-%d %H:%M:%S') - ChromaDB not responding, attempting restart" >> "\$LOG"
        "\$LIFEOS_DIR/scripts/chromadb.sh" restart >> "\$LOG" 2>&1
        sleep 5
        if curl -s --max-time 5 "\$HEALTH_URL" > /dev/null 2>&1; then
            echo "\$(date '+%Y-%m-%d %H:%M:%S') - ChromaDB restarted successfully" >> "\$LOG"
        else
            echo "\$(date '+%Y-%m-%d %H:%M:%S') - ERROR: ChromaDB restart failed" >> "\$LOG"
        fi
        ;;

    exec)
        shift
        exec "\$@"
        ;;

    run)
        # Run command as subprocess, keeping LifeOS.app as parent process.
        # Unlike exec, this preserves FDA because macOS TCC checks the
        # responsible process (LifeOS.app) rather than the child binary.
        shift
        "\$@"
        ;;

    *)
        echo "Unknown command: \$1" >&2
        echo "Usage: LifeOS [server|fda-sync|watchdog|server-watchdog|exec <cmd>|run <cmd>]" >&2
        exit 1
        ;;
esac
SCRIPT

chmod +x "$APP_PATH/Contents/MacOS/LifeOS"

echo ""
echo "Created $APP_PATH"
echo ""
echo "Next steps:"
echo "  1. Open System Settings → Privacy & Security → Full Disk Access"
echo "  2. Click '+' and add /Applications/LifeOS.app"
echo "  3. Also ensure Terminal.app has Full Disk Access"
echo ""
echo "Verify: /Applications/LifeOS.app/Contents/MacOS/LifeOS watchdog"
