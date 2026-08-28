"""Guard tracked code against maintainer-specific values (#789).

This project is meant to be installable by anyone, but nothing previously
stopped maintainer-specific values from landing in the tracked codebase
itself. Three instances were found by a person reading the code by hand:
a hardcoded directory path rooted in the maintainer's own home folder
(#767), a specific computer model named in operator-facing alert text
(#770), and a data-store write restriction wired unconditionally to the
maintainer's own personal capture pipeline (#769). This module is the
standing guard against a fourth: a fast scan of tracked source for two of
those value classes, each with a maintained allowlist/denylist rather
than a fuzzy heuristic (see the third class -- #769's "hardcoded value
tied to one person's pipeline" -- is not something a text pattern can
express; it's out of scope for this scan, same as #789's own Out of Scope
section says).

Two scans:

1. `find_home_path_violations` -- an absolute path rooted under a real
   home directory (`/home/<user>/...`, `/Users/<user>/...`). A handful of
   docstrings in this repo already use paths shaped exactly like that as
   illustrative examples (e.g. "e.g. /Users/x/Notes/Work/note.md") --
   `_HOME_PATH_PLACEHOLDER_SEGMENTS` excludes the common placeholder
   tokens those examples use (a single letter, "user", literal "...") so
   the scan doesn't flag its own project's documentation. A *real*
   maintainer username is never one of these.

2. `find_hardware_name_violations` -- a specific device/computer model
   name (seeded with "Mac Mini", #770) in tracked source, allowlisted at
   the *file* level rather than by line number: line numbers drift as
   unrelated code above them changes (this repo's own issue tracker
   already needed a citation correction for exactly that reason), so a
   file-level allowlist is the more stable "known instance" record. A new
   mention in a file not already on the allowlist still fails.

Both scans exclude `tests/` (which is where this file itself and its
allowlists live -- the "check's own allowlist file" exclusion the issue
asks for) and `docs/`. Neither scan requires network access, a real
credential, a GPU, a running server, or writes to a real database --
`git ls-files` plus reading tracked files off disk is the entire
mechanism.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

_EXCLUDED_DIR_PREFIXES = ("tests/", "docs/")

# Hardware/device model names that must never appear in tracked source as
# operator-facing text (an alert or a message shown to a person). Extend as
# new device-specific wording is found -- a maintained denylist, not a
# heuristic classifier.
_HARDWARE_MODEL_DENYLIST = ("Mac Mini",)

# File-level allowlist for _HARDWARE_MODEL_DENYLIST matches. Repo-relative
# path strings (as `git ls-files` reports them).
_HARDWARE_NAME_ALLOWLIST = {
    # #770 landed -- it genericized the operator-facing alert text
    # ("Check Mac Mini cron and rsync pipeline.", etc.) and the specific
    # comments its own acceptance criteria called out. These two files
    # still contain OTHER "Mac Mini" mentions #770 didn't touch (docstrings
    # like "Import phone call history from the Mac Mini's JSON export",
    # an argparse --help description, an unrelated SHA-drift-warning
    # comment) -- same not-operator-facing rationale as the four entries
    # below. Verified by removing these two entries and re-running the
    # scan: it still fails on both files for exactly these residual lines.
    "scripts/apple_data_import.py",
    "scripts/run_all_syncs.py",
    # Not operator-facing (no alert or message a person sees at runtime) --
    # engineering comments/docstrings describing this maintainer's own
    # configured Apple Data Agent hardware for a future reader of the code.
    # #770 does not cover these. Recorded here as a known instance of the
    # same value class (not tracked by any issue yet) so a *new*
    # operator-facing mention elsewhere still fails the scan.
    "api/services/job_queue.py",
    "api/services/whatsapp.py",
    "scripts/apple_data_agent.sh",
    "scripts/apple_data_export.py",
    # Found only once the scan below was made case-insensitive (these two
    # use Apple's own "Mac mini" styling, not this codebase's usual "Mac
    # Mini") -- same not-operator-facing rationale as the four above.
    "config/settings.py",
    "scripts/push_birthdays_to_contacts.py",
}

# Case-insensitive: Apple's own styling ("Mac mini") and this codebase's
# ("Mac Mini") shouldn't be two different denylist entries to keep in sync
# -- Codex review of this file flagged the case-sensitive version as
# trivially bypassed by the other styling.
_HARDWARE_MODEL_DENYLIST_LOWER = tuple(n.lower() for n in _HARDWARE_MODEL_DENYLIST)

# #767 (investments SYNC_DIR hardcoded to the maintainer's home directory)
# and #769 (vault.py's unconditional Journal-write reservation) are the
# other two instances #789 was filed to allowlist. #767 is already fixed
# on this integration branch (api/routes/investments.py now reads
# settings.investments_sync_dir) -- no live path violation remains, so it
# needs no entry above. #769 isn't a path or a hardware name -- it's the
# third value class (a hardcoded value tied to one person's individual
# pipeline), which isn't expressible as a text pattern for either scan
# below and is out of scope for this check (see module docstring and
# #789's own Out of Scope section); noted here for the record rather than
# as a functioning allowlist entry.

# Path-segment placeholders that are unambiguously never a real username --
# a single character, or a literal "...". Deliberately does NOT include
# words like "user"/"username"/"you": those are common *real* account
# names too (e.g. a single-user Linux/container install), and excluding
# them outright would let a genuine hardcoded "/home/user/..." default
# through undetected (Codex review of this file flagged exactly this).
_HOME_PATH_PLACEHOLDER_SEGMENTS = {"..."}

# File-level allowlist, same rationale as _HARDWARE_NAME_ALLOWLIST: a
# comment demonstrating a path-transform generically, not a real default.
_HOME_PATH_ALLOWLIST = {
    # web/agents.html:987 -- "// /home/user/Code/LifeOS -> /Code/LifeOS",
    # illustrating a generic prefix-stripping transform. "user" isn't
    # excluded as a placeholder segment (see above), so this needs an
    # explicit entry instead.
    "web/agents.html",
}

# The trailing `(?=...)` requires the username segment to be followed by a
# path separator, a closing quote, whitespace, or end of line/string --
# not just a literal `/` -- so a bare "/home/nathanramia" at the end of a
# quoted string (no further path components) is still caught. Codex review
# flagged the original (mandatory trailing `/`) as missing exactly that case.
_HOME_PATH_RE = re.compile(r"(?:/home/|/Users/)([A-Za-z0-9_.-]+)(?=/|['\"\s]|$)")


def _is_excluded(rel_path: str) -> bool:
    return rel_path.startswith(_EXCLUDED_DIR_PREFIXES)


def _tracked_files() -> list[Path]:
    """Every git-tracked file, excluding tests/, docs/, and this file
    itself (redundant with the tests/ exclusion today, kept explicit per
    #789's own wording in case the allowlist ever moves out of tests/)."""
    result = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, check=True, capture_output=True, text=True,
    )
    paths = []
    for rel in result.stdout.splitlines():
        if not rel or _is_excluded(rel):
            continue
        full = _REPO_ROOT / rel
        if full == _THIS_FILE or not full.is_file():
            continue
        paths.append(full)
    return paths


def find_hardware_name_violations(files: list[Path], *, root: Path = _REPO_ROOT) -> list[str]:
    """Pure scan function over an explicit file list (absolute paths under
    `root`) so the fixture-proof tests below can exercise it against a
    synthetic file without needing a real git-tracked instance."""
    violations = []
    for path in files:
        rel_path = str(path.relative_to(root))
        if rel_path in _HARDWARE_NAME_ALLOWLIST:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            line_lower = line.lower()
            for name, name_lower in zip(_HARDWARE_MODEL_DENYLIST, _HARDWARE_MODEL_DENYLIST_LOWER):
                if name_lower in line_lower:
                    violations.append(f"{rel_path}:{lineno}: hardcoded hardware name {name!r}")
    return violations


def find_home_path_violations(files: list[Path], *, root: Path = _REPO_ROOT) -> list[str]:
    """Pure scan function, same shape as `find_hardware_name_violations`."""
    violations = []
    for path in files:
        rel_path = str(path.relative_to(root))
        if rel_path in _HOME_PATH_ALLOWLIST:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _HOME_PATH_RE.finditer(line):
                segment = match.group(1)
                if len(segment) <= 1 or segment.lower() in _HOME_PATH_PLACEHOLDER_SEGMENTS:
                    continue
                violations.append(
                    f"{rel_path}:{lineno}: absolute home-directory path (user segment {segment!r})"
                )
    return violations


def test_no_hardcoded_hardware_model_names_outside_allowlist():
    violations = find_hardware_name_violations(_tracked_files())
    assert not violations, "Hardcoded hardware model name(s) found:\n" + "\n".join(violations)


def test_no_hardcoded_home_directory_paths():
    violations = find_home_path_violations(_tracked_files())
    assert not violations, "Hardcoded home-directory path(s) found:\n" + "\n".join(violations)


class TestScanFunctionsActuallyDetectViolations:
    """#789's own Verification requirement: a fixture case proving each
    scan actually fails when it should, not just trivially passing because
    nothing in the current tree ever exercises its failure path."""

    def test_hardware_name_scan_fails_on_a_synthetic_bad_file(self, tmp_path):
        bad = tmp_path / "bad_alert.py"
        bad.write_text('ALERT = "Check the Mac Mini agent."\n')

        violations = find_hardware_name_violations([bad], root=tmp_path)

        assert len(violations) == 1
        assert violations[0].startswith("bad_alert.py:1:")

    def test_hardware_name_scan_respects_the_allowlist(self, tmp_path):
        """Same content, but under an allowlisted repo-relative path --
        must not fire."""
        allowlisted_dir = tmp_path / "scripts"
        allowlisted_dir.mkdir()
        allowlisted = allowlisted_dir / "apple_data_import.py"
        allowlisted.write_text('ALERT = "Check the Mac Mini agent."\n')

        violations = find_hardware_name_violations([allowlisted], root=tmp_path)

        assert violations == []

    def test_hardware_name_scan_is_case_insensitive(self, tmp_path):
        """Apple's own styling ("Mac mini") must be caught, not just this
        codebase's ("Mac Mini") -- a case-sensitive check would be
        trivially bypassed by the other styling (Codex review finding)."""
        bad = tmp_path / "bad_alert_lowercase.py"
        bad.write_text('ALERT = "Check the Mac mini agent."\n')

        violations = find_hardware_name_violations([bad], root=tmp_path)

        assert len(violations) == 1

    def test_home_path_scan_fails_on_a_synthetic_bad_file(self, tmp_path):
        bad = tmp_path / "bad_config.py"
        bad.write_text('SYNC_DIR = "/home/realuser/Code/Sync/investments"\n')

        violations = find_home_path_violations([bad], root=tmp_path)

        assert len(violations) == 1
        assert violations[0].startswith("bad_config.py:1:")

    def test_home_path_scan_catches_a_bare_home_directory_with_no_trailing_slash(self, tmp_path):
        """A hardcoded path ending exactly at the username (no further path
        components after it) must still be caught -- Codex review flagged
        the original regex's mandatory trailing `/` as missing this case."""
        bad = tmp_path / "bad_config.py"
        bad.write_text('HOME_DIR = "/home/realuser"\n')

        violations = find_home_path_violations([bad], root=tmp_path)

        assert len(violations) == 1

    def test_home_path_scan_does_not_exempt_a_real_looking_user_account(self, tmp_path):
        """"user" is a common *real* Linux/container account name, not
        only a documentation placeholder -- it must NOT be blanket-exempt
        (Codex review flagged the original placeholder set, which included
        "user", as capable of hiding a genuine violation)."""
        bad = tmp_path / "bad_config.py"
        bad.write_text('SYNC_DIR = "/home/user/Code/Sync/investments"\n')

        violations = find_home_path_violations([bad], root=tmp_path)

        assert len(violations) == 1

    def test_home_path_scan_ignores_known_placeholder_examples(self, tmp_path):
        """Only the unambiguous placeholders (a single letter, literal
        "...") are exempt -- see _HOME_PATH_PLACEHOLDER_SEGMENTS."""
        ok = tmp_path / "docstring_example.py"
        ok.write_text(
            '"""e.g. /home/n/Code/LifeOS or /Users/x/Notes/Work/note.md or /home/.../Code"""\n'
        )

        violations = find_home_path_violations([ok], root=tmp_path)

        assert violations == []

    def test_home_path_scan_respects_the_allowlist(self, tmp_path):
        allowlisted = tmp_path / "web"
        allowlisted.mkdir()
        allowlisted_file = allowlisted / "agents.html"
        allowlisted_file.write_text("// /home/user/Code/LifeOS -> /Code/LifeOS\n")

        violations = find_home_path_violations([allowlisted_file], root=tmp_path)

        assert violations == []


class TestTrackedFilesHelper:
    """#5 from Codex review: prove `_tracked_files()` itself returns a sane
    real-tree set, not just that the pure scan functions behave correctly
    on synthetic data fed to them directly."""

    def test_includes_representative_real_source_files(self):
        rel_paths = {str(p.relative_to(_REPO_ROOT)) for p in _tracked_files()}
        assert "api/main.py" in rel_paths
        assert "config/settings.py" in rel_paths

    def test_excludes_tests_and_docs_directories(self):
        rel_paths = {str(p.relative_to(_REPO_ROOT)) for p in _tracked_files()}
        assert not any(p.startswith("tests/") for p in rel_paths)
        assert not any(p.startswith("docs/") for p in rel_paths)
        assert not any(p == "tests" for p in rel_paths)
