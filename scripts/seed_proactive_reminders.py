#!/usr/bin/env python3
"""
Seed proactive intelligence reminders for LifeOS.

Creates four prompt-type reminders:
1. Pre-meeting prep (every 15 min on weekdays, checks for upcoming meetings)
2. Morning briefing (daily at 6:30 AM in the configured LifeOS timezone)
3. Weekly communication gap digest (Sundays at 10 AM in the configured timezone)
4. Weekly Life Inbox review (Sundays at 10 AM in the configured timezone)

Usage:
    ~/.venvs/lifeos/bin/python scripts/seed_proactive_reminders.py [--dry-run] [--only NAME]

Each module is a standard prompt-type reminder — no new infrastructure.
Delete any reminder via the API to disable it.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.reminder_store import get_reminder_store
from config.settings import settings

# Use the same resolved timezone as the running LifeOS service.  Importing the
# settings object also loads the repository .env for manual CLI runs, so a
# seed command cannot silently fall back to a different machine timezone.
_TZ = settings.timezone

# ---------------------------------------------------------------------------
# Prompt definitions
# ---------------------------------------------------------------------------

MEETING_PREP_PROMPT = """\
Check my calendar for the next meeting happening in the next 20 minutes. \
If there is no meeting in the next 20 minutes, reply with exactly "NO_MEETING" and nothing else.

If there IS an upcoming meeting:
1. Get the meeting details (title, time, attendees)
2. For each attendee, look up their profile and recent interactions
3. Check for any related tasks or notes about the meeting topic

Format your response as:

**[Meeting Title]** — [time]

**Attendees:**
- [Name]: [role/company if known]. Last interaction: [date, brief context]. [Any relevant facts or pending items]

**Context:**
- [Any relevant notes, past meetings on this topic, or related tasks]

**Suggested talking points:**
- [Based on pending items, recent interactions, or communication gaps]

Keep it concise and scannable. Only include information that's actually useful for the meeting.\
"""

MORNING_BRIEFING_PROMPT = """\
Generate my morning briefing for today. Use the following structure exactly:

**Today's Schedule**
Search my calendar for today's events. For each meeting, include the time and a one-line context note \
(who's attending, what it's about based on past interactions or notes). Flag any back-to-back meetings.

**Tasks**
List tasks that are due today or overdue. For each, include the task description and context. \
If there are no due/overdue tasks, say "No urgent tasks today."

**Overnight Emails**
Search my email for messages received since 6 PM yesterday. Highlight only emails that seem \
important or actionable (not newsletters, automated notifications, or marketing). For each, \
include sender, subject, and a one-line summary.

**Relationship Check-in**
Check for communication gaps — anyone important I haven't contacted in over 14 days. \
Only mention the top 2-3 most relevant gaps, not an exhaustive list.

Keep the entire briefing concise — aim for something I can read in 2 minutes. \
Use bullet points, not paragraphs. Skip any section that has nothing to report.

If there is truly nothing to report (no events, no tasks, no emails), respond with exactly NOTHING_TO_REPORT\
"""

COMMUNICATION_GAPS_PROMPT = """\
Generate my weekly relationship check-in digest. \
Search for people in my network and check communication gaps.

For close contacts (family, close friends), flag gaps over 14 days.
For professional contacts, flag gaps over 30 days.

Format as:

**Close Contacts — Overdue Check-in**
- [Name]: Last contact [X days ago] via [source]. [One-line context of last interaction]

**Professional Network — Consider Reaching Out**
- [Name]: Last contact [X days ago]. [Brief context — what you discussed, any pending items]

Only include people where reaching out would be genuinely valuable. \
Skip anyone where the gap is expected or normal. \
Limit to 5-7 people maximum. \
If there are no significant gaps, say "All relationships are current — no action needed."\
"""

LIFE_INBOX_REVIEW_PROMPT = """\
Review my Life Inbox from the last 7 days.

Process every clear, low-risk capture automatically: save durable memories,
ideas, projects, relationships, sources, knowledge, and preferences with their
original evidence. Dismiss ordinary acknowledgements and conversational noise.
Keep tasks and reminders as proposals unless I explicitly approved creating
them. Do not ask whether you should process the inbox after you have reviewed
it. Report a concise summary of what was filed, which proposals need my
approval, and which ambiguous items remain.

If there is nothing unresolved, reply with exactly NOTHING_TO_REPORT.
"""

# ---------------------------------------------------------------------------
# Reminder definitions
# ---------------------------------------------------------------------------

REMINDERS = [
    {
        "name": "Pre-Meeting Prep",
        "schedule_type": "cron",
        "schedule_value": "*/15 8-18 * * 1-5",  # Every 15 min, 8am-6pm, weekdays
        "message_type": "prompt",
        "message_content": MEETING_PREP_PROMPT,
        "enabled": True,
        "timezone": _TZ,
    },
    {
        "name": "Morning Briefing",
        "schedule_type": "cron",
        "schedule_value": "30 6 * * *",  # 6:30 AM daily
        "message_type": "prompt",
        "message_content": MORNING_BRIEFING_PROMPT,
        "enabled": True,
        "timezone": _TZ,
    },
    {
        "name": "Weekly Relationship Digest",
        "schedule_type": "cron",
        "schedule_value": "0 10 * * 0",  # Sundays at 10 AM
        "message_type": "prompt",
        "message_content": COMMUNICATION_GAPS_PROMPT,
        "enabled": True,
        "timezone": _TZ,
    },
    {
        "name": "Weekly Life Inbox review",
        "schedule_type": "cron",
        "schedule_value": "0 10 * * 0",  # Sundays at 10:00 AM
        "message_type": "prompt",
        "message_content": LIFE_INBOX_REVIEW_PROMPT,
        "enabled": True,
        "timezone": _TZ,
    },
]


def main():
    parser = argparse.ArgumentParser(description="Seed proactive intelligence reminders")
    parser.add_argument("--dry-run", action="store_true", help="Print reminders without creating them")
    parser.add_argument("--force", action="store_true", help="Create even if reminders with same names exist")
    parser.add_argument(
        "--only",
        action="append",
        dest="only",
        metavar="NAME",
        help="Seed only the named reminder; may be passed more than once.",
    )
    args = parser.parse_args()

    selected = set(args.only or [])
    definitions = [
        reminder_def for reminder_def in REMINDERS
        if not selected or reminder_def["name"] in selected
    ]
    unknown = selected - {reminder_def["name"] for reminder_def in REMINDERS}
    if unknown:
        parser.error("unknown reminder name(s): " + ", ".join(sorted(unknown)))

    store = get_reminder_store()
    existing = {r.name for r in store.list_all()}

    created = 0
    skipped = 0

    for reminder_def in definitions:
        name = reminder_def["name"]

        if name in existing and not args.force:
            print(f"  SKIP  {name} (already exists, use --force to recreate)")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  DRY   {name}")
            print(f"        schedule: {reminder_def['schedule_value']}")
            print(f"        prompt length: {len(reminder_def['message_content'])} chars")
            continue

        reminder = store.create(**reminder_def)
        print(f"  OK    {name} (id={reminder.id})")
        created += 1

    print()
    if args.dry_run:
        print(f"Dry run: {len(definitions)} reminders would be created, {skipped} skipped")
    else:
        print(f"Created {created} reminders, skipped {skipped}")


if __name__ == "__main__":
    main()
