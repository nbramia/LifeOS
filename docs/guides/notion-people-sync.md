# Notion People Sync

**Status:** Complete
**Last Updated:** 2026-11-09
**Audience:** Operator

A one-way sync from LifeOS to a Notion People data source. LifeOS wins: the
script overwrites the Notion contact fields with what LifeOS knows, links each
row to its LifeOS person through a `LifeOS_ID` property, and adds a Notion page
for LifeOS people that have none. Notion is never read back into LifeOS, and no
page is ever archived or deleted.

## What is synced

For every Notion row that maps to a LifeOS person:

| Notion property (type) | From LifeOS | Rule |
|---|---|---|
| `LifeOS_ID` (text) | person id | Written when the cell is empty. |
| `Email` (email) | `emails` | Left alone when it is one of the person's known addresses (case-insensitive). Otherwise set to the first address, replacing any other value. |
| `Phone` (phone) | `phone_numbers` | Left alone when its last 10 digits match a known number. Otherwise set to the first number, in E.164 as stored. |
| `Company` (text) | `company` | Set when the LifeOS value passes the checks below and differs from the Notion value. |
| `Title` (text) | `position` | Same rule as Company. |
| `LinkedIn` (URL) | `linkedin_url`, else the `linkedin` entry of `social_links` | Compared without scheme, `www.`, query, fragment and trailing slash; written as an https URL. |
| `Instagram` (URL) | `social_links` platform `instagram` | Same comparison and write. |
| `Twitter/X` (URL) | `social_links` platform `twitter` or `x` | Same; `x.com` and `twitter.com` count as the same account. |
| `Website` (URL) | `social_links` platform `website`, `web`, `personal`, `personal_website`, `site` or `homepage` | Same. |
| `Last Check-In` (date) | `last_seen` | Set to its local date only when the cell is empty or older. It never moves backwards, and a `last_seen` in the future is skipped. The write replaces the whole date value, so any end date or time in the cell is dropped. |

**Company and Title checks.** A LifeOS value is not written when it has no
letters (`--`, `—`, quotes, digits only), looks like an email address, is longer
than 100 characters, or contains `|` (a headline). A bare `@` used as the word
"at" (`Partnerships @ The Carlyle Group`) is fine. Values are compared after
lowercasing, removing punctuation and quotes, reading `&` as `and` and
collapsing spaces, so formatting-only differences never cause a write. When the
LifeOS value is a proper substring of, or its words are a proper subset of, the
Notion value (Notion `Business Development at iCapital Network`, LifeOS
`Business Development`), Notion holds more detail and is kept. Each value not
written is logged as `SKIPPED` and counted in `skipped_values`.

**Protecting values: fill-only fields.** `--fill-only-fields` (or
`LIFEOS_NOTION_FILL_ONLY_FIELDS`) takes a comma-separated list of the properties
`Email`, `Phone`, `Company`, `Title`, `LinkedIn`, `Instagram`, `Twitter/X`,
`Website` and `Last Check-In`. A listed field is written only into an empty
Notion cell and never overwrites. An unknown name stops the run with exit status
2. Add `Email` and `Phone` to the list to protect addresses and numbers LifeOS
has never seen: every Email or Phone overwrite that replaces such a value is
logged as `DISPLACES a value LifeOS has never seen` (also in a dry run) and
counted in `displaced_values`.

A LifeOS field with no value never blanks a Notion value. Other properties,
including birthdays and relationship fields, are never touched. Each page gets
at most two update requests: the `LifeOS_ID` write, then one request holding only
the fields that changed. Platforms
LifeOS holds that have no Notion property (for example `facebook`) are not
synced. `LinkedIn`, `Instagram`, `Twitter/X`, `Website` and `Dex_ID` are
optional in the Notion schema: when one is absent that field is skipped and the
run logs it once.

An unreadable `Last Check-In` date in Notion is reported as a conflict and left
alone.

## Matching rows to people

Each Notion row is resolved in this order:

1. **`LifeOS_ID`** equals a LifeOS person id. No name check applies. An id that
   matches no person is an **orphan**: it is reported and nothing is written. An
   id that appears on two rows is a **duplicate**: both rows are reported and
   nothing is written for either.
2. **Stored mapping**, `data/notion_people_map.json`. An `--execute` run adds
   every newly resolved row and every page it creates. A dry run never writes it.

   ```json
   {"version": 1, "pages": {"<notion_page_id>": {"lifeos_id": "...", "dex_id": "...", "matched_by": "dex_map", "added": "2026-01-15"}}}
   ```

3. **Dex_ID seed**, `data/notion_people_dexmap.json`, keyed by the row's
   `Dex_ID` text. The file is optional.

   ```json
   {"version": 1, "dex_to_lifeos": {"<dex_id>": "<lifeos_person_id>"}}
   ```

4. **Identifiers**: the row's Email, Phone (last 10 digits) and LinkedIn URL
   against the LifeOS people list. Exactly one person must match; none or
   several leaves the row unmatched.

A match from sources 2 to 4 must pass a name check. The Notion `Full Name` is
cut at the first comma (dropping credentials such as `, CFP`), non-letters are
removed and case is ignored; the same is done to the LifeOS display name and
canonical name. The first word and the last word must both equal those of the
display name or of the canonical name. A LifeOS name with a parenthesised part,
such as `Adrienne (Szarmack) Smith`, is tried three ways: with the parentheses
removed (`Adrienne Smith`), with the parenthesised word replacing the surname
(`Adrienne Szarmack`), and with it replacing the first name (`Szarmack Smith`).
Otherwise the row is unmatched with the reason `name mismatch`, so two people
who share a phone number or one name part are not confused.

If a stored mapping and the seed file disagree about which LifeOS person a
`Dex_ID` belongs to, the run logs a warning naming both ids and skips that row
until the disagreement is fixed. A LifeOS person can be linked to only one row:
a second row that resolves to the same person is reported as unmatched.

An unmatched row whose name matches exactly one still-unlinked LifeOS person
(and no other row matches that person) is reported as `possible match, review by
hand`. It receives no writes, neither field changes nor a `LifeOS_ID`; the match
only prevents a duplicate page for that person. A row whose own email, phone or
LinkedIn match a LifeOS person is resolved by those identifiers (and must pass
the name check) or stays unmatched; it is never linked by name. To link a
reported row, enter the person's id in its `LifeOS_ID` cell or add it to the
Dex_ID seed file.

## New pages

LifeOS people that have no Notion row become new pages each run, closest
relationships first (by `relationship_strength`, descending). A person
qualifies when their category is not `self`, their display name is not empty
and has no `@`, not all of their known emails look like automated senders, and
at least one of these holds: `message_count`,
`email_count` or `meeting_count` is above zero, or `dunbar_circle` is between 1
and 5. A `last_seen` date alone does not qualify, so contacts known only from
the calendar or Outlook are not created. Single-word names qualify.

An email looks like an automated sender when its local part is `noreply`,
`no-reply`, `donotreply`, `do-not-reply`, `notifications`, `notification`,
`newsletter`, `news`, `marketing`, `mailer` or `bounce`, or when its domain has
at least three labels and the first label is `em<digits>` or `mail`, `email`,
`mg`, `bounce` or `notify` with optional digits (so `ann@mail.com` is an
ordinary address). A person with at least one ordinary email, or with no email,
is not affected.

A person is skipped when their id is already some row's `LifeOS_ID`, is already
in the stored mapping (a page created earlier that the query does not show yet),
or is the possible match of a row as described above. A person is also skipped,
with a `SKIPPED CREATE` warning counted in `duplicate_name_skips`, when their
normalised name (credentials after a comma dropped) equals the name of any
Notion row, whether matched, unmatched or an orphan, or the name of another
person planned in the same run. A second, looser rule skips a person (logged as
`possible duplicate, review by hand`, counted in `possible_duplicate_skips`) when
their surname and first initial equal those of any Notion row or of another
person planned in the run. Names are read with "Last, First" order handled,
titles (Dr, Mr, Mrs, Ms, Miss, Prof) dropped and credentials after a comma
dropped, so `Matt Eastman`, `Rogers, Leah` and `Dr. Ann Smith` match
`Matthew Eastman`, `Leah Rogers` and `Ann Smith`, while `Ann Smith` and
`Bob Smith` are different people. These rules stop a second page for a merged
person or for two LifeOS records of one human. Page creation is skipped when `--limit` is
set or when no Notion rows were read.

Creating one page takes these steps:

1. `POST /v1/pages` with the data source as parent and only `Full Name` (the
   display name) and `LifeOS_ID`. When `LIFEOS_NOTION_PERSON_TEMPLATE` is set,
   the request names the template and the machine's time zone; with the setting
   empty the page is created without a template. No page content is sent.
2. Notion applies the template (the About, Interactions, Quick Notes, Gift
   Ideas and Journal & Meetings sections) asynchronously, and there is no job
   endpoint. The script reads the page's block children every 2 seconds for up
   to 20 seconds until content exists.
3. It then writes the remaining properties (Email, Phone, Company, Title,
   LinkedIn, Instagram, Twitter/X, Website, Last Check-In) so a late template
   cannot overwrite them. If the template content never appears, the properties
   are still written, a warning is logged and `template_pending` is counted.

`Pipeline Status` is left to the database default.

If a creation request fails with any error (for example `403
restricted_resource`, which Notion returns when a free workspace reaches its
block limit), page creation stops for the run, the full Notion message is
logged and `creates_stopped` in the stats line holds the reason. The field
updates and `LifeOS_ID` writes of the same run still run, and the run ends with
status `creates_stopped` and exit status 1 so the nightly run does not hide it.
A page creation is never retried after a server error, so one request cannot
create two pages.

Before each creation the script stores a `pending:<person id>` entry in the
stored mapping. It is replaced by the page's entry when Notion answers. When the
outcome is unknown (timeout, transport error or HTTP 5xx) the pending entry
stays: that person is not created again until a later query shows a row with
that `LifeOS_ID`, which resolves the entry. Unresolved entries are logged as
`PENDING create` with the number of runs they have been pending (kept in the
entry's `runs` field), counted in `pending_creates`. From the third run the log
line asks the operator to check Notion. While any pending entry remains after a
run, the run ends with status `pending_creates` and exit status 1. If the page
was never created, delete the `pending:` entry from `data/notion_people_map.json`
to allow a retry; if it was, enter the person's id in the page's `LifeOS_ID`
cell, which resolves the entry on the next run.

## Setup

1. In Notion, open **Settings > Connections > Develop or manage integrations**
   and create an **internal** integration with the Read content, Update content
   and Insert content capabilities. Copy its token.
2. Open the People database, choose the `•••` menu, then **Connections**, and
   add the integration. Without this step Notion answers 404.
3. The data source needs these properties. Required: `Full Name` (title),
   `LifeOS_ID` (text), `Email` (email), `Phone` (phone), `Last Check-In`
   (date), `Company` (text) and `Title` (text). Optional: `LinkedIn`,
   `Instagram`, `Twitter/X`, `Website` (all URL) and `Dex_ID` (text). A property
   that exists with the wrong type stops the run with exit status 2; the Notion
   API calls a text property `rich_text`.
4. Put the token in the LifeOS `.env`:

   ```bash
   LIFEOS_NOTION_TOKEN=<integration token>
   ```

   The data source, template and API version have defaults; see
   [Configuration](configuration.md#notion).
5. Make sure the LifeOS API is running. The script reads people from
   `http://127.0.0.1:8000/api/crm/people`; use `--api-base` for another address.

## First run

1. Dry run. It reads Notion and LifeOS and prints what it would do. It sends
   read requests only (the schema GET and the query POSTs, no writes), and
   writes no mapping file and no ledger:

   ```bash
   ~/.venvs/lifeos/bin/python scripts/sync_notion_people.py
   ```

   The output lists each planned field change (page, field, old value -> new
   value, long values shortened), the Email and Phone overwrites that displace
   an unknown value, skipped Company and Title values, the number of planned
   `LifeOS_ID` writes, the people that would become pages (names, count,
   deferred, duplicate-name skips), and orphans, duplicates, possible matches and
   unmatched rows.
2. Review it, then apply:

   ```bash
   ~/.venvs/lifeos/bin/python scripts/sync_notion_people.py --execute
   ```

   The first run writes `LifeOS_ID` on most rows. Field changes are capped per
   run, so repeat the command until the dry run reports nothing left.
3. Enable the nightly timer, which fires daily at 04:15 local time:

   ```bash
   sudo systemctl enable --now lifeos-notion-people-sync.timer
   ```

   `scripts/setup-systemd.sh` copies the unit files into `/etc/systemd/system`
   but does not enable this timer; the command above does.

`scripts/notion_people_sync_nightly.sh` runs the sync with `--execute
--max-writes 100 --max-creates 25`. When `LIFEOS_NOTION_TOKEN` is empty it logs
`LIFEOS_NOTION_TOKEN not set; skipping` and exits 0, so the timer is harmless
before setup. If the token check itself fails (for example the Python
interpreter is missing or the settings cannot be imported), it logs
`ERROR: could not check LIFEOS_NOTION_TOKEN ...` and exits 1, so a broken
environment is never mistaken for "no token".

## Options

| Option | Meaning |
|---|---|
| `--execute` | Apply the changes. Without it nothing is written. |
| `--max-writes N` | Cap on page updates for field changes per run (default 100). |
| `--max-id-writes N` | Cap on `LifeOS_ID` writes per run (default 300). |
| `--max-creates N` | Cap on new pages per run (default 25). |
| `--fill-only-fields LIST` | Comma-separated properties that only fill empty cells (default `LIFEOS_NOTION_FILL_ONLY_FIELDS`). |
| `--api-base URL` | LifeOS API base URL (default `http://127.0.0.1:8000`). |
| `--data-source-id ID` | Notion data source to sync, overriding `LIFEOS_NOTION_PEOPLE_DATA_SOURCE`. |
| `--map PATH` | Stored mapping file (default `data/notion_people_map.json`). |
| `--dexmap PATH` | Dex_ID seed file (default `data/notion_people_dexmap.json`). |
| `--ledger PATH` | Change ledger (default `data/notion_sync_ledger.jsonl`). |
| `--limit N` | Read at most N Notion rows. Page creation is skipped. |
| `--log-file PATH` / `--no-file-log` | Choose the log file, or log to stdout only. |

Pages are processed in name order. A page over `--max-writes` still receives its
`LifeOS_ID` while `--max-id-writes` allows it, and the reverse. The
`LifeOS_ID` write is always its own request, sent before the field changes, so a
field value Notion rejects cannot block the link. What the caps hold back is logged as
`DEFERRED` and picked up by the next run.

## Guardrails

- Dry run by default; `--execute` is required to write.
- If fewer than 50% of the Notion rows match a LifeOS person, the run aborts
  with exit status 3 and writes nothing. Orphans, duplicates and rows that
  resolve to nothing count as unmatched.
- Repeat runs are safe: once everything is applied, the next run finds nothing
  to change or create.
- Only the changed properties are sent, paced at roughly three requests per
  second counting every request, including template polling. HTTP 409, 429
  (honouring `Retry-After`) and 5xx responses are retried a few times.
- The token is never printed or logged; it is masked in any error text. Every
  Notion 4xx error is logged with Notion's own code and message.

## Ledger, restoring a value, undoing a create

An `--execute` run appends to the ledger, and flushes each line to disk, around
every request. Before a request it writes an `intent` line holding the old
value; after the response it writes a `result` line with `status` `ok`,
`failed` or (for a page creation with an unknown outcome) `unknown`. If a line
cannot be written before a request, that request is not sent and the run stops
with exit status 2.

```json
{"ts": "2026-11-09T04:15:02+00:00", "run_id": "3f9c0a1b2c4d", "phase": "intent", "action": "update", "page_id": "<notion page id>", "lifeos_id": "<person id>", "field": "Company", "old": "Old Co", "new": "Acme", "old_full": [{"plain_text": "Old Co", "annotations": {"bold": true}, "href": null}]}
```

`action` is `update` for a field change, `id_write` for a `LifeOS_ID` write and
`create` for a new page (field `page`) and for the properties written to it.
`old` is the plain old value (`null` when the cell was empty) and `old_full` the
complete old Notion value: the whole `rich_text` array (annotations and links)
for text properties and the whole date object (`start`, `end`, `time_zone`) for
`Last Check-In`. A dry run never writes the ledger.

To restore a value by hand, find the `intent` line for the page and field
(search for the `page_id` and `field`) whose `result` is `ok`, open that page in
Notion and put the `old` value back, using `old_full` for formatting, links or
a date range. For an empty `old`, clear the property. After restoring, change
the LifeOS value as well, or the next run writes the LifeOS value again (or add
the field to `--fill-only-fields`).

To undo a created page, archive the page in Notion. The page's entry in
`data/notion_people_map.json` (`matched_by` is `created`) then keeps the sync
from creating that person again. Delete that entry from the file to let the
person be created again on a later run.

## Reading the output

The last line is always `SYNC_STATS:` followed by a JSON object, on every exit
path including errors. Its keys are `mode`, `status`, `exit_code`, `rows`,
`matched`, `unmatched`, `orphans`, `duplicates`, `changes` (pages with field
changes), `writes` (pages updated), `id_writes`, `creates`, `template_pending`,
`failed`, `conflicts`, `possible_matches`, `displaced_values`,
`skipped_values`, `duplicate_name_skips`, `possible_duplicate_skips`,
`pending_creates`, `deferred`,
`deferred_id_writes`, `planned_id_writes`, `planned_creates`, `deferred_creates`,
`creates_stopped`, `updates_stopped`, `new_mappings`, `fill_only`, `max_writes`,
`max_id_writes`, `max_creates` and `ledger_path` (empty in a dry run). When a run stops midway, the counts show how far it got. `status` is one
of `ok`, `write_failures`, `creates_stopped`, `pending_creates`, `config_error`,
`mapping_abort` or `error`. The same
detail goes to `logs/notion-people-sync.log`.

- `WOULD UPDATE` (dry run) and `UPDATED` lines name the row, the field and the
  old and new values.
- `UNMATCHED` lines give the reason: no identifiers to match on, no matching
  person, an ambiguous match, a name mismatch, an orphan or a duplicate
  `LifeOS_ID`. Fill in the row's Email, Phone or LinkedIn, add it to the Dex_ID
  seed file, or fix its `LifeOS_ID`.
- `DEFERRED` lines are changes held back by a cap.
- `POSSIBLE MATCH, review by hand` lines name a Notion row and a LifeOS person
  that look alike by name only; nothing is written for them.
- `PENDING create` lines name a person whose page creation had an unknown
  outcome.
- `FAILED` lines are single page updates Notion rejected. The run continues and
  exits with status 1.

Exit status:

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Some page updates failed (a 403 on an update stops the remaining updates), page creation stopped on an error, or a page creation has an unresolved `pending:` entry; the rest was applied. |
| `2` | No token, the LifeOS API is unreachable or returned a truncated people list, Notion access or data source property problem, an unreadable response or file, an unwritable ledger, or an unreadable stored mapping file during `--execute`. Nothing further is written. |
| `3` | Aborted because the mapping looks broken (under 50% of rows matched). |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `LIFEOS_NOTION_TOKEN is not set` | Add the token to `.env` (see setup). |
| `HTTP 401` | Notion rejected the token. Copy it again from the integration page. |
| `HTTP 403` or `HTTP 404` | The data source is not shared with the integration, the integration lacks a capability, or `LIFEOS_NOTION_PEOPLE_DATA_SOURCE` is wrong. Add the integration under the database's `•••` menu > Connections. |
| `Notion property 'X' has type ..., expected ...` or `has no property 'X'` | The data source lacks a property the sync needs, or it has a different type. Fix the property in Notion. |
| `Page creation stopped for this run` | Notion refused a page creation; the logged message says why (for example the free-plan block limit). Updates still ran. |
| `PENDING create for LifeOS ...` | A page creation had an unknown outcome. The person is not created again until a row with that `LifeOS_ID` appears. Check Notion; if there is no page, delete the `pending:` entry from the stored mapping. |
| `Template content ... has not appeared` | Notion had not applied the template within 20 seconds. The properties were written anyway. |
| `Could not read people from the LifeOS API` | The API is not running or `--api-base` is wrong. |
| `returned a truncated people list` | The LifeOS API did not return every person. Nothing is synced until it does. |
| `Stored mapping ... is unreadable` | The stored mapping file is damaged. Fix it or move it aside; a dry run continues with a warning, `--execute` stops. |
| `ABORTED: only N of M Notion rows matched` | Fewer than half the rows matched. Check the Dex_ID seed file and that the LifeOS people list is complete. |

## Related Documents

### Guides
- [Configuration](configuration.md#notion) — `LIFEOS_NOTION_TOKEN`, data source, template and API version settings
- [Scripts Reference](scripts.md) — Other operator scripts

### Code References
- [`scripts/sync_notion_people.py`](../../scripts/sync_notion_people.py) — Sync script
- [`scripts/notion_people_sync_nightly.sh`](../../scripts/notion_people_sync_nightly.sh) — Nightly wrapper
- [`config/systemd/lifeos-notion-people-sync.timer`](../../config/systemd/lifeos-notion-people-sync.timer) — Daily 04:15 timer
