"""Regression tests for #598: api/main.py's load_dotenv() must not search
upward past this checkout's own root.

## The bug

`api/main.py` used to call the bare, argument-less `load_dotenv()`. With no
explicit path, python-dotenv (`usecwd=False`, the default) walks upward
from `api/main.py`'s own directory -- not the process cwd; see
`dotenv.main.find_dotenv`, which inspects the call stack to find the
calling file -- looking for the first `.env` it finds, all the way to the
filesystem root if necessary. From a normal checkout that finds the
checkout's own `.env` one level up, which looks correct -- but from a git
worktree (which has no `.env` of its own; it's gitignored and not copied by
`git worktree add`), the search keeps climbing and can find a *different*,
real, machine-specific `.env` belonging to the checkout the worktree was
created from.

That mutates `os.environ` for the rest of the process. Every module-level
constant computed from `config.settings.settings` at import time then bakes
in whatever that real config happened to contain -- for whichever test
process reaches that import first. See the PR for #598 for the full audit:
at minimum `agent_system_prompt._STATIC_PROMPT`, `synthesizer.SYSTEM_CONTEXT`,
`agent_tools._user`, `crm.WORK_EMAIL_DOMAIN`/`MY_PERSON_ID`, and
`slack_integration.SLACK_TEAM_ID` (already worked around locally in
test_slack_sync.py -- see the comment there) are all affected by the same
mechanism.

## The fix

Anchor the path explicitly: `load_dotenv(Path(__file__).resolve().parent.parent / ".env")`,
matching the pattern every other entry point in this repo already uses
(`scripts/sync_slack.py`, `scripts/run_all_syncs.py`, etc). For the real
checkout this resolves to the exact same file the old upward search found
first, so server behavior is unchanged. For a worktree (or anything else
nested with no `.env` of its own) it loads nothing rather than escaping
upward -- there is no longer an "upward" to search.

## What these tests prove

`test_bare_load_dotenv_leaks_from_a_worktree_parent` reproduces the original
bug directly, in isolation from the rest of the app, so it's clear what was
actually wrong. `test_pinned_load_dotenv_does_not_leak` proves the fix
removes exactly that behavior. `test_main_py_does_not_use_bare_load_dotenv`
is a static guard against silently reverting the fix.
`test_settings_user_name_is_deterministic_regardless_of_import_order` then
demonstrates the downstream effect is fixed too: a config-derived value
comes out the same regardless of what else has already imported `api.main`
in this process -- which is what makes it safe under `pytest -n N
--dist loadscope`, where that ordering is an accident of work distribution,
not something a test can control.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LEAKED_SENTINEL = "LEAKED-FROM-PARENT-ENV"


def _build_nested_checkout(tmp_path: Path) -> Path:
    """Build `tmp_path/parent/.env` (a stand-in for a real, machine-specific
    config) plus `tmp_path/parent/worktrees/nested_checkout/api/`, a stand-in
    for a worktree that has no `.env` of its own. Returns the `api/` dir,
    where the probe scripts below get written (mirroring `api/main.py`'s
    own depth: one level under the checkout root).
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / ".env").write_text(f"LIFEOS_USER_NAME={_LEAKED_SENTINEL}\n")

    api_dir = parent / "worktrees" / "nested_checkout" / "api"
    api_dir.mkdir(parents=True)
    return api_dir


def _run_probe(probe_path: Path) -> str:
    """Run a probe script in a fresh interpreter with a clean-of-LIFEOS_ env,
    and return whatever it printed for LIFEOS_USER_NAME (empty string if
    unset). A subprocess is essential here, not just a fresh import in this
    process: python-dotenv mutates real process-global `os.environ`, and we
    must not let either probe's outcome leak into the pytest process running
    the rest of the suite.
    """
    env = os.environ.copy()
    env.pop("LIFEOS_USER_NAME", None)
    result = subprocess.run(
        [sys.executable, str(probe_path)],
        cwd=str(probe_path.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}"
    return result.stdout.strip()


def test_bare_load_dotenv_leaks_from_a_worktree_parent(tmp_path):
    """Reproduce the original defect: the exact idiom api/main.py used to
    use (bare `load_dotenv()`), run from a file nested under a checkout with
    no `.env` of its own, picks up an ancestor directory's `.env`.
    """
    api_dir = _build_nested_checkout(tmp_path)
    probe = api_dir / "probe_bare.py"
    probe.write_text(
        "from dotenv import load_dotenv\n"
        "load_dotenv()\n"
        "import os\n"
        "print(os.environ.get('LIFEOS_USER_NAME', ''))\n"
    )

    assert _run_probe(probe) == _LEAKED_SENTINEL


def test_pinned_load_dotenv_does_not_leak(tmp_path):
    """The fix: anchoring load_dotenv() to this file's own repo root (one
    level up, matching api/main.py's actual layout) finds nothing in the
    nested checkout and does not escape upward to the parent's `.env`.
    """
    api_dir = _build_nested_checkout(tmp_path)
    probe = api_dir / "probe_fixed.py"
    probe.write_text(
        "from pathlib import Path\n"
        "from dotenv import load_dotenv\n"
        "load_dotenv(Path(__file__).resolve().parent.parent / '.env')\n"
        "import os\n"
        "print(os.environ.get('LIFEOS_USER_NAME', ''))\n"
    )

    assert _run_probe(probe) == ""


def test_main_py_does_not_use_bare_load_dotenv():
    """Static guard: fail loudly if api/main.py ever reverts to the bare,
    upward-searching form instead of the pinned repo-root path.
    """
    main_py = Path(__file__).resolve().parent.parent / "api" / "main.py"
    code_lines = [
        line for line in main_py.read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    assert not re.search(r"load_dotenv\(\s*\)", "\n".join(code_lines)), (
        "api/main.py calls load_dotenv() with no explicit path again -- "
        "this reopens #598 (upward search can escape a worktree into a "
        "parent checkout's real .env). Pass an explicit repo-root path."
    )

def test_settings_user_name_is_deterministic_regardless_of_import_order():
    """The downstream effect of the fix: `settings.user_name` (read into a
    module-level constant at import time by several modules -- see this
    file's module docstring) comes from *this checkout's own* config, whether
    or not `api.main` has already been imported by something else in this
    worker process. Before the fix, whichever test triggered that import
    first decided the answer for the rest of the process; there was no way
    for a single test to assert this, because the outcome depended on
    collection order under xdist.

    The expected value is derived from whether this checkout has its own
    `.env`, rather than assumed absent (#623). Asserting the bare field
    default only holds in a worktree; in the canonical checkout a real `.env`
    sits at the repo root and *is* the correct thing to have loaded. Pinning
    the default there made a correct environment look broken and blocked
    `git push` outright, since the pre-push hook runs the suite in the
    pushing repository.

    Be clear about how much this test guards, because it is less than it
    looks. It is a demonstration of the downstream effect, not the primary
    regression catcher:

    - In the canonical checkout a bare `load_dotenv()` finds this same
      repo-root file anyway, so the regression is invisible *by value* here.
    - Even in a worktree it only catches the regression when `config.settings`
      is imported *after* `api.main` in the same process. `settings` is a
      module-level singleton built at its own import time, so when something
      (conftest, another test) has already imported it, a later leak into
      `os.environ` no longer changes `settings.user_name`. Verified by
      reverting `api/main.py` to a bare `load_dotenv()`: this test still
      passed, and only the static guard below failed.

    So the real coverage lives elsewhere in this file, and both of those are
    location-independent: `test_bare_load_dotenv_leaks_from_a_worktree_parent`
    / `test_pinned_load_dotenv_does_not_leak` prove the mechanism in a
    subprocess against a synthetic nested checkout, and
    `test_main_py_does_not_use_bare_load_dotenv` guards the call form
    statically. Keep this test for the downstream illustration it provides --
    just don't rely on it as the guard.
    """
    from dotenv import dotenv_values

    import api.main  # noqa: F401 -- exercise the (now-pinned) load_dotenv path
    from config.settings import settings

    field = type(settings).model_fields["user_name"]
    own_dotenv = Path(__file__).resolve().parent.parent / ".env"

    # dotenv_values parses without touching os.environ, so reading a real
    # machine-specific file here can't itself leak config into this process
    # (same approach as tests/test_fixtures_no_personal_data.py).
    expected = field.default
    if own_dotenv.is_file():
        expected = dotenv_values(own_dotenv).get(field.alias) or field.default

    if settings.user_name != expected:
        # Deliberately not echoing either value: on a real machine both may
        # be personal, and this message can reach CI logs.
        pytest.fail(
            "settings.user_name did not come from this checkout's own config. "
            "Either api/main.py's load_dotenv regressed to an upward search "
            "(escaping a worktree into an ancestor checkout's .env, reopening "
            "#598), or some other .env is being loaded. Values withheld -- "
            "they may be personal."
        )
