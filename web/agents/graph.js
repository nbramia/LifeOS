// web/agents/graph.js
//
// The Graph tab — a deterministic delegation timeline. Node rendering,
// layout, filters, chips, and search live here; the side panel's
// rendering, event feed, label edit, and summary fetch come from the
// shared `SessionPanel` in ./panel.js (also used by the Board tab's
// drawer), and the pure encoding functions (label precedence, engine
// shape, lane colour, node size, hover-card content, search-tier
// validation) live in ./graph_encoding.js so they're unit-testable without
// a DOM or d3 (see tests/test_agents_graph_encoding_browser.py).
//
// `initGraph()` is called once, lazily, the first time the operator opens
// the Graph tab (see web/agents.html) — the graph's own snapshot fetch + SSE
// stream only start then, so loading the board (the primary view) doesn't
// also open a second live connection nobody is looking at.

import {
  STATUS_COLORS, TERMINAL, isSubagentSession, escapeHtml, showToast, SessionPanel,
} from './panel.js';
import { LANES } from './lanes.js';
import {
  nodeLabel, isRawIdValue, engineOf, ENGINE_SHAPES, shapeTagFor,
  radiusForActiveSeconds, ringWidthForToolCalls, laneColor, routingFilterValue,
  isKnownSearchField, SEARCH_TIER, SEARCH_BADGE, hoverCardRows,
  descendantsOf as sharedDescendantsOf, delegationTimelineLayout,
} from './graph_encoding.js';
import {
  getFilters, setFilter, setFilters, resetFilters, subscribe as subscribeFilters,
  requestGraphFocus, takeGraphFocus, requestBoardFocus,
  onTabActivate, activateTab, setSelectedGraphCardId,
} from './linking.js';

// `boardApi` is the object `web/agents/board.js`'s `initBoard()` returns —
// board.js boots immediately on page load (the Graph tab only lazily,
// on first visit), so by the time an operator can even click a node here
// the board's own live state (kept current by its own SSE stream) already
// exists. Reusing it — rather than a second `/api/agents/board` fetch —
// is how the side panel finds the card linked to a session, the same way
// the Board drawer already does, so both surfaces decide the same action
// row for the same session (see ./session_actions.js's `decideActions`).
export function initGraph(boardApi) {
  const getCardForSession = (boardApi && boardApi.getCardForSession) || (() => null);
  const filterTerminalEl = document.getElementById('filter-terminal');
  const filterRouteEl = document.getElementById('filter-route');
  const filterStatusEl = document.getElementById('filter-status');
  const filterRecencyEl = document.getElementById('filter-recency');
  const filterCwdEl = document.getElementById('filter-cwd');
  const filterHostEl = document.getElementById('filter-host');
  const connStateEl = document.getElementById('connection-state');
  const emptyStateEl = document.getElementById('empty-state');
  const panelEl = document.getElementById('panel');
  const panelOuterEl = document.getElementById('panel-outer');
  const panelResizerEl = document.getElementById('panel-resizer');
  const hoverCardEl = document.getElementById('graph-hover-card');
  const zoomFitBtn = document.getElementById('graph-zoom-fit');
  const zoomResetBtn = document.getElementById('graph-zoom-reset');
  const laneLegendEl = document.getElementById('graph-lane-legend');
  const engineLegendEl = document.getElementById('graph-engine-legend');
  const filterLaneEl = document.getElementById('filter-lane');
  const filterAssigneeEl = document.getElementById('filter-assignee');
  const filterTagEl = document.getElementById('filter-tag');
  const filterClearBtn = document.getElementById('graph-filter-clear');
  const panelActionsEl = document.getElementById('graph-panel-actions');
  // Operator chooses recency manually → don't auto-flip on include-finished toggle.
  let recencyManuallySet = false;

  let allSessions = [];
  let allEdges = [];
  let selectedSessionId = null;
  let apiHost = '';
  // Whether the first `/api/agents/snapshot` payload has landed — a tab
  // activation (or a URL deep link, set while this module has not yet
  // fetched anything at all) can ask `drainGraphFocus` to resolve a pending focus
  // intent before `allSessions` is populated; re-queuing it here rather
  // than resolving against an empty array is what keeps the intent alive
  // until `applySnapshot`'s own drain (below) actually can.
  let snapshotLoaded = false;

  // Subagent trees — a session with `parent_session_id` set is
  // hidden by default and its parent renders a count badge; clicking the
  // badge toggles that parent's id in this set.
  const expandedParents = new Set();

  function renderLegend() {
    if (laneLegendEl) {
      laneLegendEl.innerHTML = LANES.map(l =>
        `<span class="legend-item"><span class="legend-swatch" style="background:${laneColor(l.id)}"></span>${escapeHtml(l.label)}</span>`
      ).join('');
    }
    if (engineLegendEl) {
      engineLegendEl.innerHTML = Object.values(ENGINE_SHAPES).map(info =>
        `<span class="legend-item"><span class="legend-glyph legend-glyph-${info.glyph}"></span>${escapeHtml(info.label)}</span>`
      ).join('');
    }
  }
  renderLegend();

  // Descendants (via parent_session_id) for the kill-modal preview — every
  // known session already lives in `allSessions`, so this is synchronous,
  // unlike the Board drawer's own on-demand fetch (web/agents/board.js).
  function descendantsOf(session) {
    return sharedDescendantsOf(allSessions, session);
  }

  const panel = new SessionPanel({
    container: panelEl,
    getDescendants: descendantsOf,
    // `boardApi.findCard` resolves a card by id from the board's own live
    // state — the same lookup the Board drawer's Delete confirmation uses
    // to re-resolve the freshest copy of the card at confirm time, so
    // Delete on the Graph tab makes its kill-first decision from a live
    // card rather than the one captured when the panel was opened.
    findCard: (boardApi && boardApi.findCard) || null,
    onCardChanged: () => { if (boardApi && boardApi.refresh) boardApi.refresh(); },
    onCardAccepted: () => {
      // Accept is a selection-changing action. Use the same full close path
      // as clicking away/closing the panel so the selected node, action row,
      // panel body, persistent board-link hint, and graph focus all clear
      // together and remain clear on the next snapshot.
      closePanel();
    },
    onLabelSaved: (sessionId, customLabel) => {
      const canonical = allSessions.find(x => x.session_id === sessionId);
      if (canonical) canonical.custom_label = customLabel;
      nodeLayer.selectAll('.node')
        .filter(d => d.session_id === sessionId)
        .each(function(d) { d.custom_label = customLabel; })
        .select('text.node-label')
        .each(renderNodeLabel);
      // A relabel resets that one node's tspans to the unboosted 12px
      // layout (`renderNodeLabel`'s own doing) — reapply whatever boost
      // is currently in effect so it doesn't fall out of step with every
      // other label.
      updateLabelLegibility(currentZoomK());
    },
    onSummaryFetched: (sessionId, shortLabel) => {
      const s = allSessions.find(x => x.session_id === sessionId);
      if (s) s.short_label = shortLabel;
      nodeLayer.selectAll('.node')
        .filter(d => d.session_id === sessionId)
        .each(function(d) { d.short_label = shortLabel; })
        .select('text.node-label')
        .each(renderNodeLabel);
      updateLabelLegibility(currentZoomK());
    },
  });

  function closePanel() {
    selectedSessionId = null;
    // A deep-link/tab activation may have queued a graph focus while the
    // panel was open. Closing is authoritative: consume that stale intent so
    // a later snapshot or activation cannot reopen a selection the operator
    // just dismissed.
    takeGraphFocus();
    panel.close();
    panelEl.innerHTML = '<div class="panel-empty" id="panel-empty">Click a node to inspect its transcript.</div>';
    applySelectionStyles();
    clearPanelActions();
    setSelectedGraphCardId(null);
  }

  function openPanel(sessionId) {
    const s = allSessions.find(x => x.session_id === sessionId);
    if (!s) return;
    selectedSessionId = sessionId;
    applySelectionStyles();
    // `getCardForSession` returns null both for a genuinely bare session
    // and for one whose linked card the board hasn't fetched yet (the
    // Graph tab can be opened before `initBoard()`'s first
    // `GET /api/agents/board` resolves) — `decideActions` already treats
    // an absent card as "no card-only actions", so this never throws or
    // renders a half-decided action set; the next snapshot tick's
    // `updateMeta` call below picks the card up once it's available.
    panel.open(s, getCardForSession(sessionId));
    renderPanelActions(s);
  }

  // --- Card actions above the transcript panel ---------------------------

  function clearPanelActions() {
    if (panelActionsEl) panelActionsEl.innerHTML = '';
  }

  function renderPanelActions(source) {
    if (!panelActionsEl) return;
    if (!source) { clearPanelActions(); setSelectedGraphCardId(null); return; }
    const cardId = source.card_id || null;
    setSelectedGraphCardId(cardId);
    let html = '';
    if (cardId) {
      html += `<button type="button" class="graph-panel-action" data-action="show-on-board">Show on board</button>`;
    }
    panelActionsEl.innerHTML = html;
    const showBtn = panelActionsEl.querySelector('[data-action="show-on-board"]');
    if (showBtn) {
      showBtn.addEventListener('click', () => {
        requestBoardFocus(cardId, { openDrawer: false });
        activateTab('board');
      });
    }
  }

  // Standalone Go To for the node dblclick handler — fires regardless of
  // whether the side panel is currently open for this session (SessionPanel's
  // own focus button only exists once its panel is rendered).
  async function focusSessionQuick(s) {
    try {
      const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/focus`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
      });
      if (!r.ok) {
        const text = await r.text();
        let msg = text;
        try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
        if (r.status === 404) {
          showToast(`Couldn't locate pane — session not running, wezterm unreachable, or SessionStart hook not installed.`, true);
        } else if (r.status === 410) {
          showToast(`Pane no longer exists. Click Resume to open a new one.`, true);
        } else {
          showToast(`Go To failed: ${msg}`, true);
        }
        return;
      }
      showToast(`Pane selected in wezterm. Click the wezterm dock icon to bring it forward.`, false);
    } catch (err) {
      showToast(`Go To failed: ${err.message}`, true);
    }
  }

  // -------------------------------------------------------------------
  // D3 renders and zooms the graph; coordinates are computed deterministically.
  // -------------------------------------------------------------------
  const svg = d3.select('#graph-svg');
  const VIEW_W = 1000;
  const VIEW_H = 700;
  svg.attr('viewBox', `0 0 ${VIEW_W} ${VIEW_H}`);
  svg.attr('data-zoom-k', '1');
  svg.style('overflow', 'visible');
  const viewport = svg.append('g').attr('class', 'viewport');
  const guideLayer = viewport.append('g').attr('class', 'timeline-guides');
  const linkLayer = viewport.append('g').attr('class', 'links');
  const nodeLayer = viewport.append('g').attr('class', 'nodes');

  // A `.node-label`'s on-screen CSS pixel size is its font-size in SVG
  // user-space units (12px, set in web/agents.html) times BOTH the zoom
  // transform's own scale (`k`) AND the ratio between the SVG element's
  // actual rendered width and its `viewBox` width — `#graph-svg` sits next
  // to `#panel-outer` (the side panel), so that ratio is well under 1 at
  // any realistic viewport, not the ~1 a `k`-only threshold implicitly
  // assumes. At `k = 1` (the resting zoom, what Reset restores) on a
  // 1280×800 viewport that works out to well under half the ~11px a label
  // needs to stay legible.
  //
  // So: measure the real on-screen size and counter-scale the label's own
  // font-size (in user-space units) just enough to hold it at the legible
  // floor whenever the natural size would fall under it — zoomed in far
  // enough that labels are already comfortably sized, nothing changes.
  // Below a point, though, no reasonable font-size compensates without the
  // labels themselves overlapping and cluttering a dense, zoomed-far-out
  // graph — past `LABEL_MAX_BOOST_PX`, hide them instead and rely on the
  // hover card (`showHoverCard`, below) to name a node, exactly as before.
  const LABEL_BASE_FONT_PX = 12;   // matches `.node-label`'s CSS font-size
  const LABEL_MIN_SCREEN_PX = 11;  // never render a shown label under this
  const LABEL_MAX_BOOST_PX = 36;   // beyond this, hide rather than enlarge further

  // The label layout metrics below (`LABEL_CHAR_W`/`LABEL_LINE_H`/
  // `LABEL_GAP`, defined further down alongside `renderNodeLabel`) are all
  // tuned for the 12px base font. Counter-scaling `font-size` without
  // scaling these by the same factor is what makes a boosted multi-line
  // label's lines draw on top of each other (the per-tspan `dy` stays a
  // fixed 12px-era user-space value), so apply the same boost to spacing.

  // `#graph-svg` carries `preserveAspectRatio="xMidYMid meet"`
  // (web/agents.html) — the SVG is letterboxed to fit inside its rendered
  // box, so the real user-space→screen scale is the SMALLER of the width
  // and height ratios, never the width ratio alone. Using the width ratio
  // alone overestimates the scale (and so undercounts the needed boost)
  // whenever the graph area is height-bound — a short browser window, or
  // the side panel dragged wide.
  function svgScreenScale() {
    const rect = svg.node().getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return 1;
    return Math.min(rect.width / VIEW_W, rect.height / VIEW_H);
  }

  function currentZoomK() {
    return parseFloat(svg.attr('data-zoom-k')) || 1;
  }

  // Re-derives every label's own line spacing (the gap from its node and
  // each tspan's `dy`) for the given boost factor, so a boosted font's
  // lines stay the same distance apart, proportionally, as the unboosted
  // 12px layout — never overlapping regardless of how large the
  // counter-scaled font gets.
  function applyLabelBoost(boost) {
    nodeLayer.selectAll('.node').each(function(d) {
      const label = d3.select(this).select('text.node-label');
      if (label.empty()) return;
      label.attr('y', nodeRadius(d) + LABEL_GAP * boost);
      label.selectAll('tspan').each(function(_, i) {
        if (i > 0) d3.select(this).attr('dy', LABEL_LINE_H * boost);
      });
    });
  }

  function updateLabelLegibility(k) {
    const screenScale = svgScreenScale();
    const naturalPx = LABEL_BASE_FONT_PX * k * screenScale;
    let boost = 1;
    let hide = false;
    if (naturalPx < LABEL_MIN_SCREEN_PX) {
      const neededUserPx = LABEL_MIN_SCREEN_PX / (k * screenScale);
      if (neededUserPx <= LABEL_MAX_BOOST_PX) {
        boost = neededUserPx / LABEL_BASE_FONT_PX;
      } else {
        hide = true;
      }
    }
    nodeLayer.classed('labels-below-legible', hide);
    nodeLayer.selectAll('.node-label')
      .style('font-size', (!hide && boost !== 1) ? `${LABEL_BASE_FONT_PX * boost}px` : null);
    // A hidden label has no on-screen footprint to defend against — treat
    // it as unboosted for layout/collision purposes.
    const effectiveBoost = hide ? 1 : boost;
    applyLabelBoost(effectiveBoost);
  }

  const zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on('zoom', (event) => {
      viewport.attr('transform', event.transform);
      svg.attr('data-zoom-k', event.transform.k);
      updateLabelLegibility(event.transform.k);
    });
  svg.call(zoom);
  // The screen-scale factor above depends on the SVG's own rendered
  // width, which changes independent of any zoom event — the side panel
  // resizer (below) or the browser window itself. Re-measure whenever it
  // does, at whatever zoom is currently in effect.
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => updateLabelLegibility(currentZoomK())).observe(svg.node());
  }
  // d3.zoom's own double-click-to-zoom would otherwise fire alongside the
  // node dblclick handler below on every double-click anywhere on the
  // canvas, including non-CLI nodes that have no focus action of their own.
  svg.on('dblclick.zoom', null);
  svg.style('cursor', 'grab');
  svg.on('mousedown.cursor', () => svg.style('cursor', 'grabbing'));
  svg.on('mouseup.cursor',   () => svg.style('cursor', 'grab'));

  svg.on('click', (event) => {
    if (event.target === svg.node() && selectedSessionId) closePanel();
  });

  function transitionMs() {
    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    return reduced ? 0 : 300;
  }

  function zoomFit() {
    const nodes = nodeLayer.selectAll('.node').data();
    if (!nodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    nodes.forEach(d => {
      const pad = nodeRadius(d) + 24 + (d._labelW ? d._labelW / 2 : 0);
      minX = Math.min(minX, d.x - pad); maxX = Math.max(maxX, d.x + pad);
      minY = Math.min(minY, d.y - pad); maxY = Math.max(maxY, d.y + pad);
    });
    const w = Math.max(1, maxX - minX);
    const h = Math.max(1, maxY - minY);
    // `zoom.transform` operates in the SVG's viewBox coordinate space (same
    // space node `x`/`y` are already in — see `panToNode`), not CSS pixels,
    // so the fit box is `VIEW_W`/`VIEW_H`, never the element's client rect.
    const scale = Math.max(0.2, Math.min(5, 0.9 / Math.max(w / VIEW_W, h / VIEW_H)));
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const tx = VIEW_W / 2 - scale * cx;
    const ty = VIEW_H / 2 - scale * cy;
    svg.transition().duration(transitionMs())
      .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }

  function zoomReset() {
    svg.transition().duration(transitionMs()).call(zoom.transform, d3.zoomIdentity);
  }

  if (zoomFitBtn) zoomFitBtn.addEventListener('click', zoomFit);
  if (zoomResetBtn) zoomResetBtn.addEventListener('click', zoomReset);

  let visibleCount = 0;
  function hostOf(s) {
    return s.host || apiHost || 'unknown';
  }

  function shortenCwd(p) {
    if (!p) return p;
    const m = p.match(/^\/(home|Users)\/[^/]+(\/.*)?$/);
    if (m) return m[2] || '/';
    return p;
  }

  let _lastCwdOptionKey = '';
  function updateCwdOptions(sessions) {
    if (!filterCwdEl) return;
    const cwds = [...new Set(sessions.map(s => s.decoded_cwd).filter(Boolean))].sort();
    const key = cwds.join('|');
    if (key === _lastCwdOptionKey) return;
    _lastCwdOptionKey = key;
    const current = filterCwdEl.value;
    filterCwdEl.innerHTML = '<option value="all">all</option>'
      + cwds.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(shortenCwd(c))}</option>`).join('');
    if (current && (current === 'all' || cwds.includes(current))) {
      filterCwdEl.value = current;
    }
    const wrap = filterCwdEl.closest('label');
    if (wrap) wrap.style.display = cwds.length > 0 ? '' : 'none';
  }

  // Host filter (#849) — same pattern as cwd above: options derive from
  // whatever hosts are present in the current snapshot (local + any
  // cross-machine cli_sessions rows), hidden entirely on a single-host
  // deployment where the filter has nothing to distinguish.
  let _lastHostOptionKey = '';
  function updateHostOptions(sessions) {
    if (!filterHostEl) return;
    const hosts = [...new Set(sessions.map(s => s.host).filter(Boolean))].sort();
    const key = hosts.join('|');
    if (key === _lastHostOptionKey) return;
    _lastHostOptionKey = key;
    const current = filterHostEl.value;
    filterHostEl.innerHTML = '<option value="all">all</option>'
      + hosts.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join('');
    // `host` is shared (linking.js) — the persisted/cross-tab value wins
    // over the select's own pre-repopulation value once it's actually a
    // valid option, so a host filter restored from localStorage before
    // this option list existed yet still lands once it can.
    const preferred = getFilters().host;
    if (preferred && (preferred === 'all' || hosts.includes(preferred))) {
      filterHostEl.value = preferred;
    } else if (current && (current === 'all' || hosts.includes(current))) {
      filterHostEl.value = current;
    }
    const wrap = filterHostEl.closest('label');
    if (wrap) wrap.style.display = hosts.length > 1 ? '' : 'none';
  }

  function applyFilters(sessions) {
    const showTerm = filterTerminalEl.checked;
    const status = filterStatusEl.value;
    const recencyRaw = filterRecencyEl ? filterRecencyEl.value : 'all';
    const recencySec = (recencyRaw === 'all') ? null : Number(recencyRaw);
    const cwdSel = filterCwdEl ? filterCwdEl.value : 'all';
    // Lane/assignee/tag/engine ("route")/host are the shared filters —
    // read straight from the shared store rather than trusting the DOM
    // mirror (`#filter-lane` etc., kept in sync by `syncSharedControls`
    // below) is always up to date.
    const shared = getFilters();
    const route = shared.engine;
    const hostSel = shared.host;
    const laneSet = new Set(shared.lanes);
    const assigneeSel = shared.assignee;
    const tagQuery = (shared.tag || '').trim().toLowerCase();
    const searchQueryLower = (shared.search || '').trim().toLowerCase();
    const nowSec = Date.now() / 1000;
    return sessions.filter(s => {
      if (!showTerm && TERMINAL.has(s.status)) return false;
      if (recencySec !== null && s.last_activity_at
          && (nowSec - s.last_activity_at) > recencySec) {
        return false;
      }
      if (cwdSel !== 'all' && s.decoded_cwd !== cwdSel) return false;
      if (hostSel !== 'all' && s.host !== hostSel) return false;
      if (route !== 'all' && routingFilterValue(s) !== route) return false;
      if (status !== 'all' && s.status !== status) return false;
      // `s.lane` is only ever absent from a fixture synthesized without it
      // (predating linked-card metadata) — treat that as "not excludable by
      // lane" rather than hiding it, since a real snapshot row always
      // carries one. The `done` lane is the one exception: whether a
      // done-lane session renders is decided by `showTerm` (include
      // finished) — not the shared lane selection — so the two controls
      // never fight over the exact same set of sessions.
      if (s.lane != null && s.lane !== 'done' && !laneSet.has(s.lane)) return false;
      if (assigneeSel !== 'all') {
        if (assigneeSel === 'unassigned') { if (s.assignee) return false; }
        else if (s.assignee !== assigneeSel) return false;
      }
      if (tagQuery && !(s.card_tags || []).some(t => t.toLowerCase().includes(tagQuery))) return false;
      if (searchQueryLower && !sessionMatchesSearch(s, searchQueryLower)) return false;
      return true;
    });
  }

  // The shared `search` text's match against a session — case-insensitive
  // against the node's own label, its LLM-generated short label, its linked
  // card's title, and its linked card's tags. Shared with
  // `relaxSharedFiltersFor` below, so a search-dropdown result chosen for a
  // session that doesn't actually match this predicate (e.g. it matched only
  // via the server-side transcript-summary search) has its shared search
  // cleared rather than becoming permanently unreachable.
  function sessionMatchesSearch(s, q) {
    if (!q) return true;
    const haystack = [nodeLabel(s), s.short_label, s.card_title, ...(s.card_tags || [])]
      .filter(Boolean)
      .map(v => String(v).toLowerCase());
    return haystack.some(h => h.includes(q));
  }

  // Active branches stay visible. Terminal-only branches collapse into a
  // count badge until their parent is explicitly expanded.
  function applyCollapse(filtered) {
    const byId = new Map(filtered.map(s => [s.session_id, s]));
    const activeBranch = new Set(filtered.filter(s => !TERMINAL.has(s.status)).map(s => s.session_id));
    for (const session of filtered) {
      if (!activeBranch.has(session.session_id)) continue;
      let parent = byId.get(session.parent_session_id);
      while (parent && !activeBranch.has(parent.session_id)) {
        activeBranch.add(parent.session_id);
        parent = byId.get(parent.parent_session_id);
      }
    }
    const visibleMemo = new Map();
    function isVisible(session, visiting = new Set()) {
      if (visibleMemo.has(session.session_id)) return visibleMemo.get(session.session_id);
      const parent = byId.get(session.parent_session_id);
      if (!parent || visiting.has(session.session_id)) return true;
      const next = new Set(visiting);
      next.add(session.session_id);
      const shown = isVisible(parent, next) && (
        expandedParents.has(parent.session_id) || activeBranch.has(session.session_id)
      );
      visibleMemo.set(session.session_id, shown);
      return shown;
    }
    return filtered.filter(session => isVisible(session));
  }

  // Direct-child counts per parent (in the filtered set, before collapse) —
  // used both for the badge's hidden-count text and to decide whether the
  // badge should render at all. A parent with children keeps its badge
  // visible even fully expanded (0 currently hidden): otherwise expanding
  // would remove the only affordance that collapses it back.
  function totalChildCounts(filtered) {
    const ids = new Set(filtered.map(s => s.session_id));
    const children = new Map();
    for (const session of filtered) {
      if (!children.has(session.parent_session_id)) children.set(session.parent_session_id, []);
      children.get(session.parent_session_id).push(session);
    }
    const activeMemo = new Map();
    function branchIsActive(session, visiting = new Set()) {
      if (activeMemo.has(session.session_id)) return activeMemo.get(session.session_id);
      if (!TERMINAL.has(session.status)) return true;
      if (visiting.has(session.session_id)) return false;
      const next = new Set(visiting);
      next.add(session.session_id);
      const active = (children.get(session.session_id) || []).some(child => branchIsActive(child, next));
      activeMemo.set(session.session_id, active);
      return active;
    }
    const counts = new Map();
    for (const s of filtered) {
      if (!s.parent_session_id) continue;
      if (!ids.has(s.parent_session_id)) continue;
      if (branchIsActive(s)) continue;
      counts.set(s.parent_session_id, (counts.get(s.parent_session_id) || 0) + 1);
    }
    return counts;
  }

  function applyRecencyDefault() {
    if (recencyManuallySet || !filterRecencyEl) return;
    filterRecencyEl.value = filterTerminalEl.checked ? '604800' : '1800';
  }
  applyRecencyDefault();

  function hexWithAlpha(hex, alpha) {
    const h = hex.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function nodeRadius(d) {
    return radiusForActiveSeconds(d.total_active_seconds);
  }

  // Fill = the session's board lane colour (shared with the board — see
  // web/agents/lanes.js); status stays visible via the stroke: a thicker
  // border for `blocked`, and reduced fill opacity once a session is
  // terminal. The live-pulse animation is a CSS keyframe via `.pulsing`.
  function nodeColors(d) {
    const fillHex = laneColor(d.lane);
    const isTerm = TERMINAL.has(d.status);
    return {
      fill: isTerm ? hexWithAlpha(fillHex, 0.35) : hexWithAlpha(fillHex, 0.85),
      stroke: STATUS_COLORS[d.status] || '#6b7280',
      borderWidth: d.status === 'blocked' ? 4 : 2,
    };
  }

  const LABEL_CHAR_W = 6.6;
  const LABEL_MAX_W = 132;
  const LABEL_MAX_CHARS = Math.floor(LABEL_MAX_W / LABEL_CHAR_W);
  const LABEL_LINE_H = 13;
  const LABEL_MAX_LINES = 3;
  const LABEL_GAP = 14;

  function wrapLabelText(str, maxChars) {
    const words = String(str).split(/\s+/).filter(Boolean);
    const lines = [];
    let line = '';
    for (let w of words) {
      while (w.length > maxChars) {
        if (line) { lines.push(line); line = ''; }
        lines.push(w.slice(0, maxChars));
        w = w.slice(maxChars);
      }
      const candidate = line ? line + ' ' + w : w;
      if (candidate.length > maxChars) {
        if (line) lines.push(line);
        line = w;
      } else {
        line = candidate;
      }
    }
    if (line) lines.push(line);
    return lines.length ? lines : [''];
  }

  function renderNodeLabel(d) {
    const textEl = d3.select(this);
    let lines = wrapLabelText(nodeLabel(d), LABEL_MAX_CHARS);
    if (lines.length > LABEL_MAX_LINES) {
      lines = lines.slice(0, LABEL_MAX_LINES);
      let last = lines[LABEL_MAX_LINES - 1];
      if (last.length >= LABEL_MAX_CHARS) last = last.slice(0, LABEL_MAX_CHARS - 1);
      lines[LABEL_MAX_LINES - 1] = last.replace(/\s+$/, '') + '…';
    }
    textEl.attr('y', nodeRadius(d) + LABEL_GAP).text(null);
    lines.forEach((ln, i) => {
      textEl.append('tspan')
        .attr('x', 0)
        .attr('dy', i === 0 ? 0 : LABEL_LINE_H)
        .text(ln);
    });
    d._labelLines = lines.length;
    d._labelW = Math.max(...lines.map(l => l.length)) * LABEL_CHAR_W;
  }

  function isActivelyWriting(d) {
    const nowSec = Date.now() / 1000;
    return d.status === 'running'
      && d.last_activity_at
      && (nowSec - d.last_activity_at) < 60;
  }

  // Regular-hexagon points, flat-top, centered on the origin.
  function hexagonPoints(r) {
    const pts = [];
    for (let i = 0; i < 6; i++) {
      const angle = (Math.PI / 3) * i - Math.PI / 2;
      pts.push(`${(r * Math.cos(angle)).toFixed(2)},${(r * Math.sin(angle)).toFixed(2)}`);
    }
    return pts.join(' ');
  }

  // Five-point star path, centered on the origin.
  function starPath(r) {
    const outer = r, inner = r * 0.42;
    let d = '';
    for (let i = 0; i < 10; i++) {
      const rad = i % 2 === 0 ? outer : inner;
      const angle = (Math.PI / 5) * i - Math.PI / 2;
      d += (i === 0 ? 'M' : 'L') + (rad * Math.cos(angle)).toFixed(2) + ',' + (rad * Math.sin(angle)).toFixed(2) + ' ';
    }
    return d + 'Z';
  }

  function applyShapeAttrs(sel) {
    sel.each(function(d) {
      const el = d3.select(this);
      const r = nodeRadius(d);
      const colors = nodeColors(d);
      const glyph = ENGINE_SHAPES[engineOf(d)].glyph;
      el.attr('fill', colors.fill).attr('stroke', colors.stroke)
        .attr('stroke-width', colors.borderWidth)
        .attr('data-shape', glyph)
        .classed('pulsing', isActivelyWriting(d));
      if (this.tagName === 'circle') {
        el.attr('r', r);
      } else if (this.tagName === 'rect') {
        const side = r * 1.8;
        el.attr('x', -side / 2).attr('y', -side / 2)
          .attr('width', side).attr('height', side)
          .attr('rx', Math.min(side * 0.22, 12))
          .attr('ry', Math.min(side * 0.22, 12));
      } else if (this.tagName === 'polygon') {
        if (glyph === 'hexagon') {
          el.attr('points', hexagonPoints(r * 1.15));
        } else {
          const h = r * 1.4;
          el.attr('points', `0,${-h} ${h},0 0,${h} ${-h},0`);
        }
      } else if (this.tagName === 'path') {
        el.attr('d', starPath(r * 1.2));
      }
    });
  }

  // Secondary tool-call ring — a thin accent circle around the node whose
  // width is `ringWidthForToolCalls(tool_call_count)`.
  function applyToolRing(sel) {
    sel.each(function(d) {
      const r = nodeRadius(d);
      const width = ringWidthForToolCalls(d.tool_call_count);
      d3.select(this)
        .attr('r', r + 4 + width / 2)
        .attr('stroke-width', width);
    });
  }

  // Badges: a question ring+glyph when a pending question is open for the
  // operator, an error count, and (on a collapsed parent) the hidden
  // direct-child count. All three are offset from the label, positioned at
  // fixed corners of the node so they never collide with it.
  function applyBadges(sel) {
    sel.each(function(d) {
      const g = d3.select(this);
      const r = nodeRadius(d);
      const hasQuestion = !!d.pending_question;
      g.select('.node-badge-question-ring')
        .style('display', hasQuestion ? '' : 'none')
        .attr('cx', r * 0.85).attr('cy', -r * 0.85).attr('r', 8);
      g.select('text.node-badge-question')
        .style('display', hasQuestion ? '' : 'none')
        .attr('x', r * 0.85).attr('y', -r * 0.85)
        .text('?');

      const errorCount = d.error_count || 0;
      g.select('text.node-badge-errors')
        .style('display', errorCount > 0 ? '' : 'none')
        .attr('x', r * 0.85).attr('y', r * 0.85 + 4)
        .text(errorCount > 99 ? '99+' : String(errorCount));

      const totalChildren = d._totalChildren || 0;
      const hiddenChildren = d._collapsedChildren || 0;
      g.select('.node-badge-children-hit')
        .style('display', totalChildren > 0 ? '' : 'none')
        .attr('cx', -r * 0.85).attr('cy', -r * 0.85 + 1).attr('r', 16);
      g.select('text.node-badge-children')
        .style('display', totalChildren > 0 ? '' : 'none')
        .attr('x', -r * 0.85).attr('y', -r * 0.85 + 4)
        .text(hiddenChildren > 0 ? '+' + (hiddenChildren > 99 ? '99+' : hiddenChildren) : '−');
    });
  }

  function showHoverCard(event, d) {
    if (!hoverCardEl) return;
    const rows = hoverCardRows(d);
    hoverCardEl.innerHTML = `<div class="hc-title">${escapeHtml(nodeLabel(d))}</div>`
      + rows.map(([label, value]) =>
        `<div class="hc-row"><span class="hc-label">${escapeHtml(label)}</span><span class="hc-value">${escapeHtml(String(value))}</span></div>`
      ).join('');
    // Unhide before positioning — `positionHoverCard` measures the card's
    // rendered size to clamp it inside the viewport, which needs it laid
    // out (non-`hidden`) first.
    hoverCardEl.hidden = false;
    positionHoverCard(event);
  }

  function positionHoverCard(event) {
    if (!hoverCardEl) return;
    const pad = 16;
    const rect = hoverCardEl.getBoundingClientRect();
    const left = Math.max(0, Math.min(event.clientX + pad, window.innerWidth - rect.width - pad));
    const top = Math.max(0, Math.min(event.clientY + pad, window.innerHeight - rect.height - pad));
    hoverCardEl.style.left = left + 'px';
    hoverCardEl.style.top = top + 'px';
  }

  function hideHoverCard() {
    if (hoverCardEl) hoverCardEl.hidden = true;
  }

  function toggleParentExpanded(sessionId) {
    if (expandedParents.has(sessionId)) expandedParents.delete(sessionId);
    else expandedParents.add(sessionId);
    renderGraph(allSessions);
  }

  function renderGraph(sessions) {
    const filtered = applyFilters(sessions);
    const visible = applyCollapse(filtered);
    const totalCounts = totalChildCounts(filtered);
    const visibleIds = new Set(visible.map(s => s.session_id));

    const visibleLinks = visible
      .filter(session => session.parent_session_id && visibleIds.has(session.parent_session_id))
      .map(session => ({
        id: `${session.parent_session_id}->${session.session_id}`,
        source: session.parent_session_id,
        target: session.session_id,
      }));

    linkLayer.selectAll('path.link')
      .data(visibleLinks, d => d.id)
      .join(
        enter => enter.append('path').attr('class', 'link'),
        update => update.attr('class', 'link'),
        exit => exit.remove()
      );

    const merged = delegationTimelineLayout(visible).map(row => {
      const total = totalCounts.get(row.session_id) || 0;
      row._totalChildren = total;
      row._collapsedChildren = expandedParents.has(row.session_id) ? 0 : total;
      return row;
    });

    const sel = nodeLayer.selectAll('.node')
      .data(merged, d => d.session_id);

    const entered = sel.enter().append('g')
      .attr('class', 'node')
      .on('click', (event, d) => {
        // `event.detail` is the click count in the browser's own
        // click/click/dblclick sequence — the second click of a
        // double-click carries `detail === 2`. Ignoring it here means a
        // real double-click only ever opens the panel (from the first
        // click) and never also toggles it back closed.
        if (event.detail > 1) return;
        if (d.session_id === selectedSessionId) closePanel();
        else openPanel(d.session_id);
      })
      .on('dblclick', (event, d) => {
        const engine = engineOf(d);
        const isCli = engine === 'claude_code' || engine === 'codex';
        if (!isCli || isSubagentSession(d)) return;
        event.preventDefault();
        event.stopPropagation();
        // The double-click's own first click (`detail === 1`) toggles an
        // already-selected node's panel closed, which the `click` handler's
        // own listener runs ahead of this `dblclick` handler — reopen the
        // panel here so a double-click on a selected CLI node still shows
        // it, not just fires focus.
        if (selectedSessionId !== d.session_id) openPanel(d.session_id);
        focusSessionQuick(d);
      })
      .on('mouseenter', (event, d) => showHoverCard(event, d))
      .on('mousemove', (event) => positionHoverCard(event))
      .on('mouseleave', () => hideHoverCard());
    entered.append(d => document.createElementNS('http://www.w3.org/2000/svg', shapeTagFor(engineOf(d))))
      .attr('class', 'node-shape');
    entered.append('circle').attr('class', 'node-ring-tools')
      .attr('fill', 'none').attr('stroke', 'rgba(232,232,237,0.35)');
    entered.append('text').attr('class', 'node-label');
    entered.append('circle').attr('class', 'node-badge-question-ring')
      .attr('fill', 'none').attr('stroke', 'var(--accent, #6366f1)').attr('stroke-width', 2);
    entered.append('text').attr('class', 'node-badge-question')
      .attr('text-anchor', 'middle').attr('font-size', 11).attr('fill', 'var(--accent, #6366f1)');
    entered.append('text').attr('class', 'node-badge-errors')
      .attr('text-anchor', 'middle').attr('font-size', 10).attr('fill', '#f87171');
    // A transparent circle behind the badge text, sized for a real click
    // target — the text glyph alone renders only a few pixels tall.
    entered.append('circle').attr('class', 'node-badge-children-hit')
      .attr('fill', 'transparent')
      .style('cursor', 'pointer').style('pointer-events', 'all')
      .on('click', (event, d) => { event.stopPropagation(); toggleParentExpanded(d.session_id); });
    entered.append('text').attr('class', 'node-badge-children')
      .attr('text-anchor', 'middle').attr('font-size', 10).attr('fill', '#e8e8ed')
      .style('cursor', 'pointer')
      .on('click', (event, d) => { event.stopPropagation(); toggleParentExpanded(d.session_id); });

    const all = entered.merge(sel);
    // A routing change on an existing node can change its engine (and
    // therefore its shape's SVG tag, e.g. a `<polygon>` diamond becoming a
    // `<rect>` square) — `applyShapeAttrs` below only sets attributes on
    // whatever tag is already there, so swap the element itself first
    // whenever the wanted tag doesn't match the one currently mounted.
    all.each(function(d) {
      const shapeEl = this.querySelector('.node-shape');
      const wantedTag = shapeTagFor(engineOf(d));
      if (shapeEl && shapeEl.tagName.toLowerCase() !== wantedTag) {
        const replacement = document.createElementNS('http://www.w3.org/2000/svg', wantedTag);
        replacement.setAttribute('class', 'node-shape');
        shapeEl.replaceWith(replacement);
      }
    });
    applyShapeAttrs(all.select('.node-shape'));
    applyToolRing(all.select('.node-ring-tools'));
    all.select('text.node-label').each(renderNodeLabel);
    applyBadges(all);
    // Newly-entered labels start at the CSS default font-size — size them
    // to the currently-in-effect zoom immediately, not just on the next
    // zoom/resize event (covers the very first render, at the identity
    // transform, before any zoom event has ever fired).
    updateLabelLegibility(currentZoomK());

    sel.exit().remove();

    // Anchor rendering mirrors the session-node join above (preserve x/y
    // across re-renders via `oldAnchorsById`) but never runs through
    // `applyShapeAttrs`/`applyBadges`/`renderNodeLabel` — those are the
    // session node's own rendering, off limits here by design.
    const byId = new Map(merged.map(row => [row.session_id, row]));

    visibleCount = merged.length;
    nodeLayer.selectAll('.node')
      .attr('transform', d => `translate(${d.x},${d.y})`)
      .attr('data-depth', d => d._delegationDepth)
      .attr('data-timeline-order', d => d._timelineOrder);
    linkLayer.selectAll('path.link').attr('d', edge => {
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      if (!source || !target) return '';
      const midX = source.x + (target.x - source.x) / 2;
      return `M${source.x},${source.y} C${midX},${source.y} ${midX},${target.y} ${target.x},${target.y}`;
    });

    const maxX = merged.reduce((max, row) => Math.max(max, row.x), 130);
    const depths = [...new Set(merged.map(row => row._delegationDepth))].sort((a, b) => a - b);
    guideLayer.selectAll('line.timeline-row')
      .data(depths, d => d)
      .join('line')
      .attr('class', 'timeline-row')
      .attr('x1', 70).attr('x2', maxX + 90)
      .attr('y1', d => 120 + d * 190).attr('y2', d => 120 + d * 190);
    guideLayer.selectAll('text.depth-label')
      .data(depths, d => d)
      .join('text')
      .attr('class', 'depth-label')
      .attr('x', 70).attr('y', d => 124 + d * 190)
      .text(d => d === 0 ? 'ROOT' : `LEVEL ${d}`);
    guideLayer.selectAll('text.time-label')
      .data(merged.length ? ['EARLIER', 'LATER'] : [])
      .join('text')
      .attr('class', 'time-label')
      .attr('x', d => d === 'EARLIER' ? 130 : maxX)
      .attr('y', 44)
      .attr('text-anchor', d => d === 'EARLIER' ? 'start' : 'end')
      .text(d => d);

    emptyStateEl.style.display = visible.length === 0 ? '' : 'none';
    updateChips(filtered);
    updateCwdOptions(allSessions);
    updateHostOptions(allSessions);
    applySelectionStyles();
  }

  function linkEndpoints(e) {
    const s = (typeof e.source === 'object') ? e.source.session_id : e.source;
    const t = (typeof e.target === 'object') ? e.target.session_id : e.target;
    return [s, t];
  }

  function relatedTo(sessionId) {
    const out = new Set();
    if (!sessionId) return out;
    const neighbors = new Map();
    linkLayer.selectAll('path.link').each(function(e) {
      const [s, t] = linkEndpoints(e);
      if (!neighbors.has(s)) neighbors.set(s, []);
      if (!neighbors.has(t)) neighbors.set(t, []);
      neighbors.get(s).push(t);
      neighbors.get(t).push(s);
    });
    const queue = [sessionId];
    const seen = new Set(queue);
    while (queue.length) {
      for (const neighbor of (neighbors.get(queue.shift()) || [])) {
        if (seen.has(neighbor)) continue;
        seen.add(neighbor);
        out.add(neighbor);
        queue.push(neighbor);
      }
    }
    return out;
  }

  function applySelectionStyles() {
    const hasSelection = !!selectedSessionId;
    const related = relatedTo(selectedSessionId);

    nodeLayer.selectAll('.node-shape')
      .classed('selected', d => hasSelection && d.session_id === selectedSessionId)
      .classed('related',  d => hasSelection && related.has(d.session_id))
      .classed('dimmed',   d => hasSelection && d.session_id !== selectedSessionId && !related.has(d.session_id));

    nodeLayer.selectAll('text.node-label')
      .classed('dimmed', d => hasSelection && d.session_id !== selectedSessionId && !related.has(d.session_id));

    linkLayer.selectAll('path.link')
      .classed('highlighted', e => {
        if (!hasSelection) return false;
        const [s, t] = linkEndpoints(e);
        const lineage = new Set([selectedSessionId, ...related]);
        return lineage.has(s) && lineage.has(t);
      })
      .classed('dimmed', e => {
        if (!hasSelection) return false;
        const [s, t] = linkEndpoints(e);
        const lineage = new Set([selectedSessionId, ...related]);
        return !lineage.has(s) || !lineage.has(t);
      });
  }

  function updateChips(sessions) {
    const running = sessions.filter(s => s.status === 'running').length;
    const blocked = sessions.filter(s => s.status === 'blocked').length;
    const recent = sessions.filter(s => s.status === 'completed' || s.status === 'ended').length;
    const cli = sessions.filter(s => s.source === 'claude_code' || s.source === 'codex').length;
    const apiSpend = sessions
      .filter(s => s.source !== 'claude_code' && s.source !== 'codex')
      .reduce((acc, s) => acc + (s.total_dollars || 0), 0);
    document.getElementById('chip-running').textContent = running;
    document.getElementById('chip-blocked').textContent = blocked;
    document.getElementById('chip-recent').textContent = recent;
    document.getElementById('chip-cc').textContent = cli;
    document.getElementById('chip-spend').textContent = '$' + apiSpend.toFixed(2);
  }

  function applySnapshot(snap) {
    allSessions = snap.sessions || [];
    allEdges = snap.edges || [];
    apiHost = snap.api_host || apiHost;
    snapshotLoaded = true;
    renderGraph(allSessions);
    if (selectedSessionId) {
      const s = allSessions.find(x => x.session_id === selectedSessionId);
      // Passing the card on every tick (not just at `open`) is what lets
      // an already-open panel pick up a card the board hadn't loaded yet
      // when the panel first opened, or a card whose lane/policy changed
      // after a board action fired from this same panel.
      if (s) {
        panel.updateMeta(s, getCardForSession(selectedSessionId));
        renderPanelActions(s);
      }
    }
    // Resolves a pending `?session=<id>` deep link, or a session chip's
    // `requestGraphFocus`, once this snapshot is the first to land after
    // the intent was set — a no-op on every other tick, since
    // `takeGraphFocus` consumes the intent on its first read regardless of
    // outcome.
    drainGraphFocus();
  }

  // --- Filters ---
  function onFilterChange() {
    svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity);
    renderGraph(allSessions);
    if (searchQuery.trim()) renderSearchResults();
  }
  // Re-renders for a shared-filter change (from either tab, a card-chip
  // jump's relaxation, or a keystroke in the search/tag inputs) WITHOUT
  // resetting the pan/zoom transform — unlike
  // `onFilterChange` above, which stays the right behaviour for the
  // graph's own local-only controls (recency/cwd/status/include-finished),
  // still called directly below.
  function renderForSharedChange() {
    renderGraph(allSessions);
    if (searchQuery.trim()) renderSearchResults();
  }
  filterTerminalEl.addEventListener('change', () => {
    applyRecencyDefault();
    onFilterChange();
  });
  if (filterRecencyEl) {
    filterRecencyEl.addEventListener('change', () => {
      recencyManuallySet = true;
      setFilter('recency', filterRecencyEl.value);
    });
  }
  [filterStatusEl, filterCwdEl].filter(Boolean).forEach(el =>
    el.addEventListener('change', onFilterChange)
  );

  // --- Shared filters: search/lanes/assignee/host/engine/tag/recency,
  // bound bidirectionally with the board's own filter bar via linking.js.
  // `#filter-route` becomes the shared engine control; `#filter-lane` (a
  // single select) sets the shared `lanes` array to just the chosen lane,
  // or every lane id for "all".
  if (filterRouteEl) filterRouteEl.addEventListener('change', () => setFilter('engine', filterRouteEl.value));
  if (filterHostEl) filterHostEl.addEventListener('change', () => setFilter('host', filterHostEl.value));
  if (filterLaneEl) {
    filterLaneEl.addEventListener('change', () => {
      const v = filterLaneEl.value;
      if (v === 'done') {
        // The `done` lane is never governed by the shared lane selection
        // (see `applyFilters`) — only by include-finished. Explicitly
        // picking it here means "show me only finished sessions", so tick
        // that checkbox too; otherwise the selection would show nothing at
        // all, which is worse than confusing.
        if (!filterTerminalEl.checked) {
          filterTerminalEl.checked = true;
          applyRecencyDefault();
        }
      }
      setFilter('lanes', v === 'all' ? LANES.map(l => l.id) : [v]);
    });
  }
  if (filterAssigneeEl) filterAssigneeEl.addEventListener('change', () => setFilter('assignee', filterAssigneeEl.value));
  if (filterTagEl) filterTagEl.addEventListener('input', () => setFilter('tag', filterTagEl.value));
  if (filterClearBtn) filterClearBtn.addEventListener('click', () => resetFilters());

  // --- Search (issue #252) ---
  const searchInputEl = document.getElementById('search-input');
  const searchResultsEl = document.getElementById('search-results');
  const searchWrapEl = document.getElementById('search-wrap');
  let searchQuery = '';
  let summaryMatches = new Map();
  let searchSeq = 0;
  let searchActiveIndex = -1;
  let searchDebounceTimer = null;

  function sessionDisplayName(s) {
    // The dropdown's display name is the node's label: one precedence chain
    // (`nodeLabel`, with its `isRawIdValue` guard), so the dropdown cannot
    // show an id the node refuses.
    return nodeLabel(s);
  }

  // Title for a dropdown result: the matched field's own value, unless that
  // value is a raw id (`isRawIdValue`) — then the display name.
  function searchResultTitle(s, field) {
    const own = field === 'label' ? (s.custom_label || s.label) : field === 'short_label' ? s.short_label : '';
    return (isRawIdValue(s, own) ? '' : own) || sessionDisplayName(s);
  }

  function highlightMatch(text, q) {
    const t = String(text || '');
    if (!q) return escapeHtml(t);
    const idx = t.toLowerCase().indexOf(q.toLowerCase());
    if (idx < 0) return escapeHtml(t);
    return escapeHtml(t.slice(0, idx))
      + '<mark>' + escapeHtml(t.slice(idx, idx + q.length)) + '</mark>'
      + escapeHtml(t.slice(idx + q.length));
  }

  function buildSearchResults() {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return [];
    const visibleIds = new Set(applyFilters(allSessions).map(s => s.session_id));
    const byId = new Map(allSessions.map(s => [s.session_id, s]));
    const entries = new Map();

    function consider(sessionId, field, snippet) {
      if (!isKnownSearchField(field)) return;
      const s = byId.get(sessionId);
      if (!s) return;
      const prev = entries.get(sessionId);
      if (prev && SEARCH_TIER[prev.field] <= SEARCH_TIER[field]) return;
      entries.set(sessionId, { session: s, field, snippet, visible: visibleIds.has(sessionId) });
    }

    for (const s of allSessions) {
      const name = s.custom_label || s.label || '';
      if (name.toLowerCase().includes(q)) consider(s.session_id, 'label', name);
    }
    for (const [sid, m] of summaryMatches) consider(sid, m.field, m.snippet);

    return [...entries.values()].sort((a, b) => {
      if (a.visible !== b.visible) return a.visible ? -1 : 1;
      if (SEARCH_TIER[a.field] !== SEARCH_TIER[b.field]) return SEARCH_TIER[a.field] - SEARCH_TIER[b.field];
      return sessionDisplayName(a.session).localeCompare(sessionDisplayName(b.session));
    });
  }

  function renderSearchResults() {
    const q = searchQuery.trim();
    if (!q) { hideSearchResults(); return; }
    searchActiveIndex = -1;
    const results = buildSearchResults();
    if (results.length === 0) {
      searchResultsEl.innerHTML = '<div class="search-empty">No matches</div>';
      searchResultsEl.hidden = false;
      return;
    }
    const visible = results.filter(r => r.visible);
    const hidden = results.filter(r => !r.visible);
    let html = '';
    const renderGroup = (items, groupClass, labelText) => {
      if (items.length === 0) return;
      if (labelText) html += `<div class="search-group-label">${labelText}</div>`;
      html += `<div class="search-group ${groupClass}">`;
      for (const r of items) {
        const titleText = searchResultTitle(r.session, r.field);
        const snippetHtml = r.field === 'summary'
          ? `<div class="sr-snippet">${highlightMatch(r.snippet, q)}</div>`
          : '';
        html += `<button class="search-result" type="button" data-session="${escapeHtml(r.session.session_id)}">`
          + `<div class="sr-title"><span class="sr-name">${highlightMatch(titleText, q)}</span>`
          + `<span class="sr-badge">${SEARCH_BADGE[r.field]}</span></div>`
          + snippetHtml + `</button>`;
      }
      html += `</div>`;
    };
    renderGroup(visible, 'visible-group', null);
    renderGroup(hidden, 'hidden-group', 'Hidden by filters');
    searchResultsEl.innerHTML = html;
    searchResultsEl.hidden = false;
  }

  function hideSearchResults() {
    searchResultsEl.hidden = true;
    searchResultsEl.innerHTML = '';
    searchActiveIndex = -1;
  }

  function relaxFiltersFor(s) {
    const nowSec = Date.now() / 1000;
    if (!filterTerminalEl.checked && TERMINAL.has(s.status)) filterTerminalEl.checked = true;
    if (filterRecencyEl && filterRecencyEl.value !== 'all' && s.last_activity_at) {
      const age = nowSec - s.last_activity_at;
      if (age > Number(filterRecencyEl.value)) {
        const fit = [...filterRecencyEl.options]
          .map(o => o.value)
          .find(v => v === 'all' || age <= Number(v));
        // Routed through the shared store (never the select directly) so
        // the board and localStorage see the widened window too, and so a
        // later shared-filter write in the same relaxation (see
        // `relaxSharedFiltersFor`) can't clobber it back to the old value —
        // `setFilter` updates the store first, and the sync callback it
        // triggers just reaffirms the value this already set.
        recencyManuallySet = true;
        setFilter('recency', fit || 'all');
      }
    }
    if (filterCwdEl && filterCwdEl.value !== 'all' && s.decoded_cwd !== filterCwdEl.value) {
      filterCwdEl.value = 'all';
    }
    if (filterStatusEl.value !== 'all' && s.status !== filterStatusEl.value) {
      filterStatusEl.value = 'all';
    }
  }

  // Expands every collapsed ancestor of `s` so a search hit inside a
  // collapsed subagent tree becomes visible.
  function expandAncestorsFor(s) {
    const byId = new Map(allSessions.map(x => [x.session_id, x]));
    let current = s;
    while (current && current.parent_session_id) {
      expandedParents.add(current.parent_session_id);
      current = byId.get(current.parent_session_id);
    }
  }

  function panToNode(sessionId, delay) {
    const run = () => {
      let target = null;
      nodeLayer.selectAll('.node').each(function(d) { if (d.session_id === sessionId) target = d; });
      if (!target || target.x == null || target.y == null) return;
      const k = Math.max(d3.zoomTransform(svg.node()).k, 1);
      const tx = VIEW_W / 2 - k * target.x;
      const ty = VIEW_H / 2 - k * target.y;
      svg.transition().duration(450)
        .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k));
    };
    if (delay) setTimeout(run, delay); else run();
  }

  // Delegates to `focusNode` (below) — the search dropdown is just one more
  // caller that needs to relax whatever LOCAL or SHARED filter currently
  // hides the picked session (a shared lane/assignee/host/engine/tag, not
  // only the graph's own local recency/cwd/status), so the operator doesn't
  // land on a fully-dimmed graph with nothing actually selected.
  function selectSearchResult(sessionId) {
    hideSearchResults();
    focusNode(sessionId);
  }

  async function fetchSummaryMatches(q) {
    const seq = ++searchSeq;
    try {
      const r = await fetch('/api/agents/search?q=' + encodeURIComponent(q));
      if (!r.ok) return;
      const data = await r.json();
      if (seq !== searchSeq || searchQuery.trim() !== q) return;
      summaryMatches = new Map((data.matches || []).map(m => [m.session_id, m]));
      renderSearchResults();
    } catch (_) { /* network blip — the label tier still works offline */ }
  }

  function onSearchInput() {
    searchQuery = searchInputEl.value;
    setFilter('search', searchQuery);
    const q = searchQuery.trim();
    clearTimeout(searchDebounceTimer);
    summaryMatches = new Map();
    if (q.length >= 2) {
      searchDebounceTimer = setTimeout(() => fetchSummaryMatches(q), 180);
    }
    renderSearchResults();
  }

  if (searchInputEl) {
    searchInputEl.addEventListener('input', onSearchInput);
    searchInputEl.addEventListener('focus', () => { if (searchQuery.trim()) renderSearchResults(); });
    searchInputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        searchInputEl.value = ''; searchQuery = ''; summaryMatches = new Map();
        hideSearchResults(); searchInputEl.blur();
        return;
      }
      const btns = [...searchResultsEl.querySelectorAll('.search-result')];
      if (!btns.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        searchActiveIndex = Math.min(searchActiveIndex + 1, btns.length - 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        searchActiveIndex = Math.max(searchActiveIndex - 1, 0);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const pick = searchActiveIndex >= 0 ? btns[searchActiveIndex] : btns[0];
        if (pick) selectSearchResult(pick.dataset.session);
        return;
      } else {
        return;
      }
      btns.forEach((b, i) => b.classList.toggle('active', i === searchActiveIndex));
      if (searchActiveIndex >= 0) btns[searchActiveIndex].scrollIntoView({ block: 'nearest' });
    });
    searchResultsEl.addEventListener('click', (e) => {
      const btn = e.target.closest('.search-result');
      if (btn) selectSearchResult(btn.dataset.session);
    });
    document.addEventListener('click', (e) => {
      if (searchWrapEl && !searchWrapEl.contains(e.target)) hideSearchResults();
    });
  }

  // --- Shared filters: reconciling every control against the shared store
  // once it changes, no matter which control or tab caused it.

  // A shared `lanes` selection can't always be represented exactly by
  // `#filter-lane`'s single-select — every lane maps to "all", exactly one
  // maps to that lane's own option, and anything else (e.g. the board's own
  // "every lane but Done" default) has no exact single-value display, so it
  // falls back to showing "all" rather than an arbitrary pick.
  function laneSelectValueFor(state) {
    if (state.lanes.length === 1) return state.lanes[0];
    return 'all';
  }

  function syncSharedFilterControls(state) {
    if (filterRouteEl && filterRouteEl.value !== state.engine) filterRouteEl.value = state.engine;
    // Only assigns when the shared host is already a valid option — the
    // options list itself is dynamic (`updateHostOptions`, above), which
    // also re-applies the shared value once a host that wasn't options yet
    // becomes one.
    if (filterHostEl && filterHostEl.value !== state.host) {
      const validHosts = [...filterHostEl.options].map(o => o.value);
      if (validHosts.includes(state.host)) filterHostEl.value = state.host;
    }
    if (filterLaneEl) {
      const want = laneSelectValueFor(state);
      if (filterLaneEl.value !== want) filterLaneEl.value = want;
    }
    if (filterAssigneeEl && filterAssigneeEl.value !== state.assignee) filterAssigneeEl.value = state.assignee;
    if (filterTagEl && document.activeElement !== filterTagEl && filterTagEl.value !== state.tag) {
      filterTagEl.value = state.tag;
    }
    if (searchInputEl && document.activeElement !== searchInputEl && searchInputEl.value !== state.search) {
      searchInputEl.value = state.search;
      searchQuery = state.search;
    }
    if (state.recency != null) {
      // A concrete shared recency value always wins over the
      // include-finished toggle's own auto-default from here on — a
      // restored/migrated/reset concrete value counts as "the operator
      // already chose one".
      if (filterRecencyEl && filterRecencyEl.value !== state.recency) filterRecencyEl.value = state.recency;
      recencyManuallySet = true;
    } else {
      // `null` means "the operator has never set it" — the graph keeps
      // deciding its own default (30 min, or 7 days once include-finished
      // is ticked) rather than being overwritten by the board's own
      // default (all time).
      recencyManuallySet = false;
      applyRecencyDefault();
    }
    renderForSharedChange();
  }
  subscribeFilters(syncSharedFilterControls);
  syncSharedFilterControls(getFilters());

  // Loosens whichever SHARED filters would hide session `s` — the shared
  // counterpart to `relaxFiltersFor` above (which only ever touches the
  // graph's own local-only filters: cwd/status; recency is routed through
  // the shared store too, see above). Every key that needs to change is
  // batched into one `setFilters` call so the FIRST key's own synchronous
  // sync-callback can't reconcile a control against a shared-store snapshot
  // that doesn't have the later keys' changes yet, undoing them. Returns
  // whether anything actually changed, so a caller that already knows a
  // shared change re-renders via the `subscribe` callback above doesn't
  // also force a second, redundant render of its own.
  function relaxSharedFiltersFor(s) {
    const state = getFilters();
    const updates = {};
    if (s.lane != null && !state.lanes.includes(s.lane)) {
      updates.lanes = [...state.lanes, s.lane];
    }
    if (state.assignee !== 'all') {
      const matches = state.assignee === 'unassigned' ? !s.assignee : s.assignee === state.assignee;
      if (!matches) updates.assignee = 'all';
    }
    if (state.host !== 'all' && s.host !== state.host) updates.host = 'all';
    if (state.engine !== 'all' && routingFilterValue(s) !== state.engine) updates.engine = 'all';
    if (state.tag && !(s.card_tags || []).some(t => t.toLowerCase().includes(state.tag.toLowerCase()))) {
      updates.tag = '';
    }
    // A shared search whose text doesn't actually match `s` (e.g. a
    // dropdown result found only via the server-side transcript-summary
    // search, not `sessionMatchesSearch`'s own label/tag fields) would
    // otherwise stay unreachable — clear it rather than leave the target
    // permanently filtered out of its own jump target.
    if (state.search) {
      const q = state.search.trim().toLowerCase();
      if (q && !sessionMatchesSearch(s, q)) updates.search = '';
    }
    if (Object.keys(updates).length === 0) return false;
    setFilters(updates);
    return true;
  }

  // Selects a session by id, relaxing whatever filters (local or shared)
  // currently hide it and expanding any collapsed ancestor first — the
  // model `selectSearchResult` above already follows, generalized for a
  // caller with no search UI of its own (a card's session chip, a URL
  // `?session=` deep link). Unknown ids are reported here rather than left
  // to fail silently.
  function focusNode(sessionId) {
    const s = allSessions.find(x => x.session_id === sessionId);
    if (!s) {
      showToast(`No such session: ${sessionId}`, true);
      // Leaves the default view (the Board tab) rather than stranding the
      // operator on a Graph tab that never resolved to anything — matters
      // most for a `?session=<id>` deep link that switched to this tab
      // before the id was known to be unresolvable.
      activateTab('board');
      return;
    }
    const visibleIds = new Set(applyFilters(allSessions).map(x => x.session_id));
    const wasFilteredOut = !visibleIds.has(sessionId);
    const wasCollapsed = !!(s.parent_session_id && !expandedParents.has(s.parent_session_id));
    let sharedChanged = false;
    if (wasFilteredOut) {
      relaxFiltersFor(s);
      sharedChanged = relaxSharedFiltersFor(s);
    }
    if (wasCollapsed) expandAncestorsFor(s);
    const needsRerender = wasFilteredOut || wasCollapsed;
    // A shared-filter change above already triggers its own re-render (via
    // `syncSharedFilterControls`, subscribed to the shared store) — only
    // force one here for the local-only relax/expand paths, which don't go
    // through it.
    if (needsRerender && !sharedChanged) {
      renderGraph(allSessions);
    }
    openPanel(sessionId);
    panToNode(sessionId, 0);
  }

  function drainGraphFocus() {
    const sessionId = takeGraphFocus();
    if (sessionId == null) return;
    if (!snapshotLoaded) {
      // Data hasn't arrived yet (a tab-activation drain can fire before the
      // first snapshot fetch resolves) — put the intent back so
      // `applySnapshot`'s own drain resolves it once it actually can,
      // rather than wrongly reporting a real session as unknown.
      requestGraphFocus(sessionId);
      return;
    }
    focusNode(sessionId);
  }
  onTabActivate((name) => { if (name === 'graph') drainGraphFocus(); });

  // --- Side panel resize ---
  (function setupPanelResizer() {
    if (!panelResizerEl || !panelOuterEl) return;
    const STORAGE_KEY = 'lifeos.agents.panelWidth';
    const MIN_WIDTH = 280;
    const MAX_RATIO = 0.7;
    const setWidth = (px) => {
      const max = Math.floor(window.innerWidth * MAX_RATIO);
      const clamped = Math.max(MIN_WIDTH, Math.min(max, Math.round(px)));
      document.documentElement.style.setProperty('--panel-width', clamped + 'px');
      return clamped;
    };
    try {
      const saved = parseInt(localStorage.getItem(STORAGE_KEY) || '', 10);
      if (saved && !isNaN(saved)) setWidth(saved);
    } catch (_) {}

    let dragging = false;
    let startX = 0;
    let startWidth = 0;
    panelResizerEl.addEventListener('mousedown', (e) => {
      dragging = true;
      startX = e.clientX;
      startWidth = panelOuterEl.getBoundingClientRect().width;
      panelResizerEl.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const newWidth = setWidth(startWidth + (startX - e.clientX));
      try { localStorage.setItem(STORAGE_KEY, String(newWidth)); } catch (_) {}
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      panelResizerEl.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      updateLabelLegibility(currentZoomK());
    });
  })();

  // --- Initial load + stream ---
  function fetchSnapshotOnce() {
    return fetch('/api/agents/snapshot')
      .then(r => r.json())
      .then(applySnapshot)
      .catch(err => { connStateEl.textContent = 'failed: ' + err; });
  }
  fetchSnapshotOnce();

  const snapshotES = new EventSource('/api/agents/stream');
  snapshotES.onopen = () => { connStateEl.textContent = 'live'; };
  snapshotES.onerror = () => { connStateEl.textContent = 'reconnecting…'; };
  snapshotES.addEventListener('snapshot', e => {
    try { applySnapshot(JSON.parse(e.data)); } catch (_) {}
  });
}
