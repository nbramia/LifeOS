"""Enforces docs/AGENTS.md's "Current behavior only" principle: docs, code
comments, docstrings, and test docstrings describe the system as it is, not
its development history. No comparisons to an older state, no review-process
play-by-play, no issue/PR numbers cited as a timeline -- git history holds
that narrative.

Scans (comments and docstrings only, not code or string literals used at
runtime): docs/ (excluding docs/adr, docs/archive, docs/plans -- ADRs are an
immutable historical record, archive/plans are explicitly ephemeral/historical
by design), AGENTS.md, CLAUDE.md, README.md, every .py file under api/,
scripts/, tests/, plus mcp_server.py, and the JS/CSS comments in web/*.html.

This is a ratchet, not a zero-tolerance gate: `narration_baseline.json` maps
each file with existing hits to its currently-allowed count. A file's
count may not exceed its baseline (or exceed 0 if it has no baseline entry).
Reducing a file's count is always allowed and doesn't require a baseline
edit; the test only fails on file counts that got worse, or exceeding the
per-file cap.

To lower a file's baseline after cleaning it up: rewrite the narration per
the rule above, re-run this test, and if the file's count decreased, update
(or remove) its entry in narration_baseline.json to match the new count (or
delete the entry once it reaches 0). Never raise a baseline number to make a
new violation pass -- fix the narration instead.
"""
import ast
import bisect
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "narration_baseline.json"

PATTERN = re.compile(
    r'\b(previously|used to|no longer|now (that|runs|uses|does|returns|caches|computes|reads|serves|renders|records|handles|lives|sends|includes|applies|filters|accepts|performs|takes|opens|keeps|points|reports|writes|rejects|carries|resolves|means)|'
    r'(was|were) (a |an |the |previously |once )?(added|removed|replaced|introduced|changed|reworked|rewritten|moved|dropped)|'
    r'before this|after this|improv(ed|ement|es)|faster than|instead of the (old|previous)|'
    r'the old (code|behaviou?r|implementation|version|path|endpoint|flow)|'
    r'(this|the) (change|fix|milestone|PR)\b|#[0-9]{3}\b|as of 20|regression|used to be|'
    r'has been (added|moved|changed|removed|replaced|rewritten)|historically|formerly|in the past|'
    r'earlier (version|implementation|code)|round [0-9]|reviewer|finding [0-9]|bugfix|belt-and|'
    r'orchestrator|coordinator)',
    re.IGNORECASE,
)

# A short allowlist of present-tense false positives the pattern above can
# otherwise catch inside a comment/docstring that merely mentions them.
ALLOWLIST = re.compile(r"datetime\.now\(|\bnow\s*=|time\.time\(\)")

_HASH_RE = re.compile("#")
_DOC_EXCLUDED_DIRS = ("adr", "archive", "plans")


def _line_offsets(src: str) -> list[int]:
    offs = [0]
    for line in src.splitlines(keepends=True):
        offs.append(offs[-1] + len(line))
    return offs


def _py_lines_to_check(src: str, path: str):
    """Yield (lineno, text) for every comment and docstring line in a Python
    file. Comments are found by locating every literal '#' and excluding the
    ones that fall inside a string-literal span, using spans collected from
    the same ast.parse() pass that finds docstrings -- this avoids a second,
    much slower tokenize() pass over every file."""
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return
    offs = _line_offsets(src)
    starts: list[int] = []
    ends: list[int] = []
    docstring_lines: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            starts.append(offs[node.lineno - 1] + node.col_offset)
            ends.append(offs[node.end_lineno - 1] + node.end_col_offset)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                const = body[0].value
                for i, line in enumerate(const.value.split("\n")):
                    docstring_lines.append((const.lineno + i, line))
    order = sorted(range(len(starts)), key=lambda k: starts[k])
    starts_sorted = [starts[k] for k in order]
    ends_sorted = [ends[k] for k in order]
    for m in _HASH_RE.finditer(src):
        i = m.start()
        k = bisect.bisect_right(starts_sorted, i) - 1
        if k >= 0 and starts_sorted[k] <= i < ends_sorted[k]:
            continue
        j = src.find("\n", i)
        if j == -1:
            j = len(src)
        lineno = src.count("\n", 0, i) + 1
        yield (lineno, src[i:j])
    yield from docstring_lines


def _js_comments(src: str, base_line: int):
    """String/template-literal-aware scan for // and /* */ comments in a
    <script> block (mirrors the tokenizer used to verify web/crm.html edits
    in this cleanup, minimal version: only needs to find comment spans, not
    reconstruct the surrounding code)."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i + 2)
            if j == -1:
                j = n
            out.append((base_line + src.count("\n", 0, i), src[i:j]))
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                j = n - 2
            start_line = base_line + src.count("\n", 0, i)
            for k, line in enumerate(src[i + 2:j].split("\n")):
                out.append((start_line + k, line))
            i = j + 2
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
                if ch == quote:
                    break
            continue
        if c == "`":
            i += 1
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "`":
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return out


def _html_lines_to_check(src: str):
    out = []
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", src, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        base_line = src.count("\n", 0, m.start(1)) + 1
        out.extend(_js_comments(block, base_line))
    for m in re.finditer(r"<style\b[^>]*>(.*?)</style>", src, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        base_line = src.count("\n", 0, m.start(1)) + 1
        for cm in re.finditer(r"/\*(.*?)\*/", block, re.DOTALL):
            start = base_line + block.count("\n", 0, cm.start())
            for i, line in enumerate(cm.group(1).split("\n")):
                out.append((start + i, line))
    return out


def _scan_file(path: Path, rel: str) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        pairs = _py_lines_to_check(text, str(path))
    elif path.suffix == ".html":
        pairs = _html_lines_to_check(text)
    else:
        pairs = enumerate(text.split("\n"), 1)
    hits = []
    for lineno, line in pairs:
        if PATTERN.search(line) and not ALLOWLIST.search(line):
            hits.append(f"{rel}:{lineno}: {line.strip()[:160]}")
    return hits


def _gather_targets() -> list[Path]:
    targets = []
    for f in (REPO / "docs").rglob("*.md"):
        parts = f.relative_to(REPO).parts
        if len(parts) > 1 and parts[1] in _DOC_EXCLUDED_DIRS:
            continue
        targets.append(f)
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        p = REPO / name
        if p.exists():
            targets.append(p)
    for d in ("api", "scripts", "tests"):
        targets.extend((REPO / d).rglob("*.py"))
    mcp = REPO / "mcp_server.py"
    if mcp.exists():
        targets.append(mcp)
    targets.extend((REPO / "web").glob("*.html"))
    return targets


def _current_hit_counts() -> tuple[dict[str, int], dict[str, list[str]]]:
    counts: dict[str, int] = {}
    hits_by_file: dict[str, list[str]] = {}
    for f in _gather_targets():
        rel = str(f.relative_to(REPO))
        hits = _scan_file(f, rel)
        if hits:
            counts[rel] = len(hits)
            hits_by_file[rel] = hits
    return counts, hits_by_file


def _load_baseline() -> dict[str, int]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text())


@pytest.mark.unit
def test_no_change_narration_beyond_baseline():
    baseline = _load_baseline()
    counts, hits_by_file = _current_hit_counts()

    violations = []
    for rel, count in counts.items():
        allowed = baseline.get(rel, 0)
        if count > allowed:
            sample = hits_by_file[rel][:10]
            violations.append(
                f"{rel}: {count} hits, {allowed} allowed\n  "
                + "\n  ".join(sample)
                + ("\n  ..." if len(hits_by_file[rel]) > 10 else "")
            )

    assert not violations, (
        "Change-narration found beyond the checked-in baseline "
        "(tests/narration_baseline.json). Rewrite these to describe current "
        "behavior only (see docs/AGENTS.md 'Current behavior only'), or if "
        "a hit is a genuine false positive, extend the ALLOWLIST in this "
        "test:\n\n" + "\n\n".join(violations)
    )
