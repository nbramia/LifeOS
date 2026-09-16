"""Verifies the conftest-level guard that blocks real `claude`/`codex` spawns.

A test that reaches an agent-worker dispatch path without an injected executor
can spawn a live CLI session — billed, running with
`--dangerously-skip-permissions`, and pointed at whatever vault path survives
fixture teardown. The guard installed in `tests/conftest.py` patches
`subprocess.Popen` to fail any test that execs one of those binaries. This
module is the canary that proves the guard works: the marker
`allow_cli_spawn` lets a deliberate spawn through, and the unmarked tests
confirm everything else is blocked.

Nothing here ever executes a CLI — the guard raises before exec, which is the
property being asserted.
"""
from __future__ import annotations

import subprocess
import threading

import pytest

pytestmark = pytest.mark.unit

_GUARD_MESSAGE = "attempted to spawn the real"


def test_guard_blocks_a_claude_spawn():
    """An unmarked test must not be able to exec the `claude` CLI."""
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        subprocess.Popen(["claude", "-p", "canary"])


def test_guard_blocks_a_codex_spawn():
    """`codex` is banned on the same terms as `claude`."""
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        subprocess.Popen(["codex", "exec", "canary"])


def test_guard_blocks_an_absolute_path_to_the_binary():
    """The ban keys on the argv0 basename, so a full path is still caught."""
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        subprocess.Popen(["/usr/local/bin/claude", "-p", "canary"])


def test_guard_blocks_a_string_command():
    """A string argv (shell form) resolves to the same basename check."""
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        subprocess.Popen("claude -p canary", shell=True)


def test_guard_blocks_a_spawn_from_a_background_thread():
    """The worker dispatches CLI sessions from a pool thread, so the guard
    has to hold there too — a spawn off the main thread is the path that
    reaches production code under `-n auto`."""
    outcome: dict[str, str] = {}

    def attempt() -> None:
        try:
            subprocess.Popen(["claude", "-p", "canary"])
        except RuntimeError as exc:
            outcome["result"] = "blocked" if _GUARD_MESSAGE in str(exc) else "other"
        except Exception:  # noqa: BLE001 - any other failure is still a spawn attempt
            outcome["result"] = "other"
        else:
            outcome["result"] = "spawned"

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "spawn attempt hung"
    assert outcome.get("result") == "blocked"


def test_guard_allows_an_unrelated_binary():
    """The ban is targeted: ordinary subprocesses still run."""
    completed = subprocess.run(
        ["echo", "canary"], capture_output=True, text=True, check=True,
    )
    assert completed.stdout.strip() == "canary"


@pytest.mark.allow_cli_spawn
def test_marker_bypasses_the_guard():
    """`@pytest.mark.allow_cli_spawn` is the documented opt-in. The spawn is
    pointed at a path that cannot exist, so the guard's absence surfaces as an
    OS-level failure rather than a live session."""
    with pytest.raises(OSError) as exc_info:
        subprocess.Popen(["/nonexistent/claude", "-p", "canary"])

    assert _GUARD_MESSAGE not in str(exc_info.value)
