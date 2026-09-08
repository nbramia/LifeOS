"""Tests for `api/services/agent_transcript_mirror.py`: the incremental
ssh+rsync pull of each registered host's Claude Code/Codex transcripts.

No real ssh/rsync is invoked. `_FakeRsyncRunner` stands in for the injectable
`runner` — it records every invocation's argv (proving the shape of the real
command) and performs a minimal, mtime-aware `*.jsonl` copy from a plain
local "remote" directory into the mirror destination, so the copy/skip
behavior can be asserted on real files without a network. Fixtures are
synthetic per repo convention.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from api.services import agent_transcript_mirror as mirror


pytestmark = pytest.mark.unit


class _FakeRsyncRunner:
    """Records argv and performs a rsync-`-rt`-like incremental *.jsonl
    copy (skip if dest is newer/equal-size). `fail_hosts` names ssh targets
    (the string before the first `:` in the source spec) that should
    simulate an unreachable host instead of copying.

    This HONORS the argv: a short-option bundle carrying `t` (e.g. `-rtz`)
    is what makes the fake preserve mtime on copy (`shutil.copy2`, mirroring
    real rsync's `-t`) and therefore lets a later tick detect "unchanged" at
    all; without it (or with `--ignore-times`/`--whole-file`, which force a
    full re-transfer in real rsync), every file is always re-copied.
    """

    def __init__(self, fail_hosts: frozenset = frozenset()):
        self.calls: list[list[str]] = []
        self.copied_by_call: list[list[str]] = []
        self.fail_hosts = fail_hosts

    def __call__(self, argv: list[str]) -> "subprocess.CompletedProcess":
        self.calls.append(argv)
        remote_spec = argv[-2]
        dst = Path(argv[-1])
        target, remote_dir = remote_spec.split(":", 1)
        remote_dir = remote_dir.rstrip("/")

        if target in self.fail_hosts:
            self.copied_by_call.append([])
            return subprocess.CompletedProcess(
                args=argv, returncode=255, stdout=b"",
                stderr=b"ssh: connect to host x port 22: Connection refused\n",
            )

        has_dash_t = any(
            a.startswith("-") and not a.startswith("--") and "t" in a[1:] for a in argv
        )
        preserves_mtime = (
            has_dash_t and "--ignore-times" not in argv and "--whole-file" not in argv
        )

        copied: list[str] = []
        src_root = Path(remote_dir)
        if src_root.exists():
            for jsonl in sorted(src_root.rglob("*.jsonl")):
                rel = jsonl.relative_to(src_root)
                dest_file = dst / rel
                if (
                    preserves_mtime
                    and dest_file.exists()
                    and dest_file.stat().st_mtime >= jsonl.stat().st_mtime
                    and dest_file.stat().st_size == jsonl.stat().st_size
                ):
                    continue  # unchanged — rsync's quick check would skip this
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                if preserves_mtime:
                    shutil.copy2(jsonl, dest_file)  # copy2 preserves mtime, like rsync -t
                else:
                    shutil.copy(jsonl, dest_file)  # no -t — mtime not preserved, forces re-copy next tick
                copied.append(str(rel))
        self.copied_by_call.append(copied)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


def _configure(monkeypatch, tmp_path, cc_dir, cx_dir, agent_hosts, this_host="api-host"):
    from config.settings import settings
    from api.services.agent_worker import remote_spawn

    monkeypatch.setattr(settings, "claude_code_projects_dir", str(cc_dir), raising=False)
    monkeypatch.setattr(settings, "codex_sessions_dir", str(cx_dir), raising=False)
    monkeypatch.setattr(settings, "agent_hosts", agent_hosts, raising=False)
    monkeypatch.setattr(settings, "agent_transcript_mirror_dir", str(tmp_path / "mirror"), raising=False)
    monkeypatch.setattr(settings, "agent_ssh_connect_timeout", 5, raising=False)
    monkeypatch.setattr(remote_spawn, "api_host_name", lambda: this_host)


# ---------------------------------------------------------------------------
# argv shape
# ---------------------------------------------------------------------------


def test_rsync_argv_is_readonly_incremental_pull_to_correct_dest(tmp_path, monkeypatch):
    cc_dir = tmp_path / "remote-claude-projects"
    cx_dir = tmp_path / "remote-codex-sessions"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=runner)

    assert result.ok is True
    assert len(runner.calls) == 2  # one rsync invocation per engine

    cc_dst, cx_dst = mirror.host_dirs("laptop")
    for argv, remote_dir, dst_dir in zip(runner.calls, (cc_dir, cx_dir), (cc_dst, cx_dst)):
        assert argv[0] == "rsync"
        assert "-rtz" in argv
        joined = " ".join(argv)
        assert "BatchMode=yes" in joined
        assert "ConnectTimeout=5" in joined
        assert "--delete" not in argv  # never delete on the remote side (pull, not sync)
        # Source is the ssh target + the settings path (passed through
        # as-is — the remote shell, not this process, expands `~`).
        assert argv[-2] == f"user@laptop.example:{remote_dir}/"
        assert argv[-1] == f"{dst_dir}/"


def test_rsync_argv_has_separator_before_positional_operands(tmp_path, monkeypatch):
    """Round-1 finding #15: `--` must precede the source/dest so an
    `agent_hosts` VALUE beginning with `-` can't be parsed by rsync as an
    option rather than a positional operand."""
    cc_dir = tmp_path / "remote-claude-projects"
    cx_dir = tmp_path / "remote-codex-sessions"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)

    for argv in runner.calls:
        assert argv[-3] == "--"


def test_rsync_argv_never_pushes_local_to_remote(tmp_path, monkeypatch):
    """The destination (last argv element) is always a LOCAL path under the
    mirror root, never the ssh target — this is a pull, never a push."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)

    for argv in runner.calls:
        assert "user@laptop.example" not in argv[-1]
        assert str(mirror.mirror_root()) in argv[-1]


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_copy_lands_under_per_host_engine_dirs(tmp_path, monkeypatch):
    cc_dir = tmp_path / "remote-claude-projects"
    (cc_dir / "-home-user-proj").mkdir(parents=True)
    (cc_dir / "-home-user-proj" / "abc.jsonl").write_text('{"type": "user"}\n')
    cx_dir = tmp_path / "remote-codex-sessions" / "2026" / "08" / "01"
    cx_dir.mkdir(parents=True)
    (cx_dir / "rollout-2026-08-01T00-00-00-xyz.jsonl").write_text('{"type": "session_meta"}\n')
    _configure(monkeypatch, tmp_path, tmp_path / "remote-claude-projects",
               tmp_path / "remote-codex-sessions", {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=runner)
    assert result.ok is True

    cc_dst, cx_dst = mirror.host_dirs("laptop")
    assert cc_dst == mirror.mirror_root() / "laptop" / "claude_code"
    assert cx_dst == mirror.mirror_root() / "laptop" / "codex"

    cc_file = cc_dst / "-home-user-proj" / "abc.jsonl"
    assert cc_file.exists()
    assert cc_file.read_text() == '{"type": "user"}\n'

    cx_file = cx_dst / "2026" / "08" / "01" / "rollout-2026-08-01T00-00-00-xyz.jsonl"
    assert cx_file.exists()
    assert cx_file.read_text() == '{"type": "session_meta"}\n'


# ---------------------------------------------------------------------------
# skip-unchanged
# ---------------------------------------------------------------------------


def test_second_tick_does_not_retransfer_unchanged_file(tmp_path, monkeypatch):
    cc_dir = tmp_path / "remote-claude-projects"
    (cc_dir / "proj").mkdir(parents=True)
    (cc_dir / "proj" / "s1.jsonl").write_text("{}\n")
    cx_dir = tmp_path / "remote-codex-sessions"
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)
    first_cc_copied = runner.copied_by_call[0]
    assert first_cc_copied == ["proj/s1.jsonl"]  # positive: copied on tick 1

    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)
    second_cc_copied = runner.copied_by_call[2]  # [cc1, cx1, cc2, cx2]
    assert second_cc_copied == []  # positive: nothing copied on tick 2

    # The mirrored file is still byte-identical to the source — proves
    # tick 2 didn't disturb it (e.g. via a truncate-then-fail).
    cc_dst, _cx_dst = mirror.host_dirs("laptop")
    dest = cc_dst / "proj" / "s1.jsonl"
    src = cc_dir / "proj" / "s1.jsonl"
    assert dest.read_text() == src.read_text()

    # Both ticks used rsync's ordinary quick-check flags — no
    # --whole-file/--ignore-times that would force a full retransfer.
    for argv in runner.calls:
        assert "--whole-file" not in argv
        assert "--ignore-times" not in argv


def test_changed_file_is_retransferred(tmp_path, monkeypatch):
    cc_dir = tmp_path / "remote-claude-projects"
    (cc_dir / "proj").mkdir(parents=True)
    src = cc_dir / "proj" / "s1.jsonl"
    src.write_text("{}\n")
    cx_dir = tmp_path / "remote-codex-sessions"
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})
    runner = _FakeRsyncRunner()

    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)

    time.sleep(1.05)  # ensure a distinct mtime second (filesystem mtime resolution)
    src.write_text('{"type": "user"}\n')
    mirror.mirror_host("laptop", "user@laptop.example", runner=runner)

    second_cc_copied = runner.copied_by_call[2]
    assert second_cc_copied == ["proj/s1.jsonl"]  # positive: re-copied because it changed


# ---------------------------------------------------------------------------
# unreachable host
# ---------------------------------------------------------------------------


def test_unreachable_host_logs_one_line_others_still_mirror(tmp_path, monkeypatch, caplog):
    remote_root = tmp_path / "remote"
    cc_dir = remote_root / "cc"
    (cc_dir / "proj").mkdir(parents=True)
    (cc_dir / "proj" / "a.jsonl").write_text("{}\n")
    cx_dir = remote_root / "cx"
    cx_dir.mkdir()
    _configure(
        monkeypatch, tmp_path, cc_dir, cx_dir,
        {"deadhost": "user@deadhost.example", "goodhost": "user@goodhost.example"},
    )
    runner = _FakeRsyncRunner(fail_hosts=frozenset({"user@deadhost.example"}))
    caplog.set_level(logging.WARNING, logger="api.services.agent_transcript_mirror")

    results = mirror.mirror_once(runner=runner)

    assert results["deadhost"].ok is False
    assert results["goodhost"].ok is True

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "deadhost" in r.getMessage()]
    assert len(warnings) == 1
    assert "Connection refused" in warnings[0].getMessage()

    # goodhost's own pull actually completed with real files — proves the
    # dead host didn't break or block it.
    good_cc_dst, _ = mirror.host_dirs("goodhost")
    assert (good_cc_dst / "proj" / "a.jsonl").exists()


def test_unreachable_host_result_is_failed_and_no_exception_raised(tmp_path, monkeypatch):
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"deadhost": "user@deadhost.example"})
    runner = _FakeRsyncRunner(fail_hosts=frozenset({"user@deadhost.example"}))

    result = mirror.mirror_host("deadhost", "user@deadhost.example", runner=runner)
    assert result.ok is False
    assert result.error  # non-empty, carries the stderr tail


# ---------------------------------------------------------------------------
# rsync exit codes 23/24 — expected during a live write, not real failures
# ---------------------------------------------------------------------------


def test_rsync_exit_23_is_treated_as_success(tmp_path, monkeypatch):
    """Round-1 finding #6: exit 23 ("partial transfer due to error" —
    typically a non-regular file this pull's --include/--exclude already
    filters out) means the transfer that mattered still succeeded."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    def _partial_runner(argv):
        return subprocess.CompletedProcess(args=argv, returncode=23, stdout=b"", stderr=b"")

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=_partial_runner)
    assert result.ok is True


def test_rsync_exit_24_is_treated_as_success(tmp_path, monkeypatch):
    """Exit 24 ("partial transfer due to vanished source files") is the
    EXPECTED outcome of pulling a transcript a live CLI is actively
    writing to on the remote end — not a real failure."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    def _vanished_runner(argv):
        return subprocess.CompletedProcess(args=argv, returncode=24, stdout=b"", stderr=b"")

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=_vanished_runner)
    assert result.ok is True


def test_rsync_exit_other_than_23_24_still_fails(tmp_path, monkeypatch):
    """Guards against over-correction: only 23 and 24 are treated as
    success — every other non-zero code is still a real failure."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    def _real_failure_runner(argv):
        return subprocess.CompletedProcess(
            args=argv, returncode=12, stdout=b"",
            stderr=b"rsync: error in rsync protocol data stream (code 12)\n",
        )

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=_real_failure_runner)
    assert result.ok is False


# ---------------------------------------------------------------------------
# Diagnostic stderr line — skip rsync's own generic trailer
# ---------------------------------------------------------------------------


def test_diagnostic_stderr_line_prefers_ssh_error_over_rsync_trailer(tmp_path, monkeypatch):
    """Round-1 finding #7: `last_nonempty_line` alone always picked rsync's
    own generic trailer, byte-identical across NXDOMAIN, connection-refused,
    and unroutable-address failures. The first non-trailer stderr line
    (ssh's own diagnostic) is preferred."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    stderr = (
        b"ssh: connect to host laptop.example port 22: Connection timed out\n"
        b"rsync: connection unexpectedly closed (0 bytes received so far) [Receiver]\n"
        b"rsync error: unexplained error (code 255) at io.c(232) [Receiver=3.2.7]\n"
    )

    def _timeout_runner(argv):
        return subprocess.CompletedProcess(args=argv, returncode=255, stdout=b"", stderr=stderr)

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=_timeout_runner)
    assert result.ok is False
    assert "Connection timed out" in result.error
    assert "rsync error:" not in result.error


def test_diagnostic_stderr_line_falls_back_to_trailer_when_nothing_else(tmp_path, monkeypatch):
    """When stderr is ONLY the rsync trailer (no preceding ssh line at
    all), that trailer is still surfaced rather than an empty error."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    stderr = b"rsync error: unexplained error (code 255) at io.c(232) [Receiver=3.2.7]\n"

    def _trailer_only_runner(argv):
        return subprocess.CompletedProcess(args=argv, returncode=255, stdout=b"", stderr=stderr)

    result = mirror.mirror_host("laptop", "user@laptop.example", runner=_trailer_only_runner)
    assert result.ok is False
    assert "rsync error:" in result.error


def test_diagnostic_stderr_line_skips_ssh_known_hosts_warning():
    """`_SSH_KNOWN_HOSTS_WARNING_RE` is in `_diagnostic_stderr_line`'s skip
    condition so ssh's benign first-connect notice ("Warning: Permanently
    added ... to the list of known hosts.") never gets picked as the
    'diagnostic' line ahead of the real ssh error right after it, which
    would otherwise bury the actual failure behind noise on every
    first-connect tick. Mutation-proved: dropping the
    `_SSH_KNOWN_HOSTS_WARNING_RE.match(...)` disjunct from the skip
    condition makes this return the known-hosts line instead."""
    stderr = (
        "Warning: Permanently added '127.0.0.1' (ED25519) to the list of known hosts.\n"
        "ssh: connect to host laptop.example port 22: Connection refused\n"
        "rsync error: unexplained error (code 255) at io.c(232) [Receiver=3.2.7]\n"
    )

    diag = mirror._diagnostic_stderr_line(stderr)

    assert diag == "ssh: connect to host laptop.example port 22: Connection refused"


@pytest.fixture
def _reset_source_dir_warned():
    """`_source_dir_warned` is a never-cleared module
    global that accumulates (host, engine, message) keys for the life of
    the process — without resetting it before AND after a test that
    exercises the once-per-triple WARNING, the assertion becomes
    order-dependent under `-n auto --dist loadscope` (another test in the
    same worker could have already warned for the identical triple, or
    could run afterward and be silently starved of its own first
    warning)."""
    mirror._source_dir_warned.clear()
    yield
    mirror._source_dir_warned.clear()


def test_rsync_exit_23_with_unreadable_source_dir_warns_once_then_stays_quiet(
    tmp_path, monkeypatch, caplog, _reset_source_dir_warned,
):
    """rc-23/24 debug logging plus a once-per-(host, engine, message)
    WARNING fires when the diagnostic line specifically names an
    unreadable source directory. Neither of the other rc-23/24 tests
    catches a regression here: both pass `stderr=b""`, so `diag` is empty
    and the whole block is skipped. Mutation-proved: replacing the
    `if rc in (23, 24): ...` body with `pass` leaves this failing (no
    DEBUG, no WARNING on either call) while the rest of the suite stays
    green.

    Only the codex engine's pull fails here (the runner keys off the
    remote source dir in argv) so the assertions can pin an exact WARNING
    count of one per `mirror_host` call, not two."""
    cc_dir = tmp_path / "remote-claude-projects"
    cx_dir = tmp_path / "remote-codex-sessions"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"laptop": "user@laptop.example"})

    stderr = (
        b'rsync: change_dir "/home/user/.codex/sessions" failed: '
        b"No such file or directory (2)\n"
        b"rsync error: unexplained error (code 23) at main.c(123) [Receiver=3.2.7]\n"
    )

    def _mixed_runner(argv):
        remote_spec = argv[-2]
        if "codex" in remote_spec:
            return subprocess.CompletedProcess(args=argv, returncode=23, stdout=b"", stderr=stderr)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    caplog.set_level(logging.DEBUG, logger="api.services.agent_transcript_mirror")

    result1 = mirror.mirror_host("laptop", "user@laptop.example", runner=_mixed_runner)
    assert result1.ok is True

    def _records(level):
        return [r for r in caplog.records if r.levelno == level and "laptop" in r.getMessage()]

    warnings_after_first = _records(logging.WARNING)
    debugs_after_first = _records(logging.DEBUG)
    assert len(warnings_after_first) == 1, [r.getMessage() for r in warnings_after_first]
    assert "unreadable" in warnings_after_first[0].getMessage()
    assert len(debugs_after_first) == 1, [r.getMessage() for r in debugs_after_first]

    caplog.clear()

    result2 = mirror.mirror_host("laptop", "user@laptop.example", runner=_mixed_runner)
    assert result2.ok is True

    warnings_after_second = _records(logging.WARNING)
    debugs_after_second = _records(logging.DEBUG)
    assert warnings_after_second == [], "warned twice for the same (host, engine, message) triple"
    assert len(debugs_after_second) == 1, "debug logging must fire on every call, not just the first"


def test_mirror_once_runs_hosts_concurrently_not_serially(tmp_path, monkeypatch):
    """Asserting on `len(set(idents)) > 1` alone still depends on thread
    scheduling: `mirror_host` is submitted per HOST into one
    `ThreadPoolExecutor`, but CPython's own worker-reuse behavior can land
    two hosts' calls on the SAME worker thread even when `mirror_once`
    genuinely dispatches concurrently — a scheduling coincidence, not a
    property, and measured to flake in both directions on an
    otherwise-idle box.

    Bound here on a `threading.Barrier(len(hosts))`: the fake runner
    blocks on it every call. A genuinely CONCURRENT dispatch has both
    hosts' threads alive and calling into the runner around the same
    time, so the barrier releases quickly on every one of its 2 rounds
    (2 engines x 2 hosts). A SERIAL implementation runs `mirror_host`
    for `hostA` to completion — both of its engine calls — before
    `hostB` ever starts, so `hostA`'s first call waits alone until the
    barrier's own short timeout trips, breaking the barrier (permanently,
    for every party) and turning BOTH hosts' results into failures. No
    timing assertion: the test's outcome (both hosts `ok`, or both
    hosts failed) is decided by the barrier by construction, not by how
    fast anything ran."""
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir,
               {"hostA": "user@hostA.example", "hostB": "user@hostB.example"})

    # Ample margin above any real thread-startup latency, tiny next to a
    # human-perceptible test run — a serial implementation blocks the sole
    # caller thread on `barrier.wait()` until this 5s timeout trips (it
    # resolves AT the bound, not under it — see the docstring above for
    # why that still turns both hosts' results into failures).
    barrier = threading.Barrier(2, timeout=5)

    def _recording_runner(argv):
        barrier.wait()
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    results = mirror.mirror_once(runner=_recording_runner)

    assert results["hostA"].ok is True, results["hostA"].error
    assert results["hostB"].ok is True, results["hostB"].error


# ---------------------------------------------------------------------------
# mirror_root() path resolution
# ---------------------------------------------------------------------------


def test_mirror_root_relative_default_anchors_to_repo_root_not_cwd(tmp_path, monkeypatch):
    """Round-1 finding #14: a relative `agent_transcript_mirror_dir` (the
    default, "data/agent-transcript-mirror") must resolve against the REPO
    ROOT, not the process's current working directory — same precedent as
    `cc_wezterm_store.DEFAULT_DB_PATH`."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_transcript_mirror_dir", "data/agent-transcript-mirror", raising=False)
    # A cwd that is NOT the repo root — if mirror_root() resolved relative
    # to cwd (the bug), the result would land under tmp_path instead.
    monkeypatch.chdir(tmp_path)

    root = mirror.mirror_root()

    expected_repo_root = Path(mirror.__file__).resolve().parent.parent.parent
    assert root == expected_repo_root / "data" / "agent-transcript-mirror"
    assert not str(root).startswith(str(tmp_path))


def test_mirror_root_absolute_value_returned_unchanged(tmp_path, monkeypatch):
    from config.settings import settings
    abs_dir = tmp_path / "custom-mirror-root"
    monkeypatch.setattr(settings, "agent_transcript_mirror_dir", str(abs_dir), raising=False)
    assert mirror.mirror_root() == abs_dir


def test_mirror_root_tilde_prefixed_value_expands_under_home(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_transcript_mirror_dir", "~/synthetic-mirror-root", raising=False)
    assert mirror.mirror_root() == Path.home() / "synthetic-mirror-root"


# ---------------------------------------------------------------------------
# host-name sanitization
# ---------------------------------------------------------------------------


def test_host_dirs_rejects_traversal_and_hidden_names():
    for bad in ["../escape", "a/b", "a\\b", ".hidden", ""]:
        with pytest.raises(ValueError):
            mirror.host_dirs(bad)


def test_host_dirs_rejects_shell_and_flag_like_names():
    """Round-1 finding #16: the old reject-list (`/`, `\\`, `..`, leading
    `.`, empty) let through shapes rsync/ssh could misinterpret as an
    option or an expansion. The new positive allowlist closes them too."""
    for bad in [
        "-e", "--delete",       # would be parsed as an rsync/ssh flag
        "~", "~laptop",         # shell tilde-expansion
        "$HOME", "${HOME}",     # shell variable expansion
        " ", "\t", "host name",  # whitespace-only / embedded whitespace
        "trailing-",            # trailing hyphen — not a plausible hostname
        "trailing.",            # trailing dot — not a plausible hostname
        "a" * 300,              # implausibly long
    ]:
        with pytest.raises(ValueError):
            mirror.host_dirs(bad)


def test_unsafe_host_name_is_skipped_without_writing_outside_root(tmp_path, monkeypatch, caplog):
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir, {"../escape": "user@evil.example"})
    runner = _FakeRsyncRunner()
    caplog.set_level(logging.WARNING, logger="api.services.agent_transcript_mirror")

    result = mirror.mirror_host("../escape", "user@evil.example", runner=runner)

    assert result.ok is False
    assert runner.calls == []  # rsync never invoked for an unsafe host name
    root = mirror.mirror_root()
    assert not (root.parent / "escape").exists()


# ---------------------------------------------------------------------------
# no-op with no hosts / disabled
# ---------------------------------------------------------------------------


def test_mirror_once_is_noop_with_no_hosts_configured(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    runner = _FakeRsyncRunner()

    results = mirror.mirror_once(runner=runner)

    assert results == {}
    assert runner.calls == []


def test_mirror_once_skips_the_api_host_itself(tmp_path, monkeypatch):
    cc_dir = tmp_path / "cc"
    cx_dir = tmp_path / "cx"
    cc_dir.mkdir()
    cx_dir.mkdir()
    _configure(monkeypatch, tmp_path, cc_dir, cx_dir,
               {"this-box": "user@this-box.example"}, this_host="this-box")
    runner = _FakeRsyncRunner()

    results = mirror.mirror_once(runner=runner)

    assert results == {}
    assert runner.calls == []


def test_start_is_noop_when_disabled(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_transcript_mirror_enabled", False, raising=False)
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)

    mirror.start()
    assert mirror._task is None
    mirror.stop()  # no crash on stop with nothing started


def test_start_is_noop_with_no_hosts_registered(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_transcript_mirror_enabled", True, raising=False)
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    mirror.start()
    assert mirror._task is None


async def test_start_creates_task_that_ticks_and_stop_cancels_it(monkeypatch):
    """Round-1 finding #12: only the disabled/no-hosts no-op cases were
    covered — AC 4's "the API stays responsive" half (the loop actually
    runs a tick, and can be stopped) was unbound. No timing assertions
    (they flake under xdist) — a bounded poll loop, not a fixed sleep."""
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_transcript_mirror_enabled", True, raising=False)
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)
    # Long enough that only the FIRST tick fires during this test — a
    # short interval could let a second tick race the assertions below.
    monkeypatch.setattr(settings, "agent_transcript_mirror_interval_seconds", 3600, raising=False)

    tick_calls: list[int] = []
    monkeypatch.setattr(mirror, "mirror_once", lambda **kw: (tick_calls.append(1), {})[1])

    mirror.start()
    try:
        assert mirror._task is not None
        for _ in range(200):
            if tick_calls:
                break
            await asyncio.sleep(0.01)
        assert tick_calls, "the loop never performed its first tick"
        assert not mirror._task.done()  # still alive, parked in its (long) interval sleep
    finally:
        mirror.stop()
        assert mirror._task is None


async def test_mirror_loop_invokes_mirror_once_through_to_thread(monkeypatch):
    """The other half of finding #12: the tick must run `mirror_once`
    through `asyncio.to_thread` (so a slow/hanging rsync can't block the
    event loop) — patches that seam directly rather than inferring it from
    timing."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_transcript_mirror_interval_seconds", 3600, raising=False)

    to_thread_calls: list = []

    async def _fake_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(mirror, "mirror_once", lambda: {})

    task = asyncio.create_task(mirror._mirror_loop())
    try:
        for _ in range(200):
            if to_thread_calls:
                break
            await asyncio.sleep(0.01)
        assert to_thread_calls, "the loop never reached its first tick"
        assert to_thread_calls[0] is mirror.mirror_once
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
