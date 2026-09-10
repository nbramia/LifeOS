#!/usr/bin/env python3
"""Run one command with a bounded lifetime and inherited standard streams."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def main(command: list[str]) -> int:
    if not command:
        return 127

    child: subprocess.Popen[object] | None = None

    def stop_child(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Reap it now rather than leaving a zombie for the parent process
            # to inherit and orphan once this interpreter exits — the timeout
            # path below already does this after its own kill; a signal
            # shouldn't be handled less carefully than a plain timeout.
            child.wait()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, stop_child)
    signal.signal(signal.SIGTERM, stop_child)
    try:
        child = subprocess.Popen(command, start_new_session=True)
    except FileNotFoundError:
        return 127

    try:
        returncode = child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return 124
    finally:
        # A shell leader can exit successfully after forking a background
        # process which inherited this command's captured stdout.  This
        # process group belongs only to this invocation, so reap any such
        # descendant before handing the pipe back to the caller.
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return 128 - returncode if returncode < 0 else returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
