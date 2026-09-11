"""
Deterministic capture of journal fragments into `LifeOS/Log/Journal/YYYY-MM-DD.md`.

The `journal` persona has no tool that can perform this write:
`lifeos_vault_write` exists only in the MCP catalog, not in the native
agentic loop's `TOOL_DEFINITIONS`. Capture is therefore done here, in code,
before the model is involved at all — the fragment survives whether or not
the model does anything useful, and the model is left only the
*interpretation* job (does this fragment warrant a task or a schedule? does
it need one clarifying question?).

Existing day files written under the old `Personal/Log/` location are moved
(never copied) into the current directory the first time each is touched —
see `_migrate_other_legacy_day_files` and the in-flock migration inside
`capture_fragment` — so history stays in one place without a separate
operator-run migration step.

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
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# The journal persona's registry name (config/telegram_bots.json). Capture is
# keyed off this id on every surface — Telegram bot, `/chat`, ring ingest.
JOURNAL_PERSONA_ID = "journal"

# Fixed capture target. Not caller-supplied and not configurable: it is what
# keeps this write path from being able to reach `Personal/Journal/`, the
# gsheet_sync-generated subtree that `api/routes/vault.py` reserves. A
# subdirectory of `LifeOS/Log/` rather than the container itself, so this
# never mixes into `LifeOS/Log/Pebble/`, which the Pebble producer owns.
_LOG_DIR_PARTS = ("LifeOS", "Log", "Journal")

# A legacy day-file location. Read-only from here on: nothing writes here,
# but leftover files are moved out of it (never copied) so a fresh capture
# doesn't leave history split across two directories.
_LEGACY_LOG_DIR_PARTS = ("Personal", "Log")

# Matches a day file's name exactly, so migration never touches an unrelated
# file that happens to sit in the legacy directory.
_DAY_FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

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

    path: str  # vault-relative, e.g. "LifeOS/Log/Journal/2026-08-24.md"
    created: bool  # True if this fragment started the day's file


def log_path_for(day: date) -> str:
    """Vault-relative path of a day's capture log."""
    return "/".join((*_LOG_DIR_PARTS, f"{day.isoformat()}.md"))


def _frontmatter(day: date) -> str:
    """The header the vault's Dataview queries over this log depend on.
    Written exactly once, on the first fragment of the day."""
    return f"---\ntype: log\ndate: {day.isoformat()}\n---\n"


def _bullet(text: str, now: datetime) -> str:
    return f"- {now:%H:%M} · {_NEWLINE_RUN.sub(' ', text.strip())}\n"


def _take_legacy_content(legacy_path: Path) -> Optional[str]:
    """Read and remove a leftover legacy day file for the day being captured
    right now. Called only from inside the caller's own flock on the
    new-location target, while that target is still empty — so this is the
    one case where the migration genuinely needs to run under the append's
    own lock rather than a plain atomic rename: the destination already
    exists as an open file descriptor the caller is about to write into,
    there is nothing to `os.replace` onto.
    """
    try:
        if not legacy_path.is_file():
            return None
        text = legacy_path.read_text(encoding="utf-8")
        legacy_path.unlink()
        return text
    except OSError:
        return None


def _migrate_other_legacy_day_files(vault_root: Path, *, skip_name: str) -> None:
    """Move every leftover legacy day file except the one being captured
    right now (that one is migrated inside `capture_fragment`'s own flock,
    below) into the current capture directory.

    Race-safe without any extra locking: `os.replace` is an atomic rename,
    so when two fragments (for different days, or a day other than today)
    reach this at once, whichever replace the kernel actually executes
    first moves the file; the other's source is already gone by the time
    its own replace runs, which raises `FileNotFoundError` rather than
    silently recreating or overwriting anything. Never overwrites an
    existing destination file — a file already at the new location (however
    it got there) is left alone rather than clobbered.
    """
    old_dir = vault_root.joinpath(*_LEGACY_LOG_DIR_PARTS)
    if not old_dir.is_dir():
        return
    new_dir = vault_root.joinpath(*_LOG_DIR_PARTS)
    for entry in sorted(old_dir.iterdir()):
        if entry.name == skip_name or not _DAY_FILE_NAME.match(entry.name):
            continue
        dest = new_dir / entry.name
        if dest.exists():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(entry, dest)
        except FileNotFoundError:
            pass  # already migrated by a concurrent capture
        except OSError as e:
            logger.warning(
                "journal log migration: could not move a legacy day file (errno=%s)",
                e.errno,
            )


def capture_fragment(text: str, *, now: Optional[datetime] = None) -> CaptureResult:
    """Append one fragment to today's capture log and return once it is on disk.

    Raises `JournalCaptureError` if the fragment was not written — never
    returns a result the caller could mistake for a successful capture.

    Concurrency: the day file is opened once, in append mode, and held under an
    exclusive `flock` for the whole read-decide-write. Two fragments racing on
    the first write of the day therefore serialize: the loser sees a non-empty
    file and appends its bullet below the header rather than writing a second
    one, and neither can interleave a partial line into the other's write.

    Migration: a leftover legacy `Personal/Log/<today>.md` is moved (not
    copied) into place under this same lock, before anything is written — so
    the file this fragment appends to already carries prior content and no
    duplicate is left behind. Every other leftover legacy day file is swept
    on the way in (see `_migrate_other_legacy_day_files`), so history ends
    up in one place regardless of which day is captured next.
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
    if target.parent != vault_root.joinpath(*_LOG_DIR_PARTS):
        raise JournalCaptureError(
            f"journal capture target resolved outside {'/'.join(_LOG_DIR_PARTS)}/"
        )

    legacy_path = vault_root.joinpath(*_LEGACY_LOG_DIR_PARTS, f"{day.isoformat()}.md")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _migrate_other_legacy_day_files(vault_root, skip_name=f"{day.isoformat()}.md")
        # Binary mode: the trailing-byte probe below must not have to decode a
        # partial multi-byte character out of a hand-edited file.
        with target.open("ab+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    legacy_text = _take_legacy_content(legacy_path)
                    if legacy_text is not None:
                        prefix = legacy_text if legacy_text.endswith("\n") else legacy_text + "\n"
                        chunk = prefix + _bullet(fragment, now)
                        created = False
                    else:
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
