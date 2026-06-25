// Answer affordance for web/voice orchestrating-persona parity (#412).
//
// When an orchestrating persona (e.g. doctor) is selected on /chat and a message
// is sent, the SSE returns a "🩺 On it…" ack + `done` and the conversation is
// linked server-side to the spawned Claude Code session (#403). If that session
// emits a `[CLARIFY]`/`[GOAL]`, `GET /api/conversations/{id}` surfaces a
// `pending_question` ({session_id, question, kind}) while it awaits an answer.
//
// This module polls that GET for the active conversation and, when a question is
// present, renders an inline answer card below #messages. Submitting POSTs to
// `/api/conversations/{id}/answer` — the server deposits the answer onto the open
// question and the worker resumes the session via its existing path (the same
// resume a Telegram reply takes). It is purely additive: the normal chat stream,
// the Telegram round-trip, and existing SSE behavior are untouched.

import { state, endpoints } from './session.js';
import { escapeHtml, addMessage } from './thread.js';

const POLL_INTERVAL_MS = 4000;
const CARD_ID = 'pendingQuestionCard';

// Single in-flight poll loop. We track the conversation it belongs to so a
// navigation (loadConversation / newChat) auto-stops the stale loop, and so a
// restart for the already-polled conversation is a no-op.
let pollTimer = null;
let pollingConversationId = null;

// #311: ids of conversation messages already on screen, so a late-arriving
// message (the spawned session's streamed [NOTIFY] or terminal result) is
// rendered exactly once. Seeded on the FIRST poll from the messages already in
// the thread (those were rendered by askStream / loadConversation, not by us),
// then every subsequent poll appends only messages whose id is new. Reset on
// stop so a re-opened/other conversation starts clean.
let seenMessageIds = new Set();
let seededSeenSet = false;

function answerEndpoint(conversationId) {
  return `${endpoints.conversations}/${encodeURIComponent(conversationId)}/answer`;
}

function conversationEndpoint(conversationId) {
  return `${endpoints.conversations}/${encodeURIComponent(conversationId)}`;
}

// Begin (or continue) polling the given conversation for a pending question.
// Idempotent: re-calling for the conversation already being polled does nothing.
// Called after an orchestrating-persona spawn `done`, and on loadConversation so
// reopening a thread with an outstanding question re-shows the affordance.
export function startPendingQuestionPolling(conversationId) {
  if (!conversationId) return;
  if (pollTimer && pollingConversationId === conversationId) return;
  stopPendingQuestionPolling();
  pollingConversationId = conversationId;
  // #311: the first poll seeds the seen-message set from whatever is already in
  // the thread (so we don't re-render existing messages); subsequent polls
  // render only newly-arrived ones.
  seenMessageIds = new Set();
  seededSeenSet = false;
  // Poll once immediately so a re-opened thread shows the affordance without
  // waiting a full interval, then on a cadence.
  pollOnce();
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

export function stopPendingQuestionPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollingConversationId = null;
  // #311: drop the dedup state so the next polled conversation seeds fresh.
  seenMessageIds = new Set();
  seededSeenSet = false;
}

async function pollOnce() {
  const conversationId = pollingConversationId;
  // Stop if the user has navigated away from the polled conversation — the
  // affordance only makes sense for the conversation currently in view.
  if (!conversationId || state.currentConversationId !== conversationId) {
    stopPendingQuestionPolling();
    clearAffordance();
    return;
  }

  let data;
  try {
    const resp = await fetch(conversationEndpoint(conversationId));
    if (!resp.ok) return;  // transient — keep polling
    data = await resp.json();
  } catch (e) {
    return;  // network blip — keep polling
  }

  // A late response can arrive after the user navigated away; ignore it.
  if (state.currentConversationId !== conversationId) return;

  // #311: render messages that arrived since the thread was last rendered —
  // the spawned session's streamed [NOTIFY]/[GOAL] and its terminal result,
  // written into the conversation by the worker out-of-band. The first poll
  // only SEEDS the seen-set (those messages are already on screen, rendered by
  // askStream/loadConversation); later polls append only ids we haven't seen.
  renderNewMessages(data && data.messages);

  const pq = data && data.pending_question;
  if (pq && pq.question) {
    renderAffordance(conversationId, pq);
  } else {
    // No (longer a) pending question — the session resolved or hasn't asked
    // yet. Drop any stale card but keep polling so the next [CLARIFY]/[GOAL]
    // (a session can ask more than once) still surfaces.
    clearAffordance();
  }
}

// #311: append conversation messages that aren't on screen yet, deduped by id.
// On the first poll we only record ids (seed) without rendering — those came
// from the initial thread render. Afterward, any id we haven't recorded is a
// late arrival from the worker, so we render it and record it. Idempotent: a
// re-poll of the same messages adds nothing (every id is already seen), so
// there's no duplication and no extra polling loop — this reuses the 4s poll.
function renderNewMessages(messages) {
  if (!Array.isArray(messages)) return;

  if (!seededSeenSet) {
    for (const m of messages) {
      if (m && m.id) seenMessageIds.add(m.id);
    }
    seededSeenSet = true;
    return;
  }

  for (const m of messages) {
    if (!m || !m.id || seenMessageIds.has(m.id)) continue;
    seenMessageIds.add(m.id);
    // Only render assistant output. The worker mirrors its progress/result as
    // assistant messages; user messages here are the operator's own answer
    // (POST /answer records it server-side), which submitAnswer already echoed
    // into the thread with addMessage(answer, 'user') and has no client-side id
    // to dedup against — so rendering it would duplicate. Skip user roles.
    if (m.role !== 'assistant') continue;
    addMessage(m.content, m.role, m.sources || []);
  }
}

function clearAffordance() {
  const card = document.getElementById(CARD_ID);
  if (card) card.remove();
}

// Render (or refresh) the inline answer card for the given pending question.
// goal_approval frames the input as locking a proposed goal; clarification /
// followup are a plain answer.
function renderAffordance(conversationId, pq) {
  const messagesEl = document.getElementById('messages');
  if (!messagesEl) return;

  const isGoal = pq.kind === 'goal_approval';
  const heading = isGoal ? '🎯 Approve the goal' : '❓ The session needs your input';
  const hint = isGoal
    ? 'Reply to lock the goal (e.g. "yes" / "go"), or refine it.'
    : 'Answer to continue the session.';
  const placeholder = isGoal ? 'Approve or refine the goal…' : 'Type your answer…';

  let card = document.getElementById(CARD_ID);
  if (!card) {
    card = document.createElement('div');
    card.id = CARD_ID;
    card.className = 'pending-question';
    messagesEl.appendChild(card);
  }

  // Rebuild the card content (the question text may change across questions).
  card.innerHTML = `
    <div class="pq-heading">${escapeHtml(heading)}</div>
    <div class="pq-question">${escapeHtml(pq.question)}</div>
    <div class="pq-row">
      <input type="text" class="pq-input" id="pqInput" placeholder="${escapeHtml(placeholder)}"
             autocomplete="off" aria-label="Answer the session's question">
      <button type="button" class="pq-send" id="pqSend">Send</button>
    </div>
    <div class="pq-hint">${escapeHtml(hint)}</div>
    <div class="pq-status" id="pqStatus"></div>
  `;

  const input = card.querySelector('#pqInput');
  const sendBtn = card.querySelector('#pqSend');
  const submit = () => submitAnswer(conversationId, input, sendBtn, card);
  sendBtn.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function submitAnswer(conversationId, input, sendBtn, card) {
  const answer = (input.value || '').trim();
  const statusEl = card.querySelector('#pqStatus');
  if (!answer) {
    // Empty answer would be a 400; keep the input and nudge instead of POSTing.
    if (statusEl) statusEl.textContent = 'Enter an answer first.';
    input.focus();
    return;
  }

  input.disabled = true;
  sendBtn.disabled = true;
  if (statusEl) statusEl.textContent = 'Sending…';

  let resp;
  try {
    resp = await fetch(answerEndpoint(conversationId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    });
  } catch (e) {
    input.disabled = false;
    sendBtn.disabled = false;
    if (statusEl) statusEl.textContent = 'Network error — try again.';
    return;
  }

  if (resp.ok) {
    // Answer deposited; the server echoed it into the thread and the worker
    // resumes the session. Echo the answer into the thread (the server records
    // it as a user message too, but the thread isn't re-rendered until reopen),
    // then clear the card and keep polling so a follow-up question (or, via
    // #311, the resumed output) surfaces here. A re-render of the just-answered
    // question on the next poll (≤4s, before the worker consumes the deposit) is
    // idempotent — a re-submit returns 409 and clears.
    addMessage(answer, 'user');
    clearAffordance();
    return;
  }

  if (resp.status === 409) {
    // No longer awaiting — already answered elsewhere (e.g. Telegram), timed
    // out, or resolved. Drop the affordance with a brief note.
    clearAffordance();
    return;
  }

  if (resp.status === 400) {
    // Empty answer per the server — keep the input so the user can retry.
    input.disabled = false;
    sendBtn.disabled = false;
    if (statusEl) statusEl.textContent = 'Answer cannot be empty.';
    input.focus();
    return;
  }

  // Other errors (404, 5xx) — re-enable and let the user retry.
  input.disabled = false;
  sendBtn.disabled = false;
  if (statusEl) statusEl.textContent = 'Could not send — try again.';
}
