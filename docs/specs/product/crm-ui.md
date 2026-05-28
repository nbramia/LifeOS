# Personal CRM

**Status:** Complete
**Owner:** CRM
**Last Updated:** 2026-05-27

LifeOS's Personal CRM is built on top of the [two-tier data model](data-model.md) ([ADR-003](../../adr/003-two-tier-data-model.md)) and provides network management and relationship context across every observed touchpoint with the people in your life. The UI lives at `/crm`; the API lives at `/api/crm/*`.

The CRM is split across four focused specs by feature area — this file is the index.

**Primary use cases:**
- Network discovery: "Who do I know at company X?"
- Relationship visualization: "Show me my connections."
- Meeting prep context: "What do I know about this person?"
- Communication tracking: "When did I last talk to X?"

**Non-goals:**
- Outbound sales/marketing CRM features.
- Contact management (creating/editing raw contact details — the system ingests; the underlying sources own creation/edit).
- Email automation or scheduling.

---

## Sub-specs

| Spec | Covers |
|------|--------|
| [crm-people.md](crm-people.md) | Person list view, detail view, edit flows; the two-tier entity model; contact-source aggregation; split and merge operations; link overrides; cleanup queue; relationship-strength scoring; Dunbar circles; the multi-stage person-facts extraction pipeline. |
| [crm-interactions.md](crm-interactions.md) | The interaction timeline and the data-source integrations behind it (Gmail, Calendar, iMessage, Apple Contacts, Slack, WhatsApp, Signal). |
| [crm-graph.md](crm-graph.md) | The D3 force-directed relationship graph; multi-source `Relationship` model; per-source counts; source-filter UI; edge-weight calculation. |
| [crm-analytics.md](crm-analytics.md) | Aggregated views: Family Dashboard (`/crm#family`), Me Dashboard (`/me` — the landing page), Birthdays Page (`/birthdays`), Relationship Dashboard (`/relationship`). |

---

## Related Documents

- [data-model.md](data-model.md) — Two-tier data model (SourceEntity / PersonEntity) the CRM is built on
- [entity-resolution.md](entity-resolution.md) — How identifiers map to canonical people
- [api-reference.md](api-reference.md) — Full HTTP endpoint catalog (`/api/crm/*` lives here)
- [Frontend (technical)](../technical/frontend.md) — Vanilla-HTML/JS implementation details for `web/crm.html`
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) — Why SourceEntity and PersonEntity are separate
- [Agent Viz](agent-viz.md) — The other D3 force-graph in LifeOS; shares visual conventions and code patterns with crm-graph.md
