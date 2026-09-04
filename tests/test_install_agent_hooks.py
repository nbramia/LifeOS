"""Tests for scripts/install-agent-hooks.sh (issue #849).

Runs the installer via subprocess against temp copies of a Claude Code
settings.json and a Codex hooks.json — NEVER the operator's real
~/.claude/settings.json or ~/.codex/hooks.json, which are pointed at only
via LIFEOS_CLAUDE_SETTINGS / LIFEOS_CODEX_HOOKS overrides here. Confirms
idempotency (running twice adds exactly one lifeos-agent-hook.sh entry per
event) and that every pre-existing entry — a synthetic Orca-like entry, an
atuin-like entry, a legacy claude-session-pane.sh entry — survives
byte-for-byte.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-agent-hooks.sh"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.unit

_SYNTHETIC_SETTINGS = {
    "hooks": {
        "SessionStart": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "orca-session-hook"}]},
            {"matcher": "*", "hooks": [{"type": "command", "command": "atuin-init-hook"}]},
            {"matcher": "*", "hooks": [{"type": "command",
                                        "command": "/opt/synthetic/scripts/claude-session-pane.sh"}]},
        ],
        "Stop": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "orca-stop-hook"}]},
        ],
    },
    "someOtherTopLevelSetting": "preserve-me",
}

_SYNTHETIC_HOOKS_JSON = {
    "hooks": {
        "SessionStart": [
            {"matcher": "startup|resume",
             "hooks": [{"type": "command", "command": "/opt/synthetic/scripts/codex-session-pane.sh"}]},
        ],
    },
}


def _run_installer(settings_path: Path, hooks_path: Path):
    env = {
        "LIFEOS_CLAUDE_SETTINGS": str(settings_path),
        "LIFEOS_CODEX_HOOKS": str(hooks_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(settings_path.parent),
    }
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, timeout=15,
    )


def _lifeos_entries(hook_list: list[dict]) -> list[dict]:
    out = []
    for group in hook_list:
        for h in group.get("hooks", []):
            if "lifeos-agent-hook.sh" in h.get("command", ""):
                out.append(group)
    return out


@pytest.fixture
def temp_configs(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    hooks_path = tmp_path / "hooks.json"
    settings_path.write_text(json.dumps(_SYNTHETIC_SETTINGS, indent=2), encoding="utf-8")
    hooks_path.write_text(json.dumps(_SYNTHETIC_HOOKS_JSON, indent=2), encoding="utf-8")
    return settings_path, hooks_path


@pytest.mark.unit
def test_installer_adds_one_entry_per_event_and_preserves_existing(temp_configs):
    settings_path, hooks_path = temp_configs
    original_settings = copy.deepcopy(_SYNTHETIC_SETTINGS)
    original_hooks = copy.deepcopy(_SYNTHETIC_HOOKS_JSON)

    r = _run_installer(settings_path, hooks_path)
    assert r.returncode == 0, r.stderr.decode()

    new_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    new_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))

    # Every pre-existing entry survives unchanged, in the same order.
    assert new_settings["hooks"]["SessionStart"][:3] == original_settings["hooks"]["SessionStart"]
    assert new_settings["hooks"]["Stop"][:1] == original_settings["hooks"]["Stop"]
    assert new_settings["someOtherTopLevelSetting"] == "preserve-me"
    assert new_hooks["hooks"]["SessionStart"][:1] == original_hooks["hooks"]["SessionStart"]

    # Exactly one lifeos-agent-hook.sh entry per Claude Code event.
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        entries = _lifeos_entries(new_settings["hooks"].get(event, []))
        assert len(entries) == 1, f"{event}: expected 1 lifeos entry, found {len(entries)}"

    # Exactly one for each Codex event the installer supports; no SessionEnd.
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        entries = _lifeos_entries(new_hooks["hooks"].get(event, []))
        assert len(entries) == 1, f"{event}: expected 1 lifeos entry, found {len(entries)}"
    assert "SessionEnd" not in new_hooks["hooks"]


@pytest.mark.unit
def test_installer_is_idempotent_across_two_runs(temp_configs):
    settings_path, hooks_path = temp_configs

    r1 = _run_installer(settings_path, hooks_path)
    assert r1.returncode == 0, r1.stderr.decode()
    after_first = json.loads(settings_path.read_text(encoding="utf-8"))
    after_first_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))

    r2 = _run_installer(settings_path, hooks_path)
    assert r2.returncode == 0, r2.stderr.decode()
    after_second = json.loads(settings_path.read_text(encoding="utf-8"))
    after_second_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert after_first == after_second
    assert after_first_hooks == after_second_hooks

    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        entries = _lifeos_entries(after_second["hooks"].get(event, []))
        assert len(entries) == 1


@pytest.mark.unit
def test_installer_creates_missing_files(tmp_path: Path):
    settings_path = tmp_path / "nested" / "settings.json"
    hooks_path = tmp_path / "nested2" / "hooks.json"
    assert not settings_path.exists()
    assert not hooks_path.exists()

    r = _run_installer(settings_path, hooks_path)
    assert r.returncode == 0, r.stderr.decode()

    new_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    new_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        assert len(_lifeos_entries(new_settings["hooks"].get(event, []))) == 1
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        assert len(_lifeos_entries(new_hooks["hooks"].get(event, []))) == 1


@pytest.mark.unit
def test_installer_never_writes_a_token(temp_configs):
    settings_path, hooks_path = temp_configs
    r = _run_installer(settings_path, hooks_path)
    assert r.returncode == 0, r.stderr.decode()
    for path in (settings_path, hooks_path):
        text = path.read_text(encoding="utf-8")
        assert "LIFEOS_AGENT_HOOK_TOKEN=" not in text


@pytest.mark.unit
def test_bash_syntax_check():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, timeout=5)
    assert r.returncode == 0, r.stderr.decode()
