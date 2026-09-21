#!/usr/bin/env python3
"""Advisory Jev triage of a failed candidate-verification gate run.

Given a run id, this reads that run's retained lane-execution receipts and
lane logs, the candidate's diff, and the retained receipts of other recent
runs, then prints every failing node id with a bounded traceback excerpt.
When another retained run verified the identical tree successfully, a
failing test that passed there is marked "passing elsewhere on this tree" --
using the App-published check's structured output (`candidate`, `tree`,
`trusted_runner`, `mode`, `lanes`, `conclusion`; see the publisher step in
`.github/workflows/candidate-verification.yml`) to identify the tree and the
candidate's base commit.

With a configured `TYPESAFE_API_KEY`, each failing test is also given to Jev
for two typed judgments: `caused_by_candidate` (a probability) and
`failure_class` (a choice among `timing`/`ordering`/`environment`/`real`).
Nothing beyond a bounded traceback excerpt and the candidate's changed-file
list is ever sent to Jev -- never full test output, never file contents.

This command changes nothing. It never creates, updates, or resolves a
check run, issue, or Human-queue card, never reruns a lane, and never files
anything. It refuses to run outright when the candidate's diff touches
`data/` or `config/`, which could carry personal values. With no Jev key
configured, or when a judgment call fails or times out, it prints the
deterministic facts alone and still exits successfully -- only the
data/config refusal exits non-zero.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from api.services.jev_client import JevClient, JevError, jev_configured

logger = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]

_CHECK_NAMES = ("candidate-verification", "candidate-verification-shadow")
_PROTECTED_PREFIXES = ("data/", "config/")
_MAX_EXCERPT_CHARS = 4000

_FAILURE_CLASS_CRITERIA: dict[str, str] = {
    "timing": "A race or timing sensitivity: a sleep too short, an async wait, a scheduling race.",
    "ordering": "Depends on test execution order, or state leaking between tests sharing a worker.",
    "environment": "The test environment, not the code: a missing service, network flake, stale cache, platform difference.",
    "real": "A genuine defect the candidate's diff introduced.",
}

EXIT_OK = 0
EXIT_ERROR = 1


class GateTriageError(RuntimeError):
    """A step this command cannot recover from -- printed and exits non-zero."""


class GateTriageRefusal(GateTriageError):
    """The candidate's diff touches a path this command refuses to triage."""


def _run(run: Runner, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return run(list(args), **kwargs)


# ---------------------------------------------------------------------------
# GitHub reads
# ---------------------------------------------------------------------------

def resolve_repo(explicit: str | None, *, run: Runner = subprocess.run) -> str:
    if explicit:
        return explicit
    result = _run(run, ["gh", "repo", "view", "--json", "nameWithOwner"])
    if result.returncode != 0:
        raise GateTriageError(f"could not determine the repository: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)["nameWithOwner"]
    except (ValueError, KeyError) as exc:
        raise GateTriageError("could not parse `gh repo view` output") from exc


@dataclasses.dataclass(frozen=True)
class RunSummary:
    run_id: str
    head_sha: str
    workflow_name: str


def fetch_run_summary(repo: str, run_id: str, *, run: Runner = subprocess.run) -> RunSummary:
    result = _run(run, ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "headSha,workflowName"])
    if result.returncode != 0:
        raise GateTriageError(f"could not read run {run_id}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        head_sha = data["headSha"]
    except (ValueError, KeyError) as exc:
        raise GateTriageError(f"could not parse `gh run view` output for run {run_id}") from exc
    return RunSummary(run_id=str(run_id), head_sha=head_sha, workflow_name=data.get("workflowName", ""))


def fetch_check_output(repo: str, sha: str, *, run: Runner = subprocess.run) -> dict | None:
    """The newest App-published `candidate-verification*` structured record
    on `sha`, or None when none is found or the API call fails."""
    result = _run(run, ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    candidates: list[tuple[str, int, dict]] = []
    for check in payload.get("check_runs", []) if isinstance(payload, dict) else []:
        if not isinstance(check, dict) or check.get("name") not in _CHECK_NAMES:
            continue
        text = (check.get("output") or {}).get("text")
        if not isinstance(text, str):
            continue
        try:
            output = json.loads(text)
        except ValueError:
            continue
        if not isinstance(output, dict):
            continue
        candidates.append((str(check.get("started_at") or ""), int(check.get("id") or 0), output))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def changed_files(repo: str, base_sha: str, head_sha: str, *, run: Runner = subprocess.run) -> list[str]:
    # GitHub's compare API caps `files` at 300 entries per page; a triage
    # candidate's diff is expected to stay well under that, so this reads
    # only the first page rather than paginating.
    result = _run(run, ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}"])
    if result.returncode != 0:
        raise GateTriageError(f"could not diff {base_sha}...{head_sha}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise GateTriageError("could not parse `gh api compare` output") from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return []
    return [f["filename"] for f in files if isinstance(f, dict) and isinstance(f.get("filename"), str)]


def refusal_reason(files: Sequence[str]) -> str | None:
    hits = sorted(f for f in files if f.startswith(_PROTECTED_PREFIXES))
    if not hits:
        return None
    return (
        "refusing to triage: the candidate's diff touches "
        f"{', '.join(hits)}, under data/ or config/, which could carry personal values"
    )


def download_artifacts(repo: str, run_id: str, pattern: str, dest: Path, *, run: Runner = subprocess.run) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    result = _run(run, ["gh", "run", "download", str(run_id), "--repo", repo, "--pattern", pattern, "--dir", str(dest)])
    return result.returncode == 0


@dataclasses.dataclass(frozen=True)
class HistoryRun:
    run_id: str
    head_sha: str


def list_recent_runs(
    repo: str, workflow: str, *, exclude_run_id: str, limit: int, run: Runner = subprocess.run,
) -> list[HistoryRun]:
    if not workflow:
        return []
    result = _run(run, [
        "gh", "run", "list", "--repo", repo, "--workflow", workflow,
        "--limit", str(limit), "--json", "databaseId,headSha,status",
    ])
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    runs = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        run_id = str(item.get("databaseId") or "")
        head_sha = item.get("headSha")
        if not run_id or run_id == str(exclude_run_id) or not isinstance(head_sha, str) or not head_sha:
            continue
        runs.append(HistoryRun(run_id=run_id, head_sha=head_sha))
    return runs


def find_tree_matches(
    repo: str, tree: str, candidates: Sequence[HistoryRun], *, run: Runner = subprocess.run,
) -> list[HistoryRun]:
    matches = []
    for candidate in candidates:
        output = fetch_check_output(repo, candidate.head_sha, run=run)
        if output is None:
            continue
        if output.get("tree") == tree and output.get("conclusion") == "success":
            matches.append(candidate)
    return matches


def find_passing_elsewhere(
    repo: str, tree: str, failing: Sequence[str], *, run_id: str, workflow: str,
    history_limit: int, work_dir: Path, run: Runner = subprocess.run,
) -> dict[str, str]:
    """`{nodeid: other_run_id}` for failing tests that passed in another
    retained run that verified the identical tree successfully."""
    if not tree or not failing:
        return {}
    recent = list_recent_runs(repo, workflow, exclude_run_id=run_id, limit=history_limit, run=run)
    matches = find_tree_matches(repo, tree, recent, run=run)
    remaining = set(failing)
    found: dict[str, str] = {}
    for match in matches:
        if not remaining:
            break
        dest = work_dir / f"history-{match.run_id}"
        if not download_artifacts(repo, match.run_id, "lane-receipts-*", dest, run=run):
            continue
        reports = load_receipts(dest)
        for nodeid in list(remaining):
            if reports.get(nodeid) == "passed":
                found[nodeid] = match.run_id
                remaining.discard(nodeid)
    return found


# ---------------------------------------------------------------------------
# Receipts and lane logs (local parsing, no network)
# ---------------------------------------------------------------------------

def load_receipts(receipts_dir: Path) -> dict[str, str]:
    """Merge every downloaded `<lane>.json` receipt's `reports` map (node id
    -> outcome) into one dict. See `scripts/test_lane_plugin.py`."""
    reports: dict[str, str] = {}
    if not receipts_dir.exists():
        return reports
    for json_path in sorted(receipts_dir.rglob("*.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for nodeid, outcome in (payload.get("reports") or {}).items():
            if isinstance(nodeid, str) and isinstance(outcome, str):
                reports[nodeid] = outcome
    return reports


def failing_nodeids(reports: Mapping[str, str]) -> list[str]:
    return sorted(nodeid for nodeid, outcome in reports.items() if outcome == "failed")


def load_lane_log_text(logs_dir: Path) -> str:
    if not logs_dir.exists():
        return ""
    parts = []
    for log_path in sorted(logs_dir.rglob("*.log")):
        try:
            parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _failure_header_text(nodeid: str) -> str:
    """pytest's `--tb=short` FAILURES header uses the node id's test portion
    with `::` between a class and its method rendered as `.` -- e.g.
    `tests/test_foo.py::TestBar::test_baz` -> `TestBar.test_baz`. A plain
    function or a parametrized id (`test_baz[param]`) has no further `::`
    and is used as-is."""
    _, _, remainder = nodeid.partition("::")
    return remainder.replace("::", ".") if remainder else nodeid


def _is_failure_header(line: str, header: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("_") and stripped.endswith("_")):
        return False
    return stripped.strip("_ ") == header


def extract_traceback_excerpt(log_text: str, nodeid: str, *, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    """The bounded slice of `log_text` between this test's FAILURES header
    and the next header or `===`-delimited section, or "" when the log
    holds no such section (e.g. the failing lane's part never uploaded a
    log, or no log was retained because nothing in this run's parts
    failed)."""
    header = _failure_header_text(nodeid)
    if not header:
        return ""
    lines = log_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _is_failure_header(line, header):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        candidate = lines[j].strip()
        if candidate.startswith("_") and candidate.endswith("_") and len(candidate) > 1:
            end = j
            break
        if candidate.startswith("==="):
            end = j
            break
    excerpt = "\n".join(lines[start:end]).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip() + "\n... (truncated)"
    return excerpt


# ---------------------------------------------------------------------------
# Jev judgment
# ---------------------------------------------------------------------------

def _jev_questions() -> dict:
    return {
        "caused_by_candidate": {
            "type": "noul",
            "instructions": (
                "This test's failure was caused by the candidate's diff, "
                "not by flakiness, ordering, or the test environment."
            ),
        },
        "failure_class": {
            "type": "choice",
            "instructions": "Which of these best describes why this test failed?",
            "criteria": dict(_FAILURE_CLASS_CRITERIA),
        },
    }


def _valid_probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not (0.0 <= number <= 1.0):
        return None
    return number


@dataclasses.dataclass(frozen=True)
class JevVerdict:
    caused_by_candidate: float | None
    failure_class: str | None


def judge_failure(
    client: JevClient, nodeid: str, traceback_excerpt: str,
    changed: Sequence[str], passing_elsewhere: bool,
) -> JevVerdict:
    state = {
        "context": (
            "A candidate verification gate run failed. This is one of its "
            "failing tests, from this repository's test suite."
        ),
        "nodeid": nodeid,
        "traceback_excerpt": traceback_excerpt,
        "candidate_changed_files": list(changed),
        "passing_elsewhere_on_this_tree": passing_elsewhere,
    }
    answers = client.ask(state, _jev_questions())
    caused = _valid_probability((answers.get("caused_by_candidate") or {}).get("noul"))
    failure_class = (answers.get("failure_class") or {}).get("choice")
    if failure_class not in _FAILURE_CLASS_CRITERIA:
        failure_class = None
    return JevVerdict(caused_by_candidate=caused, failure_class=failure_class)


# ---------------------------------------------------------------------------
# Report assembly and rendering
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class TestReport:
    nodeid: str
    traceback_excerpt: str
    passing_elsewhere_run: str | None
    verdict: JevVerdict | None


def build_reports(
    failing: Sequence[str], log_text: str, passing_elsewhere: Mapping[str, str],
) -> list[TestReport]:
    return [
        TestReport(
            nodeid=nodeid,
            traceback_excerpt=extract_traceback_excerpt(log_text, nodeid),
            passing_elsewhere_run=passing_elsewhere.get(nodeid),
            verdict=None,
        )
        for nodeid in failing
    ]


def apply_jev(reports: Sequence[TestReport], changed: Sequence[str], *, jev_timeout: float) -> list[TestReport]:
    """Best-effort: a Jev failure or timeout for one test leaves that
    report's `verdict` at None (falls back to the deterministic facts
    alone) and never raises -- so the command always finishes and exits
    successfully whether or not Jev is configured or reachable."""
    if not jev_configured():
        return list(reports)
    client = JevClient(timeout=jev_timeout)
    updated = []
    for report in reports:
        verdict = None
        try:
            verdict = judge_failure(
                client, report.nodeid, report.traceback_excerpt, changed,
                report.passing_elsewhere_run is not None,
            )
        except JevError as exc:
            logger.warning("Jev triage judgment failed for %s: %s", report.nodeid, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - Jev must never fail triage
            logger.warning("Jev triage judgment failed unexpectedly for %s: %s", report.nodeid, type(exc).__name__)
        updated.append(dataclasses.replace(report, verdict=verdict))
    return updated


def render_report(run_id: str, head_sha: str, tree: str | None, reports: Sequence[TestReport]) -> str:
    lines = [f"Gate triage for run {run_id} (head {head_sha}" + (f", tree {tree}" if tree else "") + ")"]
    if not reports:
        lines.append("No failing tests found in the retained receipts.")
        return "\n".join(lines)

    def _rank(report: TestReport) -> tuple[float, str]:
        caused = report.verdict.caused_by_candidate if report.verdict else None
        return (-(caused if caused is not None else -1.0), report.nodeid)

    ranked = sorted(reports, key=_rank)
    lines.append(f"{len(reports)} failing test(s):")
    for report in ranked:
        lines.append("")
        lines.append(f"FAILED  {report.nodeid}")
        if report.passing_elsewhere_run:
            lines.append(f"  passing elsewhere on this tree: yes (run {report.passing_elsewhere_run})")
        else:
            lines.append("  passing elsewhere on this tree: no")
        if report.verdict is not None:
            caused = report.verdict.caused_by_candidate
            caused_text = f"{caused:.2f}" if caused is not None else "unavailable"
            lines.append(f"  caused_by_candidate: {caused_text}")
            lines.append(f"  failure_class: {report.verdict.failure_class or 'unavailable'}")
        else:
            lines.append("  Jev judgment: unavailable (no key configured, or the call failed)")
        lines.append("  traceback:")
        excerpt = report.traceback_excerpt or "(no traceback captured in the retained lane log)"
        for line in excerpt.splitlines():
            lines.append(f"    {line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_triage(args: argparse.Namespace, *, run: Runner = subprocess.run) -> tuple[int, str]:
    """Returns `(EXIT_OK, report_text)` on success. Raises `GateTriageError`
    (or its `GateTriageRefusal` subclass) for a data/config refusal or any
    other unrecoverable step -- `main()` prints that to stderr and exits
    non-zero."""
    repo = resolve_repo(args.repo, run=run)
    summary = fetch_run_summary(repo, args.run_id, run=run)
    check_output = fetch_check_output(repo, summary.head_sha, run=run)
    base_sha = check_output.get("trusted_runner") if check_output else None
    if not isinstance(base_sha, str) or not base_sha:
        raise GateTriageError(
            f"no App-published verification check found on {summary.head_sha}; cannot "
            "establish the candidate's base commit to diff against, so refusing to run "
            "rather than skip the data/config safety check"
        )
    tree = check_output.get("tree") if isinstance(check_output.get("tree"), str) else None

    files = changed_files(repo, base_sha, summary.head_sha, run=run)
    reason = refusal_reason(files)
    if reason:
        raise GateTriageRefusal(reason)

    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="gate-triage-"))
    receipts_dir = work_dir / "receipts"
    logs_dir = work_dir / "logs"

    if not download_artifacts(repo, args.run_id, "lane-receipts-*", receipts_dir, run=run):
        raise GateTriageError(f"could not download lane-receipts artifacts for run {args.run_id}")
    download_artifacts(repo, args.run_id, "lane-logs-*", logs_dir, run=run)  # best-effort; failure-only artifact

    reports_map = load_receipts(receipts_dir)
    failing = failing_nodeids(reports_map)
    log_text = load_lane_log_text(logs_dir)

    passing_elsewhere: dict[str, str] = {}
    if tree:
        passing_elsewhere = find_passing_elsewhere(
            repo, tree, failing, run_id=args.run_id, workflow=summary.workflow_name,
            history_limit=args.history_limit, work_dir=work_dir, run=run,
        )

    reports = build_reports(failing, log_text, passing_elsewhere)
    reports = apply_jev(reports, files, jev_timeout=args.jev_timeout)

    return EXIT_OK, render_report(args.run_id, summary.head_sha, tree, reports)


def main(argv: Sequence[str] | None = None, *, run: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="the failed candidate-verification run id")
    parser.add_argument("--repo", help="owner/repo; defaults to `gh repo view`'s current repository")
    parser.add_argument("--work-dir", type=Path, help="defaults to a fresh temp directory")
    parser.add_argument("--history-limit", type=int, default=20, help="recent runs to scan for a matching tree")
    parser.add_argument("--jev-timeout", type=float, default=30.0, help="per-test Jev call timeout, in seconds")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    try:
        exit_code, output = run_triage(args, run=run)
    except GateTriageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    print(output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
