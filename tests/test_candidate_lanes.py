"""The hosted lane selection agrees with the local plan about what is docs-only."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.candidate_lanes import ALL_LANES, classify

REPO = Path(__file__).resolve().parent.parent

_CASES = [
    ("README.md", "docs-only", (), "docs_readme"),
    ("docs/guides/scripts.md\ndocs/AGENTS.md", "docs-only", (), "docs_multiple"),
    ("CHANGELOG.txt", "docs-only", (), "docs_txt"),
    ("docs/adr/001.rst", "docs-only", (), "docs_rst"),
    ("requirements.txt", "executed", ("fast-unit",), "manifest_alone"),
    ("requirements-dev.txt\nREADME.md", "executed", ("fast-unit",), "manifest_plus_docs"),
    ("docs/x.md\napi/services/llm_client.py", "executed", ("fast-unit",), "mixed_docs_code"),
    ("api/routes/chat.py", "executed", ("fast-unit",), "code_without_web"),
    ("web/chat.js", "executed", ALL_LANES, "web_only"),
    ("docs/x.md\nweb/index.html", "executed", ALL_LANES, "docs_plus_web"),
    ("", "executed", ALL_LANES, "empty"),
    ("\n  \n", "executed", ALL_LANES, "blank_lines"),
]


@pytest.mark.unit
@pytest.mark.parametrize("changed,mode,lanes", [(c, m, lanes) for c, m, lanes, _ in _CASES], ids=[i for *_, i in _CASES])
def test_classify(changed, mode, lanes):
    selection = classify(changed.splitlines())
    assert (selection.mode, selection.lanes) == (mode, lanes)
    assert selection.reason


@pytest.mark.unit
def test_cli_prints_output_lines_from_stdin():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "candidate_lanes.py")],
        input="docs/a.md\n", capture_output=True, text=True, check=True,
    )
    assert result.stdout.splitlines()[:2] == ["mode=docs-only", "lanes="]


def _local_plan(changed_files: str) -> str:
    env = {**os.environ, "LIFEOS_TEST_PLAN_ONLY": "1", "LIFEOS_TEST_CHANGED_FILES": changed_files}
    result = subprocess.run(["bash", str(REPO / "scripts" / "test.sh"), "auto"], capture_output=True, text=True, env=env)
    for line in result.stdout.splitlines():
        if line.startswith("auto-plan: "):
            return line.removeprefix("auto-plan: ")
    raise AssertionError(f"no auto-plan line in test.sh output: {result.stdout!r} {result.stderr!r}")


@pytest.mark.unit
@pytest.mark.parametrize("changed", [c for c, *_ in _CASES if c.strip()], ids=[i for c, *_, i in _CASES if c.strip()])
def test_docs_only_agrees_with_the_local_plan(changed):
    assert (classify(changed.splitlines()).mode == "docs-only") == (_local_plan(changed) == "skip")
