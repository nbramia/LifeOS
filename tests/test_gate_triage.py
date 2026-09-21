"""Tests for scripts/gate_triage.py -- advisory Jev triage of a failed
candidate-verification gate run.

Every `gh` call is stubbed via an injected `Runner` (see
`scripts/candidate_publisher.py`'s own `Runner` type) -- no test calls the
real `gh` CLI or the real TypeSafe API.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.gate_triage as gt
from api.services.jev_client import JevClient, JevError

pytestmark = pytest.mark.unit

REPO = "acme/widgets"
HEAD_SHA = "1111111111111111111111111111111111abcd"
BASE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MAIN_RUN_ID = "1001"


def _args(**overrides) -> argparse.Namespace:
    base = dict(run_id=MAIN_RUN_ID, repo=REPO, work_dir=None, history_limit=10, jev_timeout=5.0)
    base.update(overrides)
    return argparse.Namespace(**base)


def _check_runs_response(name: str, output: dict, *, started_at: str = "2026-01-01T00:00:00Z", check_id: int = 1) -> str:
    return json.dumps({
        "check_runs": [{
            "name": name, "id": check_id, "started_at": started_at,
            "output": {"text": json.dumps(output)},
        }],
    })


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_refusal_reason_flags_data_and_config_paths():
    reason = gt.refusal_reason(["api/main.py", "data/vault_index.sqlite", "config/people_dictionary.json"])
    assert reason is not None
    assert "data/vault_index.sqlite" in reason
    assert "config/people_dictionary.json" in reason
    assert "api/main.py" not in reason


def test_refusal_reason_none_for_clean_diff():
    assert gt.refusal_reason(["api/main.py", "tests/test_main.py", "configuration/notes.md"]) is None


def test_failure_header_text_plain_function():
    assert gt._failure_header_text("tests/test_foo.py::test_bar") == "test_bar"


def test_failure_header_text_class_method():
    assert gt._failure_header_text("tests/test_foo.py::TestBar::test_baz") == "TestBar.test_baz"


def test_failure_header_text_parametrized():
    assert gt._failure_header_text("tests/test_foo.py::test_bar[case-1]") == "test_bar[case-1]"


_LOG_TEXT = """\
============================= test session starts ==============================
FF..
=================================== FAILURES ===================================
_____________________________ test_bar ______________________________
    def test_bar():
>       assert 1 == 2
E       assert 1 == 2

tests/test_foo.py:10: AssertionError
_______________________________ TestX.test_qux _________________________________
    def test_qux(self):
>       raise ValueError("boom")
E       ValueError: boom

tests/test_foo.py:20: ValueError
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_bar - assert 1 == 2
FAILED tests/test_foo.py::TestX::test_qux - ValueError: boom
======================== 2 failed, 1 passed in 0.12s ========================
"""


def test_extract_traceback_excerpt_bounds_to_the_matching_section():
    excerpt = gt.extract_traceback_excerpt(_LOG_TEXT, "tests/test_foo.py::test_bar")
    assert "assert 1 == 2" in excerpt
    assert "test_bar" in excerpt
    # Must not bleed into the next test's failure section.
    assert "boom" not in excerpt
    assert "TestX" not in excerpt


def test_extract_traceback_excerpt_matches_class_method_exactly():
    """Mutation-check witness for the exact-match header comparison: a
    substring match (e.g. 'test_qux' alone) would also match a hypothetical
    'test_qux_extra' header -- this asserts the excerpt starts exactly at
    the TestX.test_qux header, not an unrelated one."""
    excerpt = gt.extract_traceback_excerpt(_LOG_TEXT, "tests/test_foo.py::TestX::test_qux")
    assert "boom" in excerpt
    assert "assert 1 == 2" not in excerpt


def test_extract_traceback_excerpt_missing_returns_empty():
    assert gt.extract_traceback_excerpt(_LOG_TEXT, "tests/test_other.py::test_missing") == ""


def test_extract_traceback_excerpt_truncates_when_too_long():
    long_body = "\n".join(f"line {i}" for i in range(500))
    log = f"_____ test_bar _____\n{long_body}\n=== short test summary info ===\n"
    excerpt = gt.extract_traceback_excerpt(log, "tests/test_foo.py::test_bar", max_chars=100)
    assert len(excerpt) <= 100 + len("\n... (truncated)")
    assert excerpt.endswith("... (truncated)")


def test_failing_nodeids_filters_only_failed_outcome():
    reports = {"tests/a.py::test_1": "passed", "tests/a.py::test_2": "failed", "tests/a.py::test_3": "skipped"}
    assert gt.failing_nodeids(reports) == ["tests/a.py::test_2"]


def test_load_receipts_merges_multiple_json_files(tmp_path):
    part0 = tmp_path / "lane-receipts-abc-part0"
    part0.mkdir()
    (part0 / "fast-unit.json").write_text(json.dumps({"reports": {"tests/a.py::test_1": "failed"}}))
    part1 = tmp_path / "lane-receipts-abc-part1"
    part1.mkdir()
    (part1 / "browser-free.json").write_text(json.dumps({"reports": {"tests/b.py::test_2": "passed"}}))
    reports = gt.load_receipts(tmp_path)
    assert reports == {"tests/a.py::test_1": "failed", "tests/b.py::test_2": "passed"}


def test_load_receipts_missing_directory_returns_empty(tmp_path):
    assert gt.load_receipts(tmp_path / "does-not-exist") == {}


def test_render_report_ranks_by_caused_by_candidate_descending():
    low = gt.TestReport("tests/a.py::test_low", "tb", None, gt.JevVerdict(0.1, "environment"))
    high = gt.TestReport("tests/a.py::test_high", "tb", None, gt.JevVerdict(0.9, "real"))
    unknown = gt.TestReport("tests/a.py::test_unknown", "tb", None, None)
    output = gt.render_report(MAIN_RUN_ID, HEAD_SHA, "tree1", [low, high, unknown])
    high_pos = output.index("test_high")
    low_pos = output.index("test_low")
    unknown_pos = output.index("test_unknown")
    assert high_pos < low_pos < unknown_pos


def test_render_report_no_failing_tests():
    output = gt.render_report(MAIN_RUN_ID, HEAD_SHA, None, [])
    assert "No failing tests" in output


# ---------------------------------------------------------------------------
# Jev judgment (mutation-check witnesses for the two typed questions)
# ---------------------------------------------------------------------------

def test_judge_failure_asks_caused_by_candidate_and_failure_class(monkeypatch):
    captured = {}

    def fake_ask(self, state, questions, *, model=None):
        captured["state"] = state
        captured["questions"] = questions
        return {"caused_by_candidate": {"noul": 0.73}, "failure_class": {"choice": "ordering"}}

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = JevClient(api_key="test-key")
    verdict = gt.judge_failure(client, "tests/test_x.py::test_y", "some traceback", ["a.py"], True)

    assert verdict.caused_by_candidate == pytest.approx(0.73)
    assert verdict.failure_class == "ordering"

    questions = captured["questions"]
    assert set(questions) == {"caused_by_candidate", "failure_class"}
    assert questions["caused_by_candidate"]["type"] == "noul"
    assert questions["failure_class"]["type"] == "choice"
    assert set(questions["failure_class"]["criteria"]) == {"timing", "ordering", "environment", "real"}

    state = captured["state"]
    assert state["nodeid"] == "tests/test_x.py::test_y"
    assert state["traceback_excerpt"] == "some traceback"
    assert state["candidate_changed_files"] == ["a.py"]
    assert state["passing_elsewhere_on_this_tree"] is True


def test_judge_failure_rejects_unknown_failure_class(monkeypatch):
    def fake_ask(self, state, questions, *, model=None):
        return {"caused_by_candidate": {"noul": 0.5}, "failure_class": {"choice": "not-a-real-class"}}

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    verdict = gt.judge_failure(JevClient(api_key="test-key"), "tests/x.py::test_y", "tb", [], False)
    assert verdict.failure_class is None


def test_valid_probability_rejects_bool_and_out_of_range():
    """Mutation-check witness: `bool` is an `int` subclass, so
    `float(True) == 1.0` would silently pass without the explicit guard."""
    assert gt._valid_probability(True) is None
    assert gt._valid_probability(False) is None
    assert gt._valid_probability(1.5) is None
    assert gt._valid_probability(-0.1) is None
    assert gt._valid_probability(float("nan")) is None
    assert gt._valid_probability(0.42) == pytest.approx(0.42)


def test_apply_jev_skips_entirely_when_not_configured(monkeypatch):
    monkeypatch.setattr(gt, "jev_configured", lambda: False)

    def boom(self, *a, **kw):
        raise AssertionError("must not construct JevClient when not configured")

    monkeypatch.setattr(JevClient, "__init__", boom)
    report = gt.TestReport("tests/a.py::test_1", "tb", None, None)
    result = gt.apply_jev([report], [], jev_timeout=5.0)
    assert result[0].verdict is None


def test_apply_jev_falls_back_on_jev_error(monkeypatch):
    monkeypatch.setattr(gt, "jev_configured", lambda: True)

    def fake_ask(self, state, questions, *, model=None):
        raise JevError("boom")

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    report = gt.TestReport("tests/a.py::test_1", "tb", None, None)
    result = gt.apply_jev([report], [], jev_timeout=5.0)
    assert result[0].verdict is None


def test_apply_jev_falls_back_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(gt, "jev_configured", lambda: True)

    def fake_ask(self, state, questions, *, model=None):
        raise RuntimeError("something else broke")

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    report = gt.TestReport("tests/a.py::test_1", "tb", None, None)
    result = gt.apply_jev([report], [], jev_timeout=5.0)
    assert result[0].verdict is None


# ---------------------------------------------------------------------------
# End-to-end run_triage() -- refusal, keyless fallback, and the full path
# ---------------------------------------------------------------------------

def test_run_triage_refuses_when_diff_touches_data_or_config(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "run", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"headSha": HEAD_SHA, "workflowName": "Candidate verification"}), stderr="")
        if args[:2] == ["gh", "api"] and "check-runs" in args[2]:
            output = {"candidate": HEAD_SHA, "tree": "tree1", "trusted_runner": BASE_SHA, "mode": "executed", "lanes": ["fast-unit"], "conclusion": "failure"}
            return SimpleNamespace(returncode=0, stdout=_check_runs_response("candidate-verification", output), stderr="")
        if args[:2] == ["gh", "api"] and "compare" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"files": [{"filename": "config/people_dictionary.json"}]}), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    with pytest.raises(gt.GateTriageRefusal) as exc_info:
        gt.run_triage(_args(work_dir=tmp_path / "work"), run=fake_run)
    assert "config/people_dictionary.json" in str(exc_info.value)
    assert not any(c[:3] == ["gh", "run", "download"] for c in calls), "must never download artifacts on refusal"


def test_run_triage_refuses_when_no_verification_check_is_published(tmp_path):
    """Fail closed: with no App-published check to establish the base
    commit, the diff safety check cannot run, so triage refuses rather than
    skip it."""
    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "run", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"headSha": HEAD_SHA, "workflowName": "Candidate verification"}), stderr="")
        if args[:2] == ["gh", "api"] and "check-runs" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"check_runs": []}), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    with pytest.raises(gt.GateTriageError):
        gt.run_triage(_args(work_dir=tmp_path / "work"), run=fake_run)


def _base_fake_run(calls, *, files, extra=None):
    extra = extra or {}

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "run", "view"] and args[3] == MAIN_RUN_ID:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"headSha": HEAD_SHA, "workflowName": "Candidate verification"}), stderr="")
        if args[:2] == ["gh", "api"] and f"commits/{HEAD_SHA}/check-runs" in args[2]:
            output = extra.get("head_check_output") or {
                "candidate": HEAD_SHA, "tree": None, "trusted_runner": BASE_SHA,
                "mode": "executed", "lanes": ["fast-unit"], "conclusion": "failure",
            }
            return SimpleNamespace(returncode=0, stdout=_check_runs_response("candidate-verification", output), stderr="")
        if args[:2] == ["gh", "api"] and "compare" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"files": [{"filename": f} for f in files]}), stderr="")
        if args[:3] == ["gh", "run", "download"]:
            handler = extra.get("download")
            if handler is not None:
                return handler(args)
            raise AssertionError(f"unexpected download call: {args}")
        if args[:3] == ["gh", "run", "list"]:
            handler = extra.get("run_list")
            if handler is not None:
                return handler(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    return fake_run


def test_run_triage_prints_deterministic_facts_when_jev_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(gt, "jev_configured", lambda: False)
    calls = []

    def download(args):
        dest = Path(args[args.index("--dir") + 1])
        pattern = args[args.index("--pattern") + 1]
        dest.mkdir(parents=True, exist_ok=True)
        if pattern == "lane-receipts-*":
            part = dest / f"lane-receipts-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.json").write_text(json.dumps({
                "reports": {"tests/test_foo.py::test_bar": "failed", "tests/test_foo.py::test_baz": "passed"},
            }))
        elif pattern == "lane-logs-*":
            part = dest / f"lane-logs-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.log").write_text(_LOG_TEXT)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    fake_run = _base_fake_run(calls, files=["api/main.py"], extra={"download": download})

    exit_code, output = gt.run_triage(_args(work_dir=tmp_path / "work"), run=fake_run)

    assert exit_code == gt.EXIT_OK
    assert "tests/test_foo.py::test_bar" in output
    assert "tests/test_foo.py::test_baz" not in output  # only the failing test is reported
    assert "Jev judgment: unavailable" in output
    assert "passing elsewhere on this tree: no" in output


def test_run_triage_marks_passing_elsewhere_and_asks_jev(tmp_path, monkeypatch):
    monkeypatch.setattr(gt, "jev_configured", lambda: True)
    jev_calls = []

    def fake_ask(self, state, questions, *, model=None):
        jev_calls.append((state, questions))
        return {"caused_by_candidate": {"noul": 0.87}, "failure_class": {"choice": "real"}}

    monkeypatch.setattr(JevClient, "ask", fake_ask)

    history_run_id = "2002"
    history_head_sha = "2222222222222222222222222222222222222b"

    def download(args):
        run_id = args[3]
        dest = Path(args[args.index("--dir") + 1])
        pattern = args[args.index("--pattern") + 1]
        dest.mkdir(parents=True, exist_ok=True)
        if run_id == MAIN_RUN_ID and pattern == "lane-receipts-*":
            part = dest / f"lane-receipts-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.json").write_text(json.dumps({
                "reports": {"tests/test_foo.py::test_bar": "failed"},
            }))
        elif run_id == MAIN_RUN_ID and pattern == "lane-logs-*":
            part = dest / f"lane-logs-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.log").write_text(_LOG_TEXT)
        elif run_id == history_run_id and pattern == "lane-receipts-*":
            part = dest / f"lane-receipts-{history_head_sha}-part0"
            part.mkdir()
            (part / "fast-unit.json").write_text(json.dumps({
                "reports": {"tests/test_foo.py::test_bar": "passed"},
            }))
        else:
            raise AssertionError(f"unexpected download for run {run_id} pattern {pattern}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run_list(args):
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"databaseId": int(history_run_id), "headSha": history_head_sha, "status": "completed"},
            {"databaseId": int(MAIN_RUN_ID), "headSha": HEAD_SHA, "status": "completed"},
        ]), stderr="")

    calls = []
    head_check_output = {
        "candidate": HEAD_SHA, "tree": "tree-xyz", "trusted_runner": BASE_SHA,
        "mode": "executed", "lanes": ["fast-unit"], "conclusion": "failure",
    }
    fake_run = _base_fake_run(
        calls, files=["api/main.py"],
        extra={"head_check_output": head_check_output, "download": download, "run_list": run_list},
    )

    def fake_run_with_history_check(args, **kwargs):
        if args[:2] == ["gh", "api"] and f"commits/{history_head_sha}/check-runs" in args[2]:
            output = {
                "candidate": history_head_sha, "tree": "tree-xyz", "trusted_runner": BASE_SHA,
                "mode": "executed", "lanes": ["fast-unit"], "conclusion": "success",
            }
            return SimpleNamespace(returncode=0, stdout=_check_runs_response("candidate-verification", output), stderr="")
        return fake_run(args, **kwargs)

    exit_code, output = gt.run_triage(_args(work_dir=tmp_path / "work"), run=fake_run_with_history_check)

    assert exit_code == gt.EXIT_OK
    assert "passing elsewhere on this tree: yes (run 2002)" in output
    assert "caused_by_candidate: 0.87" in output
    assert "failure_class: real" in output

    assert len(jev_calls) == 1
    state, questions = jev_calls[0]
    assert state["nodeid"] == "tests/test_foo.py::test_bar"
    assert state["passing_elsewhere_on_this_tree"] is True
    assert "assert 1 == 2" in state["traceback_excerpt"]
    assert state["candidate_changed_files"] == ["api/main.py"]
    assert set(questions) == {"caused_by_candidate", "failure_class"}


def test_run_triage_never_calls_check_run_issue_or_human_queue_apis(tmp_path, monkeypatch):
    """Mutation-check witness for the 'changes nothing' invariant: scan
    every gh invocation this run made and assert none writes a check run,
    issue, or the Human-queue."""
    monkeypatch.setattr(gt, "jev_configured", lambda: False)
    calls = []

    def download(args):
        dest = Path(args[args.index("--dir") + 1])
        pattern = args[args.index("--pattern") + 1]
        dest.mkdir(parents=True, exist_ok=True)
        if pattern == "lane-receipts-*":
            part = dest / f"lane-receipts-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.json").write_text(json.dumps({"reports": {}}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    fake_run = _base_fake_run(calls, files=["api/main.py"], extra={"download": download})
    gt.run_triage(_args(work_dir=tmp_path / "work"), run=fake_run)

    for call in calls:
        joined = " ".join(call)
        assert "checks create" not in joined
        assert "issue create" not in joined
        assert "issue comment" not in joined
        assert call[:2] != ["gh", "issue"]


# ---------------------------------------------------------------------------
# main() CLI wiring
# ---------------------------------------------------------------------------

def test_main_returns_nonzero_and_prints_reason_on_refusal(tmp_path, capsys):
    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "run", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"headSha": HEAD_SHA, "workflowName": "Candidate verification"}), stderr="")
        if args[:2] == ["gh", "api"] and "check-runs" in args[2]:
            output = {"candidate": HEAD_SHA, "tree": "t1", "trusted_runner": BASE_SHA, "mode": "executed", "lanes": ["fast-unit"], "conclusion": "failure"}
            return SimpleNamespace(returncode=0, stdout=_check_runs_response("candidate-verification", output), stderr="")
        if args[:2] == ["gh", "api"] and "compare" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"files": [{"filename": "data/vault.sqlite"}]}), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    exit_code = gt.main(["--run-id", MAIN_RUN_ID, "--repo", REPO, "--work-dir", str(tmp_path / "work")], run=fake_run)
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "data/vault.sqlite" in captured.err


def test_main_exits_zero_on_the_happy_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gt, "jev_configured", lambda: False)
    calls = []

    def download(args):
        dest = Path(args[args.index("--dir") + 1])
        pattern = args[args.index("--pattern") + 1]
        dest.mkdir(parents=True, exist_ok=True)
        if pattern == "lane-receipts-*":
            part = dest / f"lane-receipts-{HEAD_SHA}-part0"
            part.mkdir()
            (part / "fast-unit.json").write_text(json.dumps({"reports": {"tests/a.py::test_x": "failed"}}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    fake_run = _base_fake_run(calls, files=["api/main.py"], extra={"download": download})
    exit_code = gt.main(["--run-id", MAIN_RUN_ID, "--repo", REPO, "--work-dir", str(tmp_path / "work")], run=fake_run)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "tests/a.py::test_x" in captured.out
