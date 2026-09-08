// web/agents/linking.js
//
// The Board and Graph tabs describe the same work from two angles — this
// module is the one shared surface between them: a persisted filter state
// both tabs' filter bars read and write, and a tiny cross-tab navigation
// bus. It owns no DOM and no rendering — `web/agents/board.js` and
// `web/agents/graph.js` each bind their own filter controls to
// `getFilters()`/`setFilter()`/`subscribe()`, and drain their own pending
// focus intent via `takeGraphFocus()`/`takeBoardFocus()`, applying it to
// their own view (scrolling, panning, opening a drawer or panel).
// `web/agents.html`'s own tab-switch code calls `activateTab()`; either tab
// module (or the host page) can call `onTabActivate()` to react to it.

import { LANES } from './lanes.js';

const FILTERS_STORAGE_KEY = 'lifeos.agents.filters.v1';
// Mirrors LANE_FILTER_STORAGE_KEY in web/agents/board.js — duplicated as a
// literal (not imported) so this module and board.js never form an import
// cycle: board.js imports FROM here, not the other way around. Read only
// once, for the one-time migration below; board.js writes only to this key.
const LEGACY_BOARD_LANE_STORAGE_KEY = 'lifeos.agents.board.lanes';

// The board's own default (every lane but Done) — the shared `lanes`
// filter's default too, so an operator who never touched the lane filter
// sees the same board they always did, and the graph honours that same
// default rather than showing every lane unfiltered.
const DEFAULT_LANE_IDS = LANES.filter(l => l.id !== 'done').map(l => l.id);

export const DEFAULT_FILTERS = Object.freeze({
  search: '',
  lanes: DEFAULT_LANE_IDS,
  assignee: 'all',
  host: 'all',
  engine: 'all',
  tag: '',
  recency: 'all',
});

// Tolerates a hand-edited or stale array: drops any id that doesn't name a
// current lane, but keeps a deliberately-empty selection (`[]`) as-is — an
// operator can legitimately hide every lane — and only falls back to the
// default when nothing in the stored value is a real lane id at all.
function sanitizeLaneIds(ids) {
  if (!Array.isArray(ids)) return [...DEFAULT_LANE_IDS];
  if (ids.length === 0) return [];
  const validIds = new Set(LANES.map(l => l.id));
  const filtered = ids.filter(id => validIds.has(id));
  return filtered.length > 0 ? filtered : [...DEFAULT_LANE_IDS];
}

function readLegacyLaneIds() {
  try {
    const raw = localStorage.getItem(LEGACY_BOARD_LANE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : null;
  } catch (_) {
    return null;
  }
}

function loadFilters() {
  try {
    const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        return {
          ...DEFAULT_FILTERS,
          ...parsed,
          lanes: sanitizeLaneIds(parsed.lanes),
        };
      }
    }
  } catch (_) { /* corrupt value — fall through to the legacy/default read */ }
  // No v1 key yet — seed `lanes` from the board's own legacy selection if
  // the operator had ever changed it, so that choice survives.
  const legacyLaneIds = readLegacyLaneIds();
  return {
    ...DEFAULT_FILTERS,
    lanes: legacyLaneIds !== null ? sanitizeLaneIds(legacyLaneIds) : [...DEFAULT_LANE_IDS],
  };
}

let filters = loadFilters();
const filterSubscribers = new Set();

function persist() {
  try { localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters)); } catch (_) {}
}

function notifyFilters() {
  for (const fn of filterSubscribers) {
    try { fn(filters); } catch (_) {}
  }
}

// The current shared filter state — callers must treat the returned object
// as read-only (every mutation goes through `setFilter`/`resetFilters`,
// which replace it wholesale so `subscribe` callbacks always see a fresh
// reference for change detection).
export function getFilters() {
  return filters;
}

export function setFilter(key, value) {
  if (!(key in DEFAULT_FILTERS)) return;
  filters = { ...filters, [key]: key === 'lanes' ? sanitizeLaneIds(value) : value };
  persist();
  notifyFilters();
}

export function resetFilters() {
  filters = { ...DEFAULT_FILTERS, lanes: [...DEFAULT_FILTERS.lanes] };
  persist();
  notifyFilters();
}

// Called on every filter change (`setFilter`/`resetFilters`), including the
// change a caller's own `setFilter` just made — callers reconcile their own
// controls against the passed-in state rather than assuming it's someone
// else's change.
export function subscribe(fn) {
  filterSubscribers.add(fn);
  return () => filterSubscribers.delete(fn);
}

// ---------------------------------------------------------------------
// Cross-tab navigation — a pending-intent store plus a tab-activation bus.
// Deliberately dumb: it remembers what was asked for and tells whoever's
// listening that the tab changed; the tabs themselves do the scrolling,
// panning, and opening. An intent is consumed (`take*`) at most once — a
// module drains it both right after `activateTab` fires for its own tab
// (the common case: data is already loaded) and again the next time its own
// data arrives (a snapshot/board payload landing after a URL deep link set
// the intent before that module had fetched anything yet). Whichever drain
// runs first wins; the other finds nothing pending.
// ---------------------------------------------------------------------

let pendingGraphFocus = null;   // a session id, or null
let pendingBoardFocus = null;   // { cardId, openDrawer } or null

export function requestGraphFocus(sessionId) {
  pendingGraphFocus = sessionId;
}

export function takeGraphFocus() {
  const value = pendingGraphFocus;
  pendingGraphFocus = null;
  return value;
}

export function requestBoardFocus(cardId, opts) {
  pendingBoardFocus = { cardId, openDrawer: !!(opts && opts.openDrawer) };
}

export function takeBoardFocus() {
  const value = pendingBoardFocus;
  pendingBoardFocus = null;
  return value;
}

const tabSubscribers = [];

export function onTabActivate(fn) {
  tabSubscribers.push(fn);
}

export function activateTab(name) {
  for (const fn of tabSubscribers) {
    try { fn(name); } catch (_) {}
  }
}
