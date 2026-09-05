"""Guard for tests/conftest.py's module-scoped UI-driver fixture overrides.

conftest.py redefines pytest-playwright's `playwright`/`browser_type`/
`browser_context_args`/`launch_browser`/`browser` fixtures at module scope
(instead of the plugin's own session scope) and delegates each one's actual
body to the upstream implementation via `.__wrapped__`, rather than copying
the bodies. Two things have to hold for that to keep working, and this file
guards both:

- The override actually exists and is scope="module" -- if the five
  overrides in tests/conftest.py were ever removed or their scope regressed,
  the fixtures would silently fall back to pytest-playwright's own
  session-scoped versions, resurrecting the failure the module scoping
  exists to prevent. `test_local_ui_driver_fixtures_are_module_scoped`
  guards this.
- The delegation matches upstream's shape -- if a pytest-playwright upgrade
  changes a fixture's parameters or switches it between a plain return and a
  generator (`yield`), the override would pass arguments in the wrong shape
  or hand callers a generator object instead of the real value.
  `test_upstream_ui_driver_fixture_signatures_unchanged` guards this, and
  fails loudly on every test run rather than the override breaking
  mysteriously the next time a browser test runs.

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

# Whether each upstream fixture is a generator function (`yield`, delegated
# with `yield from`) or a plain function (`return`, delegated with `return`).
# tests/conftest.py's override must use the matching delegation shape for
# each fixture -- keep this in sync with both _EXPECTED_PARAMS above and the
# override bodies in tests/conftest.py.
_EXPECTED_IS_GENERATOR = {
    "playwright": True,
    "browser_type": False,
    "browser_context_args": False,
    "launch_browser": False,
    "browser": True,
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
    expected_is_generator = _EXPECTED_IS_GENERATOR[fixture_name]
    actual_is_generator = inspect.isgeneratorfunction(upstream.__wrapped__)
    assert actual_is_generator == expected_is_generator, (
        f"pytest_playwright.pytest_playwright.{fixture_name} changed from "
        f"{'a generator (`yield`)' if expected_is_generator else 'a plain `return`'} "
        f"function to {'a generator (`yield`)' if actual_is_generator else 'a plain `return`'} "
        "-- tests/conftest.py's override must switch between `yield from` and "
        "`return` to match, or callers get a generator object instead of the "
        "real value."
    )


@pytest.mark.parametrize(
    "fixture_name",
    list(_EXPECTED_PARAMS),
    ids=_UI_DRIVER_FIXTURE_IDS,
)
def test_local_ui_driver_fixtures_are_module_scoped(request, fixture_name):
    defs = request._fixturemanager.getfixturedefs(fixture_name, request.node)
    assert defs, f"no fixture named `{fixture_name}` is visible from this test"
    assert defs[-1].scope == "module", (
        f"the nearest `{fixture_name}` fixture definition has scope "
        f"{defs[-1].scope!r}, not 'module' -- tests/conftest.py's override that "
        "re-scopes it from session to module (so pytest-playwright's driver "
        "loop is torn down at the end of each test file; see the comment above "
        "the overrides) appears to be missing or has regressed."
    )
    expected_is_generator = _EXPECTED_IS_GENERATOR[fixture_name]
    actual_is_generator = inspect.isgeneratorfunction(defs[-1].func)
    assert actual_is_generator == expected_is_generator, (
        f"tests/conftest.py's `{fixture_name}` override is "
        f"{'a generator' if actual_is_generator else 'a plain function'} but upstream's "
        f"fixture is {'a generator' if expected_is_generator else 'a plain function'} -- "
        "the override must delegate with `yield from` for a generator upstream and "
        "`return` otherwise, or callers get a generator object instead of the real value."
    )
