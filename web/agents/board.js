// web/agents/board.js
//
// The Kanban board (#850) — the primary /agents view. Backed by the vault
// task store via GET/PUT /api/agents/board*, with a card drawer that reuses
// the shared SessionPanel (./panel.js) for the linked session's transcript,
// exactly like the Graph tab's side panel does.
//
// No card reordering within a lane (file order is lane order, per the
// issue) — drag only ever changes which lane a card is in.

import {
  TERMINAL, routingLabel, sourceLabelFor, escapeHtml, showToast, SessionPanel,
} from './panel.js';

const LANES = [
  { id: 'unassigned',  label: 'Unassigned' },
  { id: 'assigned',    label: 'Assigned' },
  { id: 'in_progress', label: 'In progress' },
  { id: 'human_queue', label: 'Human queue' },
  { id: 'scheduled',   label: 'Scheduled' },
  { id: 'review',      label: 'Review' },
  { id: 'done',        label: 'Done' },
];

const ASSIGNEES = ['me', 'claude', 'codex', 'hermes', 'local'];

// Card fields the drawer renders as editable inputs — used to decide
// whether an SSE tick needs to rebuild the drawer at all (#850 finding 2).
const DRAWER_EDITABLE_FIELDS = [
  'title', 'notes', 'tags', 'context', 'assignee', 'lane',
  // Scheduled-card fields, editable in the drawer since #850 finding 4.
  'name', 'message_content', 'enabled',
];

export function initBoard() {
  const lanesEl = document.getElementById('board-lanes');
  const searchEl = document.getElementById('board-search');
  const laneFilterEl = document.getElementById('board-filter-lane');
  const assigneeFilterEl = document.getElementById('board-filter-assignee');
  const hostFilterEl = document.getElementById('board-filter-host');
  const tagFilterEl = document.getElementById('board-filter-tag');
  const contextFilterEl = document.getElementById('board-filter-context');
  const recencyFilterEl = document.getElementById('board-filter-recency');
  const includeDoneEl = document.getElementById('board-filter-done');
  const newCardBtn = document.getElementById('board-new-card');
  const connStateEl = document.getElementById('board-connection-state');
  const drawerBackdrop = document.getElementById('board-drawer-backdrop');
  const drawerEl = document.getElementById('board-drawer');

  let board = { lanes: Object.fromEntries(LANES.map(l => [l.id, []])) };
  let openCardId = null;
  let openCardLane = null;
  let openCardSnapshot = null;  // last card object the drawer was fully rendered from
  let panel = null;  // SessionPanel for the drawer's linked-session transcript

  // ------------------------------------------------------------------
  // Data load + live updates
  // ------------------------------------------------------------------

  function allCards() {
    const out = [];
    for (const lane of LANES) {
      for (const card of (board.lanes[lane.id] || [])) out.push({ ...card, lane: lane.id });
    }
    return out;
  }

  function findCard(id) {
    return allCards().find(c => c.id === id) || null;
  }

  function applyBoard(next) {
    board = next;
    updateFilterOptions();
    render();
    if (openCardId) {
      const fresh = findCard(openCardId);
      if (!fresh) { closeDrawer(); return; }
      updateOpenDrawer(fresh);
    }
  }

  // A board tick (SSE, ~every 0.75s) reaches here even when nothing about
  // the open card changed. Rebuilding the drawer via innerHTML every time
  // drops unsaved edits mid-keystroke, re-opens the linked session's
  // transcript EventSource, and re-fires GET /sessions/{id}/summary (an LLM
  // call) on every tick (#850 finding 2). So: refresh the linked session in
  // place via panel.updateMeta when its id hasn't changed, and only rebuild
  // the editable field block when a field actually changed and the operator
  // isn't mid-edit in the drawer.
  function updateOpenDrawer(fresh) {
    const prev = openCardSnapshot;
    const prevSessionId = (prev && prev.session && prev.session.session_id) || null;
    const freshSessionId = (fresh.session && fresh.session.session_id) || null;
    const sessionUnchanged = prevSessionId === freshSessionId;

    if (sessionUnchanged && panel && freshSessionId) {
      panel.updateMeta(fresh.session);
    }

    // Beyond the editable fields, also watch pending_question and the
    // linked session's status — neither drives an input, but both drive
    // which action buttons the drawer shows (Answer, Kill). Without this,
    // answering from the drawer or a session reaching a terminal state
    // leaves a stale button behind: a second "Answer" click 404s, and
    // "Kill" survives a session that already exited (#850 round-2 finding 3).
    const prevPendingId = (prev && prev.pending_question && prev.pending_question.id) ?? null;
    const freshPendingId = (fresh.pending_question && fresh.pending_question.id) ?? null;
    const prevSessionStatus = (prev && prev.session && prev.session.status) ?? null;
    const freshSessionStatus = (fresh.session && fresh.session.status) ?? null;

    const fieldsChanged = !prev || DRAWER_EDITABLE_FIELDS.some(
      f => JSON.stringify(prev[f]) !== JSON.stringify(fresh[f])
    ) || prevPendingId !== freshPendingId || prevSessionStatus !== freshSessionStatus;
    const focused = !!(drawerEl && drawerEl.contains(document.activeElement));
    if ((fieldsChanged || !sessionUnchanged) && !focused) {
      renderDrawer(fresh);
      // Only advance the snapshot on the branch that actually rendered —
      // otherwise a frame skipped because the drawer had focus is treated
      // as "no change" forever, and a later change gets silently dropped
      // too because it's diffed against this stale snapshot instead of the
      // last card the drawer actually shows (#850 round-2 finding 4).
      openCardSnapshot = fresh;
    }
  }

  function fetchBoard() {
    return fetch('/api/agents/board')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(applyBoard)
      .catch(err => { if (connStateEl) connStateEl.textContent = 'failed: ' + err; });
  }

  function connectStream() {
    const es = new EventSource('/api/agents/board/stream');
    es.onopen = () => { if (connStateEl) connStateEl.textContent = 'live'; };
    es.onerror = () => { if (connStateEl) connStateEl.textContent = 'reconnecting…'; };
    es.addEventListener('board', e => {
      try { applyBoard(JSON.parse(e.data)); } catch (_) {}
    });
    return es;
  }

  // ------------------------------------------------------------------
  // Filters
  // ------------------------------------------------------------------

  let _lastHostKey = '';
  let _lastContextKey = '';
  function updateFilterOptions() {
    const hosts = [...new Set(
      allCards().map(c => c.session && c.session.host).filter(Boolean)
    )].sort();
    const hostKey = hosts.join('|');
    if (hostFilterEl && hostKey !== _lastHostKey) {
      _lastHostKey = hostKey;
      const current = hostFilterEl.value;
      hostFilterEl.innerHTML = '<option value="all">all hosts</option>'
        + hosts.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join('');
      if (current && (current === 'all' || hosts.includes(current))) hostFilterEl.value = current;
    }

    const contexts = [...new Set(
      allCards().filter(c => c.kind === 'task').map(c => c.context).filter(Boolean)
    )].sort();
    const contextKey = contexts.join('|');
    if (contextFilterEl && contextKey !== _lastContextKey) {
      _lastContextKey = contextKey;
      const current = contextFilterEl.value;
      contextFilterEl.innerHTML = '<option value="all">all contexts</option>'
        + contexts.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
      if (current && (current === 'all' || contexts.includes(current))) contextFilterEl.value = current;
    }
  }

  function cardMatchesFilters(card) {
    const search = (searchEl?.value || '').trim().toLowerCase();
    if (search) {
      const haystack = card.kind === 'schedule'
        ? (card.name || '')
        : `${card.title || ''} ${card.notes || ''}`;
      if (!haystack.toLowerCase().includes(search)) return false;
    }

    const laneSel = laneFilterEl?.value || 'all';
    if (laneSel !== 'all' && card.lane !== laneSel) return false;

    const assigneeSel = assigneeFilterEl?.value || 'all';
    if (assigneeSel !== 'all') {
      if (card.kind !== 'task') return false;
      if (assigneeSel === 'unassigned') {
        if (card.assignee) return false;
      } else if (card.assignee !== assigneeSel) {
        return false;
      }
    }

    const hostSel = hostFilterEl?.value || 'all';
    if (hostSel !== 'all') {
      const host = card.session && card.session.host;
      if (host !== hostSel) return false;
    }

    const tagQuery = (tagFilterEl?.value || '').trim().toLowerCase().replace(/^#/, '');
    if (tagQuery) {
      if (card.kind !== 'task') return false;
      if (!(card.tags || []).some(t => t.toLowerCase().includes(tagQuery))) return false;
    }

    const contextSel = contextFilterEl?.value || 'all';
    if (contextSel !== 'all') {
      if (card.kind !== 'task' || card.context !== contextSel) return false;
    }

    const recencyRaw = recencyFilterEl?.value || 'all';
    if (recencyRaw !== 'all') {
      const recencySec = Number(recencyRaw);
      const stamp = card.kind === 'schedule' ? card.next_fire_at : card.updated_at;
      if (stamp) {
        const ageSec = (Date.now() - new Date(stamp).getTime()) / 1000;
        if (ageSec > recencySec) return false;
      }
    }

    // Only cancelled task cards are behind this filter — the Done lane
    // itself (finished tasks, retired/fired schedules) always stays visible
    // (#850 finding 3).
    if (!includeDoneEl?.checked && card.kind === 'task' && card.status === 'cancelled') return false;

    return true;
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  function cardChips(card) {
    const chips = [];
    if (card.assignee) chips.push(`<span class="board-chip board-chip-assignee">${escapeHtml(card.assignee)}</span>`);
    if (card.fields && card.fields.model) chips.push(`<span class="board-chip">${escapeHtml(card.fields.model)}</span>`);
    if (card.fields && card.fields.effort) chips.push(`<span class="board-chip">${escapeHtml(card.fields.effort)}</span>`);
    if (card.session && card.session.host) chips.push(`<span class="board-chip board-chip-host">${escapeHtml(card.session.host)}</span>`);
    for (const t of (card.tags || [])) {
      if (ASSIGNEES.includes(t.toLowerCase())) continue;  // already shown as the assignee chip
      chips.push(`<span class="board-chip board-chip-tag">#${escapeHtml(t)}</span>`);
    }
    return chips.join('');
  }

  function renderTaskCard(card) {
    const live = !!(card.session && !TERMINAL.has(card.session.status));
    const div = document.createElement('div');
    div.className = 'board-card';
    div.dataset.cardId = card.id;
    div.dataset.lane = card.lane;
    div.innerHTML = `
      <div class="board-card-title">${live ? '<span class="live-dot" title="live"></span>' : ''}${escapeHtml(card.title || '(untitled)')}</div>
      ${card.pending_question ? `<div class="board-card-question">❓ ${escapeHtml(card.pending_question.question)}</div>` : ''}
      <div class="board-card-chips">${cardChips(card)}</div>
    `;
    div.addEventListener('click', () => {
      if (suppressNextClick === card.id) { suppressNextClick = null; return; }
      openDrawer(card.id);
    });
    div.addEventListener('mousedown', (e) => onCardMouseDown(e, card));
    return div;
  }

  function renderScheduleCard(card) {
    const div = document.createElement('div');
    div.className = 'board-card board-card-schedule';
    div.dataset.cardId = card.id;
    div.dataset.lane = card.lane;
    const nextFire = card.next_fire_at ? new Date(card.next_fire_at).toLocaleString() : '—';
    div.innerHTML = `
      <div class="board-card-title">${escapeHtml(card.name || '(schedule)')}</div>
      <div class="board-card-chips">
        ${card.recurring ? '<span class="board-chip">recurring</span>' : '<span class="board-chip">one-off</span>'}
        <span class="board-chip">next: ${escapeHtml(nextFire)}</span>
      </div>
      ${card.last_run ? `<div class="board-card-lastrun">${escapeHtml(card.last_run.outcome || '')} · ${escapeHtml(card.last_run.snippet || '')}</div>` : ''}
    `;
    // Scheduled cards open the drawer (name/message/enabled are editable
    // there — #850 finding 4) but never drag between lanes: their lane is
    // derived from the scheduler entry's own enabled/next-fire state, not
    // settable by dropping a card.
    div.addEventListener('click', () => openDrawer(card.id));
    return div;
  }

  function render() {
    lanesEl.innerHTML = '';
    for (const lane of LANES) {
      const column = document.createElement('div');
      column.className = 'board-lane';
      column.dataset.lane = lane.id;

      const cards = (board.lanes[lane.id] || [])
        .map(c => ({ ...c, lane: lane.id }))
        .filter(cardMatchesFilters);

      column.innerHTML = `<div class="board-lane-header">${escapeHtml(lane.label)} <span class="board-lane-count">${cards.length}</span></div>`;
      const cardsEl = document.createElement('div');
      cardsEl.className = 'board-lane-cards';
      for (const card of cards) {
        cardsEl.appendChild(card.kind === 'schedule' ? renderScheduleCard(card) : renderTaskCard(card));
      }
      column.appendChild(cardsEl);
      lanesEl.appendChild(column);
    }
  }

  // ------------------------------------------------------------------
  // Drag and drop — pointer-based (mousedown/mousemove/mouseup), not the
  // native HTML5 Drag and Drop API. draggable="true" + dragstart/drop only
  // fires through the browser's OS-level drag gesture, which synthetic
  // mouse events (Playwright included) can't reliably trigger — a plain
  // pointer drag works the same in real use and is what the server-free
  // browser test drives.
  // ------------------------------------------------------------------

  let dragState = null;   // { cardId, sourceLane, cardEl, ghost, startX, startY, moved }
  let suppressNextClick = null;  // card id whose trailing click (after a real drag) should be swallowed

  function onCardMouseDown(e, card) {
    if (e.button !== 0) return;
    dragState = {
      cardId: card.id, sourceLane: card.lane, cardEl: e.currentTarget,
      startX: e.clientX, startY: e.clientY, moved: false, ghost: null,
    };
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragUp);
  }

  function onDragMove(e) {
    if (!dragState) return;
    const dx = e.clientX - dragState.startX;
    const dy = e.clientY - dragState.startY;
    if (!dragState.moved && Math.hypot(dx, dy) < 4) return;
    if (!dragState.moved) {
      dragState.moved = true;
      dragState.cardEl.classList.add('dragging-source');
      const rect = dragState.cardEl.getBoundingClientRect();
      const ghost = dragState.cardEl.cloneNode(true);
      ghost.classList.add('board-card-ghost');
      ghost.style.position = 'fixed';
      ghost.style.pointerEvents = 'none';
      ghost.style.width = rect.width + 'px';
      ghost.style.zIndex = '200';
      document.body.appendChild(ghost);
      dragState.ghost = ghost;
    }
    dragState.ghost.style.left = (e.clientX + 12) + 'px';
    dragState.ghost.style.top = (e.clientY + 12) + 'px';
    document.querySelectorAll('.board-lane.drag-over').forEach(el => el.classList.remove('drag-over'));
    const laneEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('.board-lane');
    if (laneEl) laneEl.classList.add('drag-over');
  }

  function onDragUp(e) {
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragUp);
    if (!dragState) return;
    const { cardId, sourceLane, moved, ghost, cardEl } = dragState;
    document.querySelectorAll('.board-lane.drag-over').forEach(el => el.classList.remove('drag-over'));
    if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
    if (cardEl) cardEl.classList.remove('dragging-source');
    if (moved) {
      const laneEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('.board-lane');
      const targetLane = laneEl && laneEl.dataset.lane;
      if (targetLane && targetLane !== sourceLane) {
        suppressNextClick = cardId;  // the mouseup will also fire a click — swallow it
        onCardDropped(cardId, targetLane);
      }
    }
    dragState = null;
  }

  function onCardDropped(cardId, targetLane) {
    const card = findCard(cardId);
    if (!card || card.kind !== 'task') return;
    let assignee;
    if (targetLane === 'assigned') {
      // No mid-drag assignee picker with plain HTML5 DnD — default to "me"
      // (the common case: an operator claiming a card for themself) unless
      // the card already has one, which the lane endpoint keeps as-is only
      // when we pass it through explicitly.
      assignee = card.assignee || 'me';
    }
    // moveCard already toasts and re-renders on failure — nothing more to
    // do here, just avoid an unhandled rejection now that it re-throws.
    moveCard(cardId, targetLane, assignee).catch(() => {});
  }

  function laneLabel(laneId) {
    const lane = LANES.find(l => l.id === laneId);
    return lane ? lane.label : laneId;
  }

  function moveCard(cardId, targetLane, assignee) {
    const body = { lane: targetLane };
    if (assignee) body.assignee = assignee;
    return fetch(`/api/agents/board/cards/${encodeURIComponent(cardId)}/lane`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(async (r) => {
        if (!r.ok) {
          const text = await r.text();
          let msg = text;
          try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
          throw new Error(msg || `HTTP ${r.status}`);
        }
        return r.json();
      })
      .then((data) => {
        // The server-landed lane can differ from what was requested for the
        // tags-only assigned/unassigned moves (e.g. a Human-queue card
        // assigned to someone stays in Human queue) — surface that instead
        // of leaving the operator to notice the card "snapped back" on its
        // own (#850 round-2 finding 2b).
        if (data && data.lane && data.lane !== targetLane) {
          showToast(`Card landed in ${laneLabel(data.lane)}, not ${laneLabel(targetLane)}.`, false);
        }
        return fetchBoard();
      })
      .catch(err => {
        showToast(`Couldn't move card: ${err.message}`, true);
        // Nothing was mutated client-side before the request resolved, so
        // the card is already still in its original lane — just re-render
        // in case a stray class (drag-over) was left behind.
        render();
        // Re-throw so a caller mid-edit (e.g. the drawer's assignee select)
        // can revert its own unsaved UI state instead of leaving a value
        // that was never actually persisted (#850 finding 9).
        throw err;
      });
  }

  // ------------------------------------------------------------------
  // New-card composer
  // ------------------------------------------------------------------

  function openNewCardForm() {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="new-card-title">
        <h2 id="new-card-title">New card</h2>
        <label style="font-size:0.75rem;color:var(--text-secondary)">Title</label>
        <input id="new-card-desc" type="text" style="width:100%;box-sizing:border-box;margin:0.35rem 0;padding:0.4rem;background:var(--bg-elev);color:var(--text-primary);border:1px solid var(--border);border-radius:6px" />
        <label style="font-size:0.75rem;color:var(--text-secondary)">Notes (optional)</label>
        <textarea id="new-card-notes" placeholder="Notes…"></textarea>
        <label style="font-size:0.75rem;color:var(--text-secondary)">Assignee</label>
        <select id="new-card-assignee" style="width:100%;margin:0.35rem 0;padding:0.4rem;background:var(--bg-elev);color:var(--text-primary);border:1px solid var(--border);border-radius:6px">
          <option value="">unassigned</option>
          ${ASSIGNEES.map(a => `<option value="${a}">${a}</option>`).join('')}
        </select>
        <div class="actions">
          <button id="new-card-cancel">Cancel</button>
          <button class="danger" id="new-card-create">Create</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
    backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
    backdrop.querySelector('#new-card-cancel').onclick = cleanup;
    backdrop.querySelector('#new-card-create').onclick = async () => {
      const desc = backdrop.querySelector('#new-card-desc').value.trim();
      if (!desc) return;
      const notes = backdrop.querySelector('#new-card-notes').value.trim();
      const assignee = backdrop.querySelector('#new-card-assignee').value;
      const btn = backdrop.querySelector('#new-card-create');
      btn.disabled = true;
      btn.textContent = 'Creating…';
      try {
        const r = await fetch('/api/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            description: desc,
            notes: notes || undefined,
            tags: assignee ? [assignee] : undefined,
          }),
        });
        if (!r.ok) {
          const text = await r.text();
          throw new Error(text);
        }
        cleanup();
        fetchBoard();
      } catch (err) {
        showToast(`Couldn't create card: ${err.message}`, true);
        btn.disabled = false;
        btn.textContent = 'Create';
      }
    };
  }

  if (newCardBtn) newCardBtn.addEventListener('click', openNewCardForm);

  // ------------------------------------------------------------------
  // Drawer
  // ------------------------------------------------------------------

  function closeDrawer() {
    openCardId = null;
    openCardLane = null;
    openCardSnapshot = null;
    if (panel) { panel.close(); panel = null; }
    if (drawerBackdrop) drawerBackdrop.hidden = true;
    if (drawerEl) drawerEl.innerHTML = '';
  }

  function openDrawer(cardId) {
    const card = findCard(cardId);
    if (!card) return;
    openCardId = cardId;
    openCardLane = card.lane;
    openCardSnapshot = card;
    if (drawerBackdrop) drawerBackdrop.hidden = false;
    renderDrawer(card);
  }

  async function putTask(taskId, patch) {
    const r = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return r.json();
  }

  async function putSchedule(scheduleId, patch) {
    const r = await fetch(`/api/scheduler/${encodeURIComponent(scheduleId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return r.json();
  }

  function renderDrawer(card) {
    if (!drawerEl) return;
    const isTask = card.kind === 'task';
    const nonAssigneeTags = (card.tags || []).filter(t => !ASSIGNEES.includes(t.toLowerCase()));
    const titleValue = isTask ? (card.title || '') : (card.name || '');
    drawerEl.innerHTML = `
      <div class="drawer-header">
        <button class="panel-close" data-action="drawer-close">×</button>
        <input class="drawer-title" data-field="title" value="${escapeHtml(titleValue)}" />
      </div>
      ${isTask ? `
      <label class="drawer-label">Notes</label>
      <textarea class="drawer-notes" data-field="notes" placeholder="Notes…">${escapeHtml(card.notes || '')}</textarea>
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Assignee</label>
          <select class="drawer-assignee" data-field="assignee">
            <option value="">unassigned</option>
            ${ASSIGNEES.map(a => `<option value="${a}" ${card.assignee === a ? 'selected' : ''}>${a}</option>`).join('')}
          </select>
        </div>
        <div>
          <label class="drawer-label">Context</label>
          <input class="drawer-context" data-field="context" value="${escapeHtml(card.context || '')}" />
        </div>
      </div>
      <label class="drawer-label">Tags</label>
      <input class="drawer-tags" data-field="tags" value="${escapeHtml(nonAssigneeTags.join(' '))}" placeholder="space-separated tags" />
      <div class="drawer-actions" data-field="actions"></div>
      <div class="drawer-session" data-field="session-panel"></div>
      ` : `
      <label class="drawer-label">Message</label>
      <textarea class="drawer-notes" data-field="message-content" placeholder="Message…">${escapeHtml(card.message_content || '')}</textarea>
      <label class="drawer-label"><input type="checkbox" data-field="enabled" ${card.enabled ? 'checked' : ''} /> Enabled</label>
      <div class="drawer-schedule-hint">Name, message, and enabled save here through the scheduler API — for schedule type, timing, or executor, use the existing scheduler UI.</div>
      `}
    `;
    drawerEl.querySelector('[data-action="drawer-close"]').onclick = closeDrawer;

    const titleEl = drawerEl.querySelector('[data-field="title"]');
    titleEl.addEventListener('blur', async () => {
      const value = titleEl.value.trim();
      if (!value || value === titleValue) return;
      try {
        if (isTask) await putTask(card.id, { description: value });
        else await putSchedule(card.id, { name: value });
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't save title: ${err.message}`, true);
        titleEl.value = titleValue;
      }
    });

    if (!isTask) {
      const msgEl = drawerEl.querySelector('[data-field="message-content"]');
      msgEl.addEventListener('blur', async () => {
        const value = msgEl.value;
        if (value === (card.message_content || '')) return;
        try { await putSchedule(card.id, { message_content: value }); await fetchBoard(); }
        catch (err) { showToast(`Couldn't save message: ${err.message}`, true); msgEl.value = card.message_content || ''; }
      });
      const enabledEl = drawerEl.querySelector('[data-field="enabled"]');
      enabledEl.addEventListener('change', async () => {
        try { await putSchedule(card.id, { enabled: enabledEl.checked }); await fetchBoard(); }
        catch (err) { showToast(`Couldn't update enabled: ${err.message}`, true); enabledEl.checked = !!card.enabled; }
      });
      return;
    }

    const notesEl = drawerEl.querySelector('[data-field="notes"]');
    notesEl.addEventListener('blur', async () => {
      const value = notesEl.value;
      if (value === (card.notes || '')) return;
      try { await putTask(card.id, { notes: value }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save notes: ${err.message}`, true); notesEl.value = card.notes || ''; }
    });

    const assigneeEl = drawerEl.querySelector('[data-field="assignee"]');
    assigneeEl.addEventListener('change', async () => {
      const value = assigneeEl.value;
      try {
        await moveCard(card.id, value ? 'assigned' : 'unassigned', value || undefined);
      } catch (err) {
        // moveCard already toasted the failure and nothing was persisted —
        // re-render the drawer from the still-current card so the select
        // snaps back to the actual assignee instead of showing the
        // rejected choice (#850 finding 9).
        const fresh = findCard(card.id);
        if (fresh) renderDrawer(fresh);
      }
    });

    const contextEl = drawerEl.querySelector('[data-field="context"]');
    contextEl.addEventListener('blur', async () => {
      const value = contextEl.value.trim();
      if (!value || value === card.context) return;
      try { await putTask(card.id, { context: value }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save context: ${err.message}`, true); contextEl.value = card.context || ''; }
    });

    const tagsEl = drawerEl.querySelector('[data-field="tags"]');
    const VALID_TAG = /^[\w-]+$/;
    tagsEl.addEventListener('blur', async () => {
      const tokens = tagsEl.value.split(/\s+/).map(t => t.replace(/^#/, '')).filter(Boolean);
      // Free text here writes straight to the task store — reject anything
      // that isn't a plain word/hyphen token (blocks a vault-comment
      // injection like `<!--id:...-->` stealing another task's id) and drop
      // any assignee-name token (the assignee comes from the select above,
      // not this field) rather than letting it silently double up as a tag
      // (#850 finding 8).
      const parsed = [];
      const rejected = [];
      for (const t of tokens) {
        if (VALID_TAG.test(t) && !ASSIGNEES.includes(t.toLowerCase())) parsed.push(t);
        else rejected.push(t);
      }
      if (rejected.length) {
        showToast(`Ignored invalid tag${rejected.length > 1 ? 's' : ''}: ${rejected.join(', ')}`, true);
      }
      tagsEl.value = parsed.join(' ');
      const assigneeTag = card.assignee ? [card.assignee] : [];
      try { await putTask(card.id, { tags: [...assigneeTag, ...parsed] }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save tags: ${err.message}`, true); tagsEl.value = nonAssigneeTags.join(' '); }
    });

    renderDrawerActions(card);
    renderDrawerSession(card);
  }

  function renderDrawerActions(card) {
    const actionsEl = drawerEl.querySelector('[data-field="actions"]');
    if (!actionsEl) return;
    const buttons = [];

    if (card.session && (card.session.source === 'claude_code' || card.session.source === 'codex')) {
      buttons.push(['Focus', async () => {
        try {
          const r = await fetch(`/api/agents/sessions/${encodeURIComponent(card.session.session_id)}/focus`, { method: 'POST' });
          if (!r.ok) throw new Error(await r.text());
          showToast('Pane selected in wezterm.', false);
        } catch (err) { showToast(`Focus failed: ${err.message}`, true); }
      }]);
    }
    if (card.session && !TERMINAL.has(card.session.status)) {
      buttons.push(['Kill', async () => {
        try {
          const r = await fetch(`/api/agents/sessions/${encodeURIComponent(card.session.session_id)}/kill`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: '' }),
          });
          if (!r.ok) throw new Error(await r.text());
          showToast('Session killed.', false);
          fetchBoard();
        } catch (err) { showToast(`Kill failed: ${err.message}`, true); }
      }]);
    }
    if (card.pending_question) {
      buttons.push(['Answer', () => openAnswerPrompt(card)]);
    }
    if (card.lane === 'review') {
      buttons.push(['Accept', async () => {
        try {
          const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/accept`, { method: 'POST' });
          if (!r.ok) throw new Error(await r.text());
          showToast('Accepted.', false);
          fetchBoard();
        } catch (err) { showToast(`Accept failed: ${err.message}`, true); }
      }]);
    }
    if (card.lane === 'human_queue' && !card.pending_question) {
      // A manually-filed #human card with no agent question behind it —
      // "Resolve" is the operator saying they've handled it by hand.
      buttons.push(['Resolve', async () => { await moveCard(card.id, 'done').catch(() => {}); }]);
    }

    for (const [label, handler] of buttons) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'drawer-action';
      btn.textContent = label;
      btn.onclick = handler;
      actionsEl.appendChild(btn);
    }
  }

  function openAnswerPrompt(card) {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="answer-title">
        <h2 id="answer-title">Answer</h2>
        <div class="target">${escapeHtml(card.pending_question.question)}</div>
        <textarea id="answer-text" placeholder="Your answer…"></textarea>
        <div class="actions">
          <button id="answer-cancel">Cancel</button>
          <button class="danger" id="answer-send">Send</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
    backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
    backdrop.querySelector('#answer-cancel').onclick = cleanup;
    backdrop.querySelector('#answer-send').onclick = async () => {
      const answer = backdrop.querySelector('#answer-text').value.trim();
      if (!answer) return;
      const btn = backdrop.querySelector('#answer-send');
      btn.disabled = true;
      btn.textContent = 'Sending…';
      try {
        const r = await fetch(`/api/agents/pending-questions/${encodeURIComponent(card.pending_question.id)}/answer`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer }),
        });
        if (!r.ok) throw new Error(await r.text());
        showToast('Answer sent.', false);
        cleanup();
        fetchBoard();
      } catch (err) {
        showToast(`Couldn't send answer: ${err.message}`, true);
        btn.disabled = false;
        btn.textContent = 'Send';
      }
    };
  }

  function renderDrawerSession(card) {
    const sessionWrap = drawerEl.querySelector('[data-field="session-panel"]');
    if (!sessionWrap) return;
    if (panel) { panel.close(); panel = null; }
    if (!card.session) {
      sessionWrap.innerHTML = '<div class="panel-empty">No linked session yet.</div>';
      return;
    }
    panel = new SessionPanel({ container: sessionWrap });
    panel.open(card.session);
  }

  // A change that arrived while the drawer had focus is deferred by
  // updateOpenDrawer's `!focused` check — flush it as soon as the operator
  // leaves the field, using the latest board already applied by applyBoard
  // (#850 round-2 finding 4).
  if (drawerEl) {
    drawerEl.addEventListener('focusout', () => {
      if (!openCardId) return;
      const fresh = findCard(openCardId);
      if (fresh) updateOpenDrawer(fresh);
    });
  }

  // ------------------------------------------------------------------
  // Wire filters + boot
  // ------------------------------------------------------------------

  [searchEl, laneFilterEl, assigneeFilterEl, hostFilterEl, tagFilterEl,
   contextFilterEl, recencyFilterEl, includeDoneEl].filter(Boolean).forEach(el => {
    const evt = (el.tagName === 'SELECT' || el.type === 'checkbox') ? 'change' : 'input';
    el.addEventListener(evt, () => render());
  });

  fetchBoard();
  connectStream();
}
