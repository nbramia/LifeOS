"""
Deterministic capture of journal fragments into `Personal/Log/YYYY-MM-DD.md` (#674).

The `journal` persona (#659) used to be told to append the fragment itself via
`lifeos_vault_write`. That tool exists only in the MCP catalog, not in the
native agentic loop's `TOOL_DEFINITIONS`, so the model could not call it — it
emitted the bullet as chat prose instead and every fragment was silently lost
while the reply looked like a successful capture.

Capture is therefore done here, in code, before the model is involved at all:
the fragment survives whether or not the model does anything useful, and the
model is left only the *interpretation* job (does this fragment warrant a task
or a schedule? does it need one clarifying question?).

Privacy: the fragment text is never logged and never appears in a raised
error. Only the vault-relative path of the day file is ever recorded.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# The journal persona's registry name (config/telegram_bots.json). Capture is
# keyed off this id on every surface — Telegram bot, `/chat`, ring ingest.
JOURNAL_PERSONA_ID = "journal"

# Fixed capture target. Not caller-supplied and not configurable: it is what
# keeps this write path from being able to reach `Personal/Journal/`, the
# gsheet_sync-generated subtree that `api/routes/vault.py` reserves.
_LOG_DIR_PARTS = ("Personal", "Log")

# Collapse a multi-line fragment onto the single line a bullet needs. Only
# newlines (and the whitespace hugging them) are touched — the fragment is
# otherwise written exactly as given: not paraphrased, tidied, or summarized.
_NEWLINE_RUN = re.compile(r"\s*\n\s*")


class JournalCaptureError(RuntimeError):
    """A fragment could not be durably written.

    Carries no fragment text — callers surface this to a user or an HTTP
    response, and the whole point of the log is that its content stays private.
    """


@dataclass(frozen=True)
class CaptureResult:
    """Proof that a fragment reached disk. Returned only after fsync."""

    path: str  # vault-relative, e.g. "Personal/Log/2026-08-24.md"
    created: bool  # True if this fragment started the day's file


def log_path_for(day: date) -> str:
    """Vault-relative path of a day's capture log."""
    return f"{_LOG_DIR_PARTS[0]}/{_LOG_DIR_PARTS[1]}/{day.isoformat()}.md"


def _frontmatter(day: date) -> str:
    """The header the vault's Dataview queries over this log depend on.
    Written exactly once, on the first fragment of the day."""
    return f"---\ntype: log\ndate: {day.isoformat()}\n---\n"


def _bullet(text: str, now: datetime) -> str:
    return f"- {now:%H:%M} · {_NEWLINE_RUN.sub(' ', text.strip())}\n"


def capture_fragment(text: str, *, now: Optional[datetime] = None) -> CaptureResult:
    """Append one fragment to today's capture log and return once it is on disk.

    Raises `JournalCaptureError` if the fragment was not written — never
    returns a result the caller could mistake for a successful capture.

    Concurrency: the day file is opened once, in append mode, and held under an
    exclusive `flock` for the whole read-decide-write. Two fragments racing on
    the first write of the day therefore serialize: the loser sees a non-empty
    file and appends its bullet below the header rather than writing a second
    one, and neither can interleave a partial line into the other's write.
    """
    fragment = (text or "").strip()
    if not fragment:
        raise JournalCaptureError("refusing to capture an empty fragment")

    now = now or datetime.now()
    day = now.date()
    rel = log_path_for(day)

    vault_root = settings.vault_path.resolve()
    target = (vault_root / rel).resolve()
    # Defence in depth. `rel` is built from module constants, so this can only
    # fire if _LOG_DIR_PARTS itself is ever changed to something unsafe — but a
    # write path that quietly relocates is exactly the failure this issue is
    # about, so it is checked rather than assumed.
    if target.parent != (vault_root / _LOG_DIR_PARTS[0] / _LOG_DIR_PARTS[1]):
        raise JournalCaptureError("journal capture target resolved outside Personal/Log/")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Binary mode: the trailing-byte probe below must not have to decode a
        # partial multi-byte character out of a hand-edited file.
        with target.open("ab+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    chunk = _frontmatter(day) + _bullet(fragment, now)
                    created = True
                else:
                    # A day file that doesn't end in a newline (hand-edited, or
                    # a previous write cut short) would otherwise glue the new
                    # bullet onto the last line.
                    f.seek(size - 1)
                    needs_newline = f.read(1) != b"\n"
                    chunk = ("\n" if needs_newline else "") + _bullet(fragment, now)
                    created = False
                f.seek(0, os.SEEK_END)
                f.write(chunk.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        # `from None`: an OSError's message carries errno and a path, but
        # chaining it would put the whole write frame — and the local holding
        # the fragment — into any traceback rendered downstream.
        logger.error("Journal capture failed for %s: %s", rel, e.strerror or e.__class__.__name__)
        raise JournalCaptureError(f"could not write {rel}") from None

    logger.info("Journal capture: appended a fragment to %s (created=%s)", rel, created)
    return CaptureResult(path=rel, created=created)
