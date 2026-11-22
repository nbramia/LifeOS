"""Tests for the bot-notices log: every notice the main bot sends on its own is
copied to a JSON Lines log, and nothing else is."""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services import bot_notices, telegram

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "api" / "services" / "bot_notices.py"
SHELL_SENDERS = {
    "network-watchdog.sh": "LifeOS/network-watchdog",
    "server-watchdog.sh": "LifeOS/server-watchdog",
    "infra-watchdog.sh": "LifeOS/infra-watchdog",
    "gpu-watchdog.sh": "LifeOS/gpu-watchdog",
    "auto-deploy.sh": "LifeOS/auto-deploy",
    "run_sync_wrapper.sh": "LifeOS/run_sync_wrapper",
}
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "state" / "bot-notices" / "notices.jsonl"
    monkeypatch.setenv("BOT_NOTICE_LOG", str(path))
    return path


def _entries(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "123:synthetic-token")
    monkeypatch.setattr(telegram.settings, "telegram_chat_id", "999000")
    monkeypatch.setattr(type(telegram.settings), "telegram_enabled", property(lambda self: True))


def _resp(status=200):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    r.json.return_value = {"result": {"message_id": 7}}
    return r


# --- the log itself ---------------------------------------------------------


def test_record_appends_one_json_line_and_creates_the_directory(log):
    assert bot_notices.record("Disk 91% full", "LifeOS/server-watchdog")
    assert bot_notices.record("line one\nline two", "LifeOS/agent_worker.worker")

    lines = log.read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert set(first) == {"ts", "bot", "source", "text"}
    assert TS.match(first["ts"])
    assert first["bot"] == "lifeos"
    assert first["source"] == "LifeOS/server-watchdog"
    assert first["text"] == "Disk 91% full"
    assert second["text"] == "line one\nline two"


def test_record_failure_returns_false_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_NOTICE_LOG", str(tmp_path))  # a directory: open() fails
    assert bot_notices.record("x", "LifeOS/test") is False


def test_no_home_and_no_override_writes_nothing(monkeypatch):
    monkeypatch.delenv("BOT_NOTICE_LOG")
    monkeypatch.delenv("HOME", raising=False)
    assert bot_notices.log_path() is None
    assert bot_notices.record("x", "LifeOS/test") is False


def test_cli_reads_text_from_stdin(log):
    result = subprocess.run(
        ["python3", "-I", str(MODULE), "LifeOS/gpu-watchdog"],
        input="🚨 *GPU* 95% & rising",
        text=True,
        capture_output=True,
        env={**os.environ, "BOT_NOTICE_LOG": str(log)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    [entry] = _entries(log)
    assert entry["source"] == "LifeOS/gpu-watchdog"
    assert entry["text"] == "🚨 *GPU* 95% & rising"


# --- telegram.py senders ------------------------------------------------------


def test_send_message_records_primary_notice_and_never_the_token(log, telegram_on):
    with patch.object(telegram.httpx, "post", return_value=_resp()):
        assert telegram.send_message("Sync finished: 3 sources stale")

    [entry] = _entries(log)
    assert entry["text"] == "Sync finished: 3 sources stale"
    assert entry["source"] == "LifeOS/tests.test_bot_notices"
    raw = log.read_text()
    assert "synthetic-token" not in raw and "999000" not in raw


def test_failed_send_is_still_recorded(log, telegram_on):
    with patch.object(telegram.httpx, "post", return_value=_resp(500)):
        assert telegram.send_message("Backup failed") is False
    assert [e["text"] for e in _entries(log)] == ["Backup failed"]


def test_long_message_is_recorded_once_whole(log, telegram_on):
    text = "x" * 9000  # split into several Telegram chunks
    with patch.object(telegram.httpx, "post", return_value=_resp()) as post:
        telegram.send_message(text)
    assert post.call_count > 1
    assert [e["text"] for e in _entries(log)] == [text]


def test_capture_ids_and_async_senders_record(log, telegram_on):
    with patch.object(telegram.httpx, "post", return_value=_resp()):
        assert telegram.send_message_capture_ids("Agent task needs input", parse_mode=None) == [7]

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    async def post(*a, **k):
        return _resp()

    client.post = post
    with patch.object(telegram.httpx, "AsyncClient", return_value=client):
        assert asyncio.run(telegram.send_message_async("Reminder: renew passport"))

    assert [e["text"] for e in _entries(log)] == ["Agent task needs input", "Reminder: renew passport"]


def test_alert_relay_is_attributed_to_its_caller(log, telegram_on, monkeypatch):
    from api.services import notifications

    monkeypatch.setattr(notifications.settings, "alert_email", "", raising=False)
    monkeypatch.setattr(telegram.settings, "alert_email", "", raising=False)
    # Run send_alert's Telegram leg from a frame in a synthetic sender module.
    sender = {"__name__": "api.services.synthetic_sender", "notifications": notifications}
    exec("def fire():\n    return notifications.send_alert('Disk full', 'Free up space', to='a@example.com')", sender)
    with patch("api.services.gmail.get_gmail_service", side_effect=RuntimeError("no gmail")):
        with patch.object(telegram.httpx, "post", return_value=_resp()):
            sender["fire"]()
    [entry] = _entries(log)
    assert entry["source"] == "LifeOS/synthetic_sender"
    assert entry["text"].startswith("*[LifeOS Alert] Disk full*")


def test_persona_bot_sends_are_not_recorded(log, telegram_on):
    with patch.object(telegram.httpx, "post", return_value=_resp()):
        telegram.send_message("Spent $40 on groceries", bot="finance")
    assert _entries(log) == []


def test_replies_while_handling_an_update_are_not_recorded(log, telegram_on):
    token = telegram._active_bot_token.set("123:synthetic-token")
    try:
        with patch.object(telegram.httpx, "post", return_value=_resp()):
            telegram.send_message("Here is your answer")
    finally:
        telegram._active_bot_token.reset(token)
    assert _entries(log) == []


def test_log_failure_does_not_fail_the_send(tmp_path, monkeypatch, telegram_on):
    monkeypatch.setenv("BOT_NOTICE_LOG", str(tmp_path))  # unwritable as a file
    with patch.object(telegram.httpx, "post", return_value=_resp()):
        assert telegram.send_message("still sent") is True


# --- shell senders --------------------------------------------------------------


@pytest.mark.parametrize("script,source", sorted(SHELL_SENDERS.items()))
def test_shell_sender_records_each_telegram_send(script, source):
    body = (REPO_ROOT / "scripts" / script).read_text()
    assert "api/services/bot_notices.py" in body
    assert f'"{source}"' in body


def test_shell_sender_records_after_send_and_keeps_its_exit_status(tmp_path, log):
    """Run infra-watchdog's real send_telegram against a failing fake curl."""
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:synthetic-token\nTELEGRAM_CHAT_ID=999000\n")
    script = REPO_ROOT / "scripts" / "infra-watchdog.sh"
    funcs = re.search(
        r"^record_bot_notice\(\) \{.*?^\}\n\nsend_telegram\(\) \{.*?^\}\n", script.read_text(), re.S | re.M
    ).group(0)
    driver = f'PROJECT_DIR="{REPO_ROOT}"\nENV_FILE="{env_file}"\n{funcs}\nsend_telegram "Route /pebble down"\necho "rc=$?"\n'
    result = subprocess.run(
        ["bash", "-c", driver],
        env={**os.environ, "LIFEOS_WATCHDOG_CURL": str(curl), "BOT_NOTICE_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "rc=22" in result.stdout
    [entry] = _entries(log)
    assert entry == {**entry, "source": "LifeOS/infra-watchdog", "text": "Route /pebble down"}
    assert "synthetic-token" not in log.read_text()


def test_module_run_as_main_is_named_by_its_path():
    worker = REPO_ROOT / "api" / "services" / "agent_worker" / "worker.py"
    assert telegram._main_module_name(str(worker)) == "api.services.agent_worker.worker"
    assert telegram._main_module_name("/elsewhere/tool.py") == "tool"


def test_record_gives_up_when_the_lock_is_held(log, monkeypatch):
    import fcntl

    monkeypatch.setattr(bot_notices, "LOCK_TIMEOUT_SECONDS", 0.1)
    log.parent.mkdir(parents=True)
    with open(log, "ab") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        child = subprocess.run(
            ["python3", "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(MODULE.parent)!r}); import bot_notices as b; "
             "b.LOCK_TIMEOUT_SECONDS = 0.1; print(b.record('x', 'LifeOS/test'))"],
            env={**os.environ, "BOT_NOTICE_LOG": str(log)}, capture_output=True, text=True, timeout=30,
        )
    assert child.stdout.strip() == "False"
    assert _entries(log) == []
