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
# Schedule: 50 2 * * * /path/to/LifeOS/scripts/run_sync_with_fda.sh
#
# See also: scripts/run_fda_syncs.py (the actual sync runner with health tracking)

LIFEOS_DIR="${LIFEOS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script requires macOS (Full Disk Access via Terminal.app)."
    echo "On Linux, FDA syncs (iMessage, phone calls) are handled via the Apple Data Bridge."
    exit 0
fi

# Wake NVMe before running Python (Homebrew/venv live on the NVMe)
ls /opt/homebrew/bin > /dev/null 2>&1
sleep 2

osascript <<EOF
tell application "Terminal"
    activate
    do script "cd ${LIFEOS_DIR} && ~/.venvs/lifeos/bin/python scripts/run_fda_syncs.py && sleep 2 && exit"
end tell
EOF
