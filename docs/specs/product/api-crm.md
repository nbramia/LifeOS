# CRM API Reference

**Status:** Complete
**Owner:** API Gateway
**Last Updated:** 2026-09-04

Every `/api/crm/*` HTTP endpoint. Split out of the main [api-reference.md](api-reference.md) because the CRM endpoint catalog is large enough to deserve its own file. For the consumer view of the CRM features these endpoints back, see the [CRM specs](crm-ui.md).

---

## Table of Contents

1. [People](#people)
2. [Contact Sources, Split, Merge](#contact-sources-split-merge)
3. [Relationships and Graph](#relationships-and-graph)
4. [Strengths, Discovery, Statistics](#strengths-discovery-statistics)
5. [Facts](#facts)
6. [Review Queue](#review-queue)
7. [Data Health](#data-health)
8. [Config](#config)
9. [Me Dashboard](#me-dashboard)
10. [Family](#family)
11. [Birthdays](#birthdays)
12. [Sync Health](#sync-health)
13. [Relationship Insights](#relationship-insights)
14. [Tone Analysis](#tone-analysis)
15. [Slack Integration](#slack-integration)
16. [Contacts Sync](#contacts-sync)

---

## People

### GET /api/crm/people

List/search people with filters.

**Query parameters:**
- `q` (string): Search query (name, email, company)
- `category` (string): work, personal, family
- `source` (string): gmail, calendar, slack, etc.
- `has_pending` (bool): Has pending links
- `sort` (string): name, last_seen, interaction_count, strength
- `order` (string): asc, desc
- `limit` (int): Results per page (default: 50)
- `offset` (int): Pagination offset

Each returned person carries `has_profile_photo` (bool, from `photo_count > 0`) alongside the usual fields, so a client can request `GET /api/photos/profile/{id}` only for people flagged true instead of probing everyone (#875). `GET /api/crm/people/{id}` carries the same field for consistency.

### GET /api/crm/people/{id}

Get person detail with source entities.

### GET /api/crm/people/{id}/timeline

Chronological interaction history.

**Query parameters:**
- `source_type` (string): Filter by source
- `days_back` (int): Lookback period
- `limit` (int): Max items

### GET /api/crm/people/{id}/connections

Related people with overlap scores.

**Query parameters:**
- `relationship_type` (string): Filter by type (e.g., "coworker")
- `limit` (int): Max connections to return (default: 50)

**Response:**
```json
{
  "connections": [
    {
      "person_id": "uuid",
      "name": "Alex Chen",
      "company": "Example Corp",
      "relationship_type": "coworker",
      "shared_events_count": 42,
      "shared_threads_count": 5,
      "shared_messages_count": 0,
      "shared_whatsapp_count": 0,
      "shared_slack_count": 0,
      "relationship_strength": 91.5,
      "last_seen_together": "2023-02-26T14:00:00"
    }
  ],
  "count": 15
}
```

### GET /api/crm/people/{id}/strength

Detailed relationship strength components.

### GET /api/crm/people/{id}/source-entities

Get raw source entities linked to a person (low-level, paginated).

**Query parameters:**
- `limit` (int): Max entities to return (default: 500, max: 5000)
- `offset` (int): Pagination offset

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Name",
  "total_count": 49987,
  "returned_count": 500,
  "has_more": true,
  "source_entities": ["..."]
}
```

---

## Contact Sources, Split, Merge

### GET /api/crm/people/{id}/contact-sources

**Recommended for split UI.** Get aggregated contact sources (emails, phones, etc.) linked to a person.

Contact sources are the meaningful units for entity splitting — each represents a unique identifier (email address, phone number) rather than individual messages.

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Alex Chen",
  "total_contact_sources": 2,
  "total_observations": 49986,
  "contact_sources": [
    {
      "identifier": "alex.chen@example.com",
      "identifier_type": "email",
      "source_types": ["gmail", "calendar", "contacts"],
      "observation_count": 49984,
      "source_entity_ids": ["uuid1", "uuid2", "..."],
      "observed_names": ["Alex Chen", "Alex"],
      "first_seen": "2024-01-15T...",
      "last_seen": "2023-01-29T..."
    },
    {
      "identifier": "+15550123",
      "identifier_type": "phone",
      "source_types": ["imessage", "whatsapp"],
      "observation_count": 2,
      "source_entity_ids": ["uuid3", "uuid4"],
      "observed_names": ["Alex"],
      "first_seen": "2024-06-01T...",
      "last_seen": "2023-01-28T..."
    }
  ]
}
```

**Identifier types:**
- `email` — Email address (appears in gmail, calendar, contacts, etc.)
- `phone` — Phone number in E.164 format (appears in imessage, whatsapp, phone)
- `slack_user` — Slack workspace user ID
- `linkedin_profile` — LinkedIn profile URL
- `name_only` — Vault/Granola mentions with no email/phone

### POST /api/crm/people/split

Split source entities from one person to another.

**Request:**
```json
{
  "from_person_id": "uuid",
  "to_person_id": "uuid",
  "new_person_name": "New Person",
  "source_entity_ids": ["uuid1", "uuid2"],
  "create_overrides": true
}
```

Exactly one of `to_person_id` and `new_person_name` is supplied.

**Response:**
```json
{
  "status": "completed",
  "from_person_id": "uuid",
  "to_person_id": "uuid",
  "source_entities_moved": 5,
  "interactions_moved": 10,
  "overrides_created": 2
}
```

### GET /api/crm/link-overrides

List disambiguation rules that prevent future entity mis-linking.

### DELETE /api/crm/link-overrides/{id}

Delete a link override rule.

### POST /api/crm/people/merge

Merge two person records. Combines all interactions, relationships, and source entities from the secondary person into the primary person.

**Request:**
```json
{
  "primary_id": "uuid",
  "secondary_ids": ["uuid1", "uuid2"]
}
```

**Response:**
```json
{
  "status": "completed",
  "primary_id": "uuid",
  "merged_ids": ["uuid1", "uuid2"],
  "stats": {
    "interactions_updated": 156,
    "source_entities_updated": 12,
    "emails_merged": 3,
    "phones_merged": 1,
    "aliases_added": 2
  }
}
```

---

## Relationships and Graph

### GET /api/crm/network

Network graph data (nodes + edges). With `center_on`, this is a bounded
neighborhood — the strongest connections within `depth` hops, capped by
`max_nodes`, `max_second_degree_per_node`, and `max_edges` — not the full
relationship table (see
[crm-graph.md § Bounded Neighborhood](crm-graph.md#bounded-neighborhood)).

**Query parameters:**
- `center_on` (string): Person ID to center on. Required unless `allow_full_graph=true`.
- `depth` (int, 1–4, default 2): Graph depth
- `min_strength` (float, 0.0–1.0, default 0.0): Minimum node relationship strength. Currently a no-op regardless of value — validated to 0.0–1.0 while `relationship_strength` is 0–100.
- `category` (string): Filter by category. Best-effort for a centered request — see the docs linked above.
- `max_nodes` (int, 1–500, default 150): Total nodes in the response, including the center
- `max_second_degree_per_node` (int, 0–50, default 10): Second-(and deeper-)degree neighbors added per node at the previous depth
- `max_edges` (int, 1–20000, default 2000): Target maximum edges; every edge touching the center is always included even if that alone exceeds this
- `allow_full_graph` (bool, default false): Opt-in to load every person and relationship (no `center_on`); ignores the three caps above

**Response includes edge source breakdown:**
- `shared_events_count`
- `shared_threads_count`
- `shared_messages_count`
- `shared_whatsapp_count`
- `shared_slack_count`
- `is_linkedin_connection`

### GET /api/crm/relationship/{person_a_id}/{person_b_id}

Detailed edge data between two people.

### POST /api/crm/relationships/discover

Trigger full relationship discovery. Scans interactions to find/update relationships between people.

**Response:**
```json
{
  "status": "completed",
  "duration_seconds": 12.5,
  "relationships_created": 45,
  "relationships_updated": 120
}
```

---

## Strengths, Discovery, Statistics

### POST /api/crm/strengths/update

Recalculate relationship strength for all people.

**Response:**
```json
{
  "status": "completed",
  "updated": 542,
  "failed": 0,
  "total": 542
}
```

### GET /api/crm/discover

Get suggested connections and relationship insights for UI.

**Query parameters:**
- `person_id` (string, optional): Focus on specific person
- `limit` (int): Max suggestions to return

**Response:**
```json
{
  "suggested_connections": [
    {
      "person_a": {"id": "uuid", "name": "Alex"},
      "person_b": {"id": "uuid", "name": "Jordan"},
      "reason": "3 shared calendar events, 5 email threads",
      "confidence": 0.85
    }
  ],
  "network_insights": {
    "total_people": 542,
    "connected_people": 380,
    "bridge_people": ["uuid1", "uuid2"]
  }
}
```

### GET /api/crm/statistics

Dashboard stats (counts by category, source, strength distribution).

---

## Facts

### GET /api/crm/people/{id}/facts

Get extracted facts about a person (auto-extracted from interactions).

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Alex Chen",
  "facts": [
    {
      "id": "uuid",
      "category": "work",
      "content": "Works at Example Corp as VP Engineering",
      "confidence": 0.9,
      "source": "calendar:meeting-uuid",
      "created_at": "2023-01-15T...",
      "confirmed": false
    }
  ]
}
```

### POST /api/crm/people/{id}/facts/extract

Trigger fact extraction for a person using LLM. See [crm-people.md § Person Facts Pipeline](crm-people.md#person-facts-pipeline) for the multi-stage pipeline.

### PUT /api/crm/people/{id}/facts/{fact_id}

Update a fact's content or category.

### DELETE /api/crm/people/{id}/facts/{fact_id}

Delete a fact.

### POST /api/crm/people/{id}/facts/{fact_id}/confirm

Mark a fact as confirmed/verified.

---

## Review Queue

### GET /api/crm/review-queue

Get pending entity links requiring human review.

**Query parameters:**
- `min_confidence` (float): Minimum confidence threshold
- `limit` (int): Max items to return

### POST /api/crm/review-queue/{entity_id}/confirm

Confirm an entity link (mark as correct).

### POST /api/crm/review-queue/{entity_id}/reject

Reject an entity link (mark as incorrect, will be unlinked).

---

## Data Health

### GET /api/crm/data-health

Data coverage and sync health report.

### GET /api/crm/data-health/summary

Summary for UI display.

---

## Config

### GET /api/crm/config

Get CRM configuration values for the frontend (owner person ID, work email domain, partner ID, family default selected IDs). Reads from `LIFEOS_*` env vars — see [configuration.md](../../guides/configuration.md).

---

## Me Dashboard

### GET /api/crm/me/stats

Aggregate statistics for the owner's personal dashboard. Returns total people, emails, meetings, and messages across the CRM.

### GET /api/crm/me/timeline

Chronological interaction history for the owner. Returns ALL interactions across all people (since all interactions involve the owner).

**Query parameters:**
- `source_type` (string): Filter by source type (comma-separated for compound filters)
- `days_back` (int): Lookback period (default: 365)
- `date` (string): Filter to specific date (YYYY-MM-DD)
- `offset` (int): Pagination offset
- `limit` (int): Max results (default: 50)

### GET /api/crm/me/interactions

Aggregated interaction data for the "Me" dashboard. Returns pre-aggregated data for heatmaps, charts, trends, network growth, and messaging volume by Dunbar circle.

**Query parameters:**
- `days_back` (int): Days of history (default: 365, max: 3660)
- `trend_period` (string): Trend comparison period (week, month, quarter, year)
- `health_period` (string): Health score history period (month, quarter, year)

### GET /api/crm/me/interactions/span

Earliest and latest interaction dates (excluding self, hidden, and peripheral people — the same population `/me/interactions` aggregates) plus a suggested heatmap year count clamped to 1–10. The Me page calls this first to size its heatmap window instead of requesting a fixed 10 years and shrinking the display afterward.

```json
{
  "earliest": "2016-03-01T00:00:00+00:00",
  "latest": "2026-09-04T12:00:00+00:00",
  "years": 10
}
```

`earliest`/`latest` are `null` when there is no data.

---

## Family

### GET /api/crm/family/members

Get configured family members with relationship data.

### GET /api/crm/family/stats

Aggregate family statistics.

**Query parameters:**
- `member_ids` (string): Comma-separated person IDs to include

### GET /api/crm/family/timeline

Family interaction timeline across selected members. Filters by `person_id IN (...)` in SQL rather than loading every interaction in the window and filtering in Python.

**Query parameters:**
- `person_ids` (string): Comma-separated person IDs
- `source_type` (string): Filter by source type
- `days_back` (int): Lookback period
- `limit` (int): Max results

### GET /api/crm/family/interactions

Aggregated family interaction data for charts and heatmaps. Filters by `person_id IN (...)` in SQL. Unlike `/me/interactions`, this does not apply a "sent email only" rule — all email (sent and received) counts.

**Query parameters:**
- `person_ids` (string): Comma-separated person IDs
- `days_back` (int): Days of history

### GET /api/crm/family/channel-mix

Communication channel breakdown for family members. Shows distribution across email, calendar, messaging, etc.

**Query parameters:**
- `member_ids` (string): Comma-separated person IDs
- `days_back` (int): Lookback period

---

## Birthdays

### GET /api/crm/birthdays/today

Get all people with birthdays today.

### GET /api/crm/birthdays/all

Get all people with birthdays, grouped by date (MM-DD format).

---

## Sync Health

### GET /api/crm/sync/health

Get health status for all sync sources. Returns staleness, last sync time, and error status for each source.

### GET /api/crm/sync/health/summary

Summary of sync health across all sources. Returns counts of healthy, stale, and failed sources.

### GET /api/crm/sync/health/{source}

Get health status for a specific sync source.

### GET /api/crm/sync/errors

Get recent sync errors for debugging.

**Query parameters:**
- `source` (string): Filter by source
- `limit` (int): Max results (default: 50)

---

## Relationship Insights

### GET /api/crm/relationship/insights

Get relationship insights and patterns extracted from therapy notes and conversations.

**Query parameters:**
- `person_id` (string, optional): Focus on specific person (defaults to primary relationship)

**Response:**
```json
{
  "insights": [
    {
      "id": "uuid",
      "person_id": "uuid",
      "category": "focus_areas",
      "text": "Lead with feelings before facts in conflicts",
      "source_title": "Couples therapy 20230120",
      "source_link": "obsidian://...",
      "source_date": "2023-01-20T00:00:00",
      "confirmed": true,
      "created_at": "2023-02-01T19:54:45",
      "category_icon": "📝"
    }
  ],
  "last_generated": "2023-02-01T23:56:20",
  "confirmed_count": 7,
  "unconfirmed_count": 33
}
```

**Categories:** `focus_areas`, `recurring_themes`, `relationship_strengths`, `growth_patterns`, `for_me`, `for_partner`, `ai_suggestions`.

### POST /api/crm/relationship/insights/generate

Generate new relationship insights using Claude. Keeps confirmed insights, regenerates unconfirmed ones.

**Query parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `category` (string, optional): Only regenerate for this category

### POST /api/crm/relationship/insights/{insight_id}/confirm

Mark an insight as confirmed. Confirmed insights persist across regenerations.

### DELETE /api/crm/relationship/insights/{insight_id}

Delete/dismiss an insight.

---

## Tone Analysis

### POST /api/crm/relationship/tone-analysis

Analyze tone/sentiment in iMessage conversations over time. Samples messages monthly and uses Claude to classify emotional warmth (0–100 scale).

**Query parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `months` (int): Months to analyze (default: 12)

### POST /api/crm/relationship/tone-analysis-detailed

Detailed tone analysis with separate scores for the user and their partner. Messages are bucketed by calendar month first, then by week within that month (so a week straddling a month boundary is never double-counted), and each stale month gets one overall score per person.

Results are persisted per person and month (see [crm-analytics.md](crm-analytics.md#tone-analysis-apis)). Freshness is checked with a lightweight per-month count query that loads no interaction rows, so a fully-cached response touches storage only and returns in well under 200ms regardless of how many interactions exist in the window; only when at least one month is stale does the handler load and bucket the actual messages. Stale months are recomputed in one or more LLM calls, chunked at a handful of months per call so a single slow or timed-out call can't affect the whole window, and every chunk's result is saved as soon as it completes. Concurrent requests for the same person are serialized (with a short timeout) so two open tabs don't both pay for the same call.

**Query parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `months` (int): Months to analyze (default: 12)
- `refresh` (bool, optional): Force recomputation of every month in the window, bypassing the freshness cache (default: false)

**Response:** a stale month that couldn't be recomputed this request (the LLM failed, was unavailable, or its response omitted that month) is never discarded -- it comes back with its last stored score and `"status": "stale"` if one exists, or `"status": "error"` (a neutral placeholder score, not a real one) only when nothing was ever stored for it. `null`/omitted status means the score is current. The Relationship page's Tone Evolution chart plots a stale month as a dimmed point and an error month as a gap with a "not analysed" marker.

---

## Slack Integration

### GET /api/crm/slack/status

Get Slack OAuth integration status (configured, connected, workspaces).

### GET /api/crm/slack/oauth/start

Start Slack OAuth flow. Returns the authorization URL.

### GET /api/crm/slack/callback

Handle Slack OAuth callback. Exchanges authorization code for access token.

### POST /api/crm/slack/sync

Sync Slack users to the CRM. Creates SourceEntity records for workspace users.

**Query parameters:**
- `workspace_id` (string): Workspace to sync (default: "default")

### DELETE /api/crm/slack/disconnect

Disconnect a Slack workspace. Removes the stored OAuth token.

**Query parameters:**
- `workspace_id` (string): Workspace to disconnect (default: "default")

---

## Contacts Sync

### GET /api/crm/contacts/status

Get Apple Contacts integration status (availability and authorization).

### POST /api/crm/contacts/sync

Sync Apple Contacts to the CRM. Creates SourceEntity records for all contacts. On macOS, reads directly via Contacts framework. On Linux, imports via Apple Data Agent exports — see [ADR-010](../../adr/010-apple-data-agent.md).

---

## Related Documents

- [api-reference.md](api-reference.md) — Non-CRM API endpoints (chat, search, Google, messaging, tasks, photos, Monarch, admin, etc.)
- [mcp-tools.md](mcp-tools.md) — MCP tool catalog (the canonical home — was previously duplicated in api-reference.md)
- [crm-ui.md](crm-ui.md) — CRM index pointing at the four product sub-specs
- [crm-people.md](crm-people.md) — Consumer view of the people endpoints above
- [crm-interactions.md](crm-interactions.md) — Consumer view of timeline + source integrations
- [crm-graph.md](crm-graph.md) — Consumer view of the graph and relationship model
- [crm-analytics.md](crm-analytics.md) — Consumer view of the family/me/birthday/relationship dashboards
- [data-model.md](data-model.md) — Two-tier data model semantics behind every endpoint here
