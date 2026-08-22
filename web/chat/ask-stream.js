// Chat SSE client (#358): streams an answer from /api/ask/stream and the
// orchestrator's engine-handoff to /api/chat/handoff. Extracted verbatim from
// index.html's inline <script>, with the transport split into askStream() so
// follow-on surfaces (#359 persona, #361 Voice|Text) can reuse it.

import { state, config, elements, endpoints, hooks } from './session.js';
import { addMessage, updateMessage, setStatus, buildSourcesHtml } from './thread.js';
import { clearAttachments } from './attachments.js';
import { loadConversations } from './conversations.js';
import { personaSupportsHandoff, personaOrchestrates } from './persona.js';
import { setStoredConversationId } from './backend.js';
import { startPendingQuestionPolling } from './pending-question.js';

// Low-level SSE transport. Builds the request body, opens the stream, and calls
// `on(data)` for each parsed `data:` event. If `on` returns `true`, processing
// stops immediately (used for the server-`error` path). `personaId`/`backend`
// are reserved for follow-ons and omitted from the body when unset, so today's
// request is byte-identical.
export async function askStream({ question, conversationId, attachments, personaId, backend, model, on }) {
  const body = { question };
  if (conversationId) {
    body.conversation_id = conversationId;
  }

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
  // `lifeos_context` envelope (#590), so it gets persona_id exactly like
  // lifeos does; the Agent backend still has no persona pass-through.
  // model_override stays lifeos-only — a persona's `model` frontmatter field
  // is a no-op on the other backends.
  const proxiedAskEndpoint = { agent: endpoints.agentAsk, hermes: endpoints.hermesAsk };
  // Orchestrating personas (e.g. doctor) used to always run on LifeOS, even
  // with Hermes selected, diverting their turn to /api/ask/stream because the
  // spawn path (background Claude Code session + thread linking) was
  // LifeOS-native with no Hermes equivalent (#596). #642 removed that divert:
  // Hermes now drives its own background worker for these personas
  // (lifeos_agent_spawn, #640), carrying a Hermes-specific preamble the proxy
  // attaches server-side (surface="hermes", hermes_proxy.py) — so a Hermes
  // turn is no longer special-cased here at all, orchestrating or not.
  const isLifeos = !proxiedAskEndpoint[backend];
  if (backend !== 'agent' && personaId != null) body.persona_id = personaId;
  // Per-turn model picker (lifeos backend only). 'auto' is the default — omit
  // it so the request stays byte-identical for users who never touch the picker.
  if (isLifeos && model && model !== 'auto') body.model_override = model;

  const response = await fetch(isLifeos ? endpoints.ask : proxiedAskEndpoint[backend], {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new Error('Request failed');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const chunk = decoder.decode(value);
    const lines = chunk.split('\n');

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          if (on(data) === true) return;  // handler asked to stop processing
        } catch (e) {
          // Skip malformed JSON
        }
      }
    }
  }
}

// Stop the turn in flight for the current conversation (#611). A turn now
// keeps running server-side after the browser gives up on the stream, so an
// explicit cancel is the only way to actually stop it — closing the tab or
// navigating away no longer does. Best-effort: on failure there's nothing
// useful to show the user; the turn just keeps running, same as before #611.
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

export async function sendMessage() {
  const question = elements.inputField.value.trim();
  if (!question || state.isLoading) return;

  // In agent-thread mode the composer continues that thread instead
  // of starting a normal chat query (#236).
  if (state.currentAgentThread) {
    await hooks.onAgentThreadReply(question);
    return;
  }

  state.isLoading = true;
  setStatus('loading', 'Thinking...');
  elements.sendBtn.disabled = true;
  // Swap Send for Stop (#611) — hidden again once this turn settles, in the
  // same place sendBtn is re-enabled below.
  elements.sendBtn.style.display = 'none';
  elements.stopBtn.classList.add('visible');

  // Capture attachments before clearing
  const messageAttachments = [...state.attachments];
  const attachmentCount = messageAttachments.length;

  // Add user message with attachment indicator
  let userMsgContent = question;
  if (attachmentCount > 0) {
    userMsgContent += `\n<span class="attachment-indicator">📎 ${attachmentCount} attachment${attachmentCount > 1 ? 's' : ''}</span>`;
  }
  addMessage(userMsgContent, 'user');
  elements.inputField.value = '';
  elements.inputField.style.height = 'auto';

  // Clear attachments after capturing them
  clearAttachments();

  // Update title if new conversation
  if (!state.currentConversationId) {
    elements.chatTitle.textContent = question.slice(0, 40) + (question.length > 40 ? '...' : '');
  }

  // Add placeholder for assistant response
  const msgId = 'msg-' + Date.now();
  const msg = addMessage('', 'assistant', [], msgId);

  // Add typing indicator
  const typingHtml = '<div class="typing"><span></span><span></span><span></span></div>';
  msg.querySelector('.message-content').innerHTML = typingHtml;

  let fullContent = '';
  let sources = [];
  let routingSources = [];  // Track which sources were used
  // The server-`error` event stops the stream and (matching the original
  // behavior) leaves the composer locked — no Ready status, no re-enable.
  let serverError = false;

  try {
    await askStream({
      question,
      conversationId: state.currentConversationId,
      attachments: messageAttachments,
      personaId: config.personaId,
      backend: config.backend,
      model: config.model,
      on: (data) => {
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
          state.currentConversationId = data.conversation_id;
          setStoredConversationId(data.conversation_id);  // per-backend persistence
        } else if (data.type === 'usage') {
          // #602: a backend that can't price a turn sends no `cost_usd`
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
          // Engine handoff (#305b/c): the orchestrator delegated to a CLI
          // worker. Gate it on the selected persona's advertised capabilities
          // (#359) — only personas with the `handoff` capability may trigger
          // it; for others, ignore the intent (no handoff UI). An explicit
          // `claude_code` model pick is itself the handoff opt-in, so it
          // bypasses the persona gate; inferred intents still require a
          // handoff-capable persona.
          if (personaSupportsHandoff() || config.model === 'claude_code') {
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
              }),
            }).then(r => r.json()).then(d => {
              fullContent = (d && d.message)
                ? d.message
                : '⚠️ Handoff to ' + label + ' failed.';
              updateMessage(msgId, fullContent);
              // #311: the handoff linked the spawned session to the conversation
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

          // Orchestrating-persona spawn (#403/#412): this turn started a
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
          let userMessage = 'Sorry, something went wrong.';

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
          updateMessage(msgId, userMessage);
          setStatus('error', 'Error');
          serverError = true;
          return true; // Stop processing
        }
      },
    });

    if (serverError) return;

    setStatus('', 'Ready');

  } catch (error) {
    console.error('Error:', error);
    updateMessage(msgId, 'Sorry, something went wrong. Please try again.');
    setStatus('error', 'Error');
  }

  state.isLoading = false;
  elements.sendBtn.disabled = false;
  elements.sendBtn.style.display = '';
  elements.stopBtn.classList.remove('visible');
  elements.inputField.focus();
}

export function askQuestion(question) {
  elements.inputField.value = question;
  sendMessage();
}
