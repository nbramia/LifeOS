#!/usr/bin/env python3
"""
Generic supervised-exec wrapper: runs an arbitrary command in the SAME
process group as this wrapper — which the caller (``run_supervised_command``)
always spawns with ``start_new_session=True``, so that group's id equals
this wrapper's own pid — and reaps that whole group on every exit path:
normal completion, a failing/raising command, owner death (the liveness
pipe's write end closing, which the kernel does automatically when every
process holding it exits, for any reason including a SIGKILL), AND a direct
kill of this wrapper *by the caller* (which can't run this process's own
`finally` at all, since SIGKILL isn't catchable) — the caller can reach the
exact same group independently, because it already knows this wrapper's pid
and that pid IS the group id. No side needs to ask the OS for the other's
process group at cleanup time (which would fail once a process is already
reaped); both sides always use the one id fixed at spawn time.

Reaping the whole group (not just the direct child) is what catches a
command that spawns its own background descendant and then exits normally
— without this, that descendant would outlive both the command and this
wrapper. It does NOT reach a descendant that calls `setsid`/detaches into
its own new process group; nothing here claims coverage of that case.

A caller treating this as a capacity/resource lease should consider the
lease held until the group is actually confirmed gone, not merely until its
own call into ``run_supervised_command`` returns — a caller that force-kills
this wrapper is itself now responsible for the group-kill, exactly as
``run_supervised_command`` does in its own exception path.

Not launched directly by a human — spawned by
``scripts/test_instance.run_supervised_command``.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading

import psutil


def _kill_own_group() -> None:
    """Kill every process in this wrapper's group, including this one — only
    appropriate when this process is about to exit anyway; the owner-death
    path does not use this process's exit code."""
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass


def _kill_other_group_members() -> None:
    """Kill every OTHER process sharing this wrapper's group — i.e. reap a
    leftover descendant the command spawned and left behind — without
    touching this process itself. Used on the normal-exit path, where this
    process still needs to return the command's real exit code afterward;
    `os.killpg` targets the whole group indiscriminately (including the
    caller of it), which would SIGKILL this process before it could ever
    report that code.
    """
    my_pid = os.getpid()
    my_pgid = os.getpgrp()
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        if pid == my_pid:
            continue
        try:
            if os.getpgid(pid) == my_pgid:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError, psutil.NoSuchProcess):
            pass


def _watch(fd: int) -> None:
    try:
        os.read(fd, 1)  # blocks until EOF when every writer (our owner) exits
    except OSError:
        pass
    _kill_own_group()
    os._exit(1)  # reached only if killpg somehow didn't also kill this process


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-liveness-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    threading.Thread(
        target=_watch, args=(args.owner_liveness_fd,),
        daemon=True, name="lifeos-supervised-exec-owner-liveness",
    ).start()
    # Deliberately no start_new_session here: the command (and anything IT
    # spawns without detaching) stays in THIS process's group, which the
    # caller already set up as its own session via start_new_session=True —
    # so both this wrapper's own cleanup below and the caller's fallback
    # cleanup (if it has to kill this wrapper outright) reach the same group.
    proc = subprocess.Popen(command)
    try:
        returncode = proc.wait()
    finally:
        # Reached on normal completion, a raised exception, or Ctrl-C —
        # every path except owner-death (which _watch already handles and
        # then exits this whole process before returning here). Uses the
        # self-excluding variant: this process still needs to actually
        # return `returncode` below, which `_kill_own_group` would prevent
        # by SIGKILLing this process along with everything else.
        _kill_other_group_members()
    return returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
