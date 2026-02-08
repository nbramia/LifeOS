# Data & Sync Architecture

How LifeOS ingests, stores, and resolves data from multiple sources.

**Related Documentation:**
- [API & MCP Reference](API-MCP-REFERENCE.md) - API endpoints
- [CRM UI PRD](../prd/CRM-UI.md) - CRM features and requirements

---

## Table of Contents

1. [Data Sources](#data-sources)
2. [Sync Schedule](#sync-schedule)
3. [Data Stores](#data-stores)
4. [Entity Resolution](#entity-resolution)
5. [Search Pipeline](#search-pipeline)
6. [Relationship Tracking](#relationship-tracking)

---

## Data Sources

### Source Types and Sync Methods

| Source | Sync Method | Data Extracted |
|--------|-------------|----------------|
| Gmail | Google API | From/To/CC, subjects, timestamps, threads |
| Calendar | Google API | Attendees, organizer, titles, times |
| Apple Contacts | CSV Export | Names, emails, phone numbers, companies |
| Apple Photos | Photos.sqlite | Face recognition, co-appearances, timestamps |
| Phone Calls | macOS CallHistoryDB | Numbers, names, duration, direction |
| WhatsApp | wacli CLI | JIDs, names, phone numbers |
| iMessage | macOS chat.db | Phone/email, message content, timestamps |
| Slack | Slack API (OAuth) | User profiles, DMs, channels |
| Vault Notes | Obsidian markdown | Name mentions, context paths |
| LinkedIn | CSV Import | Connections, companies, titles |
| LinkedIn Profiles | Browser Scraping | Full profile data (experience, education, skills) |
| Granola | Folder watcher | Meeting transcripts, attendees |

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

### Unified Daily Sync (5 Phases)

All data syncing is consolidated into a single daily sync with proper phase ordering. This ensures downstream processes always have access to fresh upstream data.

```
02:30          Pre-sync health check (API server)
03:00          Unified sync starts (via run_all_syncs.py)

               === PHASE 1: Data Collection ===
               Pull fresh data from all external sources
03:00          └─ Gmail (sent + received + CC emails)
03:01          └─ Calendar (Google Calendar events)
03:02          └─ LinkedIn (connections CSV export)
03:03          └─ Contacts (Apple Contacts CSV)
03:04          └─ Phone (macOS CallHistoryDB)
03:05          └─ WhatsApp (wacli database)
03:06          └─ iMessage (macOS chat.db)
03:07          └─ Slack (users + DM messages)

               === PHASE 2: Entity Processing ===
               Link source entities to canonical PersonEntity records
03:08          └─ Link Slack (match by email)
03:08          └─ Link iMessage (match by phone)
03:09          └─ Photos (sync face recognition to people)

               === PHASE 3: Relationship Building ===
               Build relationships using all collected interaction data
               (Note: person stats are now refreshed inline by each sync script)
03:09          └─ Relationship discovery (populate edge weights)
03:10          └─ Strengths (calculate relationship scores)

               === PHASE 4: Vector Store Indexing ===
               Index content with fresh people data available
03:12          └─ Vault reindex (ChromaDB + BM25)

               === PHASE 5: Content Sync ===
               Pull external content into vault
03:13          └─ Google Docs (configured docs → vault)
03:14          └─ Google Sheets (form responses → vault)

~03:15         Unified sync complete
07:00          Post-sync health check (API server)

08:00          Calendar sync (Google Calendar → ChromaDB)
12:00          Calendar sync
15:00          Calendar sync

24/7           File watcher (real-time vault changes → ChromaDB + BM25)
24/7           Granola processor (every 5 min, Granola/ → vault)
24/7           Omi processor (every 5 min, Omi/Events/ → vault)
```

### Phase Dependencies

The 5-phase structure ensures correct data flow:

1. **Data Collection** runs first so all external data is fresh
2. **Entity Processing** links source entities after they exist
3. **Relationship Building** computes metrics using linked entities
4. **Vector Store Indexing** indexes content with fresh CRM data available for entity resolution
5. **Content Sync** pulls external content (indexed on next run)

### Process Summary

| Process | Schedule | Reads From | Writes To |
|---------|----------|------------|-----------|
| ChromaDB Server | Continuous (boot) | HTTP requests | Vector data |
| Launchd API Service | Continuous (boot) | All data | API logs |
| Unified Sync | Daily 3:00 AM ET | All sources | All stores |
| Calendar Indexer | 8 AM, 12 PM, 3 PM ET | Google Calendar | ChromaDB (`lifeos_calendar`) |
| Vault File Watcher | Continuous | Vault filesystem | ChromaDB, BM25 |
| Granola Processor | Every 5 minutes | `Granola/` folder | Vault (classified) |
| Omi Processor | Every 5 minutes | `Omi/Events/` folder | Vault (classified) |

### Failure Notifications

Configure `LIFEOS_ALERT_EMAIL` in `.env` to receive notifications when sync steps fail.

---

## Data Stores

### Two-Tier Data Model

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           TWO-TIER DATA MODEL                                    │
│                                                                                  │
│  TIER 1: SOURCE ENTITIES (Raw Observations)                                     │
│  • Stored in SQLite (data/crm.db)                                               │
│  • One record per observation from each source                                  │
│  • Immutable - preserves original data                                          │
│                                                                                  │
│  TIER 2: PERSON ENTITIES (Canonical Records)                                    │
│  • Stored in JSON (data/people_entities.json)                                   │
│  • One unified record per person                                                │
│  • Merged data from all sources                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Store Locations

| Store | Location | Purpose | Updated By |
|-------|----------|---------|------------|
| ChromaDB | `data/chromadb/` | Vector embeddings | Nightly reindex, File watcher |
| ChromaDB (Slack) | `lifeos_slack` collection | Slack message vectors | Nightly Slack sync |
| BM25 Index | `data/chromadb/bm25_index.db` | Keyword search | Nightly reindex, File watcher |
| Vault | Configured via `LIFEOS_VAULT_PATH` | Primary knowledge base | User, Granola, Omi, GDoc Sync |
| PersonEntity | `data/people_entities.json` | Resolved identities | People v2 sync, iMessage sync |
| SourceEntity | `data/crm.db` | Raw observations | All sync scripts |
| Interactions | `data/crm.db` | Interactions per person | People v2 sync, Slack sync |
| Relationships | `data/crm.db` | Person-to-person edges | Relationship discovery |
| iMessage | `data/imessage.db` | Message export cache | iMessage sync |
| Task Index | `data/task_index.json` | Parsed task cache | Task CRUD, file watcher |
| Reminders | `~/.lifeos/reminders.json` | Scheduled reminders | Reminder CRUD, scheduler |
| Memories | `~/.lifeos/memories.json` | User-saved memories | Memory CRUD |

---

## Entity Resolution

### Resolution Algorithm

The EntityResolver uses a three-pass algorithm with weighted scoring:

**Pass 1: Exact Identifier Matching**
1. Email exact match → confidence=1.0
2. Phone exact match (E.164 format) → confidence=1.0

**Pass 2: Fuzzy Name Matching**
- Name similarity: RapidFuzz `token_set_ratio` × 0.4
- Context boost: +30 points if vault path matches
- Recency boost: +10 points if last_seen < 30 days
- Minimum threshold: score >= 40

**Pass 3: Disambiguation**
- If top two candidates differ by < 15 points → ambiguous
- Create new entity with disambiguation suffix or reduce confidence

### Scoring Weights

| Component | Weight/Points |
|-----------|---------------|
| Name Similarity | × 0.4 (0-40 points) |
| Context Boost | +30 points |
| Recency Boost | +10 points |
| Minimum Score | 40 |
| Disambiguation Threshold | 15 points |

### Domain-to-Context Mapping

Configured in `config/people_config.py`:

| Email Domain | Vault Context | Category |
|--------------|---------------|----------|
| yourcompany.com | Work/YourCompany/ | work |
| gmail.com | Personal/ | personal |

---

## Relationship Discovery

The relationship discovery system scans interactions to build person-to-person relationship edges.

### Discovery Methods

| Method | Source | Signal |
|--------|--------|--------|
| `discover_from_calendar` | Calendar events | Shared attendees |
| `discover_from_calendar_direct` | Calendar events | User ↔ each attendee |
| `discover_from_email_threads` | Gmail threads | Co-recipients in threads |
| `discover_from_vault_comments` | Vault notes | Co-mentioned people |
| `discover_from_imessage_direct` | iMessage | User ↔ message recipient |
| `discover_from_whatsapp_direct` | WhatsApp | User ↔ chat participant |
| `discover_from_phone_calls` | Phone history | User ↔ caller/callee |
| `discover_from_slack_direct` | Slack DMs | User ↔ DM participant |
| `discover_linkedin_connections` | LinkedIn | Mark is_linkedin_connection |

### Discovery Window

- Default: 3650 days (~10 years) - processes all available historical data
- Configurable via `DISCOVERY_WINDOW_DAYS` in `relationship_discovery.py`
- Future calendar events excluded from last_seen_together

### Daily Sync Integration

Relationship discovery runs as Phase 3 of the unified daily sync:
```
Phase 1 - Data Collection (Gmail, Calendar, Contacts, Phone, WhatsApp, iMessage, Slack)
Phase 2 - Entity Processing (Link Slack entities by email)
Phase 3 - Relationship Building:
  └─ relationship_discovery ← discovers/updates relationships
  └─ strengths ← recalculate relationship strengths
  (person_stats refreshed inline by each sync script)
Phase 4 - Vector Store Indexing
Phase 5 - Content Sync
```

### Triggering Discovery

- **Automatic**: Daily sync Phase 3
- **Manual**: `POST /api/crm/relationships/discover`
- **Script**: `uv run python scripts/sync_relationship_discovery.py --execute`

---

## Search Pipeline

```
Query → Name Expansion → [Vector Search + BM25 Search] → RRF Fusion → Boosting → Results
```

### Components

1. **Name Expansion**: Nicknames → canonical names ("Al" → "Alex")
2. **Dual Search**:
   - Vector: semantic similarity via ChromaDB
   - BM25: keyword matching via SQLite FTS5
3. **RRF Fusion**: `score = Σ 1/(60 + rank)`
4. **Boosting**: Recency (0-50%) + Filename match (2x)

### Key Files

| File | Purpose |
|------|---------|
| `api/services/hybrid_search.py` | Main search logic |
| `api/services/vectorstore.py` | ChromaDB wrapper |
| `api/services/bm25_index.py` | BM25 index |
| `api/services/query_classifier.py` | Factual vs semantic detection |
| `api/services/query_router.py` | LLM-based source routing + person name extraction |

---

## Relationship Tracking

### Relationship Data Model

Each relationship between two people tracks signals from multiple sources:

| Field | Description |
|-------|-------------|
| shared_events_count | Calendar events together |
| shared_threads_count | Email threads together |
| shared_messages_count | iMessage/SMS threads |
| shared_whatsapp_count | WhatsApp threads |
| shared_slack_count | Slack DM messages |
| is_linkedin_connection | Both have LinkedIn source |

### Graph Edge Weight

Graph edges use unified strength scoring:
- **Owner edges** (you ↔ someone): Uses the person's `relationship_strength`
- **Non-owner edges** (others ↔ others): Uses `pair_strength` computed from shared interactions

### Relationship Strength Formula

```
strength = (recency × 0.30) + (frequency × 0.60) + (diversity × 0.10)

Where:
- recency = max(0, 1 - days_since_last / 200)
- frequency = hybrid of recent (70%) and lifetime (30%) weighted interactions
- diversity = unique_sources / total_sources
```

### Pair Strength Formula (for non-owner edges)

```
pair_strength = (recency × 0.30) + (frequency × 0.60) + (diversity × 0.10)

Where:
- recency = max(0, 1 - days_since_last_seen_together / 200)
- frequency = log(1 + weighted_count) / log(1 + 100)
- diversity = source_types_with_interactions / 6
```

### Manual Overrides (Strength, Circle & Tags)

Some relationships require manual overrides that persist through sync cycles. These are configured by **person ID** (not name) for durability.

**Configuration File:** `config/relationship_weights.py`

```python
# Strength overrides - force specific relationship_strength values
STRENGTH_OVERRIDES_BY_ID = {
    "<partner-person-id>": 100.0,  # Partner
}

# Circle overrides - force specific Dunbar circle assignments
CIRCLE_OVERRIDES_BY_ID = {
    "<partner-person-id>": 0,  # Partner
}

# Tag overrides - apply tags from LinkedIn data extraction
# Format: industry:X, seniority:X, state:XX, city:X
TAG_OVERRIDES_BY_ID = {
    "cb93e7bd-036c-4ef5-adb9-34a9147c4984": ["city:oakland", "state:ca", "industry:tech", "seniority:executive"],
}
```

**Where Overrides Are Applied:**

| Override Type | Used In | When Applied |
|---------------|---------|--------------|
| `STRENGTH_OVERRIDES_BY_ID` | `api/services/relationship_metrics.py` | Dunbar circle computation (sorting) |
| `STRENGTH_OVERRIDES_BY_ID` | `api/routes/crm.py` | API responses (display strength) |
| `CIRCLE_OVERRIDES_BY_ID` | `api/services/relationship_metrics.py` | `compute_all_dunbar_circles()` |
| `TAG_OVERRIDES_BY_ID` | `api/services/relationship_metrics.py` | `apply_tag_overrides()` |

**How It Works:**

1. **Strength overrides** affect both the displayed `relationship_strength` in API responses AND the sorting order when computing Dunbar circles
2. **Circle overrides** force specific people into specific circles regardless of their ranking
3. **Tag overrides** apply tags (industry, seniority, location) extracted from LinkedIn profiles
4. All use **person IDs** (UUIDs) as keys, not names, so renames don't break overrides
5. Overrides are applied during nightly sync via `update_all_strengths()`

**Important:** To find a person's ID, use the API: `GET /api/crm/people?search=name`

**Why ID-Based:**
- Names can change (renames, typos, merges)
- IDs are immutable UUIDs assigned at person creation
- Prevents overrides from silently breaking when names change

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
| `sync_contacts_csv.py` | Import Apple Contacts | CSV export |
| `sync_phone_calls.py` | Sync phone calls | macOS CallHistoryDB |
| `sync_whatsapp.py` | Sync WhatsApp contacts and messages | `~/.wacli/wacli.db` |
| `sync_imessage_interactions.py` | Sync iMessage | macOS chat.db |
| `sync_slack.py` | Sync Slack users and DMs | Slack API |

### Phase 2: Entity Processing

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `link_slack_entities.py` | Link Slack users to people by email | `data/crm.db` |
| `link_imessage_entities.py` | Link iMessage handles to people by phone | `data/imessage.db` |
| `sync_photos.py` | Sync Photos face recognition to people | Photos.sqlite |

### Phase 3: Relationship Building

| Script | Purpose | Data Source |
|--------|---------|-------------|
| `sync_relationship_discovery.py` | Discover relationships and populate edge weights | All interactions |
| `sync_person_stats.py` | Verify/repair interaction counts (not in nightly sync) | `data/interactions.db` |
| `sync_strengths.py` | Recalculate relationship strengths | `data/crm.db` |

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

### Unified Sync Runner

```bash
# View sync health status
uv run python scripts/run_all_syncs.py --status

# Dry run (shows what would run)
uv run python scripts/run_all_syncs.py --dry-run

# Run specific source only
uv run python scripts/run_all_syncs.py --source gmail --force

# Execute full sync (all 5 phases)
uv run python scripts/run_all_syncs.py --force
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

**Data Source:** `~/.wacli/wacli.db` (wacli CLI tool database)

**Sync Process:**
1. Sync contacts from wacli's contact database
2. Sync messages from wacli's message database
3. Create interactions for each message thread
4. Link to PersonEntity via phone number (E.164 format)

**Phone Number Format:**
- Expected: E.164 format (`+15551234567`)
- JID extraction: `15551234567@s.whatsapp.net` → `+15551234567`
- 10-digit US numbers get `+1` prefix automatically

**Message Types:**
- DMs: `title = "WhatsApp DM: {contact_name}"`
- Groups: `title = "WhatsApp group: {group_name}"`

**Entity Resolution:**
- Messages sync uses `create_if_missing=True` to create PersonEntity for new contacts
- Ensures message history from unknown contacts is not lost

### Slack Sync

**Data Source:** Slack API via OAuth token

**Required Environment:**
```bash
SLACK_USER_TOKEN=xoxp-...      # User OAuth token with scopes: users:read, conversations.history, im:history
SLACK_TEAM_ID=T02XXXXXXXX      # Your workspace ID
```

**Sync Process:**
1. `sync_slack.py` - Syncs Slack users to SourceEntity, indexes DMs to ChromaDB
2. `link_slack_entities.py` - Links Slack users to PersonEntity by matching email addresses

**Entity Linking:**
- Slack users are matched to existing PersonEntity records by email address
- Email matching is case-insensitive
- Unmatched users remain as SourceEntity only (can be manually linked later)

**Interaction Counts:**
- `shared_slack_count` is populated by relationship discovery after entity linking
- Counts DM message exchanges between linked users

### Daily Sync Order

The unified sync runner (`run_all_syncs.py`) executes in this order:

**Phase 1: Data Collection**
1. `gmail` - Email sync (sent + received + CC)
2. `calendar` - Calendar sync
3. `linkedin` - LinkedIn connections
4. `contacts` - Apple Contacts
5. `phone` - Phone calls
6. `whatsapp` - WhatsApp contacts and messages
7. `imessage` - iMessage sync
8. `slack` - Slack users and DMs

**Phase 2: Entity Processing**
9. `link_slack` - Link Slack entities by email
10. `link_imessage` - Link iMessage handles by phone
11. `photos` - Sync Photos face recognition to people

**Phase 3: Relationship Building**
11. `relationship_discovery` - Discover relationships, populate edge weights
12. `strengths` - Recalculate relationship strengths
(Note: person_stats refreshed inline by each data collection sync)

**Phase 4: Vector Store Indexing**
14. `vault_reindex` - Reindex vault to ChromaDB + BM25
15. `crm_vectorstore` - Index CRM people for semantic search

**Phase 5: Content Sync**
15. `google_docs` - Sync Google Docs to vault
16. `google_sheets` - Sync Google Sheets to vault

**Automated via launchd:**
- Service: `com.lifeos.crm-sync`
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
