#!/bin/bash

# LifeOS Launchd Setup Script
# Generates plist files from templates and installs to ~/Library/LaunchAgents

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFEOS_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$LIFEOS_PATH/config/launchd"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "LifeOS Launchd Setup"
echo "===================="
echo ""
echo "This script will configure launchd services for:"
echo "  - com.lifeos.api (API server)"
echo "  - com.lifeos.crm-sync (nightly sync)"
echo ""
echo "Note: ChromaDB should use cron watchdog instead of launchd."
echo "See docs/guides/LAUNCHD-SETUP.md for ChromaDB cron setup."
echo ""

# Prompt for vault path
read -p "Enter your Obsidian vault path: " VAULT_PATH

# Validate vault path
if [ ! -d "$VAULT_PATH" ]; then
    echo "Error: Vault path does not exist: $VAULT_PATH"
    exit 1
fi

# Expand ~ if present
VAULT_PATH="${VAULT_PATH/#\~/$HOME}"

echo ""
echo "Configuration:"
echo "  Home:       $HOME"
echo "  LifeOS:     $LIFEOS_PATH"
echo "  Vault:      $VAULT_PATH"
echo ""

read -p "Continue? (y/n) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# Create logs directory
mkdir -p "$LIFEOS_PATH/logs"

# Generate plist files from templates
echo ""
echo "Generating plist files..."

for template in "$LAUNCHD_DIR"/*.plist.template; do
    if [ -f "$template" ]; then
        output="${template%.template}"
        filename=$(basename "$output")

        sed -e "s|__HOME__|$HOME|g" \
            -e "s|__LIFEOS_PATH__|$LIFEOS_PATH|g" \
            -e "s|__VAULT_PATH__|$VAULT_PATH|g" \
            "$template" > "$output"

        echo "  Generated: $filename"
    fi
done

# Validate plist files
echo ""
echo "Validating plist files..."

for plist in "$LAUNCHD_DIR"/*.plist; do
    if [ -f "$plist" ] && [[ ! "$plist" == *.template ]]; then
        filename=$(basename "$plist")
        if plutil -lint "$plist" > /dev/null 2>&1; then
            echo "  Valid: $filename"
        else
            echo "  ERROR: $filename is invalid!"
            plutil -lint "$plist"
            exit 1
        fi
    fi
done

# Copy to LaunchAgents (skip chromadb)
echo ""
echo "Installing to $LAUNCH_AGENTS..."

mkdir -p "$LAUNCH_AGENTS"

for plist in "$LAUNCHD_DIR"/*.plist; do
    if [ -f "$plist" ] && [[ ! "$plist" == *.template ]]; then
        filename=$(basename "$plist")
        # Skip chromadb - should use cron watchdog
        if [[ "$filename" == *"chromadb"* ]]; then
            echo "  Skipped: $filename (use cron watchdog instead)"
            continue
        fi
        cp "$plist" "$LAUNCH_AGENTS/"
        echo "  Installed: $filename"
    fi
done

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo ""
echo "1. Load the services:"
echo "   launchctl load ~/Library/LaunchAgents/com.lifeos.api.plist"
echo "   launchctl load ~/Library/LaunchAgents/com.lifeos.crm-sync.plist"
echo ""
echo "2. Set up ChromaDB cron watchdog:"
echo "   crontab -e"
echo "   * * * * * pgrep -f \"chroma run\" || (cd $LIFEOS_PATH && ./scripts/chromadb.sh start >> /tmp/chromadb-watchdog.log 2>&1)"
echo ""
echo "3. Verify services are running:"
echo "   launchctl list | grep lifeos"
echo ""
echo "See docs/guides/LAUNCHD-SETUP.md for troubleshooting."
