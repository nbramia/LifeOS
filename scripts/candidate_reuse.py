#!/usr/bin/env python3
"""Decide whether a passing shadow verification already covers a candidate.

The required ``candidate-verification`` run verifies the publisher's
normalized candidate commit, whose tree is byte-for-byte the pull request
head's tree whenever the branch is already up to date with the base. The
shadow run on that head, by the same trusted runner, has then already
executed the same lanes over the same bytes. This module decides -- from
the check runs the dedicated App published on the head, never from
candidate-authored text -- whether one of those shadow verdicts can stand
in for executing the lanes again.

A verdict is reusable only when every one of these holds:

* it is a ``candidate-verification-shadow`` check run with conclusion
  ``success`` published by the dedicated App (its ``app.id`` matches);
* its structured output (the JSON object the publisher writes into
  ``output.text``) names the same ``trusted_runner`` commit, so the same
  workflow, verifier, lane registry, and selection rules produced it;
* that output's ``tree`` equals the tree git computed for the head, and
  the caller already established that the candidate's tree equals it;
* its ``mode`` is ``executed`` and its ``lanes`` cover every lane the
  candidate's own selection requires.

Anything else -- a missing field, malformed JSON, an unknown App, a
different runner -- is not reusable, and the run executes the lanes as it
otherwise would. The decision never widens what counts as verified; it
only recognises verification that already happened.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any, Iterable, Mapping, Sequence

SHADOW_CHECK_NAME = "candidate-verification-shadow"


@dataclasses.dataclass(frozen=True)
class Reuse:
    check_id: int
    candidate: str


def _structured_output(check: Mapping[str, Any]) -> Mapping[str, Any] | None:
    text = (check.get("output") or {}).get("text")
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def reusable_shadow(
    check_runs: Iterable[Mapping[str, Any]],
    *,
    tree: str,
    trusted_runner: str,
    app_id: int,
    required_lanes: Sequence[str],
) -> Reuse | None:
    """The newest reusable shadow verdict among ``check_runs``, or ``None``."""
    if not tree or not trusted_runner or not required_lanes:
        return None
    candidates: list[tuple[tuple[str, int], Reuse]] = []
    for check in check_runs:
        if not isinstance(check, Mapping):
            continue
        if check.get("name") != SHADOW_CHECK_NAME or check.get("conclusion") != "success":
            continue
        if (check.get("app") or {}).get("id") != app_id:
            continue
        output = _structured_output(check)
        if output is None:
            continue
        if output.get("trusted_runner") != trusted_runner or output.get("tree") != tree:
            continue
        if output.get("mode") != "executed":
            continue
        lanes = output.get("lanes")
        if not isinstance(lanes, list) or not set(required_lanes) <= {lane for lane in lanes if isinstance(lane, str)}:
            continue
        candidate = output.get("candidate")
        check_id = check.get("id")
        if not isinstance(candidate, str) or not isinstance(check_id, int):
            continue
        recency = (str(check.get("started_at") or ""), check_id)
        candidates.append((recency, Reuse(check_id, candidate)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", required=True, help="the head tree git computed; must already equal the candidate's")
    parser.add_argument("--trusted-runner", required=True)
    parser.add_argument("--app-id", required=True, type=int)
    parser.add_argument("--lanes", required=True, help="comma-separated lanes the candidate's own selection requires")
    args = parser.parse_args(argv)
    # Fail closed on any input problem: an empty answer executes the lanes.
    try:
        payload = json.load(sys.stdin)
        check_runs = payload.get("check_runs", []) if isinstance(payload, Mapping) else []
        reuse = reusable_shadow(
            check_runs, tree=args.tree, trusted_runner=args.trusted_runner, app_id=args.app_id,
            required_lanes=tuple(filter(None, args.lanes.split(","))),
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "not reusable"
        print(f"reuse decision unavailable: {exc}", file=sys.stderr)
        reuse = None
    if reuse is None:
        print("reused_check_id=")
        return 0
    print("mode=reused")
    print(f"reused_check_id={reuse.check_id}")
    print(f"reused_candidate={reuse.candidate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
