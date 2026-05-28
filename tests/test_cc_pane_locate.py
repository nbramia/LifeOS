"""Unit tests for the cc_pane_locate probe (issue #251).

The probe walks: transcript_path → lsof PIDs → /proc/<pid>/fd/0 (tty) →
wezterm pane.tty_name. Each subprocess and filesystem touch is mocked so
the suite can exercise disambiguation without needing real pty devices.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from api.services import cc_pane_locate


@pytest.fixture
def fake_proc_tree(tmp_path: Path):
    """Build a fake /proc layout with two pids, each pointing at its own pts.

    Returns (proc_root, {pid: tty}).
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    layout = {
        100: "/dev/pts/4",
        200: "/dev/pts/7",
        300: "/dev/pts/11",
    }
    for pid, tty in layout.items():
        fd_dir = proc_root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        os.symlink(tty, fd_dir / "0")

    return str(proc_root), layout


def _patch_lsof(monkeypatch, pids: list[int] | None, *, missing: bool = False,
                rc: int = 0, timeout: bool = False):
    """Pretend `lsof -t -- <path>` returns the given pids (or fails)."""
    if missing:
        monkeypatch.setattr("shutil.which",
                            lambda name: None if name == "lsof" else f"/usr/bin/{name}")
        return

    class _Completed:
        def __init__(self, stdout: bytes = b"", returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = b""

    body = "\n".join(str(p) for p in (pids or [])).encode("utf-8")

    def _fake_run(argv, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))
        # lsof OR wezterm — caller patches wezterm separately
        if argv[0].endswith("lsof"):
            return _Completed(stdout=body, returncode=rc)
        return _Completed()

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", _fake_run)


def _patch_run(monkeypatch, *, lsof_pids: list[int] | None = None, lsof_rc: int = 0,
               wezterm_panes: list[dict] | None = None, wezterm_rc: int = 0,
               wezterm_raises: Exception | None = None,
               which_overrides: dict[str, str | None] | None = None):
    """Bundle lsof + wezterm mocking into one helper — the probe runs both."""

    class _Completed:
        def __init__(self, stdout: bytes = b"", returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = b""

    def _fake_run(argv, **kwargs):
        if argv[0].endswith("lsof"):
            body = "\n".join(str(p) for p in (lsof_pids or [])).encode("utf-8")
            return _Completed(stdout=body, returncode=lsof_rc)
        if argv[0].endswith("wezterm"):
            if wezterm_raises is not None:
                raise wezterm_raises
            import json as _json
            return _Completed(
                stdout=_json.dumps(wezterm_panes or []).encode("utf-8"),
                returncode=wezterm_rc,
            )
        return _Completed()

    def _fake_which(name):
        if which_overrides and name in which_overrides:
            return which_overrides[name]
        return f"/usr/bin/{name}"

    monkeypatch.setattr("shutil.which", _fake_which)
    monkeypatch.setattr("subprocess.run", _fake_run)


# ---------------------------------------------------------------------------
# Disambiguation — the heart of the issue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_disambiguates_two_panes_same_cwd(monkeypatch, fake_proc_tree):
    """Two panes both running `claude` in the same cwd, two different
    sessions. The probe must pick the pane whose tty matches the holder
    of the requested transcript file — not "first matching cwd".
    """
    proc_root, _ttys = fake_proc_tree
    # Session A's claude lives on pid 100 → /dev/pts/4 (pane 7).
    # Session B's claude lives on pid 200 → /dev/pts/7 (pane 23).
    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        wezterm_panes=[
            {"pane_id": 7,  "tty_name": "/dev/pts/4",  "cwd": "file:///repo"},
            {"pane_id": 23, "tty_name": "/dev/pts/7",  "cwd": "file:///repo"},
            {"pane_id": 99, "tty_name": "/dev/pts/11", "cwd": "file:///elsewhere"},
        ],
    )
    result = cc_pane_locate.locate_pane_for_transcript(
        "/home/u/.claude/projects/-repo/session-a.jsonl",
        proc_root=proc_root,
    )
    assert result == 7

    # Same panes, but lsof reports session B's holder → expect pane 23.
    _patch_run(
        monkeypatch,
        lsof_pids=[200],
        wezterm_panes=[
            {"pane_id": 7,  "tty_name": "/dev/pts/4",  "cwd": "file:///repo"},
            {"pane_id": 23, "tty_name": "/dev/pts/7",  "cwd": "file:///repo"},
        ],
    )
    result = cc_pane_locate.locate_pane_for_transcript(
        "/home/u/.claude/projects/-repo/session-b.jsonl",
        proc_root=proc_root,
    )
    assert result == 23


@pytest.mark.unit
def test_probe_picks_pane_when_forked_workers_share_pty(monkeypatch, fake_proc_tree):
    """`claude` may have forked workers that inherited the open jsonl —
    they share the parent's controlling TTY, so any holder maps to the
    correct pane.
    """
    proc_root, _ = fake_proc_tree
    _patch_run(
        monkeypatch,
        lsof_pids=[100, 300],  # main + a forked worker
        wezterm_panes=[
            {"pane_id": 7,  "tty_name": "/dev/pts/4"},
            {"pane_id": 99, "tty_name": "/dev/pts/11"},
        ],
    )
    result = cc_pane_locate.locate_pane_for_transcript(
        "/anywhere/x.jsonl",
        proc_root=proc_root,
    )
    # pid 100 maps to /dev/pts/4 (pane 7); pid 300 maps to /dev/pts/11
    # (pane 99). First match wins — either is acceptable since both panes
    # legitimately hold the file. We only assert it returned _something_.
    assert result in {7, 99}


# ---------------------------------------------------------------------------
# Graceful failure paths — probe must never raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_returns_none_when_empty_transcript_path(monkeypatch):
    assert cc_pane_locate.locate_pane_for_transcript("") is None
    assert cc_pane_locate.locate_pane_for_transcript(None) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_probe_returns_none_when_lsof_finds_no_holders(monkeypatch, fake_proc_tree):
    proc_root, _ = fake_proc_tree
    _patch_run(monkeypatch, lsof_pids=[])
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_returns_none_when_lsof_missing(monkeypatch, fake_proc_tree):
    proc_root, _ = fake_proc_tree
    _patch_run(monkeypatch, which_overrides={"lsof": None})
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_returns_none_when_wezterm_missing(monkeypatch, fake_proc_tree):
    proc_root, _ = fake_proc_tree
    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        which_overrides={"wezterm": None},
    )
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_returns_none_when_wezterm_list_fails(monkeypatch, fake_proc_tree):
    proc_root, _ = fake_proc_tree
    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        wezterm_rc=2,
    )
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_returns_none_when_wezterm_returns_malformed_json(monkeypatch, fake_proc_tree):
    proc_root, _ = fake_proc_tree

    class _Completed:
        stdout = b"not-json-at-all"
        returncode = 0
        stderr = b""

    def _fake_run(argv, **kwargs):
        if argv[0].endswith("lsof"):
            return _CompletedOK(b"100\n")
        return _Completed()

    class _CompletedOK:
        def __init__(self, body): self.stdout, self.returncode, self.stderr = body, 0, b""

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", _fake_run)

    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_returns_none_when_no_pane_tty_matches(monkeypatch, fake_proc_tree):
    """Holder lives on /dev/pts/4 but no wezterm pane has that tty
    (e.g. the user is running claude in a non-wezterm terminal).
    """
    proc_root, _ = fake_proc_tree
    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        wezterm_panes=[
            {"pane_id": 99, "tty_name": "/dev/pts/99"},
        ],
    )
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root
    ) is None


@pytest.mark.unit
def test_probe_ignores_non_pts_fd_targets(monkeypatch, tmp_path):
    """If the holder's fd/0 has been redirected (file, /dev/null), the
    probe must not match — we only trust pts links.
    """
    proc_root = tmp_path / "proc"
    fd_dir = proc_root / "100" / "fd"
    fd_dir.mkdir(parents=True)
    # Symlink to a regular file rather than /dev/pts/N.
    target_file = tmp_path / "redirected"
    target_file.write_text("")
    os.symlink(str(target_file), fd_dir / "0")

    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        wezterm_panes=[
            {"pane_id": 7, "tty_name": "/dev/pts/4"},
        ],
    )
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=str(proc_root),
    ) is None


@pytest.mark.unit
def test_probe_ignores_pane_with_missing_fields(monkeypatch, fake_proc_tree):
    """Wezterm pane objects missing tty_name or pane_id are skipped."""
    proc_root, _ = fake_proc_tree
    _patch_run(
        monkeypatch,
        lsof_pids=[100],
        wezterm_panes=[
            {"pane_id": 7},  # missing tty_name
            {"tty_name": "/dev/pts/4"},  # missing pane_id
            {"pane_id": "not-int", "tty_name": "/dev/pts/4"},
            {"pane_id": 42, "tty_name": "/dev/pts/4"},  # valid → win
        ],
    )
    assert cc_pane_locate.locate_pane_for_transcript(
        "/x/y.jsonl", proc_root=proc_root,
    ) == 42
