"""Shared atomic file-write helper.

Writes a temp file in the target's own directory, fsyncs it, then renames it
into place with ``os.replace``. A reader (Obsidian, Syncthing, a watcher) that
opens the target path mid-write always sees either the old content or the new
content in full — never a partial write, since ``os.replace`` is atomic on
the same filesystem and a temp file in the same directory guarantees that.

Used by ``task_manager.py`` and ``scheduler_store.py`` so both vault stores
share one atomic-write implementation instead of each hand-rolling it.
"""
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path``, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_lines(
    path: Path,
    lines: list[str],
    newline: str = "\n",
    trailing_newline: bool = True,
) -> None:
    """Atomically write ``lines`` to ``path``, joined by ``newline``.

    ``newline`` and ``trailing_newline`` let a caller preserve a file's
    original line-terminator convention (e.g. CRLF) and whether it ended
    with a trailing terminator, across a rewrite that only touches a few
    lines. ``scheduler_store.py`` never calls this helper (it writes whole
    files via ``atomic_write_text``), so its behavior is unaffected by
    these defaults.
    """
    content = newline.join(lines)
    if trailing_newline:
        content += newline
    atomic_write_text(path, content)
