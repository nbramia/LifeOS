"""Copy each Telegram notice LifeOS sends on its own to the shared bot-notices log.

A bot can't read back what it sent, so every sender that posts a notice to the
operator (an alert, a reminder, an agent-worker status) also appends a copy here
for an outside watcher to read. Replies to the operator's own messages are not
notices and are never recorded.

The log is JSON Lines at ``~/.local/state/bot-notices/notices.jsonl``
(``$BOT_NOTICE_LOG`` overrides the path), one object per message::

    {"ts": "2026-01-01T09:00:00Z", "bot": "lifeos", "source": "LifeOS/server-watchdog", "text": "..."}

Each line is appended whole under an exclusive ``flock``, waited on for at
most a second. On a host that only sends (a Mac running the Apple data
agent), ``$BOT_NOTICE_REMOTE_HOST`` names the ssh host that keeps the log, and
the line is appended there with ``flock(1)``, bounded at 15 seconds. Recording
is best-effort: it runs after the send, and a failure is swallowed so it can
never fail or delay a notice. Tokens and chat ids are never written.

This module is stdlib-only so the shell senders can run it by path with the
system interpreter, the text on stdin::

    printf '%s' "$message" | python3 -I api/services/bot_notices.py LifeOS/server-watchdog
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = "~/.local/state/bot-notices/notices.jsonl"
LOCK_TIMEOUT_SECONDS = 1.0
REMOTE_TIMEOUT_SECONDS = 15
# Run by the remote user's login shell, so ``~`` is that user's home.
REMOTE_APPEND = (
    "mkdir -p ~/.local/state/bot-notices && "
    "flock -w 5 ~/.local/state/bot-notices/notices.jsonl "
    "tee -a ~/.local/state/bot-notices/notices.jsonl > /dev/null"
)


def log_path() -> Path | None:
    """The log to append to, or None when neither ``$BOT_NOTICE_LOG`` nor ``$HOME`` is set.

    A process with no ``$HOME`` (a sender run with a scrubbed environment)
    writes nothing rather than guess whose home the log belongs in.
    """
    override = os.environ.get("BOT_NOTICE_LOG")
    if override:
        return Path(override).expanduser()
    if not os.environ.get("HOME"):
        return None
    return Path(DEFAULT_LOG).expanduser()


def _lock(f, timeout: float = LOCK_TIMEOUT_SECONDS) -> bool:
    """Take the exclusive lock, giving up after ``timeout`` so a send is never held up."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _append_remote(host: str, line: bytes) -> bool:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, REMOTE_APPEND],
        input=line,
        capture_output=True,
        timeout=REMOTE_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def record(text: str, source: str, bot: str = "lifeos") -> bool:
    """Append one notice to the log. Returns False instead of raising on failure."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": bot,
            "source": source,
            "text": text,
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        remote = os.environ.get("BOT_NOTICE_REMOTE_HOST")
        if remote and not os.environ.get("BOT_NOTICE_LOG"):
            return _append_remote(remote, line)
        path = log_path()
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            if not _lock(f):
                return False
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def _drop_root() -> None:
    """Write as the checkout's owner when a sender runs as root.

    The network watchdog runs as root; left as root it would write to
    ``/root`` (or create a root-owned log the other senders can't append to).
    """
    import pwd

    st = os.stat(__file__)
    if st.st_uid == 0:
        return
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)
    os.environ["HOME"] = pwd.getpwuid(st.st_uid).pw_dir


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: bot_notices.py <source>  (notice text on stdin)", file=sys.stderr)
        return 2
    try:
        if os.geteuid() == 0:
            _drop_root()
    except Exception:
        return 1
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return 0 if record(text, argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
