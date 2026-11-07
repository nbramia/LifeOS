// Chat SSE client: streams an answer from /api/ask/stream and the
// orchestrator's engine-handoff to /api/chat/handoff. Extracted verbatim from
// index.html's inline <script>, with the transport split into askStream() so
// follow-on surfaces (persona picker, Voice|Text) can reuse it.

import { state, config, elements, endpoints, hooks } from './session.js';
import { addMessage, updateMessage, setStatus, buildSourcesHtml } from './thread.js';
import { clearAttachments } from './attachments.js';
import { loadConversations } from './conversations.js';
import { personaSupportsHandoff, personaOrchestrates } from './persona.js';
import {
  setStoredConversationId, setPendingNewTurn, clearPendingNewTurn,
} from './backend.js';
import { startPendingQuestionPolling } from './pending-question.js';
import {
  localTurns, hasLocalTurns, isWatching, stopWatching, watchConversation,
  findPendingNewConversation,
} from './turn-recovery.js';
import { loadConversation } from './conversations.js';

// Low-level SSE transport. Builds the request body, opens the stream, and calls
// `on(data)` for each parsed `data:` event. If `on` returns `true`, processing
// stops immediately (used for the server-`error` path). `personaId`/`backend`
// are reserved for follow-ons and omitted from the body when unset, so today's
// request is byte-identical. `ifBusy: 'queue'` asks the server to run this
// turn after whatever is already running or queued on the conversation
// instead of superseding it; `clientTurnId` is the opaque key the server
// echoes on the `queued` event. `signal` aborts the read.
//
// Resolves with `{ responded: true }` once the stream ends; throws on a
// failure, with `err.responded` telling a drop mid-stream (the turn exists
// server-side) apart from a request that never got a response.
export async function askStream({
  question, conversationId, attachments, personaId, backend, model, on,
  ifBusy, clientTurnId, signal, onResponse,
}) {
  const body = { question };
  if (conversationId) {
    body.conversation_id = conversationId;
  }
  if (ifBusy) body.if_busy = ifBusy;
  if (clientTurnId) body.client_turn_id = clientTurnId;

  // Include attachments if any
  if (attachments && attachments.length > 0) {
    body.attachments = attachments.map(att => ({
      filename: att.filename,
      media_type: att.mediaType,
      data: att.dataUrl.split(',')[1]  // Extract base64 data
    }));
  }

  // Both proxied backends are reached via their own endpoint (bearer added
  // server-side). Hermes resolves persona_id server-side into the
  // `lifeos_context` envelope, so it gets persona_id exactly like
  // lifeos does; the Agent backend still has no persona pass-through.
  // model_override stays lifeos-only — a persona's `model` frontmatter field
  // is a no-op on the other backends.
  const proxiedAskEndpoint = { agent: endpoints.agentAsk, hermes: endpoints.hermesAsk };
  // Orchestrating personas (e.g. doctor) run entirely on their selected
  // backend: Hermes drives its own background worker for these personas
  // (lifeos_agent_spawn), carrying a Hermes-specific preamble the proxy
  // attaches server-side (surface="hermes", hermes_proxy.py) — so a Hermes
  // turn is never special-cased here, orchestrating or not.
  const isLifeos = !proxiedAskEndpoint[backend];
  if (backend !== 'agent' && personaId != null) body.persona_id = personaId;
  // Per-turn model picker (lifeos backend only). 'auto' is the default — omit
  // it so the request stays byte-identical for users who never touch the picker.
  if (isLifeos && model && model !== 'auto') body.model_override = model;

  const response = await fetch(isLifeos ? endpoints.ask : proxiedAskEndpoint[backend], {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  // The server has registered (or queued) the turn by the time headers arrive.
  if (onResponse) onResponse();

  if (!response.ok) {
    throw new Error('Request failed');
  }

  try {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    // A network read is not an SSE frame: keep a trailing partial line for
    // the next read.
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6));
            if (on(data) === true) return { responded: true };  // handler asked to stop processing
          } catch (e) {
            // Skip malformed JSON
          }
        }
      }
    }
  } catch (err) {
    err.responded = true;
    throw err;
  }
  return { responded: true };
}

// Stop the turn in flight for the current conversation. A turn
// keeps running server-side after the browser gives up on the stream, so an
// explicit cancel is the only way to actually stop it — closing the tab or
// navigating away does not. Best-effort: on failure there's nothing
// useful to show the user; the turn just keeps running.
//
// The cancel endpoint also drops every turn queued behind the running one;
// their streams then end with nothing after `queued`.
export async function stopTurn() {
  const conversationId = state.currentConversationId;
  if (!conversationId) return;
  try {
    await fetch(`${endpoints.conversations}/${encodeURIComponent(conversationId)}/cancel`, {
      method: 'POST',
    });
  } catch (e) {
    // network blip — nothing to surface here
  }
}

// Text turns can be sent while earlier ones are still running: each later
// send is rendered at once with a "Queued" marker and POSTed with
// `if_busy: 'queue'`, so the server runs them in order, each with the
// previous answers in its history. Stop cancels the running turn and the
// queue (stopTurn()).
function syncTurnUi() {
  const busy = localTurns.size > 0;
  state.isLoading = busy;
  elements.sendBtn.disabled = false;
  elements.stopBtn.classList.toggle('visible', busy || isWatching());
}

function markQueued(userMsg, queued) {
  if (!userMsg) return;
  userMsg.classList.toggle('queued', queued);
  const existing = userMsg.querySelector('.queued-marker');
  if (queued && !existing) {
    userMsg.insertAdjacentHTML('beforeend', '<div class="queued-marker">Queued</div>');
  } else if (!queued && existing) {
    existing.remove();
  }
}

// A queued send on a brand-new conversation needs the id the first turn's
// `conversation_id` frame carries; it normally arrives well under a second
// after that turn was sent.
async function waitForConversationId(self, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  const othersPending = () => [...localTurns].some((t) => t !== self && t.conversationId === null);
  while (!state.currentConversationId && othersPending() && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 100));
  }
  return state.currentConversationId;
}

let placeholderSeq = 0;
// Settles once the most recent send's request has been answered (or failed).
let lastPosted = Promise.resolve();

function newClientTurnId() {
  try {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  } catch (e) { /* fall through */ }
  return 'turn-' + Date.now() + '-' + Math.random().toString(36).slice(2);
}

export async function sendMessage() {
  const question = elements.inputField.value.trim();
  if (!question) return;

  // In agent-thread mode the composer continues that thread instead
  // of starting a normal chat query.
  if (state.currentAgentThread) {
    await hooks.onAgentThreadReply(question);
    return;
  }

  // Something other than a text turn holds the composer: the backend
  // default still resolving, or a voice turn.
  if (state.isLoading && localTurns.size === 0) return;

  // Queue behind a turn this page is streaming, or one it is watching after
  // a reload. The Agent backend has no server-side queue.
  const queued = hasLocalTurns(state.currentConversationId) || isWatching(state.currentConversationId || undefined);
  if (queued && config.backend === 'agent') return;
  const turn = { controller: new AbortController(), conversationId: state.currentConversationId };
  localTurns.add(turn);
  // A queued send takes over from the watcher; the thread is reloaded once
  // the last local turn finishes, so the watched reply shows up in order.
  if (isWatching(state.currentConversationId || undefined)) {
    stopWatching();
    turn.reloadWhenDone = true;
  }
  syncTurnUi();
  setStatus('loading', 'Thinking...');

  // Capture attachments before clearing
  const messageAttachments = [...state.attachments];
  const attachmentCount = messageAttachments.length;

  // Add user message with attachment indicator
  let userMsgContent = question;
  if (attachmentCount > 0) {
    userMsgContent += `\n<span class="attachment-indicator">📎 ${attachmentCount} attachment${attachmentCount > 1 ? 's' : ''}</span>`;
  }
  const userMsg = addMessage(userMsgContent, 'user');
  markQueued(userMsg, queued);
  elements.inputField.value = '';
  elements.inputField.style.height = 'auto';

  // Clear attachments after capturing them
  clearAttachments();

  // Update title if new conversation
  if (!state.currentConversationId) {
    elements.chatTitle.textContent = question.slice(0, 40) + (question.length > 40 ? '...' : '');
  }

  // Add placeholder for assistant response
  const msgId = 'msg-' + Date.now() + '-' + (++placeholderSeq);  // unique across back-to-back sends
  const msg = addMessage('', 'assistant', [], msgId);

  // Add typing indicator
  const typingHtml = '<div class="typing"><span></span><span></span><span></span></div>';
  msg.querySelector('.message-content').innerHTML = typingHtml;

  let fullContent = '';
  let sources = [];
  let routingSources = [];  // Track which sources were used
  // A server `error` event marks the turn failed while the shared cleanup
  // below still releases the composer for a retry.
  let serverError = false;
  let sawDone = false;
  let sawRunningFrame = false;

  // Queued sends reach the server in the order they were made: each waits
  // until the previous send's request has been answered (the server has
  // registered or queued that turn) before posting its own.
  const previousPosted = lastPosted;
  let markPosted;
  const posted = new Promise((resolve) => { markPosted = resolve; });
  lastPosted = posted;
  if (queued) await previousPosted;
  if (queued && !turn.conversationId) {
    turn.conversationId = await waitForConversationId(turn);
  }
  // Until the `conversation_id` frame arrives, remember this turn so a load
  // after the page closed can find the conversation the server created.
  if (!turn.conversationId) setPendingNewTurn(question);
  const startConversationId = turn.conversationId;

  try {
    await askStream({
      question,
      conversationId: turn.conversationId,
      attachments: messageAttachments,
      personaId: config.personaId,
      backend: config.backend,
      model: config.model,
      ifBusy: queued && turn.conversationId ? 'queue' : undefined,
      // Echoed on the `queued` event; an ordinary turn's body stays unchanged.
      clientTurnId: queued && turn.conversationId ? newClientTurnId() : undefined,
      signal: turn.controller.signal,
      onResponse: markPosted,
      on: (data) => {
        if (data.type === 'queued') {
          markQueued(userMsg, true);
          return;
        }
        if (!sawRunningFrame) {
          sawRunningFrame = true;
          markQueued(userMsg, false);
        }
        if (data.type === 'routing') {
          // Capture which sources are being used
          routingSources = data.sources || [];
          console.log('Routing to:', routingSources);
        } else if (data.type === 'self_correction') {
          fullContent = '';
          updateMessage(msgId, '');
        } else if (data.type === 'content') {
          fullContent += data.content;
          updateMessage(msgId, fullContent);
        } else if (data.type === 'sources') {
          sources = data.sources;
        } else if (data.type === 'conversation_id') {
          turn.conversationId = data.conversation_id;
          clearPendingNewTurn();
          // Only adopt it if the user is still on the conversation this
          // turn was sent from (or on the new one it is creating).
          if (state.currentConversationId === startConversationId || !state.currentConversationId) {
            state.currentConversationId = data.conversation_id;
            setStoredConversationId(data.conversation_id);  // per-backend persistence
          }
        } else if (data.type === 'usage') {
          // A backend that can't price a turn sends no `cost_usd`
          // rather than inventing a zero. `data.cost_usd || 0` treated an
          // absent cost the same as a real one, silently turning "unknown"
          // into a confident (wrong) claim of "free". An explicit
          // presence-and-type check keeps the two apart -- `0` still takes
          // this branch and adds nothing, same as before, but leaves no
          // mark; anything else (missing, null, a string) adds nothing and
          // marks the total as a lower bound instead.
          const cost = data.cost_usd;
          if (typeof cost === 'number' && Number.isFinite(cost)) {
            state.sessionCost += cost;
          } else {
            state.sessionCostUnpriced += 1;
          }
          const prefix = state.sessionCostUnpriced > 0 ? '~' : '';
          elements.sessionCostEl.textContent = prefix + '$' + state.sessionCost.toFixed(3);
          elements.sessionCostEl.title = state.sessionCostUnpriced > 0
            ? state.sessionCostUnpriced + ' turn(s) this session had no reported cost -- total is a lower bound.'
            : '';
        } else if (data.type === 'claude_intent') {
          // Engine handoff: the orchestrator delegated to a CLI
          // worker. Gate it on the selected persona's advertised capabilities
          // — only personas with the `handoff` capability may trigger
          // it; for others, ignore the intent (no handoff UI). An explicit
          // `claude_code` model pick is itself the handoff opt-in, so it
          // bypasses the persona gate; inferred intents still require a
          // handoff-capable persona.
          if (personaSupportsHandoff() || config.model === 'claude_code' || data.execution) {
            const engine = data.engine || 'claude_code';
            const label = engine === 'codex' ? 'Codex' : 'Claude Code';
            fullContent = '🤝 Handing off to ' + label + '…';
            updateMessage(msgId, fullContent);
            // Pin the conversation this handoff targets BEFORE the in-flight
            // POST: the server links the spawned session to exactly this
            // conversation, so the post-await poll must target it too. Reading
            // state.currentConversationId again inside the `.then()` would poll
            // the wrong thread if the user navigated mid-POST.
            const handoffConversationId = state.currentConversationId;
            fetch(endpoints.handoff, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                engine: engine,
                task: data.task || '',
                conversation_id: handoffConversationId,
                ...(data.execution ? { execution: data.execution } : {}),
              }),
            }).then(r => r.json()).then(d => {
              fullContent = (d && d.message)
                ? d.message
                : '⚠️ Handoff to ' + label + ' failed.';
              updateMessage(msgId, fullContent);
              // The handoff linked the spawned session to the conversation
              // it targeted server-side, so poll THAT conversation for the
              // session's streamed progress + terminal result and render them
              // into the thread (parity with the orchestrating-persona path
              // below). The seen-message seed in startPendingQuestionPolling
              // assumes the handoff ack message is already persisted server-side
              // (chat_handoff writes it synchronously before returning) when the
              // first poll fires — if ack persistence ever becomes async, the
              // seed must be revisited or the ack would render twice. Only on a
              // successful spawn with a conversation to land results in.
              if (d && d.ok && handoffConversationId) {
                startPendingQuestionPolling(handoffConversationId);
              }
            }).catch(() => {
              fullContent = '⚠️ Handoff to ' + label + ' failed.';
              updateMessage(msgId, fullContent);
            });
          }
        } else if (data.type === 'done') {
          sawDone = true;
          // Add sources and meta to message
          const msgEl = document.getElementById(msgId);
          if (msgEl) {
            let metaHtml = '';

            // Show routing sources used — but only genuine data sources. The
            // `routing` event also carries internal plumbing labels (agent,
            // claude_code, clarification, escalation model names, codex) that
            // aren't "sources" to the user; those get filtered out here so an
            // answer that pulled no data doesn't render a lone "agent" pill.
            const sourceIcons = {
              vault: '📚',
              calendar: '📅',
              gmail: '✉️',
              drive: '📁',
              attachment: '📎',
              people: '👤',
              actions: '✅'
            };
            const dataSources = routingSources.filter(src => sourceIcons[src]);
            if (dataSources.length > 0) {
              metaHtml += '<div class="routing-sources">';
              dataSources.forEach(src => {
                metaHtml += `<span class="routing-source ${src}">${sourceIcons[src]} ${src}</span>`;
              });
              metaHtml += '</div>';
            }

            metaHtml += buildSourcesHtml(sources);

            // Remove any existing meta
            const existingMeta = msgEl.querySelector('.message-meta');
            if (existingMeta) existingMeta.remove();

            // Only render the meta block when there's something to show
            // (routing sources or vault sources); no save-to-vault button.
            if (metaHtml) {
              msgEl.insertAdjacentHTML('beforeend', `<div class="message-meta">${metaHtml}</div>`);
            }
          }

          loadConversations();

          // Orchestrating-persona spawn: this turn started a
          // background Claude Code session (routed to `claude_code`, the
          // doctor spawn path) and the conversation is now linked to it. Begin
          // polling for a `[CLARIFY]`/`[GOAL]` so it can be answered here
          // without Telegram. Gated on BOTH the routing and the orchestrating
          // persona: `claude_code` is also emitted by plain handoffs (model
          // picker / inferred-terminal / escalation ladder) that never link a
          // session, and only an orchestrating persona's turn is a spawn — so
          // this excludes those handoffs and never polls on a normal turn.
          if (routingSources.includes('claude_code') && personaOrchestrates()
              && state.currentConversationId) {
            startPendingQuestionPolling(state.currentConversationId);
          }
        } else if (data.type === 'error') {
          // Handle error from server
          const errorMsg = data.message || 'An error occurred';
          let userMessage = 'Sorry, I encountered an error and could not complete this request.';

          // Provide helpful messages for common errors
          if (errorMsg.toLowerCase().includes('api') ||
            errorMsg.toLowerCase().includes('auth') ||
            errorMsg.toLowerCase().includes('key') ||
            errorMsg.toLowerCase().includes('token')) {
            userMessage = 'API configuration error. Please check that your API key is set correctly.';
          } else if (errorMsg.toLowerCase().includes('timeout')) {
            userMessage = 'The request timed out. Please try again.';
          } else if (errorMsg.toLowerCase().includes('rate')) {
            userMessage = 'Rate limit exceeded. Please wait a moment and try again.';
          }

          console.error('Stream error:', errorMsg);
          // Keep any content already rendered (and persisted by the server)
          // visible when the terminal error follows a partial response. Only
          // use the generic failure copy when no useful content arrived.
          if (fullContent) {
            updateMessage(msgId, fullContent);
          } else {
            updateMessage(msgId, userMessage);
          }
          setStatus('error', 'Error');
          serverError = true;
          return true; // Stop processing
        }
      },
    });

    if (!sawRunningFrame) {
      // The stream ended while the turn was still queued: it was cancelled
      // (Stop drops the queue) before it ever ran.
      markQueued(userMsg, false);
      updateMessage(msgId, 'Cancelled.');
    }
    if (!serverError && localTurns.size <= 1) setStatus('', 'Ready');
    if (!serverError && !sawDone) clearPendingNewTurn();

  } catch (error) {
    if (turn.handedOff) {
      // Aborted so the conversation watcher could take over this thread.
    } else if (error && error.responded) {
      // The stream dropped after the server accepted the turn (network
      // switch, the phone sleeping). The turn keeps running server-side, so
      // this is not an error: catch up on the persisted thread instead.
      console.warn('Stream dropped; recovering from the server:', error);
      const conversationId = turn.conversationId;
      localTurns.delete(turn);
      if (conversationId && state.currentConversationId === conversationId) {
        for (const other of localTurns) {
          if (other.conversationId === conversationId) {
            other.handedOff = true;
            other.controller.abort();
          }
        }
        watchConversation(conversationId);
      } else if (!conversationId) {
        findPendingNewConversation();
      }
    } else {
      console.error('Error:', error);
      updateMessage(msgId, 'Sorry, something went wrong. Please try again.');
      setStatus('error', 'Error');
      if (!turn.conversationId) clearPendingNewTurn();
    }
  }

  markPosted();
  localTurns.delete(turn);
  syncTurnUi();
  if (turn.reloadWhenDone) {
    if (localTurns.size > 0) {
      // Hand the reload to whichever local turn finishes last.
      for (const other of localTurns) other.reloadWhenDone = true;
    } else if (turn.conversationId && state.currentConversationId === turn.conversationId) {
      loadConversation(turn.conversationId);
    }
  }
  if (localTurns.size === 0) elements.inputField.focus();
}

export function askQuestion(question) {
  elements.inputField.value = question;
  sendMessage();
}
