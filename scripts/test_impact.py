#!/usr/bin/env python3
"""Report what an import-graph test selector would have chosen for a candidate.

This is measurement, not selection: the hosted runner records what a
static selector *would* run next to what actually ran, and whether any
failing module fell outside the selection. Nothing here changes which
tests execute. The report accumulates across runs in the retained lane
receipts, and that data decides whether selection is worth shipping.

The selector is deliberately simple and deterministic. It parses every
Python file under the candidate's ``api/``, ``config/``, ``scripts/``,
``tests/`` and ``mcp_server.py`` with ``ast`` -- never importing candidate
code -- and follows ``import`` / ``from ... import`` edges to files in the
tree. A test module is selected when its transitive import closure reaches
any changed ``.py`` file, or when it changed itself.

It fails closed to ``full`` (every module) when a changed path could affect
tests through something other than an import edge: the shared conftest or
any helper or fixture under ``tests/``, a dependency manifest, the
workflow, any verifier input, a file type the model does not know, or any
error while building the graph.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

PACKAGES = ("api", "config", "scripts", "tests")
TOP_LEVEL_MODULES = ("mcp_server.py",)
# Files whose change reaches tests through paths the import graph cannot see.
FULL_TRIGGERS = (
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"^tests/(?!test_[^/]*\.py$)"),  # fixtures, helpers, baselines, subpackages
    re.compile(r"(^|/)(requirements|constraints)[^/]*\.txt$"),
    re.compile(r"^\.github/"),
    re.compile(r"^(pyproject\.toml|pytest\.ini|setup\.cfg|tox\.ini)$"),
)
# Extensions the model knows to carry no import edge. A changed file of one
# of these kinds selects nothing by itself and is listed as unmodeled so a
# failure it caused shows up as a miss. Anything else fails closed.
INERT_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst", ".html", ".css", ".js", ".json", ".yml", ".yaml",
    ".sh", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".ico", ".toml", ".cfg",
    ".ini", ".conf", ".service", ".timer", ".plist",
})
PASSING_OUTCOMES = frozenset({"passed", "skipped"})


@dataclasses.dataclass(frozen=True)
class Report:
    mode: str  # "select" | "full"
    reason: str
    selected_modules: tuple[str, ...]
    total_modules: int
    selected_duration_share: float
    unmodeled_paths: tuple[str, ...]
    failing_modules: tuple[str, ...] = ()
    missed_failures: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _runner_inputs() -> tuple[str, ...]:
    from scripts.verify_candidate import RUNNER_INPUTS  # stdlib-only module
    return tuple(RUNNER_INPUTS)


def _module_files(root: Path) -> list[str]:
    files: list[str] = []
    for package in PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            files.append(path.relative_to(root).as_posix())
    for name in TOP_LEVEL_MODULES:
        if (root / name).is_file():
            files.append(name)
    return files


def _resolve(root: Path, dotted: str, known: frozenset[str]) -> str | None:
    parts = dotted.split(".")
    while parts:
        base = "/".join(parts)
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in known:
                return candidate
        parts.pop()
    return None


def build_graph(root: Path) -> dict[str, frozenset[str]]:
    """Map each Python file to the in-tree files it imports directly."""
    files = _module_files(root)
    known = frozenset(files)
    graph: dict[str, frozenset[str]] = {}
    for rel in files:
        try:
            tree = ast.parse((root / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError):
            graph[rel] = frozenset()
            continue
        edges: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _resolve(root, alias.name, known)
                    if target:
                        edges.add(target)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                target = _resolve(root, node.module, known)
                if target:
                    edges.add(target)
                for alias in node.names:
                    target = _resolve(root, f"{node.module}.{alias.name}", known)
                    if target:
                        edges.add(target)
        graph[rel] = frozenset(edges)
    return graph


def _is_test_module(rel: str) -> bool:
    return rel.startswith("tests/") and rel.count("/") == 1 and os.path.basename(rel).startswith("test_") and rel.endswith(".py")


def _reaching(graph: Mapping[str, frozenset[str]], changed: Iterable[str]) -> set[str]:
    importers: dict[str, set[str]] = {}
    for source, targets in graph.items():
        for target in targets:
            importers.setdefault(target, set()).add(source)
    seen: set[str] = set()
    stack = list(changed)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(importers.get(current, ()))
    return seen


def select(root: Path, changed: Sequence[str], durations: Mapping[str, float] | None = None) -> Report:
    """What a static selector would run for ``changed``; fails closed to ``full``."""
    files = [line.strip() for line in changed if line.strip()]
    try:
        graph = build_graph(root)
        test_modules = sorted(rel for rel in graph if _is_test_module(rel))
        total = len(test_modules)
        recorded = {k: float(v) for k, v in (durations or {}).items() if isinstance(v, (int, float))}
        total_seconds = sum(recorded.get(m, 0.0) for m in test_modules)

        def full(reason: str) -> Report:
            return Report("full", reason, tuple(test_modules), total, 1.0 if total else 0.0, ())

        if not files:
            return full("changed set unavailable")
        triggers = FULL_TRIGGERS + tuple(re.compile("^" + re.escape(p) + "$") for p in _runner_inputs())
        for path in files:
            for pattern in triggers:
                if pattern.search(path):
                    return full(f"{path} can affect tests outside the import graph")
        unmodeled: list[str] = []
        py_changed: list[str] = []
        for path in files:
            if path.endswith(".py"):
                py_changed.append(path)
            elif Path(path).suffix.lower() in INERT_EXTENSIONS:
                unmodeled.append(path)
            else:
                return full(f"{path} has a file type the selector does not model")
        reached = _reaching(graph, [p for p in py_changed if p in graph])
        selected = sorted({m for m in test_modules if m in reached} | {p for p in py_changed if _is_test_module(p)})
        share = (sum(recorded.get(m, 0.0) for m in selected) / total_seconds) if total_seconds else (len(selected) / total if total else 0.0)
        return Report("select", "import closure of the changed Python files", tuple(selected), total, round(share, 4), tuple(unmodeled))
    except Exception as exc:  # noqa: BLE001 - any failure is a full run
        return Report("full", f"selector error: {type(exc).__name__}: {exc}", (), 0, 1.0, ())


def failing_modules_from_receipts(receipt_dir: Path) -> tuple[str, ...]:
    """Module scopes with any non-passing call outcome in the lane receipts."""
    failing: set[str] = set()
    for receipt in sorted(receipt_dir.glob("*.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        reports = payload.get("reports") if isinstance(payload, Mapping) else None
        if not isinstance(reports, Mapping):
            continue
        for nodeid, outcome in reports.items():
            if outcome not in PASSING_OUTCOMES:
                failing.add(str(nodeid).split("::", 1)[0])
    return tuple(sorted(failing))


def with_outcomes(report: Report, failing: Sequence[str]) -> Report:
    selected = set(report.selected_modules)
    missed = tuple(m for m in failing if report.mode == "select" and m not in selected)
    return dataclasses.replace(report, failing_modules=tuple(failing), missed_failures=missed)


def markdown(report: Report) -> str:
    lines = [
        "### Test-impact selection (shadow; the gate ran every retained lane)",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| mode | `{report.mode}` |",
        f"| reason | {report.reason} |",
        f"| selected modules | {len(report.selected_modules)} of {report.total_modules} |",
        f"| selected duration share | {report.selected_duration_share:.0%} |",
        f"| unmodeled changed paths | {len(report.unmodeled_paths)} |",
        f"| failing modules in this part | {len(report.failing_modules)} |",
        f"| failing modules the selection would have missed | {', '.join(report.missed_failures) or 'none'} |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="the candidate tree (parsed, never imported)")
    parser.add_argument("--changed-files", required=True, type=Path, help="newline-separated changed paths")
    parser.add_argument("--durations", type=Path, default=None, help="scripts/lane_scope_durations.json from the runner")
    parser.add_argument("--receipts", type=Path, default=None, help="the run's --lane-log-dir, to attribute failures")
    parser.add_argument("--output", type=Path, default=None, help="where to write the JSON report")
    parser.add_argument("--summary", type=Path, default=None, help="a markdown file to append to (GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    try:
        changed = args.changed_files.read_text(encoding="utf-8").splitlines()
    except OSError:
        changed = []
    durations: Mapping[str, float] = {}
    if args.durations is not None:
        try:
            durations = json.loads(args.durations.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            durations = {}
    report = select(args.root, changed, durations)
    if args.receipts is not None and args.receipts.is_dir():
        report = with_outcomes(report, failing_modules_from_receipts(args.receipts))
    payload = json.dumps({"impact_selection": report.as_dict()}, indent=2, sort_keys=True) + "\n"
    print(payload, end="")
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(markdown(report))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
