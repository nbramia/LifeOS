"""Tests for scripts/register_persona_bot.py (#794).

Exercises the script against a temporary directory standing in for the repo
(a fake .env, config/telegram_bots.json template, and no committed registry
override) rather than any real install — never runs against the actual repo
root's .env or config/telegram_bots.local.json.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "register_persona_bot.py"

TEMPLATE = [
    {
        "name": "fitness",
        "token_env": "TELEGRAM_FITNESS_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_FITNESS_CHAT_ID",
        "persona_file": "config/personas/fitness.md",
    },
]


def _init_project(root: Path, env_contents: str = "ANTHROPIC_API_KEY=sk-ant-test\n") -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(env_contents)
    (root / "config" / "telegram_bots.json").write_text(json.dumps(TEMPLATE, indent=2) + "\n")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(root),
        capture_output=True,
        text=True,
    )


def test_appends_env_vars_without_touching_existing_content(tmp_path: Path):
    root = tmp_path
    _init_project(root)
    original_env = (root / ".env").read_text()

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode == 0, result.stderr
    new_env = (root / ".env").read_text()
    assert new_env.startswith(original_env)
    assert "TELEGRAM_TRAVEL_BOT_TOKEN=TOKEN123" in new_env
    assert "TELEGRAM_TRAVEL_CHAT_ID=CHAT456" in new_env


def test_appends_without_leading_newline_issue_when_env_already_ends_in_newline(tmp_path: Path):
    root = tmp_path
    _init_project(root, env_contents="ANTHROPIC_API_KEY=sk-ant-test\n")

    _run(root, "travel", "TOKEN123", "CHAT456")

    lines = (root / ".env").read_text().splitlines()
    assert lines == [
        "ANTHROPIC_API_KEY=sk-ant-test",
        "TELEGRAM_TRAVEL_BOT_TOKEN=TOKEN123",
        "TELEGRAM_TRAVEL_CHAT_ID=CHAT456",
    ]


def test_appends_safely_when_env_has_no_trailing_newline(tmp_path: Path):
    root = tmp_path
    _init_project(root, env_contents="ANTHROPIC_API_KEY=sk-ant-test")  # no trailing \n

    _run(root, "travel", "TOKEN123", "CHAT456")

    lines = (root / ".env").read_text().splitlines()
    assert lines == [
        "ANTHROPIC_API_KEY=sk-ant-test",
        "TELEGRAM_TRAVEL_BOT_TOKEN=TOKEN123",
        "TELEGRAM_TRAVEL_CHAT_ID=CHAT456",
    ]


def test_env_symlink_survives(tmp_path: Path):
    """A symlinked .env keeps pointing at the same target after the append
    (the exact landmine #601 warns against: a rewrite-in-place would replace
    the symlink with a plain file)."""
    root = tmp_path
    _init_project(root)
    real_env = root / "real.env"
    real_env.write_text((root / ".env").read_text())
    env_link = root / ".env"
    env_link.unlink()
    env_link.symlink_to(real_env)

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode == 0, result.stderr
    assert env_link.is_symlink()
    assert env_link.resolve() == real_env.resolve()
    assert "TELEGRAM_TRAVEL_BOT_TOKEN=TOKEN123" in real_env.read_text()


def test_refuses_duplicate_bot_name_in_local_override(tmp_path: Path):
    root = tmp_path
    _init_project(root)
    local = root / "config" / "telegram_bots.local.json"
    local.write_text(json.dumps(TEMPLATE + [{
        "name": "travel",
        "token_env": "TELEGRAM_TRAVEL_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_TRAVEL_CHAT_ID",
        "persona_file": "config/personas/travel.md",
    }], indent=2))
    before = local.read_text()

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode != 0
    assert "travel" in result.stderr.lower()
    assert local.read_text() == before  # untouched on refusal
    assert "TELEGRAM_TRAVEL_BOT_TOKEN" not in (root / ".env").read_text()


def test_creates_local_override_seeded_from_template_when_missing(tmp_path: Path):
    root = tmp_path
    _init_project(root)
    local = root / "config" / "telegram_bots.local.json"
    assert not local.exists()

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode == 0, result.stderr
    entries = json.loads(local.read_text())
    names = {e["name"] for e in entries}
    assert names == {"fitness", "travel"}  # seeded from template + new bot


def test_adds_to_existing_local_override_without_disturbing_other_entries(tmp_path: Path):
    root = tmp_path
    _init_project(root)
    local = root / "config" / "telegram_bots.local.json"
    local.write_text(json.dumps([{
        "name": "finance",
        "token_env": "TELEGRAM_FINANCE_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_FINANCE_CHAT_ID",
        "persona_file": "config/personas/finance.md",
    }], indent=2))

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode == 0, result.stderr
    entries = json.loads(local.read_text())
    names = {e["name"] for e in entries}
    # Only finance (pre-existing) + travel (new) — the template's "fitness"
    # entry is NOT pulled in, since the override already existed and replaces
    # rather than merges with the template.
    assert names == {"finance", "travel"}
    finance_entry = next(e for e in entries if e["name"] == "finance")
    assert finance_entry["token_env"] == "TELEGRAM_FINANCE_BOT_TOKEN"


def test_prints_restart_reminder_on_success(tmp_path: Path):
    root = tmp_path
    _init_project(root)

    result = _run(root, "travel", "TOKEN123", "CHAT456")

    assert result.returncode == 0, result.stderr
    assert "lifeos-api" in result.stdout
    assert "restart" in result.stdout.lower()


def test_rejects_invalid_bot_name(tmp_path: Path):
    root = tmp_path
    _init_project(root)

    result = _run(root, "Not A Valid Name!", "TOKEN123", "CHAT456")

    assert result.returncode != 0
    assert not (root / "config" / "telegram_bots.local.json").exists()
    assert "TELEGRAM" not in (root / ".env").read_text()


def test_rejects_reserved_primary_name(tmp_path: Path):
    root = tmp_path
    _init_project(root)

    result = _run(root, "primary", "TOKEN123", "CHAT456")

    assert result.returncode != 0
    assert not (root / "config" / "telegram_bots.local.json").exists()
