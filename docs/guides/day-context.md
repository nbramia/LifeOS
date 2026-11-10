# Day-context job

**Status:** Complete
**Audience:** Operators

LifeOS assembles the Dailies suggestions (Home, `home.ramia.us/dailies`) and
writes them to Home's `home_day_context` table. Home reads that table; Dailies
never calls LifeOS, Pendant or Apple directly, and nothing is saved into an
entry unless Nathan saves.

## What it does

`api/services/day_context.py` runs as a background loop started by the API
lifespan (`api/main.py`), once at startup and then every 30 minutes. Each pass
covers the Dailies logical day (America/New_York, before 03:00 counts as the
previous day) and the day before:

| Field | Source |
|---|---|
| `sleep_hours`, `sleep_source` (`apple_watch`) | fitness.db `health_metrics`, `metric_type='sleep_hours'`, the row keyed on the wake-up date |
| `feelings` | Pendant `emotional_moment`, only when `confidence >= 0.8`; otherwise `[]` |
| `resonant`, `words` | Pendant day signals |

Pendant signals come from `GET <LIFEOS_PENDANT_FEED_URL>/day-signals?day=YYYY-MM-DD`
with the feed's bearer token. A 404, connection error or malformed body means
no signal. Any missing source leaves its field null or empty, and a day with
nothing at all is not written.

The subject is the single distinct `subject` in `home_dailies`, read through
`LIFEOS_DAILIES_DATABASE_URL`; zero or several subjects skip the pass. Rows are
upserted on `(subject, entry_date)` through `LIFEOS_DAY_CONTEXT_DATABASE_URL`
(role `home_day_context_writer`), and rows with `entry_date` more than 14 days
before the logical day are deleted in the same transaction.

## Configuration

See [configuration.md](configuration.md#day-context-dailies-suggestions). An
unset `LIFEOS_DAY_CONTEXT_DATABASE_URL` disables the job. Restart the API
after changing it.

## Logging

Only fixed categories are logged (`dailies_db_not_configured`,
`subject_lookup_failed`, `subject_not_unique`, `write_failed`,
`sleep_source_failed`, `pendant_source_failed`, `tick_failed`), never sleep,
signal or entry content. No failure reaches the scheduler or stops the loop.

Coverage: [tests/test_day_context.py](../../tests/test_day_context.py).
