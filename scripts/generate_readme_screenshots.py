#!/usr/bin/env python3
"""Generate the LifeOS README screenshots against an isolated test instance.

Seeds an owned, isolated candidate instance (see `scripts/test_instance.py`)
with obviously synthetic data — tasks, board cards, a schedule, and chat
conversations — then drives the real web UI with Playwright to capture PNGs
into docs/images/. Never touches the live instance, the production vault, or
any real credential; the instance is sanitized and torn down automatically
when this script exits.

Run (from the repo root, in a worktree — never the production checkout):

    ./scripts/server.sh test-instance run --source . -- \\
        ~/.venvs/lifeos/bin/python scripts/generate_readme_screenshots.py \\
        --out-dir "$(pwd)/docs/images"

This script is itself the "command" `test-instance run` executes inside the
candidate's sanitized environment (same PYTHONPATH/cwd as the candidate
server), so it can import application modules directly and reach the
instance's own databases and vault. `--out-dir` must be an absolute path to
the real checkout's docs/images — `__file__` inside the candidate resolves
to an ephemeral snapshot copy of this script that is deleted when the
instance tears down, so the output directory can't be derived from it.
"""
from __future__ import annotations

import argparse
import os
import pwd
import socket
import sqlite3
import sys
import time
from pathlib import Path

import requests

# The candidate instance's HOME is a synthetic per-instance directory (see
# scripts/test_instance.py), so Playwright's default browser cache lookup
# under ~/.cache/ms-playwright finds nothing there. Point it at the
# operator's real, read-only browser cache — the same narrow exception
# test_instance.py's own `_scoped_hf_cache_dir` makes for model weights —
# before the driver subprocess launches. No personal data lives there, only
# binaries. The real home comes from the passwd database (unaffected by the
# sanitized HOME override), never a hardcoded path.
_real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
_real_browsers_path = _real_home / ".cache" / "ms-playwright"
if _real_browsers_path.is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_real_browsers_path)

from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

_parser = argparse.ArgumentParser()
_parser.add_argument(
    "--out-dir", required=True, type=Path,
    help="Absolute path to the real checkout's docs/images (see module docstring).",
)
IMAGES_DIR = _parser.parse_args().out_dir.resolve()

BASE_URL = os.environ["LIFEOS_TEST_BASE_URL"]
VAULT_PATH = Path(os.environ["LIFEOS_VAULT_PATH"])

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}


def seed_vault_notes() -> None:
    """A handful of obviously synthetic notes — gives the vault something to
    point at, even though the seeded conversations below carry their own
    authored content and don't depend on live retrieval."""
    notes_dir = VAULT_PATH / "Notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "Q3 Offsite.md").write_text(
        "# Q3 Offsite\n\n"
        "Dana Whitfield is coordinating the Q3 offsite for Acme Corp's "
        "product team. Venue is booked for the third week of September; "
        "catering and travel are still open questions.\n"
    )
    (notes_dir / "Acme Corp Onboarding.md").write_text(
        "# Acme Corp Onboarding\n\n"
        "New hires get badge + laptop pickup on their first Monday, "
        "followed by a welcome sync with the product team at 11am.\n"
    )
    (notes_dir / "Standing Desk Research.md").write_text(
        "# Standing Desk Research\n\n"
        "Comparing a couple of standing desk options for the home office: "
        "a crank-adjustable frame vs. a memory-preset electric one.\n"
    )


def api_post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{BASE_URL}{path}", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def seed_tasks() -> dict:
    """Task cards covering every board lane per the README manifest."""
    ids: dict[str, str] = {}

    # Unassigned — open task, no assignee.
    t = api_post("/api/tasks", {
        "description": "Research standing desk options for the home office",
        "context": "Home",
        "tags": ["research"],
        "notes": "Compare a crank-adjustable frame vs. a memory-preset electric one.",
    })
    ids["unassigned"] = t["id"]

    # Assigned — claimed by an engine tag, not yet running.
    t = api_post("/api/tasks", {
        "description": "Draft the Q3 offsite agenda for the product team",
        "context": "Work",
        "tags": ["codex"],
        "due_date": "2026-09-25",
        "notes": "Dana Whitfield is coordinating; venue is booked, catering and travel are open.",
    })
    ids["assigned"] = t["id"]

    # In progress.
    t = api_post("/api/tasks", {
        "description": "Fix the flaky import on the finance sync",
        "context": "Work",
        "status": "in_progress",
        "tags": ["claude", "agent-running"],
        "notes": "Intermittent timeout on the monthly transaction import job.",
    })
    ids["in_progress"] = t["id"]

    # Review — agent-completed, outcome recorded separately below.
    t = api_post("/api/tasks", {
        "description": "Add a weekly digest of upcoming birthdays to the CRM",
        "context": "Work",
        "status": "done",
        "tags": ["agent-completed"],
    })
    ids["review"] = t["id"]

    # Done.
    t = api_post("/api/tasks", {
        "description": "Reply to the plumber about Thursday",
        "context": "Home",
        "status": "done",
    })
    ids["done"] = t["id"]

    # Snoozed — unassigned base, future snoozed_until field.
    t = api_post("/api/tasks", {
        "description": "Evaluate the new expense-report template",
        "context": "Work",
        "tags": ["deferred-review"],
        "fields": {"snoozed_until": "2099-01-01T00:00:00+00:00"},
    })
    ids["snoozed"] = t["id"]

    # Human queue — an agent's question for the operator.
    hq = api_post("/api/tasks/human-queue", {
        "title": "Confirm the Q3 offsite budget ceiling",
        "notes": (
            "Agent question: should the $15k budget include travel, or "
            "venue + catering only? Blocked on this before booking flights."
        ),
        "key": "readme-shot-offsite-budget",
    })
    ids["human_queue"] = hq["id"]

    return ids


def seed_review_outcome(task_id: str) -> None:
    """Attach a session + card outcome (summary + PR badge) to the Review
    card, and a fresh (non-stale) PR status so the badge renders live."""
    sys.path.insert(0, str(REPO_ROOT))
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore()
    session_id = "readme-shot-session-1"
    store.create(task_id=task_id, session_id=session_id, status="completed")
    pr_url = "https://github.com/nbramia/LifeOS/pull/9821"
    store.record_card_outcome(
        task_id,
        session_id=session_id,
        engine_label="Claude",
        summary=(
            "Added a weekly birthdays-upcoming digest to the CRM dashboard, "
            "sourced from existing contact birthdate fields. Tests cover the "
            "date-window boundary and timezone handling."
        ),
        branch="feat/crm-birthday-digest",
        pr_urls=[pr_url],
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO pr_status_cache (url, number, title, state, merged_at, checked_at, stale) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(url) DO UPDATE SET number=excluded.number, title=excluded.title, "
            "state=excluded.state, merged_at=excluded.merged_at, checked_at=excluded.checked_at, stale=0",
            (pr_url, 9821, "feat: add CRM birthdays-upcoming digest", "open", None, int(time.time())),
        )


def seed_schedules() -> None:
    api_post("/api/scheduler", {
        "name": "Morning briefing",
        "schedule_type": "cron",
        "schedule_value": "0 7 * * *",
        "action": "prompt",
        "message_content": "Summarize today's calendar and any open tasks due soon.",
        "bot": "",
    })
    api_post("/api/scheduler", {
        "name": "Weekly finance digest",
        "schedule_type": "cron",
        "schedule_value": "0 8 * * MON",
        "action": "prompt",
        "message_content": "Summarize last week's spending against budget by category.",
        "bot": "",
    })
    api_post("/api/scheduler", {
        "name": "Offsite reminder",
        "schedule_type": "once",
        "schedule_value": "2026-09-24T09:00:00",
        "action": "notify",
        "message_content": "The Q3 offsite agenda is due today.",
        "bot": "",
    })


def seed_conversations() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from api.services.conversation_store import ConversationStore

    store = ConversationStore()

    conv = store.create_conversation(title="Q3 offsite planning", persona_id="primary")
    store.add_message(conv.id, "user", "What's the latest on the Q3 offsite?")
    store.add_message(
        conv.id, "assistant",
        "Dana Whitfield has the venue booked for the third week of September. "
        "Catering and travel are still open — there's a task tracking the agenda draft, "
        "due the 25th.",
        sources=[
            {"file_name": "Q3 Offsite.md", "source_type": "vault", "obsidian_path": "Notes/Q3 Offsite.md"},
            {"file_name": "Draft the Q3 offsite agenda", "source_type": "task"},
        ],
    )

    conv2 = store.create_conversation(title="Acme Corp onboarding", persona_id="primary")
    store.add_message(conv2.id, "user", "When does a new Acme Corp hire's first day start?")
    store.add_message(
        conv2.id, "assistant",
        "Badge and laptop pickup at 9am, then a welcome sync with the product team at 11.",
        sources=[{
            "file_name": "Acme Corp Onboarding.md", "source_type": "vault",
            "obsidian_path": "Notes/Acme Corp Onboarding.md",
        }],
    )

    conv3 = store.create_conversation(title="Standing desk options", persona_id="primary")
    store.add_message(conv3.id, "user", "Did I ever settle on a standing desk?")
    store.add_message(
        conv3.id, "assistant",
        "Not yet — you were comparing a crank-adjustable frame against a memory-preset "
        "electric one. No decision recorded.",
        sources=[{
            "file_name": "Standing Desk Research.md", "source_type": "vault",
            "obsidian_path": "Notes/Standing Desk Research.md",
        }],
    )

    voice_conv = store.create_conversation(title="Voice: offsite budget check", persona_id="primary")
    store.add_message(voice_conv.id, "user", "What's the offsite budget looking like?")
    store.add_message(
        voice_conv.id, "assistant",
        "Fifteen thousand dollars total, and it's not yet clear whether that includes travel.",
    )


VOICE_TURN_STREAM_BODY = (
    'data: {"type":"started","turn_id":"readme-shot-turn-1"}\n'
    'data: {"type":"transcript","text":"What\'s the offsite budget looking like?"}\n'
    'data: {"type":"done","data":{"conversation_id":"readme-shot-voice-live",'
    '"transcript":"What\'s the offsite budget looking like?",'
    '"response_text":"Fifteen thousand dollars total, and it\'s not yet clear whether that includes travel.",'
    '"audio_url":null,"status_audio_urls":[]}}\n\n'
)

_GET_USER_MEDIA_STUB = """
(() => {
  const dest = new (window.AudioContext || window.webkitAudioContext)().createMediaStreamDestination();
  navigator.mediaDevices.getUserMedia = async () => dest.stream;
})();
"""


def crop_and_save(page, path: Path, clip=None) -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), clip=clip)


REAL_HOSTNAME = socket.gethostname().split(".")[0]


def _redact_real_hostname(route) -> None:
    """The board's initial GET labels session rows with this machine's real
    hostname (`api_host_name()` in api/routes/agents.py, straight from
    `socket.gethostname()` — not something the sanitized candidate env
    touches). Swap it for an obviously synthetic placeholder in the response
    body before it ever reaches the page, so no screenshot leaks the
    operator's real machine name. Deliberately an exact-path route (never
    `/api/agents/board/stream`, an EventSource whose response Playwright's
    `route.fetch()` would block reading forever) — the DOM-level sweep in
    `redact_hostname_in_dom` below is the safety net for the live stream.
    """
    response = route.fetch()
    body = response.text().replace(REAL_HOSTNAME, "readme-host")
    route.fulfill(response=response, body=body)


def redact_hostname_in_dom(page) -> None:
    """Belt-and-suspenders sweep for anywhere the real hostname could have
    reached the DOM (e.g. via the board's live EventSource, not covered by
    the route interception above): walk every text node and scrub it."""
    page.evaluate(
        """(realHost) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const nodes = [];
            let n;
            while ((n = walker.nextNode())) nodes.push(n);
            for (const node of nodes) {
                if (node.nodeValue && node.nodeValue.includes(realHost)) {
                    node.nodeValue = node.nodeValue.split(realHost).join('readme-host');
                }
            }
        }""",
        REAL_HOSTNAME,
    )


def capture(playwright) -> list[str]:
    produced: list[str] = []
    browser = playwright.chromium.launch()
    context = browser.new_context(
        viewport=DESKTOP_VIEWPORT,
        device_scale_factor=2,
        color_scheme="dark",
    )
    context.route("**/api/agents/board", _redact_real_hostname)
    page = context.new_page()

    # --- chat-thread.png ---------------------------------------------------
    page.goto(f"{BASE_URL}/chat", wait_until="networkidle")
    page.wait_for_selector("#personaPicker option", state="attached", timeout=15000)
    # Open the most recent (Q3 offsite) conversation so the thread + sources
    # + persona/model picker are all visible together.
    page.click("text=Q3 offsite planning")
    page.wait_for_selector(".message.assistant")
    page.wait_for_timeout(300)
    crop_and_save(page, IMAGES_DIR / "chat-thread.png")
    produced.append("chat-thread.png")

    # --- chat-personas.png ---------------------------------------------------
    # Force the native <select> open inline (size = option count) so every
    # shipped persona is visible in one frame — a real click opens an OS
    # popup Playwright can't capture inside the page image.
    page.evaluate(
        "() => { const el = document.getElementById('personaPicker'); "
        "el.setAttribute('size', el.options.length); el.style.position='absolute'; "
        "el.style.zIndex = 9999; }"
    )
    page.wait_for_timeout(150)
    picker = page.locator("#personaPicker")
    box = picker.bounding_box()
    if box:
        crop_and_save(page, IMAGES_DIR / "chat-personas.png", clip={
            "x": max(box["x"] - 10, 0), "y": max(box["y"] - 10, 0),
            "width": box["width"] + 260, "height": box["height"] + 20,
        })
        produced.append("chat-personas.png")
    page.evaluate(
        "() => { const el = document.getElementById('personaPicker'); "
        "el.removeAttribute('size'); }"
    )

    # --- chat-voice.png ------------------------------------------------------
    voice_page = context.new_page()
    voice_page.add_init_script(_GET_USER_MEDIA_STUB)
    voice_page.route("**/api/voice/turn/stream", lambda route: route.fulfill(
        status=200, content_type="text/event-stream", body=VOICE_TURN_STREAM_BODY,
    ))
    voice_page.goto(f"{BASE_URL}/chat?mode=voice", wait_until="networkidle")
    voice_page.wait_for_timeout(500)
    try:
        voice_page.wait_for_selector(".listen-dot.live", timeout=5000)
    except Exception:
        pass  # best-effort — the mic-live dot depends on a real getUserMedia grant
    voice_page.evaluate(
        "() => window.lifeChatVoice.submitTurn({transcript: \"What's the offsite budget looking like?\"})"
    )
    voice_page.wait_for_selector(".message.assistant", timeout=15000)
    voice_page.wait_for_timeout(300)
    crop_and_save(voice_page, IMAGES_DIR / "chat-voice.png")
    produced.append("chat-voice.png")
    voice_page.close()

    # --- agents-board.png ------------------------------------------------------
    board_page = context.new_page()
    board_page.goto(f"{BASE_URL}/agents", wait_until="domcontentloaded")
    board_page.wait_for_selector(".board-card", timeout=15000)
    board_page.wait_for_timeout(500)
    redact_hostname_in_dom(board_page)
    crop_and_save(board_page, IMAGES_DIR / "agents-board.png")
    produced.append("agents-board.png")

    # --- agents-schedules.png (the Scheduled lane column) ------------------
    # The Scheduled lane is a column on the same board, not a separate tab.
    scheduled_lane = board_page.locator('.board-lane[data-lane="scheduled"]')
    box = scheduled_lane.bounding_box()
    if box:
        crop_and_save(board_page, IMAGES_DIR / "agents-schedules.png", clip={
            "x": max(box["x"] - 10, 0), "y": max(box["y"] - 10, 0),
            "width": box["width"] + 20, "height": min(box["height"] + 20, DESKTOP_VIEWPORT["height"]),
        })
        produced.append("agents-schedules.png")

    # --- agents-card.png (Review-lane card drawer) ------------------------
    board_page.click("text=Add a weekly digest of upcoming birthdays to the CRM")
    board_page.wait_for_selector("#board-drawer-backdrop:not([hidden])", timeout=5000)
    board_page.wait_for_timeout(300)
    redact_hostname_in_dom(board_page)
    crop_and_save(board_page, IMAGES_DIR / "agents-card.png")
    produced.append("agents-card.png")
    board_page.close()

    # --- tasks-view.png (plain task card drawer: due date/context/tags) ---
    tasks_page = context.new_page()
    tasks_page.goto(f"{BASE_URL}/agents", wait_until="domcontentloaded")
    tasks_page.wait_for_selector(".board-card", timeout=15000)
    tasks_page.click("text=Draft the Q3 offsite agenda for the product team")
    tasks_page.wait_for_selector("#board-drawer-backdrop:not([hidden])", timeout=5000)
    tasks_page.wait_for_timeout(300)
    redact_hostname_in_dom(tasks_page)
    crop_and_save(tasks_page, IMAGES_DIR / "tasks-view.png")
    produced.append("tasks-view.png")
    tasks_page.close()

    # --- home-dashboard.png -------------------------------------------------
    home_page = context.new_page()
    home_page.goto(f"{BASE_URL}/", wait_until="networkidle")
    home_page.wait_for_timeout(300)
    crop_and_save(home_page, IMAGES_DIR / "home-dashboard.png")
    produced.append("home-dashboard.png")
    home_page.close()

    browser.close()
    return produced


def main() -> None:
    seed_vault_notes()
    task_ids = seed_tasks()
    seed_review_outcome(task_ids["review"])
    seed_schedules()
    seed_conversations()

    # Give the task-file watcher a moment to index the freshly written
    # markdown before the board's first read.
    time.sleep(2.5)

    with sync_playwright() as p:
        produced = capture(p)

    print("Produced:", ", ".join(produced))


if __name__ == "__main__":
    main()
