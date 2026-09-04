"""Enforces docs/AGENTS.md's "Current behavior only" principle: docs, code
comments, docstrings, and test docstrings describe the system as it is, not
its development history. No comparisons to an older state, no review-process
play-by-play, no issue/PR numbers cited as a timeline -- git history holds
that narrative.

Scans (comments, docstrings, and any bare string-expression statement --
never code that runs, and never an ordinary string literal used as a value):
docs/ (excluding docs/adr, docs/archive, docs/plans -- ADRs are an immutable
historical record, archive/plans are explicitly ephemeral/historical by
design), AGENTS.md, CLAUDE.md, README.md, every .py file under api/,
scripts/, tests/, plus mcp_server.py, the JS/CSS comments in web/*.html
(including HTML `<!-- -->` comments), and every .js file under web/
(including subdirectories such as web/chat/ and web/agents/).

This is a ratchet, not a zero-tolerance gate, and it's line-keyed, not
count-keyed: `narration_baseline.json` maps each file to the set of
whitespace-normalized offending-line texts it was already carrying. A line
whose normalized text isn't in its file's set is a new violation. Deleting
an offending line (fixing it, or deleting the file) never requires a
baseline edit -- the set is a ceiling, not an exact match, so a shrunk file
just carries unused slack. There is deliberately no way to trade one
baseline entry for a different new violation: swapping out old narration
for new narration in the same file does not free up any allowance, because
every current hit is checked against the set independently.

To actually shrink a file's baseline (tighten the ratchet, not just pass the
test): rewrite the narration out per the rule above, re-run this test, and
if it passes, delete that file's now-unused entries from
narration_baseline.json (or the whole file key once it has none left). This
is always safe to do immediately after a cleanup -- if an entry you deleted
turns out to still be needed, the test fails and tells you which line.
Never add or edit an entry to make a *new* violation pass; only remove
entries that verifiably describe current behavior.
"""
import ast
import bisect
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "narration_baseline.json"

# The issue-number-as-history clause is deliberately its own top-level
# alternative, NOT inside the `\b( ... )` group above: a hash mark begins
# with a non-word character, so nesting it inside a group whose entry point
# is asserted by `\b` made it unreachable (a leading `\b` requires a word
# character immediately after the boundary). `(?<![\w#])` instead of `\b`
# correctly allows a hash-plus-digits reference at the start of a string or
# after whitespace/punctuation, while still rejecting a hex colour literal
# (no digits follow the hash) and a hash mark immediately preceded by a word
# character (not itself meaningful here, but excluded to avoid matching
# inside identifiers or URLs).
PATTERN = re.compile(
    r'\b(previously|used to|no longer|now (that|runs|uses|does|returns|caches|computes|reads|serves|renders|records|handles|lives|sends|includes|applies|filters|accepts|performs|takes|opens|keeps|points|reports|writes|rejects|carries|resolves|means)|'
    r'(was|were) (a |an |the |previously |once )?(added|removed|replaced|introduced|changed|reworked|rewritten|moved|dropped)|'
    r'before this|after this|improv(ed|ement|es)|faster than|instead of the (old|previous)|'
    r'the old (code|behaviou?r|implementation|version|path|endpoint|flow)|'
    r'(this|the) (change|fix|milestone|PR)\b|as of 20|used to be|'
    r'has been (added|moved|changed|removed|replaced|rewritten)|historically|formerly|in the past|'
    r'earlier (version|implementation|code)|round [0-9]|finding [0-9]|review(er)? (round|finding))'
    r'|(?<![\w#])#\d{3,5}\b',
    re.IGNORECASE,
)

# A short allowlist of present-tense false positives the pattern above can
# otherwise catch inside a comment/docstring that merely mentions them. Each
# entry masks only its own matched span (replaced with spaces of the same
# length) before PATTERN runs, never the whole line -- so a line that also
# carries a real violation elsewhere is still caught.
ALLOWLIST = re.compile(r"datetime\.now\(|\bnow\s*=|time\.time\(\)")

_HASH_RE = re.compile("#")
_DOC_EXCLUDED_DIRS = ("adr", "archive", "plans")


def _line_offsets(src: str) -> list[int]:
    offs = [0]
    for line in src.splitlines(keepends=True):
        offs.append(offs[-1] + len(line))
    return offs


def _py_lines_to_check(src: str, path: str):
    """Yield (lineno, text) for every comment and every bare string-expression
    statement (docstrings included, but not limited to a body's first
    statement -- a stray string literal used as a mid-function "comment" is
    just as much an `Expr(Constant(str))` node as a real docstring, and is
    scanned the same way) in a Python file. Comments are found by locating
    every literal '#' and excluding the ones that fall inside a string-literal
    span, using spans collected from the same ast.parse() pass that finds the
    string expressions -- this avoids a second, much slower tokenize() pass
    over every file."""
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return
    offs = _line_offsets(src)
    starts: list[int] = []
    ends: list[int] = []
    string_expr_lines: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            starts.append(offs[node.lineno - 1] + node.col_offset)
            ends.append(offs[node.end_lineno - 1] + node.end_col_offset)
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            const = node.value
            for i, line in enumerate(const.value.split("\n")):
                string_expr_lines.append((const.lineno + i, line))
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
    yield from string_expr_lines


def _js_comments(src: str, base_line: int):
    """String/template-literal-aware scan for // and /* */ comments in a
    JavaScript source (a whole .js file, or a <script> block extracted from
    HTML): tracks quoted strings, template literals, and regex-vs-division
    just enough to find comment spans without mistaking one inside a string
    for a real comment."""
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


def _html_comment_lines(src: str):
    """`<!-- ... -->` comments anywhere in an HTML file, independent of
    whether they sit inside/outside a <script> or <style> block."""
    out = []
    for m in re.finditer(r"<!--(.*?)-->", src, re.DOTALL):
        start = src.count("\n", 0, m.start()) + 1
        for i, line in enumerate(m.group(1).split("\n")):
            out.append((start + i, line))
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
    out.extend(_html_comment_lines(src))
    return out


def _line_has_violation(line: str) -> bool:
    masked = ALLOWLIST.sub(lambda m: " " * len(m.group(0)), line)
    return bool(PATTERN.search(masked))


def _scan_file(path: Path, rel: str) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        pairs = _py_lines_to_check(text, str(path))
    elif path.suffix == ".html":
        pairs = _html_lines_to_check(text)
    elif path.suffix == ".js":
        pairs = _js_comments(text, 1)
    else:
        pairs = enumerate(text.split("\n"), 1)
    return [(lineno, line) for lineno, line in pairs if _line_has_violation(line)]


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
    targets.extend((REPO / "web").rglob("*.js"))
    return targets


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _current_hits() -> dict[str, list[tuple[int, str]]]:
    hits_by_file: dict[str, list[tuple[int, str]]] = {}
    for f in _gather_targets():
        rel = str(f.relative_to(REPO))
        hits = _scan_file(f, rel)
        if hits:
            hits_by_file[rel] = hits
    return hits_by_file


def _load_baseline() -> dict[str, set[str]]:
    if not BASELINE_PATH.exists():
        return {}
    raw = json.loads(BASELINE_PATH.read_text())
    return {rel: {_normalize(t) for t in lines} for rel, lines in raw.items()}


@pytest.mark.unit
def test_no_change_narration_beyond_baseline():
    baseline = _load_baseline()
    hits_by_file = _current_hits()

    violations = []
    for rel, hits in hits_by_file.items():
        allowed = baseline.get(rel, set())
        offending = [
            f"{rel}:{lineno}: {line.strip()[:160]}"
            for lineno, line in hits
            if _normalize(line) not in allowed
        ]
        if offending:
            sample = offending[:10]
            more = f"\n  ... and {len(offending) - 10} more" if len(offending) > 10 else ""
            violations.append(rel + ":\n  " + "\n  ".join(sample) + more)

    assert not violations, (
        "Change-narration found beyond the checked-in baseline "
        "(tests/narration_baseline.json). Rewrite these to describe current "
        "behavior only (see docs/AGENTS.md 'Current behavior only'), or if "
        "a hit is a genuine false positive, extend the ALLOWLIST in this "
        "test:\n\n" + "\n\n".join(violations)
    )
