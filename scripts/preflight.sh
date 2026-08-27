#!/bin/bash
# LifeOS Preflight Check
# Validates prerequisites before starting the server.
# Usage: ./scripts/preflight.sh
#        ./scripts/server.sh preflight

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$HOME/.venvs/lifeos/bin/python"

pass=0
warn=0
fail=0

check_pass() { echo "  [PASS] $1"; pass=$((pass + 1)); }
check_warn() { echo "  [WARN] $1"; warn=$((warn + 1)); }
check_fail() { echo "  [FAIL] $1"; fail=$((fail + 1)); }

# Read a KEY=value from .env, stripping only outer whitespace and one layer
# of surrounding matching quotes — interior whitespace/quotes are preserved.
env_value() {
    local key="$1" raw
    raw=$(grep -E "^${key}=" "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    if [ "${#raw}" -ge 2 ]; then
        if { [[ "$raw" == \"*\" ]] && [[ "$raw" == *\" ]]; } || { [[ "$raw" == \'*\' ]] && [[ "$raw" == *\' ]]; }; then
            raw="${raw:1:-1}"
        fi
    fi
    printf '%s' "$raw"
}

echo ""
echo "LifeOS Preflight Check"
echo "======================"
echo ""

# 1. .env exists
if [ -f "$PROJECT_DIR/.env" ]; then
    check_pass ".env file exists"
else
    check_fail ".env file missing — cp .env.example .env"
fi

# 2. ANTHROPIC_API_KEY set and not placeholder
if [ -f "$PROJECT_DIR/.env" ]; then
    api_key=$(env_value ANTHROPIC_API_KEY)
    if [ -n "$api_key" ] && [ "$api_key" != "sk-ant-..." ]; then
        check_pass "Anthropic API key configured"
    else
        check_fail "ANTHROPIC_API_KEY not set or still placeholder — edit .env"
    fi
fi

# 3. LIFEOS_VAULT_PATH set and directory exists
if [ -f "$PROJECT_DIR/.env" ]; then
    vault_path=$(env_value LIFEOS_VAULT_PATH)
    if [ -n "$vault_path" ] && [ -d "$vault_path" ]; then
        check_pass "Vault path exists: $vault_path"
    elif [ -n "$vault_path" ]; then
        check_fail "Vault path not found: $vault_path"
    else
        check_fail "LIFEOS_VAULT_PATH not set in .env"
    fi
fi

# 4. LIFEOS_USER_NAME set
if [ -f "$PROJECT_DIR/.env" ]; then
    user_name=$(env_value LIFEOS_USER_NAME)
    if [ -n "$user_name" ] && [ "$user_name" != "User" ]; then
        check_pass "User name: $user_name"
    else
        check_warn "LIFEOS_USER_NAME not set — AI will use generic 'User' name"
    fi
fi

# 5. Python venv exists
if [ -f "$PYTHON" ]; then
    check_pass "Python venv found at ~/.venvs/lifeos"
else
    check_fail "Python venv missing — python3 -m venv ~/.venvs/lifeos && source ~/.venvs/lifeos/bin/activate && pip install -r requirements.txt"
fi

# 6. Key packages importable
if [ -f "$PYTHON" ]; then
    if "$PYTHON" -c "import fastapi, chromadb, anthropic" 2>/dev/null; then
        check_pass "Dependencies installed"
    else
        check_fail "Missing dependencies — source ~/.venvs/lifeos/bin/activate && pip install -r requirements.txt"
    fi
fi

# 7. ChromaDB running
if curl -sf http://localhost:8001/api/v2/heartbeat > /dev/null 2>&1; then
    check_pass "ChromaDB running"
else
    check_fail "ChromaDB not running — ./scripts/chromadb.sh start"
fi

# 8. Port 8000 available (or LifeOS already running)
if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    check_pass "LifeOS already running on port 8000"
elif lsof -i :8000 > /dev/null 2>&1; then
    check_warn "Port 8000 in use by another process"
else
    check_pass "Port 8000 available"
fi

# 9. MY_PERSON_ID check (informational)
if [ -f "$PROJECT_DIR/.env" ]; then
    person_id=$(env_value LIFEOS_MY_PERSON_ID)
    if [ -n "$person_id" ]; then
        check_pass "Person ID configured"
    else
        check_warn "LIFEOS_MY_PERSON_ID not set — relationship features limited (set after first sync)"
    fi
fi

echo ""
total=$((pass + warn + fail))
echo "Result: $pass/$total passed, $warn warning(s), $fail failure(s)"

if [ $fail -gt 0 ]; then
    echo ""
    echo "Fix the failures above before starting the server."
    exit 1
elif [ $warn -gt 0 ]; then
    echo ""
    echo "Ready to start (with warnings): ./scripts/server.sh start"
    exit 0
else
    echo ""
    echo "Ready to start: ./scripts/server.sh start"
    exit 0
fi
