"""Tests for the Tailscale address self-heal in scripts/network-watchdog.sh.

Drives the real script end to end with stubbed `nmcli`, `tailscale`, `ip`,
`systemctl`, and `curl` binaries, so no real network, NetworkManager, or
Tailscale daemon is touched. `nmcli` is always stubbed to report no WiFi
device, so every run exits right after the Tailscale check — these tests
isolate that check from the pre-existing WiFi recovery path.

The script resolves its own project directory from its own path
(`dirname "$(dirname "${BASH_SOURCE[0]}")")`), so each test copies the real
script into a throwaway project layout instead of running it in place —
this keeps state files (logs, cooldown markers) out of the real repo tree.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = REPO_ROOT / "scripts" / "network-watchdog.sh"

NMCLI_SHIM = """#!/usr/bin/env bash
echo "nmcli $*" >> "$WATCHDOG_ACTIONS"
echo "eth0:ethernet"
exit 0
"""

# BackendState is controlled by $FAKE_TAILSCALE_STATE (default: Running).
TAILSCALE_SHIM = """#!/usr/bin/env bash
echo "tailscale $*" >> "$WATCHDOG_ACTIONS"
if [ "$1" = "status" ]; then
    echo "{\\"BackendState\\":\\"${FAKE_TAILSCALE_STATE:-Running}\\"}"
fi
exit 0
"""

# Reports tailscale0 as having an IPv4 address once $RESTART_MARKER exists —
# i.e., only after the systemctl shim below has "fixed" it by restarting.
IP_SHIM = """#!/usr/bin/env bash
echo "ip $*" >> "$WATCHDOG_ACTIONS"
if [ "$1" = "-4" ]; then
    if [ -f "$RESTART_MARKER" ]; then
        echo "    inet 100.64.0.1/32 scope global tailscale0"
    fi
fi
exit 0
"""

# Creates $RESTART_MARKER on `restart tailscaled` unless $FAKE_RESTART_FIXES=0
# (simulating a restart that doesn't actually restore the address).
SYSTEMCTL_SHIM = """#!/usr/bin/env bash
echo "systemctl $*" >> "$WATCHDOG_ACTIONS"
if [ "$1" = "restart" ] && [ "${FAKE_RESTART_FIXES:-1}" = "1" ]; then
    touch "$RESTART_MARKER"
fi
exit 0
"""

CURL_SHIM = """#!/usr/bin/env bash
echo "curl $*" >> "$WATCHDOG_ACTIONS"
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway project layout with the real script and recording shims."""
    project_dir = tmp_path / "project"
    (project_dir / "scripts").mkdir(parents=True)
    script_path = project_dir / "scripts" / "network-watchdog.sh"
    shutil.copy(SCRIPT_SOURCE, script_path)
    script_path.chmod(0o755)

    env_file = project_dir / ".env"
    env_file.write_text(
        "LIFEOS_NETWORK_WATCHDOG_ENABLED=true\n"
        "TELEGRAM_BOT_TOKEN=xtoken\n"
        "TELEGRAM_CHAT_ID=123\n"
    )

    # Curated PATH: a shim dir (first) plus real copies of only the plain
    # utilities the script needs, so a real `tailscale` binary elsewhere on
    # this machine's PATH can never leak into the "CLI absent" scenario.
    fake_bin = tmp_path / "fake_bin"
    real_bin = tmp_path / "real_bin"
    fake_bin.mkdir()
    real_bin.mkdir()
    for name in (
        "date", "cat", "grep", "tr", "cut", "tail", "mkdir", "awk", "sleep",
        "bash", "dirname", "touch",
    ):
        real = shutil.which(name)
        assert real, f"{name} not found on test host PATH"
        (real_bin / name).symlink_to(real)

    for name, body in (
        ("nmcli", NMCLI_SHIM),
        ("ip", IP_SHIM),
        ("systemctl", SYSTEMCTL_SHIM),
        ("curl", CURL_SHIM),
    ):
        p = fake_bin / name
        p.write_text(body)
        p.chmod(0o755)

    actions_file = tmp_path / "actions.log"
    restart_marker = tmp_path / "restarted.marker"

    class Sandbox:
        def __init__(self):
            self.project_dir = project_dir
            self.actions_file = actions_file
            self.restart_marker = restart_marker
            self.cooldown_file = project_dir / "logs" / "network-watchdog.tailscale-restart"

        def with_tailscale(self):
            """Symlink the tailscale shim into PATH ('CLI present')."""
            (fake_bin / "tailscale").write_text(TAILSCALE_SHIM)
            (fake_bin / "tailscale").chmod(0o755)
            return self

        def run(self, *, state="Running", restart_fixes="1", extra_env=None):
            env = {
                "PATH": f"{fake_bin}:{real_bin}",
                "WATCHDOG_ACTIONS": str(actions_file),
                "RESTART_MARKER": str(restart_marker),
                "FAKE_TAILSCALE_STATE": state,
                "FAKE_RESTART_FIXES": restart_fixes,
                "LIFEOS_WATCHDOG_CURL": str(fake_bin / "curl"),
            }
            if extra_env:
                env.update(extra_env)
            return subprocess.run(
                ["bash", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        def actions(self):
            if not actions_file.exists():
                return []
            return actions_file.read_text().splitlines()

    return Sandbox()


def test_noop_when_tailscale_cli_absent(sandbox):
    """No `tailscale` binary on PATH at all — must not touch systemctl/curl."""
    result = sandbox.run()

    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert not any(a.startswith("systemctl restart") for a in actions)
    assert not any(a.startswith("curl") for a in actions)


def test_noop_when_tailscale_not_running(sandbox):
    sandbox.with_tailscale()
    result = sandbox.run(state="Stopped")

    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert not any(a.startswith("systemctl restart") for a in actions)
    assert not any(a.startswith("curl") for a in actions)


def test_noop_when_address_already_present(sandbox):
    sandbox.with_tailscale()
    sandbox.restart_marker.touch()  # ip shim reports an address immediately
    result = sandbox.run(state="Running")

    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert not any(a.startswith("systemctl restart") for a in actions)
    assert not any(a.startswith("curl") for a in actions)


def test_restarts_tailscaled_and_alerts_on_missing_address(sandbox):
    sandbox.with_tailscale()
    result = sandbox.run(state="Running")

    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert any(a.startswith("systemctl restart tailscaled") for a in actions)
    assert any(a.startswith("curl") for a in actions)
    assert sandbox.cooldown_file.exists()


def test_alerts_failure_when_restart_does_not_fix_address(sandbox):
    sandbox.with_tailscale()
    result = sandbox.run(state="Running", restart_fixes="0")

    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert any(a.startswith("systemctl restart tailscaled") for a in actions)
    assert any(a.startswith("curl") for a in actions)


def test_does_not_restart_again_within_cooldown(sandbox):
    sandbox.with_tailscale()
    first = sandbox.run(state="Running")
    assert first.returncode == 0, first.stderr

    restart_calls_after_first = sum(
        1 for a in sandbox.actions() if a.startswith("systemctl restart")
    )
    assert restart_calls_after_first == 1

    # Address is now present (the first run's "restart" fixed it via the
    # marker), but simulate it dropping again immediately — still within
    # the default cooldown, the second tick must not restart again.
    sandbox.restart_marker.unlink()
    second = sandbox.run(state="Running")
    assert second.returncode == 0, second.stderr

    restart_calls_after_second = sum(
        1 for a in sandbox.actions() if a.startswith("systemctl restart")
    )
    assert restart_calls_after_second == 1


def test_restarts_again_once_cooldown_elapses(sandbox):
    sandbox.with_tailscale()
    first = sandbox.run(state="Running", extra_env={"LIFEOS_NET_WATCHDOG_TAILSCALE_COOLDOWN_SECONDS": "0"})
    assert first.returncode == 0, first.stderr

    sandbox.restart_marker.unlink()
    second = sandbox.run(state="Running", extra_env={"LIFEOS_NET_WATCHDOG_TAILSCALE_COOLDOWN_SECONDS": "0"})
    assert second.returncode == 0, second.stderr

    restart_calls = sum(1 for a in sandbox.actions() if a.startswith("systemctl restart"))
    assert restart_calls == 2


def test_state_and_log_follow_the_root_owned_locations(sandbox, tmp_path):
    """Under the unit, state and log go to the configured dirs, not the checkout's logs/."""
    sandbox.with_tailscale()
    state_dir = tmp_path / "var_lib"
    log_file = tmp_path / "var_log" / "lifeos-network-watchdog.log"
    checkout = sandbox.project_dir
    result = sandbox.run(
        state="Running",
        extra_env={
            "LIFEOS_DIR": str(checkout),
            "LIFEOS_NET_WATCHDOG_STATE_DIR": str(state_dir),
            "LIFEOS_NET_WATCHDOG_LOG": str(log_file),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (state_dir / "network-watchdog.tailscale-restart").exists()
    assert "tailscaled" in log_file.read_text()
    assert not (checkout / "logs").exists()
