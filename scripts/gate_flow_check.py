#!/usr/bin/env python3
"""Check that every candidate-verification run the gate started also finished
with a published check: "jobs in == jobs out".

The hosted gate can stall in ways that look like slowness -- a capacity
deadlock, a runner that never picks up a queued job, a publisher job that
never reports. This check reads the recent runs of the verification
workflow and the check runs on each run's commit, and reports two kinds
of imbalance:

* **stalled** -- a run still queued or in progress past the ceiling;
* **unpublished** -- a completed run whose commit carries no check of the
  expected name published by the dedicated App (``candidate-verification``
  for a dispatched candidate, ``candidate-verification-shadow`` for a pull
  request head).

Cancelled runs are expected (a newer push or candidate supersedes the one
in flight) and are not counted. The check changes nothing: it prints a
report, exits non-zero on an imbalance, and can optionally deliver the
report through the operator's Telegram or alert email -- only when there
is an imbalance to report.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

WORKFLOW_FILE = "candidate-verification.yml"
CHECK_NAME_BY_EVENT = {
    "workflow_dispatch": "candidate-verification",
    "pull_request_target": "candidate-verification-shadow",
}
Runner = Callable[..., subprocess.CompletedProcess]


@dataclasses.dataclass(frozen=True)
class Imbalance:
    kind: str  # "stalled" | "unpublished"
    run_id: int
    event: str
    head_sha: str
    started_at: str
    detail: str
    url: str


@dataclasses.dataclass(frozen=True)
class FlowReport:
    window_hours: float
    ceiling_minutes: float
    counted: int
    cancelled: int
    imbalances: tuple[Imbalance, ...]

    @property
    def balanced(self) -> bool:
        return not self.imbalances


class FlowCheckError(RuntimeError):
    pass


def _api(run: Runner, path: str) -> Any:
    result = run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise FlowCheckError(f"gh api {path} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise FlowCheckError(f"gh api {path} returned malformed JSON") from exc


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _published(run: Runner, repository: str, head_sha: str, check_name: str, app_id: int) -> bool:
    payload = _api(run, f"repos/{repository}/commits/{head_sha}/check-runs?per_page=100")
    for check in payload.get("check_runs", []) if isinstance(payload, Mapping) else []:
        if not isinstance(check, Mapping):
            continue
        if check.get("name") != check_name or check.get("status") != "completed":
            continue
        if (check.get("app") or {}).get("id") != app_id:
            continue
        return True
    return False


PAGE_SIZE = 100
MAX_PAGES = 20


def _runs_since(run: Runner, repository: str, since: str) -> list[Mapping[str, Any]]:
    """Every run created at or after ``since``, following pages until one
    comes back short; a window that would need more than MAX_PAGES pages is
    an error rather than a silently truncated report."""
    runs: list[Mapping[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = _api(run, f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs?per_page={PAGE_SIZE}&page={page}&created=%3E%3D{since}")
        batch = payload.get("workflow_runs", []) if isinstance(payload, Mapping) else []
        runs.extend(item for item in batch if isinstance(item, Mapping))
        if len(batch) < PAGE_SIZE:
            return runs
    raise FlowCheckError(f"more than {MAX_PAGES * PAGE_SIZE} runs in the window; narrow --hours")


def collect(
    repository: str,
    *,
    app_id: int,
    hours: float = 24.0,
    ceiling_minutes: float = 25.0,
    now: Optional[datetime] = None,
    run: Runner = subprocess.run,
) -> FlowReport:
    """Every imbalance among the workflow's runs created in the last ``hours``."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = _runs_since(run, repository, since)
    imbalances: list[Imbalance] = []
    counted = cancelled = 0
    for item in runs:
        if not isinstance(item, Mapping):
            continue
        event = str(item.get("event") or "")
        check_name = CHECK_NAME_BY_EVENT.get(event)
        if check_name is None:
            continue
        run_id = int(item.get("id") or 0)
        head_sha = str(item.get("head_sha") or "")
        started = str(item.get("created_at") or "")
        url = str(item.get("html_url") or "")
        status = item.get("status")
        conclusion = item.get("conclusion")
        if status == "completed" and conclusion == "cancelled":
            cancelled += 1
            continue
        counted += 1
        if status in ("queued", "in_progress", "waiting", "pending", "requested"):
            age = (now - _parse(started)).total_seconds() / 60 if started else float("inf")
            if age > ceiling_minutes:
                imbalances.append(Imbalance("stalled", run_id, event, head_sha, started, f"{status} for {age:.0f} min, ceiling {ceiling_minutes:.0f}", url))
            continue
        if status == "completed" and head_sha and not _published(run, repository, head_sha, check_name, app_id):
            imbalances.append(Imbalance("unpublished", run_id, event, head_sha, started, f"completed ({conclusion}) but no App-published {check_name} check on its commit", url))
    return FlowReport(hours, ceiling_minutes, counted, cancelled, tuple(imbalances))


def render(report: FlowReport) -> str:
    lines = [
        f"candidate-verification flow check: last {report.window_hours:g} h, ceiling {report.ceiling_minutes:g} min",
        f"runs counted: {report.counted} (plus {report.cancelled} cancelled, expected)",
    ]
    if report.balanced:
        lines.append("balanced: every counted run is within the ceiling or has its published check")
        return "\n".join(lines) + "\n"
    lines.append(f"IMBALANCE: {len(report.imbalances)} run(s)")
    for i in report.imbalances:
        lines.append(f"- {i.kind}: run {i.run_id} ({i.event}, {i.head_sha[:12]}, started {i.started_at}) — {i.detail}\n  {i.url}")
    return "\n".join(lines) + "\n"


def _notify(channel: str, text: str) -> bool:
    if channel == "telegram":
        from api.services.telegram import send_message
        return bool(send_message(text))
    if channel == "email":
        from api.services.notifications import send_alert
        return bool(send_alert("candidate-verification flow imbalance", text))
    raise FlowCheckError(f"unknown notify channel: {channel}")


def main(argv: Optional[Sequence[str]] = None, *, run: Runner = subprocess.run, notify: Callable[[str, str], bool] = _notify) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", default="nbramia/LifeOS", help="owner/name")
    parser.add_argument("--trusted-app-id", type=int, default=4891159, help="the dedicated check-issuer App id")
    parser.add_argument("--hours", type=float, default=24.0, help="how far back to look")
    parser.add_argument("--ceiling-minutes", type=float, default=25.0, help="a run still queued or in progress past this is stalled")
    parser.add_argument("--notify", choices=("telegram", "email"), default=None, help="deliver the report there, only on an imbalance")
    args = parser.parse_args(argv)
    try:
        report = collect(args.repository, app_id=args.trusted_app_id, hours=args.hours, ceiling_minutes=args.ceiling_minutes, run=run)
    except FlowCheckError as exc:
        print(f"flow check could not run: {exc}", file=sys.stderr)
        return 1
    text = render(report)
    print(text, end="")
    if report.balanced:
        return 0
    if args.notify:
        delivered = notify(args.notify, text)
        print(f"notified via {args.notify}: {'sent' if delivered else 'not sent'}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
