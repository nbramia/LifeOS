# Notion People Sync

**Status:** Complete
**Last Updated:** 2026-11-09
**Audience:** Operator

A one-way sync from LifeOS to a Notion People database. It fills in a Notion
row's Email, Phone and Last Check-In from the matching LifeOS person, so the
Notion database keeps up with what LifeOS has learned. Notion is never read
back into LifeOS.

## What is synced

For every Notion row that maps to a LifeOS person:

| Notion property | Rule |
|---|---|
| `Email` | Set to the person's first LifeOS email when the Notion cell is empty. A filled cell is never overwritten. |
| `Phone` | Set to the person's first LifeOS phone number (E.164, as stored) when the cell is empty. A filled cell is never overwritten. |
| `Last Check-In` | Set to the local date of the person's LifeOS `last_seen` when the cell is empty or holds an older date. It never moves backwards, and a `last_seen` in the future is skipped. The date is taken in the machine's local time zone. The write replaces the whole date value, so any end date or time in the cell is dropped; this suits a single check-in date. |

Never synced: any other property, birthdays, page creation, archiving or
deletion, and database schema changes. The sync only reads the `Full Name`,
`Dex_ID`, `Email`, `Phone`, `LinkedIn` and `Last Check-In` properties.

## One-time setup

1. In Notion, open **Settings > Connections > Develop or manage integrations**
   and create an **internal** integration. Give it the Read content and Update
   content capabilities. Copy its token.
2. Open the People database, choose the `•••` menu, then **Connections**, and
   add the integration. Without this step Notion answers 404.
3. Put the token in the LifeOS `.env`:

   ```bash
   LIFEOS_NOTION_TOKEN=<integration token>
   ```

   To sync a database other than the default, also set
   `LIFEOS_NOTION_PEOPLE_DB` to its ID. See
   [Configuration](configuration.md#notion).
4. Make sure the LifeOS API is running. The script reads people from
   `http://127.0.0.1:8000/api/crm/people`; use `--api-base` for another address.

## Running it

Dry run (the default). It reads Notion and LifeOS, prints what it would change,
and writes nothing, including the mapping file:

```bash
~/.venvs/lifeos/bin/python scripts/sync_notion_people.py
```

Apply the changes:

```bash
~/.venvs/lifeos/bin/python scripts/sync_notion_people.py --execute
```

The last line of output is always `SYNC_STATS:` followed by a JSON object, on
every exit path including errors. Its keys are `mode`, `status`, `exit_code`,
`rows`, `matched`, `unmatched`, `changes`, `writes`, `failed`, `conflicts`,
`deferred`, `new_mappings` and `max_writes`. When a run stops midway, the
counts show how far it got, including `writes` already made. `status` is one of
`ok`, `write_failures`, `config_error`, `mapping_abort` or `error`. The same detail goes to
`logs/notion-people-sync.log`. The token is never printed or logged; it is
masked in any error text.

| Option | Meaning |
|---|---|
| `--execute` | Apply the changes. Without it nothing is written. |
| `--max-writes N` | Cap on page updates per run (default 50). |
| `--api-base URL` | LifeOS API base URL (default `http://127.0.0.1:8000`). |
| `--database-id ID` | Notion database to sync, overriding `LIFEOS_NOTION_PEOPLE_DB`. |
| `--map PATH` | Stored mapping file (default `data/notion_people_map.json`). |
| `--dexmap PATH` | Dex_ID seed file (default `data/notion_people_dexmap.json`). |
| `--limit N` | Read at most N Notion rows, useful for a first trial. |
| `--log-file PATH` / `--no-file-log` | Choose the log file, or log to stdout only. |

Exit status:

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Some page updates failed; the rest were applied. |
| `2` | No token, the LifeOS API is unreachable or returned a truncated people list, Notion access or database property problem, an unreadable response or file, or an unreadable stored mapping file during `--execute`. Nothing further is written. |
| `3` | Aborted because the mapping looks broken (under 50% of rows matched). |

Before reading rows, the script fetches the database definition and checks
that `Full Name` (title), `Dex_ID` (text), `Email` (email), `Phone` (phone) and
`Last Check-In` (date) exist with those types; Notion's API calls a text
property `rich_text`. A missing or differently typed property stops the run
with exit status 2, naming the property and the type found. `LinkedIn` (URL) is
optional: without it, rows are matched on the other identifiers. If the
property exists it must be of type URL, otherwise the preflight check stops the
run with exit status 2.

## Nightly timer

`scripts/notion_people_sync_nightly.sh` runs the sync with `--execute
--max-writes 50`. When `LIFEOS_NOTION_TOKEN` is empty it logs
`LIFEOS_NOTION_TOKEN not set; skipping` and exits 0, so the timer is harmless
before setup. If the token check itself fails (for example the Python
interpreter is missing or the settings cannot be imported), it logs
`ERROR: could not check LIFEOS_NOTION_TOKEN ...` and exits 1, so a broken
environment is never mistaken for "no token". Enable the timer, which fires daily at 04:15 local time:

```bash
sudo systemctl enable --now lifeos-notion-people-sync.timer
```

`scripts/setup-systemd.sh` copies the unit files into `/etc/systemd/system`
but does not enable this timer; the command above does.

## Guardrails

- Dry run by default; `--execute` is required to write.
- `--max-writes` caps page updates per run. Pages are processed in name order
  and the remainder is logged as `DEFERRED` and picked up by the next run.
- If fewer than 50% of the Notion rows match a LifeOS person, the run aborts
  with exit status 3 and writes nothing, since a mapping that poor means
  something is wrong.
- Repeat runs are safe: once a value is filled in, the next run finds nothing
  to change.
- Only the changed properties are sent, at roughly three requests per second.
  HTTP 429 (honouring `Retry-After`) and 5xx responses are retried a few times.

## How rows are matched

Each Notion row is resolved to a LifeOS person in this order:

1. **Stored mapping**, `data/notion_people_map.json`. An `--execute` run adds
   every newly resolved row. A dry run never writes it.

   ```json
   {"version": 1, "pages": {"<notion_page_id>": {"lifeos_id": "...", "dex_id": "...", "matched_by": "dex_map", "added": "2026-01-15"}}}
   ```

2. **Dex_ID seed**, `data/notion_people_dexmap.json`, keyed by the row's
   `Dex_ID` text. The file is optional.

   ```json
   {"version": 1, "dex_to_lifeos": {"<dex_id>": "<lifeos_person_id>"}}
   ```

3. **Identifiers**: the row's Email, Phone (last 10 digits) and LinkedIn URL
   (case, scheme, `www.`, query, fragment and trailing slash ignored) against
   the LifeOS people list. Exactly one person must match; none or several
   leaves the row unmatched.

Every match, from any of the three sources, must also pass a name check. The
Notion `Full Name` is cut at the first comma (dropping credentials such as
`, CFP`), non-letters are removed and case is ignored; the same is done to the
LifeOS display name and canonical name. The first word and the last word must
both equal those of the display name or of the canonical name. A LifeOS name
with a parenthesised part, such as `Adrienne (Szarmack) Smith`, is tried three
ways: with the parentheses removed (`Adrienne Smith`), with the parenthesised
word replacing the surname (`Adrienne Szarmack`), and with it replacing the
first name (`Szarmack Smith`). Otherwise the row
is unmatched with the reason `name mismatch`, so two people who share a phone
number or one name part are not confused.

If a stored mapping and the seed file disagree about which LifeOS person a
`Dex_ID` belongs to, the run logs a warning naming both ids and skips that row
until the disagreement is fixed.

A stored mapping file that exists but cannot be parsed stops an `--execute` run
with exit status 2 and nothing is written; a dry run only warns. Fix or move
the file to continue.

## Reading the output

- `WOULD UPDATE` (dry run) or `UPDATED` lines name the row and the properties
  written.
- `UNMATCHED` lines give the reason: no identifiers to match on, no matching
  person, an ambiguous match, or a name mismatch. Add the row to the Dex_ID
  seed file, or fill in its Email, Phone or LinkedIn, to match it.
- `CONFLICT` lines mean Notion already holds an Email or Phone that LifeOS does
  not know about (Phone is compared on its last 10 digits). Nothing is written
  for that property. Decide which side is right and correct it there. A
  `Last Check-In` cell that does not hold a readable date is also reported as a
  conflict and left alone.
- `DEFERRED` lines are updates held back by `--max-writes`.
- `FAILED` lines are single page updates Notion rejected. The run continues
  and exits with status 1.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `LIFEOS_NOTION_TOKEN is not set` | Add the token to `.env` (see setup). |
| `HTTP 401` | Notion rejected the token. Copy it again from the integration page. |
| `HTTP 403` or `HTTP 404` | The People database is not shared with the integration, or `LIFEOS_NOTION_PEOPLE_DB` is wrong. Add the integration under the database's `•••` menu > Connections. |
| `Could not read people from the LifeOS API` | The API is not running or `--api-base` is wrong. |
| `ABORTED: only N of M Notion rows matched` | Fewer than half the rows matched. Check that the Dex_ID seed file is present and that the LifeOS people list is complete. |
| `Notion property 'X' has type ..., expected ...` or `has no property 'X'` | The People database does not have the property the sync needs, or it has a different type. Fix the property in Notion. |
| `Stored mapping ... is unreadable` | The stored mapping file is damaged. Fix it or move it aside; a dry run continues with a warning, `--execute` stops. |
| `returned a truncated people list` | The LifeOS API did not return every person. Nothing is synced until it does. |
| Many `UNMATCHED` rows | Rows lacking Email, Phone and LinkedIn cannot be matched by identifiers; use the Dex_ID seed file. |

## Related Documents

### Guides
- [Configuration](configuration.md#notion) — `LIFEOS_NOTION_TOKEN` and `LIFEOS_NOTION_PEOPLE_DB`
- [Scripts Reference](scripts.md) — Other operator scripts

### Code References
- [`scripts/sync_notion_people.py`](../../scripts/sync_notion_people.py) — Sync script
- [`scripts/notion_people_sync_nightly.sh`](../../scripts/notion_people_sync_nightly.sh) — Nightly wrapper
- [`config/systemd/lifeos-notion-people-sync.timer`](../../config/systemd/lifeos-notion-people-sync.timer) — Daily 04:15 timer
