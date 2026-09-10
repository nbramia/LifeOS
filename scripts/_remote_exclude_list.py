#!/usr/bin/env python3
"""Turn `git ls-files -z` output into an rsync --exclude-from pattern file.

Reads NUL-separated relative paths from stdin (the output of `git ls-files
-z --others --ignored --exclude-standard --directory`) and writes NUL-
separated rsync exclude patterns to stdout, for use with `rsync --from0
--exclude-from=FILE`. Each path is anchored to the transfer root with a
leading "/" and escaped so it matches its own filename literally.

NUL-separated output (paired with the caller's --from0) is what makes an
ignored filename containing a literal embedded newline transferable as
one exclude pattern: rsync's default --exclude-from format is newline-
delimited, so a pattern with an embedded newline would silently split
into two separate (and wrong) patterns there. NUL cannot appear in a
POSIX filename at all, so it's a safe, unambiguous delimiter for any
filename git can produce.

Escaping: rsync (per rsync(1), "WILDCARD MATCHING RULES") does a plain,
un-escaped *string* match for any pattern containing none of `*`, `?`,
`[` — backslash has no special meaning there, so a literal backslash in
the filename must be left alone. But the moment a pattern contains any of
those three characters, backslash becomes an escape-introducer for the
*entire* pattern, and every other literal backslash must then be doubled
or it gets misread as escaping whatever follows it (rsync's own manual
gives exactly this example: "foo\\bar" matches literally, but
"foo\\bar*" must become "foo\\\\bar*"). This module picks the right mode
per filename rather than always escaping, which would break the common
case of a literal backslash in a filename that has no wildcard character
in it at all.
"""
from __future__ import annotations

import sys

_WILDCARD_CHARS = "*?["


def escape(name: str) -> str:
    if not any(c in _WILDCARD_CHARS for c in name):
        return name
    out = []
    for c in name:
        if c == "\\" or c in _WILDCARD_CHARS:
            out.append("\\")
        out.append(c)
    return "".join(out)


def main() -> int:
    data = sys.stdin.buffer.read()
    patterns = []
    for raw in data.split(b"\0"):
        if not raw:
            continue
        text = raw.decode("utf-8", "surrogateescape")
        pattern = "/" + escape(text)
        patterns.append(pattern.encode("utf-8", "surrogateescape"))
    sys.stdout.buffer.write(b"\0".join(patterns))
    if patterns:
        sys.stdout.buffer.write(b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
