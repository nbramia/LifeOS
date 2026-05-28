# Personal CRM — Relationship Graph

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-05-27

The D3 force-directed graph at `/crm` plus the multi-source `Relationship` model that backs it. Covers connection discovery, edge-weight calculation, source-filter UI, and the graph rendering itself.

See [crm-ui.md](crm-ui.md) for the CRM index and the sibling specs that cover people, interactions, and dashboards.

---

## Table of Contents

1. [Connections API](#connections-api)
2. [Graph Visualization](#graph-visualization)
3. [Extended Relationship Data Model](#extended-relationship-data-model)
4. [Relationship Discovery](#relationship-discovery)
5. [Source Breakdown API](#source-breakdown-api)
6. [Graph Source Filter UI](#graph-source-filter-ui)
7. [Edge Weight Calculation](#edge-weight-calculation)

---

## Connections API

**Endpoint:** `GET /api/crm/people/{id}/connections`

Discovers and returns people connected to the given person through:

1. **Shared calendar events** — same attendee lists.
2. **Shared email threads** — CC'd together.
3. **Vault co-mentions** — same note.
4. **Explicit relationships** — manually tagged (family, coworker, etc.).

**Response shape:**

```json
{
  "connections": [
    {
      "person_id": "uuid",
      "name": "Sam",
      "company": "Example Corp",
      "relationship_type": "coworker",
      "shared_events_count": 45,
      "shared_threads_count": 12,
      "shared_contexts": ["Work/Example/"],
      "connection_strength": 0.78
    }
  ],
  "count": 15
}
```

Sorted by `connection_strength` descending. No self-connections returned. Strength is derived from interaction frequency across the surfaces above; `relationship_type` is inferred from context (e.g., shared `Work/Example/` vault paths → `coworker`).

---

## Graph Visualization

D3-based force-directed graph that renders the selected person's network.

```
┌─────────────────────────────────────────────────────────────┐
│  Graph                    [Reset Zoom] [☑ Show Labels]      │
├─────────────────────────────────────────────────────────────┤
│              ○ Madi                                         │
│             /    \                                          │
│         ○ Sam ── ● Alex ── ○ Mom                            │
│           \                   /                             │
│            ○ Hayley ─────────○ Dad                          │
│                                                             │
│  Legend:  ● Selected  ○ Connection  ━ Strong  ─ Weak        │
└─────────────────────────────────────────────────────────────┘
```

**Core behaviors:**

- D3 force-directed layout; selected person is the center node with a distinct color.
- Edge thickness scales with relationship strength.
- Nodes are draggable; the canvas is zoomable and pannable.
- Clicking a node navigates to that person; hovering shows a tooltip with name and company.
- Toggle the labels and reset-zoom controls in the toolbar.
- Re-renders when the person selection changes.
- Renders < 2s for ~30 nodes.

**Graph enhancements:**

- **Fullscreen mode** — toggle expands the graph to the viewport (mobile + desktop).
- **Color modes** — switch between category colors (work/personal/family) and Dunbar circle colors.
- **Node sizing** — switch between strength-based and centrality-based sizing.
- **Edge detail panel** — clicking an edge opens a breakdown of the relationship (shared events, threads, messages) with copy and navigation actions.
- **Dunbar circle filter** — multi-select dropdown to show/hide nodes by circle (Dunbar circles are defined in [crm-people.md § Dunbar Circles](crm-people.md#dunbar-circles)).
- **Fit-to-first-degree** — layout prioritizes the first-degree connections of the selected person; first-degree edges render distinctively (on top, thicker).

---

## Extended Relationship Data Model

The `Relationship` table tracks pairwise edges between two `PersonEntity` rows, with one column per signal source so the UI can break down where the connection comes from.

```python
@dataclass
class Relationship:
    person_a: str
    person_b: str
    relationship_type: str
    shared_contexts: list[str]

    # Calendar + email
    shared_events_count: int = 0
    shared_threads_count: int = 0

    # Direct messaging
    shared_messages_count: int = 0       # iMessage / SMS direct threads
    shared_whatsapp_count: int = 0       # WhatsApp direct threads
    shared_slack_count: int = 0          # Slack DM message count
    shared_phone_calls_count: int = 0    # Synchronous voice

    # LinkedIn signal
    is_linkedin_connection: bool = False
```

Older relationships preserve their counts during migration. New columns default to `0` / `false`.

---

## Relationship Discovery

A nightly job populates the per-source counts.

| Signal | Source | What's counted |
|--------|--------|----------------|
| `shared_events_count` | Calendar | Each calendar event where both people are attendees. |
| `shared_threads_count` | Gmail | Each email thread where both are recipients. |
| `shared_messages_count` | iMessage | Each direct 1:1 message thread. Group-chat membership is tracked via `shared_contexts`. |
| `shared_whatsapp_count` | WhatsApp | Each WhatsApp direct thread (from imported export). |
| `shared_slack_count` | Slack | Slack DM message count between the two people. |
| `is_linkedin_connection` | LinkedIn CSV | Whether both have LinkedIn `SourceEntity` rows. |

The discovery job updates the per-source counts on each `Relationship` row. Adding a new source means adding a column and a discovery step — no schema-wide rework.

---

## Source Breakdown API

The relationship-detail endpoint returns every per-source count separately so the UI can render an edge breakdown.

```python
class RelationshipDetailResponse(BaseModel):
    person_a_id: str
    person_a_name: str
    person_b_id: str
    person_b_name: str
    relationship_type: str
    shared_contexts: list[str] = []

    # Per-source breakdown
    shared_events_count: int = 0
    shared_threads_count: int = 0
    shared_messages_count: int = 0
    shared_whatsapp_count: int = 0
    shared_slack_count: int = 0
    shared_phone_calls_count: int = 0
    is_linkedin_connection: bool = False

    # Computed totals
    total_interactions: int = 0
    weight: int = 0
```

The network endpoint (`/api/crm/people/{id}/network`) includes the same breakdown per edge so the graph can filter and re-weight client-side.

---

## Graph Source Filter UI

Multi-select dropdown in the graph toolbar that filters edges by source type.

```
Edge Weight: [===|=======] 15%    Sources: [▼ All Sources]
                                           ☑ Calendar
                                           ☑ Email
                                           ☑ iMessage
                                           ☑ WhatsApp
                                           ☑ Slack
                                           ☑ Phone
                                           ☑ LinkedIn
```

**Behavior:**

- Default: all sources selected.
- An edge is visible if ANY selected source has `count > 0` on the relationship.
- Edge weight is recalculated using only the selected sources.
- Filter state persists across node navigation.
- The edge-detail panel always shows the full per-source breakdown.

---

## Edge Weight Calculation

Each edge's weight is a weighted sum of the per-source counts. Different sources carry different signal strength:

```python
weight = (
    shared_events_count        * 3   # Calendar meetings — high signal
    + shared_threads_count     * 2   # Email threads
    + shared_messages_count    * 2   # iMessage threads
    + shared_whatsapp_count    * 2   # WhatsApp threads
    + shared_slack_count       * 1   # Slack DMs — weaker per-message
    + shared_phone_calls_count * 4   # Phone calls — highest, synchronous voice
    + (10 if is_linkedin_connection else 0)
)
```

Per-source weights are configurable. The graph respects the source-filter selection (only selected sources contribute), so the same pair of people may render as a thicker or thinner edge depending on which surfaces the operator wants to see.

---

## Related Documents

- [crm-ui.md](crm-ui.md) — CRM index
- [crm-people.md](crm-people.md) — Person list/detail, Dunbar circles (drives graph filtering and coloring)
- [crm-interactions.md](crm-interactions.md) — The per-source observations the discovery job aggregates over
- [crm-analytics.md](crm-analytics.md) — Dashboards that share the underlying relationship model
- [Frontend](../technical/frontend.md) — D3 / vanilla-JS implementation details
- [Agent Viz](agent-viz.md) — The other D3 force-graph in LifeOS; the two share visual conventions and code patterns
