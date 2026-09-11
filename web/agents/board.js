// web/agents/board.js
//
// The Kanban board (#850) — the primary /agents view. Backed by the vault
// task store via GET/PUT /api/agents/board*, with a card drawer that reuses
// the shared SessionPanel (./panel.js) for the linked session's transcript,
// exactly like the Graph tab's side panel does. The drawer's own action
// row (Open, Go To, Resume, Kill, Answer, Accept, Reject, Reassign, Mark Done, Cancel, Delete)
// is rendered by session_actions.js's `renderActionRow` — the same
// function the Graph tab's side panel uses for its own header — so the
// embedded SessionPanel here is constructed with `showActions: false`
// (see `renderDrawerSession`) to avoid rendering the same session's
// Kill/Resume/Go To twice.
//
// Sorting is client-only — drag only ever changes which lane a card is in;
// no sort choice rewrites the vault's file order.

import {
  TERMINAL, routingLabel, escapeHtml, escapeAttr, showToast, SessionPanel,
} from './panel.js';
import { renderActionRow } from './session_actions.js';
import { descendantsOf } from './graph_encoding.js';
import { acceptCard, cardActionHandlers, cancelCard, openDeleteCardModal } from './card_actions.js';
import { renderAssignmentPickers } from './assignment.js';
import { LANES, laneColor } from './lanes.js';
import { routingFilterValue } from './graph_encoding.js';
import { POINTER_SLOP, pointerIsActive, shouldCancelPointerGesture } from './board_gesture.js';
import {
  compareSortValues, loadSortSelection as readSortSelection,
  saveSortSelection as writeSortSelection, sortCards,
} from './board_sort.js';
import {
  getFilters, setFilter, setFilters, resetFilters, subscribe as subscribeFilters,
  requestGraphFocus, requestBoardFocus, takeBoardFocus,
  onTabActivate, activateTab, getSelectedGraphCardId,
} from './linking.js';

const ASSIGNEES = ['me', 'claude', 'codex', 'hermes', 'local', 'cloud'];
// plan_lane_move (api/services/agent_board.py) 409s a lane=in_progress move
// whose assignee is one of these — "only the worker claims agent-assigned
// tasks" — so the composer must not let one through.
const AGENT_ASSIGNEES = ASSIGNEES.filter(a => a !== 'me');

const SORT_STORAGE_KEY = 'lifeos.agents.board.sort';
const DEFAULT_SORT = 'modified_desc';
const SORT_OPTIONS = new Set([
  'file', 'created_asc', 'created_desc', 'modified_asc', 'modified_desc', 'assignee_asc',
]);

// Tags the worker itself writes as it drives a task through its lifecycle
// (agent_board.py's RUNNING_TAG/BLOCKED_TAG/COMPLETED_TAG, worker.py's
// FAILED_TAG/BUDGET_EXCEEDED_TAG, and the accept endpoint's ACCEPTED_TAG)
// — the drawer's free-text Tags field must never show these as editable
// tokens, never let them be typed in (mirrors the ASSIGNEES rejection
// immediately below), and always preserve whatever the card already has
// on every save, the same way the field never lets a human type an
// assignee name into it. An explicit set, not a prefix match on `agent-`
// or the bare `agent` tag — `agent` is the worker's queue marker (an
// operator-editable label, not a claim), and an operator label that
// happens to start with `agent-` must stay editable too.
const LIFECYCLE_TAGS = new Set([
  'cloud-haiku', 'cloud-sonnet',
  'agent-running', 'agent-blocked', 'agent-completed',
  'agent-failed', 'agent-budget-exceeded', 'accepted',
  'agent-reassigned',
]);

// Card fields the drawer renders as editable inputs — used to decide
// whether an SSE tick needs to rebuild the drawer at all (#850 finding 2).
const DRAWER_EDITABLE_FIELDS = [
  'title', 'notes', 'tags', 'context', 'assignee', 'lane',
  // Scheduled-card fields, editable in the drawer since #850 finding 4.
  'name', 'message_content', 'enabled',
  // Full schedule editing — trigger type, timing, timezone, action,
  // executor, and delivery bot — all through PUT /api/scheduler/{id}.
  'schedule_type', 'schedule_value', 'timezone', 'action', 'executor', 'bot',
];

// Action a schedule fires when it's due. Mirrors VALID_ACTIONS in
// api/services/scheduler_store.py.
const SCHEDULE_ACTIONS = ['notify', 'prompt', 'endpoint', 'agent'];
// Executor tags a schedule's `agent` action hands off to the agent worker
// with. Mirrors the executor values accepted by api/routes/scheduler.py.
const SCHEDULE_EXECUTORS = ['local', 'cloud', 'cloud-haiku', 'cloud-sonnet'];

// Lane filter — multi-select checkbox dropdown. Hidden lanes are
// removed from the grid entirely (not just emptied), so the remaining
// .board-lane columns (flex: 1 1 260px, see web/agents.html CSS) widen to
// fill the space. The selection itself is the shared `lanes` filter
// (web/agents/linking.js) — persistence, migration, and validation of a
// stored id list all live there now; `visibleLanes` below is a local mirror
// kept in sync via `subscribeFilters`.
const DEFAULT_VISIBLE_LANE_IDS = LANES.filter(l => l.id !== 'done').map(l => l.id);
// plan_lane_move (api/services/agent_board.py) rejects `review` and
// `scheduled` with "cannot be set directly" — no per-lane "+" button for
// either, and both are excluded from the new-card composer's lane select.
const DIRECT_LANE_IDS = new Set(LANES.filter(l => l.id !== 'review' && l.id !== 'scheduled').map(l => l.id));

function loadSortSelection() {
  return readSortSelection(localStorage, SORT_STORAGE_KEY, SORT_OPTIONS, DEFAULT_SORT);
}

function saveSortSelection(value) {
  writeSortSelection(localStorage, SORT_STORAGE_KEY, value);
}


export function initBoard() {
  const lanesEl = document.getElementById('board-lanes');
  const searchEl = document.getElementById('board-search');
  const laneFilterDropdown = document.getElementById('board-lane-filter-dropdown');
  const laneFilterBtn = document.getElementById('board-lane-filter-btn');
  const laneFilterOptions = document.getElementById('board-lane-filter-options');
  const laneFilterLabel = document.getElementById('board-lane-filter-label');
  const laneFilterAllBtn = document.getElementById('board-lane-filter-all');
  const laneFilterClearBtn = document.getElementById('board-lane-filter-clear');
  const assigneeFilterEl = document.getElementById('board-filter-assignee');
  const hostFilterEl = document.getElementById('board-filter-host');
  const engineFilterEl = document.getElementById('board-filter-engine');
  const tagFilterEl = document.getElementById('board-filter-tag');
  const recencyFilterEl = document.getElementById('board-filter-recency');
  const sortFilterEl = document.getElementById('board-filter-sort');
  const includeDoneEl = document.getElementById('board-filter-done');
  const filterClearBtn = document.getElementById('board-filter-clear');
  const filterToggleBtn = document.getElementById('board-filter-toggle');
  const filterSummaryEl = document.getElementById('board-filter-summary');
  const filterControlsEl = document.getElementById('board-filter-controls');
  const newCardBtn = document.getElementById('board-new-card');
  const connStateEl = document.getElementById('board-connection-state');
  const assigneeDropsEl = document.getElementById('board-assignee-drops');
  const doneDropEl = document.getElementById('board-done-drop');
  const dropStatusEl = document.getElementById('board-drop-status');
  const drawerBackdrop = document.getElementById('board-drawer-backdrop');
  const drawerEl = document.getElementById('board-drawer');

  let board = { lanes: Object.fromEntries(LANES.map(l => [l.id, []])) };
  let visibleLanes = new Set(getFilters().lanes);
  let sortMode = loadSortSelection();
  if (sortFilterEl) sortFilterEl.value = sortMode;
  // Whether the first GET /api/agents/board (or board/stream tick) has
  // landed — see `drainBoardFocus` below, the same "re-queue if not loaded
  // yet" pattern graph.js's `drainGraphFocus` uses.
  let boardLoaded = false;
  let openCardId = null;
  let openCardLane = null;
  let openCardSnapshot = null;  // last card object the drawer was fully rendered from
  let panel = null;  // SessionPanel for the drawer's linked-session transcript
  let assignmentHandle = null;  // renderAssignmentPickers()'s return value for the open drawer, or null
  let tagPickerHandle = null;
  let selectedAssignee = null;
  let quickActionCardId = null;

  // A card snapshot older than an in-flight picker save re-seeds the
  // model/effort/host pickers with the pre-save value on remount -- a
  // drawer rebuild must not run while one of the open card's own picker
  // saves hasn't settled yet.
  function assignmentSaveInFlight() {
    return !!(assignmentHandle && assignmentHandle.isSaving && assignmentHandle.isSaving());
  }

  // A board refresh during an atomic tag save must not rebuild the drawer:
  // renderDrawer cancels the picker handle, which would discard any later
  // queued tag edits before their CAS writes run.
  function tagPickerSaveInFlight() {
    return !!(tagPickerHandle && tagPickerHandle.isSaving && tagPickerHandle.isSaving());
  }

  // A focused TEXTAREA or text INPUT inside the drawer holds uncommitted
  // keystrokes a `renderDrawer` innerHTML replacement would destroy.
  // `captureFocusedTextField` snapshots its identity (`data-field`), value,
  // and selection range immediately before such a repaint, and
  // `restoreFocusedTextField` puts them back into the rebuilt drawer's
  // matching control afterward. The old control's own `blur` still fires
  // during the replacement, so its normal save handler runs with the typed
  // value -- this only restores the on-screen state, it never suppresses a
  // save. If the rebuilt drawer carries no control for the same field (the
  // card's shape changed), restoring is skipped.
  function captureFocusedTextField() {
    const active = document.activeElement;
    if (!active || !drawerEl || !drawerEl.contains(active)) return null;
    if (active.tagName !== 'TEXTAREA' && active.tagName !== 'INPUT') return null;
    const field = active.dataset.field;
    if (!field) return null;
    return {
      field, value: active.value,
      selectionStart: active.selectionStart, selectionEnd: active.selectionEnd,
    };
  }

  function restoreFocusedTextField(captured) {
    if (!captured || !drawerEl) return;
    const el = drawerEl.querySelector(`[data-field="${captured.field}"]`);
    if (!el || (el.tagName !== 'TEXTAREA' && el.tagName !== 'INPUT')) return;
    el.value = captured.value;
    if (typeof el.setSelectionRange === 'function') {
      el.setSelectionRange(captured.selectionStart, captured.selectionEnd);
    }
    el.focus();
  }

  // Repaints the drawer's editable fields (including the model/effort/host
  // pickers) for `cardId` from the board state already applied to `board`,
  // preserving a focused text control's in-progress edit across the
  // repaint. A card id that doesn't match the open drawer -- closed, or
  // switched to another card -- is dropped.
  function attemptDrawerRebuild(cardId) {
    if (openCardId !== cardId) return;
    const captured = captureFocusedTextField();
    const f = findCard(cardId);
    if (!f) return;
    renderDrawer(f);
    openCardSnapshot = f;
    restoreFocusedTextField(captured);
  }

  // `revealCard`'s highlight — kept here (not just poked onto a DOM node
  // once) so a `render()` that rebuilds every card element in the middle of
  // the ~2s window (a board-stream SSE tick, common on a cold load) still
  // stamps it back onto the freshly-built element instead of losing it.
  let revealedCardId = null;
  let revealHighlightTimer = null;

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
    boardLoaded = true;
    updateFilterOptions();
    render();
    if (openCardId) {
      const fresh = findCard(openCardId);
      if (!fresh) { closeDrawer(); return; }
      updateOpenDrawer(fresh);
    }
    // Resolves a pending `?card=<id>` deep link, or a graph node's "Show on
    // board", once this board payload is the first to land after the
    // intent was set — a no-op on every other tick.
    drainBoardFocus();
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

    if (sessionUnchanged) {
      if (panel && freshSessionId) panel.updateMeta(fresh.session);
      // Refresh the action row in place on every tick, independent of the
      // full-drawer-rebuild's own `!focused` guard below — that guard
      // exists to protect the notes/title/tags inputs from a mid-keystroke
      // reset, and this container holds none of them.
      // `renderActionRow`'s own signature check (session_actions.js) makes
      // this a no-op unless the decided action set actually changed, so a
      // session reaching a terminal state or an Answer being sent updates
      // the row even while the drawer has focus, rather than leaving a
      // stale button behind until focus leaves.
      renderDrawerActions(fresh);
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
    if ((fieldsChanged || !sessionUnchanged) && !focused
      && !assignmentSaveInFlight() && !tagPickerSaveInFlight()) {
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
  function updateFilterOptions() {
    // Unions the assignment (fields.host — where a card WILL run) with the
    // observation (session.host — where a session DID run), so a host a
    // card is assigned to but hasn't run a session on yet still appears in
    // the option list.
    const hosts = [...new Set(
      allCards().flatMap(c => [c.session && c.session.host, c.fields && c.fields.host]).filter(Boolean)
    )].sort();
    // The shared `host` filter (linking.js) can name a host no board card
    // currently uses at all — e.g. a session running on it never got linked
    // to a task, so `_task_card` never surfaces it — in which case the
    // option list above would never contain it and the select would fall
    // back to blank. Inject it as a selectable option too, so the control
    // always shows what's actually filtering rather than rendering blank.
    const sharedHost = getFilters().host;
    const optionHosts = (sharedHost && sharedHost !== 'all' && !hosts.includes(sharedHost))
      ? [...hosts, sharedHost].sort()
      : hosts;
    const hostKey = optionHosts.join('|');
    if (hostFilterEl && hostKey !== _lastHostKey) {
      _lastHostKey = hostKey;
      const current = hostFilterEl.value;
      hostFilterEl.innerHTML = '<option value="all">all hosts</option>'
        + optionHosts.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join('');
      // `host` is shared (linking.js) — the persisted/cross-tab value wins
      // over the select's own pre-repopulation value once it's actually a
      // valid option, so a host filter restored from localStorage before
      // this option list existed yet still lands once it can.
      const preferred = getFilters().host;
      if (preferred && (preferred === 'all' || optionHosts.includes(preferred))) {
        hostFilterEl.value = preferred;
      } else if (current && (current === 'all' || optionHosts.includes(current))) {
        hostFilterEl.value = current;
      }
    }

  }

  function cardMatchesFilters(card) {
    // Read every SHARED key straight from the store rather than trusting a
    // DOM select's current value — a select can lag the store (its option
    // list populated asynchronously, e.g. `host` above) or simply not be
    // the thing the render loop should trust, the same reasoning
    // `applyFilters` in graph.js already follows.
    const shared = getFilters();

    const search = (shared.search || '').trim().toLowerCase();
    if (search) {
      const haystack = card.kind === 'schedule'
        ? (card.name || '')
        : `${card.title || ''} ${card.notes || ''}`;
      if (!haystack.toLowerCase().includes(search)) return false;
    }

    const assigneeSel = shared.assignee || 'all';
    if (assigneeSel !== 'all') {
      if (card.kind !== 'task') return false;
      if (assigneeSel === 'unassigned') {
        if (card.assignee) return false;
      } else if (card.assignee !== assigneeSel) {
        return false;
      }
    }

    const hostSel = shared.host || 'all';
    if (hostSel !== 'all') {
      // Matches on either the assignment or the observation — a
      // card matches a selected host when its fields.host names it OR its
      // linked session ran on it.
      const sessionHost = card.session && card.session.host;
      const assignedHost = card.fields && card.fields.host;
      if (sessionHost !== hostSel && assignedHost !== hostSel) return false;
    }

    const tagQuery = (shared.tag || '').trim().toLowerCase().replace(/^#/, '');
    if (tagQuery) {
      if (card.kind !== 'task') return false;
      if (!(card.tags || []).some(t => t.toLowerCase().includes(tagQuery))) return false;
    }

    // Shared engine filter — mirrored on the graph as `#filter-route`.
    // A card matches when its linked session's routing/source (the same
    // notion `routingFilterValue` computes for a graph node) matches; a
    // card with no linked session matches only when the filter is "all".
    const engineSel = shared.engine || 'all';
    if (engineSel !== 'all') {
      if (!card.session || routingFilterValue(card.session) !== engineSel) return false;
    }

    // `null` (the shared default) means "the operator has never set a
    // recency" — the board's own default is all time, so it filters
    // nothing, same as an explicit 'all'.
    const recencyRaw = shared.recency;
    if (recencyRaw != null && recencyRaw !== 'all') {
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

  function setDropStatus(message = '', isError = false) {
    if (!dropStatusEl) return;
    dropStatusEl.textContent = message;
    dropStatusEl.classList.toggle('error', isError);
  }

  function assignmentPolicyReason(card) {
    const policy = card && card.policy && card.policy.assignee;
    return policy && policy.allowed === false
      ? (policy.reason || "This card's assignee can't be changed right now.")
      : null;
  }

  function canDropCard(card, targetLane) {
    if (!card || card.kind !== 'task') return { allowed: false, reason: 'Only task cards can be moved.' };
    if (!DIRECT_LANE_IDS.has(targetLane)) {
      return { allowed: false, reason: `Can't move card to ${laneLabel(targetLane)}.` };
    }
    const policy = card.policy && card.policy.lanes && card.policy.lanes[targetLane];
    if (policy && policy.allowed === false) {
      return { allowed: false, reason: policy.reason || `Can't move card to ${laneLabel(targetLane)}.` };
    }
    return { allowed: true, reason: '' };
  }

  // Every way to assign a card — tapping an assignee then a card, dragging
  // an assignee onto a card, dragging a card onto an assignee — lands here,
  // so none of them can clear the tray selection, report, or offer undo
  // differently from the others.
  function assignCardTo(card, assignee) {
    if (!card || card.kind !== 'task') return;
    clearAssigneeSelection();
    const reason = assignmentPolicyReason(card);
    if (reason) {
      setDropStatus(reason, true);
      showToast(reason, true);
      return;
    }
    // Captured before the write so Undo restores where the card actually
    // was, not wherever the board has drifted to by the time it is clicked.
    const priorLane = card.lane;
    const priorAssignee = card.assignee || null;
    const title = card.title || card.id;
    setDropStatus(`Assigning ${assignee}…`);
    // Keep this on the same lane endpoint and request shape as the drawer's
    // assignee select. The server remains authoritative for claimed cards
    // and for cards whose derived lane cannot change with the tag update.
    moveCard(card.id, 'assigned', assignee)
      .then(() => {
        setDropStatus(`Assigned to ${assignee}.`);
        showUndoableToast(`Assigned "${title}" to ${assignee}.`, () => (
          moveCard(card.id, priorLane, priorAssignee || undefined)
        ));
      })
      .catch(() => setDropStatus('Assignment refused.', true));
  }

  function assignSelectedAssignee(card) {
    if (!selectedAssignee || !card || card.kind !== 'task') return false;
    assignCardTo(card, selectedAssignee);
    return true;
  }

  function assignAssigneeToCard(cardId, assignee) {
    assignCardTo(findCard(cardId), assignee);
  }

  // A confirmation the operator can take back. `undo` returns a promise;
  // its rejection is reported rather than leaving the toast claiming a
  // reversal that never happened — `moveCard` already toasts the server's
  // own reason, so the label just has to stop pretending it is working.
  function showUndoableToast(message, undo) {
    showToast(message, false, {
      duration: 8000,
      actionLabel: 'Undo',
      actionAriaLabel: `Undo: ${message}`,
      onAction: ({ toast, action }) => {
        action.textContent = 'Undoing…';
        return undo()
          .then(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); })
          .catch(() => { action.textContent = 'Undo'; });
      },
    });
  }

  function clearAssigneeSelection() {
    if (!selectedAssignee) return;
    selectedAssignee = null;
    renderAssigneeDrops();
  }

  function renderAssigneeDrops() {
    if (!assigneeDropsEl) return;
    assigneeDropsEl.innerHTML = ASSIGNEES.map(assignee => `
      <button type="button" class="board-drop-target board-assignee-drop${selectedAssignee === assignee ? ' selected' : ''}"
              data-assignee="${assignee}" aria-pressed="${selectedAssignee === assignee}"
              aria-label="Assign selected card to ${assignee}">
        ${assignee}
      </button>
    `).join('');
    assigneeDropsEl.querySelectorAll('.board-assignee-drop').forEach(button => {
      button.addEventListener('click', () => {
        if (consumeClickSuppression('tray', `assignee:${button.dataset.assignee}`)) {
          return;
        }
        selectedAssignee = selectedAssignee === button.dataset.assignee ? null : button.dataset.assignee;
        renderAssigneeDrops();
        setDropStatus(selectedAssignee
          ? `Tap a card to assign it to ${selectedAssignee}, or drag this button onto a card.`
          : '');
      });
      button.addEventListener('pointerdown', e => onPointerDown(e, {
        kind: 'assignee', assignee: button.dataset.assignee, sourceEl: button,
      }));
    });
  }

  function updateQuickDropTargets() {
    if (!doneDropEl) return;
    const hidden = !visibleLanes.has('done');
    doneDropEl.hidden = !hidden;
    doneDropEl.setAttribute('aria-label', hidden
      ? 'Drop a card here to move it to Done' : 'Done lane is visible');
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  function cardChips(card) {
    const chips = [];
    if (card.assignee) chips.push(`<span class="board-chip board-chip-assignee">${escapeHtml(card.assignee)}</span>`);
    if (card.fields && card.fields.model) chips.push(`<span class="board-chip">${escapeHtml(card.fields.model)}</span>`);
    if (card.fields && card.fields.effort) chips.push(`<span class="board-chip">${escapeHtml(card.fields.effort)}</span>`);
    // Assignment chip: fields.host is where the card WILL run,
    // written by the drawer's host dropdown. Rendered only when it names a
    // machine other than the API host — a card assigned to "this machine"
    // shows no assignment chip, matching the drawer's own "this machine"
    // empty-choice semantics. If board.api_host is missing (older/broken
    // payload), every non-empty fields.host is treated as "other" — fail
    // visible rather than silently hiding the assignment.
    const assignedHost = card.fields && card.fields.host;
    let assignedChipRendered = false;
    if (assignedHost && assignedHost !== board.api_host) {
      chips.push(`<span class="board-chip board-chip-assigned-host" title="assigned host">${escapeHtml(assignedHost)}</span>`);
      assignedChipRendered = true;
    }
    // Observation chip: session.host is where a linked session DID run —
    // distinct from the assignment above (both render, distinguishably,
    // when they differ). Suppressed when it would repeat the assignment
    // chip actually rendered above (the steady state once a worker
    // dispatches to fields.host: the session it creates records that same
    // host, so showing both would print the identical hostname twice on a
    // narrow lane).
    if (card.session && card.session.host && !(assignedChipRendered && card.session.host === assignedHost)) {
      chips.push(`<span class="board-chip board-chip-host" title="ran on">${escapeHtml(card.session.host)}</span>`);
    }
    // Session chip → graph tab — clickable only when a session is
    // actually linked; `renderTaskCard` wires its click once this markup
    // is mounted (a `stopPropagation` handler can't be expressed inline
    // here without re-escaping into an attribute).
    if (card.session) {
      chips.push(`<span class="board-chip board-chip-session" data-session-id="${escapeHtml(card.session.session_id)}" title="Open in graph">↗ session</span>`);
    }
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
    div.tabIndex = 0;
    div.setAttribute('role', 'article');
    // Re-stamps the reveal highlight on a freshly-built element — a
    // `render()` in the middle of `revealCard`'s ~2s window (e.g. a
    // board-stream SSE tick) rebuilds every card node from scratch, so this
    // is what keeps the highlight surviving that rebuild rather than a
    // one-time class added to a node that gets discarded.
    if (card.id === revealedCardId) div.classList.add('reveal-highlight');
    const showAccept = card.lane === 'review';
    div.innerHTML = `
      <div class="board-card-title">${live ? '<span class="live-dot" title="live"></span>' : ''}${escapeHtml(card.title || '(untitled)')}</div>
      ${card.pending_question ? `<div class="board-card-question">❓ ${escapeHtml(card.pending_question.question)}</div>` : ''}
      <div class="board-card-chips">${cardChips(card)}</div>
      ${showAccept ? '<button type="button" class="board-card-accept">Accept</button>' : ''}
    `;
    div.addEventListener('click', () => {
      if (consumeClickSuppression('card', card.id)) return;
      if (assignSelectedAssignee(card)) return;
      openDrawer(card.id);
    });
    div.addEventListener('focus', () => { quickActionCardId = card.id; });
    div.addEventListener('keydown', e => {
      if (e.target !== div || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      if (assignSelectedAssignee(card)) return;
      openDrawer(card.id);
    });
    const sessionChip = div.querySelector('.board-chip-session');
    if (sessionChip) {
      sessionChip.addEventListener('click', (e) => {
        e.stopPropagation();
        requestGraphFocus(sessionChip.dataset.sessionId);
        activateTab('graph');
      });
    }
    div.addEventListener('pointerdown', (e) => onPointerDown(e, {
      kind: 'card', card, sourceEl: div,
    }));
    if (showAccept) {
      const acceptBtn = div.querySelector('.board-card-accept');
      acceptBtn.addEventListener('mousedown', (e) => e.stopPropagation());
      acceptBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        acceptBtn.disabled = true;
        runCardAction(() => acceptCard(card, fetchBoard)).finally(() => {
          if (acceptBtn.isConnected) acceptBtn.disabled = false;
        });
      });
    }
    return div;
  }

  function renderScheduleCard(card) {
    const div = document.createElement('div');
    div.className = 'board-card board-card-schedule';
    div.dataset.cardId = card.id;
    div.dataset.lane = card.lane;
    if (card.id === revealedCardId) div.classList.add('reveal-highlight');
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
    if (visibleLanes.size === 0) {
      const hint = document.createElement('div');
      hint.className = 'board-lanes-empty-hint';
      hint.textContent = 'No lanes selected — use the Lanes filter above to show columns.';
      lanesEl.appendChild(hint);
      return;
    }
    for (const lane of LANES) {
      if (!visibleLanes.has(lane.id)) continue;
      const column = document.createElement('div');
      column.className = 'board-lane';
      column.dataset.lane = lane.id;

      const cards = sortCards(
        (board.lanes[lane.id] || [])
          .map(c => ({ ...c, lane: lane.id }))
          .filter(cardMatchesFilters),
        sortMode,
      );

      column.innerHTML = `
        <div class="board-lane-header" style="border-top-color:${laneColor(lane.id)}">${escapeHtml(lane.label)} <span class="board-lane-count">${cards.length}</span></div>
        ${DIRECT_LANE_IDS.has(lane.id) ? `<button type="button" class="board-lane-add" data-lane="${lane.id}" title="New card in ${escapeHtml(lane.label)}">+</button>` : ''}
      `;
      const addBtn = column.querySelector('.board-lane-add');
      if (addBtn) addBtn.addEventListener('click', () => openNewCardForm(lane.id));
      const cardsEl = document.createElement('div');
      cardsEl.className = 'board-lane-cards';
      for (const card of cards) {
        cardsEl.appendChild(card.kind === 'schedule' ? renderScheduleCard(card) : renderTaskCard(card));
      }
      column.appendChild(cardsEl);
      lanesEl.appendChild(column);
    }
  }

  // Makes card `cardId` visible and scrolls it into view — the graph tab's
  // "Show on board" action, a `?card=<id>` deep link, and activating the
  // board tab with a card selected on the graph (see `drainBoardFocus`
  // below) all land here. Relaxes EVERY shared filter that currently hides
  // the card (lanes, assignee, host, engine, tag, search, recency) through
  // one batched `setFilters` call, then confirms the card actually rendered
  // before scrolling/highlighting — a board-local filter (context, include
  // cancelled) is left as-is, the operator set those on purpose and they
  // have no shared counterpart to relax. An unknown id is reported rather
  // than left to fail silently.
  function revealCard(cardId, opts) {
    const openDrawerFlag = !!(opts && opts.openDrawer);
    const card = findCard(cardId);
    if (!card) {
      showToast(`No such card: ${cardId}`, true);
      return;
    }
    const shared = getFilters();
    const updates = {};
    if (!visibleLanes.has(card.lane)) updates.lanes = [...shared.lanes, card.lane];
    if (shared.assignee !== 'all') {
      const matches = shared.assignee === 'unassigned' ? !card.assignee : card.assignee === shared.assignee;
      if (!matches) updates.assignee = 'all';
    }
    if (shared.host !== 'all') {
      const sessionHost = card.session && card.session.host;
      const assignedHost = card.fields && card.fields.host;
      if (sessionHost !== shared.host && assignedHost !== shared.host) updates.host = 'all';
    }
    if (shared.engine !== 'all' && (!card.session || routingFilterValue(card.session) !== shared.engine)) {
      updates.engine = 'all';
    }
    if (shared.tag) {
      const tagQuery = shared.tag.trim().toLowerCase().replace(/^#/, '');
      if (!(card.tags || []).some(t => t.toLowerCase().includes(tagQuery))) updates.tag = '';
    }
    if (shared.search) {
      const search = shared.search.trim().toLowerCase();
      const haystack = card.kind === 'schedule' ? (card.name || '') : `${card.title || ''} ${card.notes || ''}`;
      if (!haystack.toLowerCase().includes(search)) updates.search = '';
    }
    if (shared.recency != null && shared.recency !== 'all') {
      const stamp = card.kind === 'schedule' ? card.next_fire_at : card.updated_at;
      if (stamp && (Date.now() - new Date(stamp).getTime()) / 1000 > Number(shared.recency)) {
        updates.recency = 'all';
      }
    }
    // `setFilters` notifies synchronously — by the time it returns, this
    // module's own `syncSharedFilterControls` subscriber has already
    // re-rendered the board against the widened filters, so the card's
    // element (if nothing board-local still hides it) already exists below.
    if (Object.keys(updates).length > 0) setFilters(updates);

    revealedCardId = cardId;
    const el = lanesEl.querySelector(`.board-card[data-card-id="${CSS.escape(cardId)}"]`);
    if (el) {
      el.scrollIntoView({ block: 'nearest' });
      el.classList.add('reveal-highlight');
    }
    clearTimeout(revealHighlightTimer);
    revealHighlightTimer = setTimeout(() => {
      revealedCardId = null;
      const current = lanesEl.querySelector(`.board-card[data-card-id="${CSS.escape(cardId)}"]`);
      if (current) current.classList.remove('reveal-highlight');
    }, 2000);
    if (openDrawerFlag) openDrawer(cardId);
  }

  function drainBoardFocus() {
    const intent = takeBoardFocus();
    if (intent) {
      if (!boardLoaded) {
        // Data hasn't arrived yet (a tab-activation drain can fire before
        // the first GET /api/agents/board resolves) — put the intent back
        // so `applyBoard`'s own drain resolves it once it actually can,
        // rather than wrongly reporting a real card as unknown.
        requestBoardFocus(intent.cardId, { openDrawer: intent.openDrawer });
        return;
      }
      revealCard(intent.cardId, { openDrawer: intent.openDrawer });
      return;
    }
    // No explicit chip/URL intent pending — if the graph currently has a
    // card-linked session selected,
    // activating the board tab reveals that card too, without requiring the
    // panel's own "Show on board" button click. That button stays as a
    // separate, explicit way to do the same thing.
    if (!boardLoaded) return;
    const graphCardId = getSelectedGraphCardId();
    if (graphCardId) revealCard(graphCardId, { openDrawer: false });
  }
  onTabActivate((name) => { if (name === 'board') drainBoardFocus(); });

  // ------------------------------------------------------------------
  // Drag and drop — one Pointer Events model for mouse, pen, and touch. CSS
  // reserves the horizontal axis on draggable sources (`pan-y`), so the UA
  // cannot negotiate away a horizontal drag after pointerdown; vertical
  // movement remains native scrolling. This avoids the native HTML5 DnD path,
  // which is not available to touch users.
  // ------------------------------------------------------------------

  let dragState = null;   // { kind, card, assignee, sourceEl, pointerId, pointerType, ghost, startX, startY, moved }
  let suppressNextClick = null;
  let suppressNextTrayClick = null;

  function setClickSuppression(kind, key) {
    const slot = kind === 'tray' ? 'suppressNextTrayClick' : 'suppressNextClick';
    const current = { key, timer: null };
    current.timer = setTimeout(() => {
      if ((kind === 'tray' ? suppressNextTrayClick : suppressNextClick) === current) {
        if (kind === 'tray') suppressNextTrayClick = null;
        else suppressNextClick = null;
      }
    }, 700);
    if (kind === 'tray') suppressNextTrayClick = current;
    else suppressNextClick = current;
  }

  function consumeClickSuppression(kind, key) {
    const current = kind === 'tray' ? suppressNextTrayClick : suppressNextClick;
    if (!current || current.key !== key) return false;
    clearTimeout(current.timer);
    if (kind === 'tray') suppressNextTrayClick = null;
    else suppressNextClick = null;
    return true;
  }

  function clearDragTarget() {
    document.querySelectorAll('.board-lane.drag-over, .board-drop-target.drop-allowed, .board-drop-target.drop-refused')
      .forEach(el => el.classList.remove('drag-over', 'drop-allowed', 'drop-refused'));
  }

  function endPointerDrag() {
    document.removeEventListener('pointermove', onDragMove);
    document.removeEventListener('pointerup', onDragUp);
    document.removeEventListener('pointercancel', onDragCancel);
    clearDragTarget();
    if (!dragState) return;
    const { ghost, sourceEl, pointerId } = dragState;
    if (sourceEl && pointerId != null && sourceEl.releasePointerCapture) {
      try { sourceEl.releasePointerCapture(pointerId); } catch (_) {}
    }
    if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
    if (sourceEl) sourceEl.classList.remove('dragging-source');
    document.body.classList.remove('board-dragging');
  }

  function onPointerDown(e, source) {
    if (e.isPrimary === false || dragState || (e.pointerType === 'mouse' && e.button !== 0)) return;
    if (e.target.closest('button, input, select, textarea, a') && source.kind === 'card') return;
    // A session chip has its own click navigation. Do not let the card's
    // drag handler capture that pointer on the card, or the browser retargets
    // the trailing pointerup/click to the card and opens its drawer instead
    // of running the chip's graph jump.
    if (source.kind === 'card' && e.target.closest('.board-chip-session')) return;
    const pointerType = e.pointerType || 'mouse';
    const state = {
      ...source, sourceEl: source.sourceEl || e.currentTarget,
      pointerId: e.pointerId, pointerType,
      startX: e.clientX, startY: e.clientY, moved: false, cancelled: false, ghost: null,
      holdReady: true,
    };
    dragState = state;
    // Capturing a touch pointer at pointerdown can make the UA's scroll
    // negotiation race the custom drag. Let the browser retain the touch
    // target until a horizontal drag has actually started; mouse/pen still
    // capture immediately so leaving the source does not lose the gesture.
    if (pointerType !== 'touch' && state.sourceEl && state.sourceEl.setPointerCapture && e.pointerId != null) {
      try { state.sourceEl.setPointerCapture(e.pointerId); } catch (_) {}
    }
    document.addEventListener('pointermove', onDragMove);
    document.addEventListener('pointerup', onDragUp);
    document.addEventListener('pointercancel', onDragCancel);
  }

  function clearDragSelection() {
    const selection = window.getSelection && window.getSelection();
    if (selection && selection.removeAllRanges) selection.removeAllRanges();
  }

  function onDragMove(e) {
    if (!pointerIsActive(dragState, e)) return;
    const dx = e.clientX - dragState.startX;
    const dy = e.clientY - dragState.startY;
    if (!dragState.moved && Math.hypot(dx, dy) < POINTER_SLOP) return;
    // Let the browser own vertical scrolling from a card. Once cancelled,
    // pointerup is ignored and the card still receives its ordinary click
    // only when the browser decides this was a tap rather than a scroll.
    if (!dragState.moved && shouldCancelPointerGesture(dragState, dx, dy)) {
      dragState.cancelled = true;
      endPointerDrag();
      dragState = null;
      return;
    }
    if (!dragState.moved) {
      dragState.moved = true;
      if (e.cancelable) e.preventDefault();
      document.body.classList.add('board-dragging');
      clearDragSelection();
      dragState.sourceEl.classList.add('dragging-source');
      if (dragState.pointerType === 'touch' && dragState.sourceEl.setPointerCapture && e.pointerId != null) {
        try { dragState.sourceEl.setPointerCapture(e.pointerId); } catch (_) {}
      }
      const rect = dragState.sourceEl.getBoundingClientRect();
      const ghost = dragState.sourceEl.cloneNode(true);
      ghost.classList.add('board-card-ghost');
      ghost.style.position = 'fixed';
      ghost.style.pointerEvents = 'none';
      ghost.style.width = rect.width + 'px';
      ghost.style.zIndex = '200';
      document.body.appendChild(ghost);
      dragState.ghost = ghost;
    }
    if (dragState.kind === 'card' && lanesEl) {
      const lanesRect = lanesEl.getBoundingClientRect();
      const edge = 28;
      if (e.clientX < lanesRect.left + edge) lanesEl.scrollLeft -= 18;
      else if (e.clientX > lanesRect.right - edge) lanesEl.scrollLeft += 18;
    }
    dragState.ghost.style.left = (e.clientX + 12) + 'px';
    dragState.ghost.style.top = (e.clientY + 12) + 'px';
    clearDragTarget();
    const under = document.elementFromPoint(e.clientX, e.clientY);
    if (dragState.kind === 'card') {
      const target = under?.closest('.board-lane, #board-done-drop, .board-assignee-drop');
      // An assignee button is a drop target in both directions — dragging a
      // card onto one assigns it, exactly as dragging the button onto the
      // card does — so it is decided against the same assignment policy
      // rather than the lane policy the lanes and Done target use.
      if (target && target.classList.contains('board-assignee-drop')) {
        const assignee = target.dataset.assignee;
        const reason = assignmentPolicyReason(dragState.card);
        target.classList.add(reason ? 'drop-refused' : 'drop-allowed');
        dragState.targetLane = null;
        dragState.targetAssignee = reason ? null : assignee;
        dragState.targetEl = target;
        setDropStatus(reason || `Drop to assign ${assignee}.`, !!reason);
      } else if (target) {
        const targetLane = target.id === 'board-done-drop' ? 'done' : target.dataset.lane;
        const decision = canDropCard(dragState.card, targetLane);
        target.classList.add(decision.allowed ? 'drop-allowed' : 'drop-refused');
        if (target.classList.contains('board-lane')) target.classList.add('drag-over');
        dragState.targetLane = targetLane;
        dragState.targetAssignee = null;
        dragState.targetEl = target;
        setDropStatus(decision.allowed ? `Drop in ${laneLabel(targetLane)}.` : decision.reason, !decision.allowed);
      } else {
        dragState.targetLane = null;
        dragState.targetAssignee = null;
        dragState.targetEl = null;
        setDropStatus('');
      }
    } else {
      const cardEl = under?.closest('.board-card[data-card-id]');
      if (cardEl) {
        const targetCard = findCard(cardEl.dataset.cardId);
        const reason = !targetCard || targetCard.kind !== 'task'
          ? 'Only task cards can be assigned.'
          : assignmentPolicyReason(targetCard);
        cardEl.classList.add(reason ? 'drop-refused' : 'drop-allowed');
        dragState.targetCardId = targetCard && targetCard.kind === 'task' ? targetCard.id : null;
        setDropStatus(reason || `Drop to assign ${dragState.assignee}.`, !!reason);
      } else {
        dragState.targetCardId = null;
        setDropStatus('');
      }
    }
  }

  function onDragUp(e) {
    if (!dragState || e.isPrimary === false || e.pointerId !== dragState.pointerId) return;
    const state = dragState;
    endPointerDrag();
    dragState = null;
    if (state.moved) {
      clearDragSelection();
      if (state.kind === 'card') {
        // A drag that ends in the source lane (or outside a lane) is still
        // a drag, not a request to open the drawer through its trailing tap.
        setClickSuppression('card', state.card.id);
        if (state.targetAssignee) {
          setClickSuppression('tray', `assignee:${state.targetAssignee}`);
          assignCardTo(state.card, state.targetAssignee);
        } else if (state.targetLane && state.targetLane !== state.card.lane) {
          onCardDropped(state.card.id, state.targetLane);
        }
      } else {
        setClickSuppression('tray', `assignee:${state.assignee}`);
        if (state.targetCardId) {
          setClickSuppression('card', state.targetCardId);
          assignAssigneeToCard(state.targetCardId, state.assignee);
        }
      }
    }
    setDropStatus('');
  }

  function onDragCancel(e) {
    if (!dragState || e && (e.isPrimary === false || e.pointerId !== dragState.pointerId)) return;
    endPointerDrag();
    dragState = null;
    setDropStatus('');
  }

  function onCardDropped(cardId, targetLane) {
    const card = findCard(cardId);
    if (!card || card.kind !== 'task') return;
    // Review and Scheduled are never a direct drag target —
    // plan_lane_move 400s both with "cannot be set directly" for EVERY
    // card, regardless of state, so refusing them here — matching the
    // existing DIRECT_LANE_IDS gating on the composer/lane-add button —
    // means dropping on either never round-trips to the server just to
    // 400.
    if (!DIRECT_LANE_IDS.has(targetLane)) {
      showToast(`Can't move card to ${laneLabel(targetLane)}.`, true);
      render();
      return;
    }
    // The server is still the authority — this is a fast path that skips
    // the round trip when the board already knows the move is refused,
    // matching `card.policy` exactly (see _card_policy in
    // api/routes/agents.py). A stale board (the policy hasn't caught up
    // with an out-of-band change) still gets caught by moveCard's own
    // server-error toast path below.
    const laneEntry = card.policy && card.policy.lanes && card.policy.lanes[targetLane];
    if (laneEntry && laneEntry.allowed === false) {
      showToast(laneEntry.reason || `Can't move card to ${laneLabel(targetLane)}.`, true);
      // Matches moveCard's own failure path: clears any stray drag-over
      // class while the short-lived trailing-click guard expires normally.
      render();
      return;
    }
    if (targetLane === 'done' && card.lane === 'review') {
      // Review -> Done is the same explicit acceptance path as the drawer
      // and the card's inline Accept button, including its tag transition.
      runCardAction(() => acceptCard(card, fetchBoard));
      return;
    }
    let assignee;
    if (targetLane === 'assigned') {
      // Dragging onto Assigned defaults to "me" (the common case: an
      // operator claiming a card for themself) unless the card already has
      // an assignee, which the lane endpoint keeps when passed explicitly.
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
        // Callers that need to know where the card actually ended up (e.g.
        // the composer revealing the right lane) read
        // it off the resolved value; fetchBoard()'s own resolution (undefined)
        // is irrelevant to them, so hand back `data` once the board refresh
        // settles.
        return fetchBoard().then(() => data);
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

  // `targetLane` preselects the composer's own Lane select (still
  // changeable by the operator) — omitted (defaults to unassigned) for the
  // top-bar "+ New card" button, which never moves the card after creation.
  function openNewCardForm(targetLane) {
    const initialLane = DIRECT_LANE_IDS.has(targetLane) ? targetLane : 'unassigned';
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="new-card-title">
        <h2 id="new-card-title">New card</h2>
        <label style="font-size:0.75rem;color:var(--text-secondary)">Title</label>
        <input id="new-card-desc" type="text" style="width:100%;box-sizing:border-box;margin:0.35rem 0;padding:0.4rem;background:var(--bg-elev);color:var(--text-primary);border:1px solid var(--border);border-radius:6px" />
        <label style="font-size:0.75rem;color:var(--text-secondary)">Notes (optional)</label>
        <textarea id="new-card-notes" placeholder="Notes…"></textarea>
        <label style="font-size:0.75rem;color:var(--text-secondary)">Lane</label>
        <select id="new-card-lane" style="width:100%;margin:0.35rem 0;padding:0.4rem;background:var(--bg-elev);color:var(--text-primary);border:1px solid var(--border);border-radius:6px">
          ${LANES.filter(l => DIRECT_LANE_IDS.has(l.id)).map(l => `<option value="${l.id}" ${l.id === initialLane ? 'selected' : ''}>${escapeHtml(l.label)}</option>`).join('')}
        </select>
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

    const laneSelectEl = backdrop.querySelector('#new-card-lane');
    const assigneeSelectEl = backdrop.querySelector('#new-card-assignee');
    // Picking an assignee while Lane still reads Unassigned would otherwise
    // silently file the card in Assigned anyway (derive_lane files any task
    // carrying an assignee tag there) with the Lane control still
    // contradicting that outcome — flip it to what will actually happen
    // instead of leaving it to lie.
    assigneeSelectEl.addEventListener('change', () => {
      if (assigneeSelectEl.value && laneSelectEl.value === 'unassigned') {
        laneSelectEl.value = 'assigned';
      } else if (!assigneeSelectEl.value && laneSelectEl.value === 'assigned') {
        // The reverse of the flip above — clearing the assignee back to
        // blank must not leave Lane stuck on Assigned, or Create then fails
        // on "Pick an assignee for the Assigned lane." against a select the
        // operator never touched.
        laneSelectEl.value = 'unassigned';
      }
    });

    backdrop.querySelector('#new-card-create').onclick = async () => {
      const desc = backdrop.querySelector('#new-card-desc').value.trim();
      if (!desc) return;
      const notes = backdrop.querySelector('#new-card-notes').value.trim();
      const lane = laneSelectEl.value;
      const assignee = assigneeSelectEl.value;
      // The assignee-select's own `change` listener (above) only flips Lane
      // when the operator picks an assignee — it never re-fires if they then
      // edit Lane back to Unassigned by hand, leaving it lying about where
      // the card will actually go: derive_lane (api/services/agent_board.py)
      // files any task carrying an assignee tag under Assigned regardless of
      // what Lane says. Recompute here so both the guard checks below and
      // the lane PUT match reality.
      const effectiveLane = (lane === 'unassigned' && assignee) ? 'assigned' : lane;
      if (effectiveLane === 'assigned' && !assignee) {
        showToast('Pick an assignee for the Assigned lane.', true);
        return;
      }
      // plan_lane_move 409s In progress for any AGENT_ASSIGNEES tag ("only
      // the worker claims agent-assigned tasks") — reject client-side
      // before creating anything, mirroring the Assigned guard above.
      if (effectiveLane === 'in_progress' && assignee && AGENT_ASSIGNEES.includes(assignee)) {
        showToast('Only "me" can be assigned directly to In progress — the worker claims agent-assigned tasks itself.', true);
        return;
      }
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
        // A 200 with a non-JSON body must not throw here — the task was
        // already created; falling into the outer catch left the composer
        // open with Create re-enabled, and a second click created a
        // duplicate. The `created && created.id` guard
        // below already handles a null result cleanly.
        const created = await r.json().catch(() => null);
        // Where the card is actually filed before any lane PUT runs:
        // derive_lane keys off the assignee tag the create call sent, never
        // off the composer's own Lane select — a fresh card with no
        // assignee tag lands Unassigned, one with an assignee tag lands
        // Assigned. Updated below with whatever a successful moveCard PUT
        // reports it actually landed in.
        let landedLane = assignee ? 'assigned' : 'unassigned';
        if (effectiveLane !== 'unassigned' && created && created.id) {
          try {
            const moved = await moveCard(created.id, effectiveLane, assignee || undefined);
            // moveCard's own success path already re-fetches the board —
            // avoid a second GET /api/agents/board round-trip here.
            if (moved && moved.lane) landedLane = moved.lane;
          } catch (_) {
            // moveCard already toasted the failure and never re-fetches on
            // its own failure path — do it here so the board reflects the
            // card that DID get created (just not moved). Nothing beyond the
            // create call landed, so landedLane keeps the pre-move value
            // above rather than the lane the PUT failed to reach.
            await fetchBoard();
          }
        } else {
          // A non-Unassigned lane was requested but there's no id to move
          // with — a 200 whose body didn't parse to an object with one.
          // Without the toast below, the operator would see nothing at
          // all: the task IS created, just not where they asked, with
          // zero indication of that otherwise.
          if (effectiveLane !== 'unassigned' && !(created && created.id)) {
            showToast(`Card created, but couldn't confirm its id to move it to ${laneLabel(effectiveLane)} — check ${laneLabel(landedLane)}.`, true);
          }
          await fetchBoard();
        }
        // A card that landed in a lane the filter is currently hiding would
        // otherwise have zero on-screen feedback — reveal that lane so it's
        // actually visible. landedLane is always the lane the card actually
        // reached, never the one requested: a failed move leaves it at the
        // tag-derived resting lane set above, and a card whose id we never
        // learned only ever reached that same tag-derived lane. So
        // revealing landedLane can never surface a lane the card isn't
        // actually in — no separate failure gate is needed.
        ensureLaneVisible(landedLane);
        cleanup();
      } catch (err) {
        showToast(`Couldn't create card: ${err.message}`, true);
        btn.disabled = false;
        btn.textContent = 'Create';
      }
    };
  }

  if (newCardBtn) newCardBtn.addEventListener('click', () => openNewCardForm());

  // ------------------------------------------------------------------
  // Drawer
  // ------------------------------------------------------------------

  function closeDrawer() {
    if (tagPickerHandle && tagPickerHandle.cancel) tagPickerHandle.cancel();
    tagPickerHandle = null;
    openCardId = null;
    openCardLane = null;
    openCardSnapshot = null;
    if (panel) { panel.close(); panel = null; }
    if (drawerBackdrop) drawerBackdrop.hidden = true;
    if (drawerEl) drawerEl.innerHTML = '';
  }

  function cancelTagPickerWrites() {
    if (tagPickerHandle && tagPickerHandle.cancel) tagPickerHandle.cancel();
    tagPickerHandle = null;
  }

  // Confirmation modals invalidate queued picker writes while they are open:
  // Cancel leaves the drawer alive without allowing an obsolete save to land,
  // and a failed confirmed mutation re-arms the picker for a fresh edit.
  function pauseTagPickerWrites() {
    if (tagPickerHandle && tagPickerHandle.cancel) tagPickerHandle.cancel();
  }

  function rearmTagPickerWrites() {
    if (tagPickerHandle && tagPickerHandle.rearm) tagPickerHandle.rearm();
  }

  function runCardAction(action) {
    cancelTagPickerWrites();
    return action();
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

  // Click-outside-close. The drawer sits INSIDE the full-screen
  // fixed backdrop (`justify-content: flex-end` puts it at the right
  // edge), so a click anywhere on the board background/lane/card actually
  // lands on the backdrop element itself — closing when the click's target
  // IS the backdrop covers all of those in one listener, and a click
  // inside .board-drawer (whose target is never the backdrop) never
  // matches. Guarded against a mousedown/mouseup pair that starts on one
  // side of the backdrop boundary and ends on the other — a scrollbar-drag
  // (mousedown inside the drawer, mouseup on the backdrop) or a text
  // selection dragged inward (mousedown on the backdrop, mouseup inside the
  // drawer) both still fire a `click` on the backdrop (the nearest common
  // ancestor of the two targets) — so only close when the mousedown, the
  // mouseup, AND the click all targeted the backdrop itself.
  let drawerBackdropMouseDownOnSelf = false;
  let drawerBackdropMouseUpOnSelf = false;
  if (drawerBackdrop) {
    drawerBackdrop.addEventListener('mousedown', (e) => {
      drawerBackdropMouseDownOnSelf = (e.target === drawerBackdrop);
    });
    drawerBackdrop.addEventListener('mouseup', (e) => {
      drawerBackdropMouseUpOnSelf = (e.target === drawerBackdrop);
    });
    drawerBackdrop.addEventListener('click', (e) => {
      if (e.target === drawerBackdrop && drawerBackdropMouseDownOnSelf && drawerBackdropMouseUpOnSelf) closeDrawer();
      drawerBackdropMouseDownOnSelf = false;
      drawerBackdropMouseUpOnSelf = false;
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!openCardId) return;
    if (drawerBackdrop && drawerBackdrop.hidden) return;
    // A modal (new-card composer, answer prompt) renders on top of the
    // drawer (.modal-backdrop z-index 100 > .board-drawer-backdrop's 90) —
    // let it own Escape instead of closing the drawer underneath it.
    if (document.querySelector('.modal-backdrop')) return;
    closeDrawer();
  });

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

  async function putBoardTags(taskId, tags) {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(taskId)}/tags`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags }),
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

  // Bot-registry cache backing the schedule drawer's Bot select — mirrors
  // assignment.js's loadHostCatalog in NOT caching a failure (so the next
  // drawer open retries), but without its reachability TTL/cooldown: the
  // bot registry doesn't drift minute to minute the way host online/offline
  // status does, so a plain once-per-page-load cache on success is enough.
  let _botsCatalogPromise = null;
  function loadBotCatalog(fetchImpl = fetch) {
    if (_botsCatalogPromise) return _botsCatalogPromise;
    const promise = fetchImpl('/api/scheduler/bots')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .catch(() => { _botsCatalogPromise = null; return null; });
    _botsCatalogPromise = promise;
    return promise;
  }

  function formatNextFire(iso) {
    if (!iso) return 'Not scheduled to fire again.';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return 'Not scheduled to fire again.';
    return `Next fire: ${d.toLocaleString()}`;
  }

  function formatLastRun(lastRun) {
    if (!lastRun) return "Hasn't run yet.";
    const at = lastRun.at ? new Date(lastRun.at).toLocaleString() : 'unknown time';
    const outcome = lastRun.outcome || 'unknown';
    const snippet = lastRun.snippet ? ` — ${lastRun.snippet}` : '';
    return `Last run: ${at} — ${outcome}${snippet}`;
  }

  // Notes autosize — height tracks content up to 2/3 of the
  // viewport height, after which `.drawer-notes-autosize`'s
  // `overflow-y: auto` (web/agents.html) takes over scrolling. Scoped to
  // the task notes textarea only — the schedule drawer's message-content
  // textarea keeps its plain fixed/manual-resize box.
  function autosizeNotesTextarea(el) {
    if (!el) return;
    el.style.height = 'auto';
    // `* { box-sizing: border-box }` (web/agents.html) means the assigned
    // `height` is a border-box total, but `scrollHeight` never counts the
    // border — only content + padding. Without adding the border widths
    // back, the box is assigned exactly `scrollHeight`, so its actual
    // content+padding area ends up `scrollHeight` minus the border, 2px
    // (1px top + 1px bottom) short of the content at every length past the
    // minimum — clipping and forcing an early internal scroll.
    const cs = getComputedStyle(el);
    const borderY = parseFloat(cs.borderTopWidth || '0') + parseFloat(cs.borderBottomWidth || '0');
    const maxHeight = window.innerHeight * (2 / 3);
    el.style.height = Math.min(el.scrollHeight + borderY, maxHeight) + 'px';
  }

  const TAG_TOKEN = /^[\w-]+$/;
  function normalizeEditableTag(raw) {
    const normalized = String(raw || '')
      .trim()
      .replace(/^#+/, '')
      .replace(/\s+/g, '-')
      .replace(/[^\w-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^[-_]+|[-_]+$/g, '')
      .toLowerCase();
    if (!normalized || !TAG_TOKEN.test(normalized)
      || ASSIGNEES.includes(normalized) || LIFECYCLE_TAGS.has(normalized)) return null;
    return normalized;
  }

  function uniqueEditableTags(tags) {
    const seen = new Set();
    const result = [];
    for (const tag of tags || []) {
      const normalized = normalizeEditableTag(tag);
      if (normalized && !seen.has(normalized)) {
        seen.add(normalized);
        result.push(normalized);
      }
    }
    return result;
  }

  function editableTagsForCard(card) {
    return uniqueEditableTags((card.tags || []).filter(
      tag => !ASSIGNEES.includes(String(tag).toLowerCase().replace(/^#/, ''))
        && !LIFECYCLE_TAGS.has(String(tag).toLowerCase().replace(/^#/, '')),
    ));
  }

  function availableEditableTags() {
    return uniqueEditableTags(allCards().flatMap(card => card.tags || []))
      .sort((a, b) => compareSortValues(a, b));
  }

  function sameTags(left, right) {
    return left.length === right.length && left.every((tag, index) => tag === right[index]);
  }

  function mountTagPicker(card, initialTags) {
    const picker = drawerEl.querySelector('[data-field="tags-picker"]');
    const search = picker && picker.querySelector('[data-field="tags"]');
    const chips = picker && picker.querySelector('[data-field="tag-chips"]');
    const options = picker && picker.querySelector('[data-field="tag-options"]');
    if (!picker || !search || !chips || !options) return;

    let selected = uniqueEditableTags(initialTags);
    let confirmed = selected.slice();
    let saveChain = Promise.resolve();
    let pendingSaves = 0;
    let suppressBlur = false;
    let activeOption = -1;
    let showingLegacyValue = true;
    let cancelled = false;
    let saveGeneration = 0;

    function renderChips() {
      chips.innerHTML = selected.map(tag => `
        <span class="drawer-tag-chip" data-tag="${escapeHtml(tag)}">
          <span>#${escapeHtml(tag)}</span>
          <button type="button" class="drawer-tag-chip-remove" data-remove-tag="${escapeHtml(tag)}"
                  aria-label="Remove tag ${escapeHtml(tag)}">×</button>
        </span>
      `).join('');
      chips.querySelectorAll('[data-remove-tag]').forEach(button => {
        button.addEventListener('click', () => removeTag(button.dataset.removeTag));
      });
    }

    function renderOptions() {
      const query = search.value.trim().replace(/^#+/, '').toLowerCase();
      const applied = new Set(selected);
      const matches = availableEditableTags().filter(tag => !applied.has(tag)
        && (!query || tag.includes(query)));
      const canCreate = !!query && !applied.has(normalizeEditableTag(query))
        && !availableEditableTags().includes(normalizeEditableTag(query))
        && !!normalizeEditableTag(query);
      options.innerHTML = matches.map(tag =>
        `<button type="button" class="drawer-tag-option" role="option" data-select-tag="${escapeHtml(tag)}">#${escapeHtml(tag)}</button>`
      ).join('');
      if (canCreate) {
        options.innerHTML += `<button type="button" class="drawer-tag-option drawer-tag-option-create" role="option" data-create-tag="${escapeHtml(normalizeEditableTag(query))}">Create new #${escapeHtml(normalizeEditableTag(query))}</button>`;
      }
      options.hidden = document.activeElement !== search || (!matches.length && !canCreate);
      activeOption = -1;
      options.querySelectorAll('[data-select-tag], [data-create-tag]').forEach(button => {
        button.addEventListener('mousedown', () => { suppressBlur = true; });
        button.addEventListener('click', () => {
          if (button.dataset.createTag) addTag(button.dataset.createTag);
          else addTag(button.dataset.selectTag);
          suppressBlur = false;
          search.focus();
        });
      });
      search.setAttribute('aria-expanded', String(!options.hidden));
    }

    function queueSave(nextTags) {
      const requested = uniqueEditableTags(nextTags);
      const generation = saveGeneration;
      selected = requested;
      renderChips();
      pendingSaves += 1;
      saveChain = saveChain.then(async () => {
        try {
          if (cancelled || generation !== saveGeneration) return;
          await putBoardTags(card.id, requested);
          confirmed = requested.slice();
          if (!cancelled && generation === saveGeneration) await fetchBoard();
        } catch (err) {
          // A later queued edit is still the operator's current intent; only
          // revert if this failed request is what is currently displayed.
          if (sameTags(selected, requested)) {
            selected = confirmed.slice();
            renderChips();
          }
          if (!cancelled) showToast(`Couldn't save tags: ${err.message}`, true);
        } finally {
          pendingSaves -= 1;
        }
      });
    }

    function addTag(raw) {
      const tag = normalizeEditableTag(raw);
      if (!tag || selected.includes(tag)) return;
      search.value = '';
      queueSave([...selected, tag]);
      renderOptions();
    }

    function removeTag(raw) {
      const tag = normalizeEditableTag(raw);
      if (!tag) return;
      queueSave(selected.filter(current => current !== tag));
      renderOptions();
      search.focus();
    }

    function saveLegacyText() {
      const raw = search.value.trim();
      // Preserve the old space-separated edit affordance for pasted text and
      // invalid tokens while keeping a normal one-word search non-mutating.
      if (!raw) return;
      if (!/[\s#<>]/.test(raw)) {
        const normalized = normalizeEditableTag(raw);
        // A new single token retains the old blur-to-save behavior. Existing
        // tags remain searches until the operator selects them explicitly.
        if (normalized && !availableEditableTags().includes(normalized)) {
          search.value = normalized;
          queueSave([normalized]);
        }
        return;
      }
      const parsed = [];
      const rejected = [];
      raw.split(/\s+/).forEach(token => {
        const plain = token.replace(/^#/, '');
        if (TAG_TOKEN.test(plain) && normalizeEditableTag(plain)) parsed.push(plain);
        else rejected.push(token);
      });
      const normalized = uniqueEditableTags(parsed);
      if (rejected.length) {
        showToast(`Ignored invalid tag${rejected.length > 1 ? 's' : ''}: ${rejected.join(', ')}`, true);
      }
      search.value = normalized.join(' ');
      queueSave(normalized);
    }

    renderChips();
    // Keep the old space-separated value visible until the search control is
    // first focused; this makes the migration legible to keyboard users and
    // preserves pasted-text compatibility without making it the live picker
    // query once the control is opened.
    search.value = selected.join(' ');
    search.addEventListener('focus', () => {
      if (showingLegacyValue) {
        showingLegacyValue = false;
        search.value = '';
      }
      renderOptions();
    });
    search.addEventListener('input', renderOptions);
    search.addEventListener('blur', () => {
      if (suppressBlur) return;
      saveLegacyText();
      setTimeout(() => {
        if (document.activeElement && picker.contains(document.activeElement)) return;
        options.hidden = true;
        search.setAttribute('aria-expanded', 'false');
      }, 0);
    });
    search.addEventListener('keydown', (event) => {
      const optionButtons = [...options.querySelectorAll('[data-select-tag], [data-create-tag]')];
      if (event.key === 'ArrowDown' && optionButtons.length) {
        event.preventDefault();
        activeOption = (activeOption + 1) % optionButtons.length;
        optionButtons[activeOption].focus();
      } else if (event.key === 'ArrowUp' && optionButtons.length) {
        event.preventDefault();
        activeOption = (activeOption - 1 + optionButtons.length) % optionButtons.length;
        optionButtons[activeOption].focus();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        const active = optionButtons[activeOption];
        if (active) active.click();
        else if (normalizeEditableTag(search.value)) addTag(search.value);
      } else if (event.key === 'Escape') {
        options.hidden = true;
        search.setAttribute('aria-expanded', 'false');
      }
    });
    const handle = {
      cancel: () => {
        cancelled = true;
        saveGeneration += 1;
        selected = confirmed.slice();
        renderChips();
        search.value = selected.join(' ');
      },
      rearm: () => { cancelled = false; },
      isSaving: () => !cancelled && pendingSaves > 0,
      whenIdle: () => saveChain,
    };
    tagPickerHandle = handle;
    return handle;
  }

  // Dates arrive as either a bare YYYY-MM-DD (`created_date`, `done_date`,
  // `cancelled_date`, `due_date`) or a full ISO timestamp (`updated_at`).
  // Both render as a short local date; the title attribute keeps the exact
  // value for anyone who needs it.
  function formatCardDate(value) {
    if (!value) return null;
    const iso = String(value);
    const parsed = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00` : iso);
    if (Number.isNaN(parsed.getTime())) return { label: iso, exact: iso };
    return {
      label: parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }),
      exact: iso,
    };
  }

  // The read-only block: what the card reports about itself, as opposed to
  // the fields above it that the operator edits. A date the card doesn't
  // carry is omitted rather than rendered empty.
  function cardMetaHtml(card) {
    const rows = [
      ['Created', card.created_date],
      ['Updated', card.updated_at],
      ['Due', card.due_date],
      ['Completed', card.done_date],
      ['Cancelled', card.cancelled_date],
    ];
    const items = [];
    for (const [label, raw] of rows) {
      const formatted = formatCardDate(raw);
      if (!formatted) continue;
      items.push(`
        <div class="drawer-meta-item">
          <span class="drawer-meta-label">${label}</span>
          <span class="drawer-meta-value" title="${escapeAttr(formatted.exact)}">${escapeHtml(formatted.label)}</span>
        </div>
      `);
    }
    if (card.status) {
      items.push(`
        <div class="drawer-meta-item">
          <span class="drawer-meta-label">Status</span>
          <span class="drawer-meta-value">${escapeHtml(card.status)}</span>
        </div>
      `);
    }
    for (const key of ['model', 'effort', 'host']) {
      const value = card.fields && card.fields[key];
      if (!value) continue;
      items.push(`
        <div class="drawer-meta-item">
          <span class="drawer-meta-label">${key}</span>
          <span class="drawer-meta-value">${escapeHtml(value)}</span>
        </div>
      `);
    }
    items.push(`
      <div class="drawer-meta-item">
        <span class="drawer-meta-label">ID</span>
        <span class="drawer-meta-value drawer-meta-id">${escapeHtml(card.id)}</span>
      </div>
    `);
    return items.join('');
  }

  function renderDrawer(card) {
    if (!drawerEl) return;
    cancelTagPickerWrites();
    assignmentHandle = null;
    const isTask = card.kind === 'task';
    // The Tags field never shows an assignee tag OR a worker lifecycle
    // tag as an editable token — both are managed elsewhere (the
    // Assignee select above, and the worker/accept endpoint respectively)
    // and must survive a Tags-field save untouched.
    const editableTags = editableTagsForCard(card);
    const titleValue = isTask ? (card.title || '') : (card.name || '');
    // `card.policy` is the server's own decision — the drawer never
    // re-derives these rules, it just disables-and-explains. A schedule
    // card carries no `policy` at all: treat that as fully allowed rather
    // than throwing, matching every other place this file reads
    // `card.policy`.
    const assigneePolicy = (card.policy && card.policy.assignee) || { allowed: true, reason: null };
    const assigneeDisabled = assigneePolicy.allowed === false;
    drawerEl.innerHTML = `
      <div class="drawer-header">
        <button class="panel-close" data-action="drawer-close">×</button>
        <input class="drawer-title" data-field="title" value="${escapeHtml(titleValue)}" />
      </div>
      ${isTask ? `
      <div class="drawer-section drawer-meta" data-field="meta">${cardMetaHtml(card)}</div>
      <div class="drawer-section">
      <label class="drawer-label">Notes</label>
      <textarea class="drawer-notes drawer-notes-autosize" data-field="notes" placeholder="Notes…">${escapeHtml(card.notes || '')}</textarea>
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Assignee</label>
          <select class="drawer-assignee" data-field="assignee" ${assigneeDisabled ? 'disabled' : ''}>
            <option value="">unassigned</option>
            ${ASSIGNEES.map(a => `<option value="${a}" ${card.assignee === a ? 'selected' : ''}>${a}</option>`).join('')}
          </select>
          ${assigneeDisabled ? `<div class="drawer-field-reason" data-field="assignee-reason">${escapeHtml(assigneePolicy.reason || "This card's assignee can't be changed right now.")}</div>` : ''}
        </div>
        <div>
          <label class="drawer-label">Context</label>
          <input class="drawer-context" data-field="context" value="${escapeHtml(card.context || '')}" />
        </div>
      </div>
      <label class="drawer-label" id="drawer-tags-label-${escapeHtml(card.id)}">Tags</label>
      <div class="drawer-tags-picker" data-field="tags-picker" role="group" aria-labelledby="drawer-tags-label-${escapeHtml(card.id)}">
        <div class="drawer-tag-chips" data-field="tag-chips" role="list"></div>
        <input class="drawer-tags drawer-tags-search" data-field="tags" type="search" role="combobox"
               aria-autocomplete="list" aria-expanded="false" autocomplete="off"
               placeholder="Search or add tags…" ${assigneeDisabled ? 'disabled' : ''} />
        <div class="drawer-tag-options" data-field="tag-options" role="listbox" hidden></div>
      </div>
      ${assigneeDisabled ? `<div class="drawer-field-reason" data-field="tags-reason">${escapeHtml(assigneePolicy.reason || "This card's tags can't be changed right now.")}</div>` : ''}
      <div class="drawer-assignment" data-field="assignment"></div>
      </div>
      <div class="drawer-section drawer-section-actions">
        <div class="drawer-actions" data-field="actions"></div>
      </div>
      <div class="drawer-section"><div class="drawer-session" data-field="session-panel"></div></div>
      ` : `
      <label class="drawer-label">Message</label>
      <textarea class="drawer-notes" data-field="message-content" placeholder="Message…">${escapeHtml(card.message_content || '')}</textarea>
      <label class="drawer-label"><input type="checkbox" data-field="enabled" ${card.enabled ? 'checked' : ''} /> Enabled</label>
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Schedule type</label>
          <select class="drawer-select" data-field="schedule-type">
            <option value="cron" ${card.schedule_type === 'cron' ? 'selected' : ''}>cron</option>
            <option value="once" ${card.schedule_type === 'once' ? 'selected' : ''}>once</option>
          </select>
        </div>
        <div>
          <label class="drawer-label" data-field="schedule-value-label">${card.schedule_type === 'once' ? 'When (ISO datetime)' : 'Cron expression'}</label>
          <input class="drawer-schedule-value" data-field="schedule-value" value="${escapeHtml(card.schedule_value || '')}" placeholder="${card.schedule_type === 'once' ? '2026-06-03T15:05:00' : '0 9 * * *'}" />
          <div class="drawer-field-error" data-field="schedule-value-error" hidden></div>
        </div>
      </div>
      <label class="drawer-label">Timezone</label>
      <input class="drawer-timezone" data-field="timezone" value="${escapeHtml(card.timezone || '')}" placeholder="e.g. America/New_York" />
      <div class="drawer-field-error" data-field="timezone-error" hidden></div>
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Action</label>
          <select class="drawer-select" data-field="action">
            ${SCHEDULE_ACTIONS.map(a => `<option value="${a}" ${card.action === a ? 'selected' : ''}>${a}</option>`).join('')}
          </select>
        </div>
        <div>
          <div data-row="executor" ${card.action === 'agent' ? '' : 'hidden'}>
            <label class="drawer-label">Executor</label>
            <select class="drawer-select" data-field="executor">
              <option value="" ${!card.executor ? 'selected' : ''}>default route</option>
              ${SCHEDULE_EXECUTORS.map(e => `<option value="${e}" ${card.executor === e ? 'selected' : ''}>${e}</option>`).join('')}
            </select>
          </div>
          <div data-row="bot" ${card.action === 'agent' ? 'hidden' : ''}>
            <label class="drawer-label">Bot</label>
            <select class="drawer-select" data-field="bot" disabled></select>
            <div class="drawer-field-reason" data-field="bot-reason" hidden></div>
          </div>
        </div>
      </div>
      <div class="drawer-schedule-info" data-field="next-fire-preview"></div>
      <div class="drawer-schedule-info" data-field="last-run-info"></div>
      <div class="drawer-actions" data-field="schedule-actions">
        <button class="drawer-action" data-action="trigger-now">${card.schedule_type === 'once' ? 'Trigger now (disables this one-off)' : 'Trigger now'}</button>
      </div>
      <div class="drawer-actions" data-field="actions"></div>
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
      renderScheduleDrawerFields(card);
      renderDrawerActions(card);
      return;
    }

    const notesEl = drawerEl.querySelector('[data-field="notes"]');
    notesEl.addEventListener('blur', async () => {
      const value = notesEl.value;
      if (value === (card.notes || '')) return;
      try { await putTask(card.id, { notes: value }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save notes: ${err.message}`, true); notesEl.value = card.notes || ''; }
    });
    notesEl.addEventListener('input', () => autosizeNotesTextarea(notesEl));
    autosizeNotesTextarea(notesEl);  // size to existing content on open/re-render

    mountTagPicker(card, editableTags);

    const assigneeEl = drawerEl.querySelector('[data-field="assignee"]');
    assigneeEl.addEventListener('change', async () => {
      const value = assigneeEl.value;
      // Captured immediately, ahead of `moveCard`'s own `fetchBoard()`
      // possibly running `updateOpenDrawer` -> `renderDrawer`, which
      // would otherwise reset `assignmentHandle` to null ahead of the
      // wait below on the save this handler actually started with.
      const handle = assignmentHandle;
      try {
        await moveCard(card.id, value ? 'assigned' : 'unassigned', value || undefined);
        // moveCard already awaited fetchBoard(), so the board's own state is
        // current — but updateOpenDrawer's `!focused` check skips the
        // rebuild while the select (inside the drawer) still holds focus,
        // which a native <select> keeps after a change event. Re-render
        // explicitly so the model/effort/host pickers and the Open button
        // reflect the new assignee immediately, not only once focus leaves
        // the drawer.
        //
        // A picker save still in flight when the assignee change resolves
        // must not have its snapshot re-seeded by this rebuild — and
        // neither must a picker save the operator starts while this
        // rebuild is still waiting. `whenIdle()` reads `handle`'s current
        // save chain each time it's called, so re-checking `isSaving()`
        // after every fetch and calling `whenIdle()` again keeps the wait
        // going until no save is outstanding, however many queue up in the
        // meantime.
        //
        // The rebuild paints exactly once, as soon as the wait above
        // settles, whether or not a text control inside the drawer holds
        // focus — `attemptDrawerRebuild` preserves that control's
        // in-progress edit across the repaint rather than blocking on it,
        // so the model/effort/host pickers and the Open button never sit
        // stuck on the old assignee's chrome waiting for the operator to
        // leave a text field first. `attemptDrawerRebuild` also drops the
        // paint outright if the drawer isn't currently showing this card:
        // it can sit closed, or open on a different card, by the time this
        // settles.
        const rebuild = () => attemptDrawerRebuild(card.id);
        const settleThenRebuild = () => handle.whenIdle().then(() => fetchBoard()).then(() => {
          if (handle.isSaving && handle.isSaving()) return settleThenRebuild();
          rebuild();
        }).catch(() => {});
        if (handle && handle.isSaving && handle.isSaving()) {
          settleThenRebuild();
        } else {
          rebuild();
        }
      } catch (err) {
        // moveCard already toasted the failure and nothing was persisted —
        // only the Assignee select is wrong, so snap it back directly to
        // the card's actual assignee instead of rebuilding the whole
        // drawer (which would re-seed the pickers from a stale snapshot
        // while a picker save is still in flight).
        const fresh = findCard(card.id);
        assigneeEl.value = (fresh || card).assignee || '';
      }
    });

    const contextEl = drawerEl.querySelector('[data-field="context"]');
    contextEl.addEventListener('blur', async () => {
      const value = contextEl.value.trim();
      if (!value || value === card.context) return;
      try { await putTask(card.id, { context: value }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save context: ${err.message}`, true); contextEl.value = card.context || ''; }
    });

    // The drawer's own Assignee select above is the one assignee writer —
    // it already writes exactly one assignee tag through the lane endpoint
    // and supports `me`, which the module's own engine select can't
    // represent. So mount the model/effort/host pickers but hide the
    // module's engine row to avoid a second, conflicting assignee control.
    const assignmentEl = drawerEl.querySelector('[data-field="assignment"]');
    if (assignmentEl) {
      assignmentHandle = renderAssignmentPickers(assignmentEl, card, {
        putTask,
        onSaved: () => fetchBoard(),
        onError: (message) => { if (message) showToast(`Couldn't save assignment: ${message}`, true); },
      });
      const engineRow = assignmentEl.querySelector('[data-row="engine"]');
      if (engineRow) engineRow.hidden = true;
    }

    renderDrawerActions(card);
    renderDrawerSession(card);
  }

  // Wires the scheduled-card drawer's editable fields (renderDrawer's
  // `!isTask` branch above). Every field saves through putSchedule — the
  // scheduler API, never the vault file directly — on blur for text
  // inputs and on change for selects/the checkbox, refetching the board on
  // a successful save. A rejected save shows the server's `detail` inline
  // next to the offending field (schedule value, timezone) or as a toast
  // (every other field), and snaps the control back to the last value the
  // server actually accepted. The schedule type select is the one
  // exception: changing it only updates the value field's label and
  // placeholder locally — it saves together with the schedule value, on
  // the value field's own blur, so a type and a value that doesn't parse
  // under it can never reach the server in the same write (see below).
  function renderScheduleDrawerFields(card) {
    const msgEl = drawerEl.querySelector('[data-field="message-content"]');
    msgEl.addEventListener('blur', async () => {
      const value = msgEl.value;
      if (value === (card.message_content || '')) return;
      try { await putSchedule(card.id, { message_content: value }); await fetchBoard(); }
      catch (err) { showToast(`Couldn't save message: ${err.message}`, true); msgEl.value = card.message_content || ''; }
    });

    const enabledEl = drawerEl.querySelector('[data-field="enabled"]');
    enabledEl.addEventListener('change', async () => {
      try {
        const resp = await putSchedule(card.id, { enabled: enabledEl.checked });
        // The store recomputes next_trigger_at for an enabled change too
        // (clearing it on disable) — refresh the preview from the
        // response the same way the type/value/timezone saves do, since
        // updateOpenDrawer skips its own rebuild while this checkbox
        // holds focus.
        previewEl.textContent = formatNextFire(resp.next_trigger_at);
        await fetchBoard();
      }
      catch (err) { showToast(`Couldn't update enabled: ${err.message}`, true); enabledEl.checked = !!card.enabled; }
    });

    const typeEl = drawerEl.querySelector('[data-field="schedule-type"]');
    const valueEl = drawerEl.querySelector('[data-field="schedule-value"]');
    const valueLabelEl = drawerEl.querySelector('[data-field="schedule-value-label"]');
    const valueErrorEl = drawerEl.querySelector('[data-field="schedule-value-error"]');
    const tzEl = drawerEl.querySelector('[data-field="timezone"]');
    const tzErrorEl = drawerEl.querySelector('[data-field="timezone-error"]');
    const actionEl = drawerEl.querySelector('[data-field="action"]');
    const executorRow = drawerEl.querySelector('[data-row="executor"]');
    const executorEl = drawerEl.querySelector('[data-field="executor"]');
    const botRow = drawerEl.querySelector('[data-row="bot"]');
    const botEl = drawerEl.querySelector('[data-field="bot"]');
    const botReasonEl = drawerEl.querySelector('[data-field="bot-reason"]');
    const previewEl = drawerEl.querySelector('[data-field="next-fire-preview"]');
    const lastRunEl = drawerEl.querySelector('[data-field="last-run-info"]');
    const triggerBtnEl = drawerEl.querySelector('[data-action="trigger-now"]');

    previewEl.textContent = formatNextFire(card.next_fire_at);
    lastRunEl.textContent = formatLastRun(card.last_run);

    // What the server last actually accepted for each field — a rejected
    // save reverts its control to these, not to whatever was showing when
    // the drawer opened, the same rule the task drawer's assignee select
    // follows above.
    let lastSavedType = card.schedule_type;
    let lastSavedValue = card.schedule_value;
    let lastSavedTz = card.timezone || '';
    let lastSavedAction = card.action;
    let lastSavedExecutor = card.executor || '';
    let lastSavedBot = card.bot || '';

    function updateValueLabel(type) {
      if (type === 'once') {
        valueLabelEl.textContent = 'When (ISO datetime)';
        valueEl.placeholder = '2026-06-03T15:05:00';
      } else {
        valueLabelEl.textContent = 'Cron expression';
        valueEl.placeholder = '0 9 * * *';
      }
    }

    function setActionVisibility(action) {
      const isAgent = action === 'agent';
      executorRow.hidden = !isAgent;
      botRow.hidden = isAgent;
    }

    typeEl.addEventListener('change', () => {
      // Type-only, with no matching value, is unsaveable by construction
      // (a cron string and an ISO datetime never parse as each other) —
      // saving it here would either write a type/value pair the server
      // rejects, or one it accepts but that leaves a live schedule
      // pointed at the wrong parser. So this only updates the label and
      // placeholder; the value field's blur handler below carries the
      // type along with whatever value the operator enters to match it,
      // so a conversion always reaches the server as one matched pair.
      updateValueLabel(typeEl.value);
    });

    valueEl.addEventListener('blur', async () => {
      const value = valueEl.value;
      const typeChanged = typeEl.value !== lastSavedType;
      if (value === (lastSavedValue || '') && !typeChanged) return;
      const patch = { schedule_value: value };
      if (typeChanged) patch.schedule_type = typeEl.value;
      try {
        const resp = await putSchedule(card.id, patch);
        lastSavedValue = value;
        if (typeChanged) lastSavedType = typeEl.value;
        valueErrorEl.hidden = true;
        valueErrorEl.textContent = '';
        previewEl.textContent = formatNextFire(resp.next_trigger_at);
        if (typeChanged) {
          triggerBtnEl.textContent = lastSavedType === 'once' ? 'Trigger now (disables this one-off)' : 'Trigger now';
        }
        await fetchBoard();
      } catch (err) {
        valueErrorEl.textContent = err.message;
        valueErrorEl.hidden = false;
        valueEl.value = lastSavedValue || '';
        if (typeChanged) {
          typeEl.value = lastSavedType;
          updateValueLabel(lastSavedType);
        }
      }
    });

    tzEl.addEventListener('blur', async () => {
      const value = tzEl.value;
      if (value === lastSavedTz) return;
      try {
        const resp = await putSchedule(card.id, { timezone: value });
        lastSavedTz = value;
        tzErrorEl.hidden = true;
        tzErrorEl.textContent = '';
        previewEl.textContent = formatNextFire(resp.next_trigger_at);
        await fetchBoard();
      } catch (err) {
        tzErrorEl.textContent = err.message;
        tzErrorEl.hidden = false;
        tzEl.value = lastSavedTz;
      }
    });

    actionEl.addEventListener('change', async () => {
      // Show/hide the executor and bot controls immediately — no need to
      // wait for the save (or reopen the drawer) to see the right one.
      setActionVisibility(actionEl.value);
      try {
        await putSchedule(card.id, { action: actionEl.value });
        lastSavedAction = actionEl.value;
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't save action: ${err.message}`, true);
        actionEl.value = lastSavedAction;
        setActionVisibility(lastSavedAction);
      }
    });

    executorEl.addEventListener('change', async () => {
      try {
        await putSchedule(card.id, { executor: executorEl.value });
        lastSavedExecutor = executorEl.value;
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't save executor: ${err.message}`, true);
        executorEl.value = lastSavedExecutor;
      }
    });

    botEl.addEventListener('change', async () => {
      try {
        await putSchedule(card.id, { bot: botEl.value });
        lastSavedBot = botEl.value;
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't save bot: ${err.message}`, true);
        botEl.value = lastSavedBot;
      }
    });

    // When the registry loads, the bot select offers only names the API
    // accepts — the empty "default (primary)" option (distinguishable from
    // the registry's own "primary" row) plus whatever GET
    // /api/scheduler/bots returns. A stored name the loaded registry
    // doesn't include (a bot renamed after the schedule was written) is
    // appended as a selected, flagged-unknown option instead of leaving
    // the select with no matching value. When the fetch itself fails, the
    // stored name isn't known to be invalid — just unconfirmed — so it's
    // kept visible and selected without that flag, and the select is
    // disabled with the reason shown as visible text next to it,
    // mirroring the host dropdown's pattern
    // (web/agents/assignment.js's seedHostOptions/populateHostOptions).
    loadBotCatalog().then(catalog => {
      const stored = card.bot || '';
      if (!catalog) {
        const optionsHtml = ['<option value="">default (primary)</option>'];
        if (stored) {
          optionsHtml.push(`<option value="${escapeHtml(stored)}" selected>${escapeHtml(stored)}</option>`);
        }
        botEl.innerHTML = optionsHtml.join('');
        botEl.value = stored;
        botEl.disabled = true;
        botReasonEl.textContent = 'bot registry unavailable — reopen to retry';
        botReasonEl.hidden = false;
        return;
      }
      const names = catalog.bots || [];
      const known = names.includes(stored);
      const options = ['<option value="">default (primary)</option>'];
      for (const name of names) {
        options.push(`<option value="${escapeHtml(name)}" ${stored === name ? 'selected' : ''}>${escapeHtml(name)}</option>`);
      }
      if (stored && !known) {
        options.push(`<option value="${escapeHtml(stored)}" selected data-unknown="true">${escapeHtml(stored)} (unknown)</option>`);
      }
      botEl.innerHTML = options.join('');
      botEl.value = stored;
      botEl.disabled = false;
      botReasonEl.hidden = true;
      botReasonEl.textContent = '';
    });

    drawerEl.querySelector('[data-action="trigger-now"]').addEventListener('click', async () => {
      try {
        const r = await fetch(`/api/scheduler/${encodeURIComponent(card.id)}/trigger`, { method: 'POST' });
        if (!r.ok) {
          const text = await r.text();
          let msg = text;
          try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
          throw new Error(msg || `HTTP ${r.status}`);
        }
        showToast('Triggered.', false);
        await fetchBoard();
        // fetchBoard's own drawer refresh (updateOpenDrawer) skips
        // rebuilding while the drawer holds focus — and the button that
        // was just clicked still does — so rebuild explicitly here,
        // exactly like the task drawer's assignee select does above,
        // rather than leaving a stale last-run/preview showing.
        const fresh = findCard(card.id);
        if (fresh) { renderDrawer(fresh); openCardSnapshot = fresh; }
      } catch (err) {
        showToast(`Trigger failed: ${err.message}`, true);
      }
    });
  }

  // Every non-terminal descendant (via `parent_session_id`) of a card's
  // linked session, for Kill's cascade-preview modal — the same
  // `descendantsOf` the Graph tab's side panel uses, over the same
  // `/api/agents/snapshot` every session (not just card-linked ones,
  // including subagents that never get their own card) lives in. Fetched
  // on demand rather than polled continuously: the drawer only ever needs
  // this the moment Kill is clicked. Rejects (rather than resolving with
  // an empty list) on a failed fetch — `openKillModal`
  // (web/agents/session_actions.js) tells that apart from a genuine "no
  // descendants" and discloses it instead of confirming a possible
  // cascade the operator was never shown.
  async function fetchDescendantsForKill(session) {
    if (!session) return [];
    const r = await fetch('/api/agents/snapshot');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const snap = await r.json();
    return descendantsOf(snap.sessions || [], session);
  }

  // The drawer's action row — Open, Go To, Resume, Kill, Answer, Accept, Reject,
  // Reassign, Mark Done, Cancel, Delete.
  // Which of these apply and whether each is
  // enabled or disabled-with-a-reason is decided once, by
  // session_actions.js's `decideActions`, and rendered by its
  // `renderActionRow` — the exact same function the Graph tab's side panel
  // uses for its own header, so the two surfaces can't disagree about a
  // shared session. Go To/Resume/Kill/Answer are built into
  // `renderActionRow` itself (it owns Kill's cascade-preview modal,
  // Resume's host select, and Go To's "Locating…" state); Open, Accept,
  // Reject, Reassign, Mark Done, Cancel, and Delete come from ./card_actions.js, shared with a
  // card-linked Graph tab side panel — Cancel and Delete are overridden
  // below with the extra drawer-specific bookkeeping (closing/rebuilding
  // this drawer) that a bare handoff to `fetchBoard` doesn't cover.
  function renderDrawerActions(card) {
    const actionsEl = drawerEl.querySelector('[data-field="actions"]');
    if (!actionsEl) return;
    const cardHandlers = cardActionHandlers(card, {
      onChanged: fetchBoard,
      onAccepted: closeDrawer,
      onMutationOpened: pauseTagPickerWrites,
      onMutationCancelled: rearmTagPickerWrites,
      onMutationConfirmed: pauseTagPickerWrites,
      onMutationFailed: rearmTagPickerWrites,
    });
    const modalActions = new Set(['reject', 'reassign', 'delete']);
    const guardedCardHandlers = Object.fromEntries(
      Object.entries(cardHandlers).map(([name, handler]) => [name, (...args) => (
        modalActions.has(name)
          ? handler(...args)
          : runCardAction(() => handler(...args))
      )]),
    );
    renderActionRow(actionsEl, {
      session: card.session || null,
      card,
      getDescendants: fetchDescendantsForKill,
      onChange: fetchBoard,
      handlers: {
        // The embedded session panel (`renderDrawerSession`, below) owns
        // the actual label-edit UI — its own `.label` click already
        // starts the same edit; this just gives the drawer's own action
        // row a working button for it too.
        rename: () => { if (panel) panel.startRename(); },
        ...guardedCardHandlers,
        cancel: () => runCardAction(() => cancelCard(card, async () => {
          // Tear the session panel down through its own cleanup path right
          // here, rather than leaving it to whichever render call below
          // happens to touch the session-panel container next — a
          // deferred teardown aborts a summary/stream request that's still
          // legitimately in flight, which shows up as a failed request
          // even though nothing actually went wrong.
          if (panel) { panel.close(); panel = null; }
          await fetchBoard();
          // fetchBoard()'s own updateOpenDrawer skips the rebuild while
          // this button (inside the drawer) still holds focus after the
          // click — the same staleness the assignee handler above already
          // works around. Without this, the drawer keeps showing a stale
          // Open button for a card that just moved to Done, and clicking
          // it 409s.
          const fresh = findCard(card.id);
          if (fresh) { renderDrawer(fresh); openCardSnapshot = fresh; }
        })),
        delete: () => openDeleteCardModal(card, {
          findCard,
          onMutationOpened: pauseTagPickerWrites,
          onMutationCancelled: rearmTagPickerWrites,
          onMutationConfirmed: pauseTagPickerWrites,
          onMutationFailed: rearmTagPickerWrites,
          onDeleted: async () => { closeDrawer(); await fetchBoard(); },
        }),
      },
    });
  }

  function renderDrawerSession(card) {
    const sessionWrap = drawerEl.querySelector('[data-field="session-panel"]');
    if (!sessionWrap) return;
    if (panel) { panel.close(); panel = null; }
    if (!card.session) {
      sessionWrap.innerHTML = '<div class="panel-empty">No linked session yet.</div>';
      return;
    }
    // `showActions: false` — the drawer's own action row (built by
    // `renderDrawerActions`, above) already covers Go To/Resume/Kill for
    // this same session; this embedded panel renders only the session
    // header, transcript, and summary.
    panel = new SessionPanel({ container: sessionWrap, showActions: false });
    panel.open(card.session);
  }

  // A change that arrived while the drawer had focus is deferred by
  // updateOpenDrawer's `!focused` check — flush it as soon as the operator
  // leaves the field, using the latest board already applied by applyBoard
  // (#850 round-2 finding 4).
  if (drawerEl) {
    drawerEl.addEventListener('focusout', (e) => {
      // focusout fires before focus lands on the next element, so a move
      // WITHIN the drawer (e.g. Tab between fields, or a mousedown on an
      // action button before its mouseup) still sees `activeElement` as
      // <body> for an instant. relatedTarget is the element receiving
      // focus — populated for an intra-drawer move, null when blur() sends
      // focus to <body> — so only flush the full drawer update once focus
      // has actually left the drawer.
      if (e.relatedTarget && drawerEl.contains(e.relatedTarget)) return;
      if (!openCardId) return;
      const fresh = findCard(openCardId);
      if (fresh) updateOpenDrawer(fresh);
    });
  }

  // ------------------------------------------------------------------
  // Lane filter dropdown — checkboxes + All/Clear toggles + outside-click
  // close (mirrors web/crm.html's people-filter-* dropdown pattern).
  // ------------------------------------------------------------------

  function laneFilterCheckboxes() {
    return laneFilterOptions ? [...laneFilterOptions.querySelectorAll('input[type="checkbox"]')] : [];
  }

  function updateLaneFilterLabel() {
    if (!laneFilterLabel) return;
    if (visibleLanes.size === LANES.length) laneFilterLabel.textContent = 'All lanes';
    else if (visibleLanes.size === 0) laneFilterLabel.textContent = 'No lanes';
    else laneFilterLabel.textContent = `${visibleLanes.size} lane${visibleLanes.size === 1 ? '' : 's'}`;
  }

  // Reconciles `visibleLanes`, the checkbox dropdown, and the label against
  // the shared `lanes` filter — called both by the checkbox listeners'
  // round trip through `setFilter` and by any OTHER origin of a `lanes`
  // change (the graph tab's own lane select, a storage restore, Clear).
  function syncLaneFilterUI(laneIds) {
    visibleLanes = new Set(laneIds);
    laneFilterCheckboxes().forEach(cb => { cb.checked = visibleLanes.has(cb.value); });
    updateLaneFilterLabel();
    updateQuickDropTargets();
  }

  // The lane selection is the shared `lanes` filter (linking.js) — this
  // just forwards to it; `syncSharedFilterControls` (below, in "Wire
  // filters + boot") is what actually updates `visibleLanes`, the
  // checkboxes, and the label once the store notifies, so a lane change
  // made from the graph tab (or restored from storage) reaches this UI the
  // same way a change made here does.
  function applyLaneSelection(ids) {
    setFilter('lanes', ids);
  }

  // Reveals `laneId` in the filter if it's currently hidden — used after
  // creating a card straight into a lane the filter was hiding, so the new
  // card doesn't vanish with no feedback. A no-op when the lane is already
  // visible.
  function ensureLaneVisible(laneId) {
    if (visibleLanes.has(laneId)) return;
    applyLaneSelection([...visibleLanes, laneId]);
  }

  function renderLaneFilterCheckboxes() {
    if (!laneFilterOptions) return;
    for (const lane of LANES) {
      const label = document.createElement('label');
      label.className = 'board-lane-filter-option';
      label.innerHTML = `<input type="checkbox" value="${lane.id}" ${visibleLanes.has(lane.id) ? 'checked' : ''} /> ${escapeHtml(lane.label)}`;
      label.querySelector('input').addEventListener('change', () => {
        applyLaneSelection(laneFilterCheckboxes().filter(cb => cb.checked).map(cb => cb.value));
      });
      laneFilterOptions.appendChild(label);
    }
  }

  if (laneFilterBtn) {
    laneFilterBtn.addEventListener('click', () => {
      if (laneFilterOptions) laneFilterOptions.classList.toggle('show');
    });
  }
  if (laneFilterAllBtn) {
    laneFilterAllBtn.addEventListener('click', () => {
      laneFilterCheckboxes().forEach(cb => { cb.checked = true; });
      applyLaneSelection(LANES.map(l => l.id));
    });
  }
  if (laneFilterClearBtn) {
    laneFilterClearBtn.addEventListener('click', () => {
      laneFilterCheckboxes().forEach(cb => { cb.checked = DEFAULT_VISIBLE_LANE_IDS.includes(cb.value); });
      applyLaneSelection(DEFAULT_VISIBLE_LANE_IDS);
    });
  }
  document.addEventListener('click', (e) => {
    if (laneFilterDropdown && !laneFilterDropdown.contains(e.target) && laneFilterOptions) {
      laneFilterOptions.classList.remove('show');
    }
  });

  if (filterToggleBtn && filterControlsEl) {
    filterToggleBtn.addEventListener('click', () => {
      const open = document.getElementById('board-filters').classList.toggle('filters-open');
      filterToggleBtn.setAttribute('aria-expanded', String(open));
    });
  }

  renderLaneFilterCheckboxes();
  updateLaneFilterLabel();
  renderAssigneeDrops();
  updateQuickDropTargets();
  if (doneDropEl) {
    doneDropEl.addEventListener('click', () => {
      const card = quickActionCardId && findCard(quickActionCardId);
      if (!card) {
        setDropStatus('Focus a task card first, then activate Done.', true);
        return;
      }
      onCardDropped(card.id, 'done');
    });
  }

  // ------------------------------------------------------------------
  // Wire filters + boot
  // ------------------------------------------------------------------

  // "Include cancelled" and sorting stay board-local.
  [includeDoneEl].filter(Boolean).forEach(el => {
    const evt = (el.tagName === 'SELECT' || el.type === 'checkbox') ? 'change' : 'input';
    el.addEventListener(evt, () => { updateFilterSummary(getFilters()); render(); });
  });

  // Search/assignee/host/engine/tag/recency — shared with the graph
  // tab's own filter bar via linking.js; each control pushes to the store,
  // and `syncSharedFilterControls` (below) reconciles every control
  // (including these) against whatever the store ends up holding, no
  // matter which control or tab caused it.
  if (searchEl) searchEl.addEventListener('input', () => setFilter('search', searchEl.value));
  if (assigneeFilterEl) assigneeFilterEl.addEventListener('change', () => setFilter('assignee', assigneeFilterEl.value));
  if (hostFilterEl) hostFilterEl.addEventListener('change', () => setFilter('host', hostFilterEl.value));
  if (engineFilterEl) engineFilterEl.addEventListener('change', () => setFilter('engine', engineFilterEl.value));
  if (tagFilterEl) tagFilterEl.addEventListener('input', () => setFilter('tag', tagFilterEl.value));
  if (recencyFilterEl) recencyFilterEl.addEventListener('change', () => setFilter('recency', recencyFilterEl.value));
  if (sortFilterEl) {
    sortFilterEl.addEventListener('change', () => {
      sortMode = SORT_OPTIONS.has(sortFilterEl.value) ? sortFilterEl.value : DEFAULT_SORT;
      saveSortSelection(sortMode);
      updateFilterSummary(getFilters());
      render();
    });
  }
  if (filterClearBtn) filterClearBtn.addEventListener('click', () => {
    resetFilters();
    if (includeDoneEl) includeDoneEl.checked = false;
    sortMode = DEFAULT_SORT;
    if (sortFilterEl) sortFilterEl.value = DEFAULT_SORT;
    saveSortSelection(DEFAULT_SORT);
    updateFilterSummary(getFilters());
    render();
  });

  function updateFilterSummary(state) {
    if (!filterSummaryEl) return;
    const active = [];
    if (state.search) active.push(`search “${state.search}”`);
    if (state.assignee !== 'all') active.push(state.assignee);
    if (state.host !== 'all') active.push(`host ${state.host}`);
    if (state.engine !== 'all') active.push(`engine ${state.engine}`);
    if (state.tag) active.push(`#${state.tag.replace(/^#/, '')}`);
    if (state.recency != null && state.recency !== 'all') active.push('recent');
    if (visibleLanes.size !== DEFAULT_VISIBLE_LANE_IDS.length
        || DEFAULT_VISIBLE_LANE_IDS.some(id => !visibleLanes.has(id))) active.push('lanes');
    if (includeDoneEl && includeDoneEl.checked) active.push('cancelled');
    if (sortMode !== DEFAULT_SORT) active.push('sorted');
    filterSummaryEl.textContent = active.length ? `${active.length} active` : 'default';
    filterToggleBtn?.setAttribute('aria-label', active.length
      ? `Filters: ${active.join(', ')}` : 'Filters: default');
  }

  function syncSharedFilterControls(state) {
    // Rebuild (and, if needed, inject) the host option list against the
    // now-current shared state BEFORE assigning `hostFilterEl.value` below —
    // otherwise a host that isn't yet a real `<option>` silently coerces the
    // assignment to `""`, same as any other absent-value `<select>` write.
    updateFilterOptions();
    if (searchEl && document.activeElement !== searchEl && searchEl.value !== state.search) {
      searchEl.value = state.search;
    }
    if (assigneeFilterEl && assigneeFilterEl.value !== state.assignee) assigneeFilterEl.value = state.assignee;
    if (hostFilterEl && hostFilterEl.value !== state.host) hostFilterEl.value = state.host;
    if (engineFilterEl && engineFilterEl.value !== state.engine) engineFilterEl.value = state.engine;
    if (tagFilterEl && document.activeElement !== tagFilterEl && tagFilterEl.value !== state.tag) {
      tagFilterEl.value = state.tag;
    }
    // `null` (the shared default — "the operator has never set it") reads
    // as "all time" here, the board's own longstanding default; only a
    // concrete value is ever written back to `localStorage`.
    const recencyDisplay = state.recency == null ? 'all' : state.recency;
    if (recencyFilterEl && recencyFilterEl.value !== recencyDisplay) recencyFilterEl.value = recencyDisplay;
    syncLaneFilterUI(state.lanes);
    updateFilterSummary(state);
    render();
  }
  subscribeFilters(syncSharedFilterControls);
  syncSharedFilterControls(getFilters());

  fetchBoard();
  connectStream();

  // The Graph tab's side panel (web/agents/graph.js) reuses this live
  // state, rather than issuing its own `/api/agents/board` fetch, to find
  // the card linked to a session — the same lookup the drawer itself uses
  // (`findCard`, `allCards()`), so the two surfaces can never derive
  // different `card.lane`/`card.policy` for the same session. `findCard`
  // is exposed the same way, by card id, so a card-linked Graph tab panel
  // can re-resolve the freshest copy of its card at Delete-confirm time
  // exactly the way the Board drawer's own `findCard` wiring
  // (`renderDrawerActions`, above) does.
  return {
    getCardForSession(sessionId) {
      if (!sessionId) return null;
      return allCards().find(c => c.session && c.session.session_id === sessionId) || null;
    },
    findCard,
    refresh: fetchBoard,
  };
}
