// LifeOS | Agent | Hermes text-backend selector + per-backend conversation
// persistence (#361, PR-D; three-way selector added in #587).
//
// The composer can target three text backends: `lifeos` (the orchestrator, with
// personas + handoff), `agent` (the OpenClaw voice-adapter, no personas), and
// `hermes` (an agent harness, personas resolved server-side into a
// `lifeos_context` envelope — #590) — the latter two proxied with a
// server-side bearer at /api/agent/ask/stream and /api/hermes/ask/stream
// respectively. Neither proxied backend has handoff wired through. The
// selection persists in sessionStorage, and
// each backend keeps its own conversation id (lifeos and hermes are further
// scoped per persona) so switching back and forth — and refreshing — continues
// the right thread.
//
// With no stored preference, the default is conditional: hermes if it's
// configured server-side, else lifeos — so a machine with no Hermes URL set
// behaves exactly as it did before this file existed. initBackend() resolves
// that default (an async availability check) before the composer accepts a
// turn, via the same state.isLoading gate sendMessage() already respects.

import { state, config, elements, endpoints } from './session.js';
import { newChat, loadConversation, loadConversations } from './conversations.js';
import { updateOrchestratesBadge } from './persona.js';

const BACKEND_MODE_KEY = 'lifeos:chat:backend_mode';
const BACKEND_MODES = ['lifeos', 'agent', 'hermes'];
// Same-origin status checks; bounded so a wedged server can't hang the
// composer open indefinitely (falls back to lifeos on timeout, per #587).
// `window.__LIFEOS_TEST_STATUS_TIMEOUT_MS__` is a testability hook only — set
// via page.add_init_script() so a browser test can exercise the timeout path
// in milliseconds instead of waiting out the real 5s.
const STATUS_TIMEOUT_MS =
  (typeof window !== 'undefined' && window.__LIFEOS_TEST_STATUS_TIMEOUT_MS__) || 5000;

// The mode actually in effect. Starts at the pre-#587 safe default (lifeos)
// and is resolved by initBackend() before the composer accepts a turn.
let currentMode = 'lifeos';

export function getBackendMode() {
  return currentMode;
}

// The user's explicit choice, if any — distinct from getBackendMode(), which
// is the resolved-and-in-effect mode. Written only when the user picks a
// backend by hand (setBackendMode()), so the conditional default below never
// gets confused with an explicit preference.
function getStoredBackendMode() {
  try {
    const v = window.sessionStorage.getItem(BACKEND_MODE_KEY);
    return BACKEND_MODES.includes(v) ? v : null;
  } catch (e) {
    return null;
  }
}

// Conversation id is stored per backend; lifeos and hermes are also scoped per
// persona (hermes keeps the persona picker visible, unlike agent).
function convKey() {
  const mode = getBackendMode();
  if (mode === 'agent') return 'lifeos:chat:conv:agent';
  if (mode === 'hermes') return `lifeos:chat:conv:hermes:${config.personaId || 'primary'}`;
  return `lifeos:chat:conv:lifeos:${config.personaId || 'primary'}`;
}

export function getStoredConversationId() {
  try { return window.sessionStorage.getItem(convKey()) || null; } catch (e) { return null; }
}

export function setStoredConversationId(id) {
  if (!id) return;
  try { window.sessionStorage.setItem(convKey(), id); } catch (e) { /* storage blocked */ }
}

function applyBackendUi() {
  const mode = getBackendMode();
  // CSS hides the persona+model pickers in agent mode (no personas there) and
  // just the model picker in hermes mode (personas stay visible; #587).
  document.body.classList.toggle('agent-mode', mode === 'agent');
  document.body.classList.toggle('hermes-mode', mode === 'hermes');
  if (elements.backendLifeos) elements.backendLifeos.classList.toggle('active', mode === 'lifeos');
  if (elements.backendAgent) elements.backendAgent.classList.toggle('active', mode === 'agent');
  if (elements.backendHermes) elements.backendHermes.classList.toggle('active', mode === 'hermes');
  // personaOrchestrates() depends on the backend (excludes agent), so the
  // "runs on LifeOS" badge (#596) must be re-evaluated on every backend switch,
  // not just on persona change.
  updateOrchestratesBadge();
}

function setBackendMode(mode) {
  if (state.isLoading) return;  // don't switch backends mid-turn (would store the id under the wrong key)
  const next = BACKEND_MODES.includes(mode) ? mode : 'lifeos';
  if (next === currentMode) return;
  currentMode = next;
  // config.backend drives ask/stream routing + voice turns; null = lifeos
  // default (omitted from the request body, byte-identical to pre-#361).
  config.backend = next === 'lifeos' ? null : next;
  try { window.sessionStorage.setItem(BACKEND_MODE_KEY, next); } catch (e) { /* blocked */ }
  applyBackendUi();
  restoreBackendConversation();
}

// Switch the view to the selected backend's stored conversation. LifeOS and
// Hermes threads are both stored server-side (#592) and render the same way;
// the Agent backend's history genuinely lives elsewhere, so it keeps a fresh
// view and just retains the id for turn continuity.
function restoreBackendConversation() {
  const storedId = getStoredConversationId();
  if (storedId && getBackendMode() !== 'agent') {
    loadConversation(storedId);
  } else {
    newChat();
    state.currentConversationId = storedId;  // continue the agent thread on next turn
  }
}

// GET a backend's /status endpoint and report configured/reachable/available.
// Bounded by STATUS_TIMEOUT_MS and never throws — any failure (network error,
// non-2xx, timeout) reports everything false, which is exactly the "fall
// back to lifeos" behavior #587 requires.
//
// `configured`/`reachable` (#688) let the UI distinguish "not set up" (fully
// hidden, unchanged) from "set up but down" (visible, but not selectable —
// see applyBackendUi()). A backend whose /status doesn't send those fields
// (the Agent backend, as of this writing — it doesn't opt into the
// reachability probe server-side) falls back to `available` for both, so it
// behaves exactly as before this field split existed: configured and
// reachable are the same single check.
async function checkAvailable(url) {
  if (!url) return { available: false, configured: false, reachable: false };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    if (!resp.ok) return { available: false, configured: false, reachable: false };
    const data = await resp.json();
    const available = !!data.available;
    const configured = typeof data.configured === 'boolean' ? data.configured : available;
    const reachable = typeof data.reachable === 'boolean' ? data.reachable : available;
    return { available, configured, reachable };
  } catch (e) {
    return { available: false, configured: false, reachable: false };
  } finally {
    clearTimeout(timer);
  }
}

export async function initBackend(personasReady) {
  // Block the composer until the default resolves (below) so a turn sent in
  // the resolution window can't land on the wrong backend.
  state.isLoading = true;
  if (elements.sendBtn) elements.sendBtn.disabled = true;

  const [agentStatus, hermesStatus] = await Promise.all([
    checkAvailable(endpoints.agentStatus),
    checkAvailable(endpoints.hermesStatus),
  ]);
  document.body.classList.toggle('agent-available', agentStatus.available);
  document.body.classList.toggle('hermes-available', hermesStatus.available);
  // Drives visibility (unlike -available, stays true while merely down —
  // #688: a configured-but-unreachable Hermes must stay visible, marked
  // unavailable, not vanish indistinguishably from "never configured").
  document.body.classList.toggle('hermes-configured', hermesStatus.configured);
  // The one state that's visible but not selectable: configured, not reachable.
  const hermesDown = hermesStatus.configured && !hermesStatus.reachable;
  document.body.classList.toggle('hermes-down', hermesDown);
  if (elements.backendHermes) {
    elements.backendHermes.title = hermesDown
      ? 'Hermes backend (unavailable — unreachable)'
      : 'Hermes backend';
  }

  // Stored preference wins; otherwise default to hermes if available, else
  // lifeos. A stored preference for a backend that's no longer configured
  // (e.g. disabled since the last visit) must not strand the UI on a hidden
  // option, so it's re-validated against availability too. "Available" here
  // already means configured AND reachable (#688) — a down Hermes falls back
  // to lifeos exactly like an unconfigured one, never failing a turn at send
  // time.
  let mode = getStoredBackendMode();
  if (!mode) {
    mode = hermesStatus.available ? 'hermes' : 'lifeos';
  } else if (mode === 'agent' && !agentStatus.available) {
    mode = 'lifeos';
  } else if (mode === 'hermes' && !hermesStatus.available) {
    mode = 'lifeos';
  }
  currentMode = mode;
  config.backend = mode === 'lifeos' ? null : mode;

  if (elements.backendLifeos) elements.backendLifeos.addEventListener('click', () => setBackendMode('lifeos'));
  if (elements.backendAgent) elements.backendAgent.addEventListener('click', () => setBackendMode('agent'));
  if (elements.backendHermes) {
    elements.backendHermes.addEventListener('click', () => {
      // A visible-but-down Hermes (#688) is not clickable — selecting it
      // would just fail every turn at send time instead of the visible,
      // once-per-load degradation this state already represents.
      if (document.body.classList.contains('hermes-down')) return;
      setBackendMode('hermes');
    });
  }
  applyBackendUi();

  // The sidebar's single initial load (#607) — gated on BOTH resolutions, not
  // just this one. config.backend is resolved as of the assignment above, but
  // config.personaId is only *provisionally* set at this point (main.js's
  // synchronous restore, before loadPersonas() validates it against
  // /api/personas) — listing here unconditionally would still be able to fire
  // with a stale, since-deleted persona id if that validation hasn't finished
  // yet. Awaiting the caller's loadPersonas() promise (personasReady) does NOT
  // serialize the two fetches — loadPersonas()'s /api/personas request and the
  // Promise.all() above both started already, concurrently, before either
  // await point — it only delays *this* listing call until persona validation
  // has actually landed. Gating on both resolutions this way is also what
  // keeps this to one request on the common (no-Hermes, no-stale-persona)
  // path, instead of the two a "list early, then refresh" approach would send.
  // loadPersonas() is designed to never reject, but the listing must not be
  // held hostage to that guarantee holding in every browser forever (e.g. a
  // storage write throwing in Safari private mode) — a persona failure of any
  // kind must still produce a listing, so a rejection here is swallowed
  // rather than left to propagate and skip loadConversations() below.
  if (personasReady) await personasReady.catch(() => {});
  loadConversations();

  // Restore the current backend's conversation id (continuity across refresh).
  state.currentConversationId = getStoredConversationId();

  state.isLoading = false;
  if (elements.sendBtn) elements.sendBtn.disabled = false;
}
