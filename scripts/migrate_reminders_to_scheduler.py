#!/usr/bin/env python3
"""
One-shot migration: ``~/.lifeos/reminders.json`` → the Scheduler store.

The reminder store used JSON as the source of truth. The Scheduler (issue #244)
uses ``LifeOS/Scheduler/Inbox.md`` as the source of truth with a rebuildable
index cache. This script reads the legacy JSON and writes each entry as a
schedule line, mapping the old message types to actions:

    static   → notify
    prompt   → prompt
    endpoint → endpoint

It is **idempotent** (a marker file guards re-runs) and **non-destructive**
(``reminders.json`` is kept as a backup, never deleted).

Usage:
    ~/.venvs/lifeos/bin/python scripts/migrate_reminders_to_scheduler.py
    ~/.venvs/lifeos/bin/python scripts/migrate_reminders_to_scheduler.py --force
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("migrate_reminders")

DEFAULT_JSON_PATH = Path.home() / ".lifeos" / "reminders.json"

# Legacy message_type → action.
_TYPE_TO_ACTION = {"static": "notify", "prompt": "prompt", "endpoint": "endpoint"}


def migrate(
    json_path: Optional[Path] = None,
    store=None,
    force: bool = False,
) -> int:
    """Migrate legacy reminders into the Scheduler store.

    Returns the number of schedules created (0 if nothing to do / already done).
    A ``<json>.migrated`` marker file makes this safe to call repeatedly (e.g.
    on every server startup).
    """
    json_path = Path(json_path) if json_path else DEFAULT_JSON_PATH
    marker = json_path.with_suffix(json_path.suffix + ".migrated")

    if marker.exists() and not force:
        logger.info(f"Reminders already migrated (marker {marker} present); skipping.")
        return 0

    if not json_path.exists():
        logger.info(f"No legacy reminders at {json_path}; nothing to migrate.")
        marker.write_text("no-op: source absent\n", encoding="utf-8")
        return 0

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Could not read {json_path}: {e}")
        return 0

    reminders = data.get("reminders", [])
    if not reminders:
        logger.info("Legacy reminders file has no entries; nothing to migrate.")
        marker.write_text("no-op: empty\n", encoding="utf-8")
        return 0

    if store is None:
        from api.services.scheduler_store import get_scheduler_store
        store = get_scheduler_store()

    created = 0
    for r in reminders:
        message_type = r.get("message_type", "static")
        action = _TYPE_TO_ACTION.get(message_type, "notify")
        entry = store.create(
            name=r.get("name", "Untitled"),
            schedule_type=r.get("schedule_type", "once"),
            schedule_value=r.get("schedule_value", ""),
            action=action,
            message_type=message_type,
            message_content=r.get("message_content", ""),
            endpoint_config=r.get("endpoint_config"),
            enabled=r.get("enabled", True),
            timezone=r.get("timezone", ""),
        )
        # Preserve the original last-fired timestamp for the dashboard.
        if r.get("last_triggered_at"):
            store.update(entry.id, last_triggered_at=r["last_triggered_at"])
        created += 1
        logger.info(f"Migrated reminder '{entry.name}' → schedule {entry.id} (action: {action})")

    marker.write_text(
        f"migrated {created} reminders\nsource kept as backup: {json_path}\n",
        encoding="utf-8",
    )
    logger.info(f"Migration complete: {created} schedules created. "
                f"Backup retained at {json_path}.")
    return created


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Migrate reminders.json into the Scheduler store.")
    parser.add_argument("--force", action="store_true", help="Re-run even if the marker exists.")
    parser.add_argument("--path", type=str, default=None, help="Path to reminders.json.")
    args = parser.parse_args()

    # Ensure the project root is importable when run as a script.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    migrate(json_path=Path(args.path) if args.path else None, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
