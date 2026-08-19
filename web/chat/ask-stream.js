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

  // The agent and hermes backends have no persona/model pass-through yet and
  // are each reached via their own proxied endpoint (bearer added server-side);
  // lifeos sends persona_id + model_override to /api/ask/stream exactly as
  // before (#361, #587).
  const proxiedAskEndpoint = { agent: endpoints.agentAsk, hermes: endpoints.hermesAsk };
  const isLifeos = !proxiedAskEndpoint[backend];
  if (isLifeos && personaId != null) body.persona_id = personaId;
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
          state.sessionCost += data.cost_usd || 0;
          elements.sessionCostEl.textContent = '$' + state.sessionCost.toFixed(3);
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
  elements.inputField.focus();
}

export function askQuestion(question) {
  elements.inputField.value = question;
  sendMessage();
}
