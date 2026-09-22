// web/agents/board.js
//
// The Kanban board — the primary /agents view. Backed by the vault
// task store via GET/PUT /api/agents/board*, with a card drawer that reuses
// the shared SessionPanel (./panel.js) for the linked session's transcript,
// exactly like the Graph tab's side panel does. The drawer's own action
// row (Open, Go To, Resume, Kill, Answer, Accept, Reject, Reassign, Mark
// Done, Snooze, Unsnooze, Cancel, Delete)
// is rendered by session_actions.js's `renderActionRow` — the same
// function the Graph tab's side panel uses for its own header — so the
// embedded SessionPanel here is constructed with `showActions: false`
// (see `renderDrawerSession`) to avoid rendering the same session's
// Kill/Resume/Go To twice.
//
// Sorting is client-only — drag only ever changes which lane a card is in;
// no sort choice rewrites the vault's file order.

import {
  TERMINAL, routingLabel, escapeHtml, escapeAttr, showToast, showUndoableToast, SessionPanel,
} from './panel.js';
import { renderActionRow } from './session_actions.js';
import { descendantsOf } from './graph_encoding.js';
import {
  acceptCard, cardActionHandlers, cancelCard, deleteCard, openDeleteCardModal, snoozeCard,
  undoAcceptedCard,
} from './card_actions.js';
import { renderAssignmentPickers } from './assignment.js';
import { SCHEDULE_ACTIONS, renderScheduleActionSections, actionInputsSatisfied } from './schedule_sections.js';
import { LANES, laneColor } from './lanes.js';
import { routingFilterValue } from './graph_encoding.js';
import { POINTER_SLOP, pointerCanDrag, pointerIsActive, shouldCancelPointerGesture } from './board_gesture.js';
import { assignChipHues } from './chip_colors.js';
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

// Card fields the drawer renders as editable inputs — read to decide
// whether an SSE tick needs to rebuild the drawer at all.
const DRAWER_EDITABLE_FIELDS = [
  'title', 'notes', 'tags', 'assignee', 'lane',
  // Hierarchy data is derived server-side and can change after an external
  // vault edit or a child mutation while this drawer is open.
  'parent_id', 'parent_title', 'is_project', 'child_count',
  'hierarchy_valid', 'hierarchy_error', 'project', 'fields',
  // Read-only, but a background refresh can change a PR's merge status
  // after the drawer first opened — without watching it here, an open
  // drawer would show a stale status until the operator closed and
  // reopened it.
  'outcome',
  // The model/effort/host pickers write here. Without it a frame whose only
  // change is a picker value is read as "nothing changed", so a drawer
  // showing a stale picker has no later frame that can converge it.
  'fields',
  // Scheduled-card fields, editable in the drawer.
  'name', 'message_content', 'enabled',
  // Full schedule editing — trigger type, timing, timezone, action,
  // executor, and delivery bot — all through PUT /api/scheduler/{id}.
  'schedule_type', 'schedule_value', 'timezone', 'action', 'executor', 'bot',
  // Per-action inputs — an endpoint action's call config and an
  // agent action's execution context, all through the same PUT.
  'endpoint_config', 'persona_id', 'model_id', 'effort', 'host', 'working_dir',
  'budget_dollars', 'wall_seconds',
];

// Lane filter — multi-select checkbox dropdown. Hidden lanes are
// removed from the grid entirely (not just emptied), so the remaining
// .board-lane columns (flex: 1 1 260px, see web/agents.html CSS) widen to
// fill the space. The selection itself is the shared `lanes` filter
// (web/agents/linking.js) — persistence, migration, and validation of a
// stored id list all live there now; `visibleLanes` below is a local mirror
// kept in sync via `subscribeFilters`.
const DEFAULT_VISIBLE_LANE_IDS = LANES.filter(l => l.id !== 'done' && l.id !== 'snoozed').map(l => l.id);
// plan_lane_move (api/services/agent_board.py) rejects `review`,
// `scheduled`, and `snoozed` with "cannot be set directly" — no per-lane
// "+" button for any of the three, all three are excluded from the
// new-card composer's lane select, and `canDropCard`/`onCardDropped`
// (below) refuse a drop targeting one before it ever reaches the server.
// Scheduled gets its own "+" below (SCHEDULED_LANE_ID) that opens the
// schedule composer instead; Review still has none, since a card only
// reaches Review through the worker's own tags; snoozing only ever happens
// through the drawer's Snooze picker.
const DIRECT_LANE_IDS = new Set(LANES.filter(l => l.id !== 'review' && l.id !== 'scheduled' && l.id !== 'snoozed').map(l => l.id));
const SCHEDULED_LANE_ID = 'scheduled';

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
  const projectFilterEl = document.getElementById('board-filter-project');
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
  const dropTrayEl = document.getElementById('board-drop-tray');
  const bulkBarEl = document.getElementById('board-bulk-bar');
  const bulkCountEl = document.getElementById('board-bulk-count');
  const bulkTagBtn = document.getElementById('board-bulk-tag');
  const bulkTagPopover = document.getElementById('board-bulk-tag-popover');
  const bulkAssignBtn = document.getElementById('board-bulk-assign');
  const bulkAssignPopover = document.getElementById('board-bulk-assign-popover');
  const bulkDoneBtn = document.getElementById('board-bulk-done');
  const bulkDeleteBtn = document.getElementById('board-bulk-delete');
  const bulkClearBtn = document.getElementById('board-bulk-clear');

  let board = { lanes: Object.fromEntries(LANES.map(l => [l.id, []])) };
  // Assignee/tag pill colors — assignees first (their fixed order), then
  // every other distinct tag currently on the board, alphabetically.
  // Recomputed by `render()` on every pass; the drawer's tag-chip picker
  // (`mountTagPicker`'s `renderChips`) recomputes its own copy on demand
  // since it can render without a board `render()` having just run.
  let chipHueMap = new Map();
  let visibleLanes = new Set(getFilters().lanes);
  let sortMode = loadSortSelection();
  let projectFilter = 'all';
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

  // Multi-select — a Set of task-card ids, not a DOM class: `render()`
  // rebuilds every card node (an SSE tick, a filter change, a lane toggle),
  // so the selection has to be re-applied at render time the same way
  // `revealedCardId` is, rather than living on a node that gets discarded.
  // Only task cards are ever added — scheduled cards are never selectable.
  let selectedCardIds = new Set();

  function clearSelection() {
    if (selectedCardIds.size === 0) return;
    selectedCardIds.clear();
    closeBulkPopovers();
    render();
  }

  function toggleCardSelection(cardId) {
    if (selectedCardIds.has(cardId)) selectedCardIds.delete(cardId);
    else selectedCardIds.add(cardId);
    render();
  }

  // Drops any selected id absent from the board — called from
  // `applyBoard` before `render()` so a card that vanished on a live update
  // (deleted elsewhere, or moved out from under a stale selection) drops out
  // of the count rather than being fanned out over on the next bulk action.
  function pruneSelection() {
    if (selectedCardIds.size === 0) return;
    const present = new Set(allCards().map(c => c.id));
    for (const id of [...selectedCardIds]) {
      if (!present.has(id)) selectedCardIds.delete(id);
    }
  }

  function selectedTaskCards() {
    return allCards().filter(c => c.kind === 'task' && selectedCardIds.has(c.id));
  }

  function closeBulkPopovers() {
    if (bulkTagPopover) bulkTagPopover.hidden = true;
    if (bulkAssignPopover) bulkAssignPopover.hidden = true;
  }

  // Renders the bottom bulk-action bar and swaps it in for the assignee
  // tray while at least one card is selected — the tray comes back exactly
  // when the selection empties. Called from `render()` so the count and
  // visibility always match `selectedCardIds` after any rebuild.
  function renderBulkBar() {
    if (!bulkBarEl) return;
    const n = selectedCardIds.size;
    const active = n > 0;
    bulkBarEl.hidden = !active;
    if (dropTrayEl) dropTrayEl.hidden = active;
    if (!active) {
      closeBulkPopovers();
      return;
    }
    if (bulkCountEl) bulkCountEl.textContent = `${n} selected`;
  }

  // Summarizes a fan-out's per-card outcomes into the one toast a bulk
  // action shows — never one toast per card. `results` is
  // `[{card, ok, reason}]`; a refused/failed card's `reason` is the
  // server's own `detail` (or a transport error message), never
  // re-derived client-side.
  function reportBulkOutcome(verb, results) {
    const succeeded = results.filter(r => r.ok);
    const failed = results.filter(r => !r.ok);
    if (failed.length === 0) {
      showToast(`${verb} ${succeeded.length} of ${results.length}.`, false);
      return;
    }
    const refusals = failed.map(r => `${r.card.title || r.card.id}: ${r.reason}`).join('; ');
    showToast(`${verb} ${succeeded.length} of ${results.length} — refused: ${refusals}`, true);
  }

  // Runs `action(card)` for every card in `cards`, at most `limit` in
  // flight at once, and resolves with one `{card, ok, reason}` per card —
  // a rejected `action` is caught here so one card's refusal never stops
  // the rest of the batch from running.
  async function fanOut(cards, action, limit = 4) {
    const results = new Array(cards.length);
    let next = 0;
    async function worker() {
      while (next < cards.length) {
        const index = next++;
        const card = cards[index];
        try {
          await action(card);
          results[index] = { card, ok: true };
        } catch (err) {
          results[index] = { card, ok: false, reason: (err && err.message) || String(err) };
        }
      }
    }
    const workers = Array.from({ length: Math.min(limit, cards.length) }, worker);
    await Promise.all(workers);
    return results;
  }

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
    pruneSelection();
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
  // call) on every tick. So: refresh the linked session in
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
    // "Kill" survives a session that already exited.
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
      // last card the drawer actually shows.
      openCardSnapshot = fresh;
    }
  }

  // Resolves `true` when this call actually refreshed the board and `false`
  // when it did not, so a caller that chains work on can tell a current board
  // from a stale one. A caller that ignores the result is unaffected, and the
  // failure still reaches the operator through the connection-state label
  // alone — this adds a signal for callers, it does not change what is shown.
  function fetchBoard() {
    return fetch('/api/agents/board')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(applyBoard)
      .then(() => true)
      .catch(err => {
        if (connStateEl) connStateEl.textContent = 'failed: ' + err;
        return false;
      });
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

    if (projectFilter === 'projects' && !card.is_project) return false;
    if (projectFilter === 'children' && !card.parent_id) return false;
    if (projectFilter === 'ordinary' && (card.is_project || card.parent_id)) return false;

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
      if (!stamp) return false;
      const ageSec = (Date.now() - new Date(stamp).getTime()) / 1000;
      if (ageSec > recencySec) return false;
    }

    // Only cancelled task cards are behind this filter — the Done lane
    // itself (finished tasks, retired/fired schedules) always stays visible.
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

  // Mirrors api/services/agent_board.py's natural_lane(status, tags) — the
  // lane a card would derive to ignoring any snooze. Used only to compute
  // Undo's restore target for a card captured mid-snooze, since the
  // server's own `snoozed` lane can never be set directly (plan_lane_move
  // refuses it the same way review/scheduled are refused). Deliberately
  // omits the machine-wait-tag branch production's natural_lane has: a
  // card this function is ever called on was successfully snoozed, which
  // the drawer only ever offers from Unassigned, Assigned, Human queue, or
  // Review (session_actions.js's SNOOZE_ELIGIBLE_LANES), so its natural
  // lane can never actually be the machine-wait flavor of In progress.
  function naturalLaneFor(card) {
    const tags = new Set((card.tags || []).map(t => String(t).replace(/^#+/, '').toLowerCase()));
    const status = (card.status || 'todo').toLowerCase();
    if (tags.has('agent-completed') && !tags.has('accepted')) return 'review';
    if (tags.has('agent-blocked') || tags.has('human') || status === 'blocked') return 'human_queue';
    if (status === 'in_progress' || tags.has('agent-running')) return 'in_progress';
    if (status === 'done' || status === 'cancelled') return 'done';
    return card.assignee ? 'assigned' : 'unassigned';
  }

  // The shared Undo restore target for both the lane-drop (onCardDropped)
  // and the tray-assignee-drop (assignCardTo) paths. The invariant: every
  // lane-move Undo restores the card's exact pre-move status and tags,
  // through `restoreCardSnapshot` below — never a replay of the lane
  // endpoint with only a target lane, which plan_lane_move's branches
  // cannot do safely in either direction. Moving INTO Human queue forces
  // `status="blocked"` without touching tags; moving INTO Done, In
  // progress, Assigned, or Unassigned touches only tags or only status,
  // never both — so replaying any of them to UNDO a move that changed the
  // other half leaves the card wrong (stuck at the wrong status, or
  // missing/carrying the wrong tags).
  //
  // Two dedicated exceptions keep their own paths, both because
  // `plan_lane_move` refuses them as a direct target in the first place:
  // Review (an accept-by-drag-to-Done is undone via the dedicated
  // undo-accept transition, never a status/tags write — the only way a
  // snoozed Review card's Undo is ever offered is after a drag to Done
  // added `accepted`, since `agent-completed` alone already wins the
  // derived lane back to Review the instant it's written anywhere else,
  // which short-circuits the undoable toast entirely, without this
  // function ever being called — see onCardDropped's "landed elsewhere"
  // check) and Snoozed (`card.lane` itself was `snoozed`, so callers
  // resolve the card's NATURAL lane first via `naturalLaneFor` rather than
  // ever passing `snoozed` through here as `lane`). `snoozedUntil`, when
  // given and still in the future, re-applies the snooze once the
  // restore (or acceptance) completes.
  function undoToLane(cardId, lane, snoozedUntil, snapshot) {
    const restore = lane === 'review'
      ? undoAcceptedCard(cardId, () => {})
      : restoreCardSnapshot(cardId, snapshot);
    return restore
      .then(() => {
        const stillFuture = snoozedUntil && new Date(snoozedUntil).getTime() > Date.now();
        return stillFuture ? snoozeCard({ id: cardId }, snoozedUntil, () => {}) : null;
      })
      .then(() => fetchBoard());
  }

  // Writes the pre-drag status/tags back verbatim via PUT /api/tasks/{id} —
  // the same endpoint the drawer's own edits use, not the board's tags-only
  // endpoint (which refuses any payload naming a protected tag, and an
  // assignee/lifecycle tag is exactly what a full prior-state snapshot can
  // carry). That endpoint's own assignee/claim-tag guard compares the
  // snapshot against the card's CURRENT tags and refuses the write (leaving
  // the card exactly as the server currently has it, with a toast) rather
  // than silently overwriting a reassignment or claim the worker made while
  // the card sat outside Human queue — see api/routes/tasks.py's
  // `update_task`. Rethrows on failure, matching moveCard: undoToLane's
  // caller (showUndoableToast) only restores the toast's "Undo" label and
  // keeps the toast up on a rejection, so a swallowed error here would
  // report a refused restore as a success and, for a card that was
  // snoozed, still go on to re-snooze a card the restore never actually
  // touched.
  function restoreCardSnapshot(cardId, snapshot) {
    if (!snapshot) return Promise.resolve();
    return putTask(cardId, { status: snapshot.status, tags: snapshot.tags })
      .catch((err) => {
        showToast("Couldn't undo — the card may have changed since.", true);
        throw err;
      });
  }

  // Every way to assign a card — dragging an assignee onto a card, or
  // dragging a card onto an assignee — lands here, so neither path can
  // report or offer undo differently from the other.
  function assignCardTo(card, assignee) {
    if (!card || card.kind !== 'task') return;
    const reason = assignmentPolicyReason(card);
    if (reason) {
      setDropStatus(reason, true);
      showToast(reason, true);
      return;
    }
    // Captured before the write so Undo restores where the card actually
    // was, not wherever the board has drifted to by the time it is
    // clicked. A card dragged while snoozed resolves its Undo target to
    // the NATURAL lane it was snoozed from, plus the wake-up time, so
    // Undo restores the snooze too instead of trying (and failing) to
    // move the card directly into the `snoozed` lane.
    const priorLane = card.lane;
    const priorStatus = card.status;
    const priorTags = (card.tags || []).slice();
    const priorSnoozedUntil = priorLane === 'snoozed' ? (card.fields && card.fields.snoozed_until) : null;
    const priorNaturalLane = priorLane === 'snoozed' ? naturalLaneFor(card) : priorLane;
    const title = card.title || card.id;
    setDropStatus(`Assigning ${assignee}…`);
    // Keep this on the same lane endpoint and request shape as the drawer's
    // assignee select. The server remains authoritative for claimed cards
    // and for cards whose derived lane cannot change with the tag update.
    moveCard(card.id, 'assigned', assignee)
      .then((data) => {
        setDropStatus(`Assigned to ${assignee}.`);
        if (data && data.lane && data.lane !== 'assigned') return;
        showUndoableToast(`Assigned "${title}" to ${assignee}.`, () => (
          undoToLane(card.id, priorNaturalLane, priorSnoozedUntil, {
            status: priorStatus, tags: priorTags,
          })
        ));
      })
      .catch(() => setDropStatus('Assignment refused.', true));
  }

  function assignAssigneeToCard(cardId, assignee) {
    assignCardTo(findCard(cardId), assignee);
  }

  // The tray buttons mirror `#board-filter-assignee` (same shared
  // `assignee` filter, same value set) — clicking one filters the board to
  // that assignee, clicking it again clears the filter back to `all`.
  // `syncSharedFilterControls` re-renders this whenever the shared filter
  // changes from any origin (the dropdown, Clear filters, a graph-tab
  // change), so the tray and dropdown always agree on the selection.
  function renderAssigneeDrops() {
    if (!assigneeDropsEl) return;
    const current = getFilters().assignee;
    assigneeDropsEl.parentElement?.style.setProperty(
      '--board-tray-button-count', String(ASSIGNEES.length + 1),
    );
    assigneeDropsEl.innerHTML = ASSIGNEES.map(assignee => `
      <button type="button" class="board-drop-target board-assignee-drop${current === assignee ? ' selected' : ''}"
              data-assignee="${assignee}" aria-pressed="${current === assignee}"
              title="${escapeAttr(assignee)}" aria-label="Filter board to ${escapeAttr(assignee)}">
        ${assignee}
      </button>
    `).join('');
    assigneeDropsEl.querySelectorAll('.board-assignee-drop').forEach(button => {
      button.addEventListener('click', () => {
        if (consumeClickSuppression('tray', `assignee:${button.dataset.assignee}`)) {
          return;
        }
        const clicked = button.dataset.assignee;
        setFilter('assignee', getFilters().assignee === clicked ? 'all' : clicked);
      });
      button.addEventListener('pointerdown', e => onPointerDown(e, {
        kind: 'assignee', assignee: button.dataset.assignee, sourceEl: button,
      }));
    });
  }

  // The button stays visible in both states — clicking it toggles the Done
  // lane's visibility (the same `lanes` filter the lane multi-select
  // writes), so it needs to convey which state is active rather than
  // disappear in one of them. Dropping a card on it is a separate path
  // (`onDragMove`/`onCardDropped`) that keeps working regardless of this
  // toggle state.
  function updateQuickDropTargets() {
    if (!doneDropEl) return;
    const laneVisible = visibleLanes.has('done');
    doneDropEl.classList.toggle('active', laneVisible);
    doneDropEl.setAttribute('aria-pressed', String(laneVisible));
    doneDropEl.setAttribute('aria-label', laneVisible
      ? 'Done lane shown — click to hide it, or drop a card here to move it to Done'
      : 'Done lane hidden — click to show it, or drop a card here to move it to Done');
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  function projectProgressLabel(project) {
    if (!project) return 'loading summary';
    const total = Number(project.child_count) || 0;
    const resolved = Number(project.resolved_count) || 0;
    return `${resolved}/${total} resolved`;
  }

  // Every distinct tag currently on the board, lowercased, excluding
  // whichever of ASSIGNEES appear as tags too (those render as the
  // assignee chip instead — see the tag loop in `cardChips` below).
  function boardTagNames() {
    const tags = new Set();
    for (const card of allCards()) {
      for (const raw of (card.tags || [])) {
        const lower = String(raw).toLowerCase();
        if (!ASSIGNEES.includes(lower)) tags.add(lower);
      }
    }
    return Array.from(tags).sort();
  }

  function computeChipHueMap() {
    return assignChipHues([...ASSIGNEES, ...boardTagNames()]);
  }

  function chipHueStyle(name) {
    const hue = chipHueMap.get(String(name).toLowerCase());
    return hue != null ? ` style="--chip-hue:${hue}"` : '';
  }

  function cardChips(card) {
    const chips = [];
    if (card.is_project) {
      chips.push(`<span class="board-chip board-chip-project">project · ${escapeHtml(projectProgressLabel(card.project))}</span>`);
    } else if (card.parent_id) {
      chips.push(`<button type="button" class="board-chip board-chip-parent" data-parent-id="${escapeAttr(card.parent_id)}" title="Open project">↖ ${escapeHtml(card.parent_title || 'project')}</button>`);
    }
    // Snoozed cards show their wake-up time first — the one thing that
    // actually explains why the card is sitting here instead of its
    // natural lane.
    if (card.lane === 'snoozed' && card.fields && card.fields.snoozed_until) {
      const wake = formatWakeTime(card.fields.snoozed_until);
      if (wake) {
        chips.push(`<span class="board-chip board-chip-snoozed" title="wakes ${escapeAttr(wake.exact)}">⏰ ${escapeHtml(wake.label)}</span>`);
      }
    }
    if (card.assignee) chips.push(`<span class="board-chip board-chip-assignee"${chipHueStyle(card.assignee)}>${escapeHtml(card.assignee)}</span>`);
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
    // Clickable — `renderTaskCard` wires each one to toggle the shared
    // `tag` filter to exactly this tag (see `applyTagFilterFrom` below);
    // `active` marks whichever chip(s) equal the currently active filter,
    // using the same case-insensitive, `#`-stripped comparison
    // `cardMatchesFilters` uses, so a second click reads as "un-apply".
    const activeTagFilter = (getFilters().tag || '').trim().toLowerCase().replace(/^#/, '');
    for (const t of (card.tags || [])) {
      const lower = t.toLowerCase();
      if (ASSIGNEES.includes(lower)) continue;  // already shown as the assignee chip
      const active = !!activeTagFilter && lower === activeTagFilter;
      chips.push(`<span class="board-chip board-chip-tag${active ? ' active' : ''}" data-tag="${escapeAttr(t)}"${chipHueStyle(t)}>#${escapeHtml(t)}</span>`);
    }
    return chips.join('');
  }

  // Shared by the tag chip's click handler (`renderTaskCard`) — toggles
  // the board's shared `tag` filter to exactly `tag`, or clears it when
  // `tag` is already the active filter (same normalization as
  // `cardMatchesFilters`'s tag match, above).
  function applyTagFilterFrom(tag) {
    const current = (getFilters().tag || '').trim().toLowerCase().replace(/^#/, '');
    const normalized = String(tag).toLowerCase().replace(/^#/, '');
    setFilter('tag', current === normalized ? '' : tag);
  }

  // A pull request's compact open/merged/closed label for the card-face
  // badge and the drawer's outcome section. `pr.state` is whatever GitHub
  // last reported (or null when the background refresher hasn't reached
  // this url yet) — never fabricated as "open" just because it's unknown.
  function prStateLabel(pr) {
    const state = String(pr.state || '').toUpperCase();
    if (state === 'MERGED') return 'merged';
    if (state === 'OPEN') return 'open';
    if (state === 'CLOSED') return 'closed';
    return pr.stale ? 'checking…' : 'unknown';
  }

  function prStateClass(pr) {
    const state = String(pr.state || '').toUpperCase();
    if (state === 'MERGED') return 'board-pr-badge-merged';
    if (state === 'OPEN') return 'board-pr-badge-open';
    if (state === 'CLOSED') return 'board-pr-badge-closed';
    return 'board-pr-badge-unknown';
  }

  // Compact PR badge shown on a Review-lane card's face — number and
  // open/merged/closed at a glance, no drawer needed. Only the first PR
  // (the common case is zero or one per outcome) — the drawer lists every
  // one.
  function outcomePrBadge(card) {
    const prs = card.outcome && card.outcome.prs;
    if (!prs || !prs.length) return '';
    const pr = prs[0];
    const numberLabel = pr.number ? `#${pr.number}` : 'PR';
    const title = pr.stale ? 'status may be out of date — refreshing in the background' : 'pull request status';
    return `<span class="board-pr-badge ${prStateClass(pr)}" title="${escapeHtml(title)}">${escapeHtml(numberLabel)} · ${escapeHtml(prStateLabel(pr))}</span>`;
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
    if (selectedCardIds.has(card.id)) div.classList.add('board-card-selected');
    const showAccept = card.lane === 'review';
    div.innerHTML = `
      <div class="board-card-title">${live ? '<span class="live-dot" title="live"></span>' : ''}${escapeHtml(card.title || '(untitled)')}</div>
      ${card.pending_question ? `<div class="board-card-question">❓ ${escapeHtml(card.pending_question.question)}</div>` : ''}
      ${showAccept && card.outcome && card.outcome.prs && card.outcome.prs.length ? `<div class="board-card-pr">${outcomePrBadge(card)}</div>` : ''}
      <div class="board-card-chips">${cardChips(card)}</div>
      ${showAccept ? '<button type="button" class="board-card-accept">Accept</button>' : ''}
    `;
    div.addEventListener('click', (e) => {
      if (consumeClickSuppression('card', card.id)) return;
      // A modifier click toggles this card into/out of the selection
      // instead of opening the drawer, and never touches the tray/filter
      // state — a plain click is the only thing that clears a selection or
      // opens the drawer.
      if (e.metaKey || e.ctrlKey) {
        toggleCardSelection(card.id);
        return;
      }
      clearSelection();
      openDrawer(card.id);
    });
    div.addEventListener('keydown', e => {
      if (e.target !== div || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      // Mirrors the plain-click branch above: activating a card always
      // clears any selection before opening its drawer, keyboard or mouse.
      clearSelection();
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
    const parentChip = div.querySelector('.board-chip-parent');
    if (parentChip) {
      parentChip.addEventListener('click', (e) => {
        e.stopPropagation();
        openDrawer(parentChip.dataset.parentId);
      });
    }
    div.querySelectorAll('.board-chip-tag').forEach(tagChip => {
      tagChip.addEventListener('click', (e) => {
        // A modifier click is a selection toggle everywhere on the card
        // (see the card's own click handler above) — let it bubble there
        // untouched instead of applying a tag filter.
        if (e.metaKey || e.ctrlKey) return;
        e.stopPropagation();
        applyTagFilterFrom(tagChip.dataset.tag);
      });
    });
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

  // The action chip summarizes what firing this schedule actually does —
  // an endpoint's method and path (path truncated at 40 characters with
  // the full path kept as the chip's title, so a long route is still
  // fully available on hover) or an agent action's executor (falling back
  // to "default" for the empty-executor "agent worker's own default
  // route" case, matching the drawer's own Executor select label).
  function scheduleActionChipText(card) {
    if (card.action === 'endpoint') {
      const cfg = card.endpoint_config || {};
      const method = String(cfg.method || 'GET').toUpperCase();
      const path = cfg.endpoint || '';
      const truncated = path.length > 40 ? `${path.slice(0, 40)}…` : path;
      return { text: `endpoint: ${method} ${truncated}`, title: path };
    }
    if (card.action === 'agent') {
      return { text: `agent: ${card.executor || 'default'}`, title: '' };
    }
    return { text: card.action || 'notify', title: '' };
  }

  function renderScheduleCard(card) {
    const div = document.createElement('div');
    div.className = 'board-card board-card-schedule';
    div.dataset.cardId = card.id;
    div.dataset.lane = card.lane;
    if (card.id === revealedCardId) div.classList.add('reveal-highlight');
    const isManual = card.schedule_type === 'manual';
    const nextFire = card.next_fire_at ? new Date(card.next_fire_at).toLocaleString() : '—';
    const actionChip = scheduleActionChipText(card);
    div.innerHTML = `
      <div class="board-card-title">${escapeHtml(card.name || '(schedule)')}</div>
      <div class="board-card-chips">
        <span class="board-chip" title="${escapeHtml(actionChip.title)}">${escapeHtml(actionChip.text)}</span>
        ${isManual
          ? '<span class="board-chip">Manual — trigger only</span>'
          : `${card.recurring ? '<span class="board-chip">recurring</span>' : '<span class="board-chip">one-off</span>'}<span class="board-chip">next: ${escapeHtml(nextFire)}</span>`}
      </div>
      ${card.last_run ? `<div class="board-card-lastrun">${escapeHtml(card.last_run.outcome || '')} · ${escapeHtml(card.last_run.snippet || '')}</div>` : ''}
    `;
    // Scheduled cards open the drawer (name/message/enabled are editable
    // there) but never drag between lanes: their lane is
    // derived from the scheduler entry's own enabled/next-fire state, not
    // settable by dropping a card.
    div.addEventListener('click', () => openDrawer(card.id));
    return div;
  }

  function render() {
    chipHueMap = computeChipHueMap();
    lanesEl.innerHTML = '';
    if (visibleLanes.size === 0) {
      const hint = document.createElement('div');
      hint.className = 'board-lanes-empty-hint';
      hint.textContent = 'No lanes selected — use the Lanes filter above to show columns.';
      lanesEl.appendChild(hint);
      renderBulkBar();
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
        ${lane.id === SCHEDULED_LANE_ID ? `<button type="button" class="board-lane-add" data-lane="${lane.id}" title="New schedule">+</button>` : ''}
      `;
      const addBtn = column.querySelector('.board-lane-add');
      if (addBtn) {
        addBtn.addEventListener('click', () => {
          if (lane.id === SCHEDULED_LANE_ID) openNewScheduleForm();
          else openNewCardForm(lane.id);
        });
      }
      const cardsEl = document.createElement('div');
      cardsEl.className = 'board-lane-cards';
      for (const card of cards) {
        cardsEl.appendChild(card.kind === 'schedule' ? renderScheduleCard(card) : renderTaskCard(card));
      }
      column.appendChild(cardsEl);
      lanesEl.appendChild(column);
    }
    renderBulkBar();
  }

  // Makes card `cardId` visible and scrolls it into view — the graph tab's
  // "Show on board" action, a `?card=<id>` deep link, and activating the
  // board tab with a card selected on the graph (see `drainBoardFocus`
  // below) all land here. Relaxes EVERY shared filter that currently hides
  // the card (lanes, assignee, host, engine, tag, search, recency) through
  // one batched `setFilters` call, then confirms the card actually rendered
  // before scrolling/highlighting — the board-local "include cancelled"
  // filter is left as-is, the operator set it on purpose and it has no
  // shared counterpart to relax. An unknown id is reported rather than
  // left to fail silently.
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
      if (!stamp || (Date.now() - new Date(stamp).getTime()) / 1000 > Number(shared.recency)) {
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
  // Drag and drop — a Pointer Events model for mouse and pen only. Touch is
  // excluded (`pointerCanDrag`) so the UA keeps both axes: the lane strip's
  // horizontal scroll is the one way to reach another lane on a phone, and a
  // custom drag can only claim that axis by taking it from the scroller.
  // Touch users move cards through the drawer's actions instead. Mouse and
  // pen avoid the native HTML5 DnD path, which gives no consistent ghost or
  // drop-target feedback.
  // ------------------------------------------------------------------

  let dragState = null;   // { kind, card, assignee, sourceEl, pointerId, ghost, startX, startY, moved }
  let suppressNextClick = null;
  let suppressNextTrayClick = null;

  // A press on a card or tray button anchors a native text selection the
  // instant it lands on a text node — before slop distance decides whether
  // the gesture is a drag. Stopping `selectstart` while that pointer
  // gesture is live (`dragState` set by `onPointerDown`, cleared once the
  // gesture ends) keeps a selection from ever starting, rather than
  // clearing one after the fact. Scoped to `dragState` rather than a
  // standing `user-select: none` on `.board-card` so card text stays
  // selectable outside of a pointer gesture, and the drawer (which never
  // sets `dragState`) is unaffected.
  document.addEventListener('selectstart', (e) => {
    if (dragState) e.preventDefault();
  });

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
    if (!pointerCanDrag(e.pointerType)) return;
    // A modifier-held press on a card is a selection click, never a drag —
    // bail before any drag state is set so the trailing click reaches the
    // card's own listener untouched.
    if (source.kind === 'card' && (e.metaKey || e.ctrlKey)) return;
    if (e.target.closest('button, input, select, textarea, a') && source.kind === 'card') return;
    // A session chip has its own click navigation, and a tag chip its own
    // filter toggle. Do not let the card's drag handler capture that
    // pointer on the card, or the browser retargets the trailing
    // pointerup/click to the card and opens its drawer instead of running
    // the chip's own handler.
    if (source.kind === 'card' && e.target.closest('.board-chip-session, .board-chip-tag')) return;
    const state = {
      ...source, sourceEl: source.sourceEl || e.currentTarget,
      pointerId: e.pointerId,
      startX: e.clientX, startY: e.clientY, moved: false, cancelled: false, ghost: null,
      holdReady: true,
    };
    dragState = state;
    // Capture at pointerdown so leaving the source does not lose the gesture.
    if (state.sourceEl && state.sourceEl.setPointerCapture && e.pointerId != null) {
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
    // pointerup is ignored and the card still receives its ordinary click.
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
    // Captured before the write so Undo restores where the card actually
    // was, not wherever the board has drifted to by the time it is
    // clicked. A card dragged out of Snoozed resolves its Undo target to
    // the NATURAL lane it was snoozed from, plus the wake-up time — see
    // naturalLaneFor/undoToLane.
    const priorLane = card.lane;
    const priorStatus = card.status;
    const priorTags = (card.tags || []).slice();
    const priorSnoozedUntil = priorLane === 'snoozed' ? (card.fields && card.fields.snoozed_until) : null;
    const priorNaturalLane = priorLane === 'snoozed' ? naturalLaneFor(card) : priorLane;
    const title = card.title || card.id;
    // moveCard already toasts and re-renders on failure — nothing more to
    // do there, just avoid an unhandled rejection given that it re-throws.
    moveCard(cardId, targetLane, assignee)
      .then((data) => {
        // A card the server landed somewhere other than the requested lane
        // already has `moveCard`'s own toast naming where it really went.
        // A second toast claiming the requested move would contradict it.
        if (data && data.lane && data.lane !== targetLane) return;
        showUndoableToast(`Moved "${title}" to ${laneLabel(targetLane)}.`, () => (
          undoToLane(cardId, priorNaturalLane, priorSnoozedUntil, {
            status: priorStatus, tags: priorTags,
          })
        ));
      })
      .catch(() => {});
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
        // own.
        if (data && data.lane && data.lane !== targetLane) {
          showToast(`Card landed in ${laneLabel(data.lane)}, not ${laneLabel(targetLane)}.`, false);
        }
        // Callers that need to know where the card actually ended up (e.g.
        // the composer revealing the right lane) read it off the resolved
        // value, so hand back `data` once the board refresh settles. A caller
        // that goes on to paint from the board also needs to know whether
        // that refresh landed, so the outcome rides along on the same object
        // rather than being swallowed here.
        return fetchBoard().then((refreshed) => {
          if (data && typeof data === 'object') data.boardRefreshed = refreshed;
          return data;
        });
      })
      .catch(err => {
        showToast(`Couldn't move card: ${err.message}`, true);
        // Nothing was mutated client-side before the request resolved, so
        // the card is already still in its original lane — just re-render
        // in case a stray class (drag-over) was left behind.
        render();
        // Re-throw so a caller mid-edit (e.g. the drawer's assignee select)
        // can revert its own unsaved UI state instead of leaving a value
        // that was never actually persisted.
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
        <label style="font-size:0.75rem;color:var(--text-secondary)">Tags</label>
        <div class="drawer-tags-picker" data-field="tags-picker" role="group" aria-label="Tags">
          <div class="drawer-tag-chips" data-field="tag-chips" role="list"></div>
          <input class="drawer-tags drawer-tags-search" data-field="tags" type="search" role="combobox"
                 aria-autocomplete="list" aria-expanded="false" autocomplete="off"
                 placeholder="Search or add tags…" />
          <div class="drawer-tag-options" data-field="tag-options" role="listbox" hidden></div>
        </div>
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
    // Local-only mode (no `persist`): the card doesn't exist yet, so the
    // picker just tracks chosen tags in memory — read back via `.getTags()`
    // below when the card is actually created.
    const composerTagPicker = mountTagPicker(backdrop, [], {});
    const cleanup = () => {
      if (composerTagPicker && composerTagPicker.cancel) composerTagPicker.cancel();
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
    };
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
      // A tag typed into the picker's search field but never confirmed
      // (no Enter, no option pick) must still reach the payload, the same
      // way blur commits it in the drawer — flush it explicitly rather
      // than rely on Create's click happening to blur the search field
      // first.
      if (composerTagPicker && composerTagPicker.commitPendingText) composerTagPicker.commitPendingText();
      // The picker itself already refuses any tag matching an assignee name
      // (normalizeEditableTag filters ASSIGNEES the same as the drawer's
      // picker does), so a chosen tag can never collide with the assignee's
      // own routing tag — no separate de-dup pass is needed beyond `Set`.
      const chosenTags = composerTagPicker && composerTagPicker.getTags ? composerTagPicker.getTags() : [];
      const tags = assignee ? [...new Set([assignee, ...chosenTags])] : chosenTags;
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
            tags: tags.length ? tags : undefined,
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
  // New-schedule composer
  // ------------------------------------------------------------------

  const TRIGGER_MODES = [
    { id: 'once', label: 'One-time' },
    { id: 'daily', label: 'Daily' },
    { id: 'weekdays', label: 'Weekdays' },
    { id: 'custom', label: 'Custom days' },
    { id: 'cron', label: 'Cron' },
    { id: 'manual', label: 'Manual (trigger only)' },
  ];
  // Sunday-first (index 0 = Sun), matching cron's own day-of-week field.
  const DOW_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const GENERATED_TRIGGER_MODES = new Set(['daily', 'weekdays', 'custom']);

  // Parses a <input type="time"> value ("HH:MM") into cron's minute/hour
  // fields — `parseInt` drops any leading zero, matching cron convention
  // (e.g. "09:05" -> minute 5, hour 9).
  function parseTriggerTime(time) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(time || '');
    if (!m) return null;
    return { hour: parseInt(m[1], 10), minute: parseInt(m[2], 10) };
  }

  // Builds the cron expression a generated mode (daily/weekdays/custom)
  // currently represents, or `null` while its own fields are incomplete
  // (no time yet, or — for custom — no day checked). `once` and `cron`
  // aren't cron generators and always return `null` here.
  function buildGeneratedCron(mode, state) {
    const parts = parseTriggerTime(state.time);
    if (!parts) return null;
    if (mode === 'daily') return `${parts.minute} ${parts.hour} * * *`;
    if (mode === 'weekdays') return `${parts.minute} ${parts.hour} * * 1-5`;
    if (mode === 'custom') {
      if (state.days.size === 0) return null;
      const days = [...state.days].sort((a, b) => a - b).join(',');
      return `${parts.minute} ${parts.hour} * * ${days}`;
    }
    return null;
  }

  function triggerFieldsHtml(state) {
    if (state.mode === 'manual') {
      return `<div class="drawer-schedule-info">No trigger — this schedule never fires on its own. Fire it with Trigger now (or an agent's lifeos_schedule_trigger) after creating it.</div>`;
    }
    if (state.mode === 'once') {
      return `
        <label class="drawer-label">When</label>
        <input type="datetime-local" class="drawer-input" data-field="trigger-once" value="${escapeHtml(state.onceValue)}" />
      `;
    }
    if (state.mode === 'cron') {
      return `
        <label class="drawer-label">Cron expression</label>
        <input class="drawer-input" data-field="trigger-cron" value="${escapeHtml(state.cronText)}" placeholder="0 9 * * *" />
      `;
    }
    // daily / weekdays / custom all pick a time; custom also picks days.
    const daysHtml = state.mode === 'custom' ? `
        <div class="drawer-daypicker" data-field="trigger-days">
          ${DOW_LABELS.map((label, i) => `
            <label class="drawer-daycheck"><input type="checkbox" data-field="trigger-day" value="${i}" ${state.days.has(i) ? 'checked' : ''} /> ${label}</label>
          `).join('')}
        </div>
    ` : '';
    return `
      ${daysHtml}
      <label class="drawer-label">Time</label>
      <input type="time" class="drawer-input" data-field="trigger-time" value="${escapeHtml(state.time)}" />
    `;
  }

  // Reads the trigger builder's current state into `{schedule_type,
  // schedule_value}`, or `null` while it's incomplete — the same shape
  // `POST /api/scheduler` and `POST /api/scheduler/preview` both take.
  function readTrigger(state) {
    if (state.mode === 'manual') {
      return { schedule_type: 'manual', schedule_value: '' };
    }
    if (state.mode === 'once') {
      return state.onceValue ? { schedule_type: 'once', schedule_value: state.onceValue } : null;
    }
    if (state.mode === 'cron') {
      const text = (state.cronText || '').trim();
      return text ? { schedule_type: 'cron', schedule_value: text } : null;
    }
    const cron = buildGeneratedCron(state.mode, state);
    return cron ? { schedule_type: 'cron', schedule_value: cron } : null;
  }

  // Formats a preview ISO datetime in `tz` — the schedule's own timezone,
  // never the browser's, so the preview matches what the trigger actually
  // means. Falls back to the browser's own locale formatting for a
  // datetime whose `tz` didn't resolve (shouldn't happen: the server
  // already 422s an unresolvable timezone before returning any preview).
  function formatPreviewTime(iso, tz) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    try {
      return d.toLocaleString('en-US', { timeZone: tz || undefined, dateStyle: 'medium', timeStyle: 'short' });
    } catch (_) {
      return d.toLocaleString();
    }
  }

  function openNewScheduleForm() {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="new-schedule-title">
        <h2 id="new-schedule-title">New schedule</h2>
        <label class="drawer-label">Name</label>
        <input class="drawer-input" data-field="name" type="text" />
        <label class="drawer-label"><input type="checkbox" data-field="enabled" checked /> Enabled</label>
        <label class="drawer-label">Timezone</label>
        <input class="drawer-input" data-field="timezone" type="text" placeholder="Configured default" />
        <div class="drawer-field-error" data-field="timezone-error" hidden></div>
        <label class="drawer-label">Trigger</label>
        <select class="drawer-select" data-field="trigger-mode">
          ${TRIGGER_MODES.map(m => `<option value="${m.id}" ${m.id === 'daily' ? 'selected' : ''}>${m.label}</option>`).join('')}
        </select>
        <div data-field="trigger-fields"></div>
        <div class="drawer-field-error" data-field="trigger-error" hidden></div>
        <div data-field="preview-list"></div>
        <div class="drawer-field-error" data-field="preview-error" hidden></div>
        <label class="drawer-label">Action</label>
        <select class="drawer-select" data-field="action">
          ${SCHEDULE_ACTIONS.map(a => `<option value="${a}" ${a === 'notify' ? 'selected' : ''}>${a}</option>`).join('')}
        </select>
        <div class="drawer-section" data-field="action-sections"></div>
        <div class="drawer-field-error" data-field="action-error" hidden></div>
        <div class="drawer-field-error" data-field="general-error" hidden></div>
        <div class="actions">
          <button id="new-schedule-cancel">Cancel</button>
          <button class="danger" id="new-schedule-create" disabled>Create</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
    backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
    backdrop.querySelector('#new-schedule-cancel').onclick = cleanup;

    const nameEl = backdrop.querySelector('[data-field="name"]');
    const enabledEl = backdrop.querySelector('[data-field="enabled"]');
    const tzEl = backdrop.querySelector('[data-field="timezone"]');
    const tzErrorEl = backdrop.querySelector('[data-field="timezone-error"]');
    const modeEl = backdrop.querySelector('[data-field="trigger-mode"]');
    const triggerFieldsEl = backdrop.querySelector('[data-field="trigger-fields"]');
    const triggerErrorEl = backdrop.querySelector('[data-field="trigger-error"]');
    const previewListEl = backdrop.querySelector('[data-field="preview-list"]');
    const previewErrorEl = backdrop.querySelector('[data-field="preview-error"]');
    const actionEl = backdrop.querySelector('[data-field="action"]');
    const actionSectionsEl = backdrop.querySelector('[data-field="action-sections"]');
    const actionErrorEl = backdrop.querySelector('[data-field="action-error"]');
    const generalErrorEl = backdrop.querySelector('[data-field="general-error"]');
    const createBtn = backdrop.querySelector('#new-schedule-create');

    const sectionValues = {
      message_content: '', endpoint_config: null, executor: '', bot: '',
      persona_id: '', model_id: '', effort: '', host: '', working_dir: '',
      budget_dollars: null, wall_seconds: null,
    };
    const sections = renderScheduleActionSections(actionSectionsEl, actionEl.value, sectionValues);

    const triggerState = { mode: 'daily', onceValue: '', time: '09:00', days: new Set(), cronText: '' };

    function clearFieldErrors() {
      triggerErrorEl.hidden = true; triggerErrorEl.textContent = '';
      tzErrorEl.hidden = true; tzErrorEl.textContent = '';
      actionErrorEl.hidden = true; actionErrorEl.textContent = '';
      generalErrorEl.hidden = true; generalErrorEl.textContent = '';
      sections.clearParamsError();
    }

    function updateCreateEnabled() {
      createBtn.disabled = !(nameEl.value.trim() && readTrigger(triggerState));
    }

    function wireTriggerFields() {
      const onceEl = triggerFieldsEl.querySelector('[data-field="trigger-once"]');
      if (onceEl) onceEl.addEventListener('input', () => {
        triggerState.onceValue = onceEl.value;
        triggerErrorEl.hidden = true;
        updateCreateEnabled();
        schedulePreview();
      });
      const cronEl = triggerFieldsEl.querySelector('[data-field="trigger-cron"]');
      if (cronEl) cronEl.addEventListener('input', () => {
        triggerState.cronText = cronEl.value;
        triggerErrorEl.hidden = true;
        updateCreateEnabled();
        schedulePreview();
      });
      const timeEl = triggerFieldsEl.querySelector('[data-field="trigger-time"]');
      if (timeEl) timeEl.addEventListener('input', () => {
        triggerState.time = timeEl.value;
        triggerErrorEl.hidden = true;
        updateCreateEnabled();
        schedulePreview();
      });
      for (const dayEl of triggerFieldsEl.querySelectorAll('[data-field="trigger-day"]')) {
        dayEl.addEventListener('change', () => {
          const value = Number(dayEl.value);
          if (dayEl.checked) triggerState.days.add(value); else triggerState.days.delete(value);
          triggerErrorEl.hidden = true;
          updateCreateEnabled();
          schedulePreview();
        });
      }
    }

    function renderTriggerFields() {
      triggerFieldsEl.innerHTML = triggerFieldsHtml(triggerState);
      wireTriggerFields();
    }

    let previewTimer = null;
    let previewSeq = 0;
    function schedulePreview() {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(runPreview, 300);
    }
    async function runPreview() {
      const trig = readTrigger(triggerState);
      if (!trig) {
        ++previewSeq; // discard any in-flight response from before the trigger was cleared
        previewListEl.innerHTML = '';
        previewErrorEl.hidden = true;
        previewErrorEl.textContent = '';
        return;
      }
      if (trig.schedule_type === 'manual') {
        // Nothing to preview — manual has no trigger, so `/preview` (which
        // only understands once/cron) is never called for it.
        ++previewSeq;
        previewListEl.innerHTML = '<div class="drawer-schedule-info">Manual — trigger only.</div>';
        previewErrorEl.hidden = true;
        previewErrorEl.textContent = '';
        return;
      }
      const tz = tzEl.value.trim();
      const seq = ++previewSeq;
      try {
        const r = await fetch('/api/scheduler/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...trig, timezone: tz || undefined }),
        });
        if (!r.ok) {
          const text = await r.text();
          let msg = text;
          try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
          throw new Error(msg || `HTTP ${r.status}`);
        }
        const data = await r.json();
        if (seq !== previewSeq) return; // superseded by a later change
        previewErrorEl.hidden = true;
        previewErrorEl.textContent = '';
        const times = data.next || [];
        const resolvedTz = data.timezone || tz;
        previewListEl.innerHTML = times.length
          ? times.map(t => `<div class="drawer-schedule-info">${escapeHtml(formatPreviewTime(t, resolvedTz))}</div>`).join('')
          : '<div class="drawer-schedule-info">No upcoming fires.</div>';
      } catch (err) {
        if (seq !== previewSeq) return;
        previewListEl.innerHTML = '';
        previewErrorEl.textContent = err.message;
        previewErrorEl.hidden = false;
      }
    }

    // Maps the single `detail` string a rejected create returns to the
    // field it actually names — mirroring the wording
    // `api/routes/scheduler.py` and `api/services/scheduler_validation.py`
    // produce — rather than showing every rejection as one generic error.
    function applyCreateError(detail) {
      const message = detail || 'Something went wrong.';
      if (/^Invalid cron expression|^Invalid ISO datetime/.test(message)) {
        triggerErrorEl.textContent = message;
        triggerErrorEl.hidden = false;
        return;
      }
      if (/^Unknown timezone/.test(message)) {
        tzErrorEl.textContent = message;
        tzErrorEl.hidden = false;
        return;
      }
      if (message.startsWith('endpoint_config.')) {
        sections.showParamsError(message);
        return;
      }
      if (/^Unknown Telegram bot/.test(message) || message === 'message_content must not be blank') {
        actionErrorEl.textContent = message;
        actionErrorEl.hidden = false;
        return;
      }
      generalErrorEl.textContent = message;
      generalErrorEl.hidden = false;
    }

    modeEl.addEventListener('change', () => {
      const newMode = modeEl.value;
      if (newMode === 'cron' && GENERATED_TRIGGER_MODES.has(triggerState.mode)) {
        const generated = buildGeneratedCron(triggerState.mode, triggerState);
        if (generated) triggerState.cronText = generated;
      }
      triggerState.mode = newMode;
      renderTriggerFields();
      triggerErrorEl.hidden = true;
      updateCreateEnabled();
      schedulePreview();
    });
    nameEl.addEventListener('input', updateCreateEnabled);
    tzEl.addEventListener('input', () => {
      tzErrorEl.hidden = true;
      schedulePreview();
    });
    actionEl.addEventListener('change', () => {
      // Carries forward whatever the outgoing section's own fields
      // currently hold, so switching action and back doesn't discard
      // input the operator already typed (invalid endpoint params JSON
      // -- `getValues()` returning `null` -- leaves `sectionValues`
      // untouched rather than losing the section's other fields too).
      const current = sections.getValues();
      if (current) Object.assign(sectionValues, current);
      sections.setAction(actionEl.value);
      clearFieldErrors();
    });

    createBtn.addEventListener('click', async () => {
      const name = nameEl.value.trim();
      const trig = readTrigger(triggerState);
      if (!name || !trig) return; // Create stays disabled until both hold
      const sectionsValues = sections.getValues();
      if (sectionsValues === null) return; // invalid endpoint params JSON — sections already showed its own error

      clearFieldErrors();
      const tz = tzEl.value.trim();
      const body = {
        name,
        schedule_type: trig.schedule_type,
        schedule_value: trig.schedule_value,
        action: actionEl.value,
        enabled: enabledEl.checked,
        ...sectionsValues,
      };
      if (tz) body.timezone = tz;

      createBtn.disabled = true;
      createBtn.textContent = 'Creating…';
      try {
        const r = await fetch('/api/scheduler', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const text = await r.text();
          let detail = text;
          try { const j = JSON.parse(text); detail = j.detail || detail; } catch (_) {}
          applyCreateError(detail);
          createBtn.disabled = false;
          createBtn.textContent = 'Create';
          return;
        }
        const created = await r.json().catch(() => null);
        cleanup();
        await fetchBoard();
        if (created && created.id) revealCard(created.id, { openDrawer: false });
      } catch (err) {
        applyCreateError(err.message);
        createBtn.disabled = false;
        createBtn.textContent = 'Create';
      }
    });

    renderTriggerFields();
    updateCreateEnabled();
    schedulePreview();
  }

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
    // A modal (new-card composer, answer prompt, bulk delete confirm)
    // renders on top of both the drawer (.modal-backdrop z-index 100 >
    // .board-drawer-backdrop's 90) and the selection — let it own Escape
    // instead of closing/clearing what's underneath it.
    if (document.querySelector('.modal-backdrop')) return;
    if (openCardId && !(drawerBackdrop && drawerBackdrop.hidden)) {
      closeDrawer();
      return;
    }
    // Escape's existing drawer precedence takes priority; only once there's
    // no drawer to close does it fall through to clearing a selection

    clearSelection();
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

  function formatNextFire(iso, scheduleType) {
    if (scheduleType === 'manual') return 'Manual — trigger only.';
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

  // Shared autosize core: grows `el` to fit its content, up to `maxHeight`
  // (in px) when given, or without limit when omitted/null.
  function autosizeTextarea(el, maxHeight) {
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
    const target = el.scrollHeight + borderY;
    el.style.height = (maxHeight == null ? target : Math.min(target, maxHeight)) + 'px';
  }

  // Notes autosize — height tracks content up to 2/3 of the
  // viewport height, after which `.drawer-notes-autosize`'s
  // `overflow-y: auto` (web/agents.html) takes over scrolling. Scoped to
  // the task notes textarea only — the schedule drawer's message-content
  // textarea keeps its plain fixed/manual-resize box.
  function autosizeNotesTextarea(el) {
    autosizeTextarea(el, window.innerHeight * (2 / 3));
  }

  // Title autosize — grows with no cap; a title is short enough that it
  // never needs the notes field's internal-scroll ceiling.
  function autosizeTitleTextarea(el) {
    autosizeTextarea(el, null);
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

  // `container` is the DOM subtree carrying the `[data-field="tags-picker"]`
  // markup — the drawer (`drawerEl`) for an existing card, or a composer
  // modal for a card that doesn't exist yet. `config.persist(tags)` is the
  // async save call for a card that already has an id (the drawer's
  // `putBoardTags`); omitting it (the composer) puts the picker in
  // local-only mode — it just tracks the chosen tags in memory for the
  // caller to read back via `handle.getTags()` at submit time, with no
  // network call and no board re-fetch.
  function mountTagPicker(container, initialTags, config) {
    const persist = config && config.persist;
    const picker = container.querySelector('[data-field="tags-picker"]');
    const search = picker && picker.querySelector('[data-field="tags"]');
    const chips = picker && picker.querySelector('[data-field="tag-chips"]');
    const options = picker && picker.querySelector('[data-field="tag-options"]');
    if (!picker || !search || !chips || !options) return;

    let selected = uniqueEditableTags(initialTags);
    let confirmed = selected.slice();
    let saveChain = Promise.resolve();
    let pendingSaves = 0;
    let activeOption = -1;
    let showingLegacyValue = true;
    let cancelled = false;
    let saveGeneration = 0;
    // The suggestion list only opens once the operator has actually asked
    // for it — typed a query, or pressed ArrowDown/ArrowUp — not on bare
    // focus with an empty query, which would otherwise sit open over
    // whatever sits below the picker (the composer's Create button, or the
    // drawer's action row). Sticky until blur/Escape so a run of picks
    // (each of which clears the query back to empty) doesn't re-close it
    // between selections.
    let openByRequest = false;
    // iOS Safari doesn't focus a tapped <button>, so a chip's × or a
    // suggestion option can be activated by touch with no focus change at
    // all — the focusout handler below then sees a null `relatedTarget`
    // and can't tell that apart from a genuine tap outside the picker.
    // This flag is the fallback signal: true for the lifetime of a pointer
    // gesture that started on a chip or an option, so the focusout handler
    // can skip committing a still-typed query out from under that
    // gesture's own click handler (addTag/removeTag). Cleared on a delay
    // rather than by the click itself, since a tap that never fires
    // "click" (e.g. one interrupted by a scroll) must not leave it stuck.
    let pointerDownInsideChipsOrOptions = false;
    function markPointerDownInsidePicker() {
      pointerDownInsideChipsOrOptions = true;
      setTimeout(() => { pointerDownInsideChipsOrOptions = false; }, 0);
    }
    chips.addEventListener('pointerdown', markPointerDownInsidePicker);
    options.addEventListener('pointerdown', markPointerDownInsidePicker);

    function renderChips() {
      // Computed fresh rather than read off the outer `chipHueMap` — this
      // picker can render (e.g. a new-card composer) without a board
      // `render()` having just run to populate it.
      const hues = computeChipHueMap();
      chips.innerHTML = selected.map(tag => {
        const hue = hues.get(tag.toLowerCase());
        const style = hue != null ? ` style="--chip-hue:${hue}"` : '';
        return `
        <span class="drawer-tag-chip" data-tag="${escapeHtml(tag)}"${style}>
          <span>#${escapeHtml(tag)}</span>
          <button type="button" class="drawer-tag-chip-remove" data-remove-tag="${escapeHtml(tag)}"
                  aria-label="Remove tag ${escapeHtml(tag)}">×</button>
        </span>
      `;
      }).join('');
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
      // Empty-query focus alone never opens the list (see `openByRequest`
      // above) — only a non-blank query or an explicit ArrowDown/ArrowUp
      // does, so the list can't sit open over the Create button or drawer
      // action row the moment the field gains focus.
      options.hidden = document.activeElement !== search
        || (!query && !openByRequest)
        || (!matches.length && !canCreate);
      activeOption = -1;
      options.querySelectorAll('[data-select-tag], [data-create-tag]').forEach(button => {
        button.addEventListener('click', () => {
          if (button.dataset.createTag) addTag(button.dataset.createTag);
          else addTag(button.dataset.selectTag);
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
      if (!persist) {
        // Local-only mode: nothing to save yet, so confirm immediately.
        confirmed = requested.slice();
        return;
      }
      pendingSaves += 1;
      saveChain = saveChain.then(async () => {
        try {
          if (cancelled || generation !== saveGeneration) return;
          await persist(requested);
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
          // Add to whatever's already chosen (chips, or a card's other
          // editable tags in the drawer) rather than replacing the
          // selection outright — a chip picked earlier, or another tag
          // already on the card, must survive a still-typed token being
          // committed on blur or on Create. `queueSave` re-runs
          // `uniqueEditableTags`, so this can't duplicate `normalized` or
          // let a lifecycle/assignee tag slip through.
          queueSave([...selected, normalized]);
        }
        return;
      }
      const parsed = [];
      const rejected = [];
      // Commas act as separators alongside whitespace — "alpha, beta"
      // splits into "alpha" and "beta", not a rejected "alpha," token —
      // so `[\s,]+` (not `\s+`) is the split, and `.filter(Boolean)` drops
      // the empty string a trailing/doubled separator would otherwise
      // leave behind.
      raw.split(/[\s,]+/).filter(Boolean).forEach(token => {
        const plain = token.replace(/^#/, '');
        if (TAG_TOKEN.test(plain) && normalizeEditableTag(plain)) parsed.push(plain);
        else rejected.push(token);
      });
      const normalized = uniqueEditableTags(parsed);
      if (rejected.length) {
        showToast(`Ignored invalid tag${rejected.length > 1 ? 's' : ''}: ${rejected.join(', ')}`, true);
      }
      search.value = normalized.join(' ');
      // Add to whatever's already chosen — same reasoning as the
      // single-token branch above: a chip picked earlier, or a card's
      // other editable tags, must survive a multi-word blur/Create commit
      // rather than being replaced by it. `queueSave` re-runs
      // `uniqueEditableTags`, so this can't duplicate an already-selected
      // tag or let a lifecycle/assignee tag slip through.
      queueSave([...selected, ...normalized]);
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
    search.addEventListener('input', () => {
      // Typing is itself the operator asking for the list — sticky so
      // backspacing the query back to empty mid-pick doesn't close it.
      openByRequest = true;
      renderOptions();
    });
    // A picker-level `focusout` (not a `blur` on `search` alone) — this
    // container holds search, every suggestion option, and every chip's
    // remove button, and `focusout` bubbles, so one listener here sees
    // focus leaving ANY of those. That matters because Tab from `search`
    // lands on a suggestion (a real, focusable element inside the
    // container) before it ever leaves the picker: a `search`-only blur
    // would see that as "gone" and either commit too early or (with a
    // stay-inside guard) never get a second chance to commit once focus
    // actually does leave, on the option's own Tab-away. Committing here,
    // gated on the relatedTarget truly landing outside `picker`, fires
    // exactly once, on whichever element's focus move actually exits the
    // container — search moving straight out, or search moving onto a
    // suggestion/chip-remove button and THAT element then moving out.
    picker.addEventListener('focusout', (event) => {
      // Focus is still somewhere inside the picker — the operator is
      // mid-navigation (an option or a chip's remove button currently has
      // focus), not abandoning the field. Wait for the move that actually
      // clears the container.
      if (picker.contains(event.relatedTarget)) return;
      // iOS Safari doesn't focus a tapped <button>, so a genuine tap on a
      // chip's × or a suggestion can leave `relatedTarget` null exactly
      // like a real focus-out does — `pointerDownInsideChipsOrOptions`
      // tells the two apart so that gesture's own click handler
      // (addTag/removeTag) runs uncontested by a stale-query commit here.
      if (pointerDownInsideChipsOrOptions) return;
      saveLegacyText();
      openByRequest = false;
      options.hidden = true;
      search.setAttribute('aria-expanded', 'false');
    });
    search.addEventListener('keydown', (event) => {
      // ArrowDown/ArrowUp on an empty, not-yet-opened query is itself a
      // request to open the list — re-render first so `optionButtons`
      // below reflects the now-visible options instead of navigating a
      // list that's still hidden (and whose buttons can't take focus).
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && options.hidden) {
        openByRequest = true;
        renderOptions();
      }
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
        openByRequest = false;
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
      // The composer reads the chosen tags back through this at submit
      // time instead of persisting through `config.persist`.
      getTags: () => confirmed.slice(),
      // Commits whatever token is currently sitting in the search field but
      // not yet confirmed as a chip — the same normalization/rejection
      // `saveLegacyText` already applies on blur. The composer calls this
      // explicitly right before `getTags()` so a typed-but-unconfirmed tag
      // reaches the create payload the same way blur commits it in the
      // drawer, without depending on a blur event actually having fired
      // first (idempotent — a no-op if there's nothing pending, or if
      // blur already committed it).
      commitPendingText: () => saveLegacyText(),
    };
    if (persist) tagPickerHandle = handle;
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

  // A wake-up time renders as a short local date AND time (unlike
  // formatCardDate's date-only rows above) — "Sep 16, 9:00 AM" — since the
  // whole point of showing it is telling the operator when the card comes
  // back, not just what day.
  function formatWakeTime(iso) {
    if (!iso) return null;
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return { label: iso, exact: iso };
    return {
      label: parsed.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      }),
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
    if (card.lane === 'snoozed' && card.fields && card.fields.snoozed_until) {
      const wake = formatWakeTime(card.fields.snoozed_until);
      if (wake) {
        items.push(`
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Wakes</span>
            <span class="drawer-meta-value" title="${escapeAttr(wake.exact)}">${escapeHtml(wake.label)}</span>
          </div>
        `);
      }
    }
    if (card.fields && card.fields.review_accepted_by) {
      const acceptedBy = card.fields.review_accepted_by;
      const label = acceptedBy.startsWith('owner:') ? 'Project owner' : 'Operator';
      items.push(`
        <div class="drawer-meta-item">
          <span class="drawer-meta-label">Accepted by</span>
          <span class="drawer-meta-value" title="${escapeAttr(acceptedBy)}">${escapeHtml(label)}</span>
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

  // Only a real github.com pull-request URL renders as a clickable link —
  // everything else (a malformed or tampered value) falls back to plain
  // escaped text rather than being trusted into an href.
  const GITHUB_PR_URL_RE = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/;

  function outcomePrRowHtml(pr) {
    const label = pr.number ? `#${pr.number}` : 'Pull request';
    const link = GITHUB_PR_URL_RE.test(String(pr.url || ''))
      ? `<a href="${escapeHtml(pr.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
      : escapeHtml(label);
    const staleTitle = pr.stale ? ' title="status may be out of date — refreshing in the background"' : '';
    return `
      <div class="drawer-outcome-pr">
        ${link}
        <span class="board-pr-badge ${prStateClass(pr)}"${staleTitle}>${escapeHtml(prStateLabel(pr))}</span>
      </div>
    `;
  }

  // The read-only "Agent outcome" section: what the agent reported when
  // its run completed, kept deliberately separate from the editable Notes
  // textarea below it — this is server-recorded fact about a finished run,
  // not something the operator edits. Every value here is agent- or
  // git-host-provided and untrusted, so it's all rendered through
  // escapeHtml/escapeAttr like the rest of the drawer.
  function cardOutcomeHtml(card) {
    const outcome = card.outcome;
    if (!outcome) return '';
    const when = formatWakeTime(outcome.created_at);
    const prs = outcome.prs || [];
    return `
      <div class="drawer-section drawer-outcome" data-field="outcome">
        <label class="drawer-label">Agent outcome</label>
        <div class="drawer-outcome-meta">
          <span class="drawer-outcome-engine">${escapeHtml(outcome.engine_label || 'Agent')}</span>
          ${when ? `<span class="drawer-outcome-when" title="${escapeHtml(when.exact)}">${escapeHtml(when.label)}</span>` : ''}
        </div>
        <div class="drawer-outcome-summary">${escapeHtml(outcome.summary || '(the agent left no summary)')}</div>
        ${outcome.branch ? `<div class="drawer-outcome-branch">Branch: <code>${escapeHtml(outcome.branch)}</code></div>` : ''}
        ${prs.length ? `<div class="drawer-outcome-prs">${prs.map(outcomePrRowHtml).join('')}</div>` : ''}
      </div>
    `;
  }

  function projectCountsHtml(project) {
    const counts = (project && project.counts) || {};
    const labels = [
      ['done', 'done'], ['awaiting_review', 'awaiting review'],
      ['running', 'running'], ['blocked', 'blocked'], ['assigned', 'assigned'],
      ['unassigned', 'unassigned'], ['cancelled', 'cancelled'],
    ];
    return labels.filter(([key]) => counts[key]).map(([key, label]) =>
      `<span class="board-chip">${escapeHtml(`${counts[key]} ${label}`)}</span>`).join('');
  }

  function childStatusLabel(child) {
    const tags = new Set((child.tags || []).map(tag => String(tag).replace(/^#/, '').toLowerCase()));
    if (tags.has('agent-completed') && !tags.has('accepted')) return 'awaiting review';
    if (child.status === 'cancelled' || child.status === 'done') return child.status;
    if (tags.has('agent-blocked') || tags.has('human') || tags.has('agent-wait-provider') ||
        tags.has('agent-wait-dependency') || child.status === 'blocked') return 'blocked';
    if (tags.has('agent-running') || child.status === 'in_progress') return 'running';
    return child.status || 'todo';
  }

  function childAssignee(child) {
    const tags = new Set((child.tags || []).map(tag => String(tag).replace(/^#/, '').toLowerCase()));
    return ASSIGNEES.find(assignee => tags.has(assignee)) || '';
  }

  function handoffPending(card) {
    return Boolean(card.project?.handoff_pending || card.fields?.project_handoff_operation_id);
  }

  function projectDetailsHtml(card) {
    if (card.is_project) {
      const project = card.project || {};
      const pendingHandoff = handoffPending(card);
      const coordinator = project.coordinator;
      // A terminal, still-on-record owner gets woken, not replaced by a
      // second session (see `plan_and_delegate`'s wake-request path) — the
      // button says so up front rather than only after the fact in the toast.
      const wakeExisting = Boolean(
        coordinator && coordinator.session_id && coordinator.live === false
        && coordinator.status !== 'missing',
      );
      const coordination = coordinator ? `
        <div class="project-coordination" data-field="project-coordination">
          Coordination: ${escapeHtml(coordinator.status || 'pending')}
          ${coordinator.result ? ` — ${escapeHtml(typeof coordinator.result === 'string' ? coordinator.result : JSON.stringify(coordinator.result))}` : ''}
          ${coordinator.session_id ? `<button type="button" class="project-inline-action" data-action="project-session" data-session-id="${escapeAttr(coordinator.session_id)}">View session</button>` : ''}
        </div>` : '<div class="project-coordination">No coordination run yet.</div>';
      // Coding children of a project with a recorded integration branch
      // always branch off and PR into it (see git_worktree.ensure_worktree's
      // base_branch) — this just surfaces what the owner has already merged
      // there, not a live re-check.
      const integrationPrs = project.integration_prs || [];
      const integrationHtml = project.integration_branch ? `
        <div class="project-integration" data-field="project-integration">
          Integration branch: <code>${escapeHtml(project.integration_branch)}</code>
          ${integrationPrs.length ? `
            <div class="project-integration-prs">
              ${integrationPrs.map(pr => `
                <div class="drawer-integration-pr">
                  <span class="drawer-integration-pr-title">${escapeHtml(pr.title || pr.child_id)}</span>
                  ${outcomePrRowHtml(pr)}
                </div>
              `).join('')}
            </div>` : ''}
        </div>` : '';
      return `
        <div class="drawer-section project-summary" data-field="project-details">
          <label class="drawer-label">Project progress</label>
          <strong>${escapeHtml(projectProgressLabel(project))}</strong>
          ${project.ready_to_close ? '<span class="board-project-ready"> Ready to close</span>' : ''}
          <div class="project-status-row">${projectCountsHtml(project)}</div>
          ${project.execution_paused ? '<div class="project-coordination">Parent execution is paused while children own the work.</div>' : ''}
          ${project.cancellation_pending ? '<div class="project-error">Cancellation is still being reconciled. Retry cancellation after resolving any listed failures.</div>' : ''}
          ${pendingHandoff ? '<div class="project-error" data-field="handoff-pending">Handoff pending. Child execution is blocked until the source agent stop is verified. You can cancel the handoff; cancellation stays pending until that stop is verified.</div>' : ''}
          ${project.paused ? `<div class="project-error" data-field="project-paused">Paused (${escapeHtml(project.pause_reason || 'operator')}). Child claims, Open, and Plan and delegate are blocked; a child already mid-turn still finishes into Review.</div>` : ''}
          ${coordination}
          ${integrationHtml}
          <div class="drawer-actions">
            <button type="button" class="drawer-action" data-action="project-start">Start project</button>
            <button type="button" class="drawer-action" data-action="project-plan">${wakeExisting ? 'Wake project owner' : 'Plan and delegate'}</button>
            <button type="button" class="drawer-action" data-action="project-complete">Complete project</button>
            <button type="button" class="drawer-action danger" data-action="project-cancel">Cancel project</button>
            ${project.paused
              ? '<button type="button" class="drawer-action" data-action="project-resume">Resume project</button>'
              : '<button type="button" class="drawer-action" data-action="project-pause">Pause project</button>'}
            <button type="button" class="drawer-action" data-action="project-add-child">Add child</button>
            <button type="button" class="drawer-action" data-action="project-attach-child">Attach existing</button>
          </div>
          <div class="project-child-list" data-field="project-children" aria-live="polite">Loading children…</div>
        </div>`;
    }
    if (card.parent_id) {
      return `<div class="drawer-section project-summary" data-field="parent-navigation">
        <label class="drawer-label">Project</label>
        <button type="button" class="drawer-action" data-action="open-parent" data-parent-id="${escapeAttr(card.parent_id)}">Open ${escapeHtml(card.parent_title || 'project')}</button>
      </div>`;
    }
    if (handoffPending(card)) {
      return `<div class="drawer-section project-summary" data-field="handoff-pending">
        <label class="drawer-label">Handoff pending</label>
        <div class="project-error">Child execution is blocked until the source agent stop is verified. You can cancel the handoff; cancellation stays pending until that stop is verified.</div>
        <button type="button" class="drawer-action danger" data-action="project-cancel">Cancel handoff</button>
      </div>`;
    }
    if (card.fields && card.fields.execution_paused) {
      return `<div class="drawer-section project-summary" data-field="execution-paused">
        <label class="drawer-label">Execution paused</label>
        <div class="project-coordination">This former parent will not resume automatically after its final child was removed.</div>
        <button type="button" class="drawer-action" data-action="resume-execution">Resume execution</button>
      </div>`;
    }
    return '';
  }

  async function projectRequest(path, body) {
    const response = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text();
      try { throw new Error(JSON.parse(text).detail || text); } catch (error) {
        if (error instanceof SyntaxError) throw new Error(text || `HTTP ${response.status}`);
        throw error;
      }
    }
    return response.json();
  }

  function operationId() {
    return globalThis.crypto && globalThis.crypto.randomUUID
      ? globalThis.crypto.randomUUID() : `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
    drawerEl.classList.toggle('project-drawer', !!card.is_project);
    drawerEl.innerHTML = `
      <div class="drawer-header">
        <button class="panel-close" data-action="drawer-close">×</button>
        <textarea class="drawer-title" data-field="title" rows="1">${escapeHtml(titleValue)}</textarea>
      </div>
      ${isTask ? `
      <div class="drawer-section drawer-meta" data-field="meta">${cardMetaHtml(card)}</div>
      ${projectDetailsHtml(card)}
      ${cardOutcomeHtml(card)}
      <div class="drawer-section">
      <label class="drawer-label">Notes</label>
      <textarea class="drawer-notes drawer-notes-autosize" data-field="notes" placeholder="Notes…">${escapeHtml(card.notes || '')}</textarea>
      <div>
        <label class="drawer-label">${card.is_project ? 'Owner' : 'Assignee'}</label>
        <select class="drawer-assignee" data-field="assignee" ${assigneeDisabled ? 'disabled' : ''}>
          <option value="">unassigned</option>
          ${ASSIGNEES.map(a => `<option value="${a}" ${card.assignee === a ? 'selected' : ''}>${a}</option>`).join('')}
        </select>
        ${assigneeDisabled ? `<div class="drawer-field-reason" data-field="assignee-reason">${escapeHtml(assigneePolicy.reason || "This card's assignee can't be changed right now.")}</div>` : ''}
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
      <label class="drawer-label"><input type="checkbox" data-field="enabled" ${card.enabled ? 'checked' : ''} /> Enabled</label>
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Schedule type</label>
          <select class="drawer-select" data-field="schedule-type">
            <option value="cron" ${card.schedule_type === 'cron' ? 'selected' : ''}>cron</option>
            <option value="once" ${card.schedule_type === 'once' ? 'selected' : ''}>once</option>
            <option value="manual" ${card.schedule_type === 'manual' ? 'selected' : ''}>manual (trigger only)</option>
          </select>
        </div>
        <div data-field="schedule-value-group" ${card.schedule_type === 'manual' ? 'hidden' : ''}>
          <label class="drawer-label" data-field="schedule-value-label">${card.schedule_type === 'once' ? 'When (ISO datetime)' : 'Cron expression'}</label>
          <input class="drawer-schedule-value" data-field="schedule-value" value="${escapeHtml(card.schedule_value || '')}" placeholder="${card.schedule_type === 'once' ? '2026-06-03T15:05:00' : '0 9 * * *'}" />
          <div class="drawer-field-error" data-field="schedule-value-error" hidden></div>
        </div>
      </div>
      <label class="drawer-label">Timezone</label>
      <input class="drawer-timezone" data-field="timezone" value="${escapeHtml(card.timezone || '')}" placeholder="e.g. America/New_York" />
      <div class="drawer-field-error" data-field="timezone-error" hidden></div>
      <label class="drawer-label">Action</label>
      <select class="drawer-select" data-field="action">
        ${SCHEDULE_ACTIONS.map(a => `<option value="${a}" ${card.action === a ? 'selected' : ''}>${a}</option>`).join('')}
      </select>
      <div class="drawer-field-error" data-field="action-error" hidden></div>
      <div class="drawer-section" data-field="action-sections"></div>
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
    // Titles are single-line values in the vault: a pasted or otherwise
    // typed newline is collapsed to a space before it's ever compared or
    // saved, so the field never turns a task's description into a
    // multi-line value. `lastSavedTitle` (not the render-time `titleValue`)
    // is what a save compares and reverts against, so a second commit of
    // the same value — Enter followed by a later blur, with no further
    // typing in between — is a no-op instead of firing a duplicate PUT.
    let lastSavedTitle = titleValue;
    async function saveTitle() {
      const value = titleEl.value.replace(/\r\n|\r|\n/g, ' ').trim();
      if (!value || value === lastSavedTitle) return;
      try {
        if (isTask) await putTask(card.id, { description: value });
        else await putSchedule(card.id, { name: value });
        lastSavedTitle = value;
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't save title: ${err.message}`, true);
        titleEl.value = lastSavedTitle;
        autosizeTitleTextarea(titleEl);
      }
    }
    titleEl.addEventListener('blur', saveTitle);
    // Enter commits the title (the same save `blur` uses) instead of
    // inserting a line break — skipped mid-IME-composition so committing
    // an East Asian input method's conversion doesn't fire an early save.
    // This never calls `.blur()`: doing so would move
    // `document.activeElement` to `<body>`, outside `drawerEl`, which
    // would drop the field out of `updateOpenDrawer`'s `focused` guard
    // (board.js's drawer-update path) while the save above is still in
    // flight — an unrelated live-board tick for the same card could then
    // repaint the title from the server's still-stale value, flashing the
    // just-typed text back to the old one on screen. Leaving focus in the
    // field keeps that guard in effect exactly like it already does for
    // the notes field while typing.
    titleEl.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      e.preventDefault();
      saveTitle();
    });
    titleEl.addEventListener('input', () => autosizeTitleTextarea(titleEl));
    autosizeTitleTextarea(titleEl);  // size to existing content on open/re-render

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

    mountTagPicker(drawerEl, editableTags, { persist: (tags) => putBoardTags(card.id, tags) });

    const assigneeEl = drawerEl.querySelector('.drawer-assignee[data-field="assignee"]');
    assigneeEl.addEventListener('change', async () => {
      const value = assigneeEl.value;
      // Captured immediately, ahead of `moveCard`'s own `fetchBoard()`
      // possibly running `updateOpenDrawer` -> `renderDrawer`, which
      // would otherwise reset `assignmentHandle` to null ahead of the
      // wait below on the save this handler actually started with.
      const handle = assignmentHandle;
      try {
        const moved = await moveCard(card.id, value ? 'assigned' : 'unassigned', value || undefined);
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
        const settleThenRebuild = () => handle.whenIdle().then(() => fetchBoard()).then((refreshed) => {
          if (handle.isSaving && handle.isSaving()) return settleThenRebuild();
          // A failed refresh leaves `board` holding the pre-save snapshot.
          // Painting from it shows the operator committed values as though
          // they had been reset, and advancing `openCardSnapshot` would stop
          // any later frame from noticing the difference — the picker values
          // would stay wrong until the drawer is reopened, and the next
          // picker change would write the stale value back to the vault.
          // Leave both alone and let a later successful frame converge it.
          if (!refreshed) return;
          rebuild();
        }).catch(() => {});
        if (handle && handle.isSaving && handle.isSaving()) {
          settleThenRebuild();
        } else if (moved && moved.boardRefreshed === false) {
          // `moveCard`'s own board refresh failed, so there is nothing
          // fresher to paint — the same reason `settleThenRebuild` holds off
          // above. Leave the drawer and its snapshot alone for a later frame.
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

    renderProjectDrawerFields(card);
    renderDrawerActions(card);
    renderDrawerSession(card);
  }

  function openProjectPrompt(title, label, onSubmit) {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `<div class="modal" role="dialog" aria-label="${escapeAttr(title)}">
      <h2>${escapeHtml(title)}</h2><label>${escapeHtml(label)}</label>
      <input class="drawer-input" data-field="project-prompt" />
      <div class="actions"><button type="button" data-action="cancel">Cancel</button><button type="button" class="danger" data-action="confirm">Confirm</button></div>
    </div>`;
    document.body.appendChild(backdrop);
    const close = () => backdrop.remove();
    backdrop.addEventListener('click', event => { if (event.target === backdrop) close(); });
    backdrop.querySelector('[data-action="cancel"]').onclick = close;
    backdrop.querySelector('[data-action="confirm"]').onclick = async () => {
      const value = backdrop.querySelector('[data-field="project-prompt"]').value.trim();
      if (!value) return;
      try { await onSubmit(value); close(); } catch (error) { showToast(error.message, true); }
    };
    backdrop.querySelector('[data-field="project-prompt"]').focus();
  }

  function openProjectCancellation(card) {
    projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/cancel`, { confirm: false, operation_id: null })
      .then(preview => {
        const isProject = card.is_project;
        const subject = isProject ? 'project' : 'task';
        const cancellationSummary = isProject
          ? `${preview.unfinished_count || 0} unfinished child${preview.unfinished_count === 1 ? '' : 'ren'}, ${preview.running_count || 0} running agent${preview.running_count === 1 ? '' : 's'}, and ${preview.awaiting_review_count || 0} review result${preview.awaiting_review_count === 1 ? '' : 's'} will be cancelled or abandoned.`
          : 'This will cancel the whole task and abandon the pending handoff. No child tasks have been created.';
        const backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop';
        backdrop.innerHTML = `<div class="modal" role="dialog" aria-label="Cancel ${subject}">
          <h2>Cancel ${subject}?</h2>
          <p>${escapeHtml(cancellationSummary)}</p>
          ${isProject ? '<p>Completed children and their history stay intact. Pending-review output is preserved but is not accepted.</p>' : ''}
          <div class="actions"><button type="button" data-action="cancel">Keep ${subject}</button><button type="button" class="danger" data-action="confirm">Cancel ${subject}</button></div>
        </div>`;
        document.body.appendChild(backdrop);
        const close = () => backdrop.remove();
        backdrop.querySelector('[data-action="cancel"]').onclick = close;
        backdrop.querySelector('[data-action="confirm"]').onclick = async () => {
          const button = backdrop.querySelector('[data-action="confirm"]');
          button.disabled = true;
          try {
            const result = await projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/cancel`, {
              confirm: true, operation_id: preview.operation_id || operationId(),
            });
            await fetchBoard();
            const failures = (result.failures || []).length;
            showToast(result.complete ? 'Project cancelled.' : `Cancellation is pending${failures ? ` (${failures} remaining failure${failures === 1 ? '' : 's'})` : ''}.`, !result.complete);
            close();
          } catch (error) { showToast(`Couldn't cancel project: ${error.message}`, true); button.disabled = false; }
        };
      })
      .catch(error => showToast(`Couldn't preview cancellation: ${error.message}`, true));
  }

  function renderProjectDrawerFields(card) {
    const openParent = drawerEl.querySelector('[data-action="open-parent"]');
    if (openParent) openParent.onclick = () => openDrawer(openParent.dataset.parentId);
    const resume = drawerEl.querySelector('[data-action="resume-execution"]');
    if (resume) resume.onclick = async () => {
      try {
        await projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/resume-execution`);
        await fetchBoard();
        showToast('Execution resumed.', false);
      } catch (error) { showToast(`Couldn't resume execution: ${error.message}`, true); }
    };
    const pendingHandoff = handoffPending(card);
    if (!card.is_project && !pendingHandoff) return;

    if (!card.is_project) {
      const cancel = drawerEl.querySelector('[data-action="project-cancel"]');
      if (cancel) cancel.onclick = () => openProjectCancellation(card);
      return;
    }

    const actions = {
      'project-start': async () => projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/start`),
      'project-plan': async () => projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/plan`, { operation_id: operationId() }),
      'project-complete': async () => {
        const cancelled = Number(card.project?.counts?.cancelled) || 0;
        if (cancelled && !window.confirm(`Close this project with ${cancelled} cancelled child${cancelled === 1 ? '' : 'ren'}?`)) return null;
        return projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/complete`, { acknowledge_cancelled_children: cancelled > 0 });
      },
      'project-pause': async () => projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/pause`, {}),
      'project-resume': async () => projectRequest(`/api/tasks/${encodeURIComponent(card.id)}/project/resume`, {}),
    };
    const actionPolicyNames = {
      'project-start': 'can_start_project',
      'project-plan': 'can_plan_project',
      'project-complete': 'can_complete_project',
      'project-pause': 'can_pause_project',
      'project-resume': 'can_resume_project',
    };
    const actionToasts = {
      // A function reads the request's own response, since the same action
      // can mean two different things (wake vs. new session) — see
      // `plan_and_delegate`'s additive `wake_requested` field.
      'project-plan': result => (result && result.wake_requested ? 'Woke project owner.' : 'Coordination started.'),
      'project-pause': 'Project paused.',
      'project-resume': 'Project resumed.',
    };
    Object.entries(actions).forEach(([action, request]) => {
      const button = drawerEl.querySelector(`[data-action="${action}"]`);
      if (!button) return;
      const policyName = actionPolicyNames[action];
      if ((card.policy && card.policy[policyName] === false) || pendingHandoff) button.disabled = true;
      button.onclick = async () => {
        button.disabled = true;
        try {
          const result = await request();
          if (result !== null) {
            await fetchBoard();
            const toast = actionToasts[action];
            const message = typeof toast === 'function' ? toast(result) : (toast || 'Project updated.');
            showToast(message, false);
          }
        } catch (error) { showToast(`Couldn't update project: ${error.message}`, true); }
        finally { if (button.isConnected) button.disabled = false; }
      };
    });
    const cancel = drawerEl.querySelector('[data-action="project-cancel"]');
    if (cancel) {
      if (card.policy && card.policy.can_cancel_project === false) cancel.disabled = true;
      cancel.onclick = () => { if (!cancel.disabled) openProjectCancellation(card); };
    }
    const session = drawerEl.querySelector('[data-action="project-session"]');
    if (session) session.onclick = () => { requestGraphFocus(session.dataset.sessionId); activateTab('graph'); };

    const add = drawerEl.querySelector('[data-action="project-add-child"]');
    if (add) add.onclick = () => openProjectPrompt('Add project child', 'Child title', async description => {
      await fetch('/api/tasks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description, fields: { parent_id: card.id } }),
      }).then(async response => { if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`); });
      await fetchBoard();
    });
    const attach = drawerEl.querySelector('[data-action="project-attach-child"]');
    if (attach) attach.onclick = () => openProjectPrompt('Attach existing task', 'Task ID', async childId => {
      await putTask(childId, { fields: { parent_id: card.id } });
      await fetchBoard();
    });
    loadProjectChildren(card);
  }

  async function loadProjectChildren(card) {
    const list = drawerEl.querySelector('[data-field="project-children"]');
    if (!list) return;
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(card.id)}/children?limit=50&offset=0`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const children = data.tasks || [];
      list.innerHTML = children.length ? children.map(child => {
        const assignee = childAssignee(child);
        return `
        <div class="project-child-row" data-child-id="${escapeAttr(child.id)}">
          <button type="button" class="project-child-open" data-action="open-child" title="${escapeAttr(child.title || child.description || child.id)}">${escapeHtml(child.title || child.description || child.id)}</button>
          <span class="project-child-meta">${escapeHtml(`${assignee || 'unassigned'} · ${childStatusLabel(child)}`)}</span>
          <select class="project-inline-action" data-action="assign-child" aria-label="Assign ${escapeAttr(child.title || child.description || child.id)}">
            <option value="">unassigned</option>${ASSIGNEES.map(option => `<option value="${option}" ${assignee === option ? 'selected' : ''}>${option}</option>`).join('')}
          </select>
          <div class="project-child-actions"><button type="button" data-action="detach-child">Detach</button><button type="button" data-action="move-child">Move</button></div>
        </div>`;
      }).join('') : '<div class="project-coordination">No children found.</div>';
      if (children.length && Number(data.total) > children.length) {
        list.insertAdjacentHTML('beforeend', `<div class="project-coordination">Showing ${children.length} of ${Number(data.total)} children.</div>`);
      }
      list.querySelectorAll('[data-action="open-child"]').forEach(button => {
        button.onclick = () => openDrawer(button.closest('[data-child-id]').dataset.childId);
      });
      list.querySelectorAll('[data-action="detach-child"]').forEach(button => {
        button.onclick = async () => {
          const childId = button.closest('[data-child-id]').dataset.childId;
          try { await putTask(childId, { fields: { parent_id: null } }); await fetchBoard(); }
          catch (error) { showToast(`Couldn't detach child: ${error.message}`, true); }
        };
      });
      list.querySelectorAll('[data-action="assign-child"]').forEach(select => {
        select.onchange = async () => {
          const childId = select.closest('[data-child-id]').dataset.childId;
          try {
            await moveCard(childId, select.value ? 'assigned' : 'unassigned', select.value || undefined);
            await loadProjectChildren(card);
          } catch (error) {
            showToast(`Couldn't assign child: ${error.message}`, true);
            const child = children.find(item => item.id === childId);
            select.value = child ? childAssignee(child) : '';
          }
        };
      });
      list.querySelectorAll('[data-action="move-child"]').forEach(button => {
        button.onclick = () => {
          const childId = button.closest('[data-child-id]').dataset.childId;
          openProjectPrompt('Move project child', 'New project task ID', async parentId => {
            await putTask(childId, { fields: { parent_id: parentId } });
            await fetchBoard();
          });
        };
      });
    } catch (error) {
      list.innerHTML = `<div class="project-error">Couldn't load children: ${escapeHtml(error.message)}</div>`;
    }
  }

  // Wires the scheduled-card drawer's editable fields (renderDrawer's
  // `!isTask` branch above). Every field saves through putSchedule — the
  // scheduler API, never the vault file directly — on blur for text
  // inputs and on change for selects/the checkbox, refetching the board on
  // a successful save. A rejected save shows the server's `detail` inline
  // next to the offending field (schedule value, timezone, action) or as a
  // toast (every other field), and snaps the control back to the last
  // value the server actually accepted. The schedule type select is the
  // one exception: changing it only updates the value field's label and
  // placeholder locally — it saves together with the schedule value, on
  // the value field's own blur, so a type and a value that doesn't parse
  // under it can never reach the server in the same write (see below).
  // The action select is the other exception: switching to an action whose
  // section doesn't yet have what it needs to fire (see
  // `actionInputsSatisfied`) holds the switch locally instead of saving it
  // alone, and a rejected save carrying it leaves the select and its
  // section showing the operator's own entry rather than reverting them
  // (see `pendingAction` below).
  // Normalizes an `endpoint_config` (or its absence) into a comparable
  // key, for skipping a redundant save when the endpoint fields are
  // blurred/changed without actually being edited — the same "no-op if
  // unchanged" rule every other schedule field below follows.
  // Mirrors readEndpointConfig()'s own shape (schedule_sections.js):
  // method upper-cased, endpoint defaulted to "", absent params as `null`.
  function endpointConfigKey(cfg) {
    const c = cfg || {};
    return JSON.stringify({
      method: String(c.method || 'GET').toUpperCase(),
      endpoint: c.endpoint || '',
      params: c.params === undefined ? null : c.params,
    });
  }

  function renderScheduleDrawerFields(card) {
    const actionSectionsEl = drawerEl.querySelector('[data-field="action-sections"]');
    const sectionValues = {
      message_content: card.message_content || '',
      endpoint_config: card.endpoint_config || null,
      executor: card.executor || '',
      bot: card.bot || '',
      persona_id: card.persona_id || '',
      model_id: card.model_id || '',
      effort: card.effort || '',
      host: card.host || '',
      working_dir: card.working_dir || '',
      budget_dollars: card.budget_dollars != null ? card.budget_dollars : null,
      wall_seconds: card.wall_seconds != null ? card.wall_seconds : null,
    };
    const sections = renderScheduleActionSections(actionSectionsEl, card.action, sectionValues);

    // What the server last actually accepted for each per-action field —
    // a rejected save reverts its control to these, mirroring every other
    // schedule field's own lastSaved* tracking below.
    let lastSavedMessage = sectionValues.message_content;
    let lastSavedBot = sectionValues.bot;
    let lastSavedSectionExecutor = sectionValues.executor;
    let lastSavedEndpointConfig = sectionValues.endpoint_config;
    let lastSavedEndpointConfigKey = endpointConfigKey(lastSavedEndpointConfig);
    let lastSavedPersonaId = sectionValues.persona_id;
    let lastSavedModelId = sectionValues.model_id;
    let lastSavedEffort = sectionValues.effort;
    let lastSavedHost = sectionValues.host;
    let lastSavedWorkingDir = sectionValues.working_dir;
    let lastSavedBudgetDollars = sectionValues.budget_dollars;
    let lastSavedWallSeconds = sectionValues.wall_seconds;

    // (Re)wires save-on-blur/change for whichever fields the current
    // action's section actually rendered — called once after the initial
    // render and again after every `sections.setAction()` rebuild, since
    // a rebuild replaces the DOM elements the previous wiring pass
    // attached to.
    function wireActionSectionFields() {
      const els = sections.elements;

      if (els.message) {
        els.message.addEventListener('blur', async () => {
          const value = els.message.value;
          if (value === (lastSavedMessage || '')) return;
          // A pending action switch (set by the Action select's own
          // change handler below, when the target action's section
          // didn't yet satisfy the server's requirement) rides along in
          // this same PUT — the server sees one write with both fields,
          // never an action alone with nothing to back it.
          const savingAction = pendingAction;
          const patch = { message_content: value };
          if (savingAction) patch.action = savingAction;
          try {
            await putSchedule(card.id, patch);
            lastSavedMessage = value;
            // `sectionValues` is the same object `sections` reads from —
            // updating it here keeps a later `setAction()` (switching away
            // and back to this action mid-session) rendering this saved
            // value instead of the stale one the drawer opened with.
            sectionValues.message_content = value;
            if (savingAction) {
              lastSavedAction = savingAction;
              pendingAction = null;
              clearActionError();
            }
            await fetchBoard();
          } catch (err) {
            if (savingAction) {
              // The action switch is still pending — leave the select and
              // this field exactly as the operator left them so they can
              // fix and retry, rather than reverting content they just
              // typed.
              showActionError(err.message);
            } else {
              showToast(`Couldn't save message: ${err.message}`, true);
              els.message.value = lastSavedMessage || '';
            }
          }
        });
      }

      if (els.bot) {
        els.bot.addEventListener('change', async () => {
          try {
            await putSchedule(card.id, { bot: els.bot.value });
            lastSavedBot = els.bot.value;
            sectionValues.bot = lastSavedBot;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save bot: ${err.message}`, true);
            els.bot.value = lastSavedBot;
          }
        });
      }

      if (els.executor) {
        els.executor.addEventListener('change', async () => {
          try {
            await putSchedule(card.id, { executor: els.executor.value });
            lastSavedSectionExecutor = els.executor.value;
            sectionValues.executor = lastSavedSectionExecutor;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save executor: ${err.message}`, true);
            els.executor.value = lastSavedSectionExecutor;
          }
        });
      }

      if (els.method && els.path && els.params) {
        const saveEndpointConfig = async () => {
          const cfg = sections.readEndpointConfig();
          if (cfg === null) return; // invalid JSON/non-object params — error already shown, nothing sent
          const key = endpointConfigKey(cfg);
          if (key === lastSavedEndpointConfigKey) return;
          // See the message handler above: a pending action switch rides
          // along in this same PUT once these fields actually satisfy the
          // target action's requirement. Until then (e.g. the method is
          // picked before a path is entered), hold the edit locally same
          // as the switch itself, rather than sending a combined PUT the
          // server would reject for a field the operator hasn't finished.
          const savingAction = pendingAction;
          if (savingAction && !actionInputsSatisfied(savingAction, { endpoint_config: cfg })) return;
          const patch = { endpoint_config: cfg };
          if (savingAction) patch.action = savingAction;
          try {
            await putSchedule(card.id, patch);
            lastSavedEndpointConfig = cfg;
            lastSavedEndpointConfigKey = key;
            sectionValues.endpoint_config = cfg;
            sections.clearParamsError();
            if (savingAction) {
              lastSavedAction = savingAction;
              pendingAction = null;
              clearActionError();
            }
            await fetchBoard();
          } catch (err) {
            if (savingAction) {
              // Leave the method/path/params fields exactly as entered —
              // the action switch is still pending, and reverting them
              // would discard the operator's own fix along with the
              // rejection.
              showActionError(err.message);
            } else {
              sections.showParamsError(err.message);
              els.method.value = String((lastSavedEndpointConfig && lastSavedEndpointConfig.method) || 'GET').toUpperCase();
              els.path.value = (lastSavedEndpointConfig && lastSavedEndpointConfig.endpoint) || '';
              els.params.value = lastSavedEndpointConfig && lastSavedEndpointConfig.params !== undefined
                ? JSON.stringify(lastSavedEndpointConfig.params, null, 2) : '';
            }
          }
        };
        els.method.addEventListener('change', saveEndpointConfig);
        els.path.addEventListener('blur', saveEndpointConfig);
        els.params.addEventListener('blur', saveEndpointConfig);
      }

      if (els.personaId) {
        els.personaId.addEventListener('blur', async () => {
          const value = els.personaId.value.trim();
          if (value === lastSavedPersonaId) return;
          try {
            await putSchedule(card.id, { persona_id: value });
            lastSavedPersonaId = value;
            sectionValues.persona_id = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save persona: ${err.message}`, true);
            els.personaId.value = lastSavedPersonaId;
          }
        });
      }

      if (els.modelId) {
        els.modelId.addEventListener('change', async () => {
          const value = els.modelId.value;
          if (value === lastSavedModelId) return;
          try {
            await putSchedule(card.id, { model_id: value });
            lastSavedModelId = value;
            sectionValues.model_id = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save model: ${err.message}`, true);
            els.modelId.value = lastSavedModelId;
          }
        });
      }

      if (els.effort) {
        els.effort.addEventListener('change', async () => {
          const value = els.effort.value;
          if (value === lastSavedEffort) return;
          try {
            await putSchedule(card.id, { effort: value });
            lastSavedEffort = value;
            sectionValues.effort = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save effort: ${err.message}`, true);
            els.effort.value = lastSavedEffort;
          }
        });
      }

      if (els.host) {
        els.host.addEventListener('change', async () => {
          const value = els.host.value;
          if (value === lastSavedHost) return;
          try {
            await putSchedule(card.id, { host: value });
            lastSavedHost = value;
            sectionValues.host = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save host: ${err.message}`, true);
            els.host.value = lastSavedHost;
          }
        });
      }

      if (els.workingDir) {
        els.workingDir.addEventListener('blur', async () => {
          const value = els.workingDir.value.trim();
          if (value === lastSavedWorkingDir) return;
          try {
            await putSchedule(card.id, { working_dir: value });
            lastSavedWorkingDir = value;
            sectionValues.working_dir = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save working directory: ${err.message}`, true);
            els.workingDir.value = lastSavedWorkingDir;
          }
        });
      }

      // Blank leaves the stored budget alone (there is no way to clear an
      // already-set budget from the drawer) rather than sending nothing
      // meaningful — the PUT only ever carries a number here.
      if (els.budgetDollars) {
        els.budgetDollars.addEventListener('blur', async () => {
          const raw = els.budgetDollars.value.trim();
          if (raw === '') return;
          const value = Number(raw);
          if (!Number.isFinite(value) || value < 0) {
            showToast('Budget must be a non-negative number', true);
            els.budgetDollars.value = lastSavedBudgetDollars != null ? String(lastSavedBudgetDollars) : '';
            return;
          }
          if (value === lastSavedBudgetDollars) return;
          try {
            await putSchedule(card.id, { budget_dollars: value });
            lastSavedBudgetDollars = value;
            sectionValues.budget_dollars = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save budget: ${err.message}`, true);
            els.budgetDollars.value = lastSavedBudgetDollars != null ? String(lastSavedBudgetDollars) : '';
          }
        });
      }

      if (els.wallMinutes) {
        els.wallMinutes.addEventListener('blur', async () => {
          const raw = els.wallMinutes.value.trim();
          if (raw === '') return;
          const minutes = Number(raw);
          if (!Number.isFinite(minutes) || minutes < 0) {
            showToast('Wall time must be a non-negative number of minutes', true);
            els.wallMinutes.value = lastSavedWallSeconds != null ? String(Math.round(lastSavedWallSeconds / 60)) : '';
            return;
          }
          const value = Math.round(minutes * 60);
          if (value === lastSavedWallSeconds) return;
          try {
            await putSchedule(card.id, { wall_seconds: value });
            lastSavedWallSeconds = value;
            sectionValues.wall_seconds = value;
            await fetchBoard();
          } catch (err) {
            showToast(`Couldn't save wall time: ${err.message}`, true);
            els.wallMinutes.value = lastSavedWallSeconds != null ? String(Math.round(lastSavedWallSeconds / 60)) : '';
          }
        });
      }
    }
    wireActionSectionFields();

    const enabledEl = drawerEl.querySelector('[data-field="enabled"]');
    enabledEl.addEventListener('change', async () => {
      try {
        const resp = await putSchedule(card.id, { enabled: enabledEl.checked });
        // The store recomputes next_trigger_at for an enabled change too
        // (clearing it on disable) — refresh the preview from the
        // response the same way the type/value/timezone saves do, since
        // updateOpenDrawer skips its own rebuild while this checkbox
        // holds focus.
        previewEl.textContent = formatNextFire(resp.next_trigger_at, lastSavedType);
        await fetchBoard();
      }
      catch (err) { showToast(`Couldn't update enabled: ${err.message}`, true); enabledEl.checked = !!card.enabled; }
    });

    const typeEl = drawerEl.querySelector('[data-field="schedule-type"]');
    const valueGroupEl = drawerEl.querySelector('[data-field="schedule-value-group"]');
    const valueEl = drawerEl.querySelector('[data-field="schedule-value"]');
    const valueLabelEl = drawerEl.querySelector('[data-field="schedule-value-label"]');
    const valueErrorEl = drawerEl.querySelector('[data-field="schedule-value-error"]');
    const tzEl = drawerEl.querySelector('[data-field="timezone"]');
    const tzErrorEl = drawerEl.querySelector('[data-field="timezone-error"]');
    const actionEl = drawerEl.querySelector('[data-field="action"]');
    const actionErrorEl = drawerEl.querySelector('[data-field="action-error"]');
    const previewEl = drawerEl.querySelector('[data-field="next-fire-preview"]');
    const lastRunEl = drawerEl.querySelector('[data-field="last-run-info"]');
    const triggerBtnEl = drawerEl.querySelector('[data-action="trigger-now"]');

    previewEl.textContent = formatNextFire(card.next_fire_at, card.schedule_type);
    lastRunEl.textContent = formatLastRun(card.last_run);

    // What the server last actually accepted for each field — a rejected
    // save reverts its control to these, not to whatever was showing when
    // the drawer opened, the same rule the task drawer's assignee select
    // follows above.
    let lastSavedType = card.schedule_type;
    let lastSavedValue = card.schedule_value;
    let lastSavedTz = card.timezone || '';
    let lastSavedAction = card.action;
    // An action selected in the drawer but not yet included in a
    // successful PUT — set when the target action's own section doesn't
    // yet have what it needs to fire (see `actionInputsSatisfied`), and
    // cleared once a save carrying it succeeds. Never persisted anywhere
    // else, so closing and reopening the drawer (a fresh `renderDrawer`
    // from the card's actual stored action) discards it.
    let pendingAction = null;

    function showActionError(message) {
      actionErrorEl.textContent = message;
      actionErrorEl.hidden = false;
    }
    function clearActionError() {
      actionErrorEl.hidden = true;
      actionErrorEl.textContent = '';
    }

    function updateValueLabel(type) {
      valueGroupEl.hidden = type === 'manual';
      if (type === 'once') {
        valueLabelEl.textContent = 'When (ISO datetime)';
        valueEl.placeholder = '2026-06-03T15:05:00';
      } else {
        valueLabelEl.textContent = 'Cron expression';
        valueEl.placeholder = '0 9 * * *';
      }
    }

    typeEl.addEventListener('change', async () => {
      // Type-only, with no matching value, is unsaveable by construction
      // for cron/once (a cron string and an ISO datetime never parse as
      // each other) — saving it here would either write a type/value pair
      // the server rejects, or one it accepts but that leaves a live
      // schedule pointed at the wrong parser. So switching to cron/once
      // only updates the label, placeholder, and visibility; the value
      // field's blur handler below carries the type along with whatever
      // value the operator enters to match it, so a conversion always
      // reaches the server as one matched pair.
      //
      // `manual` has no value to match, so it saves immediately on
      // selection — there's nothing to wait for a blur on.
      const target = typeEl.value;
      updateValueLabel(target);
      if (target !== 'manual') return;
      try {
        const resp = await putSchedule(card.id, { schedule_type: 'manual' });
        lastSavedType = 'manual';
        lastSavedValue = '';
        valueEl.value = '';
        valueErrorEl.hidden = true;
        valueErrorEl.textContent = '';
        previewEl.textContent = formatNextFire(resp.next_trigger_at, 'manual');
        triggerBtnEl.textContent = 'Trigger now';
        await fetchBoard();
      } catch (err) {
        showToast(`Couldn't convert to manual: ${err.message}`, true);
        typeEl.value = lastSavedType;
        updateValueLabel(lastSavedType);
      }
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
        previewEl.textContent = formatNextFire(resp.next_trigger_at, lastSavedType);
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
        previewEl.textContent = formatNextFire(resp.next_trigger_at, lastSavedType);
        await fetchBoard();
      } catch (err) {
        tzErrorEl.textContent = err.message;
        tzErrorEl.hidden = false;
        tzEl.value = lastSavedTz;
      }
    });

    actionEl.addEventListener('change', async () => {
      // Switch the visible section immediately, ahead of any save, then
      // re-wire the freshly rendered fields' own save-on-blur/change
      // handlers, since the section rebuild replaced their DOM elements.
      const target = actionEl.value;
      sections.setAction(target);
      wireActionSectionFields();
      clearActionError();
      if (!actionInputsSatisfied(target, sectionValues)) {
        // The target action's section doesn't have what it needs to fire
        // yet (e.g. endpoint with no method/path, or notify/prompt/agent
        // with a blank message) — hold the switch locally instead of
        // writing an action the server would reject anyway. The section's
        // own save-on-blur/change handler above sends `action` together
        // with whatever value satisfies it, in one PUT.
        pendingAction = target;
        return;
      }
      pendingAction = null;
      try {
        await putSchedule(card.id, { action: target });
        lastSavedAction = target;
        await fetchBoard();
      } catch (err) {
        // Leave the select and its freshly rendered section showing the
        // operator's own choice — the stored action is unchanged, and a
        // later edit to the section's own field retries the switch.
        pendingAction = target;
        showActionError(err.message);
      }
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
  // Reassign, Mark Done, Snooze, Unsnooze, Cancel, Delete.
  // Which of these apply and whether each is
  // enabled or disabled-with-a-reason is decided once, by
  // session_actions.js's `decideActions`, and rendered by its
  // `renderActionRow` — the exact same function the Graph tab's side panel
  // uses for its own header, so the two surfaces can't disagree about a
  // shared session. Go To/Resume/Kill/Answer/Snooze's own picker UI are
  // built into `renderActionRow` itself (it owns Kill's cascade-preview
  // modal, Resume's host select, Go To's "Locating…" state, and Snooze's
  // preset/duration/date-time picker); Open, Accept, Reject, Reassign,
  // Mark Done, Snooze's write, Unsnooze, and Delete come from
  // ./card_actions.js, shared with a
  // card-linked Graph tab side panel — Cancel and Delete are overridden
  // below with the extra drawer-specific bookkeeping (closing/rebuilding
  // this drawer) that a bare handoff to `fetchBoard` doesn't cover.
  function renderDrawerActions(card) {
    const actionsEl = drawerEl.querySelector('[data-field="actions"]');
    if (!actionsEl) return;
    const cardHandlers = cardActionHandlers(card, {
      findCard,
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
  // leaves the field, using the latest board already applied by applyBoard.
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
      applyLaneSelection(visibleLanes.has('done')
        ? [...visibleLanes].filter(id => id !== 'done')
        : [...visibleLanes, 'done']);
    });
  }

  // ------------------------------------------------------------------
  // Bulk actions — fans out over the same per-card endpoints the
  // drawer and tray already use. Every action collects one {card, ok,
  // reason} outcome per selected card (`fanOut`, above) and shows exactly
  // one summary toast — never a toast, and for Delete never a confirmation
  // modal, per card. No undo toast: unlike a single-card action, a bulk
  // action has no single prior state to offer restoring.
  //
  // In-flight guard: a single `bulkActionInFlight` flag, checked and set
  // atomically (synchronously, before any `await`) at the top of each of
  // the four consequential entry points below — the three fan-out runners
  // and the delete confirmation's own open — so a second click dispatched
  // before the first has finished (a double-click, or two clicks while a
  // slow/loaded server holds the first fan-out's requests open) sees the
  // flag already set and does nothing, rather than starting a second,
  // fully independent fan-out or stacking a second confirmation modal. The
  // guard also disables all four bar buttons for its duration — covering
  // Delete's confirmation modal from the moment it opens through its own
  // cancel or fan-out, not just the fan-out itself — and always releases
  // in a `finally`/on every dismissal path, including a thrown error.
  // ------------------------------------------------------------------

  // Same write shape as the drawer's Assignee select and assignment.js's
  // own engine picker: drop any existing assignee tag and stamp the new
  // one (or nothing, for unassigned), keeping every other tag — including
  // protected lifecycle tags — untouched. Goes through the plain task PUT
  // (not the lane endpoint), which runs the same assignee-change policy
  // check (api/routes/tasks.py) so a claimed card still 409s per card.
  let bulkActionInFlight = false;

  function setBulkButtonsDisabled(disabled) {
    [bulkTagBtn, bulkAssignBtn, bulkDoneBtn, bulkDeleteBtn].forEach(btn => {
      if (btn) btn.disabled = disabled;
    });
  }

  // Returns `false` (and does nothing) when a bulk action is already
  // running — the caller must bail immediately without starting any work.
  // Returns `true` once the flag is claimed, having already disabled the
  // bar's buttons so a disabled second click can't even reach its own
  // handler.
  function beginBulkAction() {
    if (bulkActionInFlight) return false;
    bulkActionInFlight = true;
    setBulkButtonsDisabled(true);
    return true;
  }

  function endBulkAction() {
    bulkActionInFlight = false;
    setBulkButtonsDisabled(false);
  }

  async function bulkAssignOne(card, assignee) {
    const nonAssigneeTags = (card.tags || []).filter(t => !ASSIGNEES.includes(String(t).toLowerCase()));
    const tags = assignee ? [assignee, ...nonAssigneeTags] : nonAssigneeTags;
    await putTask(card.id, { tags });
  }

  async function runBulkAssign(assignee) {
    const cards = selectedTaskCards();
    if (cards.length === 0) return;
    closeBulkPopovers();
    if (!beginBulkAction()) return;
    try {
      const results = await fanOut(cards, (card) => bulkAssignOne(card, assignee));
      reportBulkOutcome('Assigned', results);
      await fetchBoard();
      clearSelection();
    } finally {
      endBulkAction();
    }
  }

  // Adds one tag through the same endpoint (and merge behavior) as the
  // drawer's tag picker — `putBoardTags` sends only the editable tag set,
  // and the server preserves every protected tag already on the card
  // (update_board_card_tags's `update_tags_preserving`). ADD only, matching
  // by design — no bulk untagging.
  async function bulkTagOne(card, tag) {
    const current = editableTagsForCard(card);
    if (current.includes(tag)) return;  // already has it — counts as success
    await putBoardTags(card.id, [...current, tag]);
  }

  async function runBulkTag(rawTag) {
    const tag = normalizeEditableTag(rawTag);
    if (!tag) return;
    const cards = selectedTaskCards();
    if (cards.length === 0) return;
    closeBulkPopovers();
    if (!beginBulkAction()) return;
    try {
      const results = await fanOut(cards, (card) => bulkTagOne(card, tag));
      reportBulkOutcome('Tagged', results);
      await fetchBoard();
      clearSelection();
    } finally {
      endBulkAction();
    }
  }

  // Raw per-card writes for the bulk fan-out — unlike `moveCard` (used by
  // drag/drop and the drawer), these never toast or refresh the board on
  // their own: a bulk action collects every card's outcome first and shows
  // exactly one summary toast, then one `fetchBoard()`, so a per-card
  // helper here must have no side effects beyond the network call itself.
  async function acceptCardEndpoint(cardId) {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(cardId)}/accept`, { method: 'POST' });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return r.json();
  }

  async function moveCardToLaneEndpoint(cardId, lane) {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(cardId)}/lane`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lane }),
    });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return r.json();
  }

  // Mark Done: a Review card goes through the same Accept transition as the
  // inline Accept button and the tray's Done target; every other card goes
  // through the plain lane-move endpoint, same as dragging it onto Done. A
  // card already in Done is a no-op that counts as success.
  async function runBulkMarkDone() {
    const cards = selectedTaskCards();
    if (cards.length === 0) return;
    if (!beginBulkAction()) return;
    try {
      const results = await fanOut(cards, async (card) => {
        if (card.lane === 'done') return;
        if (card.lane === 'review') await acceptCardEndpoint(card.id);
        else await moveCardToLaneEndpoint(card.id, 'done');
      });
      reportBulkOutcome('Marked done', results);
      await fetchBoard();
      clearSelection();
    } finally {
      endBulkAction();
    }
  }

  // One confirmation naming the count, then the same kill-then-delete core
  // the single-card Delete modal uses (card_actions.js's `deleteCard`) —
  // never a modal per card.
  function openBulkDeleteConfirm() {
    const cards = selectedTaskCards();
    if (cards.length === 0) return;
    // Claimed from the moment the confirmation opens, not just while its
    // fan-out runs — a second click on Delete while this modal is still
    // sitting open (awaiting a confirm/cancel) must not stack a second one.
    if (!beginBulkAction()) return;
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="bulk-delete-title">
        <h2 id="bulk-delete-title">Delete ${cards.length} card${cards.length === 1 ? '' : 's'}?</h2>
        <div class="descendants">A selected card with a live, killable session is killed first. This can't be undone.</div>
        <div class="actions">
          <button id="bulk-delete-cancel">Cancel</button>
          <button class="danger" id="bulk-delete-confirm">Delete</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
    const cancel = () => { cleanup(); endBulkAction(); };
    backdrop.addEventListener('click', e => { if (e.target === backdrop) cancel(); });
    backdrop.querySelector('#bulk-delete-cancel').onclick = cancel;
    backdrop.querySelector('#bulk-delete-confirm').onclick = async () => {
      const btn = backdrop.querySelector('#bulk-delete-confirm');
      btn.disabled = true;
      btn.textContent = 'Deleting…';
      try {
        const results = await fanOut(cards, (card) => deleteCard(card, { findCard }));
        cleanup();
        reportBulkOutcome('Deleted', results);
        await fetchBoard();
        clearSelection();
      } finally {
        endBulkAction();
      }
    };
  }

  function renderBulkAssignPopover() {
    if (!bulkAssignPopover) return;
    bulkAssignPopover.innerHTML = `
      <button type="button" class="board-bulk-popover-option" data-assignee="">unassigned</button>
      ${ASSIGNEES.map(a => `<button type="button" class="board-bulk-popover-option" data-assignee="${escapeAttr(a)}">${escapeHtml(a)}</button>`).join('')}
    `;
    bulkAssignPopover.querySelectorAll('[data-assignee]').forEach(button => {
      button.addEventListener('click', () => runBulkAssign(button.dataset.assignee || null));
    });
  }

  const bulkTagSearchEl = bulkTagPopover && bulkTagPopover.querySelector('[data-field="tag-search"]');
  const bulkTagOptionsEl = bulkTagPopover && bulkTagPopover.querySelector('[data-field="options"]');

  function renderBulkTagOptions(query) {
    if (!bulkTagOptionsEl) return;
    const q = String(query || '').trim().replace(/^#+/, '').toLowerCase();
    const matches = availableEditableTags().filter(tag => !q || tag.includes(q));
    const normalizedQuery = normalizeEditableTag(query);
    const canCreate = !!normalizedQuery && !availableEditableTags().includes(normalizedQuery);
    bulkTagOptionsEl.innerHTML = matches.map(tag => (
      `<button type="button" class="board-bulk-popover-option" data-tag="${escapeAttr(tag)}">#${escapeHtml(tag)}</button>`
    )).join('') + (canCreate
      ? `<button type="button" class="board-bulk-popover-option board-bulk-popover-create" data-tag="${escapeAttr(normalizedQuery)}">Create new #${escapeHtml(normalizedQuery)}</button>`
      : '');
    bulkTagOptionsEl.querySelectorAll('[data-tag]').forEach(button => {
      button.addEventListener('click', () => runBulkTag(button.dataset.tag));
    });
  }

  if (bulkTagSearchEl) {
    bulkTagSearchEl.addEventListener('input', () => renderBulkTagOptions(bulkTagSearchEl.value));
    bulkTagSearchEl.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      runBulkTag(bulkTagSearchEl.value);
    });
  }

  if (bulkTagBtn) {
    bulkTagBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (bulkAssignPopover) bulkAssignPopover.hidden = true;
      if (!bulkTagPopover) return;
      const opening = bulkTagPopover.hidden;
      bulkTagPopover.hidden = !opening;
      if (opening) {
        if (bulkTagSearchEl) bulkTagSearchEl.value = '';
        renderBulkTagOptions('');
        if (bulkTagSearchEl) bulkTagSearchEl.focus();
      }
    });
  }
  if (bulkAssignBtn) {
    bulkAssignBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (bulkTagPopover) bulkTagPopover.hidden = true;
      if (!bulkAssignPopover) return;
      const opening = bulkAssignPopover.hidden;
      if (opening) renderBulkAssignPopover();
      bulkAssignPopover.hidden = !opening;
    });
  }
  document.addEventListener('click', (e) => {
    if (bulkAssignPopover && !bulkAssignPopover.hidden
      && !bulkAssignPopover.contains(e.target) && e.target !== bulkAssignBtn) {
      bulkAssignPopover.hidden = true;
    }
    if (bulkTagPopover && !bulkTagPopover.hidden
      && !bulkTagPopover.contains(e.target) && e.target !== bulkTagBtn) {
      bulkTagPopover.hidden = true;
    }
  });
  if (bulkDoneBtn) bulkDoneBtn.addEventListener('click', () => runBulkMarkDone());
  if (bulkDeleteBtn) bulkDeleteBtn.addEventListener('click', () => openBulkDeleteConfirm());
  if (bulkClearBtn) bulkClearBtn.addEventListener('click', () => clearSelection());

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
  if (projectFilterEl) projectFilterEl.addEventListener('change', () => {
    projectFilter = projectFilterEl.value;
    updateFilterSummary(getFilters());
    render();
  });
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
    projectFilter = 'all';
    if (projectFilterEl) projectFilterEl.value = projectFilter;
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
    if (projectFilter !== 'all') active.push(projectFilter);
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
    renderAssigneeDrops();
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
