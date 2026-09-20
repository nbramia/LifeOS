#!/usr/bin/env python3
"""Decide which retained lanes a candidate's changed set can affect.

The hosted candidate-verification runner feeds the changed-file list to this
script and acts on its answer: which lanes to execute, or that the candidate
is docs-only and no lane can observe it. The docs-only rule is the one
``scripts/test.sh``'s ``decide_plan`` applies locally, character for
character, so the hosted gate and the pre-push plan never disagree about
what counts as documentation. Everything here fails closed: an empty or
unavailable changed set executes every retained lane.

Modes:

* ``executed`` -- run ``lanes`` (``fast-unit`` always; ``browser-free`` when
  a ``web/`` file changed).
* ``docs-only`` -- every changed file is documentation and none is a
  dependency manifest; no lane runs and the publisher records the rule.
"""
from __future__ import annotations

import dataclasses
import re
import sys
from typing import Sequence

ALL_LANES = ("fast-unit", "browser-free")

# Mirrors decide_plan() in scripts/test.sh: a dependency manifest is code-
# affecting whatever its extension, and a file is documentation when its
# extension is md/txt/rst or it lives under docs/.
_MANIFEST = re.compile(r"(^|/)(requirements|constraints)[^/]*\.txt$")
_DOCS = re.compile(r"\.(md|txt|rst)$|^docs/")
_WEB = re.compile(r"^web/")


@dataclasses.dataclass(frozen=True)
class Selection:
    mode: str
    lanes: tuple[str, ...]
    reason: str


def classify(changed: Sequence[str]) -> Selection:
    files = [line.strip() for line in changed if line.strip()]
    if not files:
        return Selection("executed", ALL_LANES, "changed set unavailable: running every retained lane")
    if not any(_MANIFEST.search(f) for f in files) and all(_DOCS.search(f) for f in files):
        return Selection(
            "docs-only", (),
            "every changed file is documentation (md/txt/rst or under docs/) and none is a dependency manifest",
        )
    if any(_WEB.search(f) for f in files):
        return Selection("executed", ALL_LANES, "a web/ file changed: the server-free browser lane can be affected")
    return Selection("executed", ("fast-unit",), "no web/ file changed: the server-free browser lane cannot be affected")


def main(argv: Sequence[str] | None = None) -> int:
    selection = classify(sys.stdin.read().splitlines())
    print(f"mode={selection.mode}")
    print(f"lanes={','.join(selection.lanes)}")
    print(f"reason={selection.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
