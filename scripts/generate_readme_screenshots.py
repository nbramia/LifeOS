#!/usr/bin/env python3
"""Generate the LifeOS README screenshots against an isolated test instance.

Seeds an owned, isolated candidate instance (see `scripts/test_instance.py`)
with obviously synthetic data — tasks, board cards, schedules, and chat
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
import json
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

# Narrower/shorter than a full desktop viewport, deliberately: these
# screenshots are meant to read as a system in active use, not a mostly-empty
# window, so the captures below crop dead space rather than leaving a big
# blank area.
CHAT_VIEWPORT = {"width": 1440, "height": 820}
BOARD_VIEWPORT = {"width": 2200, "height": 960}

REAL_HOSTNAME = socket.gethostname().split(".")[0]


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
    (notes_dir / "1099 Tracking.md").write_text(
        "# 1099 Tracking\n\n"
        "Contractor 1099s for the year live in the Schwab tax-documents "
        "portal once issued, usually mid-to-late September.\n"
    )


def api_post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{BASE_URL}{path}", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def seed_tasks() -> dict:
    """Task cards covering every board lane, several per lane, spread across
    every assignee (me/claude/codex/hermes/local/cloud, plus unassigned) with
    a believable mix of running/completed/blocked states, due dates, and a
    couple of longer titles so the lanes don't look uniform."""
    ids: dict[str, list[str]] = {
        "unassigned": [], "assigned": [], "in_progress": [], "review": [],
        "done": [], "snoozed": [], "human_queue": [],
    }

    unassigned = [
        ("Research standing desk options for the home office", "Home", ["research"], None,
         "Compare a crank-adjustable frame vs. a memory-preset electric one."),
        ("Compare flight options for the November site visit", "Work", ["research", "travel"],
         "2026-11-10", None),
        ("Draft talking points for the all-hands", "Work", ["work"], None, None),
        ("Audit the CRM's contact de-duplication rules against six months of merge "
         "conflicts and write up recommendations", "Work", ["work", "infra"], None, None),
    ]
    for description, context, tags, due, notes in unassigned:
        t = api_post("/api/tasks", {
            "description": description, "context": context,
            "tags": tags, "due_date": due, "notes": notes,
        })
        ids["unassigned"].append(t["id"])

    assigned = [
        ("Draft the Q3 offsite agenda for the product team", ["codex"], "2026-09-25",
         "Dana Whitfield is coordinating; venue is booked, catering and travel are open.", None),
        ("Summarize last week's support tickets", ["me"], "2026-09-21", None, None),
        ("Write release notes for the mobile app", ["hermes"], "2026-10-02", None, None),
        ("Triage the backlog of low-priority CRM bugs and propose which five to fix "
         "this sprint", ["cloud"], None, None, {"effort": "low"}),
        ("Set up a staging environment for the new billing service", ["local"], "2026-09-28",
         None, {"model": "gemma-local"}),
    ]
    for description, tags, due, notes, fields in assigned:
        t = api_post("/api/tasks", {
            "description": description, "context": "Work", "tags": tags, "due_date": due,
            "notes": notes, "fields": fields,
        })
        ids["assigned"].append(t["id"])

    # More than one genuinely live session below (see seed_live_sessions) —
    # two of these are plain claimed-but-not-yet-spawned, matching how a
    # real board mixes "running" with "claimed, about to start".
    in_progress = [
        ("Fix the flaky import on the finance sync", ["claude", "agent-running"],
         "Intermittent timeout on the monthly transaction import job.",
         {"model": "claude-sonnet-5", "effort": "high"}),
        ("Migrate the search index to the new schema", ["codex", "agent-running"], None, None),
        ("Investigate the Slack webhook timeout", ["hermes", "agent-running"], None, None),
        ("Rebuild the Monarch transaction categorizer with the new taxonomy and "
         "backfill six months of transaction history", ["local", "agent-running"], None,
         {"model": "gemma-local"}),
    ]
    for description, tags, notes, fields in in_progress:
        t = api_post("/api/tasks", {
            "description": description, "context": "Work", "status": "in_progress",
            "tags": tags, "notes": notes, "fields": fields,
        })
        ids["in_progress"].append(t["id"])

    review = [
        "Add a weekly digest of upcoming birthdays to the CRM",
        "Fix pagination bug on the transactions API",
        "Add dark-mode support to the journal wheel",
    ]
    for description in review:
        t = api_post("/api/tasks", {
            "description": description, "context": "Work", "status": "done", "tags": ["agent-completed"],
        })
        ids["review"].append(t["id"])

    done = [
        ("Reply to the plumber about Thursday", None),
        ("Renew the home wifi router firmware", None),
        ("Pay the quarterly estimated taxes", None),
        ("Cancel the unused streaming subscription", None),
        ("Auto-migrate the sync watchdog config to the new schema", ["agent-failed"]),
    ]
    for description, tags in done:
        t = api_post("/api/tasks", {
            "description": description, "context": "Home", "status": "done", "tags": tags,
        })
        ids["done"].append(t["id"])

    snoozed = [
        ("Evaluate the new expense-report template", "2026-10-15T09:00:00+00:00"),
        ("Look into the annual eero firmware update", "2026-11-02T09:00:00+00:00"),
        ("Revisit the standing-desk decision", "2026-10-01T09:00:00+00:00"),
    ]
    for description, wake in snoozed:
        t = api_post("/api/tasks", {
            "description": description, "context": "Home",
            "fields": {"snoozed_until": wake},
        })
        ids["snoozed"].append(t["id"])

    # Human queue — a mix of operator-filed agent questions (via the
    # human-queue endpoint: status blocked, tag human) and a genuinely
    # agent-blocked card (status blocked, tag agent-blocked) — two different
    # reasons a card ends up waiting on a human.
    human_queue = [
        ("Confirm the Q3 offsite budget ceiling",
         "Agent question: should the $15k budget include travel, or "
         "venue + catering only? Blocked on this before booking flights.",
         "readme-shot-offsite-budget"),
        ("Approve the new vendor contract terms",
         "Agent question: the vendor wants a 2-year lock-in for a 10% "
         "discount — approve, or push for a 1-year term instead?",
         "readme-shot-vendor-contract"),
        ("Pick a font family for the redesigned dashboard",
         "Agent question: Inter or Söhne for the new dashboard headings?",
         "readme-shot-font-pick"),
    ]
    for title, notes, key in human_queue:
        hq = api_post("/api/tasks/human-queue", {"title": title, "notes": notes, "key": key})
        ids["human_queue"].append(hq["id"])

    t = api_post("/api/tasks", {
        "description": "Finish the vendor-integration webhook",
        "context": "Work", "status": "blocked", "tags": ["codex", "agent-blocked"],
        "notes": "Blocked on missing API credentials for the vendor's sandbox environment.",
    })
    ids["human_queue"].append(t["id"])

    return ids


def seed_review_outcomes(review_task_ids: list[str]) -> None:
    """Attach a session + card outcome (summary + PR badge) to each Review
    card, with fresh (non-stale) PR statuses in varied states so the badges
    render live and show some variety."""
    sys.path.insert(0, str(REPO_ROOT))
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore()
    outcomes = [
        (
            "Claude",
            "Added a weekly birthdays-upcoming digest to the CRM dashboard, "
            "sourced from existing contact birthdate fields. Tests cover the "
            "date-window boundary and timezone handling.",
            "feat/crm-birthday-digest",
            "https://github.com/nbramia/LifeOS/pull/9821", 9821,
            "feat: add CRM birthdays-upcoming digest", "open",
        ),
        (
            "Codex",
            "Fixed an off-by-one in the transactions API's cursor pagination "
            "that dropped the last row of every page. Added a regression "
            "test for the page-boundary case.",
            "fix/transactions-pagination",
            "https://github.com/nbramia/LifeOS/pull/9834", 9834,
            "fix: correct transactions API pagination cursor", "merged",
        ),
        (
            "Hermes",
            "Added a dark palette for the journal emotion wheel, matching "
            "the rest of the app's dark theme. Verified contrast against "
            "the existing light palette's ratios.",
            "feat/journal-dark-mode",
            "https://github.com/nbramia/LifeOS/pull/9840", 9840,
            "feat: dark mode for the journal emotion wheel", "open",
        ),
    ]
    # A fresh, short-lived connection per iteration — NOT one connection
    # wrapping the whole loop, which would hold an uncommitted transaction
    # open across every `store.create()`/`store.record_card_outcome()` call
    # below (each of which opens its own connection via `store._connect()`)
    # and deadlock-via-busy-timeout against itself on the second iteration.
    for i, (task_id, (engine, summary, branch, pr_url, pr_number, pr_title, pr_state)) in enumerate(
        zip(review_task_ids, outcomes),
    ):
        session_id = f"readme-shot-review-session-{i + 1}"
        store.create(task_id=task_id, session_id=session_id, status="completed")
        store.record_card_outcome(
            task_id, session_id=session_id, engine_label=engine, summary=summary,
            branch=branch, pr_urls=[pr_url],
        )
        merged_at = "2026-09-18T15:04:00+00:00" if pr_state == "merged" else None
        with sqlite3.connect(store.db_path, timeout=10.0) as conn:
            conn.execute(
                "INSERT INTO pr_status_cache (url, number, title, state, merged_at, checked_at, stale) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(url) DO UPDATE SET number=excluded.number, title=excluded.title, "
                "state=excluded.state, merged_at=excluded.merged_at, checked_at=excluded.checked_at, stale=0",
                (pr_url, pr_number, pr_title, pr_state, merged_at, int(time.time())),
            )


def seed_live_sessions(in_progress_task_ids: list[str]) -> None:
    """Two genuinely live sessions (status="running", non-terminal) linked
    to In-progress cards — these are what makes the board card show the
    pulsing "live" dot and "session" chip, not just the tag. The other two
    In-progress cards are left claimed-but-unlinked, the realistic mix of
    "actually running" vs. "just claimed" a live board shows. Budgets are
    seeded on the sessions for data fidelity (a card drawer would show
    them), even though the board card face itself never renders a dollar
    figure — only the linked-session chip and live dot.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore()
    live = [
        (in_progress_task_ids[0], "readme-shot-live-session-1", "claude",
         {"wall_seconds": 3600, "max_tokens": 200000, "max_dollars": 5.0}),
        (in_progress_task_ids[1], "readme-shot-live-session-2", "codex",
         {"wall_seconds": 1800, "max_tokens": 120000, "max_dollars": 2.5}),
    ]
    for task_id, session_id, routing, budget in live:
        store.create(task_id=task_id, session_id=session_id, status="running",
                     routing=routing, budget=budget)


def seed_schedules() -> None:
    schedules = [
        ("Morning briefing", "cron", "0 7 * * *", "prompt",
         "Summarize today's calendar and any open tasks due soon."),
        ("Weekly finance digest", "cron", "0 8 * * MON", "prompt",
         "Summarize last week's spending against budget by category."),
        ("Offsite reminder", "once", "2026-09-24T09:00:00", "notify",
         "The Q3 offsite agenda is due today."),
        ("Evening wind-down check-in", "cron", "0 21 * * *", "prompt",
         "Ask how the day went and note anything worth remembering."),
    ]
    for name, schedule_type, schedule_value, action, message_content in schedules:
        api_post("/api/scheduler", {
            "name": name, "schedule_type": schedule_type, "schedule_value": schedule_value,
            "action": action, "message_content": message_content, "bot": "",
        })


def seed_conversations() -> None:
    """Seeds chat threads with several exchanges each — these populate the
    conversation sidebar (and the "agent threads" rail, via the Review-lane
    outcomes seeded separately) behind the voice screenshot."""
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
    store.add_message(conv.id, "user", "Has Dana confirmed the headcount for catering yet?")
    store.add_message(
        conv.id, "assistant",
        "Not yet — that's still one of the open questions on the budget task in the human queue.",
        sources=[{"file_name": "Confirm the Q3 offsite budget ceiling", "source_type": "task"}],
    )
    store.add_message(conv.id, "user", "Good — ping me if the venue booking changes.")
    store.add_message(conv.id, "assistant", "Will do.")

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
    store.add_message(conv2.id, "user", "Anything they need before day one?")
    store.add_message(conv2.id, "assistant", "Just a signed offer letter on file — IT provisions the rest.")

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
    store.add_message(conv3.id, "user", "Remind me to revisit it next month.")
    store.add_message(conv3.id, "assistant", "Done — snoozed the task until early October.")

    tasks_conv = store.create_conversation(title="Task and reminder setup", persona_id="primary")
    store.add_message(tasks_conv.id, "user", "Next Wednesday I need to pull down my 1099 from Schwab.")
    store.add_message(
        tasks_conv.id, "assistant",
        "Got it — I added a task: \"Pull down 1099 from Schwab,\" due next Wednesday, Sept 23.",
    )
    store.add_message(tasks_conv.id, "user", "Also remind me to follow up with Dana next Tuesday.")
    store.add_message(
        tasks_conv.id, "assistant",
        "Done — a reminder is set for Tuesday, Sept 22 to follow up with Dana.",
    )


# The live voice exchange captured in chat-voice.png — this IS the seeded
# "Voice: offsite budget check" conversation's storyline, played out live
# through the real voice turn pipeline (stubbed only at the network edge —
# see VOICE_TURN_STREAM_BODY) rather than pre-written into the store, so the
# recording indicator and message rendering are genuinely driven by the UI.
VOICE_TURNS = [
    ("What's the offsite budget looking like?",
     "Fifteen thousand dollars total, and it's not yet clear whether that includes travel."),
    ("Has Dana confirmed the venue yet?",
     "Yes — the venue's booked for the third week of September."),
    ("Who's coordinating catering?",
     "Dana Whitfield is — she's waiting on a headcount before she can confirm."),
    ("Remind me to check on catering Friday.",
     "Done — I'll remind you Friday to check on catering."),
]

_GET_USER_MEDIA_STUB = """
(() => {
  const dest = new (window.AudioContext || window.webkitAudioContext)().createMediaStreamDestination();
  navigator.mediaDevices.getUserMedia = async () => dest.stream;
})();
"""


def _voice_turn_stream_body(transcript: str, response_text: str) -> str:
    done = {
        "conversation_id": "readme-shot-voice-live",
        "transcript": transcript,
        "response_text": response_text,
        "audio_url": None,
        "status_audio_urls": [],
    }
    return (
        f'data: {json.dumps({"type": "started", "turn_id": "readme-shot-turn"})}\n'
        f'data: {json.dumps({"type": "transcript", "text": transcript})}\n'
        f'data: {json.dumps({"type": "done", "data": done})}\n\n'
    )


def crop_and_save(page, path: Path, clip=None) -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), clip=clip)


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


def crop_to_content(page, path: Path, *, bottom_locator=None, exclude_locator=None, pad: int = 32) -> None:
    """Screenshot the page cropped to its actual content height instead of
    the full (mostly empty) viewport: `bottom_locator` gives the last
    meaningful element to include, `exclude_locator` an element (e.g. the
    board's bottom quick-action tray) to crop above instead."""
    viewport = page.viewport_size
    width = viewport["width"]
    height = viewport["height"]
    if exclude_locator is not None:
        box = exclude_locator.bounding_box()
        if box:
            height = min(height, int(box["y"]))
    elif bottom_locator is not None:
        box = bottom_locator.bounding_box()
        if box:
            height = min(height, int(box["y"] + box["height"] + pad))
    crop_and_save(page, path, clip={"x": 0, "y": 0, "width": width, "height": max(height, 100)})


def scroll_messages_to_bottom(page) -> None:
    """Instant (non-smooth) scroll of the message list to its true bottom.
    addMessage()'s own scrollToBottom() uses `behavior: 'smooth'`, an
    animated scroll that a fixed short wait_for_timeout can outrace —
    leaving the last message only partially scrolled into view and cut off
    by the viewport edge in a clip-based screenshot (no full_page capture
    can recover it either: #messages is an internally-scrolling region,
    not page-level overflow, so full_page renders the same viewport-only
    content). Setting scrollTop directly bypasses the animation entirely.
    """
    page.evaluate(
        "() => { const el = document.getElementById('messages'); "
        "if (el) el.scrollTop = el.scrollHeight; }"
    )


def _lanes_content_bottom(page, lanes_locator) -> float:
    """Max bottom-edge y (viewport coords) of any card across the given set
    of `.board-lane` locators, which crops a board screenshot to just
    below the tallest column's actual cards, not the full stretched lane
    height (`.board-lane` fills the row via flex `align-items: stretch`
    regardless of card count)."""
    max_bottom = 0.0
    for i in range(lanes_locator.count()):
        cards = lanes_locator.nth(i).locator(".board-card")
        if cards.count() == 0:
            continue
        box = cards.last.bounding_box()
        if box:
            max_bottom = max(max_bottom, box["y"] + box["height"])
    return max_bottom


def _crop_lane_range(page, path: Path, lane_ids: list[str], produced: list[str], label: str) -> None:
    """Crop a contiguous run of lane columns (by `data-lane` id, in board
    order) to their actual content height, at the board's normal capture
    resolution — a legible close-up, unlike the necessarily-small-text full
    8-lane overview."""
    selector = ", ".join(f'.board-lane[data-lane="{lane_id}"]' for lane_id in lane_ids)
    lanes = page.locator(selector)
    first_box = page.locator(f'.board-lane[data-lane="{lane_ids[0]}"]').bounding_box()
    last_box = page.locator(f'.board-lane[data-lane="{lane_ids[-1]}"]').bounding_box()
    bottom = _lanes_content_bottom(page, lanes)
    if first_box and last_box and bottom:
        crop_and_save(page, path, clip={
            "x": max(first_box["x"] - 10, 0), "y": max(first_box["y"] - 10, 0),
            "width": (last_box["x"] + last_box["width"]) - first_box["x"] + 20,
            "height": (bottom + 24) - max(first_box["y"] - 10, 0),
        })
        produced.append(label)


def capture(playwright) -> list[str]:
    produced: list[str] = []
    browser = playwright.chromium.launch()
    context = browser.new_context(device_scale_factor=2, color_scheme="dark")
    context.route("**/api/agents/board", _redact_real_hostname)

    # --- chat-voice.png — the single chat/voice hero shot ------------------
    voice_page = context.new_page()
    voice_page.set_viewport_size(CHAT_VIEWPORT)
    voice_page.add_init_script(_GET_USER_MEDIA_STUB)
    turn_index = {"i": 0}

    def _fulfill_next_turn(route):
        transcript, response_text = VOICE_TURNS[turn_index["i"] % len(VOICE_TURNS)]
        turn_index["i"] += 1
        route.fulfill(
            status=200, content_type="text/event-stream",
            body=_voice_turn_stream_body(transcript, response_text),
        )

    voice_page.route("**/api/voice/turn/stream", _fulfill_next_turn)
    voice_page.goto(f"{BASE_URL}/chat?mode=voice", wait_until="networkidle")
    voice_page.wait_for_timeout(500)
    # "Listening" (wake-word) is left checked — its default state, and a
    # working, supported feature (whisper-relay's POST /api/voice/transcribe)
    # worth showing enabled in the hero voice shot.
    for transcript, _ in VOICE_TURNS:
        voice_page.evaluate(
            "(t) => window.lifeChatVoice.submitTurn({transcript: t})", transcript,
        )
        voice_page.wait_for_function(
            "(n) => document.querySelectorAll('.message.assistant').length >= n",
            arg=turn_index["i"],
            timeout=15000,
        )
        voice_page.wait_for_timeout(200)
    scroll_messages_to_bottom(voice_page)
    voice_page.wait_for_timeout(300)
    # Voice answers never carry source citations in this UI — submitTurn()'s
    # `addMessage(data.response_text, 'assistant')` call passes no sources
    # argument, unlike the text path — so none are shown here; that would be
    # depicting a capability the real voice turn doesn't have.
    crop_to_content(voice_page, IMAGES_DIR / "chat-voice.png", bottom_locator=voice_page.locator("#voiceDock"))
    produced.append("chat-voice.png")
    voice_page.close()

    # --- agents-board.png (all 8 lanes) -------------------------------------
    board_page = context.new_page()
    board_page.set_viewport_size(BOARD_VIEWPORT)
    board_page.goto(f"{BASE_URL}/agents", wait_until="domcontentloaded")
    board_page.wait_for_selector(".board-card", timeout=15000)
    # Show every lane, including Done and Snoozed (hidden by default).
    board_page.click("#board-lane-filter-btn")
    board_page.click("#board-lane-filter-all")
    # Close the filter dropdown — a click anywhere outside it closes it
    # (board.js's own document-level listener); an empty spot low in a lane
    # column is never a link and never inside the dropdown's own box.
    board_page.mouse.click(100, 700)
    board_page.wait_for_timeout(500)
    redact_hostname_in_dom(board_page)
    all_lanes = board_page.locator(".board-lane")
    board_box = all_lanes.first.bounding_box()
    content_bottom = _lanes_content_bottom(board_page, all_lanes)
    if board_box and content_bottom:
        # Ends shortly below the longest column's last card — at 8 lanes
        # wide, this is necessarily a wide/short overview image; the detail
        # crops below cover legible close-up reading.
        crop_and_save(board_page, IMAGES_DIR / "agents-board.png", clip={
            "x": 0, "y": max(board_box["y"] - 10, 0),
            "width": BOARD_VIEWPORT["width"],
            "height": (content_bottom + 24) - max(board_box["y"] - 10, 0),
        })
    else:
        crop_to_content(
            board_page, IMAGES_DIR / "agents-board.png",
            exclude_locator=board_page.locator("#board-drop-tray"),
        )
    produced.append("agents-board.png")

    # --- agents-board-detail-1.png: In progress, with its neighbours -------
    _crop_lane_range(
        board_page, IMAGES_DIR / "agents-board-detail-1.png",
        ["assigned", "in_progress", "human_queue"], produced, "agents-board-detail-1.png",
    )

    # --- agents-board-detail-2.png: Review / Human queue --------------------
    _crop_lane_range(
        board_page, IMAGES_DIR / "agents-board-detail-2.png",
        ["human_queue", "scheduled", "review"], produced, "agents-board-detail-2.png",
    )

    board_page.close()
    browser.close()
    return produced


def main() -> None:
    seed_vault_notes()
    task_ids = seed_tasks()
    seed_review_outcomes(task_ids["review"])
    seed_live_sessions(task_ids["in_progress"])
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
