"""ssh mechanism for running (and killing) an executor's CLI subprocess on a
board-assigned host other than the API host (#851).

One mechanism serves both remote execution and the remote kill path:

- `resolve_host_target()` looks up a board-facing host name in
  `settings.agent_hosts` (operator config, `{name: ssh_target}`). An empty
  host, or a host equal to the API's own hostname, means "local" — the
  executors' existing `spawn_fn` seam is used unchanged. A name not in the
  registry is a hard failure (`HostResolutionError`) with no ssh call made.
- `build_remote_argv()` wraps a local CLI invocation (the exact argv the
  executor would run locally) into an ssh call that also strips every
  provider credential on the remote side (mirrors each executor's own
  `_clean_env`) and captures the remote process group id as the first line
  of stdout, so the kill path can reach it later.
- `kill_remote_process_group()` runs `ssh <target> kill -- -<pgid>` through
  an injectable runner, so tests never invoke a real `ssh`.

Kept as its own module (rather than inside either executor) because both
`ClaudeCodeExecutor` and `CodexExecutor` need it, and so does the operator
kill endpoint (`api/routes/agents.py`) for a session whose `host` is set.
"""
from __future__ import annotations

import logging
import os
import queue
import shlex
import socket
import subprocess
import threading
from typing import Callable, Optional

from config.settings import settings


logger = logging.getLogger(__name__)


def api_host_name() -> str:
    """Short hostname of the machine running this process.

    Deliberately duplicates `api.routes.agents.api_host_name()`'s one-line
    body rather than importing it: services must not depend on routes (that
    module already imports `SessionStore` from this package — importing
    back would invert the dependency and risk a cycle). Both call sites
    strip the domain suffix the same way, so a value from either function
    reads identically.
    """
    return socket.gethostname().split(".")[0]


# (round 1, finding #2) A small static list of concrete credential env-var
# names, unioned into every `env_names_matching_prefixes()` result below —
# NOT filtered to just what THIS process's own environment happens to
# contain. `env_names_matching_prefixes` alone enumerates names present in
# the worker's own `os.environ`; a worker installed with no
# ANTHROPIC_*/CLAUDE*/OPENAI_ vars set at all (a subscription/OAuth
# install, the common case) would otherwise unset nothing on the remote
# host, silently inheriting whatever a registered host's own non-
# interactive shell exports — the exact API-billing leak `_clean_env`'s
# local strip exists to prevent (see its docstring), just on the remote
# side. `env -u` on a name that was never set is a no-op, so unconditionally
# including these costs nothing.
CANONICAL_CREDENTIAL_ENV_NAMES: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDECODE",
    # Codex/OpenAI equivalent of ANTHROPIC_API_KEY — the credential name a
    # remote host's shell would export to make Codex API-billed instead of
    # subscription-billed through the CLI.
    "OPENAI_API_KEY",
})


def env_names_matching_prefixes(prefixes: tuple[str, ...], keep: frozenset[str] = frozenset()) -> list[str]:
    """Names to `env -u` on a remote spawn: every name in the CURRENT
    process's environment matching any of `prefixes`, UNIONED with
    `CANONICAL_CREDENTIAL_ENV_NAMES` (both minus `keep`) — see that
    constant's docstring for why the union, not just the local scan, is
    required for the "must NOT inherit" guarantee to hold on a clean
    worker install."""
    local_matches = {k for k in os.environ if k.startswith(prefixes)}
    return sorted((local_matches | CANONICAL_CREDENTIAL_ENV_NAMES) - keep)


class HostResolutionError(Exception):
    """Raised by `resolve_host_target()` when `host` names a machine that
    isn't in `settings.agent_hosts` — the caller must fail the task closed
    (`#agent-failed`) rather than ever invoking ssh."""


# First line of the remote wrapper's stdout, echoed before the real
# command's own output begins — see `build_remote_argv()`.
PGID_LINE_PREFIX = "PGID:"


def is_local_host(host: str | None, api_host_name: str) -> bool:
    """True when `host` means "run on this API host" — unset/blank, or an
    exact match for the API's own short hostname."""
    host = (host or "").strip()
    return not host or host == api_host_name


def resolve_host_target(host: str | None, api_host_name: str) -> Optional[str]:
    """Return the ssh target for `host`, or `None` when `host` means local.

    Raises `HostResolutionError` when `host` is set, isn't the API host,
    and isn't a key in `settings.agent_hosts` — the caller must not spawn
    anything (locally or over ssh) in that case.
    """
    if is_local_host(host, api_host_name):
        return None
    target = settings.agent_hosts.get(host)
    if not target:
        raise HostResolutionError(
            f"host {host!r} is not configured in LIFEOS_AGENT_HOSTS"
        )
    return target


def build_remote_argv(
    argv: list[str],
    *,
    target: str,
    unset_env_names: list[str],
    connect_timeout: Optional[int] = None,
) -> list[str]:
    """Wrap a local CLI invocation into an ssh call to `target`.

    The remote command:
      1. Unsets every name in `unset_env_names` (mirrors the executor's own
         `_clean_env`, so a remote session can't inherit the operator's
         Anthropic/Claude credentials any more than a local one can).
      2. Runs under `setsid` inside a tiny `bash -c` wrapper that echoes
         `PGID:<pid>` as its very first stdout line — `$$` inside a fresh
         `setsid` shell is both the pid and the process-group id — before
         `exec`-ing the real argv, so the group leader replaces the wrapper
         process and every later stdout line is the CLI's own output. The
         caller strips that first line (see `read_remote_pgid_line()`) and
         records the pgid for the remote kill path.
    """
    timeout = connect_timeout if connect_timeout is not None else settings.agent_ssh_connect_timeout
    unset_flags: list[str] = []
    for name in unset_env_names:
        unset_flags.extend(["-u", name])
    inner = shlex.join(["env", *unset_flags, *argv])
    remote_command = f"setsid bash -c 'echo \"{PGID_LINE_PREFIX}$$\"; exec \"$@\"' _ {inner}"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        target,
        "--",
        remote_command,
    ]


def build_remote_launcher_argv(
    argv: list[str], *, target: str, connect_timeout: Optional[int] = None,
) -> list[str]:
    """Wrap a launcher command (the `/resume`/`/focus` WezTerm invocation)
    into an ssh call to `target`, with no pgid capture and no credential
    stripping — unlike `build_remote_argv` (the executor spawn path), a
    launcher doesn't need a kill target (it's a short-lived terminal
    spawner, not the long-running CLI session itself) and inherits no
    provider credentials in the first place.
    """
    timeout = connect_timeout if connect_timeout is not None else settings.agent_ssh_connect_timeout
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        target,
        "--",
        shlex.join(argv),
    ]


def read_remote_pgid_line(line: str) -> Optional[int]:
    """Parse the `PGID:<n>` line a remote-wrapped subprocess echoes first.
    Returns None if `line` isn't that line (caller then treats it as real
    stdout — defensive, shouldn't happen in practice)."""
    line = line.rstrip("\n")
    if not line.startswith(PGID_LINE_PREFIX):
        return None
    try:
        return int(line[len(PGID_LINE_PREFIX):].strip())
    except ValueError:
        return None


def read_line_with_deadline(stream, timeout: float) -> tuple[str, bool]:
    """Read one line from `stream` with a bounded wait (round 1, finding
    #3). Both executors block on `stream.readline()` for the `PGID:` line
    BEFORE their own wall-clock watchdog starts — `ssh -o ConnectTimeout`
    bounds only the TCP handshake, not a stall during auth or a host that
    accepts the connection but never answers, so an unbounded `readline()`
    here can hang the caller's thread forever.

    Runs the read on a daemon thread rather than `select.select` so this
    works uniformly whether `stream` is a real OS pipe or a test double
    (fakes generally have no `fileno()`). Returns `(line, timed_out)`:
    `timed_out` is True and `line` is `""` when nothing arrived within
    `timeout` seconds — the reader thread is left running (daemon, so it
    exits with the process) rather than joined, since a genuinely hung
    stream has no clean way to unblock `readline()` from here; callers
    that hit `timed_out` are expected to terminate the owning subprocess,
    which closes the pipe's write end and unblocks the reader.
    """
    result: "queue.Queue[str]" = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            result.put(stream.readline())
        except Exception:  # noqa: BLE001 — surface as empty/no-line, not a crash
            result.put("")

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    try:
        return result.get(timeout=timeout), False
    except queue.Empty:
        return "", True


def last_nonempty_line(text: str) -> str:
    """Last non-blank line of `text` (round 1, finding #4) — used to fold
    an ssh failure's stderr (e.g. `ssh: connect to host studio port 22:
    Connection refused`) into an executor's failure reason instead of
    leaving it reachable only via the transcript's `stderr_tail`."""
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


KillRunner = Callable[[list[str]], "subprocess.CompletedProcess"]


def _default_kill_runner(argv: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, timeout=15, check=False)  # noqa: S603


def kill_remote_process_group(
    *,
    target: str,
    pgid: int,
    connect_timeout: Optional[int] = None,
    runner: Optional[KillRunner] = None,
) -> bool:
    """Run `ssh <target> kill -- -<pgid>` (SIGTERM to the whole remote
    process group) through an injectable `runner` — production default is a
    real `subprocess.run`; tests inject a fake that records the argv and
    never touches the network.

    Best-effort: any exception or non-zero exit is logged and swallowed —
    mirrors `_kill_local_subprocess`'s contract (a kill failure must not
    break the rest of teardown).
    """
    timeout = connect_timeout if connect_timeout is not None else settings.agent_ssh_connect_timeout
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        target,
        "kill", "--", f"-{pgid}",
    ]
    run = runner or _default_kill_runner
    try:
        result = run(argv)
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            logger.warning("remote kill on %s (pgid %s) exited %s", target, pgid, returncode)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, mirrors local kill
        logger.warning("remote kill on %s (pgid %s) failed: %s", target, pgid, exc)
        return False
