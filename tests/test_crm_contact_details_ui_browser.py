"""Browser tests for labeled contact info and social links on the person page."""
import copy
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic person -- never real CRM data.
PERSON = {
    "id": "person-1",
    "canonical_name": "Test Person",
    "emails": ["test@example.com"],
    "phone_numbers": ["+15550100001"],
    "labeled_emails": [{"id": "email-1", "value": "test@example.com", "label": "work"}],
    "labeled_phones": [{"id": "phone-1", "value": "+15550100001", "label": None}],
    "social_links": [
        {"id": "link-1", "platform": "instagram", "platform_label": "Instagram", "icon": "📷",
         "value": "test_main", "url": "https://www.instagram.com/test_main", "handle": "test_main",
         "label": "<b>finsta</b>", "source": "manual"},
        {"id": "link-2", "platform": "discord", "platform_label": "Discord", "icon": "🎮",
         "value": "test_user", "url": None, "handle": "test_user", "label": None, "source": "manual"},
    ],
    "category": "personal",
}
PLATFORMS = [
    {"key": "instagram", "label": "Instagram", "icon": "📷", "placeholder": "@username"},
    {"key": "x", "label": "X (Twitter)", "icon": "𝕏", "placeholder": "@username"},
]


class _Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / "crm.html")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def crm_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/crm"
    server.shutdown()


def _open_contact_card(page: Page, crm_url: str):
    """Serve canned CRM data and render the card; returns the recorded writes."""
    writes = []
    state = copy.deepcopy(PERSON)

    def handle(route):
        request = route.request
        if request.url.endswith("/social-platforms"):
            return route.fulfill(json=PLATFORMS)
        if request.method == "GET":
            return route.fulfill(json={"people": [], "count": 0, "total": 0})
        body = json.loads(request.post_data) if request.post_data else None
        writes.append((request.method, request.url.split("/api/crm")[1], body))
        if body and body.get("value") == "@taken":
            return route.fulfill(status=409, json={"detail": "That account belongs to another contact"})
        if request.method == "POST" and "social-links" in request.url:
            state["social_links"].append({
                "id": "link-3", "platform": body["platform"], "platform_label": "X (Twitter)", "icon": "𝕏",
                "value": body["value"], "url": "https://x.com/test_new", "handle": "test_new",
                "label": body["label"], "source": "manual",
            })
        elif request.method == "PATCH" and "emails" in request.url:
            state["labeled_emails"][0].update(value=body["value"], label=body["label"])
        elif request.method == "DELETE":
            state["social_links"] = [item for item in state["social_links"] if not request.url.endswith(item["id"])]
        return route.fulfill(json={k: state[k] for k in ("labeled_emails", "labeled_phones", "social_links")})

    page.route("**/api/**", handle)
    page.goto(crm_url)
    page.evaluate(
        """person => {
            selectedPersonId = person.id;
            const card = document.getElementById('contactInfoCompact');
            for (let el = card; el; el = el.parentElement) {
                if (getComputedStyle(el).display === 'none') el.style.display = 'block';
            }
            renderCompactContactInfo(person);
        }""",
        state,
    )
    return page.locator("#contactInfoCompact"), writes


def test_labels_and_links_render_safely(page: Page, crm_url: str):
    card, _ = _open_contact_card(page, crm_url)

    expect(card.locator(".contact-label-chip").first).to_have_text("work")
    expect(card.locator(".contact-label-chip").nth(1)).to_have_text("<b>finsta</b>")
    assert card.locator(".contact-label-chip b").count() == 0
    expect(card.get_by_role("link", name="test_main")).to_have_attribute(
        "href", "https://www.instagram.com/test_main"
    )
    expect(card.locator(".contact-value-text")).to_have_text("test_user")


def test_add_social_link(page: Page, crm_url: str):
    card, writes = _open_contact_card(page, crm_url)

    card.get_by_role("button", name="+ Social").click()
    card.get_by_label("Platform").select_option("x")
    card.get_by_label("Social value").fill("@test_new")
    card.get_by_label("Label").fill("main")
    card.get_by_role("button", name="Save").click()

    expect(card.get_by_role("link", name="test_new")).to_be_visible()
    assert writes == [("POST", "/people/person-1/social-links",
                       {"value": "@test_new", "label": "main", "platform": "x"})]


def test_edit_email_prefills_and_saves(page: Page, crm_url: str):
    card, writes = _open_contact_card(page, crm_url)

    card.get_by_role("button", name="Edit Email").click()
    expect(card.get_by_label("Email value")).to_have_value("test@example.com")
    expect(card.get_by_label("Label")).to_have_value("work")
    card.get_by_label("Label").fill("school")
    card.get_by_role("button", name="Save").click()

    expect(card.locator(".contact-label-chip").first).to_have_text("school")
    assert writes[0][:2] == ("PATCH", "/people/person-1/emails/email-1")


def test_remove_link(page: Page, crm_url: str):
    card, writes = _open_contact_card(page, crm_url)
    page.on("dialog", lambda dialog: dialog.accept())

    card.get_by_role("button", name="Remove Discord").click()

    expect(card.locator(".contact-value-text")).to_have_count(0)
    assert writes == [("DELETE", "/people/person-1/social-links/link-2", None)]


def test_server_error_stays_in_the_open_form(page: Page, crm_url: str):
    card, _ = _open_contact_card(page, crm_url)

    card.get_by_role("button", name="+ Social").click()
    card.get_by_label("Social value").fill("@taken")
    card.get_by_role("button", name="Save").click()

    expect(card.locator(".contact-editor-error")).to_have_text("That account belongs to another contact")
    expect(card.get_by_label("Social value")).to_have_value("@taken")
