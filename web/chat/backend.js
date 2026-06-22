// LifeOS | Agent text-backend toggle + per-backend conversation persistence
// (#361, PR-D).
//
// The composer can target two text backends: `lifeos` (the orchestrator, with
// personas + handoff) or `agent` (the OpenClaw voice-adapter, proxied with a
// server-side bearer at /api/agent/ask/stream — no persona, no handoff). The
// selection persists in sessionStorage, and each backend keeps its own
// conversation id (lifeos is further scoped per persona) so switching back and
// forth — and refreshing — continues the right thread.

import { state, config, elements, endpoints } from './session.js';
import { newChat, loadConversation } from './conversations.js';

const BACKEND_MODE_KEY = 'lifeos:chat:backend_mode';

export function getBackendMode() {
  try {
    return window.sessionStorage.getItem(BACKEND_MODE_KEY) === 'agent' ? 'agent' : 'lifeos';
  } catch (e) {
    return 'lifeos';
  }
}

// Conversation id is stored per backend; lifeos is also scoped per persona.
function convKey() {
  if (getBackendMode() === 'agent') return 'lifeos:chat:conv:agent';
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
  const agent = getBackendMode() === 'agent';
  // CSS hides the persona picker in agent mode (no personas there).
  document.body.classList.toggle('agent-mode', agent);
  if (elements.backendLifeos) elements.backendLifeos.classList.toggle('active', !agent);
  if (elements.backendAgent) elements.backendAgent.classList.toggle('active', agent);
}

function setBackendMode(mode) {
  if (state.isLoading) return;  // don't switch backends mid-turn (would store the id under the wrong key)
  const next = mode === 'agent' ? 'agent' : 'lifeos';
  if (next === getBackendMode()) return;
  // config.backend drives ask/stream routing + voice turns; null = lifeos
  // default (omitted from the request body, byte-identical to pre-#361).
  config.backend = next === 'agent' ? 'agent' : null;
  try { window.sessionStorage.setItem(BACKEND_MODE_KEY, next); } catch (e) { /* blocked */ }
  applyBackendUi();
  restoreBackendConversation();
}

// Switch the view to the selected backend's stored conversation. LifeOS owns its
// conversation history (render it); the agent's history isn't LifeOS-owned, so
// we keep a fresh view but retain its id for turn continuity.
function restoreBackendConversation() {
  const storedId = getStoredConversationId();
  if (storedId && getBackendMode() === 'lifeos') {
    loadConversation(storedId);
  } else {
    newChat();
    state.currentConversationId = storedId;  // continue the agent thread on next turn
  }
}

export async function initBackend() {
  config.backend = getBackendMode() === 'agent' ? 'agent' : null;
  if (elements.backendLifeos) elements.backendLifeos.addEventListener('click', () => setBackendMode('lifeos'));
  if (elements.backendAgent) elements.backendAgent.addEventListener('click', () => setBackendMode('agent'));
  applyBackendUi();

  // Restore the current backend's conversation id (continuity across refresh).
  state.currentConversationId = getStoredConversationId();

  // Only expose the Agent toggle when the backend is configured server-side.
  try {
    const resp = await fetch(endpoints.agentStatus);
    if (resp.ok) {
      const data = await resp.json();
      document.body.classList.toggle('agent-available', !!data.available);
    }
  } catch (e) {
    /* leave the Agent toggle hidden */
  }
}
