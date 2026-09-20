"""A shadow verdict stands in for the required run only on an exact match."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.candidate_reuse import Reuse, reusable_shadow

REPO = Path(__file__).resolve().parent.parent
APP = 4891159
TREE = "a" * 40
RUNNER = "b" * 40
HEAD = "c" * 40


def _check(**overrides):
    output = {"candidate": HEAD, "tree": TREE, "trusted_runner": RUNNER, "mode": "executed", "lanes": ["fast-unit"]}
    output.update(overrides.pop("output", {}))
    check = {
        "id": 10, "name": "candidate-verification-shadow", "conclusion": "success",
        "app": {"id": APP}, "started_at": "2026-09-20T10:00:00Z",
        "output": {"title": "Candidate verification", "summary": "…", "text": json.dumps(output)},
    }
    check.update(overrides)
    return check


def _decide(checks, lanes=("fast-unit",), tree=TREE, runner=RUNNER, app=APP):
    return reusable_shadow(checks, tree=tree, trusted_runner=runner, app_id=app, required_lanes=lanes)


@pytest.mark.unit
def test_exact_match_is_reused():
    assert _decide([_check()]) == Reuse(10, HEAD)


@pytest.mark.unit
@pytest.mark.parametrize("overrides", [
    {"conclusion": "failure"},
    {"conclusion": None},
    {"name": "candidate-verification"},
    {"app": {"id": APP + 1}},
    {"app": None},
    {"output": {"trusted_runner": "d" * 40}},
    {"output": {"tree": "e" * 40}},
    {"output": {"mode": "docs-only"}},
    {"output": {"mode": "reused"}},
    {"output": {"lanes": []}},
    {"output": {"lanes": "fast-unit"}},
    {"output": {"candidate": None}},
    {"id": "10"},
], ids=lambda o: json.dumps(o))
def test_any_mismatch_is_not_reused(overrides):
    assert _decide([_check(**overrides)]) is None


@pytest.mark.unit
def test_shadow_lanes_must_cover_every_required_lane():
    assert _decide([_check()], lanes=("fast-unit", "browser-free")) is None
    both = _check(output={"lanes": ["fast-unit", "browser-free"]})
    assert _decide([both], lanes=("fast-unit",)) == Reuse(10, HEAD)


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "not json", "[]", '{"tree": 1}', None])
def test_unstructured_or_malformed_output_is_not_reused(text):
    check = _check()
    check["output"]["text"] = text
    assert _decide([check]) is None


@pytest.mark.unit
def test_a_summary_line_that_merely_mentions_the_runner_is_not_enough():
    check = _check()
    check["output"] = {"summary": f"Candidate {HEAD} verified by runner {RUNNER}: success"}
    assert _decide([check]) is None


@pytest.mark.unit
def test_empty_inputs_never_reuse():
    assert _decide([_check()], tree="") is None
    assert _decide([_check()], runner="") is None
    assert _decide([_check()], lanes=()) is None
    assert _decide([]) is None
    assert _decide([None, 3, "x"]) is None


@pytest.mark.unit
def test_newest_matching_verdict_wins():
    older = _check(id=5, started_at="2026-09-20T09:00:00Z", output={"candidate": "1" * 40})
    newer = _check(id=7, started_at="2026-09-20T11:00:00Z", output={"candidate": "2" * 40})
    assert _decide([newer, older, _check(id=9, conclusion="failure")]) == Reuse(7, "2" * 40)


def _cli(stdin: str, lanes="fast-unit"):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "candidate_reuse.py"), "--tree", TREE, "--trusted-runner", RUNNER,
         "--app-id", str(APP), "--lanes", lanes],
        input=stdin, capture_output=True, text=True,
    )


@pytest.mark.unit
def test_cli_prints_reuse_outputs_on_a_match():
    result = _cli(json.dumps({"check_runs": [_check()]}))
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["mode=reused", "reused_check_id=10", f"reused_candidate={HEAD}"]


@pytest.mark.unit
@pytest.mark.parametrize("stdin", ["", "garbage", "[]", json.dumps({"check_runs": [_check(conclusion="failure")]})])
def test_cli_fails_closed_to_an_empty_answer(stdin):
    result = _cli(stdin)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["reused_check_id="]
