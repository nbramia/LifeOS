// Chat module entry point (#358). Wires the extracted modules together and
// bridges them to the classic index.html shell script:
//
//   - `window.lifeChat` exposes the shared state and `initChat` so the shell can
//     boot the chat surface (passing the DOM elements, endpoints, and the
//     agent-thread hook it still owns) and read/write shared chat state.
//   - `window.*` function shims keep the inline `on*=` handlers in index.html
//     working unchanged (delegated-listener migration is a tracked follow-up).
//
// Both are installed at module load — before the shell's DOMContentLoaded
// handler runs — so the bridge exists by the time any shell code touches it.

import { state, config, elements, endpoints, hooks } from './session.js';
import { addMessage, toggleSources, setStatus } from './thread.js';
import { setupAttachmentHandlers, openFilePicker, removeAttachment } from './attachments.js';
import {
  setupSwipeGestures, toggleSidebar, closeSidebar, newChat,
  filterConversations, loadConversation, deleteConversation,
} from './conversations.js';
import { sendMessage, askQuestion, stopTurn } from './ask-stream.js';
import { loadPersonas, onPersonaChange, personaOrchestrates, personaSupportsHandoff } from './persona.js';
import { initVoice, toggleVoiceMode, submitTurn } from './voice.js';
import { initBackend } from './backend.js';
import { initModel, onModelChange } from './model.js';

// Boot the chat surface. The shell passes in the explicit DOM element map (so
// the modules never getElementById), the API endpoints, and integration hooks
// (onAgentThreadReply — the #236 reply path still lives in the shell).
export function initChat({ elements: els, endpoints: eps, hooks: hks } = {}) {
  Object.assign(elements, els || {});
  Object.assign(endpoints, eps || {});
  Object.assign(hooks, hks || {});

  const { inputField } = elements;

  // Auto-resize textarea
  inputField.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 200) + 'px';
  });

  // Enter to send (Shift+Enter for newline)
  inputField.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  setupAttachmentHandlers();
  setupSwipeGestures();
  // loadPersonas() resolves config.personaId synchronously (from sessionStorage,
  // validated against /api/personas) and then loads the persona-scoped
  // conversation sidebar. It runs BEFORE initBackend() so the per-backend
  // conversation key (which is persona-scoped for lifeos) reads the right
  // persona on a refresh.
  loadPersonas();
  // LifeOS|Agent|Hermes selector + restore per-backend conversation (#361,
  // #587). The promise is stashed on the bridge (below) so tests can await
  // "default resolution actually happened" instead of polling UI state that
  // can look identical before and after (e.g. the lifeos default matches
  // index.html's initial markup).
  window.lifeChat.backendReady = initBackend();
  initModel();  // restore the per-turn model picker (Auto/Sonnet/Opus/Gemma)
  initVoice();  // restore Voice|Text mode + wire the hold-to-talk dock (#361)
  setStatus('', 'Ready');
  inputField.focus();
}

// --- Bridge for the classic shell script + inline handlers ---
// personaOrchestrates/personaSupportsHandoff are exposed read-only for tests
// (a browser-test truth table across backends, #596) — nothing in the app
// itself needs them off this bridge; ask-stream.js/voice.js import them
// directly as module functions.
window.lifeChat = { state, config, initChat, personaOrchestrates, personaSupportsHandoff };

Object.assign(window, {
  // thread.js
  addMessage, toggleSources,
  // conversations.js
  toggleSidebar, closeSidebar, newChat, filterConversations,
  loadConversation, deleteConversation,
  // attachments.js
  openFilePicker, removeAttachment,
  // ask-stream.js
  sendMessage, askQuestion, stopTurn,
  // persona.js
  onPersonaChange,
  // model.js
  onModelChange,
  // voice.js
  toggleVoiceMode,
});

// Voice helpers for the headless test harness (mic/audio can't run headless).
window.lifeChatVoice = { submitTurn };
