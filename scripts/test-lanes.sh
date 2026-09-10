#!/bin/bash
# Shared pytest lane registry for scripts/test.sh and scripts/pre-push.
#
# The Python registry owns lane membership, marker expressions, and prerequisites.

TEST_LANE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_LANE_REGISTRY="$TEST_LANE_SCRIPT_DIR/test_lane_registry.py"
if command -v python3 >/dev/null 2>&1; then
    TEST_LANE_PYTHON=python3
else
    TEST_LANE_PYTHON=python
fi

test_lane_registry_call() {
    "$TEST_LANE_PYTHON" "$TEST_LANE_REGISTRY" "$@"
}

_test_lane_names=$(test_lane_registry_call lanes) || {
    echo "Unable to load test lane registry: $TEST_LANE_REGISTRY" >&2
    return 1 2>/dev/null || exit 1
}
TEST_LANE_NAMES=()
while IFS= read -r _test_lane_name; do
    [ -n "$_test_lane_name" ] && TEST_LANE_NAMES+=("$_test_lane_name")
done <<EOF
$_test_lane_names
EOF
[ "${#TEST_LANE_NAMES[@]}" -gt 0 ] || {
    echo "Test lane registry returned no lanes" >&2
    return 1 2>/dev/null || exit 1
}
# Membership precedence is deliberately browser-first.  Execution prioritizes
# fast feedback independently, while still dispatching each disjoint lane.
TEST_LANE_EXECUTION_ORDER=(fast-unit slow browser-free browser-server server integration)
for _test_lane_name in "${TEST_LANE_EXECUTION_ORDER[@]}"; do
    case " ${TEST_LANE_NAMES[*]} " in
        *" $_test_lane_name "*) ;;
        *) echo "Test lane execution order names unknown lane: $_test_lane_name" >&2; return 1 2>/dev/null || exit 1 ;;
    esac
done
unset _test_lane_names _test_lane_name

test_lane_marker() {
    test_lane_registry_call marker "$1"
}

test_lane_requires_server() {
    test_lane_registry_call requires "$1" server
}

test_lane_requires_playwright() {
    test_lane_registry_call requires "$1" playwright
}

# Collect once with the repository-owned plugin. The plugin refuses existing
# outputs and every path inside the checkout, so collection receipts cannot
# overwrite a source file.
test_lane_collect() {
    local output="$1"
    shift
    python -m pytest "$@" --collect-only -qq -p scripts.test_lane_plugin \
        --lifeos-lane-inventory "$output" > /dev/null
}

test_lane_inventory_status() {
    python - "$1" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["status"])
PY
}

test_lane_nodeids() {
    local inventory="$1" lane="$2"
    python - "$inventory" "$lane" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for nodeid in data["lanes"][sys.argv[2]]["nodeids"]:
    print(nodeid)
PY
}

test_lane_count() {
    local inventory="$1" lane="$2"
    python - "$inventory" "$lane" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data["lanes"][sys.argv[2]]["count"])
PY
}

test_lane_inventory_is_usable() {
    local inventory="$1"
    python - "$inventory" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if data["status"] != "ok":
    raise SystemExit(f"collection status is {data['status']}")
if data["unassigned"]:
    raise SystemExit("unassigned tests: " + ", ".join(data["unassigned"][:5]))
if data["assigned_count"] != data["collected_count"]:
    raise SystemExit("lane inventory does not partition every collected test")
PY
}

test_lane_prepush_parity() {
    python - "$1" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
fast = set(data["lanes"]["fast-unit"]["nodeids"])
browser_free = set(data["lanes"]["browser-free"]["nodeids"])
legacy_unit = {
    item["nodeid"] for item in data["items"]
    if "unit" in item["markers"] and "slow" not in item["markers"]
}
current_gate = fast | browser_free
legacy_gate = legacy_unit | browser_free
print(json.dumps({
    "status": data["status"],
    "collected_count": data["collected_count"],
    "assigned_count": data["assigned_count"],
    "lane_counts": {name: lane["count"] for name, lane in data["lanes"].items()},
    "fast_unit_count": len(fast),
    "legacy_unit_not_slow_count": len(legacy_unit),
    "fast_unit_only_legacy": sorted(legacy_unit - fast),
    "fast_unit_only_current": sorted(fast - legacy_unit),
    "current_gate_count": len(current_gate),
    "legacy_gate_count": len(legacy_gate),
    "gate_parity": current_gate == legacy_gate,
}, sort_keys=True))
PY
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    case "${1:-}" in
        inventory)
            shift
            inventory_dir=$(mktemp -d) || exit 1
            inventory="$inventory_dir/lane-inventory.json"
            test_lane_collect "$inventory" "${@:-tests/}" || exit $?
            cat "$inventory"
            rm -rf "$inventory_dir"
            ;;
        prepush-parity)
            inventory_dir=$(mktemp -d) || exit 1
            inventory="$inventory_dir/lane-inventory.json"
            test_lane_collect "$inventory" tests/ || exit $?
            test_lane_prepush_parity "$inventory"
            rm -rf "$inventory_dir"
            ;;
        marker)
            test_lane_marker "${2:?lane required}"
            ;;
        *)
            echo "Usage: $0 inventory [test path ...] | prepush-parity | marker <lane>" >&2
            exit 2
            ;;
    esac
fi
