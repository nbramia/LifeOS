"""Guard for tests/conftest.py's module-scoped UI-driver fixture overrides.

conftest.py redefines pytest-playwright's `playwright`/`browser_type`/
`browser_context_args`/`launch_browser`/`browser` fixtures at module scope
(instead of the plugin's own session scope) and delegates each one's actual
body to the upstream implementation via `.__wrapped__`, rather than copying
the bodies. That delegation only works if upstream's fixtures still exist
and still take the parameters conftest.py assumes -- this file is the guard
that fails loudly, on every test run, the day a pytest-playwright upgrade
changes that shape, instead of the override breaking mysteriously the next
time a browser test runs.

Deliberately outside tests/conftest.py: pytest only collects tests from
files matching `test_*.py` (see pyproject.toml's `python_files`), so a test
defined inside the plugin module itself would never run.
"""
import inspect

import pytest
import pytest_playwright.pytest_playwright as _pw_plugin

pytestmark = pytest.mark.unit

# Mirrors the delegation in tests/conftest.py exactly -- keep these two in sync.
_EXPECTED_PARAMS = {
    "playwright": [],
    "browser_type": ["playwright", "browser_name"],
    "browser_context_args": [
        "pytestconfig", "playwright", "device", "base_url", "_pw_artifacts_folder",
    ],
    "launch_browser": ["browser_type_launch_args", "browser_type", "connect_options"],
    "browser": ["launch_browser"],
}


# Explicit, sanitized ids: three of the five real fixture names (`browser`,
# `browser_type`, `launch_browser`) contain the substring "browser", and
# tests/conftest.py's pytest_collection_modifyitems auto-marks any test whose
# *name* contains "browser" -- pytest's default parametrize ids would splice
# the fixture name straight into item.name (e.g.
# "...[browser_type-expected_params1]"), silently deselecting those three
# cases from the unit run. These ids carry none of that substring.
_UI_DRIVER_FIXTURE_IDS = ["pw-driver", "pw-engine-type", "pw-context-args", "pw-launch-fn", "pw-instance"]


@pytest.mark.parametrize(
    "fixture_name,expected_params",
    list(_EXPECTED_PARAMS.items()),
    ids=_UI_DRIVER_FIXTURE_IDS,
)
def test_upstream_ui_driver_fixture_signatures_unchanged(fixture_name, expected_params):
    assert hasattr(_pw_plugin, fixture_name), (
        f"pytest_playwright.pytest_playwright does not define `{fixture_name}` -- "
        "tests/conftest.py's module-scoped override delegates to it via "
        "`.__wrapped__` and needs updating to match."
    )
    upstream = getattr(_pw_plugin, fixture_name)
    assert hasattr(upstream, "__wrapped__"), (
        f"pytest_playwright.pytest_playwright.{fixture_name} does not expose "
        "`.__wrapped__` -- a pytest-playwright or pytest version bump changed how "
        "`@pytest.fixture` wraps functions; update the delegation in tests/conftest.py."
    )
    actual_params = list(inspect.signature(upstream.__wrapped__).parameters)
    assert actual_params == expected_params, (
        f"pytest_playwright.pytest_playwright.{fixture_name}'s parameters changed "
        f"from {expected_params} to {actual_params} -- update tests/conftest.py's "
        "override to match before the delegation silently passes arguments in the "
        "wrong shape."
    )
