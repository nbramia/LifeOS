// Turn recovery: catching up on replies the server finished (or is still
// producing) while this page wasn't watching.
//
// A chat turn is owned by the server, not by the stream that started it, and
// further turns can wait in a server-side queue behind it (`if_busy: "queue"`).
// So a closed tab, a backgrounded phone, or a dropped connection never loses a
// reply — it just has to be fetched. This module:
//
//   - tracks the text turns this page is streaming right now (`localTurns`),
//     so recovery never fights a live stream;
//   - watches a conversation: shows a thinking indicator and polls
//     GET /api/conversations/{id} until `active_turn` is null and
//     `queued_turns` is 0, re-rendering the persisted thread as messages land;
//   - checks the current conversation when the page becomes visible again
//     (visibilitychange, pageshow) and after a load;
//   - finds the conversation a brand-new-conversation turn created when the
//     page closed before its `conversation_id` frame arrived.

import { state, config, elements, endpoints } from './session.js';
import { setStatus } from './thread.js';
import { renderConversation, loadConversation } from './conversations.js';
import {
  getPendingNewTurn, clearPendingNewTurn, setStoredConversationId, getBackendMode,
} from './backend.js';

const POLL_MS = 2500;
const SLOW_POLL_MS = 5000;
const SLOW_AFTER_MS = 60 * 1000;
// Each turn is bounded server-side by its detached-lifetime deadline; this
// cap only keeps a wedged poll from running forever.
const WATCH_MAX_MS = 20 * 60 * 1000;
const PENDING_NEW_TURN_MAX_AGE_MS = 15 * 60 * 1000;
const INDICATOR_ID = 'turn-recovery-indicator';

// Testability hook (same pattern as backend.js's STATUS_TIMEOUT_MS): a browser
// test shortens the poll interval instead of waiting out the real one.
function pollInterval(elapsed) {
  const override = typeof window !== 'undefined' && window.__LIFEOS_TEST_RECOVERY_POLL_MS__;
  if (override) return override;
  return elapsed > SLOW_AFTER_MS ? SLOW_POLL_MS : POLL_MS;
}

// --- text turns this page is streaming ---

// Each entry: {controller: AbortController, conversationId: string|null}.
export const localTurns = new Set();

export function hasLocalTurns(conversationId) {
  if (conversationId === undefined) return localTurns.size > 0;
  for (const t of localTurns) if (t.conversationId === conversationId) return true;
  return false;
}

// Hand a conversation's live streams over to the watcher: abort them, marked
// so their own error handling stays quiet.
export function abortLocalTurns(conversationId) {
  for (const t of localTurns) {
    if (t.conversationId === conversationId) {
      t.handedOff = true;
      t.controller.abort();
    }
  }
}

// --- conversation polling ---

export function isBusy(data) {
  return !!(data && (data.active_turn || (data.queued_turns || 0) > 0));
}

export async function fetchConversation(id) {
  try {
    const resp = await fetch(`${endpoints.conversations}/${encodeURIComponent(id)}`);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (e) {
    return null;
  }
}

function renderedMessageCount() {
  if (!elements.messagesEl) return 0;
  return elements.messagesEl.querySelectorAll(
    `.message.user, .message.assistant:not(#${INDICATOR_ID})`,
  ).length;
}

function showIndicator() {
  if (!elements.messagesEl || document.getElementById(INDICATOR_ID)) return;
  const welcome = elements.messagesEl.querySelector('.welcome');
  if (welcome) welcome.remove();
  const el = document.createElement('div');
  el.className = 'message assistant';
  el.id = INDICATOR_ID;
  el.innerHTML = '<div class="message-content"><div class="typing"><span></span><span></span><span></span></div></div>';
  elements.messagesEl.appendChild(el);
  setStatus('loading', 'Thinking...');
}

function hideIndicator() {
  const el = document.getElementById(INDICATOR_ID);
  if (el) el.remove();
}

let currentWatch = null;

export function isWatching(conversationId) {
  return !!currentWatch && (conversationId === undefined || currentWatch.id === conversationId);
}

export function stopWatching() {
  currentWatch = null;
  hideIndicator();
  if (elements.stopBtn && !hasLocalTurns()) elements.stopBtn.classList.remove('visible');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Poll `id` until no turn is running or queued for it, re-rendering the
// thread whenever the persisted message count changes, and end on the
// persisted thread. With `until(data)`, the watch instead ends only once
// that also holds (or `maxMs` passes), and the thread is re-rendered only
// when it does — the voice path uses this to wait for a reply its proxy tee
// persists without leaving a running turn behind, while keeping its own
// failure/Retry affordance if the reply never lands. `indicator: false` skips
// the thinking bubble. `onSettled(data, satisfied)` runs once at the end.
export function watchConversation(id, { until = null, indicator = true, maxMs = WATCH_MAX_MS, onSettled } = {}) {
  if (!id) return;
  const token = {};
  currentWatch = { id, token };
  if (indicator) {
    showIndicator();
    if (elements.stopBtn) elements.stopBtn.classList.add('visible');
  }
  const startedAt = Date.now();
  (async () => {
    let last = null;
    let lastCount = null;
    let satisfied = false;
    while (true) {
      await sleep(pollInterval(Date.now() - startedAt));
      if (!currentWatch || currentWatch.token !== token) return;
      if (state.currentConversationId !== id) { stopWatching(); return; }
      const data = await fetchConversation(id);
      if (!currentWatch || currentWatch.token !== token) return;
      if (data) {
        last = data;
        satisfied = !isBusy(data) && (!until || until(data));
        // Re-render when the stored thread gains messages, or on the first
        // poll if the page shows fewer than are stored. A local bubble the
        // store doesn't have (a send that failed) stays until then.
        const count = (data.messages || []).length;
        const grew = lastCount === null ? count > renderedMessageCount() : count !== lastCount;
        lastCount = count;
        if (!until && grew) {
          renderConversation(data);
          if (indicator && !satisfied) showIndicator();
        }
        if (satisfied) break;
      }
      if (Date.now() - startedAt > maxMs) break;
    }
    if (!currentWatch || currentWatch.token !== token) return;
    stopWatching();
    // End on the persisted thread, replacing any placeholder a dropped
    // stream left behind.
    if (last && (!until || satisfied) && state.currentConversationId === id) renderConversation(last);
    if ((!until || satisfied) && !hasLocalTurns() && !state.isLoading) setStatus('', 'Ready');
    if (onSettled) onSettled(last, satisfied);
  })();
}

// --- returning to the page ---

// Called after a load and whenever the page becomes visible again. Brings the
// current conversation up to date and, if a turn is running or queued for
// it, keeps watching until it's done.
export async function checkCurrentConversation() {
  if (document.visibilityState === 'hidden') return;
  if (state.currentAgentThread || getBackendMode() === 'agent') return;
  const id = state.currentConversationId;
  if (!id) {
    await findPendingNewConversation();
    return;
  }
  if (isWatching(id)) return;
  const data = await fetchConversation(id);
  if (!data || state.currentConversationId !== id || isWatching(id)) return;

  if (hasLocalTurns(id)) {
    // A stream that is still "open" while the server reports nothing running
    // died silently while the page was hidden. Give a stream that is just
    // finishing a moment, then hand over to the persisted thread.
    if (isBusy(data)) return;
    await sleep(3000);
    if (!hasLocalTurns(id) || state.currentConversationId !== id) return;
    abortLocalTurns(id);
    loadConversation(id);
    return;
  }
  // A voice turn manages its own stream and recovery.
  if (state.isLoading) return;

  if (isBusy(data)) {
    renderConversation(data);
    watchConversation(id);
  } else if ((data.messages || []).length !== renderedMessageCount()) {
    renderConversation(data);
  }
}

// --- a new conversation whose id never reached the page ---

let pendingSearch = null;

// The page sent the first turn of a new conversation and closed (or lost the
// stream) before the `conversation_id` frame arrived. The server created the
// conversation as soon as it received the request, so look for it among this
// persona/backend's most recent conversations: the one whose first message
// is the pending question. A conversation that isn't there yet (the request
// was still on its way) is looked for again a few times in the background
// while the record is fresh, and opened when it shows up.
export function findPendingNewConversation() {
  if (!pendingSearch) {
    pendingSearch = searchPendingNewConversation().finally(() => { pendingSearch = null; });
    pendingSearch.then((found) => { if (!found) retryPendingSearch(); });
  }
  return pendingSearch;
}

const PENDING_RETRY_WINDOW_MS = 60 * 1000;
let pendingRetry = null;

function retryPendingSearch() {
  const pending = getPendingNewTurn();
  if (pendingRetry || !pending || Date.now() - (pending.at || 0) > PENDING_RETRY_WINDOW_MS) return;
  pendingRetry = setTimeout(async () => {
    pendingRetry = null;
    if (hasLocalTurns()) return;  // the user moved on and sent something new
    const found = await searchPendingNewConversation();
    if (!found) retryPendingSearch();
  }, pollInterval(0));
}

// Server timestamps are naive server-local times, read here as browser-local;
// this allowance absorbs clock skew between the two.
const PENDING_CLOCK_SKEW_MS = 2 * 60 * 1000;

async function searchPendingNewConversation() {
  const pending = getPendingNewTurn();
  if (!pending) return false;
  if (!pending.question || Date.now() - (pending.at || 0) > PENDING_NEW_TURN_MAX_AGE_MS) {
    clearPendingNewTurn();
    return false;
  }
  if (hasLocalTurns()) return false;  // the stream that will deliver the id is still open
  // The record belongs to this persona/backend and to whatever the page was
  // showing when the search began. If the user moves on while it runs, drop
  // it rather than open (or store an id under) a different selection.
  const personaId = config.personaId || 'primary';
  const backend = config.backend || 'lifeos';
  const startConversationId = state.currentConversationId;
  const unchanged = () => (config.personaId || 'primary') === personaId
    && (config.backend || 'lifeos') === backend
    && state.currentConversationId === startConversationId
    && !hasLocalTurns();
  const known = new Set(pending.knownIds || []);
  let list = [];
  try {
    const resp = await fetch(
      `${endpoints.conversations}?persona_id=${encodeURIComponent(personaId)}&backend=${encodeURIComponent(backend)}`,
    );
    if (resp.ok) list = (await resp.json()).conversations || [];
  } catch (e) {
    return false;
  }
  if (!unchanged()) return false;
  // Only a conversation created after the turn was sent can be its own: an
  // older one may open with the same question.
  const candidates = list.filter((conv) => {
    if (known.has(conv.id)) return false;
    const created = Date.parse(conv.created_at);
    return Number.isFinite(created) && created >= (pending.at || 0) - PENDING_CLOCK_SKEW_MS;
  });
  for (const conv of candidates.slice(0, 3)) {
    const data = await fetchConversation(conv.id);
    if (!unchanged()) return false;
    const first = data && (data.messages || [])[0];
    if (first && first.role === 'user' && first.content === pending.question) {
      clearPendingNewTurn();
      state.currentConversationId = conv.id;
      setStoredConversationId(conv.id);
      await loadConversation(conv.id, { refreshList: false });
      return true;
    }
  }
  return false;
}

// Wire the page-lifecycle triggers. Called once from initChat().
export function initTurnRecovery() {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkCurrentConversation();
  });
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) checkCurrentConversation();
  });
}
