"""Line rules shared by the Markdown-backed stores (tasks and schedules).

Both stores read their files back with ``str.splitlines()``, which ends a
line at every character in ``LINE_BREAKS``, not only ``\\n``. A value written
into an entry line is therefore a single-line value: it carries none of
these characters. A multi-line body is split with ``split_body_lines`` so
each piece it writes is one line exactly as ``splitlines()`` reads it back.
"""
import re

# Every character `str.splitlines()` treats as a line boundary.
LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")

_BODY_SPLIT_RE = re.compile("\r\n|[" + re.escape("".join(sorted(LINE_BREAKS))) + "]")


def has_line_break(value: str) -> bool:
    """True when ``value`` would span more than one line once re-read."""
    return any(ch in LINE_BREAKS for ch in value)


def split_body_lines(text: str) -> list[str]:
    """Split ``text`` at every ``splitlines()`` boundary (``\\r\\n`` counts as
    one), keeping empty pieces the way ``str.split("\\n")`` does, so a body
    that uses only ``\\n`` splits exactly as before and a trailing newline
    still round-trips."""
    return _BODY_SPLIT_RE.split(text)
