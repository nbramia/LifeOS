#!/bin/bash
# Run syncs that require Full Disk Access through Terminal.app
#
# Terminal.app has FDA permission, which is needed for:
# - CallHistoryDB (phone calls, FaceTime audio/video)
# - chat.db (iMessage/SMS)
#
# This runs at 2:50 AM via cron, 10 minutes before the main 3 AM sync.
# The main sync will detect these were recently completed and skip them.
#
# Schedule: 50 2 * * * /Users/nathanramia/Documents/Code/LifeOS/scripts/run_sync_with_fda.sh
#
# See also: scripts/run_fda_syncs.py (the actual sync runner with health tracking)

LIFEOS_DIR="/Users/nathanramia/Documents/Code/LifeOS"

osascript <<EOF
tell application "Terminal"
    activate
    do script "cd ${LIFEOS_DIR} && ~/.venvs/lifeos/bin/python scripts/run_fda_syncs.py && sleep 2 && exit"
end tell
EOF
