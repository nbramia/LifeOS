"""One-pass, machine-readable pytest test-lane inventory."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.test_lane_registry import LANES, classify, marker, scope_of
from scripts.verification_evidence import PRIVACY_AUDIT_NODEID

_REPORTS: dict[str, str] = {}
_NOT_APPLICABLE_CANDIDATES: dict[str, str] = {}
_SCOPE_SECONDS: dict[str, float] = {}
_KEYBOARD_INTERRUPTED = False


def pytest_addoption(parser):
    group = parser.getgroup("lifeos-lanes")
    group.addoption(
        "--lifeos-lane-inventory",
        metavar="PATH",
        help="write a one-pass JSON test-lane inventory to a new external .json path",
    )
    group.addoption(
        "--lifeos-lane-execution",
        metavar="PATH",
        help="write external per-node execution outcomes for an exact lane dispatch",
    )
    group.addoption(
        "--lifeos-lane-nodeids",
        metavar="PATH",
        help="read exact node IDs from a private external file instead of argv",
    )


def _configure_output(config, option: str, attribute: str) -> None:
    destination = config.getoption(option)
    if not destination:
        return
    output = Path(destination).resolve()
    root = Path(config.rootpath).resolve()
    if output.suffix != ".json" or output.exists() or root in output.parents:
        raise pytest.UsageError(
            f"--{option.replace('_', '-')} must name a new .json file outside the repository"
        )
    setattr(config, attribute, output)


def pytest_configure(config):
    global _KEYBOARD_INTERRUPTED
    _configure_output(config, "lifeos_lane_inventory", "_lifeos_lane_inventory")
    _configure_output(config, "lifeos_lane_execution", "_lifeos_lane_execution")
    _REPORTS.clear()
    _NOT_APPLICABLE_CANDIDATES.clear()
    _SCOPE_SECONDS.clear()
    _KEYBOARD_INTERRUPTED = False


def _read_nodeids(config) -> frozenset[str] | None:
    destination = config.getoption("lifeos_lane_nodeids")
    if not destination:
        return None
    source = Path(destination).resolve()
    root = Path(config.rootpath).resolve()
    if source.suffix != ".txt" or root in source.parents or source.is_symlink() or not source.is_file():
        raise pytest.UsageError("--lifeos-lane-nodeids must name a regular .txt file outside the repository")
    try:
        nodeids = frozenset(line.rstrip("\n") for line in source.read_text(encoding="utf-8").splitlines())
    except OSError as exc:
        raise pytest.UsageError("--lifeos-lane-nodeids could not be read") from exc
    if not nodeids or "" in nodeids:
        raise pytest.UsageError("--lifeos-lane-nodeids must contain at least one node ID")
    return nodeids


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Select exact inventory IDs without putting an unbounded list in argv."""
    nodeids = _read_nodeids(config)
    if nodeids is None:
        return
    selected = [item for item in items if item.nodeid in nodeids]
    missing = nodeids - {item.nodeid for item in selected}
    if missing:
        raise pytest.UsageError("requested lane node IDs were not collected")
    deselected = [item for item in items if item.nodeid not in nodeids]
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def pytest_keyboard_interrupt(excinfo):
    # pytest uses ExitCode.INTERRUPTED for both an actual Ctrl-C and certain
    # collection failures (for example syntax errors). Preserve that
    # distinction in the machine-readable protocol.
    global _KEYBOARD_INTERRUPTED
    _KEYBOARD_INTERRUPTED = getattr(excinfo, "type", None) is KeyboardInterrupt


def pytest_runtest_logreport(report):
    # Every phase counts toward a module's cost, because a module's whole
    # setup/call/teardown time is what a part actually pays for taking it.
    # xdist forwards its workers' reports to the controller, so this sum is
    # the lane's total serial work regardless of the width it ran at.
    duration = getattr(report, "duration", None)
    if isinstance(duration, (int, float)):
        scope = scope_of(report.nodeid)
        _SCOPE_SECONDS[scope] = _SCOPE_SECONDS.get(scope, 0.0) + float(duration)
    if report.outcome == "skipped" and report.nodeid == PRIVACY_AUDIT_NODEID:
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
        _NOT_APPLICABLE_CANDIDATES[report.nodeid] = reason.removeprefix("Skipped: ")
    if report.when == "setup" and report.outcome == "skipped":
        # A setup fixture can terminally skip a selected test, in which case
        # pytest never emits its call report. Preserve that exact selection
        # outcome so the verifier can distinguish an intentional skip from a
        # missing report. A later call report remains authoritative.
        _REPORTS.setdefault(report.nodeid, "skipped")
    elif report.when == "call":
        _REPORTS[report.nodeid] = report.outcome


def _write_new_json(output: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix="lifeos-lanes-", suffix=".json", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def pytest_sessionfinish(session, exitstatus):
    # xdist workers receive the plugin too; only the controller owns external
    # receipts and receives their forwarded call reports.
    if hasattr(session.config, "workerinput"):
        return
    output = getattr(session.config, "_lifeos_lane_inventory", None)
    if output is not None:
        lanes = {
            lane["name"]: {
                "count": 0,
                "nodeids": [],
                "prerequisites": lane["prerequisites"],
                "marker": marker(lane),
            }
            for lane in LANES
        }
        unassigned = []
        items = []
        for item in session.items:
            markers = sorted(
                name for name in ("unit", "slow", "browser", "requires_server", "integration")
                if item.get_closest_marker(name) is not None
            )
            lane = classify(set(markers))
            items.append({"nodeid": item.nodeid, "lane": lane, "markers": markers})
            if lane is None:
                unassigned.append(item.nodeid)
            else:
                lanes[lane]["nodeids"].append(item.nodeid)
        for lane in lanes.values():
            lane["nodeids"].sort()
            lane["count"] = len(lane["nodeids"])
        items.sort(key=lambda item: item["nodeid"])
        if int(exitstatus) == int(pytest.ExitCode.OK):
            status = "empty" if not session.items else "ok"
        elif int(exitstatus) == int(pytest.ExitCode.NO_TESTS_COLLECTED):
            status = "empty"
        elif int(exitstatus) == int(pytest.ExitCode.INTERRUPTED) and _KEYBOARD_INTERRUPTED:
            status = "interrupted"
        else:
            status = "failed"
        payload = {
            "status": status,
            "lanes": lanes,
            "collected_count": len(session.items),
            "assigned_count": sum(lane["count"] for lane in lanes.values()),
            "items": items,
            "unassigned": unassigned,
            "intentional_exclusions": ["tests/archive (pytest norecursedirs)"],
        }
        _write_new_json(output, payload)
    execution_output = getattr(session.config, "_lifeos_lane_execution", None)
    if execution_output is not None:
        reports = _REPORTS
        status = "success" if int(exitstatus) == int(pytest.ExitCode.OK) else "failed"
        _write_new_json(execution_output, {
            "status": status,
            "reports": dict(sorted(reports.items())),
            "not_applicable_candidates": dict(sorted(_NOT_APPLICABLE_CANDIDATES.items())),
            "collected_count": len(session.items),
            # Measured cost per module scope, the unit the verifier assigns
            # to parts. Observational: it feeds balance, never a pass/fail
            # determination.
            "scope_durations": {
                scope: round(seconds, 3) for scope, seconds in sorted(_SCOPE_SECONDS.items())
            },
        })
