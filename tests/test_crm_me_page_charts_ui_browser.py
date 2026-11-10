"""Server-free browser tests for the CRM "Me" dashboard charts.

Covers the front-end fixes made to /me (GitHub issues #4-#8):

  #5  date-only strings were parsed as UTC, so labels/tooltips read a day early
      (and "this month" was computed in UTC) west of UTC;
  #4  the Relationship Health / Parent charts were drawn at a different scale
      from their rendered size, differed in height, and overlapped the chips;
  #6  Messaging-by-Circle legend used the wrong circle names and the Inner
      toggle did nothing on real data;
  #7  the partial current month skewed the messaging insights;
  #8  Network Growth skipped months, had an odd y-axis max, and a short panel.

Like the other Me/Graph browser tests this serves `web/` from an ephemeral
port and intercepts every /api call with page.route, so no server or CRM
data is involved. It carries no `requires_server` marker and therefore runs
at pre-push (`browser and not requires_server`). Keep it that way.

Determinism: every context uses timezone America/Chicago (UTC-6, where the
original bugs reproduce) and a fixed clock of 2026-11-09 12:00 local, so the
current month (2026-11) is partial.
"""
import http.server
import json
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ME_ID = "person-me"
TZ = "America/Chicago"
TIMEZONES = ["America/Chicago", "Asia/Tokyo", "Pacific/Auckland"]  # west, east, far east of UTC


def _now(tz):
    """2026-11-09 12:00 LOCAL in `tz` (an aware datetime, so the instant is
    right in every zone, not just the one the host happens to use)."""
    return datetime(2026, 11, 9, 12, 0, 0, tzinfo=ZoneInfo(tz))


WIDE = {"width": 1900, "height": 1000}
NARROW = {"width": 1300, "height": 1000}


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


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

ME_PERSON = {
    "id": ME_ID, "canonical_name": "Me Myself", "category": "self",
    "dunbar_circle": 0, "relationship_strength": 0, "company": "", "tags": [],
    "emails": [], "phone_numbers": [], "vault_contexts": [], "sources": [],
    "source_entities": [], "source_entity_count": 0, "relationships": [],
}


def _daily_history(start, end, base=55):
    """One health point per day, start..end inclusive ('YYYY-MM-DD')."""
    out, d, i = [], start, 0
    while d <= end:
        out.append({"date": d.isoformat(), "score": base + (i % 17) - 8, "count": 3 + i % 5})
        d += timedelta(days=1)
        i += 1
    return out


HEALTH_HISTORY = _daily_history(date(2026, 5, 10), date(2026, 11, 9))


def _months(first, last):
    """['YYYY-MM', ...] inclusive."""
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _circle_split(total, shares):
    """{'1': n1, '2': n2, ...} for a monthly total split by `shares`."""
    return {c: round(total * s) for c, s in shares.items()}


DEFAULT_SHARES = {"1": 0.55, "2": 0.35, "3": 0.10}


def _messaging_months(shares=None, oct_total=4500, nov_total=314):
    """May-Oct 2026 around 3000 messages (Oct deliberately higher so the
    "last complete month vs average" insight is deterministic), plus a
    partial 2026-11."""
    shares = shares or DEFAULT_SHARES
    rows = []
    for month in _months("2026-05", "2026-10"):
        total = oct_total if month == "2026-10" else 3000
        by_circle = _circle_split(total, shares)
        rows.append({
            "month": month, "total": sum(by_circle.values()), "by_circle": by_circle,
            "unique_by_circle": {c: max(1, n // 100) for c, n in by_circle.items()},
        })
    by_circle = _circle_split(nov_total, shares)
    rows.append({
        "month": "2026-11", "total": sum(by_circle.values()), "by_circle": by_circle,
        "unique_by_circle": {c: max(1, n // 100) for c, n in by_circle.items()},
    })
    return rows


def _growth_months(max_value=5):
    """2025-11, 2025-12, 2026-02..2026-10: 2026-01 and 2026-11 are missing
    (the API omits months with zero new people)."""
    values = [1, 2, 3, 4, max_value, 3, 2, 1, max_value - 1, 2, 3]
    months = ["2025-11", "2025-12"] + _months("2026-02", "2026-10")
    return [{"month": m, "new_people": v} for m, v in zip(months, values)]


def _me_interactions_payload(**overrides):
    """A minimal but valid `GET /api/crm/me/interactions` body."""
    payload = {
        "daily": [],
        "by_source": {},
        "top_contacts": [
            {"person_id": f"p{i}", "person_name": f"Contact {i}", "count": 40 - i}
            for i in range(10)
        ],
        "neglected_contacts": [],
        "warming": [],
        "cooling": [],
        "relationship_health_score": HEALTH_HISTORY[-1]["score"],
        "health_score_average": 12,
        "health_score_history": HEALTH_HISTORY,
        "tracked_relationships": [{
            "name": "Parent Relationship",
            "healthy_direction": "more",
            "current_score": 62,
            "average": 9,
            "person_ids": ["mom", "dad"],
            "person_names": ["Mom Example", "Dad Example"],
            "history": HEALTH_HISTORY,
        }],
        "network_growth": _growth_months(),
        "messaging_by_circle": _messaging_months(),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Page loading helpers
# ---------------------------------------------------------------------------

def _route_handler(payload, fail=()):
    def handler(route):
        url = route.request.url
        if any(f in url for f in fail):
            route.fulfill(status=500, content_type="application/json", body="{}")
            return
        path = url.split("/api/crm", 1)[1].split("?", 1)[0] if "/api/crm" in url else ""
        if path == "/config":
            body = {"my_person_id": ME_ID}
        elif path == "/birthdays/today":
            body = {"birthdays": [], "count": 0}
        elif path == "/people":
            body = {"people": [], "total": 0, "offset": 0, "count": 0}
        elif path == f"/people/{ME_ID}":
            body = ME_PERSON
        elif path == "/statistics":
            body = {"total_people": 0}
        elif path == "/me/interactions/span":
            body = {"years": 1}
        elif path == "/me/stats":
            body = {"total_emails": 0, "total_meetings": 0, "total_messages": 0, "total_people": 0}
        elif path == "/me/interactions":
            body = payload
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


@pytest.fixture
def load_me(browser: Browser, crm_base_url):
    """Factory: open /me with the given /me/interactions payload, a frozen
    clock and the Chicago timezone. Console errors / uncaught page errors are
    collected and asserted empty when the test finishes (the regression guard
    in the spec: every test below doubles as a "no JS errors" check)."""
    contexts, problems, allowed = [], [], []

    def _load(payload=None, viewport=WIDE, tz=TZ, fail=()) -> Page:
        """`fail`: URL substrings whose /api requests answer HTTP 500."""
        context = browser.new_context(viewport=viewport, timezone_id=tz)
        contexts.append(context)
        page = context.new_page()
        page.clock.set_fixed_time(_now(tz))  # before goto, so every script sees it
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.route("**/api/**", _route_handler(
            payload if payload is not None else _me_interactions_payload(), fail))
        page.goto(f"{crm_base_url}/me")
        # The dashboard is drawn once the last widget (messaging) has rendered.
        page.wait_for_selector("#messagingByCircleChart .messaging-chart")
        page.wait_for_selector("#healthChartContainer svg.health-chart-svg")
        return page

    # A test that deliberately fails a request lists the console errors that
    # failure is expected to produce: load_me.allow("Failed to load")
    _load.allow = allowed.extend
    yield _load
    for context in contexts:
        context.close()
    problems = [p for p in problems if not any(a in p for a in allowed)]
    assert not problems, "JS errors while rendering /me:\n" + "\n".join(problems)


def _svg_scale(page, container_id):
    """(sx, sy, rendered_w, rendered_h, vb_w, vb_h) of a chart SVG."""
    return page.evaluate("""
        (id) => {
            const svg = document.querySelector('#' + id + ' svg.health-chart-svg');
            const r = svg.getBoundingClientRect();
            const vb = svg.viewBox.baseVal;
            return [r.width / vb.width, r.height / vb.height, r.width, r.height, vb.width, vb.height];
        }
    """, container_id)


def _x_labels(page, container_id):
    # SVG <text> has no innerText, so read textContent
    texts = page.locator(f"#{container_id} svg text").evaluate_all("els => els.map(e => e.textContent)")
    return [t.strip() for t in texts if re.match(r"^[A-Z][a-z]{2} \d{1,2}$", t.strip())]


def _legend(page):
    items = page.locator(".messaging-legend .legend-item")
    return [t.strip() for t in items.locator("span").all_inner_texts()]


def _chip(page, label):
    return page.locator(".messaging-legend .legend-item", has_text=label)


def _inner_segments(page):
    return page.locator(".messaging-stacked-bars .messaging-segment-1").count()


# ---------------------------------------------------------------------------
# 1. Dates (issue #5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tz", TIMEZONES)
def test_health_and_tracked_axis_dates_are_local_not_utc(load_me, tz):
    """#5: 'YYYY-MM-DD' strings must not be parsed as UTC. In America/Chicago
    the old code rendered "May 9" / "Nov 8" on the x axes."""
    page = load_me(tz=tz)
    # History is daily 2026-05-10 .. 2026-11-09 (184 points); the middle
    # label is index 92 = 2026-08-10.
    assert _x_labels(page, "healthChartContainer") == ["May 10", "Aug 10", "Nov 9"]
    assert _x_labels(page, "parentChartContainer") == ["May 10", "Aug 10", "Nov 9"]


@pytest.mark.parametrize("tz", TIMEZONES)
def test_health_tooltip_date_is_local_not_utc(load_me, tz):
    """#5: hovering the last health dot shows "Nov 9, 2026" (was Nov 8)."""
    page = load_me(tz=tz)
    page.locator("#healthChartContainer .health-chart-dot").last.dispatch_event("mouseenter")
    expect(page.locator("#healthChartContainer .health-chart-tooltip-date")).to_have_text("Nov 9, 2026")
    page.locator("#parentChartContainer .tracked-chart-dot").last.dispatch_event("mouseenter")
    expect(page.locator("#parentChartContainer .health-chart-tooltip-date")).to_have_text("Nov 9, 2026")


@pytest.mark.parametrize("tz", TIMEZONES)
def test_parse_local_date_and_month_key_under_frozen_clock(load_me, tz):
    """#5: the helper functions themselves, west (Chicago) and east
    (Tokyo, Auckland) of UTC, with the clock at 2026-11-09 12:00 local."""
    page = load_me(tz=tz)
    assert page.evaluate("parseLocalDate('2026-11-09').getDate()") == 9
    assert page.evaluate("parseLocalDate('2026-11-09').getMonth()") == 10
    assert page.evaluate("parseLocalDate('2026-11').getDate()") == 1
    assert page.evaluate("localMonthKey()") == "2026-11"
    # A late-evening local time is already the next UTC day; the month key
    # must still come from the LOCAL month.
    assert page.evaluate("localMonthKey(new Date(2026, 10, 30, 23, 30))") == "2026-11"


# ---------------------------------------------------------------------------
# 2. Chart sizing (issue #4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("viewport", [WIDE, NARROW], ids=["1900", "1300"])
def test_line_charts_draw_at_1_to_1_scale_and_match_in_height(load_me, viewport):
    """#4: the viewBox matches the rendered size (no stretched/shrunk text),
    both charts are the same height, and the Parent chips sit below the
    Parent chart instead of overlapping it."""
    page = load_me(viewport=viewport)
    for container_id in ("healthChartContainer", "parentChartContainer"):
        sx, sy, *_ = _svg_scale(page, container_id)
        assert sx == pytest.approx(1.0, abs=0.02), f"{container_id} x scale {sx}"
        assert sy == pytest.approx(1.0, abs=0.02), f"{container_id} y scale {sy}"

    health_h = _svg_scale(page, "healthChartContainer")[3]
    parent_h = _svg_scale(page, "parentChartContainer")[3]
    assert abs(health_h - parent_h) <= 1, f"heights differ: {health_h} vs {parent_h}"

    svg_bottom = page.locator("#parentChartContainer svg.health-chart-svg").bounding_box()
    svg_bottom = svg_bottom["y"] + svg_bottom["height"]
    chips_top = page.locator("#parentPeopleList").bounding_box()["y"]
    assert chips_top >= svg_bottom - 0.5, f"chips top {chips_top} overlaps chart bottom {svg_bottom}"
    expect(page.locator("#parentPeopleList .tracked-person-chip")).to_have_count(2)


def test_line_charts_redraw_to_new_width_on_resize(load_me):
    """#4: narrowing the window redraws the SVGs with a viewBox that matches
    the new container width, still at ~1:1."""
    page = load_me(viewport=WIDE)
    before = {cid: _svg_scale(page, cid) for cid in ("healthChartContainer", "parentChartContainer")}

    page.set_viewport_size(NARROW)
    for cid in before:
        # resize handler is debounced (150ms); wait for the real condition
        page.wait_for_function(
            """([id, oldW]) => {
                const vb = document.querySelector('#' + id + ' svg.health-chart-svg').viewBox.baseVal;
                return Math.abs(vb.width - oldW) > 1;
            }""",
            arg=[cid, before[cid][4]],
        )
        sx, sy, rendered_w, _, vb_w, _ = _svg_scale(page, cid)
        assert vb_w < before[cid][4]
        assert sx == pytest.approx(1.0, abs=0.02)
        assert sy == pytest.approx(1.0, abs=0.02)
        container_w = page.locator(f"#{cid}").evaluate(
            "el => { const cs = getComputedStyle(el); return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight); }"
        )
        assert vb_w == pytest.approx(container_w, abs=1)


# ---------------------------------------------------------------------------
# 3. Circles (issue #6)
# ---------------------------------------------------------------------------

def _chip_states(page):
    """{label: (has 'selected' class, aria-pressed == 'true', is legend-empty)}"""
    return page.locator(".messaging-legend .legend-item").evaluate_all("""
        els => Object.fromEntries(els.map(e => [
            e.querySelector('span').textContent.trim(),
            [e.classList.contains('selected'), e.getAttribute('aria-pressed') === 'true',
             e.classList.contains('legend-empty')]]))
    """)


def _assert_aria_matches_selected(page):
    """aria-pressed must always mirror the visual `selected` class."""
    for label, (selected, pressed, _) in _chip_states(page).items():
        assert selected == pressed, f"{label}: selected={selected} aria-pressed={pressed}"


def _segment_counts(page):
    """{"0".."6": number of stacked-bar segments} (JSON keys are strings)."""
    return page.evaluate("""
        () => Object.fromEntries([0,1,2,3,4,5,6].map(c =>
            [c, document.querySelectorAll('.messaging-stacked-bars .messaging-segment-' + c).length]))
    """)


# What the real backend sends: only keys '1'..'5' ('5' is the catch-all
# `min(circle, 5)`); '0' only with manual overrides; '6' never.
REAL_SHARES = {"1": 0.40, "2": 0.25, "3": 0.15, "4": 0.12, "5": 0.08}


def test_legend_names_and_order_for_real_backend_shape(load_me):
    """#6: with the exact real-backend shape (keys 1-5 only) the legend reads
    Inner..Acquaint then "Outer" (circle 5, the catch-all); there is no Core
    and no Beyond chip, and every chip has data."""
    rows = _messaging_months(REAL_SHARES)
    page = load_me(_me_interactions_payload(messaging_by_circle=rows))
    assert _legend(page) == ["Inner", "Close", "Friend", "Acquaint", "Outer"]
    assert all(not empty for *_, empty in _chip_states(page).values())
    assert _chip(page, "Core").count() == 0 and _chip(page, "Beyond").count() == 0
    assert "Familiar" not in page.locator(".messaging-legend").inner_text()
    # every circle is selected initially, so all five segments are drawn
    assert _segment_counts(page) == {"0": 0, "1": 7, "2": 7, "3": 7, "4": 7, "5": 7, "6": 0}


def test_legend_names_and_order_when_some_circles_are_empty(load_me):
    """#6: chips for circles 1-5 are always present, in the same order, even
    when the data only touches circles 1-3."""
    page = load_me()
    assert _legend(page) == ["Inner", "Close", "Friend", "Acquaint", "Outer"]


def test_legend_chip_titles(load_me):
    """#6: each chip explains which people it covers."""
    rows = _messaging_months(REAL_SHARES)
    page = load_me(_me_interactions_payload(messaging_by_circle=rows))
    titles = {
        "Inner": "Dunbar circle 1: top 5 people",
        "Close": "Dunbar circle 2: next 15",
        "Friend": "Dunbar circle 3: next 50",
        "Acquaint": "Dunbar circle 4: next 150",
        "Outer": "Dunbar circle 5 and beyond: everyone else",
    }
    for label, title in titles.items():
        expect(_chip(page, label)).to_have_attribute("title", title)


def test_legend_defensive_core_and_beyond_chips(load_me):
    """#6: a manual-override key '0' adds "Core" first; a (never sent, but
    tolerated) key '6' adds "Beyond" last. Both carry their own titles."""
    rows = _messaging_months({"0": 0.1, "1": 0.4, "2": 0.2, "3": 0.2, "6": 0.1})
    page = load_me(_me_interactions_payload(messaging_by_circle=rows))
    assert _legend(page) == ["Core", "Inner", "Close", "Friend", "Acquaint", "Outer", "Beyond"]
    expect(_chip(page, "Core")).to_have_attribute("title", "Core: manual override circle")
    expect(_chip(page, "Beyond")).to_have_attribute("title", "Beyond circle 5")
    # Acquaint/Outer have no data in this fixture
    expect(_chip(page, "Acquaint")).to_have_class(re.compile(r"legend-empty"))
    expect(_chip(page, "Outer")).to_have_class(re.compile(r"legend-empty"))
    _assert_aria_matches_selected(page)


def test_inner_chip_selected_by_default_and_toggle_removes_and_restores_segments(load_me):
    """#6: on real data the Inner chip starts selected (not dimmed), and
    clicking it removes the red segments; clicking again restores them."""
    page = load_me()
    inner = _chip(page, "Inner")
    expect(inner).to_have_class(re.compile(r"\bselected\b"))
    expect(inner).not_to_have_class(re.compile(r"legend-empty"))
    expect(inner).to_have_attribute("aria-pressed", "true")
    expect(inner).to_have_attribute("role", "button")
    expect(inner).to_have_attribute("tabindex", "0")

    n = _inner_segments(page)
    assert n == 7  # May..Nov, one Inner segment per bar

    inner.click()
    assert _inner_segments(page) == 0
    expect(_chip(page, "Inner")).not_to_have_class(re.compile(r"\bselected\b"))
    expect(_chip(page, "Inner")).to_have_attribute("aria-pressed", "false")

    _chip(page, "Inner").click()
    assert _inner_segments(page) == n
    expect(_chip(page, "Inner")).to_have_class(re.compile(r"\bselected\b"))
    expect(_chip(page, "Inner")).to_have_attribute("aria-pressed", "true")


def test_circle_without_data_is_disabled_unselected_and_inert(load_me):
    """#6: circles 4 and 5 have no people in any month, so their chips are
    `legend-empty`, never selected, aria-disabled, titled "No people in this
    circle yet", and clicking them changes nothing."""
    page = load_me()
    before_segments = _segment_counts(page)
    before_states = _chip_states(page)
    for label in ("Acquaint", "Outer"):
        chip = _chip(page, label)
        expect(chip).to_have_class(re.compile(r"legend-empty"))
        expect(chip).not_to_have_class(re.compile(r"\bselected\b"))
        expect(chip).to_have_attribute("aria-disabled", "true")
        expect(chip).to_have_attribute("aria-pressed", "false")
        expect(chip).to_have_attribute("title", "No people in this circle yet")
        assert chip.evaluate("el => el.getAttribute('onclick') === null && el.getAttribute('onkeydown') === null")
        chip.click(force=True)  # aria-disabled is seen as "not enabled" by Playwright
        chip.focus()
        page.keyboard.press("Enter")
        page.keyboard.press("Space")
    assert _segment_counts(page) == before_segments
    assert _chip_states(page) == before_states
    for label in ("Inner", "Close", "Friend"):
        chip = _chip(page, label)
        expect(chip).not_to_have_class(re.compile(r"legend-empty"))
        assert chip.get_attribute("aria-disabled") is None
    _assert_aria_matches_selected(page)


def test_enabled_chip_toggles_with_enter_and_space(load_me):
    """#6: chips are keyboard operable: Enter and Space each toggle."""
    page = load_me()
    chip = _chip(page, "Close")
    expect(chip).to_have_attribute("aria-pressed", "true")

    chip.focus()
    page.keyboard.press("Enter")
    expect(_chip(page, "Close")).to_have_attribute("aria-pressed", "false")
    assert _segment_counts(page)["2"] == 0

    _chip(page, "Close").focus()  # the chart was re-rendered, so focus again
    page.keyboard.press("Space")
    expect(_chip(page, "Close")).to_have_attribute("aria-pressed", "true")
    assert _segment_counts(page)["2"] == 7

    _chip(page, "Close").focus()
    page.keyboard.press("Enter")
    expect(_chip(page, "Close")).to_have_attribute("aria-pressed", "false")
    _assert_aria_matches_selected(page)


def test_messaging_panel_says_text_plus_whatsapp_and_never_imessage(load_me):
    """#6: the panel subtitle and legend no longer mention iMessage."""
    page = load_me()
    expect(page.locator(".me-messaging-panel .chart-subtitle")).to_have_text("Text + WhatsApp")
    assert "imessage" not in page.locator(".me-messaging-panel").inner_text().lower()
    assert all("imessage" not in t.lower() for t in _legend(page))


# ---------------------------------------------------------------------------
# 4. Insights + partial month (issue #7)
# ---------------------------------------------------------------------------

def _insights(page):
    return [t.strip() for t in page.locator(".messaging-insight").all_inner_texts()]


@pytest.mark.parametrize("tz", TIMEZONES)
def test_partial_current_month_is_marked_and_tooltip_says_so_far(load_me, tz):
    """#7: 2026-11 (only 314 messages, today is Nov 9) is drawn as a partial
    bar and its tooltip is labelled "(so far)" - in zones west and east of
    UTC alike."""
    page = load_me(tz=tz)
    bars = page.locator(".messaging-bar-container")
    assert bars.count() == 7
    expect(bars.last.locator(".messaging-stacked-bar")).to_have_class(re.compile(r"messaging-bar-partial"))
    expect(bars.first.locator(".messaging-stacked-bar")).not_to_have_class(re.compile(r"messaging-bar-partial"))

    bars.last.dispatch_event("mouseenter")
    expect(page.locator("#messagingTooltip")).to_contain_text("Nov 2026 (so far)")
    bars.nth(5).dispatch_event("mouseenter")
    expect(page.locator("#messagingTooltip")).to_contain_text("Oct 2026")
    expect(page.locator("#messagingTooltip")).not_to_contain_text("so far")


def test_insights_describe_last_complete_month_not_the_partial_one(load_me):
    """#7: with Nov partial (314), insights compare Oct (4500) against the
    average of complete months (3250): "38% more", never "fewer messages"."""
    page = load_me()
    insights = _insights(page)
    assert insights, "expected at least one insight"
    assert any(t.startswith("In Oct, you sent") for t in insights), insights
    assert any("38% more messages than your monthly average" in t for t in insights), insights
    assert not any("fewer messages than your monthly average" in t for t in insights), insights
    assert not any("In Nov" in t for t in insights), insights


def test_insights_survive_toggling_circles_and_last_circle_cannot_be_removed(load_me):
    """#7/#6: data only in circles 1-3. Turning Inner then Close off leaves
    Friend: insights still render (about Oct, never the partial Nov), its
    segments remain, and clicking Friend (the last selected circle) is
    refused. Empty chips never change anything. The fixture asserts no JS
    errors at teardown."""
    page = load_me()
    assert _segment_counts(page) == {"0": 0, "1": 7, "2": 7, "3": 7, "4": 0, "5": 0, "6": 0}
    assert len(_insights(page)) > 0

    _chip(page, "Inner").click()
    _chip(page, "Close").click()
    states = _chip_states(page)
    assert states["Inner"][0] is False and states["Close"][0] is False and states["Friend"][0] is True
    assert _segment_counts(page)["1"] == 0 and _segment_counts(page)["2"] == 0
    assert _segment_counts(page)["3"] == 7
    insights = _insights(page)
    assert insights, "insights should still render with only Friend selected"
    assert not any("In Nov" in t for t in insights)
    assert not any("fewer messages than your monthly average" in t for t in insights)
    _assert_aria_matches_selected(page)

    # The last selected, non-empty circle cannot be switched off
    _chip(page, "Friend").click()
    expect(_chip(page, "Friend")).to_have_attribute("aria-pressed", "true")
    assert _segment_counts(page)["3"] == 7

    # Empty chips are inert
    snapshot = (_chip_states(page), _segment_counts(page))
    _chip(page, "Acquaint").click(force=True)
    _chip(page, "Outer").click(force=True)
    assert (_chip_states(page), _segment_counts(page)) == snapshot
    _assert_aria_matches_selected(page)

    # Other display modes keep working on the filtered data
    page.locator(".messaging-mode-toggle .mode-toggle-btn", has_text="Unique").click()
    page.locator(".messaging-mode-toggle .mode-toggle-btn", has_text="%").click()
    assert page.locator(".messaging-bar-container").count() == 7
    assert not any("In Nov" in t for t in _insights(page))


def test_no_insights_when_only_one_complete_month_precedes_the_partial_one(load_me):
    """#7: two bars (2026-10 complete, 2026-11 partial) are not enough to say
    anything honest, so no insight is rendered at all (and nothing throws)."""
    rows = [
        {"month": "2026-10", "total": 3000, "by_circle": {"1": 1650, "2": 1050, "3": 300},
         "unique_by_circle": {"1": 16, "2": 10, "3": 3}},
        {"month": "2026-11", "total": 200, "by_circle": {"1": 110, "2": 70, "3": 20},
         "unique_by_circle": {"1": 4, "2": 3, "3": 1}},
    ]
    page = load_me(_me_interactions_payload(messaging_by_circle=rows))
    assert page.locator(".messaging-bar-container").count() == 2
    assert page.locator(".messaging-insight").count() == 0


# ---------------------------------------------------------------------------
# 5. Network growth (issue #8)
# ---------------------------------------------------------------------------

def test_growth_axis_has_no_skipped_months(load_me):
    """#8: the API omits 2026-01 and 2026-11 (zero new people); the axis must
    still show 12 consecutive months ending at the current month (11/26)."""
    page = load_me()
    labels = [t.strip() for t in page.locator("#networkGrowthChart .growth-label").all_inner_texts()]
    assert labels == ["12/25", "01/26", "02/26", "03/26", "04/26", "05/26",
                      "06/26", "07/26", "08/26", "09/26", "10/26", "11/26"]
    assert "01/26" in labels and "11/26" in labels


def test_growth_y_axis_maximum_is_even(load_me):
    """#8: max 5 -> axis 6/3/0 (integer midpoint); max 4 -> 4/2/0."""
    page = load_me()
    y = [t.strip() for t in page.locator("#networkGrowthChart .growth-y-label").all_inner_texts()]
    assert y == ["6", "3", "0"]

    page = load_me(_me_interactions_payload(network_growth=_growth_months(max_value=4)))
    y = [t.strip() for t in page.locator("#networkGrowthChart .growth-y-label").all_inner_texts()]
    assert y == ["4", "2", "0"]


def test_growth_zero_month_still_gets_a_minimum_height_bar(load_me):
    """#8: the filled-in 01/26 month (0 new people) renders a visible
    minimum-height bar rather than no bar."""
    page = load_me()
    bars = page.locator("#networkGrowthChart .growth-bar")
    assert bars.count() == 12
    zero_bar = bars.nth(1)  # 01/26
    assert zero_bar.evaluate("el => el.style.height") == "2%"
    assert zero_bar.bounding_box()["height"] > 0
    # 12/25 has real data, so its bar is taller than the zero-month bar
    assert bars.nth(0).bounding_box()["height"] > zero_bar.bounding_box()["height"]


def test_growth_chart_fills_its_panel_and_row_heights_match(load_me):
    """#8: at 1900x1000 the month labels sit at the bottom of the Network
    Growth panel (no big empty band) and the panel is as tall as its
    sibling Top Contacts panel."""
    page = load_me(viewport=WIDE)
    panel = page.locator(".me-chart-panel").bounding_box()
    labels = page.locator("#networkGrowthChart .growth-labels").bounding_box()
    gap = (panel["y"] + panel["height"]) - (labels["y"] + labels["height"])
    assert 0 <= gap <= 24, f"growth labels end {gap}px above panel bottom"

    compact = page.locator(".me-compact-panel").bounding_box()
    assert panel["height"] == pytest.approx(compact["height"], abs=1)


# ---------------------------------------------------------------------------
# 6. Regression guard
# ---------------------------------------------------------------------------

def test_no_console_or_page_errors_across_interactions(load_me):
    """Exercise every chart (hover, period switch, resize, toggles) and rely
    on the load_me fixture to fail the test if anything logged a console
    error or threw an uncaught exception."""
    page = load_me()
    page.locator("#healthChartContainer .health-chart-dot").first.dispatch_event("mouseenter")
    page.locator("#networkGrowthChart .growth-bar-container").last.dispatch_event("mouseenter")
    page.locator(".messaging-bar-container").first.dispatch_event("mouseenter")
    page.locator("#networkGrowthChart .growth-label").first.click()  # re-centre click
    page.locator(".health-period-pill[data-period=month]").first.click()
    page.wait_for_selector("#healthChartContainer svg.health-chart-svg")
    page.set_viewport_size(NARROW)
    page.wait_for_function(
        "document.querySelector('#healthChartContainer svg.health-chart-svg').viewBox.baseVal.width < 800"
    )
    _chip(page, "Close").click()
    _chip(page, "Close").click()


@pytest.mark.parametrize("failing, container_id, month_pill, other_id", [
    ("parent", "parentChartContainer",
     "#parentPeriodSelector .health-period-pill[data-period=month]", "healthChartContainer"),
    ("health", "healthChartContainer",
     ".me-health-panel .health-period-pill[data-period=month]", "parentChartContainer"),
])
def test_failed_period_reload_is_not_redrawn_with_stale_data_on_resize(
        load_me, failing, container_id, month_pill, other_id):
    """Switching a chart to "Month" while that request fails shows "Failed to
    load"; a later window resize must not redraw the OLD data over it. The
    other (healthy) chart is used to prove the resize handler really ran."""
    load_me.allow(["Failed to load", "status of 500"])  # the expected failure
    page = load_me(fail=["health_period=month"])
    old_other_w = _svg_scale(page, other_id)[4]

    page.locator(month_pill).first.click()
    container = page.locator(f"#{container_id}")
    expect(container).to_contain_text("Failed to load")
    assert container.locator("svg").count() == 0

    page.set_viewport_size(NARROW)
    # resize handler is debounced; wait until the healthy chart was redrawn
    page.wait_for_function(
        """([id, oldW]) => Math.abs(document.querySelector('#' + id + ' svg.health-chart-svg')
                .viewBox.baseVal.width - oldW) > 1""",
        arg=[other_id, old_other_w],
    )
    expect(container).to_contain_text("Failed to load")
    assert container.locator("svg").count() == 0
