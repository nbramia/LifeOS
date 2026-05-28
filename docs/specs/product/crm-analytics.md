# Personal CRM — Dashboards & Insights

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-05-27

The aggregated views that sit alongside the people list: Family Dashboard (`/crm#family`), Me Dashboard (`/me`, the CRM landing page), Birthdays Page (`/birthdays`), and Relationship Dashboard (`/relationship`). Each surfaces interaction patterns and insights derived from the CRM data model.

See [crm-ui.md](crm-ui.md) for the CRM index and the sibling specs that cover people, interactions, and the graph.

---

## Table of Contents

1. [Family Dashboard](#family-dashboard)
2. [Me Dashboard](#me-dashboard)
3. [Birthdays Page](#birthdays-page)
4. [Relationship Dashboard](#relationship-dashboard)

---

## Family Dashboard

`URL: /crm#family`

Aggregated view of interactions across multiple selected family members. Lets you track engagement with family as a group and quickly see which member you've been out of touch with.

### Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Family Dashboard                          [Select family members... ▼] │
├─────────────────────────────────────────────────────────────────────────┤
│  Hero Stats (Lifetime Totals)                                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │ 📧 1.2K  │  │ 💬 5.6K  │  │ 📞 234   │  │ 📅 156   │                  │
│  │ emails   │  │ messages │  │ calls    │  │ meetings │                  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘                  │
│                                                                          │
│  [Overview] [Timeline]                                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  365-Day Interaction History                              [Years: 10 ▼]  │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  █ █ █   █ █ █ █   █   █ █   █ █ █ █   (heatmap)               │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Interaction Volume Over Time                                            │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  ▄█▄ ▄█▄ ▄█▄ ▄█▄ ▄█▄ (volume chart)                            │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Family Contact Health           Relationship Trends                     │
│  ┌────────────────────────────┐  ┌────────────────────────────────┐     │
│  │ David Chen      ██████░░░  │  │ Trends chart                    │     │
│  │ Maria Chen      ████░░░░░  │  │                                  │     │
│  │ Sofia Chen      ███░░░░░░  │  └────────────────────────────────┘     │
│  └────────────────────────────┘                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key behaviors

- **Hero stats show lifetime totals** from `PersonEntity` counters (`email_count`, `meeting_count`, `message_count`) — independent of any date range.
- **Hero stat pills are clickable** — they navigate to the Timeline tab filtered by the corresponding source type.
- **Year dropdown** controls the heatmap and volume chart range (up to 10 years) but **does not** affect hero stats.
- **Heatmap squares** are clickable — they navigate to the Timeline filtered to that date.
- **Default family members** are configurable; selection persists in `localStorage` as `familySelectedIds`.

### Family Stats API

**Endpoint:** `GET /api/crm/family/stats?person_ids=<csv>`

Returns lifetime totals across the selected family members.

```json
{
  "total_emails": 1247,
  "total_meetings": 156,
  "total_messages": 5623
}
```

Reads directly from `PersonEntity` counters — fast (no interaction queries).

### Family Interactions API

**Endpoint:** `GET /api/crm/family/interactions?person_ids=<csv>&days_back=365`

Used by the heatmap and volume chart. Aggregates interactions across all selected family members.

```json
{
  "daily": [
    {"date": "2026-01-15", "total": 5, "sources": {"gmail": 2, "imessage": 3}}
  ],
  "by_source": {
    "gmail": 450,
    "imessage": 2300,
    "calendar": 156
  },
  "total_interactions": 4500,
  "date_range": {"start": "2025-01-15", "end": "2026-01-15"}
}
```

`days_back` supports multi-year lookback (up to 10 years).

### Family Timeline API

**Endpoint:** `GET /api/crm/family/timeline?person_ids=<csv>&source_type=<opt>&date=<opt>&limit=100`

Returns interactions for any of the selected family members. Includes a `person_name` field so the UI can identify which family member each row belongs to.

```json
{
  "items": [
    {
      "id": "interaction-uuid",
      "person_id": "person-uuid",
      "person_name": "David Chen",
      "timestamp": "2026-01-15T10:30:00Z",
      "source_type": "imessage",
      "title": "Text conversation",
      "snippet": "Happy birthday!",
      "source_link": "imessage://+15550123"
    }
  ],
  "count": 50,
  "has_more": true
}
```

Supports filtering by `source_type` and/or a specific `date` (drives heatmap-click navigation).

### Family Member Selector

```
┌─────────────────────────────────────────┐
│ Select family members...            ▼   │
├─────────────────────────────────────────┤
│ [Select All] [Clear All]                │
├─────────────────────────────────────────┤
│ ☑ David Chen                            │
│ ☑ Maria Chen                            │
│ ☑ Sofia Chen                            │
│ ☑ Eli Chen                              │
└─────────────────────────────────────────┘
```

Multi-select dropdown with Select All / Clear All. Selection persists to `localStorage`. Adding or removing a family member re-fetches all dashboard data.

### Clickable hero stats

Each hero-stat pill maps to a Timeline source-type filter:

| Pill | Timeline filter |
|------|-----------------|
| 📧 Emails | `source_type=gmail` |
| 💬 Messages | `source_type` ∈ {`imessage`, `whatsapp`, `signal`, `slack`} |
| 📞 Calls | `source_type=phone` |
| 📅 Meetings | `source_type=calendar` |

---

## Me Dashboard

`URL: /me` (default CRM landing page; `/crm` redirects here)

Owner's network health at a glance. Replaces the generic people list as the entry point.

### Components

- **Health score** — overall network engagement metric.
- **Neglected contacts** — people with declining interaction frequency.
- **Network growth** — new contacts over time.
- **Top contacts** — most-interacted people with strength indicators.
- **Messaging by circle** — interaction volume broken down by Dunbar circle (see [crm-people.md § Dunbar Circles](crm-people.md#dunbar-circles)).
- **Trends** — configurable trend period for interaction patterns.

### API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/crm/me/stats` | Aggregated owner stats (interaction counts, health score) |
| `GET /api/crm/me/timeline` | Owner's interaction timeline, paginated |
| `GET /api/crm/me/interactions` | Interaction data with trend and health-period support (heatmap, volume chart, trend visualizations) |

---

## Birthdays Page

`URL: /birthdays`

Dedicated view showing a 12-month heatmap of contact birthdays and a chronological list of upcoming birthdays.

### Components

- **Birthday heatmap** — 12-month calendar grid with days colored when contacts have birthdays. Hovering shows names; today's date is highlighted.
- **Birthday timeline** — chronological list of upcoming birthdays with filters.

### API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/crm/birthdays/today` | Contacts whose birthday matches today's date (drives the toast notification) |
| `GET /api/crm/birthdays/all` | All contacts with known birthdays |

### Birthday toast notification

When contacts have birthdays matching today's date, a dismissible toast banner appears on CRM page load. The toast shows once per day (tracked via `localStorage`). Backed by `GET /api/crm/birthdays/today`; UI in `web/crm.html`.

---

## Relationship Dashboard

`URL: /relationship`

Therapy-informed insights and communication visualizations for a primary relationship (e.g., partner). Identified by `LIFEOS_PARTNER_NAME` and adjacent settings (see [configuration.md § Relationships](../../guides/configuration.md#relationships)).

### Therapy insights

Three-column grid:

| Panel | Content |
|-------|---------|
| **For Me** | Commitments and things to work on (extracted from therapy notes). |
| **For Partner** | Things the partner is working on. |
| **Fresh Ideas** | AI-generated therapist-style suggestions. |
| **Growth Patterns** | How the relationship has improved over time. |
| **Recurring Themes** | Patterns to watch. |
| **Strengths** | What's working well. |

Each panel has a refresh button to re-extract insights on demand.

### Communication visualizations

| Card | What it shows |
|------|---------------|
| **iMessage Dynamics** | Who initiates conversations and volume balance. |
| **Tone Evolution** | Emotional warmth over time, with view modes (combined / user-only / partner-only / both). |
| **Interaction Intensity** | Daily connection rhythm over 12 months. |
| **Weekly Rhythm** | Peak connection days of the week. |
| **Beyond Texting** | Monthly activity breakdown by channel (email, calendar, etc.). |
| **Interaction Depth** | Quick texts vs deep conversations by time of day. |

### Tone analysis APIs

| Endpoint | Purpose |
|----------|---------|
| `POST /api/crm/relationship/tone-analysis` | Aggregated tone analysis over configurable months (samples up to 20 messages per month). |
| `POST /api/crm/relationship/tone-analysis-detailed` | Separate user/partner tone scores (samples up to 10 messages per week per person). |

---

## Related Documents

- [crm-ui.md](crm-ui.md) — CRM index
- [crm-people.md](crm-people.md) — Person model, Dunbar circles, fact extraction (feeds dashboards' "neglected" / "top contacts" logic)
- [crm-interactions.md](crm-interactions.md) — Interaction source ingestion (the dashboards aggregate over these)
- [crm-graph.md](crm-graph.md) — Relationship graph (some dashboards link out to the graph view)
- [api-reference.md](api-reference.md) — Full HTTP endpoint catalog
- [configuration.md](../../guides/configuration.md) — `LIFEOS_PARTNER_NAME`, `LIFEOS_THERAPIST_PATTERNS`, `LIFEOS_PERSONAL_RELATIONSHIP_PATTERNS`
