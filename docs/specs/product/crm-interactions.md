# Personal CRM — Interactions & Source Integrations

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-09-04

The interaction timeline (chronological view of every observed touchpoint with a person) and the data-source integrations that feed it: Gmail, Calendar, iMessage, Apple Contacts, Slack, WhatsApp, Signal.

See [crm-ui.md](crm-ui.md) for the CRM index and the sibling specs that cover people, the graph, and dashboards.

---

## Table of Contents

1. [Data Flow](#data-flow)
2. [Timeline View](#timeline-view)
3. [Timeline API](#timeline-api)
4. [Slack Integration](#slack-integration)
5. [Apple Contacts Integration](#apple-contacts-integration)
6. [WhatsApp & Signal Import](#whatsapp--signal-import)

---

## Data Flow

```
Data Sources                Entity Resolution              CRM Data
──────────────             ──────────────────             ────────────
Gmail emails    ─┐
Calendar events  │         ┌──────────────────┐         ┌─────────────┐
iMessage texts   ├────────▶│  EntityResolver  │────────▶│ PersonEntity│
Vault mentions   │         │  (email/phone    │         │ Interaction │
LinkedIn CSV     │         │   anchor + fuzzy │         │ Relationship│
Phone Contacts   │         │   name)          │         └─────────────┘
Slack users    ──┤         └──────────────────┘
Apple Contacts ──┤                                       CRM UI
WhatsApp export ─┤                                    ┌─────────────┐
Signal export  ──┘                                    │  /crm page  │
                                                      │ - People    │
                                                      │ - Timeline  │
                                                      │ - Graph     │
                                                      └─────────────┘
```

Every observation from a source produces one `SourceEntity` row plus one `Interaction` row attributed to a canonical `PersonEntity`. The interaction is the unit displayed in the timeline.

For the model semantics see [data-model.md](data-model.md) and [ADR-003](../../adr/003-two-tier-data-model.md). For the sync pipeline that ingests sources see [data-and-sync.md](../technical/data-and-sync.md).

---

## Timeline View

Chronological list of every interaction with the selected person.

```
┌─────────────────────────────────────────────────────────────┐
│  Timeline                                    [All sources ▼]│
├─────────────────────────────────────────────────────────────┤
│  Today                                                      │
│  ├─ 💬 10:30 AM  Text conversation                          │
│  │  "Hey, are you coming home for dinner?"                  │
│                                                             │
│  Yesterday                                                  │
│  ├─ 📅 3:00 PM   Doctor appointment                         │
│  │  Annual checkup · [Open in Calendar]                     │
│  ├─ 📧 11:15 AM  Re: Weekend plans                          │
│  │  "Sounds good! Let's do brunch at 11" · [Open in Gmail]  │
│                                                             │
│  Jan 25                                                     │
│  ├─ 📝 Mentioned in "Daily Note 2026-01-25"                 │
│  │  Discussed vacation plans with Alex · [Open Note]        │
│                                                             │
│  [Load more...]                                             │
└─────────────────────────────────────────────────────────────┘
```

Behavior:
- Interactions are grouped by date.
- Each row shows icon, time, title, snippet (truncated, no HTML).
- Source-type filter dropdown narrows the list.
- Clicking an interaction opens the source link — `mailto:` / Google Calendar URL / `obsidian://` / `imessage://` etc.
- Infinite scroll loads older interactions.
- On the Me and Family timelines (`/me`, `/crm#family` — see [crm-analytics.md](crm-analytics.md)), an interaction still attributed to a person id that was later merged into another person displays under the surviving person's name, following the merge chain the same way a direct person lookup does. Previously such rows displayed "Unknown".

---

## Timeline API

**Endpoint:** `GET /api/crm/people/{id}/timeline`

| Parameter | Type | Description |
|-----------|------|-------------|
| `days` | int | Lookback period in days (default: 90) |
| `source` | string | Filter by source type (`gmail`, `calendar`, `imessage`, `vault`, etc.) |
| `limit` | int | Max items (default: 50) |

**Response shape:**

```json
{
  "items": [
    {
      "id": "interaction-uuid",
      "timestamp": "2026-01-27T10:30:00Z",
      "source_type": "imessage",
      "title": "Text conversation",
      "snippet": "Hey, are you coming home for dinner?",
      "source_link": "imessage://+15550123"
    },
    {
      "id": "interaction-uuid",
      "timestamp": "2026-01-26T15:00:00Z",
      "source_type": "calendar",
      "title": "Doctor appointment",
      "snippet": "Annual checkup",
      "source_link": "https://calendar.google.com/..."
    }
  ],
  "count": 50,
  "has_more": true,
  "total_interactions": 847
}
```

Newest first. Snippets are sanitized (no HTML, truncated to display width).

---

## Slack Integration

OAuth integration with Slack to sync workspace users and message history.

**Configuration:** `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI` in `.env` (see [configuration.md § Slack](../../guides/configuration.md#slack)). For direct-token auth (alternative to OAuth flow), use `SLACK_USER_TOKEN` + `SLACK_TEAM_ID`.

**OAuth scopes required:** `users:read`, `users:read.email`, `channels:history`, `im:history`.

**API endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /api/crm/slack/status` | Check connection status |
| `GET /api/crm/slack/oauth/start` | Get OAuth URL |
| `GET /api/crm/slack/callback` | OAuth callback |
| `POST /api/crm/slack/sync` | Sync users + messages |

**Entity resolution behavior:**

- Slack users are matched to existing `PersonEntity` rows by email.
- New entities are created for users with no matching email.
- `"slack"` is added to the person's `sources` list.
- Slack messages produce `Interaction` rows that appear in the timeline.
- 1:1 DM conversations are counted in `shared_slack_count` on the corresponding `Relationship` row (see [crm-graph.md § Extended Relationship Data Model](crm-graph.md#extended-relationship-data-model)).

---

## Apple Contacts Integration

Reads from the local Apple Contacts database to enhance person records with names, emails, phones, and company.

**Platform:**
- **macOS:** Direct via `pyobjc-framework-Contacts`.
- **Linux:** Via the [Apple Data Agent](../../adr/010-apple-data-agent.md) (nightly rsync from a Mac that holds Full Disk Access).

**API endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /api/crm/contacts/status` | Check availability and TCC authorization |
| `POST /api/crm/contacts/sync` | Sync contacts |

**Entity resolution priority (in order):**

1. Match by email.
2. Match by phone.
3. Match by exact name.
4. Create a new entity if no match.

`"contacts"` (or `"apple_contacts"`) is added to the person's `sources` list. Multiple phone numbers per contact are supported.

---

## WhatsApp & Signal Import

Parse exported chat files from WhatsApp (`.txt`) and Signal (`.json`).

**Endpoint:** `POST /api/crm/sources/import?source_type=whatsapp|signal` — accepts a file upload.

**WhatsApp format:**

```
[12/1/2024, 10:30:15 AM] Alex Chen: Hello!
[12/1/2024, 10:31:00 AM] Sam: Hi Alex!
```

**Signal format:**

```json
{
  "conversations": [...],
  "messages": [...]
}
```

**Behavior:**

- Participants extracted from messages.
- Phone numbers normalized to E.164.
- Message counts tracked per participant.
- First / last message timestamps captured.
- Entities created or matched for participants.
- Import statistics returned in the response.

---

## Related Documents

- [crm-ui.md](crm-ui.md) — CRM index
- [crm-people.md](crm-people.md) — People list/detail, entity model, fact extraction
- [crm-graph.md](crm-graph.md) — Multi-source relationship tracking that consumes these interactions
- [crm-analytics.md](crm-analytics.md) — Dashboards that aggregate over interactions
- [data-model.md](data-model.md) — SourceEntity / Interaction / PersonEntity semantics
- [data-and-sync.md](../technical/data-and-sync.md) — Nightly sync pipeline that ingests every source above
- [ADR-010: Apple Data Agent](../../adr/010-apple-data-agent.md) — How iMessage, contacts, calls reach the Linux server
