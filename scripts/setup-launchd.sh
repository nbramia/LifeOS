#!/bin/bash

# LifeOS Launchd Setup Script
# Generates plist files from templates and installs to ~/Library/LaunchAgents
#
# The operational body (prompting, generation, validation, install) is
# wrapped in main() and guarded at the bottom of this file so that sourcing
# this script — the pattern tests/test_deploy_drift.py established for
# scripts/auto-deploy.sh — only defines functions. It never prompts, touches
# ~/Library/LaunchAgents, or calls a real `launchctl`/`plutil`.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFEOS_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$LIFEOS_PATH/config/launchd"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
VENV_DIR="${LIFEOS_VENV:-$HOME/.venvs/lifeos}"

# --- Generation ------------------------------------------------------------

# Substitute the __HOME__/__LIFEOS_PATH__/__VAULT_PATH__ placeholder
# convention into a template, writing the result to $2.
generate_plist() {
    local template="$1" output="$2" home="$3" lifeos_path="$4" vault_path="$5"
    sed -e "s|__HOME__|$home|g" \
        -e "s|__LIFEOS_PATH__|$lifeos_path|g" \
        -e "s|__VAULT_PATH__|$vault_path|g" \
        "$template" > "$output"
}

# --- Validation (#776) -------------------------------------------------------
#
# A previous field deployment copied a plist straight into
# ~/Library/LaunchAgents with literal, unfilled-in placeholder text still in
# it — nothing caught it, because the substitution step's failure to fill in
# a value is not a plist *syntax* error, so plutil -lint (below) passed it.
# The service then crash-looped against directories that never existed on
# that machine. These two checks catch that class of mistake before install.

# Print any leftover `__TOKEN__`-shaped placeholder in a generated plist (one
# per line, sorted+deduped). Empty output means fully substituted.
check_placeholders() {
    local plist="$1"
    grep -oE '__[A-Z_]+__' "$plist" 2>/dev/null | sort -u
}

# Single-line `<key>NAME</key>` immediately followed by `<string>value</string>`
# — the structure every template in this repo uses. Avoids depending on
# plutil -extract (macOS-only, and this needs to work when sourced under
# test on Linux too).
_plist_string_value() {
    local plist="$1" key="$2"
    awk -v key="<key>${key}</key>" '
        found { if ($0 ~ /<string>/) { gsub(/.*<string>|<\/string>.*/, ""); print; exit } }
        $0 == key { found=1 }
    ' "$plist"
}

# Print any missing path this plist depends on (one per line: "<label>:
# <path>"). Empty output means every path referenced exists on this machine.
# Checks WorkingDirectory (parsed from the plist itself) and the venv this
# install resolved (every template uses the same __HOME__/.venvs/lifeos
# convention, so there's nothing plist-specific to parse for that one).
check_paths_exist() {
    local plist="$1" venv_dir="$2"
    local workdir
    workdir=$(_plist_string_value "$plist" "WorkingDirectory")
    if [ -n "$workdir" ] && [ ! -d "$workdir" ]; then
        echo "WorkingDirectory: $workdir"
    fi
    if [ ! -d "$venv_dir" ]; then
        echo "venv: $venv_dir"
    fi
}

# Refuse to install a plist that is incomplete or broken. Prints the problem
# and returns 1 rather than installing; returns 0 (silent) when clean.
validate_plist() {
    local plist="$1" venv_dir="$2"
    local filename
    filename=$(basename "$plist")
    local ok=true

    local leftover
    leftover=$(check_placeholders "$plist")
    if [ -n "$leftover" ]; then
        echo "  ERROR: $filename still has unsubstituted placeholder(s): $(echo "$leftover" | tr '\n' ' ')"
        ok=false
    fi

    if command -v plutil >/dev/null 2>&1; then
        if ! plutil -lint "$plist" > /dev/null 2>&1; then
            echo "  ERROR: $filename is invalid!"
            plutil -lint "$plist" || true
            ok=false
        fi
    fi

    local missing
    missing=$(check_paths_exist "$plist" "$venv_dir")
    if [ -n "$missing" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] && echo "  ERROR: $filename references a path that does not exist — $line"
        done <<< "$missing"
        ok=false
    fi

    [ "$ok" = true ]
}

# --- Operational run ---------------------------------------------------------
main() {

echo "LifeOS Launchd Setup"
echo "===================="
echo ""
echo "This script will configure launchd services for:"
echo "  - com.lifeos.api (API server)"
echo "  - com.lifeos.crm-sync (nightly sync)"
echo ""
echo "Note: ChromaDB should use cron watchdog instead of launchd."
echo "See docs/guides/launchd-setup.md for ChromaDB cron setup."
echo ""

# Accept vault path as CLI argument or prompt interactively
# Usage: ./scripts/setup-launchd.sh [vault_path] [--yes]
VAULT_PATH=""
AUTO_YES=false

for arg in "$@"; do
    if [ "$arg" = "--yes" ] || [ "$arg" = "-y" ]; then
        AUTO_YES=true
    elif [ -z "$VAULT_PATH" ]; then
        VAULT_PATH="$arg"
    fi
done

if [ -z "$VAULT_PATH" ]; then
    read -p "Enter your Obsidian vault path: " VAULT_PATH
fi

# Expand ~ if present
VAULT_PATH="${VAULT_PATH/#\~/$HOME}"

# Validate vault path
if [ ! -d "$VAULT_PATH" ]; then
    echo "Error: Vault path does not exist: $VAULT_PATH"
    exit 1
fi

echo ""
echo "Configuration:"
echo "  Home:       $HOME"
echo "  LifeOS:     $LIFEOS_PATH"
echo "  Vault:      $VAULT_PATH"
echo "  Venv:       $VENV_DIR"
echo ""

if [ "$AUTO_YES" = false ]; then
    read -p "Continue? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
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
        generate_plist "$template" "$output" "$HOME" "$LIFEOS_PATH" "$VAULT_PATH"
        echo "  Generated: $filename"
    fi
done

# Validate plist files — refuse to install anything incomplete or broken
# (leftover placeholder, invalid XML, or a WorkingDirectory/venv that
# doesn't exist on this machine).
echo ""
echo "Validating plist files..."

VALIDATION_FAILED=false
for plist in "$LAUNCHD_DIR"/*.plist; do
    if [ -f "$plist" ] && [[ ! "$plist" == *.template ]]; then
        filename=$(basename "$plist")
        if validate_plist "$plist" "$VENV_DIR"; then
            echo "  Valid: $filename"
        else
            VALIDATION_FAILED=true
        fi
    fi
done

if [ "$VALIDATION_FAILED" = true ]; then
    echo ""
    echo "Aborting install: one or more plist files failed validation (see ERROR lines above)."
    exit 1
fi

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
echo "See docs/guides/launchd-setup.md for troubleshooting."

}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
