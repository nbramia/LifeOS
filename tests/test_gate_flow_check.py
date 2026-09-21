"""The flow check reports every run the gate started but did not finish with a published check."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.gate_flow_check import CHECK_NAME_BY_EVENT, FlowCheckError, collect, main, render

APP = 4891159
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _run_item(run_id, event, status, conclusion, minutes_ago, sha):
    return {
        "id": run_id, "event": event, "status": status, "conclusion": conclusion,
        "created_at": (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_sha": sha, "html_url": f"https://example.invalid/runs/{run_id}",
    }


def _check(name, app=APP, status="completed"):
    return {"name": name, "status": status, "app": {"id": app}}


def _fake_run(runs, checks_by_sha, *, fail=False):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        path = args[2]
        if fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if "/actions/workflows/" in path:
            assert "created=%3E%3D" in path
            page = int(path.split("&page=")[1].split("&")[0])
            pages = runs if isinstance(runs, list) and runs and isinstance(runs[0], list) else [runs]
            batch = pages[page - 1] if page - 1 < len(pages) else []
            return SimpleNamespace(returncode=0, stdout=json.dumps({"workflow_runs": batch}), stderr="")
        sha = path.split("/commits/")[1].split("/")[0]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"check_runs": checks_by_sha.get(sha, [])}), stderr="")

    run.calls = calls
    return run


@pytest.mark.unit
def test_balanced_when_every_completed_run_has_its_app_published_check():
    runs = [
        _run_item(1, "workflow_dispatch", "completed", "success", 60, "a" * 40),
        _run_item(2, "pull_request_target", "completed", "failure", 50, "b" * 40),
        _run_item(3, "workflow_dispatch", "in_progress", None, 5, "c" * 40),
        _run_item(4, "pull_request_target", "completed", "cancelled", 40, "d" * 40),
        _run_item(5, "push", "completed", "success", 30, "e" * 40),
    ]
    checks = {"a" * 40: [_check("candidate-verification")], "b" * 40: [_check("candidate-verification-shadow")]}
    report = collect("o/r", app_id=APP, now=NOW, run=_fake_run(runs, checks))
    assert report.balanced
    assert (report.counted, report.cancelled) == (3, 1)
    assert "balanced" in render(report)


@pytest.mark.unit
def test_a_run_past_the_ceiling_is_stalled():
    runs = [_run_item(7, "workflow_dispatch", "in_progress", None, 31, "a" * 40)]
    report = collect("o/r", app_id=APP, ceiling_minutes=25, now=NOW, run=_fake_run(runs, {}))
    assert [i.kind for i in report.imbalances] == ["stalled"]
    assert "31 min" in report.imbalances[0].detail
    assert "IMBALANCE" in render(report) and "run 7" in render(report)


@pytest.mark.unit
def test_a_queued_run_within_the_ceiling_is_not_stalled():
    runs = [_run_item(7, "workflow_dispatch", "queued", None, 10, "a" * 40)]
    assert collect("o/r", app_id=APP, ceiling_minutes=25, now=NOW, run=_fake_run(runs, {})).balanced


@pytest.mark.unit
@pytest.mark.parametrize("event", sorted(CHECK_NAME_BY_EVENT))
def test_a_completed_run_without_its_published_check_is_unpublished(event):
    runs = [_run_item(9, event, "completed", "success", 60, "a" * 40)]
    report = collect("o/r", app_id=APP, now=NOW, run=_fake_run(runs, {}))
    assert [i.kind for i in report.imbalances] == ["unpublished"]
    assert CHECK_NAME_BY_EVENT[event] in report.imbalances[0].detail


@pytest.mark.unit
@pytest.mark.parametrize("check", [
    _check("candidate-verification", app=APP + 1),
    _check("candidate-verification-shadow"),
    _check("candidate-verification", status="in_progress"),
    {"name": "candidate-verification", "status": "completed", "app": None},
])
def test_only_a_completed_check_of_the_right_name_from_the_app_counts(check):
    runs = [_run_item(9, "workflow_dispatch", "completed", "success", 60, "a" * 40)]
    report = collect("o/r", app_id=APP, now=NOW, run=_fake_run(runs, {"a" * 40: [check]}))
    assert [i.kind for i in report.imbalances] == ["unpublished"]


@pytest.mark.unit
def test_cancelled_runs_are_expected_and_never_queried():
    runs = [_run_item(4, "workflow_dispatch", "completed", "cancelled", 40, "d" * 40)]
    fake = _fake_run(runs, {})
    report = collect("o/r", app_id=APP, now=NOW, run=fake)
    assert report.balanced and report.cancelled == 1
    assert len(fake.calls) == 1


@pytest.mark.unit
def test_an_api_failure_is_an_error_not_a_balanced_report():
    with pytest.raises(FlowCheckError):
        collect("o/r", app_id=APP, now=NOW, run=_fake_run([], {}, fail=True))


@pytest.mark.unit
def test_cli_exits_zero_when_balanced_and_never_notifies(capsys):
    runs = [_run_item(1, "workflow_dispatch", "completed", "success", 60, "a" * 40)]
    checks = {"a" * 40: [_check("candidate-verification")]}
    sent = []
    code = main(["--repository", "o/r", "--notify", "telegram"], run=_fake_run(runs, checks), notify=lambda ch, text: sent.append((ch, text)) or True)
    assert code == 0
    assert sent == []
    assert "balanced" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_exits_two_and_notifies_on_an_imbalance(capsys):
    runs = [_run_item(7, "workflow_dispatch", "in_progress", None, 90, "a" * 40)]
    sent = []
    code = main(["--repository", "o/r", "--notify", "telegram"], run=_fake_run(runs, {}), notify=lambda ch, text: sent.append((ch, text)) or True)
    assert code == 2
    assert len(sent) == 1 and sent[0][0] == "telegram" and "stalled" in sent[0][1]
    assert "notified via telegram: sent" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_reports_an_api_failure_with_exit_one(capsys):
    code = main(["--repository", "o/r"], run=_fake_run([], {}, fail=True), notify=lambda ch, text: True)
    assert code == 1
    assert "could not run" in capsys.readouterr().err



@pytest.mark.unit
def test_a_full_first_page_is_followed_by_the_next_page():
    """A stalled run on the second page is found, so a busy day cannot hide
    an imbalance behind the 100-run page cap."""
    first = [_run_item(i, "workflow_dispatch", "completed", "cancelled", 10, "a" * 40) for i in range(100)]
    second = [_run_item(999, "workflow_dispatch", "in_progress", None, 90, "b" * 40)]
    fake = _fake_run([first, second], {})
    report = collect("o/r", app_id=APP, now=NOW, run=fake)
    assert [i.run_id for i in report.imbalances] == [999]
    assert report.cancelled == 100
    assert sum("/actions/workflows/" in c[2] for c in fake.calls) == 2


@pytest.mark.unit
def test_a_short_first_page_is_the_only_page_fetched():
    fake = _fake_run([_run_item(1, "workflow_dispatch", "completed", "cancelled", 10, "a" * 40)], {})
    collect("o/r", app_id=APP, now=NOW, run=fake)
    assert sum("/actions/workflows/" in c[2] for c in fake.calls) == 1


@pytest.mark.unit
def test_an_unbounded_window_is_an_error_not_a_truncated_report():
    from scripts import gate_flow_check

    full = [_run_item(i, "workflow_dispatch", "completed", "cancelled", 10, "a" * 40) for i in range(100)]
    fake = _fake_run([full] * (gate_flow_check.MAX_PAGES + 1), {})
    with pytest.raises(FlowCheckError):
        collect("o/r", app_id=APP, now=NOW, run=fake)
