// web/agents/graph_encoding.js
//
// Pure session-encoding functions for the Graph tab — no DOM, no d3, no
// fetch, so every function here is directly unit-testable (see
// tests/test_agents_graph_encoding_browser.py) without spinning up a
// stubbed page. `web/agents/graph.js` imports these
// for node rendering; `web/agents/panel.js` imports `nodeLabel`,
// `isRawIdValue`, and `routingLabel` for the side panel's header, tooltip,
// and rename prefill and re-exports `routingLabel` so existing
// importers (`web/agents/board.js`, `web/agents/graph.js`) are unaffected —
// this module never imports FROM panel.js, so the two never form a cycle.

export { LANE_COLORS, laneColor } from './lanes.js';

export function routingLabel(routing) {
  if (!routing || routing === 'local') return 'Local';
  if (routing === 'claude_code' || routing === 'code') return 'Claude Code';
  if (routing === 'codex') return 'Codex';
  if (routing === 'remote') return 'Remote';  // #cloud tag — the configured remote provider, not Anthropic
  if (routing === 'hermes') return 'Hermes';
  if (routing === 'ask') return 'Ask';  // waiting on the operator, not a model
  return 'Claude';
}

// A raw identifier (the session id itself, that id with a known CLI prefix
// ("cc:"/"cx:") stripped, or the row's task_id) is never a real label,
// wherever it might render: the graph node, the search-results dropdown,
// or the side panel header/tooltip/rename prefill. Compared by equality
// (after trimming), not by `startsWith`: a `startsWith` check never
// matches a prefixed session id ("cc:<uuid>".startsWith("<uuid>") is
// false), which would let a raw id through as a label.
export function isRawIdValue(d, value) {
  const norm = (v) => (v || '').toString().trim();
  const sessionId = norm(d.session_id);
  const bareSessionId = sessionId.replace(/^(cc|cx):/, '');
  const taskId = norm(d.task_id);
  const v = norm(value);
  return !!v && (
    v === sessionId ||
    v === bareSessionId ||
    (!!taskId && v === taskId)
  );
}

// Precedence, first non-empty wins. `custom_label`, `label`, and
// `short_label` are each skipped when they're not a human label but the raw
// identifier the row fell back to (`isRawIdValue` above). `label` outranks
// `short_label`: for a card-linked session, `label` is the linked card's
// title (`_label_for_session` on the server), which is more authoritative
// than an LLM-generated summary. `model_label` is never a candidate here —
// it renders only as a chip (the hover card, the panel's `.panel-chips`
// row, the Hermes routing badge), never as the node/panel/tooltip name, so
// two sessions running the same model don't read as the same node. Never
// emits '?'.
// 1-hop-and-beyond descendants of `session` (via `parent_session_id`) among
// `sessions` — the same subtree the kill endpoint itself tears down
// (`_kill_session_subtree`, api/routes/agents.py), so the confirmation
// modal's cascade preview (./session_actions.js's `openKillModal`) names
// exactly what a Kill click actually takes with it. One function so the
// Graph tab's side panel (which already holds every known session) and the
// Board drawer (which fetches `/api/agents/snapshot` on demand for this)
// can never disagree about what "descendants" means.
export function descendantsOf(sessions, session) {
  if (!session) return [];
  const childrenOf = new Map();
  for (const x of (sessions || [])) {
    if (!x.parent_session_id) continue;
    if (!childrenOf.has(x.parent_session_id)) childrenOf.set(x.parent_session_id, []);
    childrenOf.get(x.parent_session_id).push(x);
  }
  const out = [];
  const queue = [session.session_id];
  const seen = new Set([session.session_id]);
  while (queue.length) {
    const sid = queue.shift();
    for (const child of (childrenOf.get(sid) || [])) {
      if (seen.has(child.session_id)) continue;
      seen.add(child.session_id);
      out.push(child);
      queue.push(child.session_id);
    }
  }
  return out;
}

// Deterministic coordinates for the delegation timeline. Horizontal order is
// chronological (with the id as a stable tie-breaker); vertical position is
// delegation depth. Missing or filtered parents make a session a visible root.
//
// `columnGap`'s default is sized against `web/agents/graph.js`'s own label
// legibility floor, not just visual taste: a shown label's font can be
// boosted well past its 12px base size on a height-bound viewport (up to
// LABEL_MAX_BOOST_PX/LABEL_BASE_FONT_PX = 3x before it's hidden instead),
// and a full-width label (LABEL_MAX_W = 132 user units unboosted) at that
// boost renders at up to 132 * 3 = 396 user units wide. Same-depth columns
// (every root session, at minimum) sit exactly `columnGap` apart with no
// other spacing mechanism between them, so a value near the unboosted
// label width alone lets two adjacent, fully legible-boosted labels overlap
// -- with margin added for real font-metric variance across environments.
export function delegationTimelineLayout(sessions, {
  left = 130,
  top = 120,
  columnGap = 440,
  depthGap = 190,
} = {}) {
  const byId = new Map(sessions.map(s => [s.session_id, s]));
  const timestamp = s => {
    const raw = s.started_at ?? s.created_at ?? s.last_activity_at ?? 0;
    const value = typeof raw === 'number' ? raw : Date.parse(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const compareTime = (a, b) =>
    timestamp(a) - timestamp(b) || String(a.session_id).localeCompare(String(b.session_id));
  const remaining = new Map(sessions.map(s => [s.session_id, s]));
  const ordered = [];
  while (remaining.size) {
    let available = [...remaining.values()]
      .filter(s => !s.parent_session_id || !remaining.has(s.parent_session_id))
      .sort(compareTime);
    // A malformed cycle has no topological head. Break it deterministically;
    // depth calculation below independently guards against the same cycle.
    if (!available.length) available = [[...remaining.values()].sort(compareTime)[0]];
    const next = available[0];
    ordered.push(next);
    remaining.delete(next.session_id);
  }
  const depthMemo = new Map();

  function depthOf(session, visiting = new Set()) {
    if (depthMemo.has(session.session_id)) return depthMemo.get(session.session_id);
    const parent = byId.get(session.parent_session_id);
    if (!parent || visiting.has(session.session_id)) {
      depthMemo.set(session.session_id, 0);
      return 0;
    }
    const next = new Set(visiting);
    next.add(session.session_id);
    const depth = depthOf(parent, next) + 1;
    depthMemo.set(session.session_id, depth);
    return depth;
  }

  return ordered.map((session, index) => {
    const depth = depthOf(session);
    return {
      ...session,
      x: left + index * columnGap,
      y: top + depth * depthGap,
      _timelineOrder: index,
      _delegationDepth: depth,
      _timelineTimestamp: timestamp(session),
    };
  });
}

export function nodeLabel(d) {
  const candidates = [
    isRawIdValue(d, d.custom_label) ? '' : d.custom_label,
    isRawIdValue(d, d.label) ? '' : d.label,
    isRawIdValue(d, d.short_label) ? '' : d.short_label,
    d.prompt_preview,
    routingLabel(d.routing),
  ];
  for (const c of candidates) {
    const trimmed = (c || '').toString().trim();
    if (trimmed) return trimmed;
  }
  return (d.session_id || '').slice(0, 8);
}

// The engine a session ran (or is running) on — drives the node's shape and
// the panel's engine chip. `source` (set server-side for CLI ingests) wins
// over `routing` when both are present. `remote` (the `#cloud` tag) and
// `hermes` both map to the `hermes` engine: both name the same
// remote-provider/DeepSeek execution path, just reached via different
// routing values (`hermes` from the agent worker, `remote` from `#cloud`).
export function engineOf(d) {
  if (d.source === 'claude_code' || d.source === 'codex') return d.source;
  const routing = d.routing;
  if (routing === 'hermes' || routing === 'remote') return 'hermes';
  if (!routing || routing === 'local') return 'local';
  if (routing === 'claude_code' || routing === 'code') return 'claude_code';
  if (routing === 'codex') return 'codex';
  return 'claude';
}

// The routing identity a session is matched against for the shared engine
// filter (`#filter-route` / `#board-filter-engine`) — the same
// source-over-routing precedence `engineOf` uses (a CC/Codex row's `source`
// wins), but preserving the full routing value space rather than collapsing
// engines into the five shape buckets: `remote`, `hermes`, and `ask` stay
// distinct, matching the filter's own option list.
export function routingFilterValue(d) {
  if (d.source === 'claude_code' || d.source === 'codex') return d.source;
  return d.routing || 'local';
}

// One glyph name (used as the node's `data-shape` attribute and the shape
// legend's swatch) and one legend label per engine.
export const ENGINE_SHAPES = {
  claude_code: { glyph: 'square',  label: 'Claude Code' },
  codex:       { glyph: 'hexagon', label: 'Codex' },
  hermes:      { glyph: 'star',    label: 'Hermes' },
  local:       { glyph: 'diamond', label: 'Local' },
  claude:      { glyph: 'circle',  label: 'Claude' },
};

// The SVG element to create for an engine's node. Two engines can share a
// tag (codex and local both draw as `polygon`, with different point sets)
// — `ENGINE_SHAPES[engine].glyph` (rendered as the node's `data-shape`
// attribute) is what actually distinguishes all five on screen and in tests.
export function shapeTagFor(engine) {
  const tags = {
    claude_code: 'rect',
    codex: 'polygon',
    hermes: 'path',
    local: 'polygon',
    claude: 'circle',
  };
  return tags[engine] || 'circle';
}

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

// Node radius — a monotonic function of active seconds only (never cache
// tokens, which measure caching rather than work), floored so a
// just-started session is still clickable and capped so one long-running
// session can't dwarf the rest of the layout.
export function radiusForActiveSeconds(seconds) {
  const sec = Math.max(0, Number(seconds) || 0);
  return clamp(12 + 5 * Math.log2(1 + sec / 60), 12, 40);
}

// Width of the secondary tool-call ring — small and capped so it reads as
// an accent, never competes with the node's own size for attention.
export function ringWidthForToolCalls(toolCalls) {
  const n = Math.max(0, Number(toolCalls) || 0);
  return clamp(1 + Math.log2(1 + n), 1, 6);
}

// Search dropdown tiers — a match's `field` names which tier
// it belongs to and which badge it renders. `isKnownSearchField` is an
// own-property check (not a plain `field in SEARCH_TIER`) so a `field`
// value that happens to name an `Object.prototype` member (e.g.
// `"constructor"`) is rejected rather than resolving to a prototype method.
export const SEARCH_TIER = { label: 0, short_label: 1, summary: 2 };
export const SEARCH_BADGE = { label: 'name', short_label: 'label', summary: 'summary' };

export function isKnownSearchField(field) {
  return Object.prototype.hasOwnProperty.call(SEARCH_TIER, field);
}

// Compact "1h 3m" / "42s" duration formatting for the hover card.
function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// Ordered `[label, value]` rows for the hover card body — the card's own
// first line (the node's name) is `nodeLabel(d)`, rendered by the caller
// rather than included here, so this stays purely about the secondary
// detail rows. A row is omitted when its value is empty (e.g. no branch
// and no cwd, or a session with no reported model yet).
export function hoverCardRows(d) {
  const rows = [];
  rows.push(['Duration', formatDuration(d.total_active_seconds)]);
  rows.push(['Cost', '$' + (d.total_dollars || 0).toFixed(4)]);
  const modelBits = [d.model_label, d.effort].filter(Boolean);
  if (modelBits.length) rows.push(['Model', modelBits.join(' · ')]);
  if (d.host) rows.push(['Host', d.host]);
  const place = d.branch || d.decoded_cwd;
  if (place) rows.push([d.branch ? 'Branch' : 'Cwd', place]);
  if (d.last_event_kind) rows.push(['Last event', d.last_event_kind]);
  return rows;
}
