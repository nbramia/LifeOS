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
import { newChat, loadConversation } from './conversations.js';

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

// Switch the view to the selected backend's stored conversation. LifeOS owns its
// conversation history (render it); agent/hermes history isn't LifeOS-owned, so
// we keep a fresh view but retain its id for turn continuity.
function restoreBackendConversation() {
  const storedId = getStoredConversationId();
  if (storedId && getBackendMode() === 'lifeos') {
    loadConversation(storedId);
  } else {
    newChat();
    state.currentConversationId = storedId;  // continue the agent/hermes thread on next turn
  }
}

// GET a backend's /status endpoint and report whether it's configured.
// Bounded by STATUS_TIMEOUT_MS and never throws — any failure (network error,
// non-2xx, timeout) reports unavailable, which is exactly the "fall back to
// lifeos" behavior #587 requires.
async function checkAvailable(url) {
  if (!url) return false;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    if (!resp.ok) return false;
    const data = await resp.json();
    return !!data.available;
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export async function initBackend() {
  // Block the composer until the default resolves (below) so a turn sent in
  // the resolution window can't land on the wrong backend.
  state.isLoading = true;
  if (elements.sendBtn) elements.sendBtn.disabled = true;

  const [agentAvailable, hermesAvailable] = await Promise.all([
    checkAvailable(endpoints.agentStatus),
    checkAvailable(endpoints.hermesStatus),
  ]);
  document.body.classList.toggle('agent-available', agentAvailable);
  document.body.classList.toggle('hermes-available', hermesAvailable);

  // Stored preference wins; otherwise default to hermes if available, else
  // lifeos. A stored preference for a backend that's no longer configured
  // (e.g. disabled since the last visit) must not strand the UI on a hidden
  // option, so it's re-validated against availability too.
  let mode = getStoredBackendMode();
  if (!mode) {
    mode = hermesAvailable ? 'hermes' : 'lifeos';
  } else if (mode === 'agent' && !agentAvailable) {
    mode = 'lifeos';
  } else if (mode === 'hermes' && !hermesAvailable) {
    mode = 'lifeos';
  }
  currentMode = mode;
  config.backend = mode === 'lifeos' ? null : mode;

  if (elements.backendLifeos) elements.backendLifeos.addEventListener('click', () => setBackendMode('lifeos'));
  if (elements.backendAgent) elements.backendAgent.addEventListener('click', () => setBackendMode('agent'));
  if (elements.backendHermes) elements.backendHermes.addEventListener('click', () => setBackendMode('hermes'));
  applyBackendUi();

  // Restore the current backend's conversation id (continuity across refresh).
  state.currentConversationId = getStoredConversationId();

  state.isLoading = false;
  if (elements.sendBtn) elements.sendBtn.disabled = false;
}
