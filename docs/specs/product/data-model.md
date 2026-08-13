# Data Model

> **Status:** Complete
> **Owner:** CRM
> **Last Updated:** 2026-08-12

LifeOS uses a two-tier data model to separate raw observations from canonical records.

---

## Two-Tier Data Model

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
│  • Stored in SQLite (data/crm.db: person_entities + lookup tables)              │
│  • One unified record per person                                                │
│  • Merged data from all sources                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Source Types

Source entities are tagged with one of the following types:

`gmail`, `calendar`, `slack`, `imessage`, `whatsapp`, `signal`, `contacts`, `phone_contacts`, `linkedin`, `vault`, `granola`, `phone_call`, `phone`, `photos`

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
| shared_phone_calls_count | Phone calls (synchronous, high value) |
| shared_photos_count | Photos together |
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
| `discover_from_shared_photos` | Photos | User ↔ person in photo |
| `discover_linkedin_connections` | LinkedIn | Mark is_linkedin_connection |

### Discovery Window

- Default: 3650 days (~10 years) - processes all available historical data
- Configurable via `DISCOVERY_WINDOW_DAYS` in `relationship_discovery.py`
- Future calendar events excluded from last_seen_together

### Triggering Discovery

- **Automatic**: Daily sync Phase 3
- **Manual**: `POST /api/crm/relationships/discover`
- **Script**: `~/.venvs/lifeos/bin/python scripts/sync_relationship_discovery.py --execute`

## Related Documents

- [Entity Resolution](entity-resolution.md) -- How source entities are linked to canonical records
- [Data & Sync](../technical/data-and-sync.md) -- Sync pipeline and data stores
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) -- Why SourceEntity and PersonEntity are separate
- [CRM UI](crm-ui.md) -- CRM interface product spec
- [Journal Analytics](journal-analytics.md) -- Emotion-wheel and trend views that read vault notes directly and are explicitly outside this two-tier model (one exception: the felt-vs-recorded connection view crosses into entity resolution and interaction history — see that view's section for how)
