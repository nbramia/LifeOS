# Frontend Architecture

> **Status:** Complete
> **Owner:** Frontend
> **Last Updated:** 2026-09-04

UI components, patterns, and implementation details for LifeOS web interfaces.

---

## Table of Contents

1. [Overview](#overview)
2. [Chat UI](#chat-ui)
3. [CRM UI](#crm-ui)
4. [Common Patterns](#common-patterns)

---

## Overview

LifeOS uses vanilla HTML/JavaScript with no build step. Both UIs are single-page applications served directly by FastAPI.

**Key Files:**
- `web/index.html` - Chat UI
- `web/crm.html` - CRM UI

**Design Principles:**
- No framework dependencies
- SSE for streaming responses
- Mobile-responsive layouts
- Dark mode support
- Obsidian URI scheme integration

---

## Chat UI

### Page Structure

```
┌─────────────────────────────────────────────────────────────┐
│  Header: LifeOS │ Status │ Cost │ New Chat │ Sidebar Toggle │
├─────────────────┬───────────────────────────────────────────┤
│                 │                                           │
│  Conversations  │              Messages                     │
│  Sidebar        │                                           │
│                 │  ┌────────────────────────────────────┐   │
│  [Chat 1]       │  │ User: What's on my calendar?       │   │
│  [Chat 2]       │  └────────────────────────────────────┘   │
│  ...            │  ┌────────────────────────────────────┐   │
│                 │  │ Assistant: Here are your events... │   │
│                 │  │ [Sources: event1.md, event2.md]    │   │
│                 │  │ [Remember]                         │   │
│                 │  └────────────────────────────────────┘   │
│                 │                                           │
├─────────────────┴───────────────────────────────────────────┤
│  [Attachments] │ Type your message...           │ [Send]   │
└─────────────────────────────────────────────────────────────┘
```

### SSE Streaming

Responses stream via Server-Sent Events:

```javascript
const eventSource = new EventSource(`/api/ask/stream?...`);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'routing':
      // Show which sources are being queried
      break;
    case 'content':
      // Append content to message
      break;
    case 'sources':
      // Display source citations
      break;
    case 'done':
      // Complete the message
      eventSource.close();
      break;
  }
};
```

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` | Send message |
| `Shift+Enter` | New line |
| `Ctrl/Cmd+K` | New conversation |
| `Ctrl/Cmd+/` | Toggle sidebar |
| `Esc` | Cancel/close modal |

### Obsidian Links

Source links use the `obsidian://` URI scheme:

```javascript
function createObsidianLink(filePath) {
  const vaultName = 'Notes 2025';
  const encoded = encodeURIComponent(filePath);
  return `obsidian://open?vault=${vaultName}&file=${encoded}`;
}
```

---

## CRM UI

### Page Structure

```
┌─────────────────────────────────────────────────────────────┐
│  Header: CRM │ Search │ Filters │ Stats                     │
├─────────────────────────────────────────────────────────────┤
│  Category: All │ Work │ Personal │ Family                   │
├────────────────────────┬────────────────────────────────────┤
│                        │                                    │
│  People List           │  Person Detail / Network Graph     │
│                        │                                    │
│  ┌──────────────────┐  │  ┌──────────────────────────────┐ │
│  │ 🔵 John Smith    │  │  │ Overview │ Timeline │ Network │ │
│  │   Movement Labs  │  │  └──────────────────────────────┘ │
│  │   ████████░░ 78% │  │                                    │
│  └──────────────────┘  │  Contact info, stats, notes...    │
│  ┌──────────────────┐  │                                    │
│  │ 🔵 Jane Doe      │  │                                    │
│  │   ...            │  │                                    │
│  └──────────────────┘  │                                    │
│                        │                                    │
└────────────────────────┴────────────────────────────────────┘
```

### Client-Side Navigation

Moving between the CRM's dashboards (Me, Family, Birthdays, Relationship, the person list at `/crm`), and between a person's tabs (Overview/Timeline/Network), is entirely client-side: `navigateTo()`/`selectPerson()`/`switchTab()` update the URL with `history.pushState` and dispatch straight to the target view's existing initializer, with no document reload. Browser back/forward restores the previous view the same way, and a dashboard's in-memory state (its own cached aggregates) persists for the life of the tab. A full page reload or a direct link/bookmark still renders every URL exactly as a fresh visit. Product-level behavior: [crm-ui.md § Navigation](../product/crm-ui.md#navigation).

### Network Graph

D3.js force-directed graph visualization:

**Controls:**
- Zoom/pan with mouse wheel and drag
- Click node to view person details
- Click edge to view relationship details
- Reset Zoom button restores view

**Filters:**
- Show Labels toggle
- Edge Weight slider (0-100%)
- Degree filter (1st only vs 1st & 2nd)
- Source filter (Calendar, Email, iMessage, WhatsApp, Slack, LinkedIn)

**Edge Weight Source:**

Graph edge weights are unified with relationship strength:
- **Owner edges** (you ↔ someone): Uses the person's `relationship_strength` (factors in recency, frequency, diversity)
- **Non-owner edges** (others ↔ others): Uses `pair_strength` computed from shared interactions

This ensures dormant relationships (e.g., ex-partners you haven't talked to in years) show appropriately low edge weights despite high historical interaction counts.

**Dynamic Edge Threshold:**

When opening the graph view, the edge weight slider is automatically set to show approximately 25 nodes. This ensures a readable graph regardless of how many connections a person has:

```javascript
function calculateOptimalEdgeThreshold(nodes, links, centerId, targetNodeCount = 25) {
  // Get first-degree edges connected to center
  const firstDegreeEdges = links.filter(l =>
    l.source === centerId || l.target === centerId
  );

  // If fewer than target, show all (threshold = 0)
  if (firstDegreeNodeCount <= targetNodeCount) return 0;

  // Binary search for threshold that shows ~25 nodes
  // Returns percentage (0-100) for slider position
}
```

The threshold is recalculated each time a different person is selected as the center node.

**Bounded Neighborhood Loading:**

`GET /api/crm/network?center_on={id}` never loads the full relationship table — it returns a bounded neighborhood (the center, its strongest first-degree connections, then a limited number of deeper-hop nodes per node) capped by `max_nodes`, `max_second_degree_per_node`, and `max_edges`. The Graph tab sends only `center_on` and `depth`, leaving the caps at their server defaults. Full node/edge selection algorithm: [crm-graph.md § Bounded Neighborhood](../product/crm-graph.md#bounded-neighborhood).

### Relationship Strength Visualization

Heat map colors for strength indicator:

| Strength | Color | Label |
|----------|-------|-------|
| 0.0 - 0.25 | #4299e1 (blue) | Cold |
| 0.25 - 0.5 | #48bb78 (green) | Cooling |
| 0.5 - 0.75 | #ecc94b (yellow) | Warm |
| 0.75 - 0.9 | #ed8936 (orange) | Strong |
| 0.9 - 1.0 | #e53e3e (red) | Very Strong |

### Source Badges

| Source | Badge | CSS Variable |
|--------|-------|--------------|
| gmail | 📧 | --gmail: #ea4335 |
| calendar | 📅 | --calendar: #4285f4 |
| vault | 📝 | --vault: #7c3aed |
| imessage | 💬 | --imessage: #34c759 |
| whatsapp | 💬 | --whatsapp: #25d366 |
| contacts | 📇 | --contacts: #5856d6 |
| phone | 📞 | --phone: #ff9500 |
| slack | 💼 | --slack: #4a154b |
| linkedin | 💼 | --linkedin: #0077b5 |

---

## Common Patterns

### API Calls

```javascript
async function apiCall(endpoint, options = {}) {
  const response = await fetch(endpoint, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });

  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }

  return response.json();
}
```

### Loading States

```javascript
function showLoading(element) {
  element.classList.add('loading');
  element.innerHTML = '<div class="spinner"></div>';
}

function hideLoading(element) {
  element.classList.remove('loading');
}
```

### Error Handling

```javascript
function showError(message) {
  const toast = document.createElement('div');
  toast.className = 'toast error';
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}
```

### Mobile Responsiveness

```css
@media (max-width: 768px) {
  .sidebar { display: none; }
  .sidebar.open { display: block; position: fixed; }
  .main-content { margin-left: 0; }
}
```

### Dark Mode

```css
:root {
  --bg-primary: #ffffff;
  --text-primary: #1a202c;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg-primary: #1a202c;
    --text-primary: #e2e8f0;
  }
}
```

---

## File Reference

| File | Purpose |
|------|---------|
| `web/index.html` | Chat UI (single file with HTML, CSS, JS) |
| `web/crm.html` | CRM UI (single file with HTML, CSS, JS) |
| `api/routes/chat.py` | Chat API endpoints |
| `api/routes/crm.py` | CRM API endpoints |

## Related Documents

- [Chat UI](../product/chat-ui.md) -- Chat interface product spec
- [CRM UI](../product/crm-ui.md) -- CRM interface product spec
- [API Reference](../product/api-reference.md) -- API endpoint contracts
