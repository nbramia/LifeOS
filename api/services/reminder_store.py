"""
Backward-compatibility shim for the Scheduler subsystem.

The canonical module for reminders/scheduling is
``api/services/scheduler_store.py``. This module re-exports its symbols
under their legacy ``Reminder*`` names so existing callers (HTTP routes,
chat orchestrator, agent tools, seed scripts) keep working until they
migrate to the new names directly.

Prefer importing from ``api.services.scheduler_store`` in new code.
"""
from api.services.scheduler_store import (  # noqa: F401
    ScheduleEntry as Reminder,
    SchedulerStore as ReminderStore,
    SchedulerScheduler as ReminderScheduler,
    compute_next_trigger,
    get_scheduler_store as get_reminder_store,
    get_scheduler as get_reminder_scheduler,
    _format_cron_human,
    _format_dt_short,
)
