"""On-demand access to Photos originals and previews on the Photos Mac.

The API host has no Photos library. When ``LIFEOS_PHOTOS_ORIGIN_SSH_HOST`` is
set, originals, previews, and file info are fetched by running
``scripts/photos_origin_helper.py`` on that Mac over SSH and relaying its
stdout. ``ssh`` runs as a subprocess with an argument list (never through a
local shell); the remote command is a single shell-quoted string.

Every subprocess is killed on every exit path (completion, error, client
disconnect), and a small semaphore bounds how many SSH sessions run at once.
"""
import asyncio
import json
import logging
import re
import shlex
from typing import AsyncIterator, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

UUID_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")

HELPER_PYTHON = "/usr/bin/python3"
HELPER_RELATIVE_PATH = "scripts/photos_origin_helper.py"

MAX_CONCURRENT_SESSIONS = 4
SLOT_WAIT_SECONDS = 20
INFO_TIMEOUT_SECONDS = 20
# Time to the first output byte; a preview may need the Mac to convert a HEIC.
FIRST_BYTE_TIMEOUT_SECONDS = 60
# Longest a transfer may go without receiving a byte.
IDLE_TIMEOUT_SECONDS = 60
EXIT_WAIT_SECONDS = 5
CHUNK_BYTES = 64 * 1024
MAX_INFO_BYTES = 64 * 1024

# Exit codes of scripts/photos_origin_helper.py, and ssh's own failure code.
HELPER_EXIT_NOT_FOUND = 3
HELPER_EXIT_UNAVAILABLE = 4
SSH_EXIT_FAILURE = 255


class OriginError(Exception):
    """A failed origin request. ``kind`` is one of: unconfigured,
    unreachable, busy, not_found, error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


_semaphore: Optional[asyncio.Semaphore] = None


def _slots() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    return _semaphore


def _ssh_host() -> str:
    """The configured SSH target, or OriginError when unset or unusable."""
    host = (settings.photos_origin_ssh_host or "").strip()
    if not host:
        raise OriginError("unconfigured", "Photos origin is not configured")
    if host.startswith("-") or re.search(r"\s", host):
        raise OriginError("unconfigured", "LIFEOS_PHOTOS_ORIGIN_SSH_HOST is not a valid SSH target")
    return host


def _remote_helper_path() -> str:
    """Shell-ready path of the helper script on the Mac.

    A leading ``~`` is rewritten to ``"$HOME"`` so the remote shell expands it
    even though the rest of the path is quoted.
    """
    repo = (settings.photos_origin_repo_path or "").strip() or "~/Code/LifeOS"
    home_prefix = ""
    if repo == "~" or repo.startswith("~/"):
        home_prefix = '"$HOME"'
        repo = repo[1:]
    return home_prefix + shlex.quote(repo.rstrip("/") + "/" + HELPER_RELATIVE_PATH)


def build_ssh_command(subcommand: str, asset_uuid: str) -> list:
    """Argument list for running a helper subcommand on the Photos Mac."""
    if not UUID_RE.match(asset_uuid):
        raise OriginError("error", "Invalid photo UUID")
    host = _ssh_host()
    remote = " ".join([
        shlex.quote(HELPER_PYTHON),
        _remote_helper_path(),
        shlex.quote(subcommand),
        shlex.quote(asset_uuid),
    ])
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host, remote]


def _error_for_exit(code: Optional[int]) -> OriginError:
    if code in (HELPER_EXIT_NOT_FOUND, HELPER_EXIT_UNAVAILABLE):
        return OriginError("not_found", "Photo is not available on the Mac")
    if code == SSH_EXIT_FAILURE:
        return OriginError("unreachable", "The Photos Mac is unreachable")
    return OriginError("error", "The Photos Mac returned an error")


class OriginSession:
    """One SSH subprocess holding one concurrency slot.

    ``close()`` is idempotent and synchronous (so it is safe while a task is
    being cancelled): it kills the process if still running and releases the
    slot.
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._released = False

    def close(self) -> None:
        if self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        if not self._released:
            self._released = True
            _slots().release()

    def __del__(self):
        # Safety net: a session garbage-collected unclosed still kills its process.
        try:
            self.close()
        except Exception:
            pass

    async def read_first(self, timeout: float) -> bytes:
        try:
            return await asyncio.wait_for(self._proc.stdout.read(CHUNK_BYTES), timeout)
        except asyncio.TimeoutError:
            raise OriginError("unreachable", "The Photos Mac did not respond in time")

    async def read_all(self, timeout: float) -> bytes:
        """All of stdout (at most MAX_INFO_BYTES plus one chunk)."""
        async def collect() -> bytes:
            data = b""
            while len(data) <= MAX_INFO_BYTES:
                chunk = await self._proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                data += chunk
            return data

        try:
            return await asyncio.wait_for(collect(), timeout)
        except asyncio.TimeoutError:
            raise OriginError("unreachable", "The Photos Mac did not respond in time")

    async def exit_code(self) -> int:
        try:
            return await asyncio.wait_for(self._proc.wait(), EXIT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            raise OriginError("error", "The Photos Mac helper did not exit")

    async def stream(self, first: bytes) -> AsyncIterator[bytes]:
        """Yield ``first`` then the rest of stdout, always closing the session."""
        try:
            if first:
                yield first
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self._proc.stdout.read(CHUNK_BYTES), IDLE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.warning("Photos origin transfer stalled; closing")
                    return
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()


async def _spawn(subcommand: str, asset_uuid: str) -> OriginSession:
    cmd = build_ssh_command(subcommand, asset_uuid)
    try:
        await asyncio.wait_for(_slots().acquire(), SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise OriginError("busy", "Too many photo transfers in progress")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except BaseException as exc:
        _slots().release()
        if isinstance(exc, OSError):
            raise OriginError("error", "Could not run ssh")
        raise
    return OriginSession(proc)


async def fetch_info(asset_uuid: str) -> dict:
    """The helper's ``info`` record for an asset."""
    session = await _spawn("info", asset_uuid)
    try:
        data = await session.read_all(INFO_TIMEOUT_SECONDS)
        code = await session.exit_code()
    finally:
        session.close()
    if code != 0:
        raise _error_for_exit(code)
    try:
        info = json.loads(data)
    except ValueError:
        raise OriginError("error", "The Photos Mac returned an unreadable response")
    if not isinstance(info, dict):
        raise OriginError("error", "The Photos Mac returned an unreadable response")
    return info


async def open_stream(subcommand: str, asset_uuid: str) -> tuple:
    """Start ``original`` or ``preview`` and wait for its first output.

    Returns ``(session, first_chunk)``; the caller streams the rest with
    ``session.stream(first_chunk)``, which closes the session. A failure
    before any output raises OriginError and the session is already closed.
    """
    session = await _spawn(subcommand, asset_uuid)
    try:
        first = await session.read_first(FIRST_BYTE_TIMEOUT_SECONDS)
        if not first:
            code = await session.exit_code()
            if code != 0:
                raise _error_for_exit(code)
    except BaseException:
        session.close()
        raise
    return session, first
