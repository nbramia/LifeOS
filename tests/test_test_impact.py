"""The shadow test-impact selector: import closure, fail-closed triggers, and miss attribution."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import test_impact
from scripts.test_impact import Report, failing_modules_from_receipts, select, with_outcomes

REPO = Path(__file__).resolve().parent.parent


def _tree(tmp_path: Path) -> Path:
    files = {
        "api/__init__.py": "",
        "api/leaf.py": "X = 1\n",
        "api/mid.py": "from api.leaf import X\n",
        "api/other.py": "import json\n",
        "config/__init__.py": "",
        "config/settings.py": "Y = 2\n",
        "tests/__init__.py": "",
        "tests/conftest.py": "import config.settings\n",
        "tests/test_leaf.py": "from api import leaf\n",
        "tests/test_mid.py": "import api.mid\n",
        "tests/test_other.py": "from api.other import *\n",
        "tests/test_alone.py": "import os\n",
        "tests/helpers/__init__.py": "",
        "tests/helpers/util.py": "import api.leaf\n",
        "mcp_server.py": "import api.mid\n",
        "docs/guide.md": "# hi\n",
        "requirements.txt": "x\n",
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


DURATIONS = {"tests/test_leaf.py": 10, "tests/test_mid.py": 20, "tests/test_other.py": 30, "tests/test_alone.py": 40}


@pytest.mark.unit
def test_a_leaf_change_selects_its_direct_and_transitive_importers(tmp_path):
    report = select(_tree(tmp_path), ["api/leaf.py"], DURATIONS)
    assert report.mode == "select"
    assert report.selected_modules == ("tests/test_leaf.py", "tests/test_mid.py")
    assert report.total_modules == 4
    assert report.selected_duration_share == 0.3
    assert report.unmodeled_paths == ()


@pytest.mark.unit
def test_a_mid_change_does_not_reach_the_leaf_test(tmp_path):
    report = select(_tree(tmp_path), ["api/mid.py"], DURATIONS)
    assert report.selected_modules == ("tests/test_mid.py",)


@pytest.mark.unit
def test_a_changed_test_module_is_always_selected(tmp_path):
    report = select(_tree(tmp_path), ["tests/test_alone.py"], DURATIONS)
    assert report.selected_modules == ("tests/test_alone.py",)


@pytest.mark.unit
def test_a_change_reaching_no_test_selects_nothing(tmp_path):
    report = select(_tree(tmp_path), ["config/settings.py"], DURATIONS)
    assert report.mode == "select"
    assert report.selected_modules == ()
    assert report.selected_duration_share == 0.0


@pytest.mark.unit
def test_inert_files_select_nothing_but_are_listed(tmp_path):
    report = select(_tree(tmp_path), ["docs/guide.md", "api/leaf.py"], DURATIONS)
    assert report.mode == "select"
    assert report.unmodeled_paths == ("docs/guide.md",)
    assert report.selected_modules == ("tests/test_leaf.py", "tests/test_mid.py")


@pytest.mark.unit
@pytest.mark.parametrize("path", [
    "tests/conftest.py", "api/conftest.py", "tests/helpers/util.py", "tests/narration_baseline.json",
    "tests/fixtures/x.json", "requirements.txt", "requirements-dev.txt", "constraints.txt",
    ".github/workflows/candidate-verification.yml", "pyproject.toml",
    "scripts/verify_candidate.py", "scripts/test_lane_registry.py", "scripts/test.sh",
    "data/blob.bin", "model.gguf",
])
def test_paths_the_graph_cannot_model_fail_closed_to_full(tmp_path, path):
    report = select(_tree(tmp_path), ["api/leaf.py", path], DURATIONS)
    assert report.mode == "full"
    assert path in report.reason
    assert set(report.selected_modules) == {"tests/test_leaf.py", "tests/test_mid.py", "tests/test_other.py", "tests/test_alone.py"}
    assert report.selected_duration_share == 1.0


@pytest.mark.unit
def test_an_empty_changed_set_is_full(tmp_path):
    report = select(_tree(tmp_path), ["", "  "], DURATIONS)
    assert (report.mode, report.reason) == ("full", "changed set unavailable")


@pytest.mark.unit
def test_a_selector_error_is_full(tmp_path, monkeypatch):
    def boom(_root):
        raise RuntimeError("graph exploded")
    monkeypatch.setattr(test_impact, "build_graph", boom)
    report = select(_tree(tmp_path), ["api/leaf.py"], DURATIONS)
    assert report.mode == "full"
    assert "graph exploded" in report.reason


@pytest.mark.unit
def test_a_syntax_error_in_a_module_is_not_an_error(tmp_path):
    root = _tree(tmp_path)
    (root / "api" / "broken.py").write_text("def (:\n")
    report = select(root, ["api/broken.py"], DURATIONS)
    assert (report.mode, report.selected_modules) == ("select", ())


@pytest.mark.unit
def test_failures_outside_the_selection_are_reported_as_misses(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "fast-unit.json").write_text(json.dumps({"reports": {
        "tests/test_leaf.py::test_a": "passed",
        "tests/test_other.py::test_b": "failed",
        "tests/test_alone.py::test_c": "skipped",
        "tests/test_mid.py::test_d": "error",
    }}))
    (receipts / "garbage.json").write_text("not json")
    (receipts / "no-reports.json").write_text("{}")
    failing = failing_modules_from_receipts(receipts)
    assert failing == ("tests/test_mid.py", "tests/test_other.py")
    report = with_outcomes(select(_tree(tmp_path), ["api/leaf.py"], DURATIONS), failing)
    assert report.failing_modules == failing
    assert report.missed_failures == ("tests/test_other.py",)
    full = with_outcomes(select(_tree(tmp_path), ["requirements.txt"], DURATIONS), failing)
    assert full.missed_failures == ()


@pytest.mark.unit
def test_cli_writes_the_report_and_appends_the_summary(tmp_path):
    root = _tree(tmp_path)
    changed = tmp_path / "changed"
    changed.write_text("api/leaf.py\n")
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps(DURATIONS))
    output = tmp_path / "impact_selection.json"
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_impact.py"), "--root", str(root), "--changed-files", str(changed),
         "--durations", str(durations), "--output", str(output), "--summary", str(summary)],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)["impact_selection"]
    assert payload["mode"] == "select"
    assert payload["selected_modules"] == ["tests/test_leaf.py", "tests/test_mid.py"]
    assert json.loads(output.read_text()) == {"impact_selection": payload}
    assert "| selected modules | 2 of 4 |" in summary.read_text()


@pytest.mark.unit
def test_the_real_tree_builds_and_conftest_is_a_full_trigger():
    report = select(REPO, ["tests/conftest.py"])
    assert report.mode == "full"
    assert report.total_modules > 100
    report = select(REPO, ["scripts/candidate_lanes.py"])
    assert report.mode == "select"
    assert "tests/test_candidate_lanes.py" in report.selected_modules


@pytest.mark.unit
def test_report_serializes_every_field():
    report = Report("select", "r", ("tests/test_x.py",), 1, 1.0, ())
    assert set(report.as_dict()) == {
        "mode", "reason", "selected_modules", "total_modules", "selected_duration_share",
        "unmodeled_paths", "failing_modules", "missed_failures",
    }
