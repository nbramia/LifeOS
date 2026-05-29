"""
Backward-compatibility shim for the renamed scheduler subsystem.

The reminder store was renamed to the Scheduler (see issue #244): the canonical
module is now ``api/services/scheduler_store.py``. This module re-exports the
new symbols under their legacy ``Reminder*`` names so existing callers (HTTP
routes, chat orchestrator, agent tools, seed scripts) keep working until the
public surfaces are renamed in #246.

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
