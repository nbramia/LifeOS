"""Scoped file, command and log access to the LifeOS host, exposed over MCP.

Five tools: `lifeos_fs_list`, `lifeos_fs_read`, `lifeos_fs_write`,
`lifeos_shell` and `lifeos_logs`. They are registered only when
`LIFEOS_HOST_TOOLS_ENABLED` is set, and an OAuth-connected app gets them only
when its client id is listed in `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS`.

Everything a reviewer needs to judge what these tools can reach is in the
"Policy" block below: the named roots and which are writable, the denylist,
the command allowlist, the services whose logs can be read, and the size and
time caps. Edit it there.

Paths are always given as a root name plus a relative path. Absolute paths,
`~` and `..` are refused, and the resolved path must stay inside the root, so
a symlink that points out of a root is refused. Writes refuse any symlink on
the path. The file tools run in the MCP server process; `lifeos_shell` runs
every command as a separate low-privilege account through `sudo -n -u`, and
refuses to run at all when that account is not configured.

Commands outside the allowlist are refused unless the operator turns on
Telegram approval (`LIFEOS_HOST_TOOLS_SHELL_APPROVAL`); see host_approval.py.
An approved command still runs as the low-privilege account, still passes the
path checks, and never names a shell, interpreter or network tool.

Every call is appended to an audit file (`data/host_tools_audit.jsonl`) with
the caller, the decision and, for commands, the full argv.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import pwd
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from api.services.atomic_write import atomic_write_text
from api.services.log_redaction import REDACTED_TOKEN, TELEGRAM_TOKEN_PATTERN

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Policy ────────────────────────────────────────────────────────────────
#
# Root names. The directories come from settings (vault path, scratch dir) or
# from the LifeOS checkout itself (`lifeos`, `logs`).
WRITABLE_ROOTS = ("vault", "scratch")
READABLE_ROOTS = ("vault", "scratch", "lifeos", "logs")

# Path components that are refused anywhere under a root, for reads and writes.
DENIED_COMPONENTS = frozenset({
    ".git", ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store",
})

# File names (glob, case-insensitive) that are refused anywhere under a root.
DENIED_NAME_GLOBS = (
    ".env*", "*.env", "*.pem", "*.key", "*.crt", "*.cer", "*.p12", "*.pfx", "*.kdbx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*.pickle", "*.pkl", "*.db", "*.sqlite", "*.sqlite3", "*.db-wal", "*.db-shm",
    "credentials*.json", "token*.json", "client_secret*.json", "*cookies*",
    ".netrc", ".pgpass", "*.keychain*",
)

# Directories under the LifeOS checkout that are refused outright: `data/`
# holds the OAuth store, sessions and every synced personal database.
DENIED_REPO_DIRS = ("data",)

# Services `lifeos_logs` and `systemctl status` know, mapped to the log file
# each systemd unit appends to (config/systemd/*.service).
LOG_SERVICES = {
    "lifeos-api": "server.log",
    "lifeos-mcp-http": "mcp-http.log",
    "lifeos-agent-worker": "agent-worker.log",
    "lifeos-chromadb": "chromadb.log",
    "lifeos-llm": "llama-server.log",
    "lifeos-sync": "sync.log",
}
# Units `systemctl restart` may name. lifeos-llm is left out for the same
# reason setup-systemd.sh leaves it out (GPU memory cleanup needs stop + start).
RESTARTABLE_UNITS = ("lifeos-api", "lifeos-mcp-http", "lifeos-agent-worker")

READ_MAX_BYTES = 512 * 1024
WRITE_MAX_BYTES = 1024 * 1024
# Mode for a file the host tools create (an overwritten file keeps its own).
NEW_FILE_MODE = 0o644
LIST_MAX_ENTRIES = 500
LOG_MAX_LINES = 1000
LOG_SCAN_BYTES = 4 * 1024 * 1024
SHELL_TIMEOUT_SECONDS = 30
SHELL_OUTPUT_MAX_BYTES = 64 * 1024
ARGV_MAX_ITEMS = 64
ARGV_MAX_ITEM_CHARS = 4096
SHELL_PATH = "/usr/local/bin:/usr/bin:/bin"

# Git runs with no pager, no filesystem monitor and no hooks, and diff-like
# commands skip external diff drivers and textconv filters, so repository
# config cannot turn a read into a command.
_GIT_PREFIX = (
    "git", "--no-pager", "--literal-pathspecs",
    "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
)
_GIT_DIFF_SAFETY = ("--no-ext-diff", "--no-textconv")

_GIT_STATUS_FLAGS = frozenset({"-s", "--short", "-b", "--branch", "--porcelain", "-uno", "--untracked-files=no"})
_GIT_LOG_FLAGS = frozenset({
    "--oneline", "--stat", "--shortstat", "--name-only", "--name-status", "-p", "--patch",
    "--no-merges", "--graph", "--decorate", "--first-parent", "--reverse",
})
_GIT_DIFF_FLAGS = frozenset({
    "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
    "--cached", "--staged", "--no-color",
})
_LS_FLAGS = frozenset({"-l", "-a", "-A", "-h", "-1", "-t", "-S", "-r", "-la", "-al", "-lh", "-lah", "-alh", "-ltr", "-lt"})
_GIT_VALUE_FLAG_RE = re.compile(r"^(-n\d{1,4}|-\d{1,4}|--max-count=\d{1,4}|--since=[^\s]{1,40}|--until=[^\s]{1,40}|-U\d{1,3})$")
_COUNT_RE = re.compile(r"^\d{1,4}$")

# Commands that are never sent for approval, because they run other commands
# (so the path checks below would not see what they do), change privilege, or
# move data off the host. The low-privilege account stays the real boundary;
# this list removes the obvious ways around the path checks.
NEVER_APPROVED_COMMANDS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "busybox",
    "env", "xargs", "parallel", "nohup", "setsid", "timeout", "nice", "ionice", "stdbuf",
    "watch", "script", "expect", "chroot", "unshare", "nsenter", "flock",
    "find", "systemd-run", "sed", "make", "tar",
    "sudo", "su", "doas", "pkexec", "runuser",
    "python", "python2", "python3", "perl", "ruby", "node", "nodejs", "deno", "bun",
    "php", "lua", "tclsh", "awk", "gawk", "mawk", "nawk",
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "socat",
    "telnet", "ftp", "tftp", "mail", "sendmail",
})
_NEVER_APPROVED_RE = re.compile(r"^(python|pypy|perl|ruby|node|php|lua)[\d.]*$")
# Git subcommands that are never sent for approval: they reach other hosts.
NEVER_APPROVED_GIT = frozenset({"push", "pull", "fetch", "clone", "ls-remote", "remote", "send-email", "submodule"})

# ── Errors and audit ──────────────────────────────────────────────────────


class HostToolError(Exception):
    """A refusal or failure reported to the caller as a tool error."""


_audit_lock = threading.Lock()


def _audit_path() -> Path:
    return _REPO_ROOT / "data" / "host_tools_audit.jsonl"


def _redacted_event(event: dict) -> dict:
    """The event with secrets masked in its argv and free-text fields."""
    out = dict(event)
    if isinstance(out.get("argv"), list):
        out["argv"] = [redact(a) if isinstance(a, str) else a for a in out["argv"]]
    for key in ("reason", "detail"):
        if isinstance(out.get(key), str):
            out[key] = redact(out[key])
    return out


def audit(event: dict, *, path: Path | None = None) -> bool:
    """Append one audit record; return whether it was written.

    Never raises: a failed audit write is logged. Argv and reason text are
    redacted before they are written.
    """
    event = _redacted_event(event)
    record = {"ts": time.time(), **event}
    written = False
    target = path or _audit_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str)
        with _audit_lock, open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        written = True
    except OSError as exc:
        logger.error("host tools audit write failed: %s", exc)
    logger.log(
        logging.WARNING if event.get("tool") in ("lifeos_fs_write", "lifeos_shell") else logging.INFO,
        "host tools audit: caller=%s tool=%s decision=%s",
        event.get("caller"), event.get("tool"), event.get("decision"),
    )
    return written


# ── Configuration ─────────────────────────────────────────────────────────


@dataclass
class HostToolsConfig:
    roots: dict[str, Path]
    run_as: str = ""
    vault_autocommit: bool = True
    approval_enabled: bool = False
    # Seconds a request (and an unused approval) stays open; None never expires.
    approval_timeout_seconds: int | None = 86400
    audit_path: Path | None = None
    # Injected for tests: run a command list, return CompletedProcess.
    runner: Callable[..., subprocess.CompletedProcess] = field(default=subprocess.run)
    # The Telegram approval gate (host_approval.ApprovalGate), set when approval is on.
    approvals: Any = None
    # Give the run-as account an ACL entry on the scratch directory (see
    # _grant_scratch_access). On for the real service, off in tests.
    grant_scratch_access: bool = False
    # Vault folders (as path parts) that writes may touch; empty: the whole vault.
    vault_write_dirs: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_settings(cls) -> HostToolsConfig:
        from config.settings import settings

        return cls(
            roots={
                "vault": Path(settings.vault_path),
                "scratch": Path(settings.host_tools_scratch_dir),
                "lifeos": _REPO_ROOT,
                "logs": _REPO_ROOT / "logs",
            },
            run_as=(settings.host_tools_run_as or "").strip(),
            vault_autocommit=settings.host_tools_vault_autocommit,
            approval_enabled=settings.host_tools_shell_approval,
            approval_timeout_seconds=approval_timeout(settings.host_tools_approval_timeout_seconds),
            grant_scratch_access=True,
            vault_write_dirs=parse_vault_write_dirs(settings.host_tools_vault_write_dirs),
        )


def parse_vault_write_dirs(raw: str | None) -> tuple[tuple[str, ...], ...]:
    """Vault folders from a comma-separated list, each as its path parts.

    Entries are relative to the vault root (a leading `/` is ignored). An
    entry that names no folder (`.`) or uses `..` or `~` is kept as a part no
    folder can match, so a malformed list allows less, never more.
    """
    dirs = []
    for entry in (raw or "").split(","):
        entry = entry.strip().strip("/")
        if not entry:
            continue
        parts = tuple(p for p in PurePosixPath(entry).parts if p not in ("", "."))
        if not parts or ".." in parts or entry.startswith("~"):
            parts = ("\0",)
        dirs.append(parts)
    if not dirs and (raw or "").strip():
        # A non-empty setting that names no folder (`/`, `,`) allows none.
        dirs.append(("\0",))
    return tuple(dirs)


def approval_timeout(seconds: float | None) -> int | None:
    """Settings value to a timeout: 0 or less turns expiry off; otherwise at least a minute."""
    if seconds is None or int(seconds) <= 0:
        return None
    return max(60, int(seconds))


def oauth_clients_granted() -> frozenset[str]:
    from config.settings import settings

    if not settings.host_tools_enabled:
        return frozenset()
    raw = settings.host_tools_oauth_clients or ""
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


# ── Path resolution ───────────────────────────────────────────────────────


def _is_denied_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, g) for g in DENIED_NAME_GLOBS)


def _denied_reason(root_name: str, root: Path, rel_parts: tuple[str, ...]) -> str | None:
    for part in rel_parts:
        if part in DENIED_COMPONENTS:
            return f"'{part}' paths are not available"
        if _is_denied_name(part):
            return "that file type is not available"
    if root_name == "lifeos" and rel_parts and rel_parts[0] in DENIED_REPO_DIRS:
        return f"the LifeOS '{rel_parts[0]}/' directory is not available"
    return None


_scratch_access_granted: set[tuple[str, str]] = set()
_scratch_access_lock = threading.Lock()


def _grant_scratch_access(config: HostToolsConfig, scratch: Path) -> None:
    """Let the run-as account read the scratch directory.

    The scratch directory is created private to the LifeOS account, so
    without an ACL entry the low-privilege account cannot open it and every
    command run from it fails with "Permission denied". This adds a read entry
    for that one account (and a default entry, so new files and folders
    inherit it), once per process. It never widens access for anyone else.
    """
    if not config.grant_scratch_access or not config.run_as or not scratch.is_dir():
        return
    key = (str(scratch), config.run_as)
    with _scratch_access_lock:
        if key in _scratch_access_granted:
            return
        setfacl = shutil.which("setfacl")
        # One attempt per process, whatever the outcome, so a failure is
        # logged once rather than on every call. A restart tries again.
        _scratch_access_granted.add(key)
        if setfacl is None:
            logger.warning("host tools: setfacl not found; %s cannot read the scratch directory", config.run_as)
            return
        entry = f"u:{config.run_as}:rX"
        try:
            result = config.runner(
                [setfacl, "-R", "-P", "-m", entry, "-m", f"d:{entry}", str(scratch)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("host tools: could not grant scratch access: %s", exc)
            return
        if result.returncode != 0:
            logger.warning("host tools: could not grant scratch access: %s", (result.stderr or "").strip()[:300])


def _root(config: HostToolsConfig, root_name: object, *, for_write: bool) -> tuple[str, Path]:
    allowed = WRITABLE_ROOTS if for_write else READABLE_ROOTS
    if not isinstance(root_name, str) or root_name not in allowed:
        raise HostToolError(f"root must be one of: {', '.join(allowed)}")
    raw = config.roots.get(root_name)
    if raw is None:
        raise HostToolError(f"root '{root_name}' is not configured")
    if root_name == "scratch":
        try:
            Path(raw).mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
        _grant_scratch_access(config, Path(raw))
    try:
        return root_name, Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HostToolError(f"root '{root_name}' does not exist on this host")


def _relative(path: object) -> PurePosixPath:
    if path is None:
        path = ""
    if not isinstance(path, str):
        raise HostToolError("path must be a string")
    if "\x00" in path or len(path) > 1024:
        raise HostToolError("invalid path")
    text = path.strip()
    if text.startswith(("~", "/", "\\")):
        raise HostToolError("path must be relative to the root")
    rel = PurePosixPath(text)
    if ".." in rel.parts:
        raise HostToolError("path must not contain '..'")
    return rel


def resolve_path(
    config: HostToolsConfig, root_name: object, path: object, *, for_write: bool = False,
) -> tuple[str, Path, Path, PurePosixPath]:
    """Return (root name, root, resolved target, relative path) or raise.

    The resolved target must sit inside the resolved root, so a symlink that
    points outside the root is refused. For writes, any symlink on the path
    below the root is refused, even one that stays inside it.
    """
    name, root = _root(config, root_name, for_write=for_write)
    rel = _relative(path)
    rel_parts = tuple(p for p in rel.parts if p not in ("", "."))
    reason = _denied_reason(name, root, rel_parts)
    if reason:
        raise HostToolError(reason)
    candidate = root.joinpath(*rel_parts)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise HostToolError("path could not be resolved")
    try:
        resolved_rel = resolved.relative_to(root)
    except ValueError:
        raise HostToolError("path resolves outside the root")
    reason = _denied_reason(name, root, resolved_rel.parts)
    if reason:
        raise HostToolError(reason)
    if for_write:
        probe = root
        for part in rel_parts:
            probe = probe / part
            if probe.is_symlink():
                raise HostToolError("writes do not follow symlinks")
    return name, root, resolved, PurePosixPath(*resolved_rel.parts) if resolved_rel.parts else PurePosixPath(".")


def _check_shell_path_arg(config: HostToolsConfig, cwd: Path, arg: str) -> str | None:
    """Refusal for a path argument to ls/tail, or None when it stays in a readable root."""
    if arg.startswith(("/", "~")) or "\x00" in arg:
        return f"path arguments must be relative: {arg!r}"
    if ".." in PurePosixPath(arg).parts:
        return f"path arguments must not contain '..': {arg!r}"
    try:
        resolved = (cwd / arg).resolve(strict=False)
    except (OSError, RuntimeError):
        return f"path could not be resolved: {arg!r}"
    for name in READABLE_ROOTS:
        raw = config.roots.get(name)
        if raw is None:
            continue
        try:
            root = Path(raw).resolve(strict=True)
            rel = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        reason = _denied_reason(name, root, rel.parts)
        if reason is None:
            return None
        return f"{reason}: {arg!r}"
    return f"path is outside the readable roots: {arg!r}"


# ── File tools ────────────────────────────────────────────────────────────


def fs_list(config: HostToolsConfig, arguments: dict) -> dict:
    name, _, target, rel = resolve_path(config, arguments.get("root"), arguments.get("path"))
    if not target.is_dir():
        raise HostToolError("not a directory")
    entries = []
    truncated = False
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        if child.name in DENIED_COMPONENTS or _is_denied_name(child.name):
            continue
        if name == "lifeos" and rel == PurePosixPath(".") and child.name in DENIED_REPO_DIRS:
            continue
        if len(entries) >= LIST_MAX_ENTRIES:
            truncated = True
            break
        try:
            if child.is_symlink():
                kind, size = "symlink", None
            elif child.is_dir():
                kind, size = "dir", None
            else:
                kind, size = "file", child.stat().st_size
        except OSError:
            kind, size = "unknown", None
        entries.append({"name": child.name, "type": kind, "size": size})
    return {"root": name, "path": str(rel), "entries": entries, "truncated": truncated}


def fs_read(config: HostToolsConfig, arguments: dict) -> dict:
    name, _, target, rel = resolve_path(config, arguments.get("root"), arguments.get("path"))
    if not target.is_file():
        raise HostToolError("not a file")
    limit = arguments.get("max_bytes") or READ_MAX_BYTES
    if not isinstance(limit, int) or limit < 1:
        raise HostToolError("max_bytes must be a positive integer")
    limit = min(limit, READ_MAX_BYTES)
    size = target.stat().st_size
    with open(target, "rb") as f:
        data = f.read(limit)
    if b"\x00" in data[:8192]:
        raise HostToolError("binary files are not returned")
    content = redact(data.decode("utf-8", errors="replace"))
    return {
        "root": name,
        "path": str(rel),
        "size": size,
        "truncated": size > len(data),
        "content": content,
    }


def _inside_git_work_tree(root: Path) -> bool:
    """Whether the vault is a git work tree, or a folder inside one."""
    try:
        probe = subprocess.run(
            [*_GIT_PREFIX, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _vault_commit(root: Path, rel: PurePosixPath) -> str | None:
    """Commit one vault path as the LifeOS account.

    The path is taken literally (no pattern matching), and git runs with the
    same no-hooks, no-fsmonitor settings as the shell's git commands. Only
    that path is committed, so unrelated uncommitted changes stay out.
    Returns the short commit sha, or None when there was nothing to commit.
    """
    git = (*_GIT_PREFIX, "-C", str(root))
    add = subprocess.run([*git, "add", "--", str(rel)], capture_output=True, text=True, timeout=30, check=False)
    if add.returncode != 0:
        raise HostToolError(f"written, but git add failed: {add.stderr.strip()[:300]}")
    commit = subprocess.run(
        [*git, "commit", "-m", f"Host tools: update {rel}", "--", str(rel)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if commit.returncode != 0:
        output = (commit.stdout + commit.stderr).strip()
        if "nothing to commit" in output or "no changes added" in output:
            return None
        raise HostToolError(f"written, but git commit failed: {output[:300]}")
    head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30, check=False)
    return head.stdout.strip() or None


def fs_write(config: HostToolsConfig, arguments: dict) -> dict:
    content = arguments.get("content")
    if not isinstance(content, str):
        raise HostToolError("content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > WRITE_MAX_BYTES:
        raise HostToolError(f"content is over {WRITE_MAX_BYTES} bytes")
    mode = arguments.get("mode") or "overwrite"
    if mode not in ("overwrite", "create", "append"):
        raise HostToolError("mode must be overwrite, create or append")
    name, root, target, rel = resolve_path(
        config, arguments.get("root"), arguments.get("path"), for_write=True,
    )
    if rel == PurePosixPath("."):
        raise HostToolError("path must name a file")
    if name == "vault" and config.vault_write_dirs:
        parts = rel.parts
        if not any(len(parts) > len(d) and parts[:len(d)] == d for d in config.vault_write_dirs):
            allowed = ", ".join("/".join(d) for d in config.vault_write_dirs)
            raise HostToolError(f"vault writes are limited to: {allowed}")
    if target.exists() and not target.is_file():
        raise HostToolError("path exists and is not a file")
    if mode == "create" and target.exists():
        raise HostToolError("file already exists")
    old_mode = target.stat().st_mode & 0o7777 if target.exists() else None
    if mode == "append" and target.exists():
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
    else:
        atomic_write_text(target, content)
        # The atomic write goes through a private temp file (mode 0600). Keep
        # an overwritten file's mode, and give a new file ordinary 0644, so
        # the run-as account can still read it.
        os.chmod(target, old_mode if old_mode is not None else NEW_FILE_MODE)
    result = {
        "root": name,
        "path": str(rel),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "commit": None,
    }
    if name == "vault" and config.vault_autocommit:
        if _inside_git_work_tree(root):
            result["commit"] = _vault_commit(root, rel)
        else:
            # A synced vault copy outside any git work tree: the file is saved,
            # but say plainly that nothing was committed.
            result["commit_skipped"] = "the vault on this host is not a git repository"
    return result


# ── Logs ──────────────────────────────────────────────────────────────────


def redact(text: str) -> str:
    return TELEGRAM_TOKEN_PATTERN.sub(REDACTED_TOKEN, text)


def logs(config: HostToolsConfig, arguments: dict) -> dict:
    service = arguments.get("service")
    if service not in LOG_SERVICES:
        raise HostToolError(f"service must be one of: {', '.join(LOG_SERVICES)}")
    lines = arguments.get("lines") or 100
    if not isinstance(lines, int) or lines < 1:
        raise HostToolError("lines must be a positive integer")
    lines = min(lines, LOG_MAX_LINES)
    contains = arguments.get("contains")
    if contains is not None and (not isinstance(contains, str) or len(contains) > 200):
        raise HostToolError("contains must be a string of at most 200 characters")
    _, _, target, _ = resolve_path(config, "logs", LOG_SERVICES[service])
    if not target.is_file():
        return {"service": service, "lines": [], "note": "no log file yet"}
    size = target.stat().st_size
    with open(target, "rb") as f:
        f.seek(max(0, size - LOG_SCAN_BYTES))
        tail = f.read().decode("utf-8", errors="replace").splitlines()
    if size > LOG_SCAN_BYTES and tail:
        tail = tail[1:]  # first line is likely cut mid-way
    if contains:
        tail = [line for line in tail if contains in line]
    return {"service": service, "lines": [redact(line) for line in tail[-lines:]]}


# ── Commands ──────────────────────────────────────────────────────────────


def _check_git_path_arg(config: HostToolsConfig, cwd: Path, arg: str) -> str | None:
    """Refusal for a non-flag git argument, or None.

    Every such argument is treated as a possible path and confined like an
    `ls` or `tail` argument. A `<rev>:<path>` argument has its path part
    checked against the denylist and the readable roots.
    """
    if arg.startswith((":(", ":/", ":!", ":^")):
        return f"git pathspec magic is not on the allowlist: {arg!r}"
    if ":" in arg:
        path = arg.split(":", 1)[1]
        if not path:
            return None
        parts = PurePosixPath(path.lstrip("./") or ".").parts
        reason = _denied_reason("lifeos", Path("."), parts)
        if reason:
            return f"{reason}: {arg!r}"
        return _check_shell_path_arg(config, cwd, path)
    return _check_shell_path_arg(config, cwd, arg)


def _git_args(
    rest: list[str], flags: frozenset[str], config: HostToolsConfig, cwd: Path,
) -> str | None:
    after_separator = False
    for arg in rest:
        if not after_separator:
            if arg == "--":
                after_separator = True
                continue
            if arg.startswith("-"):
                if arg in flags or _GIT_VALUE_FLAG_RE.match(arg):
                    continue
                return f"git option {arg!r} is not on the allowlist"
        why = _check_git_path_arg(config, cwd, arg)
        if why:
            return why
    return None


def _units(rest: list[str], allowed: tuple[str, ...] | dict, *, single: bool) -> tuple[list[str], str | None]:
    units = []
    for arg in rest:
        unit = arg.removesuffix(".service")
        if unit not in allowed:
            return [], f"unit {arg!r} is not on the allowlist"
        units.append(f"{unit}.service")
    if not units:
        return [], "name a LifeOS unit"
    if single and len(units) != 1:
        return [], "name exactly one unit"
    return units, None


def match_allowlist(config: HostToolsConfig, argv: list[str], cwd: Path) -> tuple[list[str] | None, str]:
    """Return (argv to execute, rule name) for an allowlisted command, or (None, reason)."""
    head, rest = argv[0], argv[1:]
    if head == "git" and rest:
        sub, args = rest[0], rest[1:]
        if sub == "status":
            why = _git_args(args, _GIT_STATUS_FLAGS, config, cwd)
            return (None, why) if why else ([*_GIT_PREFIX, "status", *args], "git status")
        if sub in ("log", "diff", "show"):
            flags = _GIT_LOG_FLAGS if sub in ("log", "show") else _GIT_DIFF_FLAGS
            why = _git_args(args, flags, config, cwd)
            return (None, why) if why else ([*_GIT_PREFIX, sub, *_GIT_DIFF_SAFETY, *args], f"git {sub}")
        return None, f"git {sub!r} is not on the allowlist"
    if head == "systemctl" and rest:
        verb, args = rest[0], [a for a in rest[1:] if a not in ("--no-pager", "-l", "--full")]
        if verb == "status":
            units, why = _units(args, LOG_SERVICES, single=False)
            return (None, why) if why else (["systemctl", "--no-pager", "--full", "status", *units], "systemctl status")
        if verb == "restart":
            units, why = _units(args, RESTARTABLE_UNITS, single=True)
            if why:
                return None, why
            return ["sudo", "-n", "/usr/bin/systemctl", "restart", *units], "systemctl restart"
        return None, f"systemctl {verb!r} is not on the allowlist"
    if head == "ls":
        for arg in rest:
            if arg.startswith("-"):
                if arg not in _LS_FLAGS:
                    return None, f"ls option {arg!r} is not on the allowlist"
                continue
            why = _check_shell_path_arg(config, cwd, arg)
            if why:
                return None, why
        return ["ls", *rest], "ls"
    if head == "tail":
        count, paths, i = None, [], 0
        while i < len(rest):
            arg = rest[i]
            if arg == "-n" and i + 1 < len(rest) and _COUNT_RE.match(rest[i + 1]):
                count, i = rest[i + 1], i + 2
                continue
            if arg.startswith("-n") and _COUNT_RE.match(arg[2:]):
                count, i = arg[2:], i + 1
                continue
            if arg.startswith("-"):
                return None, f"tail option {arg!r} is not on the allowlist"
            why = _check_shell_path_arg(config, cwd, arg)
            if why:
                return None, why
            paths.append(arg)
            i += 1
        if not paths:
            return None, "tail needs a file"
        return ["tail", "-n", count or "50", "--", *paths], "tail"
    return None, f"{head!r} is not on the allowlist"


def _validate_argv(argv: object) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise HostToolError("argv must be a non-empty list of strings")
    if len(argv) > ARGV_MAX_ITEMS:
        raise HostToolError(f"argv has more than {ARGV_MAX_ITEMS} items")
    for item in argv:
        if not isinstance(item, str) or "\x00" in item or len(item) > ARGV_MAX_ITEM_CHARS:
            raise HostToolError("argv items must be strings without NUL bytes")
    if not argv[0] or "/" in argv[0]:
        raise HostToolError("argv[0] must be a bare command name")
    return list(argv)


def check_run_as(user: str) -> str:
    """Return the run-as account name, or raise when it is unusable. Fails closed."""
    if not user:
        raise HostToolError(
            "lifeos_shell is not configured: set LIFEOS_HOST_TOOLS_RUN_AS to a "
            "low-privilege account (see docs/guides/host-tools.md)"
        )
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise HostToolError(f"run-as account {user!r} does not exist on this host")
    if entry.pw_uid == 0:
        raise HostToolError("run-as account must not be root")
    if entry.pw_uid == os.geteuid():
        raise HostToolError("run-as account must differ from the account LifeOS runs as")
    if shutil.which("sudo") is None:
        raise HostToolError("sudo is not installed")
    return user


def _cap(text: str) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= SHELL_OUTPUT_MAX_BYTES:
        return text, False
    return raw[-SHELL_OUTPUT_MAX_BYTES:].decode("utf-8", errors="replace"), True


def check_approval_argv(config: HostToolsConfig, argv: list[str], cwd: Path) -> str | None:
    """Refusal for a command that may not be sent for approval, or None.

    Every argument (and the value after any '=') that looks like a path must
    stay inside a readable root and off the denylist, as for ls and tail.
    """
    head = argv[0].lower()
    if head in NEVER_APPROVED_COMMANDS or _NEVER_APPROVED_RE.match(head):
        return f"{argv[0]!r} is never run through approval"
    if head == "git":
        if len(argv) > 1 and argv[1].startswith("-"):
            return f"git options before the subcommand are never run through approval: {argv[1]!r}"
        sub = argv[1] if len(argv) > 1 else ""
        if sub in NEVER_APPROVED_GIT:
            return f"git {sub!r} is never run through approval"
    if head == "git":
        for arg in argv[2:]:
            if not arg.startswith("-") and ":" in arg:
                why = _check_git_path_arg(config, cwd, arg)
                if why:
                    return why
    for arg in argv[1:]:
        candidates = [arg] + ([arg.split("=", 1)[1]] if "=" in arg else [])
        for value in candidates:
            if not value:
                continue
            if value.startswith(("/", "~")):
                return f"absolute and home paths are refused: {arg!r}"
            parts = PurePosixPath(value).parts
            if ".." in parts:
                return f"'..' is refused: {arg!r}"
            if any(p in DENIED_COMPONENTS or _is_denied_name(p) for p in parts):
                return f"denied path: {arg!r}"
            try:
                looks_like_path = "/" in value or (cwd / value).exists()
            except (OSError, ValueError):
                looks_like_path = True
            if looks_like_path:
                why = _check_shell_path_arg(config, cwd, value)
                if why:
                    return why
    return None


def _approval_details(*, caller: str, user: str, cwd_label: str, argv: list[str], digest: str) -> str:
    return (
        f"Caller: {caller}\n"
        f"Runs as: {user}\n"
        f"Directory: {cwd_label}\n"
        f"Command ({len(argv)} arguments, shown as shell words):\n"
        f"{shlex.join(argv)}\n"
        f"Command hash: {digest[:16]}"
    )


def _shell_via_approval(
    config: HostToolsConfig, argv: list[str], cwd: Path, cwd_label: str,
    user: str, caller: str, event: dict, reason: str,
):
    """Return (approval row, None) to run now, or (None, result) to return instead."""
    from api.services.host_approval import command_hash

    gate = config.approvals
    for row in gate.sweep():
        audit({
            "tool": "lifeos_shell", "caller": row.caller, "decision": f"approval-expired-{row.state}",
            "request_id": row.id, "command_hash": row.command_hash,
        }, path=config.audit_path)
    why = check_approval_argv(config, argv, cwd)
    if why:
        audit({**event, "decision": "refused", "reason": why}, path=config.audit_path)
        raise HostToolError(f"refused: {why}. Not sent for approval.")
    digest = command_hash(caller=caller, run_as=user, cwd=cwd_label, argv=argv)
    details = _approval_details(caller=caller, user=user, cwd_label=cwd_label, argv=argv, digest=digest)
    result = gate.check(caller=caller, command_hash=digest, details=details)
    row = result.request
    record = {**event, "command_hash": digest, "request_id": row.id if row else None, "reason": reason}
    if result.outcome == "run":
        return row, None
    audit({**record, "decision": f"approval-{result.outcome}", "detail": result.message}, path=config.audit_path)
    if result.outcome == "pending":
        return None, {
            "exit_code": None,
            "rule": "pending-approval",
            "request_id": row.short_id if row else None,
            "message": result.message,
        }
    raise HostToolError(f"refused: {result.message}")


def shell(config: HostToolsConfig, arguments: dict, *, caller: str) -> dict:
    argv = _validate_argv(arguments.get("argv"))
    cwd_root = arguments.get("cwd_root") or "lifeos"
    _, _, cwd, cwd_rel = resolve_path(config, cwd_root, arguments.get("cwd"))
    if not cwd.is_dir():
        raise HostToolError("cwd is not a directory")
    event = {"tool": "lifeos_shell", "caller": caller, "argv": argv, "cwd": f"{cwd_root}/{cwd_rel}"}
    try:
        user = check_run_as(config.run_as)
    except HostToolError as exc:
        audit({**event, "decision": "refused", "reason": str(exc)}, path=config.audit_path)
        raise
    execute, rule = match_allowlist(config, argv, cwd)
    approval = None
    if execute is None:
        reason = rule
        if not config.approval_enabled or config.approvals is None:
            audit({**event, "decision": "refused", "reason": reason}, path=config.audit_path)
            raise HostToolError(f"refused: {reason}. Commands outside the allowlist are not run.")
        approval, pending = _shell_via_approval(
            config, argv, cwd, event["cwd"], user, caller, event, reason,
        )
        if approval is None:
            return pending
        execute, rule = argv, "approved"
        event = {
            **event, "request_id": approval.id, "approver": approval.approver,
            "request_age_s": round(config.approvals.clock() - approval.created_at, 1),
        }
    command = ["sudo", "-n", "-u", user, "--", *execute]
    if not audit({**event, "decision": "started", "rule": rule}, path=config.audit_path):
        raise HostToolError("the audit log is not writable, so the command was not run")
    started = time.monotonic()
    try:
        result = config.runner(
            command,
            cwd=str(cwd),
            env={"PATH": SHELL_PATH, "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        audit({**event, "decision": rule, "exit": None, "timeout": True}, path=config.audit_path)
        if approval is not None:
            config.approvals.notify_ran(approval, None)
        raise HostToolError(f"timed out after {SHELL_TIMEOUT_SECONDS}s")
    stdout, out_cut = _cap(redact(result.stdout or ""))
    stderr, err_cut = _cap(redact(result.stderr or ""))
    audit({
        **event, "decision": rule, "exit": result.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout_bytes": len(result.stdout or ""), "stderr_bytes": len(result.stderr or ""),
    }, path=config.audit_path)
    if approval is not None:
        config.approvals.notify_ran(approval, result.returncode)
    return {
        "exit_code": result.returncode,
        "rule": rule,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_cut or err_cut,
    }


# ── MCP surface ───────────────────────────────────────────────────────────

_ROOT_DESC = "Root name. Paths are relative to it; absolute paths, '~' and '..' are refused."

HOST_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "lifeos_fs_list",
        "description": "List one directory on the LifeOS host, inside a named root "
                       "(vault, scratch, lifeos, logs). Secrets and credential files are hidden.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "enum": list(READABLE_ROOTS), "description": _ROOT_DESC},
                "path": {"type": "string", "description": "Directory relative to the root; empty for the root."},
            },
            "required": ["root"],
        },
    },
    {
        "name": "lifeos_fs_read",
        "description": "Read one text file on the LifeOS host, inside a named root "
                       "(vault, scratch, lifeos, logs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "enum": list(READABLE_ROOTS), "description": _ROOT_DESC},
                "path": {"type": "string", "description": "File path relative to the root."},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": READ_MAX_BYTES},
            },
            "required": ["root", "path"],
        },
    },
    {
        "name": "lifeos_fs_write",
        "description": "Write one text file in the vault or the scratch directory. A vault "
                       "write is committed when the vault on the host is a git repo; "
                       "otherwise the result says it was not. The operator may limit vault "
                       "writes to some folders; a refusal names them. The LifeOS code is "
                       "read-only; change it through a pull request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "enum": list(WRITABLE_ROOTS), "description": _ROOT_DESC},
                "path": {"type": "string", "description": "File path relative to the root."},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "create", "append"], "default": "overwrite"},
            },
            "required": ["root", "path", "content"],
        },
    },
    {
        "name": "lifeos_shell",
        "description": "Run one allowlisted command on the LifeOS host as a low-privilege "
                       "account: git status/log/diff/show, systemctl status or restart of a "
                       "LifeOS unit, ls, tail. Pass argv as a list; there is no shell. "
                       "Anything else is refused, unless the operator has turned on approval: "
                       "then it returns a pending request, and repeating the identical call "
                       "after the operator approves runs it once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": ARGV_MAX_ITEMS},
                "cwd_root": {"type": "string", "enum": list(READABLE_ROOTS), "default": "lifeos"},
                "cwd": {"type": "string", "description": "Working directory relative to cwd_root."},
            },
            "required": ["argv"],
        },
    },
    {
        "name": "lifeos_logs",
        "description": "Read the last lines of a LifeOS service's log, optionally only lines "
                       "containing a string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": list(LOG_SERVICES)},
                "lines": {"type": "integer", "minimum": 1, "maximum": LOG_MAX_LINES, "default": 100},
                "contains": {"type": "string"},
            },
            "required": ["service"],
        },
    },
]

HOST_TOOL_NAMES = frozenset(t["name"] for t in HOST_TOOL_SCHEMAS)
HOST_WRITE_TOOLS = frozenset({"lifeos_fs_write", "lifeos_shell"})


def call(config: HostToolsConfig, name: str, arguments: object, *, caller: str) -> dict:
    """Run one host tool. Returns an MCP tool result (content + isError)."""
    if not isinstance(arguments, dict):
        return _result({"error": "arguments must be an object"}, error=True)
    try:
        if name == "lifeos_shell":
            payload = shell(config, arguments, caller=caller)
        else:
            handler = {"lifeos_fs_list": fs_list, "lifeos_fs_read": fs_read,
                       "lifeos_fs_write": fs_write, "lifeos_logs": logs}.get(name)
            if handler is None:
                return _result({"error": f"Unknown tool: {name}"}, error=True)
            payload = handler(config, arguments)
            audit({
                "tool": name, "caller": caller, "decision": "ok",
                "root": payload.get("root"), "path": payload.get("path"),
                "service": payload.get("service"), "bytes": payload.get("bytes"),
                "sha256": payload.get("sha256"), "commit": payload.get("commit"),
                "commit_skipped": payload.get("commit_skipped"),
            }, path=config.audit_path)
    except HostToolError as exc:
        if name != "lifeos_shell":
            audit({"tool": name, "caller": caller, "decision": "refused", "reason": str(exc)},
                  path=config.audit_path)
        return _result({"error": str(exc)}, error=True)
    except OSError as exc:
        audit({"tool": name, "caller": caller, "decision": "failed", "reason": exc.strerror},
              path=config.audit_path)
        return _result({"error": f"failed: {exc.strerror or 'I/O error'}"}, error=True)
    return _result(payload, error=False)


def _result(payload: dict, *, error: bool) -> dict:
    if error:
        return {"content": [{"type": "text", "text": f"Error: {payload['error']}"}], "isError": True}
    if "content" in payload:  # fs_read: file text first, metadata after
        meta = {k: v for k, v in payload.items() if k != "content"}
        text = payload["content"] + f"\n\n[{json.dumps(meta)}]"
    else:
        text = json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload}
