"""Server-free browser tests for the CRM sidebar people-list Categories filter.

GitHub issue #10: the sidebar Categories filter used to start with Work
unchecked and a hard-coded "3 Selected" button, hiding every work contact
until the user opened the dropdown. All four categories (Self, Family,
Personal, Work) are now selected by default and the button reads
"Categories". The Graph tab has its own, separate category filter whose
default is intentionally unchanged.

Like the other CRM browser tests this serves `web/` from an ephemeral port
and intercepts every /api call with page.route, so no server or CRM data is
involved. It carries no `requires_server` marker and therefore runs at
pre-push (`browser and not requires_server`). Keep it that way.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ME_ID = "person-me"
TZ = "America/Chicago"
VIEWPORT = {"width": 1900, "height": 1000}

WORK_NAME = "Wanda Work"
OTHER_NAMES = ["Me Myself", "Fiona Family", "Percy Personal"]
ALL_NAMES = OTHER_NAMES + [WORK_NAME]


def _person(pid, name, category, circle, strength):
    return {
        "id": pid, "canonical_name": name, "category": category,
        "dunbar_circle": circle, "relationship_strength": strength,
        "company": "", "tags": [], "emails": [], "phone_numbers": [],
        "vault_contexts": [], "sources": [], "source_entities": [],
        "source_entity_count": 0, "relationships": [],
    }


PEOPLE = [
    _person(ME_ID, "Me Myself", "self", 0, 0),
    _person("p-family", "Fiona Family", "family", 1, 90),
    _person("p-personal", "Percy Personal", "personal", 2, 70),
    _person("p-work", WORK_NAME, "work", 3, 50),
]


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the CRM SPA the way api/main.py does: /, /me and /crm* return
    crm.html; everything else falls through to web/ as static files."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path == "/" or path == "/me" or path.startswith(("/me/", "/crm")):
            return str(WEB_DIR / "crm.html")
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def crm_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CrmHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _route_handler(route):
    url = route.request.url
    path = url.split("/api/crm", 1)[1].split("?", 1)[0] if "/api/crm" in url else ""
    if path == "/config":
        body = {"my_person_id": ME_ID}
    elif path == "/birthdays/today":
        body = {"birthdays": [], "count": 0}
    elif path == "/people":
        body = {"people": PEOPLE, "total": len(PEOPLE), "offset": 0, "count": len(PEOPLE)}
    elif path == f"/people/{ME_ID}":
        body = PEOPLE[0]
    elif path == "/statistics":
        body = {"total_people": len(PEOPLE)}
    elif path == "/me/interactions/span":
        body = {"years": 1}
    elif path == "/me/stats":
        body = {"total_emails": 0, "total_meetings": 0, "total_messages": 0, "total_people": 0}
    else:
        body = {}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def crm_page(browser: Browser, crm_base_url):
    """The CRM opened with one person per category. Console errors and
    uncaught page errors are collected and asserted empty at teardown."""
    context = browser.new_context(viewport=VIEWPORT, timezone_id=TZ)
    page = context.new_page()
    problems = []
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.route("**/api/**", _route_handler)
    page.goto(f"{crm_base_url}/me")
    page.wait_for_selector("#peopleList .person-card")
    yield page
    context.close()
    assert not problems, "JS errors:\n" + "\n".join(problems)


def _names(page: Page):
    return page.locator("#peopleList .person-name").all_inner_texts()


def _checked(page: Page):
    return {
        cb.get_attribute("value"): cb.is_checked()
        for cb in page.locator("#peopleCategoryOptions input[type=checkbox]").all()
    }


def test_all_four_categories_checked_by_default(crm_page: Page):
    """Issue #10: Self/Family/Personal/Work are all checked on load, the
    button reads "Categories", and the work contact is listed."""
    assert _checked(crm_page) == {"self": True, "family": True, "personal": True, "work": True}
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("Categories")
    names = _names(crm_page)
    assert WORK_NAME in names
    assert sorted(names) == sorted(ALL_NAMES)


def test_unchecking_work_hides_work_people_and_rechecking_restores(crm_page: Page):
    """Issue #10: Work is an ordinary toggle -- unchecking it gives
    "3 Selected" and hides the work person; checking it restores both."""
    crm_page.click("#peopleCategoryDropdown .people-filter-btn")
    work = crm_page.locator('#peopleCategoryOptions input[value="work"]')

    work.click()
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("3 Selected")
    names = _names(crm_page)
    assert WORK_NAME not in names
    assert sorted(names) == sorted(OTHER_NAMES)

    work.click()
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("Categories")
    assert sorted(_names(crm_page)) == sorted(ALL_NAMES)


def test_all_and_none_buttons_still_work(crm_page: Page):
    """Issue #10: None clears everything (label "None", empty list); All
    selects every category again (label "Categories", everyone listed)."""
    crm_page.click("#peopleCategoryDropdown .people-filter-btn")
    options = crm_page.locator("#peopleCategoryOptions")

    options.get_by_role("button", name="None", exact=True).click()
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("None")
    assert _names(crm_page) == []
    assert not any(_checked(crm_page).values())

    options.get_by_role("button", name="All", exact=True).click()
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("Categories")
    assert sorted(_names(crm_page)) == sorted(ALL_NAMES)
    assert all(_checked(crm_page).values())


def test_graph_tab_category_filter_default_unchanged(crm_page: Page):
    """Issue #10 scope guard: the Graph tab's own category filter still
    starts with Work unchecked and reads "3 selected"."""
    graph = {
        cb.get_attribute("value"): cb.is_checked()
        for cb in crm_page.locator("#categoryFilterOptions input[type=checkbox]").all()
    }
    assert graph == {"self": True, "family": True, "work": False, "personal": True}
    expect(crm_page.locator("#categoryFilterLabel")).to_have_text("3 selected")


def test_no_console_or_page_errors_while_toggling(crm_page: Page):
    """Issue #10: loading and exercising the filter logs no JS errors (the
    crm_page fixture asserts the error list is empty at teardown)."""
    crm_page.click("#peopleCategoryDropdown .people-filter-btn")
    for value in ("work", "work", "family", "family"):
        crm_page.locator(f'#peopleCategoryOptions input[value="{value}"]').click()
    options = crm_page.locator("#peopleCategoryOptions")
    options.get_by_role("button", name="None", exact=True).click()
    options.get_by_role("button", name="All", exact=True).click()
    expect(crm_page.locator("#peopleCategoryLabel")).to_have_text("Categories")
