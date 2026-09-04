# Personal CRM — People

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-09-04

People management: the list and detail views at `/crm`, the contact-source model behind entity splitting/merging, the relationship-strength score that drives ranking and Dunbar circles, and the multi-stage fact-extraction pipeline that surfaces memorable personal details.

See [crm-ui.md](crm-ui.md) for the CRM index and the sibling specs that cover interactions, the graph, and dashboards.

---

## Table of Contents

1. [People Listing API](#people-listing-api)
2. [Person Detail API](#person-detail-api)
3. [Relationship Strength Scoring](#relationship-strength-scoring)
4. [Dunbar Circles](#dunbar-circles)
5. [People List View](#people-list-view)
6. [Person Detail View](#person-detail-view)
7. [Two-Tier Entity Model](#two-tier-entity-model)
8. [Contact Sources API](#contact-sources-api)
9. [Split Operation](#split-operation)
10. [Link Overrides](#link-overrides)
11. [Split Modal UI](#split-modal-ui)
12. [Merge Toolbar](#merge-toolbar)
13. [Person Facts Pipeline](#person-facts-pipeline)

---

## People Listing API

**Endpoint:** `GET /api/crm/people`

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query (name, email, company) |
| `category` | string | Filter by category: `work`, `personal`, `family` |
| `source` | string | Filter by source: `gmail`, `calendar`, `linkedin`, etc. |
| `sort` | string | Sort field: `name`, `last_seen`, `interaction_count`, `strength` |
| `order` | string | Sort order: `asc`, `desc` (default: `desc`) |
| `limit` | int | Results per page (default: 50, max: 200) |
| `offset` | int | Pagination offset |

Default sort is by `relationship_strength` descending.

**Response shape:**

```json
{
  "people": [
    {
      "id": "uuid",
      "canonical_name": "Alex Chen",
      "display_name": "Alex Chen",
      "emails": ["alex.chen@example.com"],
      "phone_numbers": ["+15550123"],
      "company": null,
      "position": null,
      "category": "personal",
      "sources": ["phone_contacts", "gmail", "calendar", "imessage"],
      "interaction_count": 847,
      "meeting_count": 23,
      "email_count": 156,
      "mention_count": 12,
      "first_seen": "2024-01-15T...",
      "last_seen": "2026-01-27T...",
      "relationship_strength": 0.92
    }
  ],
  "count": 50,
  "total": 2236,
  "offset": 0,
  "has_more": true
}
```

`interaction_count` is the sum of email + meeting + mention + iMessage counts. `sources` is the list of distinct data sources where the person appears, derived from the underlying `SourceEntity` rows.

---

## Person Detail API

**Endpoint:** `GET /api/crm/people/{id}`

Returns full PersonEntity fields including emails, phones, vault contexts, tags, birthday, notes, source list, and per-source interaction counts.

```json
{
  "id": "uuid",
  "canonical_name": "Alex Chen",
  "emails": ["alex.chen@example.com"],
  "phone_numbers": ["+15550123"],
  "company": null,
  "category": "personal",
  "vault_contexts": ["Personal/Relationship/"],
  "tags": [],
  "birthday": "08-15",
  "notes": "",
  "sources": ["phone_contacts", "gmail", "calendar", "imessage"],
  "interaction_count": 847,
  "meeting_count": 23,
  "email_count": 156,
  "imessage_count": 656,
  "mention_count": 12,
  "first_seen": "2024-01-15T...",
  "last_seen": "2026-01-27T...",
  "relationship_strength": 0.92,
  "aliases": ["AC", "A. Chen"]
}
```

Birthday-toast behavior is covered in [crm-analytics.md § Birthdays Page](crm-analytics.md#birthdays-page).

---

## Relationship Strength Scoring

Each person carries a `relationship_strength` score in `[0.0, 1.0]` computed from three signals:

```
strength = (recency × 0.3) + (frequency × 0.4) + (diversity × 0.3)

where:
  recency   = max(0, 1 − days_since_last_interaction / 90)
  frequency = min(1, interactions_in_90_days / 20)
  diversity = unique_source_types / total_source_types
```

Examples:

| Person | Days Since | Interactions (90d) | Sources | Recency | Frequency | Diversity | Strength |
|--------|-----------:|-------------------:|--------:|--------:|----------:|----------:|---------:|
| Alex   | 1 | 50 | 4 (gmail, cal, imsg, vault) | 0.99 | 1.00 | 0.67 | 0.90 |
| Sam    | 3 | 30 | 3 (gmail, cal, vault) | 0.97 | 1.00 | 0.50 | 0.84 |
| Older friend | 60 | 2 | 1 (gmail) | 0.33 | 0.10 | 0.17 | 0.19 |

Strength is persisted on `PersonEntity`, updated when new interactions are recorded, and used as the default sort key in the people list.

---

## Dunbar Circles

Contacts are assigned to one of 8 Dunbar circles (`0` = closest, `6` = outermost, `7` = peripheral). Circles drive filtering, color coding, and prioritization across the CRM.

- Circles `0–6` are derived from the strength ranking among non-peripheral contacts.
- Circle `7` is anyone below `PERIPHERAL_THRESHOLD`.
- Computed in `api/services/relationship_metrics.py`.
- Operator overrides via `CIRCLE_OVERRIDES_BY_ID` in `config/relationship_weights.py` (the file is gitignored — see [configuration.md](../../guides/configuration.md)).

**UI integration:**
- Dunbar badge on people cards (color-coded per circle).
- Dunbar indicator in the person-detail header.
- Circle filter dropdown in the [graph view](crm-graph.md#graph-visualization).
- Graph color-mode toggle: category colors vs Dunbar colors.

---

## People List View

Main CRM page at `/crm`.

```
┌─────────────────────────────────────────────────────────────┐
│  LifeOS CRM                    [Search...] [👥 2,236 people]│
├───────────────────────┬─────────────────────────────────────┤
│ [All] [Work] [Personal]                                     │
├───────────────────────┤                                     │
│  ┌─────────────────┐  │  Select a person to view details    │
│  │ 🔵 Alex Chen    │  │                                     │
│  │ Personal · 847  │  │                                     │
│  │ ████████████░░ │  │                                     │
│  └─────────────────┘  │                                     │
│  ┌─────────────────┐  │                                     │
│  │ 🔵 Sam          │  │                                     │
│  │ Example Corp    │  │                                     │
│  │ ████████░░░░░░ │  │                                     │
│  └─────────────────┘  │                                     │
└───────────────────────┴─────────────────────────────────────┘
```

Behavior:
- Header shows total people count.
- Real-time search (< 300 ms), debounced; a superseded query's response is discarded so the list always reflects what's currently typed, even if an earlier search's response arrives later (#874).
- Category tabs (Work / Personal / Family).
- Cards show avatar, name, company/category, interaction count, strength bar.
- Cards sorted by relationship strength by default.
- Infinite scroll loads more people.
- Mobile-responsive layout.

---

## Person Detail View

Detail panel that slides in when a person is selected. Shows contact info (emails, phones), interaction statistics (per-source counts), source badges, last-seen date, relationship-strength indicator, notes textarea (saves on blur), and tags (add/remove inline).

```
┌─────────────────────────────────────────────────────────────┐
│  Alex Chen                                       [← Back]   │
│  alex.chen@example.com · +1 555-0123                        │
│  Personal · 847 interactions · Last seen: Today             │
├─────────────────────────────────────────────────────────────┤
│  [Overview] [Timeline] [Connections] [Graph]                │
├─────────────────────────────────────────────────────────────┤
│  Contact Information                                        │
│  📧 alex.chen@example.com                                  │
│  📱 +1 555-0123                                            │
│                                                             │
│  Statistics                                                 │
│  📧 156 emails · 📅 23 meetings · 💬 656 texts · 📝 12 mentions │
│                                                             │
│  Sources                                                    │
│  [gmail] [calendar] [imessage] [phone_contacts] [vault]    │
│                                                             │
│  Notes                                                      │
│  [                                                  ]       │
└─────────────────────────────────────────────────────────────┘
```

Timeline, Connections, and Graph tabs are covered in their respective specs ([crm-interactions.md](crm-interactions.md), [crm-graph.md](crm-graph.md)).

---

## Two-Tier Entity Model

The CRM uses a two-tier model for managing people:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           TIER 1: Source Entities                        │
│  Raw observations from data sources. Each message/event creates one.     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Gmail Message #123     Calendar Event #456     iMessage +15550123      │
│  ├─ observed_email      ├─ observed_email       ├─ observed_phone       │
│  └─ observed_name       └─ observed_name        └─ observed_name        │
│                                                                          │
│         │                       │                       │                │
│         └───────────────────────┼───────────────────────┘                │
│                                 │                                        │
│                                 ▼                                        │
│                    ┌────────────────────────┐                            │
│                    │   Entity Resolution    │                            │
│                    │   (email/phone anchor) │                            │
│                    └────────────────────────┘                            │
│                                 │                                        │
│                                 ▼                                        │
├─────────────────────────────────────────────────────────────────────────┤
│                        TIER 2: PersonEntity                              │
│  Unified person record. Multiple identifiers link to one person.         │
├─────────────────────────────────────────────────────────────────────────┤
│  PersonEntity: "Alex Chen"                                              │
│  ├─ emails: ["alex.chen@example.com"]                                  │
│  ├─ phone_numbers: ["+15550123", "+15550987"]                          │
│  ├─ sources: [gmail, calendar, imessage, whatsapp, ...]                 │
│  └─ interaction_count: 50,000+                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

The semantic background is in [ADR-003](../../adr/003-two-tier-data-model.md) and [data-model.md](data-model.md).

**Resolution priority:**

1. **Email exact match** — `_email_index[email]`
2. **Phone exact match** — `_phone_index[phone]`
3. **Name fuzzy match** with context boost
4. **Create new** if no match

The **identifier** (email or phone) is the resolution anchor, not the source type. Once `alex.chen@example.com` is linked to Person A, every future observation with that email links to Person A.

---

## Contact Sources API

**Endpoint:** `GET /api/crm/people/{id}/contact-sources`

Returns aggregated **contact sources** — the splittable units behind a person. Each contact source is one identifier (email, phone, Slack user, LinkedIn profile, or vault-only name) with all the underlying `SourceEntity` rows that use it.

| Contact Source | Example |
|----------------|---------|
| Email address | `alex.chen@example.com` across Gmail, Calendar, Contacts |
| Phone number | `+15550123` across iMessage, WhatsApp, Phone |
| Slack user | `U012345` in Slack |
| LinkedIn profile | LinkedIn connection |
| Name only | Vault/Granola mentions (no email/phone) |

**Why aggregate?** A person like Alex Chen might have 50,000+ individual `SourceEntity` rows. For entity-management UX, what matters is identifiers, not individual messages.

```json
{
  "person_id": "uuid",
  "person_name": "Alex Chen",
  "contact_sources": [
    {
      "identifier": "alex.chen@example.com",
      "identifier_type": "email",
      "source_types": ["gmail", "calendar", "contacts", "linkedin"],
      "observation_count": 49984,
      "source_entity_ids": ["uuid1", "uuid2", "..."],
      "observed_names": ["Alex Chen", "AC"],
      "first_seen": "2024-01-15T...",
      "last_seen": "2026-01-29T..."
    },
    {
      "identifier": "+15550123",
      "identifier_type": "phone",
      "source_types": ["imessage", "whatsapp", "phone"],
      "observation_count": 2,
      "source_entity_ids": ["uuid3", "uuid4"],
      "observed_names": ["AC"],
      "first_seen": "2024-06-01T...",
      "last_seen": "2026-01-28T..."
    }
  ],
  "total_contact_sources": 2,
  "total_observations": 49986
}
```

The modal loads in < 500 ms even for people with 50K+ observations because it operates on aggregated contact-source rows, not individual `SourceEntity` rows.

---

## Split Operation

**Endpoint:** `POST /api/crm/people/split`

Moves contact sources from one person to another (or to a newly created person).

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

**What happens on split:**

1. All `SourceEntity` rows with selected IDs move to the target person.
2. Related interactions move to the target person.
3. `PersonEntity.emails` / `phone_numbers` lists update on both people.
4. The email and phone resolution indexes update to point to the new owner.
5. If `create_overrides=true`, [link overrides](#link-overrides) are created to prevent future mis-linking.

**Example.** Two people both named "John" are incorrectly merged. One uses `john@example.com`, the other `john.smith@example.net`. Open the split modal, select the contact source `john.smith@example.net`, split to a new person "John Smith". Future emails from `john.smith@example.net` resolve to John Smith.

---

## Link Overrides

**Endpoint:** `GET /api/crm/link-overrides`

When a split is performed with `create_overrides=true`, the system writes **link override** rules that prevent fuzzy name matching from re-merging the entity.

**Override types:**

- **Email-based** — "email `x@y.com` always links to Person B".
- **Name + context** — "name `John` in `Work/Acme/` context links to Person A".

Without overrides, fuzzy name matching might re-link a split entity to the wrong person. Overrides make the split durable.

---

## Split Modal UI

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Split Sources from Alex Chen                                  [✕]      │
├─────────────────────────────────────────────────────────────────────────┤
│  Select contact sources to move:                                         │
│                                                                          │
│  ☐ 📧 Email: alex.chen@example.com                                       │
│     Sources: gmail, calendar, contacts · 49,984 observations             │
│                                                                          │
│  ☐ 📱 Phone: +15550123                                                   │
│     Sources: imessage, whatsapp · 2 observations                         │
│                                                                          │
│  ☐ 📱 Phone: +15550987                                                   │
│     Sources: whatsapp · 1 observation                                    │
├─────────────────────────────────────────────────────────────────────────┤
│  Move to:                                                                │
│  ○ Existing person: [Search...]                                          │
│  ○ New person: [Name...]                                                 │
│                                                                          │
│  ☑ Create override rules (prevents future mis-linking)                  │
│                                                                          │
│                                         [Cancel]  [Split 0 sources]      │
└─────────────────────────────────────────────────────────────────────────┘
```

Identifier-type icons: 📧 email, 📱 phone, 💬 Slack, 💼 LinkedIn. Each row shows the source list and observation count.

---

## Merge Toolbar

Multi-select people from the list view and perform bulk merge or hide operations.

- Checkbox on each person card for multi-selection.
- A floating toolbar appears when 1+ people are selected, showing the count and action buttons.
- **Actions:** Merge selected (combines into one PersonEntity), Hide selected (soft-deletes), Clear selection.

---

## Person Facts Pipeline

The fact-extraction pipeline surfaces **memorable personal details** about contacts (pet names, hobbies, family members, preferences, anecdotes) — not biographical or professional info that's findable elsewhere.

**Endpoint:** `POST /api/crm/people/{id}/facts/extract?model=haiku|sonnet`

- `haiku` (`claude-haiku-4-5`) — fast, low cost (~$0.01/person). Used for auto-extraction on person load.
- `sonnet` (`claude-sonnet-4-5`) — higher quality (~$0.15/person). Used when the operator clicks "Extract Facts".

### Three-stage pipeline

```
Stage 1 — Filtering (local LLM via llm_client.py)
  For each interaction (with message-context window for chat sources),
  ask: "Does this contain memorable personal facts about {person}?"
  → High-signal interaction shortlist

Stage 2 — Deep extraction (Claude, per LIFEOS_LLM_BACKEND)
  Work with the filtered, contextualized interactions.
  Focus on memorable, unusual, personal details.
  Exclude job titles, companies, generic professional info.
  → Candidate facts with source quotes (no confidence yet)

Stage 3 — Validation + calibrated confidence (local LLM)
  For each candidate fact:
    - Does the quote actually support this fact?
    - Is this about {person} or someone else?
    - Evidence-strength assessment
  → Validated facts with calibrated confidence
```

Stages 1 and 3 default to local LLM via `api/services/llm_client.py` (selected by `LIFEOS_LLM_BACKEND` — see [ADR-009](../../adr/009-llm-backend-toggle.md)). Stage 2 also follows the backend toggle. The pipeline falls back gracefully if the local LLM is unavailable.

### Message context window

Single messages out of context lead to wrong conclusions. For iMessage / WhatsApp / Slack DM interactions, the pipeline fetches surrounding messages from the same conversation thread — 5 before and 5 after — so the model evaluates each candidate in conversational context.

### Calibrated confidence

Self-reported confidence is unreliable. Stage 3 explicitly categorizes evidence strength and derives confidence from that:

| Evidence type | Confidence | Example |
|---------------|------------|---------|
| Single casual mention | 0.3–0.5 | "Went for a jog yesterday" |
| Multiple mentions | 0.5–0.7 | Jogging mentioned 3 times over 2 years |
| Explicit self-identification | 0.7–0.85 | "I'm training for a marathon" |
| Repeated, defining characteristic | 0.85–0.95 | "My weekly long run is 15 miles" |
| Direct statement of fact | 0.9+ | "My dog's name is Max" |

Single casual mentions cap at 0.5. Facts attributed to the wrong person (the user or a third party) are rejected.

### Extraction prompt focus

The Stage 2 prompt prioritizes recall-assistance facts the user **can't** find on LinkedIn:

> Extract MEMORABLE personal details about {person} that would help recall them later.
>
> **Include** (high value): pet names, hobby specifics, family member names, preferences, personal anecdotes, health/medical if mentioned.
>
> **Exclude** (low value, findable elsewhere): current job title, company name, generic professional info, basic biographical facts.
>
> The user can find "{person} works at {company}" on LinkedIn. They can't find "{person}'s dog is named Max" anywhere else.

---

## Related Documents

- [crm-ui.md](crm-ui.md) — CRM index
- [crm-interactions.md](crm-interactions.md) — Timeline view and source integrations
- [crm-graph.md](crm-graph.md) — Relationship graph, edge weights, multi-source tracking
- [crm-analytics.md](crm-analytics.md) — Family / Me / Birthdays / Relationship dashboards (Birthdays page owns the birthday-toast UX)
- [data-model.md](data-model.md) — Two-tier data model semantics
- [api-reference.md](api-reference.md) — Full HTTP endpoint catalog
- [entity-resolution.md](entity-resolution.md) — How identifiers map to canonical people
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) — Why SourceEntity and PersonEntity are separate
- [ADR-009: LIFEOS_LLM_BACKEND toggle](../../adr/009-llm-backend-toggle.md) — Why fact-extraction stages can run local or cloud
