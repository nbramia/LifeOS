// web/agents/graph.js
//
// The Graph tab (#850) — the force-directed session graph that used to be
// the whole /agents page. Node rendering, simulation, filters, chips, and
// search are unchanged from the pre-#850 single-file web/agents.html; the
// only structural change is that the side panel's rendering, event feed,
// label edit, and summary fetch now come from the shared
// `SessionPanel` in ./panel.js (also used by the Board tab's drawer)
// instead of being duplicated inline.
//
// `initGraph()` is called once, lazily, the first time the operator opens
// the Graph tab (see web/agents.html) — the graph's own snapshot fetch + SSE
// stream only start then, so loading the board (the primary view) doesn't
// also open a second live connection nobody is looking at.

import {
  STATUS_COLORS, TERMINAL, routingLabel, sourceLabelFor,
  escapeHtml, showToast, SessionPanel,
} from './panel.js';

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
  // Operator chooses recency manually → don't auto-flip on include-finished toggle.
  let recencyManuallySet = false;

  let allSessions = [];
  let allEdges = [];
  let selectedSessionId = null;

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
        .text(d => nodeLabel(d));
    },
    onSummaryFetched: (sessionId, shortLabel) => {
      const s = allSessions.find(x => x.session_id === sessionId);
      if (s) s.short_label = shortLabel;
      nodeLayer.selectAll('.node')
        .filter(d => d.session_id === sessionId)
        .each(function(d) { d.short_label = shortLabel; })
        .select('text.node-label')
        .text(d => nodeLabel(d));
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
  svg.style('overflow', 'visible');
  const viewport = svg.append('g').attr('class', 'viewport');
  const linkLayer = viewport.append('g').attr('class', 'links');
  const nodeLayer = viewport.append('g').attr('class', 'nodes');

  const zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on('zoom', (event) => { viewport.attr('transform', event.transform); });
  svg.call(zoom);
  svg.style('cursor', 'grab');
  svg.on('mousedown.cursor', () => svg.style('cursor', 'grabbing'));
  svg.on('mouseup.cursor',   () => svg.style('cursor', 'grab'));

  svg.on('click', (event) => {
    if (event.target === svg.node() && selectedSessionId) closePanel();
  });

  let visibleCount = 0;
  let _lastVisibleIdsKey = '';
  let _nodeClickTimer = null;
  function recencyRailSpan() {
    if (visibleCount <= 1) return 0;
    return Math.min(0.84, 0.15 + 0.15 * Math.log2(visibleCount));
  }

  function recencyTargetX(s) {
    const span = recencyRailSpan();
    const nowSec = Date.now() / 1000;
    const ageSec = Math.max(0, nowSec - (s.last_activity_at || nowSec));
    const ageNorm = Math.min(ageSec, 86400) / 86400;
    return VIEW_W * (0.5 + span / 2 - ageNorm * span);
  }

  const simulation = d3.forceSimulation()
    .force('link', d3.forceLink().id(d => d.session_id).distance(80).strength(0.04))
    .force('charge', d3.forceManyBody().strength(-220).distanceMax(600))
    .force('center-y', d3.forceY(VIEW_H / 2).strength(0.12))
    .force('recency-x', d3.forceX(recencyTargetX).strength(0.18))
    .force('collide', d3.forceCollide().radius(d => collideRadius(d)).strength(0.9))
    .alphaDecay(0.025)
    .alphaMin(0.001)
    .velocityDecay(0.45);

  setTimeout(() => simulation.alpha(0).stop(), 8000);

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

  function sizeForTokens(totalTokens) {
    const t = Math.max(0, totalTokens || 0);
    const grown = Math.log10(t + 10) * 6;
    return Math.min(56, Math.max(14, 14 + grown));
  }

  function nodeRadius(d) {
    return sizeForTokens(
      (d.total_input_tokens || 0)
      + (d.total_output_tokens || 0)
      + (d.total_cache_creation_tokens || 0)
      + (d.total_cache_read_tokens || 0)
    );
  }

  function nodeColors(d) {
    const c = STATUS_COLORS[d.status] || '#6b7280';
    const isTerm = TERMINAL.has(d.status);
    return {
      fill: isTerm ? hexWithAlpha(c, 0.35) : hexWithAlpha(c, 0.85),
      stroke: c,
      borderWidth: d.status === 'blocked' ? 4 : 2,
    };
  }

  // (#863) Shared by `nodeLabel` and `sessionDisplayName` below (#863 review
  // round 3 finding V) — a raw identifier (the session id itself, that id
  // with a known CLI prefix ("cc:"/"cx:") stripped, or the row's task_id)
  // is never a real label, wherever it might render: the graph node, or
  // the search-results dropdown. Compared by equality (after trimming),
  // not by `startsWith`: a `startsWith` check never matches a prefixed
  // session id ("cc:<uuid>".startsWith("<uuid>") is false), which let raw
  // ids through as labels.
  function isRawIdValue(d, value) {
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

  function nodeLabel(d) {
    // (#863) Precedence, first non-empty wins. `label` and `short_label` are
    // each skipped when they're not a human label but the raw identifier
    // the row fell back to (`isRawIdValue` above) — since both ingests and
    // `_label_for_session` fall back to exactly that raw id when there's no
    // real title, and `_fallback_label` (agent_viz_summary.py) can cache
    // that same raw id as `short_label` (#863 review finding M —
    // `short_label` sits ABOVE `label` in this precedence list, so it needs
    // the identical guard or the raw id leaks through one slot higher).
    // Never emits '?'.
    const candidates = [
      d.custom_label,
      isRawIdValue(d, d.short_label) ? '' : d.short_label,
      isRawIdValue(d, d.label) ? '' : d.label,
      d.prompt_preview,
      d.model_label,
      routingLabel(d.routing),
    ];
    for (const c of candidates) {
      const trimmed = (c || '').toString().trim();
      if (trimmed) return trimmed;
    }
    return d.session_id.slice(0, 8);
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

  function nodeTitle(d) {
    return [
      d.label,
      d.decoded_cwd ? `cwd: ${d.decoded_cwd}` : null,
      `source: ${sourceLabelFor(d)}`,
      `status: ${d.status}`,
      `routing: ${routingLabel(d.routing)}`,
      `tokens: ${d.total_input_tokens} in / ${d.total_output_tokens} out`,
      `cost: $${(d.total_dollars || 0).toFixed(4)}`,
      d.last_event_kind ? `last: ${d.last_event_kind}` : null,
    ].filter(Boolean).join('\n');
  }

  function isActivelyWriting(d) {
    const nowSec = Date.now() / 1000;
    return d.status === 'running'
      && d.last_activity_at
      && (nowSec - d.last_activity_at) < 60;
  }

  function nodeShapeTag(d) {
    if (d.source === 'claude_code' || d.source === 'codex'
        || d.routing === 'claude_code' || d.routing === 'codex') return 'rect';
    if (!d.routing || d.routing === 'local') return 'polygon';
    return 'circle';
  }

  function applyShapeAttrs(sel) {
    sel.each(function(d) {
      const el = d3.select(this);
      const r = nodeRadius(d);
      const colors = nodeColors(d);
      el.attr('fill', colors.fill).attr('stroke', colors.stroke)
        .attr('stroke-width', colors.borderWidth)
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
        const h = r * 1.4;
        el.attr('points', `0,${-h} ${h},0 0,${h} ${-h},0`);
      }
    });
  }

  function renderGraph(sessions, snapshotEdges) {
    const visible = applyFilters(sessions);
    const visibleIds = new Set(visible.map(s => s.session_id));

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
      if (prev) {
        Object.assign(prev, s);
        return prev;
      }
      return Object.assign({ x: recencyTargetX(s), y: VIEW_H / 2 }, s);
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
        if (_nodeClickTimer) clearTimeout(_nodeClickTimer);
        _nodeClickTimer = setTimeout(() => {
          _nodeClickTimer = null;
          if (d.session_id === selectedSessionId) closePanel();
          else openPanel(d.session_id);
        }, 220);
      })
      .on('dblclick', (event, d) => {
        const isCli = d.source === 'claude_code' || d.source === 'codex';
        if (!isCli || d.is_subagent) return;
        event.preventDefault();
        event.stopPropagation();
        if (_nodeClickTimer) { clearTimeout(_nodeClickTimer); _nodeClickTimer = null; }
        focusSessionQuick(d);
      });
    entered.append(d => document.createElementNS('http://www.w3.org/2000/svg', nodeShapeTag(d)))
      .attr('class', 'node-shape');
    entered.append('title');
    entered.append('text').attr('class', 'node-label');

    const all = entered.merge(sel);
    applyShapeAttrs(all.select('.node-shape'));
    all.select('text.node-label').each(renderNodeLabel);
    all.select('title').text(d => nodeTitle(d));

    sel.exit().remove();

    visibleCount = merged.length;
    simulation.nodes(merged);
    simulation.force('link').links(visibleLinks);
    const visibleIdsKey = visible.map(s => s.session_id).sort().join('|');
    if (visibleIdsKey !== _lastVisibleIdsKey) {
      _lastVisibleIdsKey = visibleIdsKey;
      simulation.alpha(0.3).restart();
    }

    emptyStateEl.style.display = visible.length === 0 ? '' : 'none';
    updateChips(visible);
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

  const SEARCH_TIER = { label: 0, short_label: 1, summary: 2 };
  const SEARCH_BADGE = { label: 'name', short_label: 'label', summary: 'summary' };

  function sessionDisplayName(s) {
    // (#863 review round 3 finding V) Same raw-id guard `nodeLabel` uses —
    // this feeds the search-results sort order and (via `renderSearchResults`
    // below) the dropdown title whenever the matched field isn't rendered
    // from its own raw value. Not independently browser-testable through
    // `#search-input` today: `buildSearchResults`'s 'label' tier only
    // matches when `custom_label || label` is truthy, and
    // `renderSearchResults` then renders that same truthy `label` directly
    // (`r.session.label || sessionDisplayName(r.session)`) rather than
    // through this function — so a search that matches on a raw-id
    // `label` never reaches this guard in the first place (a pre-existing
    // gap in `renderSearchResults` itself, out of scope for this fix; see
    // PR #890 round-3 report). This function's guard covers the paths that
    // DO route through it: the dropdown's sort comparator, and any title
    // that falls through because the matched field's own value is empty.
    const shortLabel = isRawIdValue(s, s.short_label) ? '' : s.short_label;
    const label = isRawIdValue(s, s.label) ? '' : s.label;
    return s.custom_label || shortLabel || label || s.session_id.slice(0, 8);
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
        const titleText = r.field === 'label' ? (r.session.label || sessionDisplayName(r.session))
          : r.field === 'short_label' ? (r.session.short_label || sessionDisplayName(r.session))
          : sessionDisplayName(r.session);
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
    const wasHidden = !visibleIds.has(sessionId);
    if (wasHidden) {
      relaxFiltersFor(s);
      releasePins();
      renderGraph(allSessions, allEdges);
    }
    hideSearchResults();
    openPanel(sessionId);
    panToNode(sessionId, wasHidden ? 400 : 0);
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
