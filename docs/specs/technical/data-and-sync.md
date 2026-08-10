# Data & Sync Architecture

> **Status:** Complete
> **Owner:** Data Pipeline
> **Last Updated:** 2026-08-09

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
| Apple Contacts | Apple Data Agent (Mac Mini export via rsync) | Names, emails, phone numbers, companies |
| Apple Photos | Apple Data Agent (Mac Mini export via rsync) | Face recognition, co-appearances, timestamps |
| Phone Calls | Apple Data Agent (Mac Mini export via rsync) | Numbers, names, duration, direction |
| WhatsApp | wacli CLI | JIDs, names, phone numbers |
| iMessage | Apple Data Agent (Mac Mini export via rsync) | Phone/email, message content, timestamps |
| Slack | Slack API (OAuth) | User profiles, DMs, channels |
| Vault Notes | Obsidian markdown | Name mentions, context paths |
| LinkedIn | CSV Import | Connections, companies, titles |
| LinkedIn Profiles | Browser Scraping | Full profile data (experience, education, skills) |
| Granola | Vault file watcher (notes placed by external classifier) | Meeting transcripts, attendees |

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
02:50          Apple Data Agent export (Mac Mini → Linux server via rsync)
               └─ Exports contacts, phone calls, iMessage, photos, WhatsApp
               └─ scripts/apple_data_agent.sh → scripts/apple_data_export.py (Mac Mini)
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
                 the Mac Mini export; Linux only)
               └─ Slack (users + DM and member-channel messages)

               === PHASE 2: Entity Processing ===
               Link source entities to canonical PersonEntity records
               └─ Link Slack (match by email)
               └─ iMessage (create interactions; links its own unlinked
                 messages internally before filtering, so it doesn't need
                 Link iMessage to have run first)
               └─ Link iMessage (retroactive: backfill phone-based links
                 against the latest CRM phone→person mapping)
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
                 counts; auto-fixes below a threshold)

~03:16         Unified sync complete
07:00          Post-sync health check (API server)

08:00          Calendar sync (Google Calendar → ChromaDB)
12:00          Calendar sync
15:00          Calendar sync

24/7           File watcher (real-time vault changes → ChromaDB + BM25)
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

**Note:** Apple data (contacts, phone calls, iMessage, photos, WhatsApp) is exported from the Mac Mini via the Apple Data Agent (`scripts/apple_data_agent.sh`) at 2:50 AM, before the main pipeline, and imported on Linux by the `apple_import` source. The export runs on the Mac Mini (which has FDA access) and syncs to the Linux server via rsync. Three sources are macOS-only and report `skipped` (not a failure) when the nightly sync runs on the Linux host: `contacts`, `photos`, `push_birthdays`.

### Process Summary

| Process | Schedule | Reads From | Writes To |
|---------|----------|------------|-----------|
| ChromaDB Server | Continuous (boot) | HTTP requests | Vector data |
| systemd/launchd API Service | Continuous (boot) | All data | API logs |
| Unified Sync | Daily 3:00 AM ET | All sources | All stores |
| Calendar Indexer | 8 AM, 12 PM, 3 PM ET | Google Calendar | ChromaDB (`lifeos_calendar`) |
| Vault File Watcher | Continuous | Vault filesystem | ChromaDB, BM25 |

### Failure Notifications

Configure `LIFEOS_ALERT_EMAIL` in `.env` to receive notifications when sync steps fail.

---

## Data Stores

### Store Locations

| Store | Location | Purpose | Updated By |
|-------|----------|---------|------------|
| ChromaDB | `data/chromadb/` | Vector embeddings | Nightly reindex, File watcher |
| ChromaDB (Slack) | `lifeos_slack` collection | Slack message vectors | Nightly Slack sync |
| BM25 Index | `data/chromadb/bm25_index.db` | Keyword search | Nightly reindex, File watcher |
| Vault | Configured via `LIFEOS_VAULT_PATH` | Primary knowledge base | User, external classifier, GDoc Sync |
| PersonEntity | `data/crm.db` (person_entities table) | Resolved identities | People v2 sync, iMessage sync |
| SourceEntity | `data/crm.db` | Raw observations | All sync scripts |
| Interactions | `data/interactions.db` | Interactions per person | People v2 sync, Slack sync |
| Relationships | `data/crm.db` | Person-to-person edges | Relationship discovery |
| iMessage | `data/imessage.db` | Message export cache | iMessage sync |
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
| `apple_data_import.py` | Import Apple ecosystem data (contacts, phone, iMessage, photos, WhatsApp) from the Mac Mini export | Apple Data Agent export (Linux only) |
| `sync_phone_calls.py` | Sync phone calls (not in nightly `SYNC_ORDER` — runs via the separate FDA cron on macOS) | Apple Data Agent export |
| `sync_slack.py` | Sync Slack users, DMs, and member channels | Slack API |

### Apple Data Agent

| Script | Purpose | Runs On |
|--------|---------|---------|
| `apple_data_export.py` | Export Apple data (contacts, phone, iMessage, photos, WhatsApp) | Mac Mini |
| `apple_data_import.py` | Import Apple data exports into LifeOS | Linux server |
| `apple_data_agent.sh` | Orchestrate export + rsync + import | Mac Mini (cron) |

WhatsApp data flows through the same Mac Mini → Linux pipeline. The Mac runs `wacli` (steipete/tap/wacli) which reads the WhatsApp Desktop app's local SQLite database; `apple_data_export.export_whatsapp` dumps contacts, messages, group memberships and the LID-to-phone map to `whatsapp.json`; `apple_data_import.import_whatsapp` calls into `api/services/whatsapp.py` to create SourceEntity and Interaction records.

### Phase 2: Entity Processing

Runs in this order (see [iMessage Sync Ordering](#imessage-sync-ordering) below): `link_slack` → `imessage` → `link_imessage` → `link_source_entities` → `photos`.

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `link_slack_entities.py` | Link Slack users to people by email | `data/crm.db` |
| `sync_imessage_interactions.py` | Create interactions from iMessage DB; links its own unlinked messages internally before filtering | Apple Data Agent export |
| `link_imessage_entities.py` | Retroactive: backfill phone-based links against the latest CRM phone→person mapping | `data/imessage.db` |
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
| `sync_strengths.py` | Recalculate relationship strengths | `data/crm.db` |
| `push_birthdays_to_contacts.py` | Push LifeOS birthdays to Apple Contacts (macOS-only — `skipped` on Linux) | `data/crm.db` |

### Phase 4: Vector Store Indexing

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_vault_reindex.py` | Reindex vault to ChromaDB + BM25 | Vault files |
| `sync_crm_to_vectorstore.py` | Index CRM people for semantic search | `data/crm.db` |

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
| `sync_consistency_verify.py` | Cross-store consistency check (orphans, stale merged IDs, cached counts) and auto-fix | `data/crm.db`, `data/interactions.db` |

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

**Data Source:** `~/.wacli/wacli.db` on the Mac Mini (wacli CLI tool database).
LifeOS runs on Linux; wacli is macOS-only, so WhatsApp data rides through the
Apple Data Agent pipeline alongside contacts, iMessage, phone calls, and
photos.

**Sync Process:**
1. Mac Mini cron runs `scripts/apple_data_export.py --execute`, which invokes
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
interactions rather than lagging a day behind. An earlier ordering (`link_imessage`
before `imessage`) was intentional at the time it was introduced — `imessage`
did not yet link unconditionally — but that root cause was fixed directly
inside `sync_imessage_interactions.py` afterward, making the pre-linking step
unnecessary and leaving the retroactive backfill running on stale CRM data
each night.

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
5. `apple_import` - Import Apple ecosystem data + WhatsApp from the Mac Mini export (Linux only)
6. `slack` - Slack users, DMs, and member channels

**Note:** `phone` is defined as a source but is not in nightly `SYNC_ORDER` — it runs via the separate FDA cron on macOS (`scripts/run_sync_with_fda.sh`). Apple data (contacts, phone, iMessage, photos, WhatsApp) is exported from the Mac Mini via the Apple Data Agent at 2:50 AM (before the main pipeline) and imported on the Linux server by `apple_import`.

**Phase 2: Entity Processing**
7. `link_slack` - Link Slack entities by email
8. `imessage` - Create interactions from iMessage DB (links its own unlinked messages internally)
9. `link_imessage` - Retroactive: backfill phone-based links against the latest CRM data (see [iMessage Sync Ordering](#imessage-sync-ordering))
10. `link_source_entities` - Retroactive linking for all unlinked entities
11. `photos` - Sync Photos face recognition to people (macOS-only — `skipped` on Linux)

**Phase 2b: Stale ID Cleanup**
12. `repoint_stale_ids` - Re-point interactions with stale merged person IDs to canonical IDs

**Phase 3: Relationship Building**
13. `person_stats_full` - Full refresh of all PersonEntity counts and timestamps
14. `relationship_discovery` - Discover relationships, populate edge weights
15. `strengths` - Recalculate relationship strengths
16. `push_birthdays` - Push LifeOS birthdays to Apple Contacts (macOS-only — `skipped` on Linux)

**Phase 4: Vector Store Indexing**
17. `vault_reindex` - Reindex vault to ChromaDB + BM25
18. `crm_vectorstore` - Index CRM people for semantic search

**Phase 5: Content Sync**
19. `google_docs` - Sync Google Docs to vault
20. `google_sheets` - Sync Google Sheets to vault
21. `monarch_money` - Monarch Money financial data (monthly, runs on 1st)

**Phase 6: Post-Sync Cleanup**
22. `entity_cleanup` - Auto-hide obvious non-human entities

**Phase 7: Consistency Verification**
23. `consistency_verify` - Cross-store consistency check (orphans, stale merged IDs, cached counts) and auto-fix

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
- [ADR-002: ChromaDB](../../adr/002-chromadb-vector-store.md) -- Why ChromaDB was chosen
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) -- Why SourceEntity and PersonEntity are separate
- [ADR-012: Embedding Pipeline](../../adr/012-embedding-pipeline.md) -- Embedding model, GPU/CPU fallback, pre-flight RAM gate around phase 4
