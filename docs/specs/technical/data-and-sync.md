# Data & Sync Architecture

> **Status:** Complete
> **Owner:** Data Pipeline
> **Last Updated:** 2026-11-03

How LifeOS ingests and stores data from multiple sources.

---

## Table of Contents

1. [Data Sources](#data-sources)
2. [Sync Schedule](#sync-schedule)
3. [Data Stores](#data-stores)
4. [Sync Scripts](#sync-scripts)
5. [Configuration](#configuration)
6. [Messaging Source Details](#messaging-source-details)
7. [LinkedIn Profile Scraping](#linkedin-profile-scraping)

---

## Data Sources

### Source Types and Sync Methods

| Source | Sync Method | Data Extracted |
|--------|-------------|----------------|
| Gmail | Google API | From/To/CC, subjects, timestamps, threads |
| Calendar | Google API | Attendees, organizer, titles, times |
| Apple Contacts | Apple Data Agent (export via rsync) | Names, emails, phone numbers, companies |
| Apple Photos | Apple Data Agent (export via rsync) | Face recognition, co-appearances, timestamps |
| Phone Calls | Apple Data Agent (export via rsync) | Numbers, names, duration, direction |
| WhatsApp | wacli CLI | JIDs, names, phone numbers |
| iMessage | Apple Data Agent (export via rsync) | Phone/email, message content, timestamps |
| Slack | Slack API (OAuth) | User profiles, DMs, channels |
| Vault Notes | Obsidian markdown | Name mentions, context paths |
| LinkedIn | CSV Import | Connections, companies, titles |
| LinkedIn Profiles | Browser Scraping | Full profile data (experience, education, skills) |
| Meeting notes (Wispr, Granola) | Vault index job and nightly vault reindex (notes placed by the external `local-processing` watchers) | Meeting transcripts, attendees |

### Example Data Volume

| Metric | Example Count |
|--------|---------------|
| Total People (Canonical) | ~3,500+ |
| Total Source Entities | ~125,000+ |
| Total Interactions | ~165,000+ |
| Gmail (Personal) | ~30,000+ emails |
| Gmail (Work) | ~5,000+ emails |
| Calendar (Personal) | ~1,000 events |
| Calendar (Work) | ~5,000+ events |
| Apple Contacts | ~1,000+ contacts |
| WhatsApp Contacts | ~1,500+ contacts |

---

## Sync Schedule

### Unified Daily Sync (7 Phases)

All data syncing is consolidated into a single daily sync with proper phase ordering (`SYNC_ORDER` in `scripts/run_all_syncs.py`). This ensures downstream processes always have access to fresh upstream data.

```
02:50          Apple Data Agent export (any spare Mac -> Linux server via rsync)
               └─ Exports contacts, phone calls, iMessage, photos, WhatsApp
               └─ scripts/apple_data_agent.sh → scripts/apple_data_export.py (Apple Data Agent Mac)
               └─ scripts/apple_data_import.py (Linux server)
02:30          Pre-sync health check (API server)
03:00          Unified sync starts (via run_all_syncs.py)

               === PHASE 1: Data Collection ===
               Pull fresh data from all external sources
               └─ Gmail (personal + work + work2: sent + received + CC)
               └─ Calendar (personal + work + work2 Google Calendar events)
               └─ LinkedIn (connections CSV export)
               └─ Contacts (Apple Contacts; macOS-only — reports `skipped` on Linux)
               └─ Apple import (contacts, phone, iMessage, photos, WhatsApp from
                 the Apple Data Agent export; Linux only)
               └─ Slack (users + DM and member-channel messages)

               === PHASE 2: Entity Processing ===
               Link source entities to canonical PersonEntity records
               └─ Link Slack (match by email)
               └─ iMessage (create interactions; links its own unlinked
                 messages internally before filtering, so it doesn't need
                 Link iMessage to have run first)
               └─ Link iMessage (retroactive: backfill phone-based links
                 against the latest CRM phone→person mapping)
               └─ Create contact persons (a person-less contact with enough
                 iMessage evidence gets a PersonEntity, linking the contact
                 and its messages to it)
               └─ Link source entities (retroactive linking for all unlinked)
               └─ Photos (sync face recognition to people; macOS-only —
                 reports `skipped` on Linux)

               === PHASE 2b: Stale ID Cleanup ===
               Re-point interactions with stale merged person IDs before
               relationship building
               └─ Repoint stale IDs

               === PHASE 3: Relationship Building ===
               Build relationships using all collected interaction data
               └─ Person stats (full refresh of all PersonEntity counts +
                 timestamps; each sync script also refreshes its own affected
                 stats inline)
               └─ Relationship discovery (populate edge weights)
               └─ Strengths (calculate relationship scores)
               └─ Push birthdays (LifeOS → Apple Contacts; macOS-only —
                 reports `skipped` on Linux)

               === PHASE 4: Vector Store Indexing ===
               Index content with fresh people data available
               └─ Vault reindex (ChromaDB + BM25)
               └─ CRM vectorstore (index CRM people for semantic search)

               === PHASE 5: Content Sync ===
               Pull external content into vault
               └─ Google Docs (configured docs → vault)
               └─ Google Sheets (form responses → vault)
               └─ Monarch Money (monthly, runs on 1st only)

               === PHASE 6: Post-Sync Cleanup ===
               Auto-hide obvious non-human entities
               └─ Entity cleanup (auto-hide non-human entities)

               === PHASE 7: Consistency Verification ===
               Verify cross-store data consistency after all syncs complete
               └─ Consistency verify (check orphans, stale merged IDs, cached
                 counts, vault files that moved or vanished; auto-fixes below
                 a threshold)

~03:16         Unified sync complete
07:00          Post-sync health check (API server)

08:00          Calendar sync (Google Calendar → ChromaDB)
12:00          Calendar sync
15:00          Calendar sync

```

### Phase Dependencies

The 7-phase structure ensures correct data flow:

1. **Data Collection** runs first so all external data is fresh
2. **Entity Processing** links source entities and creates interactions after source data exists
2b. **Stale ID Cleanup** re-points interactions with stale merged person IDs before relationship building consumes them
3. **Relationship Building** computes metrics using linked entities
4. **Vector Store Indexing** indexes content with fresh CRM data available for entity resolution
5. **Content Sync** pulls external content (indexed on next run)
6. **Post-Sync Cleanup** auto-hides obvious non-human entities after all other syncs
7. **Consistency Verification** checks cross-store consistency after everything else has run

**Note:** Apple data (contacts, phone calls, iMessage, photos, WhatsApp) is exported from an Apple Data Agent (any spare Mac with FDA granted) via `scripts/apple_data_agent.sh` at 2:50 AM, before the main pipeline, and imported on Linux by the `apple_import` source. The export runs on that Mac (which has FDA access) and syncs to the Linux server via rsync. Three sources are macOS-only and report `skipped` (not a failure) when the nightly sync runs on the Linux host: `contacts`, `photos`, `push_birthdays`.

**Unconfigured-source clean skip:** a source with no way to authenticate or nothing set up reports `skipped`, not `failed`, so one missing integration never fails the whole run and never repeats as a nightly error. This covers the macOS-only sources above on a Linux host, plus config-gated sources checked at run time: `gmail_personal`/`calendar_personal` when `config/credentials-personal.json` is absent, `monarch_money` when there's no cached session and `MONARCH_EMAIL`/`MONARCH_PASSWORD` are unset, and `google_sheets` when `config/gsheet_sync.yaml` doesn't exist (the gsheet-journal pipeline was never set up). A source that *is* configured but genuinely fails (bad credentials, network, expired session with no fallback) still records `failed` exactly as before — this only short-circuits the "never configured at all" case.

### Process Summary

| Process | Schedule | Reads From | Writes To |
|---------|----------|------------|-----------|
| ChromaDB Server | Continuous (boot) | HTTP requests | Vector data |
| systemd/launchd API Service | Continuous (boot) | All data | API logs |
| Unified Sync | Daily 3:00 AM ET | All sources | All stores |
| Vault Index Job | Every 15 minutes (systemd) | Changed vault notes | ChromaDB, BM25, people stores, `data/vault_index_state.json`, `data/vault_summary_failures.json` |
| Calendar Indexer | 8 AM, 12 PM, 3 PM ET | Google Calendar | ChromaDB (`lifeos_calendar`) |

A new or edited vault note reaches search through the vault index job, normally within 15 minutes of arriving on the server, or through the vault reindex in the unified sync. `POST /api/admin/reindex` runs the same reindex on demand. No process watches the vault for search indexing; the task and scheduler file watchers maintain their own indexes only.

### Vault Index Job

`scripts/vault_index_job.py`, run by `lifeos-vault-index.timer` on Linux. macOS hosts do not install it, and their notes reach search through the nightly sync. It runs outside the API server and shares `IndexerService.index_all` and `data/vault_index_state.json` with the nightly reindex, so neither repeats work the other finished.

- **Order and cap.** Each run indexes up to `LIFEOS_VAULT_INDEX_MAX_FILES` changed notes, newest modification time first, and leaves the rest for later runs. Each run also removes up to `LIFEOS_VAULT_INDEX_MAX_DELETIONS` deleted notes from both indexes, saving progress before it starts indexing, so a folder move spreads over several runs instead of repeating. A note that fails to index or cannot be read stays unrecorded and is retried. An unreadable note keeps its place at the head of the order, taking one slot of the cap per run until it is fixed.
- **Summaries.** The job generates no LLM summaries. Each note it indexes joins `data/vault_summary_failures.json`, unless its summary tier is SKIP or it is too short to summarize, and the `vault_summaries` source generates the summary. Tags and generated index pages stay nightly, and the task index stays with the task file watcher.
- **Locks.** Every vault indexer takes a `flock` on `~/.lifeos/vault_index.lock`, which the kernel releases when the holder exits: the job, `sync_vault_reindex.py`, and both `POST /api/admin/reindex` paths. A job that finds it held exits 0 and tries again next tick. `POST /api/admin/reindex/sync` runs on the event loop, so it returns `status: "busy"` rather than waiting. The nightly reindex and the queued API reindex wait, up to `LIFEOS_VAULT_INDEX_LOCK_WAIT_SECONDS`. The sync also waits for a running job before reading free GPU memory, so the job's loaded model cannot make it stop the LLM. The job's `TimeoutStartSec` is shorter than that wait, so a timeout means something is wrong beyond a slow run: the reindex then exits non-zero without indexing, the sync records the failure, and the next night runs as normal. The job also holds `~/.lifeos/sync.lock` shared, as a sync does, so auto-deploy defers around it, and exits 0 if a deploy restart holds that lock.
- **Memory.** When there are notes to index and free GPU memory is below `LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB`, or free system RAM is below the embedding minimum, the job exits 0 and logs why. It never stops the LLM service. A host with no reading, such as one without an AMD GPU, is not held back. A run that falls back to CPU embeddings says so in its log, with its note count and duration.
- **Idle runs.** The scan compares file modification times and tag rows with the saved state and needs neither ChromaDB nor BM25, so a run with nothing to do exits without loading the embedding model. A vault with no notes at all is treated as unmounted, and nothing is removed.
- **Tags.** A tag lookup that fails, because the store is locked or unreadable, leaves a note's tag state as recorded instead of making it look changed. This applies to the job and the reindex alike, and it is logged once per run. A note edited during the outage is indexed without its tag phrase, so it records no tag key and is indexed once more when lookups work again.
- **Failures.** A failure goes to `logs/vault-index.log` and the unit's status, and the next tick tries again. The job sends no alert and records no sync failure.

`--dry-run`, the default, reports what a run would index and writes nothing. It names the index state and tag database it read, because the state path follows the working directory and the tag database follows `LIFEOS_CHROMA_PATH`, which defaults to the checkout holding the code. Run it from another checkout with the working directory and `LIFEOS_CHROMA_PATH` both pointing at the live data. If the tag database cannot be read, it counts by modification time alone and says so. `--execute` indexes.

### Failure Notifications

Configure `LIFEOS_ALERT_EMAIL` in `.env` to receive notifications when sync steps fail.

The run also files a [Human queue](../../guides/human-queue.md) card keyed
`sync:<source>` for each genuinely failed source (not a clean skip),
auto-resolved by the next successful run of that source.

---

## Data Stores

### Store Locations

| Store | Location | Purpose | Updated By |
|-------|----------|---------|------------|
| ChromaDB | `data/chromadb/` | Vector embeddings | Vault index job, nightly reindex |
| ChromaDB (Slack) | `lifeos_slack` collection | Slack message vectors | Nightly Slack sync |
| BM25 Index | `data/chromadb/bm25_index.db` | Keyword search | Vault index job, nightly reindex |
| Vault | Configured via `LIFEOS_VAULT_PATH` | Primary knowledge base | User, external classifier, GDoc Sync |
| PersonEntity | `data/crm.db` (person_entities table) | Resolved identities | People v2 sync, iMessage sync |
| SourceEntity | `data/crm.db` | Raw observations | All sync scripts |
| Interactions | `data/interactions.db` | Interactions per person | People v2 sync, Slack sync |
| Relationships | `data/crm.db` | Person-to-person edges | Relationship discovery |
| Tone Analysis | `data/crm.db` (`tone_analysis_results` table) | Cached per-person/month relationship tone scores | Relationship Dashboard tone refresh (see [crm-analytics.md § Tone Analysis APIs](../product/crm-analytics.md#tone-analysis-apis)) |
| iMessage | `data/imessage.db` | Message export cache | iMessage sync |
| Gmail Skip Cache | `data/gmail_skip_cache.db` | Message IDs already judged marketing, per account | Gmail sync |

`tone_analysis_results` schema (`api/services/tone_analysis_store.py`):

| Column | Type | Notes |
|--------|------|-------|
| `person_id` | TEXT | Part of the composite primary key |
| `period_key` | TEXT | `YYYY-MM`; part of the composite primary key |
| `interaction_count` | INTEGER | Stored count used for the freshness check (a month is stale once its live count diverges) |
| `result` | TEXT | JSON-encoded scores for the period |
| `model` | TEXT | LLM model that produced `result` |
| `created_at` / `updated_at` | TEXT | Timestamps |

### Meeting Source Type

Meeting rows in `interactions` and `source_entities` carry source type `meeting` and a `provider` read from the note's frontmatter. A store initializing runs `api/services/meeting_source.py`, which relabels any `granola` rows and rewrites person `sources` lists:

- **One transaction per database.** `crm.db` holds source entities and person entities, so both change together. A failure rolls back, logs `MEETING SOURCE MIGRATION FAILED`, sends one Telegram alert per database per day (claimed by a `.meeting-migration-alert` file beside the database), and is retried by the next store initialization. Long-lived processes hold their stores for life, so the API retries only when it restarts.
- **Concurrency.** The transaction is `BEGIN IMMEDIATE`, so concurrent initializers serialize and the later one finds nothing to do. Under WAL, readers are not blocked. Person updates change only `sources` under that lock, and `PersonEntityStore` normalizes `sources` on every read and write.
- **Providers.** Each distinct note's frontmatter is read once, before the transaction opens. `wispr_id` gives `wispr`; anything else, including a missing note, gives `granola`. Paths stored by another machine are also tried under this vault by the vault folder's name. If the vault is missing, empty, or contains none of 20 or more notes, rows are relabelled with a null provider, listed in `meeting_provider_backfill`, and completed at a later initialization. This logs a warning and sends no alert. A row written between the file read and the lock takes the same route, with a log message that blames the vault.
- **Collisions.** A migrated row keeps its id. A `meeting` row with the same `source_id` and the same person link is a duplicate and is removed, and a `person_facts.source_interaction_id` naming it keeps a dangling attribution. When the links differ, both rows stay and one has `#dup-<id>` appended to its `source_id`, which path checks skip: the `meeting` row, unless it holds the stronger human decision (confirmed over rejected over automatic), so the decision stays on the `source_id` the indexer looks up.
- **Cost once complete.** An indexed existence check per row table and a `sqlite_master` lookup, with no scanning or writing. The existence check also catches a `granola` row written by a process that hasn't restarted yet.

Reads treat `granola` as `meeting` (row types, filters, and counts), so meeting data stays visible while a store awaits migration. `meeting_source_migration` in `/api/crm/data-health` reports the raw counts per store: legacy rows, null providers, and rows awaiting a provider.

Any database a store opens migrates, including a `.bak-*` copy opened by path. Adding the `provider` column runs outside the failure handling above, so another writer holding the lock past 30 seconds on a database's first initialization makes that store construction raise. The column is needed for every write, and the store singletons retry on their next use.

`scripts/migrate_meeting_source_type.py` runs the migration or `--rollback` against explicitly named database files, and reports counts per source type and per person before and after. Rollback belongs before reverting the code, with services stopped, because a store initializing on this code relabels the rows again.

### Backups

`interactions.db` and `crm.db` are snapshotted to `LIFEOS_BACKUP_PATH` before
the nightly sync runs, using SQLite's online backup API so a live WAL-mode
database is captured consistently.

Retention keeps `LIFEOS_BACKUP_KEEP` snapshots per database (default 2), and is
deliberately conservative about when it deletes:

- **Pruning is deferred to the end of the run**, and skipped entirely if any
  source failed. A snapshot is only known to be a usable rollback point once
  the run it protects has finished cleanly, so a string of failing nights
  cannot rotate away the last good copy.
- **The newest snapshot must pass an integrity check** before anything older is
  removed. A snapshot that fails verification is deleted immediately and prunes
  nothing.
- Files that don't match the `<db>.<timestamp>.backup` convention are ignored,
  so hand-named copies kept for safekeeping survive.
| Task Index | `data/task_index.json` | Parsed task cache | Task CRUD, file watcher |
| Scheduler | `LifeOS/Scheduler/Inbox.md` (source) + `data/scheduler_index.json` (cache) | Schedules (trigger + action) | Scheduler store, file watcher |
| Memories | `~/.lifeos/memories.json` | User-saved memories | Memory CRUD |
| Job Queue | `data/jobs.db` | Background job tracking | Job queue worker |

---

## Sync Scripts

All sync scripts in `scripts/` follow the pattern:
- Dry run by default (shows what would change)
- Use `--execute` flag to apply changes

### Phase 1: Data Collection

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_gmail_calendar_interactions.py` | Sync emails (sent+received+CC) and calendar | Gmail/Calendar API |
| `sync_linkedin.py` | Sync LinkedIn connections | CSV export |
| `sync_apple_contacts.py` | Sync Apple Contacts (macOS-only — `skipped` on Linux) | Apple Data Agent export |
| `apple_data_import.py` | Import Apple ecosystem data (contacts, phone, iMessage, photos, WhatsApp) from the Apple Data Agent export | Apple Data Agent export (Linux only) |
| `sync_phone_calls.py` | Sync phone calls (not in nightly `SYNC_ORDER` — runs via the separate FDA cron on macOS) | Apple Data Agent export |
| `sync_slack.py` | Sync Slack users, DMs, and member channels | Slack API |

**Gmail fetch cost.** A message is judged marketing only after it is fetched, and
a discarded message writes no interaction row — so the "already seen" set derived
from `interactions` cannot remember it, and it would be re-fetched every night for
its whole look-back window. Two mechanisms keep that bounded:

- **Batched fetches.** `GmailService.get_messages_batch()` carries many message
  fetches per HTTP round-trip. Batches are paced per message and back off on
  quota errors by retrying the whole chunk — retrying each message individually
  once the quota is exhausted only deepens the hole.
- **Skip cache.** `data/gmail_skip_cache.db` records the IDs each account has
  already judged to be marketing, so they are fetched once rather than nightly.
  Entries are keyed by `(account, message_id)` — Gmail IDs are unique only within
  a mailbox — and pruned well past the look-back window.

A cached skip is not re-evaluated while its entry lives. To apply changed rules
from `config/marketing_patterns` to already-skipped mail, delete the database
file; the next run re-fetches and re-evaluates the whole window.

### Apple Data Agent

| Script | Purpose | Runs On |
|--------|---------|---------|
| `apple_data_export.py` | Export Apple data (contacts, phone, iMessage, photos, WhatsApp) | Apple Data Agent Mac |
| `apple_data_import.py` | Import Apple data exports into LifeOS | Linux server |
| `apple_data_agent.sh` | Orchestrate export + rsync + import | Apple Data Agent Mac (cron) |

WhatsApp data flows through the same Apple Data Agent → Linux pipeline. The Mac runs `wacli` (openclaw/tap/wacli) which reads the WhatsApp Desktop app's local SQLite database; `apple_data_export.export_whatsapp` dumps contacts, messages, group memberships and the LID-to-phone map to `whatsapp.json`; `apple_data_import.import_whatsapp` calls into `api/services/whatsapp.py` to create SourceEntity and Interaction records. A non-zero `wacli sync` exit (including a "Client outdated (405)" protocol rejection, distinct from an auth failure) marks the export `status: "error"` rather than silently exporting stale data as `ok`.

### Phase 2: Entity Processing

Runs in this order (see [iMessage Sync Ordering](#imessage-sync-ordering) below): `link_slack` → `imessage` → `link_imessage` → `create_contact_persons` → `link_source_entities` → `photos`.

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `link_slack_entities.py` | Link Slack users to people by email | `data/crm.db` |
| `sync_imessage_interactions.py` | Create interactions from iMessage DB; links its own unlinked messages internally before filtering | Apple Data Agent export |
| `link_imessage_entities.py` | Retroactive: backfill phone-based links against the latest CRM phone→person mapping | `data/imessage.db` |
| `create_contact_persons.py` | For an unlinked `contacts` SourceEntity whose normalized phone or lowercased email appears as an iMessage handle at least `LIFEOS_CONTACT_PERSON_MIN_MESSAGES` times (default 5), create a person and link both the contact and the matching messages to it — closes the gap where a contact who only ever texts, with no WhatsApp or email correspondence, would otherwise never get a person entity | `data/crm.db`, `data/imessage.db` |
| `link_source_entities.py` | Retroactive linking for all unlinked entities | `data/crm.db` |
| `sync_photos.py` | Sync Photos face recognition to people (macOS-only — `skipped` on Linux) | Apple Data Agent export |

### Phase 2b: Stale ID Cleanup

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_repoint_stale_ids.py` | Re-point interactions with stale merged person IDs to canonical IDs, before relationship building | `data/interactions.db` |

### Phase 3: Relationship Building

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_person_stats.py` | Full refresh of all PersonEntity counts and timestamps | `data/interactions.db` |
| `sync_relationship_discovery.py` | Discover relationships and populate edge weights | All interactions |
| `sync_strengths.py` | Hide new services and newsletters, then recalculate relationship strengths | `data/crm.db` |
| `push_birthdays_to_contacts.py` | Push LifeOS birthdays to Apple Contacts (macOS-only — `skipped` on Linux) | `data/crm.db` |

### Phase 4: Vector Store Indexing

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_vault_reindex.py` | Reindex vault to ChromaDB + BM25; waits for the vault index lock | Vault files |
| `sync_vault_tag.py` | Tag changed vault notes into the tag store (`data/vault_tags.db`) | Vault files |
| `sync_crm_to_vectorstore.py` | Make the people index hold exactly the eligible CRM people | `data/crm.db` |

**People index.** `sync_crm_to_vectorstore.py` leaves the `lifeos_people` collection holding exactly the eligible people: not hidden, with `relationship_strength >= MIN_STRENGTH_FOR_INDEX` or at least `MIN_INTERACTIONS_FOR_INDEX` interactions in 90 days. Everyone else in the collection, whether they dropped below the cutoffs, were hidden, or were merged away, is removed. Removal is held back, and reported as a logged error, a `sync_errors` row (`removal_skipped`), and a Telegram summary line, when the index cannot be listed, no one is eligible, or it would remove more than half the entries the index held when the run started; a failure to load the people stops the run before any removal. `--allow-large-removal` lifts only the fraction limit. The run's `SYNC_STATS` line carries `people_removed`, `people_stale` and `removal_skipped`, and a held-back removal counts as one error. Without `--execute` the script reports the same plan and writes nothing.

**Reading the people index.** An entry's only reliable person field is its `person:<id>` path, so `search_people_semantic` takes identity from the path and checks every hit against the live store: anyone hidden or merged since the last sync is dropped, and strength and category filters read the live person. `find_people_by_description` uses it for `person_info(lookup)` and for `GET /api/people/search`, both only when the term says it wants whoever fits (`asks_who_fits`), so a name that was not found stays a miss. It takes the top 100 hits, keeps only profiles that say something about the person (company, tags, notes, aliases, facts), ranks a profile sharing a word with the description first, and requires any other profile to clear `DESCRIPTION_MIN_SIMILARITY`. Most entries hold only a name and channel activity, so they can't match a description.

**What the reindex skips.** Indexing and tagging leave out every file under a `_`-prefixed folder, which holds working output rather than notes (a `_`-prefixed file in an ordinary folder is still a note). An entry indexed earlier drops out through the deleted-file path. A bulk-content note stays searchable but creates no people, links, interactions, or source entities, because a page written about someone is not contact with them. A note is bulk content when it sits under a folder in `LIFEOS_VAULT_NO_PEOPLE_PATHS`, or its frontmatter has an explicit `people: false` or a `generated:` key. A blank `people:` is an empty property and doesn't count. `source: llm` alone doesn't decide it: daily notes and prep written with a model are about real contact. See [Mentions in Notes](../product/entity-resolution.md#mentions-in-notes) for how the remaining notes resolve their mentions.

**Time budget.** The nightly `vault_reindex` runs under `LIFEOS_VAULT_INDEX_BUDGET_MINUTES`, so a large first load can't hold up later phases. When the budget runs out between files, the run saves its place, refreshes stats for the people it touched, and exits successfully, reporting `files_remaining` in `SYNC_STATS` and the log. Unprocessed files and pending removals stay out of the saved state, so the next run picks them up. A budgeted run indexes ordinary notes before bulk content, newest first. A manual `sync_vault_reindex.py`, including `--force`, is unbudgeted unless given `--budget-minutes`.

**One interaction per note.** `interactions` is unique on `(source_type, source_id)` and a vault interaction's `source_id` is the note path, so a note holds one interaction however many people it names. It goes to the first person who resolves, full names first, and follows the note on reindexing: a holder the note doesn't resolve to gives it up to whoever comes first, and it is removed if no one does. A holder whose link a human confirmed keeps it. Source entities are per mention.

**Vault tagging.** Before the reindex, `sync_vault_tag.py` walks every vault `.md` file (excluding `.obsidian/`, `.trash/`, `Wiki/Vault Map/` and `_`-prefixed folders), tags only notes whose content hash or taxonomy vocabulary version differs from the stored row (`VaultTagStore.needs_tagging`), and prunes rows for notes absent from the vault (`delete_missing`). It is not an embedding source: it uses no GPU and does not pause the local LLM, and it never runs in the file watcher. Because it reads the markdown directly and runs immediately before `vault_reindex`, a new note is tagged the same night and the reindex consumes that night's tags in a single pass; `vault_reindex` declares no dependency on it, so a tagging failure never skips the reindex. The indexer records each file's tag phrase inputs — `(doc_type, domain, topic, project)`, with the project counted only at confidence 0.6 or above, and no value at all when `doc_type_conf` is below 0.6 — in `data/vault_index_state.json` under the reserved `__tag_phrase_keys__` key, and re-embeds a file whose current tuple differs from the recorded one even when its bytes are unchanged. A file with no tag row has no tuple on either side and is never re-embedded on that account. Up to 8 notes are tagged concurrently. A per-note failure is counted in `errors` and the run still exits 0. When `LIFEOS_JEV_VAULT_TAGGING` is `off` or no TypeSafe API key is configured, the source emits `SYNC_SKIPPED` with zero counts and changes nothing. It emits one `SYNC_STATS` line with `tagged`, `skipped_unchanged`, `skipped_not_allowlisted` (tagged from code facets only because the folder is not allowlisted), `code_only` (records built from code facets alone, including restricted notes), `restricted` (notes the on-box sensitivity rules class as restricted; never sent to Jev), `low_confidence` (topic confidence below 0.6), `errors` and `vocab_version`. `--dry-run` reports how many notes would be tagged and sends nothing.

**Generated index pages.** After indexing, `sync_vault_reindex.py` writes `Wiki/Vault Map/<top-level-folder>.md` for every top-level vault folder (creating the folder if missing), plus `Wiki/Vault Map/index.md` with a "By domain" section (each populated domain with its note count, linking its domain page) followed by a "By folder" section (each folder page with its note count and last-30-days change count); the hand-maintained `Wiki/index.md` is never modified and needs only one link to the map. Each page carries the wiki's frontmatter keys (`type: index`, `date:` the newest note date on the page, or across all folder pages for `index.md`) plus `generated: true` and the ownership marker `source: lifeos-index`, and opens with a `Part of [[Wiki/index|Wiki Index]] → Vault map.` wikilink under its heading. A folder page lists the folder's note count, its last-30-days change count, and up to 30 most-recent notes with the one-line summary stored in the BM25 index (`<path>::summary`) where one exists, otherwise the title alone. Output is deterministic (stable ordering, dates only) and a page is rewritten only when its bytes change; changed pages are indexed immediately so they are searchable the same night. Ownership is keyed on `source: lifeos-index`: pages for folders absent from the vault are deleted when they carry it, other files in the folder are never touched, and a handwritten file sharing a page name is left as is. Generated pages left in the legacy `LifeOS/Index/` folder are deleted the same way (and the folder is removed once empty); nothing is written if `Wiki/Vault Map` resolves outside the vault. When the tag store holds tags, `Wiki/Vault Map/Domains/<domain>.md` lists the domain's populated topics with note counts (linking their topic pages) and a count of notes classified only at the domain level, and `Wiki/Vault Map/Topics/<parent>--<child>.md` lists a populated topic's total and its 30 most recent member notes (full-mtime order, path tie-break) with title, date and one-line summary where one exists; only populated topics get a page. Topic membership is `VaultTagStore.paths_matching(topic=[...])`, the matching the topic search facet uses (primary, secondary, and top-ranked or high-probability distribution entries), so the map and search agree. Restricted notes appear on folder pages only. With no stored tags the index says so in one line and no domain or topic pages are written; owned ones left over are deleted. Domain and topic pages follow the same frontmatter, ownership, determinism and collision rules as folder pages. Notes under `Wiki/Vault Map/` are excluded from the pages' counts and are never LLM-summarized (`summarizer.SKIP_PATH_PREFIXES`).

### Phase 5: Content Sync

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_google_docs.py` | Sync Google Docs to vault | Google Docs API |
| `sync_google_sheets.py` | Sync Google Sheets to vault | Google Sheets API |
| `sync_monarch_money.py` | Sync Monarch Money financial data (monthly, runs on 1st) | Monarch Money API |

### Phase 6: Post-Sync Cleanup

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_entity_cleanup.py` | Auto-hide obvious non-human entities (noreply@, newsletters) | `data/crm.db` |

### Phase 7: Consistency Verification

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_consistency_verify.py` | Cross-store consistency check (orphans, stale merged IDs, cached counts, `last_seen`, vault files that moved or vanished) and auto-fix | `data/crm.db`, `data/interactions.db` |

### Summary Queue

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_vault_summaries.py` | Generate queued LLM summaries into BM25 | `data/vault_summary_failures.json`, vault files |

The `vault_summaries` source holds the notes the vault index job indexed and the notes whose summary failed during a reindex. It has its own timeout and sync-health row, and a failure fails only this source. It needs only the LLM to answer: it does not depend on the reindex finishing. With notes queued it waits up to 10 minutes for an LLM that isn't answering, since a restarted one takes a while to load its model, and then reports a skip and leaves the queue alone. Its run time swings widely by design, so duration-collapse detection doesn't apply to it.

It runs last. Its time budget (`LIFEOS_SUMMARY_RETRY_BUDGET_SECONDS`) then delays no other source, and any LLM stopped for embeddings is already back. No later source reads summaries. The vault map pages read them during `vault_reindex`, so a page shows a new summary the night after it is generated. Search reads them from BM25 as soon as they are written.

- **Order.** Notes with the fewest failed attempts first, then newest first, so today's notes come ahead of an old backlog and a note that keeps failing can't block the rest.
- **Failures.** A note whose summary fails stays queued. Three failures in a row end the pass, since the LLM has probably stalled, and the source reports failure so the night's summary shows it. A note that fails on three passes gets a fallback summary from its first line and leaves the queue. A note too short for a real summary gets the fallback straight away.
- **Concurrent indexing.** The source holds no vault index lock, so the job keeps running. Each queue entry has a generation. Anything about to rewrite or remove a note in BM25 first raises its generation under the queue's `flock`, and the source writes a summary and removes the entry under that same `flock`, only if the generation is the one it read before it started. Whichever comes first, no summary of old text survives: an earlier write is deleted with the note's old chunks, and a later one is refused, leaving the entry for the next pass.

### Unified Sync Runner

```bash
# View sync health status
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status

# Dry run (shows what would run)
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run

# Run specific source only
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --source gmail --force

# Execute full sync (all 7 phases)
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --force
```

### First-Backfill Entry Point

The nightly window above is deliberately narrow — Gmail/Calendar look back
only 30 days each night, to keep the nightly run fast. A fresh install
therefore starts with an empty interaction history that the nightly job
never fills in on its own. `scripts/first_backfill.py` is the one-time deep
pass to run right after initial setup: it runs the same sources, in the
same Phase 1-4 order (collection, entity processing, relationship
building, indexing), but any source whose nightly invocation narrows its
own lookback window (Gmail, Calendar) is instead run at that script's own
full-history default (currently ten years) rather than the nightly window.
Phase 5-7 (content sync, cleanup, consistency verification) and sources
with no "full history" concept (`push_birthdays`) aren't part of it — see
the script's own docstring for the exact exclusion list and reasoning.

```bash
# Preview what would run, without running anything
~/.venvs/lifeos/bin/python scripts/first_backfill.py --dry-run

# Run the one-time deep backfill
~/.venvs/lifeos/bin/python scripts/first_backfill.py --execute
```

Safe to re-run: it reuses the same per-source scripts as the nightly job
unmodified, and those are already idempotent (upsert by source ID, not
append-only). An unconfigured source is reported as a clean skip, not a
failure, and one source failing doesn't stop the rest of
the backfill from running. When it finishes, it prints a coverage report
(record count and earliest/latest date per source) read from
`data/interactions.db` — a quick way to confirm history actually arrived.
This is a one-time pass, not part of nightly health monitoring: it does
not touch `sync_health`'s run-history tables, so it can't skew the
duration/yield-collapse baselines the nightly job's own anomaly detection
is tuned against.

---

## Configuration

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `LIFEOS_VAULT_PATH` | Obsidian vault path | `./vault` |
| `LIFEOS_CHROMA_PATH` | ChromaDB data directory | `./data/chromadb` |
| `LIFEOS_CHROMA_URL` | ChromaDB server URL | `http://localhost:8001` |
| `LIFEOS_PORT` | API server port | `8000` |
| `LIFEOS_ALERT_EMAIL` | Sync failure alerts | None |
| `SLACK_USER_TOKEN` | Slack OAuth token | None |
| `SLACK_TEAM_ID` | Slack workspace ID | None |

All scheduled times use **America/New_York** (Eastern Time).

---

## Messaging Source Details

### WhatsApp Sync

**Data Source:** `~/.wacli/wacli.db` on the Apple Data Agent Mac (wacli CLI tool database).
LifeOS runs on Linux; wacli is macOS-only, so WhatsApp data rides through the
Apple Data Agent pipeline alongside contacts, iMessage, phone calls, and
photos.

**Sync Process:**
1. Apple Data Agent Mac cron runs `scripts/apple_data_export.py --execute`, which invokes
   `wacli sync --once` to refresh the local database, then dumps messages,
   group participants, LID contacts, and the whatsmeow LID→phone map to
   `data/apple-imports/whatsapp.json`. If `wacli` is missing or fails the
   manifest records `status: "error"` for the whatsapp source.
2. rsync ships the export directory to the Linux host.
3. On Linux, `scripts/apple_data_import.py` (run as the `apple_import` sync
   source by `run_all_syncs.py`) reads `whatsapp.json` and dispatches to
   `api.services.whatsapp.process_whatsapp_contacts` /
   `process_whatsapp_messages` for entity resolution and interaction
   creation.
4. If the manifest marks the whatsapp source as `status: "error"`, the
   importer still ingests any stale `whatsapp.json` on disk (data is better
   than nothing) but logs `CRITICAL` and exits non-zero, so `sync_health`
   records the apple_import run as FAILED.

**Phone Number Format:**
- Expected: E.164 format (`+15551234567`)
- JID extraction: `15551234567@s.whatsapp.net` → `+15551234567`
- 10-digit US numbers get `+1` prefix automatically
- `@lid` JIDs are resolved via the whatsmeow LID→phone map dumped from
  `~/.wacli/session.db`; LIDs with only a push name fall through to name-based
  entity resolution.

**Interaction Titles:**
- Incoming 1:1: `WhatsApp ← {name or phone}`
- Outgoing 1:1: `WhatsApp → {name or phone}`
- Outgoing group fan-out (one interaction per non-self participant):
  `WhatsApp → {group_name} ({participant_name or phone})`
- Groups larger than `LARGE_GROUP_THRESHOLD` (currently 20) are skipped
  entirely as broadcast noise.

**Bot and broadcast chats:**
- Chats whose JID domain is `@bot` or `@broadcast` are not correspondence with a
  person: their messages create no interactions and are counted in
  `skipped_non_person_chat`.
- `scripts/purge_whatsapp_non_person_chats.py` removes such rows already stored
  (dry run by default; `--execute` takes a SQLite backup first).

**Group Size:**
- A group row stores the group's participant count in `attendee_count` (0 when
  the export doesn't list the group's membership). A direct message stores NULL.
- Relationship strength reads this to weight group messages, and takes direction
  from the source_id: only a group message you sent has the
  `whatsapp_<msg_id>:<participant_jid>` fan-out form. A group message you sent
  earns frequency but no recency credit.
- `scripts/backfill_whatsapp_group_size.py` fills in rows stored without it.

**Entity Resolution:**
- 1:1 messages use `create_if_missing=True` so unknown contacts still get a
  PersonEntity — we'd rather have the interaction attached to a thin person
  than silently drop it.
- Outgoing group fan-out uses `create_if_missing=False`: group members are
  typically already known via contacts or other sources, and we don't want to
  mint one-off PersonEntities for every LID in every group.

### iMessage Sync Ordering

Two Phase 2 sources touch iMessage data, and their order matters:

1. **`imessage`** (`sync_imessage_interactions.py`) creates Interaction records
   from `data/imessage.db`. It links its own unlinked messages internally
   (`join_imessages_to_entities`, by phone/email against PersonEntity) as an
   unconditional step immediately before filtering on
   `person_entity_id IS NOT NULL` — it does not rely on any other source
   having linked first.
2. **`link_imessage`** (`link_imessage_entities.py`) is a separate, retroactive
   backfill: it re-links any still-unlinked handles against the CRM's
   phone→person mapping (`source_entities.observed_phone` /
   `canonical_person_id`), catching partially-linked handles across nights.

`link_imessage` runs after `imessage` (`depends_on: ["imessage"]` in
`SYNC_SOURCES`) so its backfill operates on the night's freshly-created
interactions rather than lagging a day behind.

#### iMessage interaction identity

An iMessage interaction is identified by its message key, `(UTC timestamp
floored to the whole second, direction)`, never by the chat.db ROWID, which
differs between Macs and changes when chat.db is rebuilt. Direction comes from
`is_from_me` on a message and from the title prefix (`→` sent, `←` received) on
an interaction. Sub-second timestamps can differ between exports, so keys never
include microseconds.

Several real messages can share a key (attachment bursts, group sends, messages
from different people), so keys are counted as a multiset and matched in two
stages: each linked, non-hidden message first claims an unused existing
`imessage` interaction of the same person at its key (merged ids compared as
their surviving person on both sides); messages still unmatched then claim any
leftover interaction at that key regardless of person, so a relinked message
does not duplicate. What remains is new and is inserted under its own person.
Matching depends only on each row's timestamp, title prefix and person, not on
its `source_id`. New rows get a rowid-free
`source_id` (`imessage_k_<hash of second, direction, ordinal>`) using the lowest
ordinal not already taken, so the UNIQUE `(source_type, source_id)` index never
drops one. Older `imessage_<rowid>` ids remain valid and coexist with these.
`api/services/imessage_identity.py` holds the key rules;
`scripts/repair_imessage_duplicates.py` applies the same rules to remove
leftover duplicates.

#### Hidden people

Consistency verification deletes interactions that belong to hidden people, so
the iMessage, Gmail, Calendar, phone, and WhatsApp syncs insert none for them
(their source entities are still linked). A person id merged into another is
resolved to the surviving person, whose `hidden` flag decides and whose id the
iMessage rows carry; the other sources receive survivors from the entity
resolver. Sync counters (`interactions_created`, `source_entities_created`)
count rows and entities actually created. Hidden services are the exception; see
below.

#### Services and newsletters

`api/services/service_sender.py` decides whether an email sender is a service or
newsletter rather than a person, from generic signals and no list of brands. The line
is a person's own address: a mailbox provider, their own site, or a local part
carrying their name.

- **A service-shaped address is judged by its shape.** Automated local parts
  (`no-reply`), generated tokens, mailing list hosts, sending hosts (`e.`, `mail.`,
  `service.` below the registered name, never on `.edu`, `.gov`, `.mil`), and role
  mailboxes (`support@`) are services whatever name is on them, and even with threads
  and replies.
- **An own address is judged by what came back.** With no thread and no sent mail, at
  least `ONE_WAY_MIN_RECEIVED` messages make it a service. So does repeated one-way
  mail on a sending host. Anything else leaves a person. On a domain matching the
  sender's name, the local part has to look personal too: their name or initials, a
  greeting such as `hello@` with repeated replies or threads, or real history. A role
  mailbox like `support@` never does.
- **A reply counts on anything but a role, automated, or generated address**, under a
  person's name and at no less than 1% of what came in. A sending host takes three
  replies and no bulk mail.
- **Bulk headers count as a share.** `GmailService` reads `List-Unsubscribe`,
  `List-Post`, and `Precedence` into `EmailMessage.bulk`, and a sender's source
  entities record it as `metadata.bulk`. A `List-Post` address marks a discussion
  list, whose posts stay their authors' own messages. At least three messages have to
  be checked, or all of them if there are fewer.

A row is out of scope, however it looks, with any of these: a reply that counts (a `→`
title or a `<message>:<recipient>` source id); a phone number;
one-to-one iMessage, WhatsApp, phone, or Slack contact, or photos; an iMessage, phone,
photo, or LinkedIn source; a human's link on an own address; a link override; a
confirmed fact; a strength, circle, or tag override; tags, notes, a birthday, or profile
fields; the owner or family; a non-inferred relationship; a review marked kept; or a
merge target. A real meeting (a meeting note, or a calendar event of up to five)
protects a row with an own address. A confirmed link on a service's address says that
message belongs to that row, not that the row is a person, and calendar invites,
contact-book entries, and note mentions vouch for nobody.

| Where | What happens |
|-------|--------------|
| Gmail sync | Mail the owner sends is always recorded. An unknown address with service evidence or on the blocklist creates no person, interaction, or source entity, and joins the skip cache, unless someone of that full name with a phone number is known. This runs before name matching. When an address gains a person, its skipped mail is released and judged again on the next run |
| `sync_strengths.py` | `hide_service_senders()` first restores any hidden service with a reply that counts, then hides new services before scoring and ranking. Past `NIGHTLY_MAX_HIDES` rows it hides nothing, records a sync error, and the sync summary says so |
| `repair_service_senders.py` | The reviewed path for existing rows. See [scripts](../../guides/scripts.md#utility-scripts) |

Hiding clears the row's strength and circle and takes it out of name lookups, so a
different address or a visible namesake never lands on it. The row takes on the
service-shaped addresses it wrote from, unless someone else holds one, and only those
are blocked; an own address that a newsletter merged into the row stays open.

A hidden service keeps its interactions and source entities, and Gmail and Calendar
keep adding to them, which also means its mail isn't fetched again every run.
Its `hidden_reason` starts with `service sender: `, which consistency verification
checks before removing interactions. Consistency verification and relationship
discovery do remove its inferred relationships; discovery rebuilds what it still
produces from the interactions after a restore. Gmail search reads Gmail directly, so nothing stops being
searchable. `repair_service_senders.py --restore` shows the row again, unblocks its
addresses, tags it `confirmed-person`, which keeps the nightly pass off it, and says what
came back. iMessage, WhatsApp, and phone contact while hidden isn't recorded.

### Slack Sync

**Data Source:** Slack API via OAuth token

**Required Environment:**
```bash
SLACK_USER_TOKEN=xoxp-...      # User OAuth token with scopes: users:read, conversations.history, im:history
SLACK_TEAM_ID=T02XXXXXXXX      # Your workspace ID
```

**Sync Process:**
1. `sync_slack.py` - Syncs Slack users to SourceEntity, indexes messages to ChromaDB:
   - **DMs** — full history, restricted to users linked to CRM people
   - **Channels** (public + private, member only) — 90-day window on first sync, incremental after; enumerated via `users.conversations` (archived channels excluded); indexed for search only, no CRM Interactions
2. `link_slack_entities.py` - Links Slack users to PersonEntity by matching email addresses

**Entity Linking:**
- Slack users are matched to existing PersonEntity records by email address
- Email matching is case-insensitive
- Unmatched users remain as SourceEntity only (can be manually linked later)

**Interaction Counts:**
- `shared_slack_count` is populated by relationship discovery after entity linking
- Counts DM message exchanges between linked users

### Daily Sync Order

The unified sync runner (`run_all_syncs.py`) executes `SYNC_ORDER` in this order:

**Phase 1: Data Collection**
1. `gmail_personal` / `gmail_work` / `gmail_work2` - Email sync (sent + received + CC)
2. `calendar_personal` / `calendar_work` / `calendar_work2` - Calendar sync
3. `linkedin` - LinkedIn connections
4. `contacts` - Apple Contacts (macOS-only — `skipped` on Linux)
5. `apple_import` - Import Apple ecosystem data + WhatsApp from the Apple Data Agent export (Linux only)
6. `slack` - Slack users, DMs, and member channels

**Note:** `phone` is defined as a source but is not in nightly `SYNC_ORDER` — it runs via the separate FDA cron on macOS (`scripts/run_sync_with_fda.sh`). Apple data (contacts, phone, iMessage, photos, WhatsApp) is exported from an Apple Data Agent (any spare Mac) at 2:50 AM (before the main pipeline) and imported on the Linux server by `apple_import`.

**Phase 2: Entity Processing**
7. `link_slack` - Link Slack entities by email
8. `imessage` - Create interactions from iMessage DB (links its own unlinked messages internally)
9. `link_imessage` - Retroactive: backfill phone-based links against the latest CRM data (see [iMessage Sync Ordering](#imessage-sync-ordering))
10. `create_contact_persons` - Create a person for a person-less contact with enough iMessage evidence (`LIFEOS_CONTACT_PERSON_MIN_MESSAGES`), linking the contact and its messages to it
11. `link_source_entities` - Retroactive linking for all unlinked entities
12. `photos` - Sync Photos face recognition to people (macOS-only — `skipped` on Linux)

**Phase 2b: Stale ID Cleanup**
13. `repoint_stale_ids` - Re-point interactions with stale merged person IDs to canonical IDs

**Phase 3: Relationship Building**
14. `person_stats_full` - Full refresh of all PersonEntity counts and timestamps
15. `relationship_discovery` - Discover relationships, populate edge weights
16. `strengths` - Recalculate relationship strengths
17. `push_birthdays` - Push LifeOS birthdays to Apple Contacts (macOS-only — `skipped` on Linux)

**Phase 4: Vector Store Indexing**
18. `vault_tag` - Tag changed vault notes (incremental, hash-keyed; skipped when unconfigured); runs first so the reindex sees fresh tags
19. `vault_reindex` - Reindex vault to ChromaDB + BM25
20. `crm_vectorstore` - Index eligible CRM people for semantic search and remove everyone else

**Phase 5: Content Sync**
21. `google_docs` - Sync Google Docs to vault
22. `google_sheets` - Sync Google Sheets to vault
23. `monarch_money` - Monarch Money financial data (monthly, runs on 1st)

**Phase 6: Post-Sync Cleanup**
24. `entity_cleanup` - Auto-hide obvious non-human entities

**Phase 7: Consistency Verification**
25. `consistency_verify` - Cross-store consistency check (orphans, stale merged IDs, cached counts, `last_seen`, vault files that moved or vanished) and auto-fix

**Summary Queue**
26. `vault_summaries` - Generate queued LLM summaries into BM25

**Automated via systemd (Linux) / launchd (macOS):**
- Service: `lifeos-sync` (systemd) or `com.lifeos.crm-sync` (launchd)
- Schedule: Daily at 3:00 AM
- Script: `scripts/run_all_syncs.py`

---

## Utilities

**Memory Monitor** (`api/utils/memory_monitor.py`): For long-running scripts, use `MemoryMonitor` or `check_memory()` to gracefully stop before OOM crashes.

---

## LinkedIn Profile Scraping

### Overview

In addition to the daily LinkedIn CSV sync, there is a **profile scraping system** for extracting detailed profile data (experience, education, skills, about sections) from LinkedIn profiles using browser automation.

**Scripts:**
- `scripts/scrape_linkedin_profiles.py` - Phase 1: Browser automation to save HTML
- `scripts/extract_linkedin_data.py` - Phase 2: Parse saved HTML to extract structured data
- `scripts/enrich_linkedin_jobs.py` - Post-processing: Classify jobs by industry/seniority

**Data Files:**
- `data/linkedin_extracted.json` - Final structured profile data (238 profiles as of Feb 2026)
- `data/linkedin_scrape_state.json` - Progress tracking (completed/pending profiles)
- `data/linkedin_profiles/` - Raw HTML files (if saved)
- `data/linkedin_photos/` - Profile photos (if downloaded)

### Data Schema

The extracted data follows this schema:

```json
{
  "metadata": {
    "extracted_at": "ISO timestamp",
    "total_profiles": 238,
    "source": "LinkedIn profile scraping via Claude in Chrome",
    "schema_version": "1.7",
    "notes": "Scraped Feb 2026. 264 profiles attempted, 238 successfully extracted."
  },
  "profiles": [
    {
      "person_id": "UUID from PersonEntity",
      "linkedin_url": "https://linkedin.com/in/username",
      "scraped_at": "ISO timestamp",
      "name": "Full Name",
      "headline": "Current title",
      "location": "Oakland, California",
      "city": "Oakland",
      "state": "CA",
      "pronouns": "they/them",
      "about": "About section text",
      "experience": [{
        "company": "Company Name",
        "title": "Job Title",
        "start_month": 1,
        "start_year": 2022,
        "end_month": null,
        "end_year": null,
        "duration_months": 25,
        "location": "San Francisco, California",
        "city": "San Francisco",
        "state": "CA",
        "description": "Job description",
        "industry": "Tech",
        "seniority": "Senior"
      }],
      "education": [{
        "institution": "University Name",
        "degree": "Bachelor of Science",
        "field": "Computer Science",
        "graduation_year": "2018",
        "activities": "Student government",
        "description": "Additional notes"
      }],
      "skills": ["Python", "Leadership"],
      "certifications": [],
      "languages": [],
      "volunteering": [],
      "honors": [],
      "publications": [],
      "organizations": [],
      "causes": []
    }
  ]
}
```

### Data Normalization Rules

**Location fields:**
- `location`: Original location string with ", United States" removed
- `city`: Simplified city name (e.g., "San Francisco Bay Area" → "San Francisco")
- `state`: 2-letter US state abbreviation (e.g., "CA", "NY", "DC")
- Washington D.C. is always normalized to: city=`"Washington, D.C."`, state=`"DC"`

**Date/duration fields:**
- `start_month`: Integer 1-12, or null if only year known
- `start_year`: Integer (e.g., 2022)
- `end_month`: Integer 1-12, or null for current positions
- `end_year`: Integer, or null for current positions (null end_month + null end_year = "Present")
- `duration_months`: Integer number of months (e.g., 25 for 2 years 1 month)

### Industry & Seniority Classification

Jobs are classified with:
- **Industry** (18 values): Consulting, Education, Energy, Entertainment, Finance, Government, Healthcare, Legal, Logistics, Media, Military, Non-profit, Other, Politics, Real Estate, Religious, Retail, Tech
- **Seniority** (4 values): Executive, Senior, Mid-level, Entry

Classification is done by Claude during scraping based on job titles and company names.

### Running the Scraper

The scraping system uses Claude in Chrome MCP for browser automation. It requires:
1. An authenticated LinkedIn session in Chrome
2. Claude in Chrome extension installed and running

**Warning:** LinkedIn may restrict accounts that scrape too quickly. The system includes random delays (8-15 seconds) between profiles, but extended scraping sessions may still trigger rate limiting.

**To resume scraping (if needed):**
1. Check `data/linkedin_scrape_state.json` for pending profiles
2. Use Claude in Chrome to navigate to profiles and extract data
3. Follow the schema above for consistency

## Related Documents

- [Data Model](../product/data-model.md) -- Two-tier data model (SourceEntity / PersonEntity)
- [Entity Resolution](../product/entity-resolution.md) -- How source entities are linked to canonical records
- [Search & Indexing](search-indexing.md) -- Hybrid search pipeline
- [Installation](../../guides/installation.md) -- Config-only second-user setup checklist, which points here for the unconfigured-source clean-skip pattern
- [First Run](../../guides/first-run.md) -- New-install walkthrough; Step 4 points here for the first-backfill entry point
- [ADR-002: ChromaDB](../../adr/002-chromadb-vector-store.md) -- Why ChromaDB was chosen
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) -- Why SourceEntity and PersonEntity are separate
- [ADR-012: Embedding Pipeline](../../adr/012-embedding-pipeline.md) -- Embedding model, GPU/CPU fallback, pre-flight RAM gate around phase 4
