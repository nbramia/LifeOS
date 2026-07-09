"""Tests for the active-reclaim logic in scripts/gpu-watchdog.sh.

The watchdog historically only *alerted* on VRAM saturation. After issue #199
recurred (2026-07-09: a network wedge left GPU work hung at 97% VRAM and nothing
reclaimed it, forcing a manual reboot), it gained an active safety valve: on
sustained saturation it stops the local LLM service to release VRAM + GPU
queues, then restarts it once VRAM drains.

These tests drive the script through fake sysfs VRAM values and fake
systemctl/sudo/curl binaries, asserting the stop/start decisions without a GPU.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "gpu-watchdog.sh"

# Fake binaries the watchdog shells out to. Each appends its argv to
# $WATCHDOG_ACTIONS so tests can assert what was invoked.
SYSTEMCTL_SHIM = """#!/usr/bin/env bash
echo "systemctl $*" >> "$WATCHDOG_ACTIONS"
if [ "$1" = "is-active" ]; then
    [ "${FAKE_LLM_ACTIVE:-1}" = "1" ] && exit 0 || exit 3
fi
exit 0
"""

# ``sudo -n systemctl <verb> <svc>`` — records the wrapped command (minus -n)
# and returns FAKE_SUDO_RC (0 = allowlist present, non-zero = denied).
SUDO_SHIM = """#!/usr/bin/env bash
args=("$@")
[ "${args[0]:-}" = "-n" ] && args=("${args[@]:1}")
echo "sudo ${args[*]}" >> "$WATCHDOG_ACTIONS"
exit "${FAKE_SUDO_RC:-0}"
"""

CURL_SHIM = """#!/usr/bin/env bash
echo "curl $*" >> "$WATCHDOG_ACTIONS"
exit 0
"""

# Empty per-PID breakdown — keeps the alert path off the real GPU during tests.
ROCM_SMI_SHIM = """#!/usr/bin/env bash
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    """A scratch environment: fake VRAM sysfs, state dir, and recording shims."""
    state_dir = tmp_path / "state"
    card_dir = tmp_path / "card"
    bin_dir = tmp_path / "bin"
    for d in (state_dir, card_dir, bin_dir):
        d.mkdir()

    for name, body in (
        ("systemctl", SYSTEMCTL_SHIM),
        ("sudo", SUDO_SHIM),
        ("curl", CURL_SHIM),
        ("rocm-smi", ROCM_SMI_SHIM),
    ):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)

    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=xtoken\nTELEGRAM_CHAT_ID=123\n")

    actions_file = tmp_path / "actions.log"

    class Sandbox:
        def __init__(self):
            self.state_dir = state_dir
            self.card_dir = card_dir
            self.actions_file = actions_file
            self.marker = state_dir / "gpu-watchdog-reclaimed.marker"
            self.strikes = state_dir / "gpu-watchdog-strikes.count"

        def tick(self, pct, *, fake_sudo_rc="0", fake_llm_active="1", extra_env=None):
            """Run one watchdog invocation at ``pct``% VRAM; return CompletedProcess."""
            (card_dir / "mem_info_vram_total").write_text("100")
            (card_dir / "mem_info_vram_used").write_text(str(pct))
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "LIFEOS_VRAM_STATE_DIR": str(state_dir),
                "LIFEOS_VRAM_CARD_DIR": str(card_dir),
                "ENV_FILE": str(env_file),
                "LIFEOS_WATCHDOG_CURL": str(bin_dir / "curl"),
                "WATCHDOG_ACTIONS": str(actions_file),
                "FAKE_SUDO_RC": fake_sudo_rc,
                "FAKE_LLM_ACTIVE": fake_llm_active,
                # Alert threshold 80, hard-reclaim ceiling 90, restore < 50,
                # two strikes to act — keeps the arithmetic obvious in tests.
                "LIFEOS_VRAM_ALERT_PCT": "80",
                "LIFEOS_VRAM_RECLAIM_PCT": "90",
                "LIFEOS_VRAM_RESTART_PCT": "50",
                "LIFEOS_VRAM_RECLAIM_STRIKES": "2",
            }
            if extra_env:
                env.update(extra_env)
            return subprocess.run(
                ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
            )

        def actions(self):
            if not actions_file.exists():
                return []
            return actions_file.read_text().splitlines()

    return Sandbox()


def _stops(actions):
    return [a for a in actions if a == "sudo systemctl stop lifeos-llm.service"]


def _starts(actions):
    return [a for a in actions if a == "sudo systemctl start lifeos-llm.service"]


def test_single_saturated_tick_does_not_reclaim(sandbox):
    """One tick over the ceiling only arms a strike — it must not stop the LLM."""
    result = sandbox.tick(95)
    assert result.returncode == 0
    assert _stops(sandbox.actions()) == []
    assert sandbox.strikes.read_text().strip() == "1"
    assert not sandbox.marker.exists()


def test_two_consecutive_saturated_ticks_reclaim(sandbox):
    """The second consecutive over-ceiling tick stops the LLM and marks it."""
    sandbox.tick(95)
    result = sandbox.tick(96)
    assert result.returncode == 0
    assert len(_stops(sandbox.actions())) == 1
    assert sandbox.marker.exists()


def test_no_double_stop_while_marker_present(sandbox):
    """Once stopped, further saturated ticks must not re-stop the LLM."""
    sandbox.tick(95)
    sandbox.tick(96)  # stops here
    sandbox.tick(97)  # marker present — must not stop again
    assert len(_stops(sandbox.actions())) == 1


def test_restore_after_vram_drains(sandbox):
    """With the marker set, a tick below RESTART_PCT restarts the LLM."""
    sandbox.marker.write_text("")  # pretend a prior tick stopped it
    result = sandbox.tick(20)  # well below restore threshold
    assert result.returncode == 0
    assert len(_starts(sandbox.actions())) == 1
    assert not sandbox.marker.exists()


def test_marker_held_until_vram_drains(sandbox):
    """A tick still above RESTART_PCT must not restart yet (marker persists)."""
    sandbox.marker.write_text("")
    result = sandbox.tick(70)  # above restore threshold (50)
    assert result.returncode == 0
    assert _starts(sandbox.actions()) == []
    assert sandbox.marker.exists()


def test_below_ceiling_resets_strikes(sandbox):
    """A tick between the alert line and the ceiling clears the strike run."""
    sandbox.tick(95)  # strike 1
    result = sandbox.tick(85)  # saturated-alert band but below ceiling
    assert result.returncode == 0
    assert _stops(sandbox.actions()) == []
    assert not sandbox.strikes.exists()  # reset


def test_sudo_denied_degrades_to_alert_only(sandbox):
    """Without the passwordless allowlist, reclaim attempts but doesn't mark."""
    sandbox.tick(95, fake_sudo_rc="1")
    result = sandbox.tick(96, fake_sudo_rc="1")
    assert result.returncode == 0
    # It tried (sudo invoked) but the stop "failed", so no marker is written.
    assert len(_stops(sandbox.actions())) == 1
    assert not sandbox.marker.exists()


def test_reclaim_disabled_never_stops(sandbox):
    """LIFEOS_VRAM_RECLAIM_PCT above 100 disables active reclaim entirely."""
    env = {"LIFEOS_VRAM_RECLAIM_PCT": "101"}
    sandbox.tick(99, extra_env=env)
    result = sandbox.tick(99, extra_env=env)
    assert result.returncode == 0
    assert _stops(sandbox.actions()) == []


def test_llm_not_running_skips_reclaim(sandbox):
    """If the LLM service isn't active, there's nothing for this valve to free."""
    sandbox.tick(95, fake_llm_active="0")
    result = sandbox.tick(96, fake_llm_active="0")
    assert result.returncode == 0
    assert _stops(sandbox.actions()) == []
    assert not sandbox.marker.exists()


def test_alert_still_fires_at_saturation(sandbox):
    """Regression: the original Telegram alert path still runs below the ceiling."""
    result = sandbox.tick(85)  # above alert (80), below ceiling (90)
    assert result.returncode == 0
    assert any(a.startswith("curl ") for a in sandbox.actions())
