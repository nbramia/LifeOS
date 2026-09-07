// web/agents/graph.js
//
// The Graph tab — the force-directed session map. Node rendering,
// simulation, filters, chips, and search live here; the side panel's
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
  STATUS_COLORS, TERMINAL, escapeHtml, showToast, SessionPanel,
} from './panel.js';
import { LANES } from './lanes.js';
import {
  nodeLabel, isRawIdValue, engineOf, ENGINE_SHAPES, shapeTagFor,
  radiusForActiveSeconds, ringWidthForToolCalls, laneColor,
  isKnownSearchField, SEARCH_TIER, SEARCH_BADGE, hoverCardRows,
} from './graph_encoding.js';

export function initGraph() {
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
  // Operator chooses recency manually → don't auto-flip on include-finished toggle.
  let recencyManuallySet = false;

  let allSessions = [];
  let allEdges = [];
  let selectedSessionId = null;
  let apiHost = '';

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

  // 1-hop descendants (via parent_session_id) for the kill-modal preview.
  function descendantsOf(session) {
    const childrenOf = new Map();
    for (const x of allSessions) {
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

  const panel = new SessionPanel({
    container: panelEl,
    getDescendants: descendantsOf,
    onLabelSaved: (sessionId, customLabel) => {
      const canonical = allSessions.find(x => x.session_id === sessionId);
      if (canonical) canonical.custom_label = customLabel;
      nodeLayer.selectAll('.node')
        .filter(d => d.session_id === sessionId)
        .each(function(d) { d.custom_label = customLabel; })
        .select('text.node-label')
        .each(renderNodeLabel);
    },
    onSummaryFetched: (sessionId, shortLabel) => {
      const s = allSessions.find(x => x.session_id === sessionId);
      if (s) s.short_label = shortLabel;
      nodeLayer.selectAll('.node')
        .filter(d => d.session_id === sessionId)
        .each(function(d) { d.short_label = shortLabel; })
        .select('text.node-label')
        .each(renderNodeLabel);
    },
  });

  function closePanel() {
    selectedSessionId = null;
    panel.close();
    panelEl.innerHTML = '<div class="panel-empty" id="panel-empty">Click a node to inspect its transcript.</div>';
    applySelectionStyles();
  }

  function openPanel(sessionId) {
    const s = allSessions.find(x => x.session_id === sessionId);
    if (!s) return;
    selectedSessionId = sessionId;
    applySelectionStyles();
    panel.open(s);
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
  // D3 force-directed graph (mirrors /crm/graph patterns).
  // -------------------------------------------------------------------
  const svg = d3.select('#graph-svg');
  const VIEW_W = 1600;
  const VIEW_H = 1100;
  svg.attr('viewBox', `0 0 ${VIEW_W} ${VIEW_H}`);
  svg.attr('data-zoom-k', '1');
  svg.style('overflow', 'visible');
  const viewport = svg.append('g').attr('class', 'viewport');
  const columnLayer = viewport.append('g').attr('class', 'host-columns');
  const linkLayer = viewport.append('g').attr('class', 'links');
  const nodeLayer = viewport.append('g').attr('class', 'nodes');

  const zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on('zoom', (event) => {
      viewport.attr('transform', event.transform);
      svg.attr('data-zoom-k', event.transform.k);
    });
  svg.call(zoom);
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
  let _lastSimKey = '';
  let _simStopTimer = null;

  // Host columns — the x-position signal is which host a session
  // runs on, not recency (recency stays a filter — see `applyFilters`
  // below). Recomputed every render from the currently-visible set so an
  // idle host's column disappears once nothing on it is shown.
  let columnHosts = [];
  let columnCenters = new Map();

  function hostOf(s) {
    return s.host || apiHost || 'unknown';
  }

  const LANE_ORDER = LANES.map(l => l.id);
  function laneIndex(d) {
    const i = LANE_ORDER.indexOf(d.lane);
    return i >= 0 ? i : LANE_ORDER.length;
  }

  const COLUMN_HEADER_H = 60;
  function laneTargetY(d) {
    const bands = LANE_ORDER.length + 1;
    const usable = VIEW_H - COLUMN_HEADER_H - 20;
    const bandH = usable / bands;
    return COLUMN_HEADER_H + laneIndex(d) * bandH + bandH / 2;
  }

  function columnTargetX(d) {
    const c = columnCenters.get(hostOf(d));
    return c == null ? VIEW_W / 2 : c;
  }

  const simulation = d3.forceSimulation()
    .force('link', d3.forceLink().id(d => d.session_id).distance(80).strength(0.04))
    .force('charge', d3.forceManyBody().strength(-220).distanceMax(600))
    .force('col-x', d3.forceX(columnTargetX).strength(0.22))
    .force('lane-y', d3.forceY(laneTargetY).strength(0.16))
    .force('collide', d3.forceCollide().radius(d => collideRadius(d)).strength(0.9))
    .alphaDecay(0.025)
    .alphaMin(0.001)
    .velocityDecay(0.45);

  simulation.on('tick', () => {
    nodeLayer.selectAll('.node').attr('transform', d => `translate(${d.x},${d.y})`);
    linkLayer.selectAll('.link').attr('d', d => {
      const sx = d.source.x, sy = d.source.y;
      const tx = d.target.x, ty = d.target.y;
      const dx = tx - sx, dy = ty - sy;
      const dr = Math.sqrt(dx * dx + dy * dy) * 1.6 || 1;
      return `M${sx},${sy}A${dr},${dr} 0 0,1 ${tx},${ty}`;
    });
  });

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
    if (current && (current === 'all' || hosts.includes(current))) {
      filterHostEl.value = current;
    }
    const wrap = filterHostEl.closest('label');
    if (wrap) wrap.style.display = hosts.length > 1 ? '' : 'none';
  }

  function applyFilters(sessions) {
    const showTerm = filterTerminalEl.checked;
    const route = filterRouteEl.value;
    const status = filterStatusEl.value;
    const recencyRaw = filterRecencyEl ? filterRecencyEl.value : 'all';
    const recencySec = (recencyRaw === 'all') ? null : Number(recencyRaw);
    const cwdSel = filterCwdEl ? filterCwdEl.value : 'all';
    const hostSel = filterHostEl ? filterHostEl.value : 'all';
    const nowSec = Date.now() / 1000;
    return sessions.filter(s => {
      if (!showTerm && TERMINAL.has(s.status)) return false;
      if (recencySec !== null && s.last_activity_at
          && (nowSec - s.last_activity_at) > recencySec) {
        return false;
      }
      if (cwdSel !== 'all' && s.decoded_cwd !== cwdSel) return false;
      if (hostSel !== 'all' && s.host !== hostSel) return false;
      if (route !== 'all') {
        const r = s.routing || 'local';
        if (r !== route) return false;
      }
      if (status !== 'all' && s.status !== status) return false;
      return true;
    });
  }

  // Subagent trees: a session with `parent_session_id` set is
  // dropped from the visible set unless its parent is in
  // `expandedParents` — collapsing it into a count badge on the parent
  // instead. A child whose parent isn't itself in the filtered set (e.g.
  // the parent was filtered out) is shown directly — there's nothing to
  // collapse it into.
  function applyCollapse(filtered) {
    const ids = new Set(filtered.map(s => s.session_id));
    return filtered.filter(s => {
      if (!s.parent_session_id) return true;
      if (!ids.has(s.parent_session_id)) return true;
      return expandedParents.has(s.parent_session_id);
    });
  }

  // Direct-child counts per parent (in the filtered set, before collapse) —
  // used both for the badge's hidden-count text and to decide whether the
  // badge should render at all. A parent with children keeps its badge
  // visible even fully expanded (0 currently hidden): otherwise expanding
  // would remove the only affordance that collapses it back.
  function totalChildCounts(filtered) {
    const ids = new Set(filtered.map(s => s.session_id));
    const counts = new Map();
    for (const s of filtered) {
      if (!s.parent_session_id) continue;
      if (!ids.has(s.parent_session_id)) continue;
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
  // terminal. The live-pulse animation (`.pulsing`, CSS keyframe) is
  // unchanged.
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

  function collideRadius(d) {
    const r = nodeRadius(d);
    const lines = d._labelLines || 1;
    const halfW = Math.max(r, (d._labelW || 0) / 2) + 6;
    const labelBottom = r + LABEL_GAP + (lines - 1) * LABEL_LINE_H + LABEL_LINE_H * 0.5;
    const enclose = Math.hypot(halfW, labelBottom) * 0.85;
    return Math.max(r + 14, enclose);
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
  // operator, an error count, and (on a collapsed parent) the hidden-
  // descendant count. All three are offset from the label, positioned at
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
    positionHoverCard(event);
    hoverCardEl.hidden = false;
  }

  function positionHoverCard(event) {
    if (!hoverCardEl) return;
    const pad = 16;
    hoverCardEl.style.left = (event.clientX + pad) + 'px';
    hoverCardEl.style.top = (event.clientY + pad) + 'px';
  }

  function hideHoverCard() {
    if (hoverCardEl) hoverCardEl.hidden = true;
  }

  function toggleParentExpanded(sessionId) {
    if (expandedParents.has(sessionId)) expandedParents.delete(sessionId);
    else expandedParents.add(sessionId);
    renderGraph(allSessions, allEdges);
  }

  function renderGraph(sessions, snapshotEdges) {
    const filtered = applyFilters(sessions);
    const visible = applyCollapse(filtered);
    const totalCounts = totalChildCounts(filtered);
    const visibleIds = new Set(visible.map(s => s.session_id));

    const hosts = [...new Set(visible.map(hostOf))].sort();
    const colWidth = VIEW_W / Math.max(1, hosts.length);
    columnHosts = hosts;
    columnCenters = new Map(hosts.map((h, i) => [h, colWidth * (i + 0.5)]));
    const hostCounts = new Map();
    for (const h of visible.map(hostOf)) hostCounts.set(h, (hostCounts.get(h) || 0) + 1);

    const columnSel = columnLayer.selectAll('text.host-column-label')
      .data(hosts, h => h)
      .join(
        enter => enter.append('text').attr('class', 'host-column-label'),
        update => update,
        exit => exit.remove()
      );
    columnSel
      .attr('x', h => columnCenters.get(h))
      .attr('y', 28)
      .text(h => `${h} · ${hostCounts.get(h)}`);

    const visibleLinks = (snapshotEdges || [])
      .filter(e => visibleIds.has(e.from) && visibleIds.has(e.to))
      .map(e => ({ id: `${e.from}->${e.to}`, source: e.from, target: e.to }));

    linkLayer.selectAll('path.link')
      .data(visibleLinks, d => d.id)
      .join(
        enter => enter.append('path').attr('class', 'link'),
        update => update,
        exit => exit.remove()
      );

    const oldById = new Map();
    nodeLayer.selectAll('.node').each(function(d) { oldById.set(d.session_id, d); });
    const merged = visible.map(s => {
      const prev = oldById.get(s.session_id);
      const row = prev ? Object.assign(prev, s) : Object.assign(
        { x: columnTargetX(s), y: laneTargetY(s) }, s,
      );
      const total = totalCounts.get(s.session_id) || 0;
      row._totalChildren = total;
      row._collapsedChildren = expandedParents.has(s.session_id) ? 0 : total;
      return row;
    });

    const sel = nodeLayer.selectAll('.node')
      .data(merged, d => d.session_id);

    const entered = sel.enter().append('g')
      .attr('class', 'node')
      .style('cursor', 'grab')
      .call(d3.drag()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(0.1).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
        }))
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
        if (!isCli || d.is_subagent || d.parent_session_id) return;
        event.preventDefault();
        event.stopPropagation();
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
    entered.append('text').attr('class', 'node-badge-children')
      .attr('text-anchor', 'middle').attr('font-size', 10).attr('fill', '#e8e8ed')
      .style('cursor', 'pointer')
      .on('click', (event, d) => { event.stopPropagation(); toggleParentExpanded(d.session_id); });

    const all = entered.merge(sel);
    applyShapeAttrs(all.select('.node-shape'));
    applyToolRing(all.select('.node-ring-tools'));
    all.select('text.node-label').each(renderNodeLabel);
    applyBadges(all);

    sel.exit().remove();

    visibleCount = merged.length;
    simulation.nodes(merged);
    simulation.force('link').links(visibleLinks);
    // Restart when either the visible-id set OR any node's size changed —
    // a snapshot tick that only updates `total_active_seconds` must still
    // reheat the layout so a grown node's collide radius is honored.
    const idsKey = visible.map(s => s.session_id).sort().join('|');
    const sizeKey = visible.map(s => `${s.session_id}:${Math.round(nodeRadius(s))}`).sort().join('|');
    const simKey = idsKey + '::' + sizeKey;
    if (simKey !== _lastSimKey) {
      _lastSimKey = simKey;
      simulation.alpha(0.3).restart();
      if (_simStopTimer) clearTimeout(_simStopTimer);
      _simStopTimer = setTimeout(() => simulation.alpha(0).stop(), 8000);
    }

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
    linkLayer.selectAll('path.link').each(function(e) {
      const [s, t] = linkEndpoints(e);
      if (s === sessionId) out.add(t);
      if (t === sessionId) out.add(s);
    });
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
        return s === selectedSessionId || t === selectedSessionId;
      })
      .classed('dimmed', e => {
        if (!hasSelection) return false;
        const [s, t] = linkEndpoints(e);
        return s !== selectedSessionId && t !== selectedSessionId;
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
    renderGraph(allSessions, allEdges);
    if (selectedSessionId) {
      const s = allSessions.find(x => x.session_id === selectedSessionId);
      if (s) panel.updateMeta(s);
    }
  }

  // --- Filters ---
  function releasePins() {
    nodeLayer.selectAll('.node').each(function(d) { d.fx = null; d.fy = null; });
  }
  function onFilterChange() {
    releasePins();
    svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity);
    renderGraph(allSessions, allEdges);
    if (searchQuery.trim()) renderSearchResults();
  }
  filterTerminalEl.addEventListener('change', () => {
    applyRecencyDefault();
    onFilterChange();
  });
  if (filterRecencyEl) {
    filterRecencyEl.addEventListener('change', () => {
      recencyManuallySet = true;
      onFilterChange();
    });
  }
  [filterRouteEl, filterStatusEl, filterCwdEl, filterHostEl].filter(Boolean).forEach(el =>
    el.addEventListener('change', onFilterChange)
  );

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
        filterRecencyEl.value = fit || 'all';
        recencyManuallySet = true;
      }
    }
    if (filterCwdEl && filterCwdEl.value !== 'all' && s.decoded_cwd !== filterCwdEl.value) {
      filterCwdEl.value = 'all';
    }
    if (filterRouteEl.value !== 'all' && (s.routing || 'local') !== filterRouteEl.value) {
      filterRouteEl.value = 'all';
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

  function selectSearchResult(sessionId) {
    const s = allSessions.find(x => x.session_id === sessionId);
    if (!s) return;
    const visibleIds = new Set(applyFilters(allSessions).map(x => x.session_id));
    const wasFilteredOut = !visibleIds.has(sessionId);
    const wasCollapsed = !!(s.parent_session_id && !expandedParents.has(s.parent_session_id));
    const needsRerender = wasFilteredOut || wasCollapsed;
    if (wasFilteredOut) relaxFiltersFor(s);
    if (wasCollapsed) expandAncestorsFor(s);
    if (needsRerender) {
      releasePins();
      renderGraph(allSessions, allEdges);
    }
    hideSearchResults();
    openPanel(sessionId);
    panToNode(sessionId, needsRerender ? 400 : 0);
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
      try { simulation.alpha(0.1).restart(); } catch (_) {}
    });
  })();

  // --- Initial load + stream ---
  fetch('/api/agents/snapshot')
    .then(r => r.json())
    .then(applySnapshot)
    .catch(err => { connStateEl.textContent = 'failed: ' + err; });

  const snapshotES = new EventSource('/api/agents/stream');
  snapshotES.onopen = () => { connStateEl.textContent = 'live'; };
  snapshotES.onerror = () => { connStateEl.textContent = 'reconnecting…'; };
  snapshotES.addEventListener('snapshot', e => {
    try { applySnapshot(JSON.parse(e.data)); } catch (_) {}
  });
}
