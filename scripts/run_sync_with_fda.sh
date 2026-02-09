#!/bin/bash
# Run syncs that require Full Disk Access through Terminal
# This inherits Terminal's FDA permission for accessing CallHistoryDB, iMessage, etc.
#
# Schedule via cron: 50 2 * * * /Users/nathanramia/Documents/Code/LifeOS/scripts/run_sync_with_fda.sh

osascript <<'EOF'
tell application "Terminal"
    activate
    do script "cd /Users/nathanramia/Documents/Code/LifeOS && echo '=== FDA Sync Started ===' && .venv/bin/python scripts/sync_phone_calls.py --execute 2>&1 && .venv/bin/python scripts/sync_imessage_interactions.py --execute 2>&1 && echo '=== FDA Sync Complete ===' && sleep 3 && exit"
end tell
EOF
