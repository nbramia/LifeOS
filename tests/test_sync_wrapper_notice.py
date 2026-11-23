"""Tests for run_sync_wrapper.sh's "LifeOS Sync did not finish" notice.

The sync's own summary is its report. When the Python run exits non-zero
before reaching its summary step (killed, crashed), the wrapper sends a notice
instead and copies it to the bot-notices log. These drive the real wrapper with
a fake venv python, curl and HOME, so nothing touches the network or the real
sync.
"""

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_PYTHON = """#!/bin/bash
[ "$1" = "-c" ] && exit 0
case "$FAKE_SYNC" in
    ok) touch "$LIFEOS_SYNC_SUMMARY_MARKER"; exit 0 ;;
    failed_sources) touch "$LIFEOS_SYNC_SUMMARY_MARKER"; exit 1 ;;
    crash) exit 1 ;;
    oom) kill -KILL $$ ;;
    hang)
        trap 'exit 143' TERM
        touch "$FAKE_STARTED"
        sleep 30 &
        wait
        ;;
esac
"""

FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_CURL_LOG"
"""


@pytest.fixture
def wrapper(tmp_path):
    checkout = tmp_path / "LifeOS"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "logs").mkdir()
    (checkout / "api" / "services").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "run_sync_wrapper.sh", checkout / "scripts")
    shutil.copy(REPO_ROOT / "api" / "services" / "bot_notices.py", checkout / "api" / "services")
    (checkout / ".env").write_text("TELEGRAM_BOT_TOKEN=123:synthetic-token\nTELEGRAM_CHAT_ID=999000\n")

    venv_bin = tmp_path / "home" / ".venvs" / "lifeos" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text(FAKE_PYTHON)
    (venv_bin / "python").chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)

    class Wrapper:
        log = tmp_path / "notices.jsonl"
        curl_log = tmp_path / "curl.log"
        started = tmp_path / "started"
        script = checkout / "scripts" / "run_sync_wrapper.sh"

        def env(self, mode):
            return {
                **os.environ,
                "HOME": str(tmp_path / "home"),
                "TMPDIR": str(tmp_path),
                "FAKE_SYNC": mode,
                "FAKE_STARTED": str(self.started),
                "FAKE_CURL_LOG": str(self.curl_log),
                "LIFEOS_WATCHDOG_CURL": str(curl),
                "BOT_NOTICE_LOG": str(self.log),
            }

        def run(self, mode):
            return subprocess.run(
                ["bash", str(self.script), "--execute"],
                env=self.env(mode),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            ).returncode

        def notices(self):
            if not self.log.exists():
                return []
            return [json.loads(line) for line in self.log.read_text().splitlines()]

        def markers_left(self):
            return list(tmp_path.glob("lifeos-sync-summary.*"))

    return Wrapper()


@pytest.mark.parametrize("mode,code", [("ok", 0), ("failed_sources", 1)])
def test_no_notice_once_the_summary_step_was_reached(wrapper, mode, code):
    assert wrapper.run(mode) == code
    assert wrapper.notices() == []
    assert not wrapper.curl_log.exists()
    assert wrapper.markers_left() == []


def test_crash_before_the_summary_sends_the_notice(wrapper):
    assert wrapper.run("crash") == 1
    [notice] = wrapper.notices()
    assert notice["source"] == "LifeOS/run_sync_wrapper"
    assert notice["text"].startswith("🚨 *LifeOS Sync did not finish*")
    assert "exited with code 1 after 0m" in notice["text"]
    sent = wrapper.curl_log.read_text()
    assert "text=🚨 *LifeOS Sync did not finish*" in sent
    assert "synthetic-token" not in wrapper.log.read_text()
    assert wrapper.markers_left() == []


def test_out_of_memory_kill_sends_the_notice(wrapper):
    assert wrapper.run("oom") == 137
    [notice] = wrapper.notices()
    assert "was killed by signal 9" in notice["text"]


def test_systemd_stop_still_sends_the_notice(wrapper):
    """systemd sends SIGTERM to every process in the unit; the wrapper outlives the sync."""
    proc = subprocess.Popen(
        ["bash", str(wrapper.script), "--execute"],
        env=wrapper.env("hang"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not wrapper.started.exists():
        assert time.monotonic() < deadline, "fake sync never started"
        time.sleep(0.05)
    os.killpg(proc.pid, signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    [notice] = wrapper.notices()
    assert "was killed by signal 15" in notice["text"]


def test_sync_touches_the_marker_only_when_the_wrapper_set_one(tmp_path, monkeypatch):
    from scripts import run_all_syncs

    marker = tmp_path / "marker"
    monkeypatch.delenv("LIFEOS_SYNC_SUMMARY_MARKER", raising=False)
    run_all_syncs._mark_summary_reached()
    assert not marker.exists()
    monkeypatch.setenv("LIFEOS_SYNC_SUMMARY_MARKER", str(marker))
    run_all_syncs._mark_summary_reached()
    assert marker.exists()


def test_marker_is_written_before_the_summary_is_sent():
    """The wrapper treats a missing marker as "no summary went out"."""
    body = (REPO_ROOT / "scripts" / "run_all_syncs.py").read_text()
    run = body[body.index("def run_all_syncs("):]
    assert run.index("_mark_summary_reached()") < run.index("send_sync_summary_telegram(result")


def test_wrapper_leaves_no_watchdog_sleep_behind(wrapper):
    before = set(subprocess.run(["pgrep", "-x", "sleep"], capture_output=True, text=True).stdout.split())
    wrapper.run("crash")
    after = set(subprocess.run(["pgrep", "-x", "sleep"], capture_output=True, text=True).stdout.split())
    leaked = [
        pid for pid in after - before
        if Path(f"/proc/{pid}/cmdline").exists()
        and Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[1:2] == [b"21600"]
    ]
    assert leaked == []
