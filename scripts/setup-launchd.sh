#!/bin/bash

# LifeOS Launchd Setup Script
# Generates plist files from templates and installs to ~/Library/LaunchAgents
#
# The operational body (prompting, generation, validation, install) is
# wrapped in main() and guarded at the bottom of this file — the pattern
# tests/test_deploy_drift.py established for scripts/auto-deploy.sh — so
# that sourcing this script never prompts, never touches
# ~/Library/LaunchAgents, and never calls a real `launchctl`. (Top-level
# variable assignments above and `set -e` still take effect on source, same
# as auto-deploy.sh; only the operational run itself is gated.)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFEOS_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$LIFEOS_PATH/config/launchd"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
# Every template hardcodes __HOME__/.venvs/lifeos (no __VENV__ placeholder
# exists to override it), so validation checks that exact path — not an
# operator-configurable one — to avoid checking a different directory than
# what's actually baked into the generated plist. Tests get an isolated venv
# by pointing $HOME at a sandbox, same as everything else __HOME__-derived.
VENV_DIR="$HOME/.venvs/lifeos"

# --- Generation ------------------------------------------------------------

# Escape a value so it's safe to drop into the replacement side of an
# `s|X|<value>|` sed expression: a literal `&` (means "whole match" in a sed
# replacement), `|` (our delimiter), or `\` in a real path would otherwise
# corrupt the substitution or make sed error out.
_sed_escape_replacement() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/&/\\\&/g' -e 's/|/\\|/g'
}

# Substitute the __HOME__/__LIFEOS_PATH__/__VAULT_PATH__ placeholder
# convention into a template, writing the result to $2.
generate_plist() {
    local template="$1" output="$2" home="$3" lifeos_path="$4" vault_path="$5"
    local esc_home esc_lifeos_path esc_vault_path
    esc_home=$(_sed_escape_replacement "$home")
    esc_lifeos_path=$(_sed_escape_replacement "$lifeos_path")
    esc_vault_path=$(_sed_escape_replacement "$vault_path")
    sed -e "s|__HOME__|$esc_home|g" \
        -e "s|__LIFEOS_PATH__|$esc_lifeos_path|g" \
        -e "s|__VAULT_PATH__|$esc_vault_path|g" \
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

# A `<key>NAME</key>` line (real templates indent it) immediately followed
# by a `<string>value</string>` line — the structure every template in this
# repo uses. Avoids depending on plutil -extract (macOS-only, and this needs
# to work when sourced under test on Linux too). Matches the key line by
# substring rather than exact equality so real (indented) templates match,
# not just an unindented test fixture.
_plist_string_value() {
    local plist="$1" key="<key>${2}</key>"
    awk -v key="$key" '
        found { if ($0 ~ /<string>/) { gsub(/.*<string>|<\/string>.*/, ""); print; exit } }
        index($0, key) > 0 { found=1 }
    ' "$plist"
}

# Print any missing path this plist depends on (one per line: "<label>:
# <path>"). Empty output means every path referenced exists on this machine.
# Checks WorkingDirectory (parsed from the plist itself) and the venv this
# install resolved (every template uses the same __HOME__/.venvs/lifeos
# convention, so there's nothing plist-specific to parse for that one).
# Checks the venv's own python interpreter, not just the directory — an
# empty or half-created venv (e.g. `python3 -m venv` ran but
# `pip install -r requirements.txt` never did) would otherwise pass with a
# directory that exists but launches nothing.
check_paths_exist() {
    local plist="$1" venv_dir="$2"
    local workdir
    workdir=$(_plist_string_value "$plist" "WorkingDirectory")
    if [ -n "$workdir" ] && [ ! -d "$workdir" ]; then
        echo "WorkingDirectory: $workdir"
    fi
    if [ ! -x "$venv_dir/bin/python" ]; then
        echo "venv: $venv_dir/bin/python"
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

# Copy a generated plist into place only if it differs from what's already
# there. Re-running setup on a host where a service is already loaded and
# running must never silently replace it — this script never calls
# launchctl itself (it only ever writes files), so "don't touch what's
# unchanged" is the whole idempotency contract.
install_plist() {
    local src="$1" dst_dir="$2"
    local filename dst
    filename=$(basename "$src")
    dst="$dst_dir/$filename"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "  Unchanged: $filename (already installed, left in place)"
        return 0
    fi
    cp "$src" "$dst"
    echo "  Installed: $filename"
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

# Copy to LaunchAgents (skip chromadb — cron watchdog instead). Only ever
# writes an unchanged file's content over itself when it actually differs
# (install_plist); never touches a file that already matches.
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
        install_plist "$plist" "$LAUNCH_AGENTS"
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
