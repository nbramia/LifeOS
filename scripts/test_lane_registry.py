"""Authoritative marker predicates for LifeOS test lanes."""
from __future__ import annotations

import sys


# Ordered by precedence. Both collection membership and pytest's execution
# selector are derived from these same required/excluded marker sets.
LANES = (
    {"name": "browser-server", "required": ("browser", "requires_server"), "excluded": (), "prerequisites": ("playwright", "server")},
    {"name": "browser-free", "required": ("browser",), "excluded": ("requires_server",), "prerequisites": ("playwright",)},
    {"name": "server", "required": ("requires_server",), "excluded": ("browser",), "prerequisites": ("server",)},
    {"name": "integration", "required": ("integration",), "excluded": ("browser", "requires_server"), "prerequisites": ()},
    {"name": "slow", "required": ("slow",), "excluded": ("browser", "requires_server", "integration"), "prerequisites": ("server",)},
    {"name": "fast-unit", "required": ("unit",), "excluded": ("browser", "requires_server", "integration", "slow"), "prerequisites": ()},
)
BY_NAME = {lane["name"]: lane for lane in LANES}
# Membership is browser-first; outer runners prioritize feedback separately.
EXECUTION_ORDER = ("fast-unit", "slow", "browser-free", "browser-server", "server", "integration")


def scope_of(nodeid: str) -> str:
    """The module a collected node ID belongs to.

    The partition unit shared by the lane plugin's duration receipt and the
    verifier's part assignment: everything before the first ``::``, so a
    module's tests -- classes included -- are one indivisible unit.
    """
    return nodeid.split("::", 1)[0]


def marker(lane: dict) -> str:
    return " and ".join((*lane["required"], *(f"not {name}" for name in lane["excluded"])))


def classify(marker_names: set[str]) -> str | None:
    """Return the first matching lane predicate, or None for an unknown test."""
    for lane in LANES:
        if set(lane["required"]).issubset(marker_names) and not set(lane["excluded"]) & marker_names:
            return lane["name"]
    return None


def _main(arguments: list[str]) -> int:
    if arguments == ["lanes"]:
        print("\n".join(BY_NAME))
        return 0
    if len(arguments) == 2 and arguments[0] == "marker":
        print(marker(BY_NAME[arguments[1]]))
        return 0
    if len(arguments) == 3 and arguments[0] == "requires":
        return 0 if arguments[2] in BY_NAME[arguments[1]]["prerequisites"] else 1
    print("Usage: test_lane_registry.py lanes | marker <lane> | requires <lane> <prerequisite>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
