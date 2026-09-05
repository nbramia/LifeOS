"""Mirrors Claude Code / Codex transcripts from registered remote hosts.

Local `cli_sessions` rows (posted by `scripts/lifeos-agent-hook.sh`) give a
remote session status and a prompt preview, but nothing else — token counts,
cost, tool calls, and the transcript feed only exist for a transcript this
API host can read off its own disk. This module closes that gap: a
background loop periodically rsyncs each registered host's Claude Code and
Codex transcript directories into a per-host mirror on this box, read-only,
so `api/routes/agents.py`'s snapshot builder can ingest them exactly like a
local transcript and merely stamp the result with the remote host's name.

Layout: `<mirror_root>/<host>/claude_code/` and `<mirror_root>/<host>/codex/`,
one subtree per host in `settings.agent_hosts`, mirroring the source
directory structure (`~/.claude/projects/<cwd>/*.jsonl` and
`~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl`) so the existing
`session_ingest.discover_sessions()` scanners work unmodified against a
mirrored root.

Public surface:
- `mirror_root()` / `host_dirs(host)` — path helpers.
- `mirror_host(host, ssh_target, ...)` — one host's incremental pull.
- `mirror_once(...)` — every registered host, concurrently.
- `start()` / `stop()` — the background interval loop (wired into
  `api/main.py`'s lifespan next to `agent_viz_summary_prefetch`).
- `mirrored_snapshot()` — `(cc_rows, cx_rows, edges)` for every mirrored host.
- `mirrored_transcript_dirs(engine)` — per-host dirs for the events/stream/
  focus lookups in `api/routes/agents.py`.

Constraints: ssh + rsync only, no new dependencies; read-only
on the remote (never `--delete` on the remote side, never push); an
unreachable host must not delay or break any other host's mirror tick.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# `(host, engine, message)` triples this process has already warned about
# for an unreadable/missing source directory (rsync exit 23/24 whose
# stderr names a `change_dir ... failed`). Emitted once per distinct
# triple for the life of the process rather than never or every tick
# (which would spam the log every mirror
# interval for a host that legitimately can't be reached).
_source_dir_warned: set[tuple[str, str, str]] = set()

# rsync's own I/O timeout (distinct from ssh's ConnectTimeout) — bounds a
# half-open connection that accepted the TCP handshake but then stalls.
_RSYNC_IO_TIMEOUT_SECONDS = 30
# Wall-clock cap on the whole `subprocess.run` call, well above the rsync
# --timeout above so that timeout (not this one) is what normally fires.
_RSYNC_SUBPROCESS_TIMEOUT = _RSYNC_IO_TIMEOUT_SECONDS + 30

RsyncRunner = Callable[[list[str]], "subprocess.CompletedProcess"]


@dataclass
class MirrorResult:
    """Outcome of mirroring one host's transcripts (both engines)."""
    host: str
    ok: bool
    error: str = ""


# `api/services/agent_transcript_mirror.py` -> repo root is three
# `.parent`s up — same pattern as `cc_wezterm_store.DEFAULT_DB_PATH`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def mirror_root() -> Path:
    """The transcript mirror's root directory.

    A relative `settings.agent_transcript_mirror_dir` (the default,
    `"data/agent-transcript-mirror"`) is anchored to the REPO ROOT, not the
    process's current working directory. An absolute or `~`-prefixed value
    (already absolute after `os.path.expanduser`) is returned as-is.
    """
    from config.settings import settings

    raw = Path(os.path.expanduser(settings.agent_transcript_mirror_dir))
    if raw.is_absolute():
        return raw
    return _REPO_ROOT / raw


# Alphanumeric first/last char; `.`, `-`, and `_` allowed in the middle
# only — real hostnames never start or end with `.`/`-`/`_`. This positive
# allowlist rejects shapes rsync/ssh could misinterpret as an option or an
# expansion — a bare `-e`, `--delete`, `~`, `$HOME`, whitespace-only —
# since nothing starting with `-` or `.` gets through, and no `/`, `\`, or
# whitespace can appear anywhere.
# It does NOT reject every embedded `..` — `a..b` matches, since `.` is a
# permitted middle character and there's no adjacency check. That's
# harmless in practice: the result is an ordinary single path segment
# (`host_dirs()` never joins it as `../..`), not a traversal.
_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
_MAX_HOST_NAME_LEN = 253  # the DNS hostname length limit — a generous, well-known bound


def _sanitize_host(host: str) -> str:
    """Raise `ValueError` if `host` isn't a plausible hostname."""
    if not host or len(host) > _MAX_HOST_NAME_LEN or not _HOST_NAME_RE.match(host):
        raise ValueError(f"unsafe host name for transcript mirror: {host!r}")
    return host


def host_dirs(host: str) -> tuple[Path, Path]:
    """`(<root>/<host>/claude_code, <root>/<host>/codex)`. Raises
    `ValueError` via `_sanitize_host` for an unsafe host name."""
    safe = _sanitize_host(host)
    root = mirror_root() / safe
    return root / "claude_code", root / "codex"


def _rsync_argv(ssh_target: str, remote_dir: str, dst_dir: Path) -> list[str]:
    """Build one incremental, read-only rsync pull.

    `-rt` (recursive, preserve mtimes) is the minimum needed for rsync's
    default quick-check (size + mtime) to skip unchanged files on the next
    tick — without `-t` the destination's mtime would always differ from
    the source's, forcing a full re-transfer every run. No `--delete`
    (never remove local mirror files just because the remote host has
    since deleted them) and no owner/group preservation (`-a`'s `-o`/`-g` need
    privileges this process doesn't have and aren't meaningful once files
    cross machines). `remote_dir` is passed through as-is (it may contain
    `~`, which the remote shell expands) — never touched or expanded here.
    """
    from config.settings import settings

    ssh_opts = (
        f"ssh -o BatchMode=yes -o ConnectTimeout={settings.agent_ssh_connect_timeout}"
    )
    remote_spec = f"{ssh_target}:{remote_dir.rstrip('/')}/"
    return [
        "rsync", "-rtz",
        "--timeout", str(_RSYNC_IO_TIMEOUT_SECONDS),
        "-e", ssh_opts,
        "--include=*/", "--include=*.jsonl", "--exclude=*",
        # `--` stops rsync from parsing anything after
        # it as an option — an `agent_hosts` VALUE beginning with `-`
        # would otherwise be interpreted as a flag rather than a
        # positional source/destination.
        "--",
        remote_spec,
        f"{dst_dir}/",
    ]


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(  # noqa: S603 — fixed argv, no shell=True
        argv, capture_output=True, timeout=_RSYNC_SUBPROCESS_TIMEOUT, check=False,
    )


def _decode(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


# rsync's own summary trailer, e.g. "rsync error: unexplained error (code
# 255) at io.c(232) [Receiver=3.2.7]" — byte-identical whether the
# underlying cause is a DNS failure, a refused connection, or an
# unroutable address, so it carries no diagnostic value of its own.
_RSYNC_TRAILER_RE = re.compile(r"^rsync error: .* \(code \d+\)")

# ssh's own benign first-connect notice, e.g. "Warning: Permanently added
# '127.0.0.1' (ED25519) to the list of known hosts.": only appears the
# first tick that learns a host key, and unlike the rejected-key/
# auth-failure line right after it, carries no diagnostic value — skip it
# the same way the rsync trailer is skipped.
_SSH_KNOWN_HOSTS_WARNING_RE = re.compile(r"^Warning: Permanently added ")


def _diagnostic_stderr_line(stderr: str) -> str:
    """The first non-blank stderr line that ISN'T rsync's own trailer or
    ssh's benign known-hosts warning — typically ssh's own error (`ssh:
    connect to host X port 22: Connection refused`, `ssh: Could not
    resolve hostname X: ...`, or an auth failure), which takes priority
    over the generic rsync trailer. Falls back to `last_nonempty_line`
    (the trailer itself) when there's nothing else in stderr at all."""
    from api.services.agent_worker.remote_spawn import last_nonempty_line

    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _RSYNC_TRAILER_RE.match(stripped) or _SSH_KNOWN_HOSTS_WARNING_RE.match(stripped):
            continue
        return stripped
    return last_nonempty_line(stderr)


def mirror_host(
    host: str,
    ssh_target: str,
    *,
    runner: Optional[RsyncRunner] = None,
) -> MirrorResult:
    """Pull both engines' transcripts for one host. Never raises.

    Runs one rsync invocation per engine so a directory that doesn't exist
    on the remote (e.g. a host that never runs Codex) doesn't block the
    other engine's pull. Logs exactly one warning line for the host if
    either engine failed — naming the host and a diagnostic stderr line
    via `_diagnostic_stderr_line` — and returns a failed `MirrorResult`;
    it never raises.

    Exit codes 23 ("partial transfer due to error" — some files skipped,
    typically non-regular files this pull's `--include`/`--exclude`
    already excludes) and 24 ("partial transfer due to vanished source
    files") are treated as SUCCESS: both are the
    expected outcome of pulling a transcript a live CLI is actively
    writing to on the remote end, not a real failure — the file(s) that
    mattered still transferred.
    """
    from config.settings import settings

    run = runner or _default_runner
    try:
        cc_dst, cx_dst = host_dirs(host)
    except ValueError as exc:
        logger.warning("transcript mirror: skipping host %r: %s", host, exc)
        return MirrorResult(host=host, ok=False, error=str(exc))

    errors: list[str] = []
    for engine, remote_dir, dst_dir in (
        ("claude_code", settings.claude_code_projects_dir, cc_dst),
        ("codex", settings.codex_sessions_dir, cx_dst),
    ):
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            argv = _rsync_argv(ssh_target, remote_dir, dst_dir)
            result = run(argv)
            rc = getattr(result, "returncode", 0)
            if rc in (23, 24):
                # 23/24 count as success (below), but a source directory
                # that doesn't exist or can't be read on the remote is
                # worth knowing about. Log the diagnostic line at debug
                # always, and once per (host, engine, message) at warning
                # when it specifically names a directory the remote rsync
                # couldn't enter.
                stderr = _decode(getattr(result, "stderr", ""))
                diag = _diagnostic_stderr_line(stderr)
                if diag:
                    logger.debug(
                        "transcript mirror: host %s %s rsync exited %d: %s",
                        host, engine, rc, diag,
                    )
                    if "change_dir" in diag and "failed" in diag:
                        key = (host, engine, diag)
                        if key not in _source_dir_warned:
                            _source_dir_warned.add(key)
                            logger.warning(
                                "transcript mirror: host %s %s source directory "
                                "unreadable, skipping: %s",
                                host, engine, diag,
                            )
            elif rc != 0:
                stderr = _decode(getattr(result, "stderr", ""))
                errors.append(_diagnostic_stderr_line(stderr) or f"rsync exited {rc}")
        except Exception as exc:  # noqa: BLE001 — one host's failure must not break the tick
            errors.append(str(exc))

    if errors:
        logger.warning("transcript mirror: host %s failed: %s", host, errors[0])
        return MirrorResult(host=host, ok=False, error=errors[0])
    return MirrorResult(host=host, ok=True)


def mirror_once(*, runner: Optional[RsyncRunner] = None) -> dict[str, MirrorResult]:
    """Mirror every registered host concurrently. Returns `{host: MirrorResult}`.

    Skips the API's own host (nothing to mirror from itself) and is a
    no-op — no logging, no thread pool — when `settings.agent_hosts` is
    empty. Hosts run in a small thread pool so one slow or unreachable
    host cannot delay any other.
    """
    from config.settings import settings
    from api.services.agent_worker.remote_spawn import api_host_name

    this_host = api_host_name()
    hosts = {
        name: target for name, target in dict(settings.agent_hosts).items()
        if name != this_host
    }
    if not hosts:
        return {}

    results: dict[str, MirrorResult] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
        future_to_host = {
            pool.submit(mirror_host, name, target, runner=runner): name
            for name, target in hosts.items()
        }
        for future in as_completed(future_to_host):
            name = future_to_host[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 — a crashed future must not break the tick
                logger.warning("transcript mirror: host %s crashed: %s", name, exc)
                results[name] = MirrorResult(host=name, ok=False, error=str(exc))
    return results


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

_task: asyncio.Task[None] | None = None
_task_lock = threading.Lock()


async def _mirror_loop() -> None:
    from config.settings import settings

    while True:
        try:
            await asyncio.to_thread(mirror_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die quietly
            logger.exception("transcript mirror loop tick failed")
        interval = float(getattr(settings, "agent_transcript_mirror_interval_seconds", 120))
        await asyncio.sleep(max(1.0, interval))


def start() -> None:
    """Launch the background loop. Idempotent, and a no-op when disabled
    or when no hosts are registered — mirrors
    `agent_viz_summary_prefetch.start()`'s shape."""
    global _task
    with _task_lock:
        if _task is not None and not _task.done():
            return
        try:
            from config.settings import settings
            enabled = bool(getattr(settings, "agent_transcript_mirror_enabled", True))
            hosts = dict(getattr(settings, "agent_hosts", {}) or {})
        except Exception:  # noqa: BLE001
            enabled = True
            hosts = {}
        if not enabled or not hosts:
            logger.info("agent transcript mirror disabled or no hosts registered")
            return
        loop = asyncio.get_event_loop()
        _task = loop.create_task(_mirror_loop(), name="agent_transcript_mirror")
        logger.info("agent transcript mirror loop started (%d host(s))", len(hosts))


def stop() -> None:
    """Cancel the loop on shutdown."""
    global _task
    with _task_lock:
        if _task is None:
            return
        _task.cancel()
        _task = None


# ---------------------------------------------------------------------------
# Snapshot + lookup surface consumed by api/routes/agents.py
# ---------------------------------------------------------------------------


def _mirrored_host_dirs() -> list[Path]:
    root = mirror_root()
    if not root.exists() or not root.is_dir():
        return []
    try:
        return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []


def mirrored_transcript_dirs(engine: str) -> list[Path]:
    """Per-host mirrored transcript dirs for one engine, existing only.

    `engine` is `"claude_code"` or `"codex"`. Used by the events/stream/
    focus lookups in `api/routes/agents.py` as the fallback search path
    once the local transcript root misses.
    """
    sub = "claude_code" if engine == "claude_code" else "codex"
    dirs: list[Path] = []
    for host_dir in _mirrored_host_dirs():
        candidate = host_dir / sub
        if candidate.exists() and candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _demote_inferred_running(row: dict[str, Any]) -> None:
    """Mutates `row` in place: an INFERRED `running`
    status on a mirrored row is demoted to `inactive`. A `running` whose
    `status_inferred` is `False` is left untouched — that would mean an
    authoritative process-scan signal reached here, which can't happen for
    a mirrored row (`live_counts={}` in the callers above), but leaving it
    alone rather than asserting is the more conservative failure mode.
    """
    if row.get("status") == "running" and row.get("status_inferred") is True:
        row["status"] = "inactive"


def mirrored_snapshot(
    cache_ttl: float = 30.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """`(cc_rows, cx_rows, edges)` — one snapshot row per mirrored session,
    across every mirrored host, plus the parent-subagent spawn edges each
    engine's own `build_snapshot()` derives. Never raises: a bad mirror for
    one host is logged and skipped, the rest still return.

    Every row gets `host` set to the mirrored host's directory name and
    `mirrored = True`. `live_counts={}` is passed to each engine's
    `build_snapshot()` so a mirrored row is never promoted to `running` by
    a LOCAL process scan — but that only blocks the process-scan branch of
    `_infer_status`; its mtime-under-10-minutes branch still says
    `running` for a freshly mirrored transcript regardless of `live_counts`
    (AC 7 requires a remote session's `running` status to come ONLY from
    hook events). So an inferred `running` (never one
    with `status_inferred=False` — that would mean a genuine hook-status
    merge already happened upstream, which never runs this early) is
    demoted here to `inactive` — the same "resumable, not actively
    running" bucket `_infer_status` itself falls into just past the 10
    minute mark. `_build_snapshot`'s hook-event overlay
    (`_apply_cli_session_to_dict`) then restores `running` afterwards when
    a hook row for this session id actually reports it.

    The caller (`_build_snapshot`) is responsible for dropping any edge
    whose endpoint didn't end up with a row in the final merged snapshot
    (e.g. an id collision with a local transcript that wins).
    """
    from api.services.claude_code import session_ingest as cc
    from api.services.codex import session_ingest as cx

    cc_rows: list[dict[str, Any]] = []
    cx_rows: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for host_dir in _mirrored_host_dirs():
        host_name = host_dir.name
        cc_dir = host_dir / "claude_code"
        if cc_dir.exists() and cc_dir.is_dir():
            try:
                sessions, cc_edges = cc.build_snapshot(
                    projects_dir=cc_dir, cache_ttl=cache_ttl, live_counts={},
                )
                # `build_snapshot()` returns `list(entry.sessions)` — a
                # shallow copy of the LIST, so these row dicts are the
                # SAME objects `cc`'s own ingest cache holds. Copying each
                # row here, before mutating it, keeps that cache's dicts
                # untouched — otherwise `host`/`mirrored` and (via
                # `_demote_inferred_running` and the hook overlay
                # `_build_snapshot` applies afterward) `status`/
                # `status_inferred` get written straight into the cached
                # entry, so a rebuild inside `cache_ttl` after the hook row
                # that produced the overlay has vanished (pruned, or a
                # failed `list_cli_sessions()` call) would otherwise replay
                # the stale cached mutation instead of recomputing it, in
                # violation of AC 7.
                sessions = [dict(row) for row in sessions]
                for row in sessions:
                    row["host"] = host_name
                    row["mirrored"] = True
                    _demote_inferred_running(row)
                cc_rows.extend(sessions)
                edges.extend(cc_edges)
            except Exception as exc:  # noqa: BLE001 — one host must not break the snapshot
                logger.warning("mirrored claude_code snapshot failed for host %s: %s", host_name, exc)
        cx_dir = host_dir / "codex"
        if cx_dir.exists() and cx_dir.is_dir():
            try:
                sessions, cx_edges = cx.build_snapshot(
                    sessions_dir=cx_dir, cache_ttl=cache_ttl, live_counts={},
                )
                # See the identical comment in the claude_code branch
                # above — same aliasing against `cx`'s own ingest cache.
                sessions = [dict(row) for row in sessions]
                for row in sessions:
                    row["host"] = host_name
                    row["mirrored"] = True
                    _demote_inferred_running(row)
                cx_rows.extend(sessions)
                edges.extend(cx_edges)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mirrored codex snapshot failed for host %s: %s", host_name, exc)
    return cc_rows, cx_rows, edges
