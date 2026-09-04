# Personal CRM — Relationship Graph

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-09-04

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
- Renders in roughly half a second on the real dataset regardless of how
  large the underlying contact graph is, because the server returns a
  bounded neighborhood rather than the full relationship set (see Bounded
  Neighborhood below); the client-side strength slider then further narrows
  that bounded set to the ~25 first-degree nodes the graph is designed to
  display at once.

### Bounded Neighborhood

`GET /api/crm/network?center_on=<id>` (used by the Graph tab) never loads
every relationship. Node selection:

1. The center person (degree 0), resolved to its canonical id first — a
   merged/legacy `center_on` id still resolves to the surviving person.
   The center is always present in the response *unless it is hidden*, in
   which case it (and anything reachable only through it) is dropped the
   same way a hidden person is dropped anywhere else in the CRM — the
   response can end up with zero nodes if the hidden person also has no
   real relationships, but that isn't guaranteed for every hidden person.
   If the center is filtered out this way, edges naming it are omitted too
   (see Edge selection below).
2. The center's strongest first-degree connections (degree 1) — up to
   `max_nodes - 1` when `depth=1`, or up to 75% of `max_nodes` when
   `depth >= 2` (see the next point for why). Ranked by the same value the
   response renders as edge weight: for an edge to the CRM owner, the other
   person's `relationship_strength`; otherwise the pair's own
   `pair_strength`. This ranking uses one indexed query for the center's own
   relationships, not the full relationship table.
3. For `depth=2` (the Graph tab's default), the *remaining* node budget goes
   to deeper hops: each first-degree node, processed strongest first,
   contributes up to `max_second_degree_per_node` (default `10`) of its own
   strongest connections, ranked by a cheaper sum-of-shared-interaction-counts
   proxy, until `max_nodes` is reached. First-degree selection reserving
   ~25% of the budget for this tier (rather than consuming the whole budget
   itself) is what makes second-degree nodes actually appear for a
   well-connected center. A node that has a genuine direct relationship to
   the center but missed the first-degree cut, and is then re-discovered
   through a friend, is relabeled `degree: 1` (and given its own center
   edge) rather than being shown as second-degree — `degree` always means
   "has a direct edge to the center," not "was found in the first pass."

A relationship row referencing a legacy (merged-away) person id is resolved
to its canonical id before any of this ranking or de-duplication, so a
stale id in the underlying data can't produce a duplicate node or a
first-degree node with no edge back to the center.

Edge selection: every edge connecting the center to a first-degree node is
always included, regardless of `max_edges` — this is what guarantees every
first-degree node has an edge to the center — except when the center itself
was dropped by the `category`/`min_strength` filters below, in which case no
center edges are emitted (there is no center node left for them to connect
to). The remaining edge budget (`max_edges` minus those center edges) is
filled with the strongest remaining edges among the selected nodes, by the
same rendered edge weight. Without this cap, the induced subgraph among a
well-connected center's neighbors is close to complete (up to `max_nodes`
choose 2 edges) — capping it means the strength slider's bottom end may
show fewer inter-node edges than an uncapped response would have.

`category`, if set, is applied during first-degree selection over a window
of the 3×`max_nodes` strongest candidates (categories computed via the same
batched source-entity lookup `GET /people` uses), then the top slots are
taken from the survivors — best-effort: a category concentrated outside
that window is under-represented in the result. Deeper-hop candidates use a
cheaper per-person category check with no batched fetch. Every node's
`category` field, and whether it survives this filter, both come from the
same computation for that node (either the batched one during selection, or
the cheap one otherwise) — an id is never selected under one category
decision and then dropped, or kept, under a different one. `min_strength` is
applied to already-selected nodes against
`relationship_strength` on its 0–100 scale — since the parameter itself is
only accepted up to 1.0, the only nodes it can drop are those with a
`relationship_strength` below 1.0 (in practice, strength-0 nodes).

A well-connected center often has few or no genuine friends-of-friends left
to fill the deeper-hop tier once its own direct connections are excluded
from that tier (point 3 above) — so any of that tier's budget that goes
unspent is backfilled from the center's next-strongest direct candidates
that missed the first-degree cut. They're real first-degree people (see the
relabeling note above), so a dense center's response still reaches
`max_nodes` rather than coming back short.

The Graph tab's own auto-selected strength-slider threshold
never picks a threshold that would hide
every first-degree node, and returns 0 outright when every first-degree
edge shares one weight (there's no discriminating threshold to pick in that
case) — without this, bounding the edge set could put a real person's
graph in a state where it rendered nothing at all on first load.

Query parameters:

| Parameter | Range | Default | Meaning |
|---|---|---|---|
| `max_nodes` | 1–500 | 150 | Total nodes in the response, including the center |
| `max_second_degree_per_node` | 0–50 | 10 | Second-(and deeper-)degree neighbors added per node at the previous depth |
| `max_edges` | 1–20000 | 2000 | Target maximum edges; every edge touching the center is always included even if that alone exceeds this |
| `allow_full_graph` | bool | false | Opt-in to load every person and relationship (no `center_on`); ignores the three caps above |

The Graph tab sends only `center_on` and `depth`, leaving the caps above at
their server defaults so a future default change reaches the tab without a
frontend edit.

`allow_full_graph=true` combined with `category` filters every person by
the same dynamically-computed category the response displays for them,
rather than their raw stored `category` field, so a person's displayed
category and whether a category filter kept them always agree.

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

The network endpoint (`GET /api/crm/network?center_on={id}`) includes the same breakdown per edge so the graph can filter and re-weight client-side.

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

- [api-crm.md](api-crm.md) — API endpoint reference for the graph/relationship data described here
- [crm-ui.md](crm-ui.md) — CRM index
- [crm-people.md](crm-people.md) — Person list/detail, Dunbar circles (drives graph filtering and coloring)
- [crm-interactions.md](crm-interactions.md) — The per-source observations the discovery job aggregates over
- [crm-analytics.md](crm-analytics.md) — Dashboards that share the underlying relationship model
- [Frontend](../technical/frontend.md#network-graph) — D3 / vanilla-JS implementation details, including the bounded-neighborhood loading pointer back here
- [Agent Viz](agent-viz.md) — The other D3 force-graph in LifeOS; the two share visual conventions and code patterns
