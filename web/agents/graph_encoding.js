// web/agents/graph_encoding.js
//
// Pure session-encoding functions for the Graph tab — no DOM, no d3, no
// fetch, so every function here is directly unit-testable (see
// tests/test_agents_graph_encoding_browser.py) without spinning up the
// force simulation or a stubbed page. `web/agents/graph.js` imports these
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
