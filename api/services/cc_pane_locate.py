"""Locate the wezterm pane running a Claude Code session.

When a CC session was started organically (user opened a wezterm pane and
ran `claude`) rather than via /agents Resume, the session→pane mapping in
`data/cc_wezterm.db` is empty, so the Focus / Go To button has nothing to
target. This module reconstructs the mapping on demand by walking from the
session's transcript file back to the wezterm pane whose TTY it lives on.

Strategy (Linux-only, uses `/proc`):

1. `lsof -t -- <transcript_path>` → PIDs that have the jsonl open. For an
   active CC session, the `claude` process holds it.
2. For each PID, read `/proc/<pid>/fd/0` to recover the controlling TTY
   (e.g. `/dev/pts/7`). `claude` is interactive so fd 0 points at the pts.
3. `wezterm cli list --format json` → each pane carries a `tty_name`
   (`/dev/pts/N`). Match on tty_name.

Cwd alone cannot disambiguate when multiple panes share a project — the
transcript path can, because every CC session writes to its own jsonl,
and the jsonl's holder is bound to exactly one pty.

Note: wezterm's JSON output does not expose pane.pid, but does expose
pane.tty_name reliably across versions, so the probe is keyed on TTY.

macOS would replace step 2 with `ttyname()` via `/dev/fd/0` (no /proc);
out of scope for now — LifeOS is Linux-targeted on the host running /agents.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


_LSOF_TIMEOUT = 2.0
_WEZTERM_LIST_TIMEOUT = 2.0


def _lsof_pids(transcript_path: str) -> set[int]:
    """Return the set of PIDs that currently have `transcript_path` open.

    Uses `lsof -t -- <path>` which prints one PID per line. Missing lsof,
    timeouts, and rc=1 (no holders) all collapse to an empty set. Never
    raises.
    """
    lsof_bin = shutil.which("lsof")
    if not lsof_bin:
        logger.debug("lsof not found on PATH — pane probe unavailable")
        return set()
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [lsof_bin, "-t", "--", transcript_path],
            capture_output=True,
            timeout=_LSOF_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("lsof probe failed for %s: %s", transcript_path, exc)
        return set()
    pids: set[int] = set()
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.add(int(line))
        except ValueError:
            continue
    return pids


def _tty_of_pid(pid: int, proc_root: str = "/proc") -> Optional[str]:
    """Resolve `/proc/<pid>/fd/0` to its target — for an interactive
    process this is `/dev/pts/N`. Returns the path string or None if the
    fd is gone (process exited) / not a tty / permission denied.
    """
    try:
        target = os.readlink(f"{proc_root}/{pid}/fd/0")
    except OSError:
        return None
    # fd/0 may point at a regular file if stdin was redirected, in which
    # case it is irrelevant to pane matching. Filter to actual pts paths.
    if not target.startswith("/dev/pts/"):
        return None
    return target


def _wezterm_panes(env: Optional[dict[str, str]] = None) -> list[dict]:
    """Run `wezterm cli list --format json`, return the decoded pane list.

    Empty list on any failure (wezterm missing, socket unavailable, JSON
    malformed). Never raises.
    """
    wezterm_bin = shutil.which("wezterm")
    if not wezterm_bin:
        return []
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [wezterm_bin, "cli", "list", "--format", "json"],
            env=env if env is not None else os.environ.copy(),
            capture_output=True,
            timeout=_WEZTERM_LIST_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("wezterm cli list failed: %s", exc)
        return []
    if proc.returncode != 0:
        return []
    try:
        decoded = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def locate_pane_for_transcript(
    transcript_path: str,
    env: Optional[dict[str, str]] = None,
    proc_root: str = "/proc",
) -> Optional[int]:
    """Probe-time pane resolver: returns the wezterm pane_id whose TTY
    matches the controlling TTY of any process holding `transcript_path`.

    Returns None if the file isn't open (session ended), wezterm isn't
    running, or no pane's tty_name intersects the lsof PIDs' TTYs.

    Pure read: no side effects, no exceptions.
    """
    if not transcript_path:
        return None

    holder_pids = _lsof_pids(transcript_path)
    if not holder_pids:
        return None

    # Resolve each holder's controlling TTY. Multiple holders are typical
    # when `claude` has forked workers — they all share the parent's pty,
    # so any match is sufficient.
    holder_ttys: set[str] = set()
    for pid in holder_pids:
        tty = _tty_of_pid(pid, proc_root=proc_root)
        if tty:
            holder_ttys.add(tty)

    if not holder_ttys:
        return None

    panes = _wezterm_panes(env=env)
    if not panes:
        return None

    # First matching pane wins. Each pty maps to exactly one wezterm pane.
    for pane in panes:
        if not isinstance(pane, dict):
            continue
        pane_tty = pane.get("tty_name")
        pane_id = pane.get("pane_id")
        if not isinstance(pane_tty, str) or not isinstance(pane_id, int):
            continue
        if pane_tty in holder_ttys:
            return pane_id

    return None
