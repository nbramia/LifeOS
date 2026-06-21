// Shared chat state for the /chat surface (#358).
//
// Extracted from the index.html SPA's single inline <script>. These objects are
// the one source of truth for chat state; the feature modules import them
// directly, and the classic shell script reaches the same objects through the
// `window.lifeChat` bridge installed in main.js. No persistence yet —
// sessionStorage (backend × persona × conversation) is a follow-on (#361).

// In-memory chat state. `attachments` and `allConversations` are reassigned in
// place (e.g. `state.attachments = state.attachments.filter(...)`), so they live
// on this object rather than as exported `let` bindings (which importers cannot
// reassign).
export const state = {
  currentConversationId: null,
  isLoading: false,
  // When set, the main chat view is showing an agent thread (#236): the
  // composer continues that thread instead of starting a normal chat query.
  // Shape: {sessionId, resumable, label, status, convCount}.
  currentAgentThread: null,
  sessionCost: 0,
  attachments: [], // {id, file, dataUrl, filename, mediaType, sizeBytes}
  allConversations: [], // all conversations, for client-side filtering
};

// Optional per-query selectors reserved for the persona (#359) and
// backend/Voice-Text (#361) follow-ons. Declared now so those PRs don't have to
// reshape the askStream/session interface; unused in this behavior-neutral PR.
export const config = {
  personaId: null,
  backend: null,
};

// DOM element handles, populated by initChat() (main.js). Modules read
// `elements.messagesEl` etc. at call time, never at module-load time.
export const elements = {};

// API endpoints, populated by initChat() so the surface stays configurable.
export const endpoints = {};

// Integration hooks the shell provides via initChat() — e.g. onAgentThreadReply
// (the agent-thread reply path, #236, which stays in the shell for now).
export const hooks = {};
