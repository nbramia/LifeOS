// Chat SSE client (#358): streams an answer from /api/ask/stream and the
// orchestrator's engine-handoff to /api/chat/handoff. Extracted verbatim from
// index.html's inline <script>, with the transport split into askStream() so
// follow-on surfaces (#359 persona, #361 Voice|Text) can reuse it.

import { state, config, elements, endpoints, hooks } from './session.js';
import { addMessage, updateMessage, setStatus } from './thread.js';
import { clearAttachments } from './attachments.js';
import { loadConversations } from './conversations.js';
import { personaSupportsHandoff } from './persona.js';
import { setStoredConversationId } from './backend.js';

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

  // The agent backend has no personas and is reached via its own proxied
  // endpoint (bearer added server-side); lifeos sends persona_id to
  // /api/ask/stream exactly as before.
  const isAgent = backend === 'agent';
  if (!isAgent && personaId != null) body.persona_id = personaId;
  // Per-turn model picker (lifeos backend only). 'auto' is the default — omit
  // it so the request stays byte-identical for users who never touch the picker.
  if (!isAgent && model && model !== 'auto') body.model_override = model;

  const response = await fetch(isAgent ? endpoints.agentAsk : endpoints.ask, {
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
            fetch(endpoints.handoff, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                engine: engine,
                task: data.task || '',
                conversation_id: state.currentConversationId,
              }),
            }).then(r => r.json()).then(d => {
              fullContent = (d && d.message)
                ? d.message
                : '⚠️ Handoff to ' + label + ' failed.';
              updateMessage(msgId, fullContent);
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

            // Show routing sources used
            if (routingSources.length > 0) {
              metaHtml += '<div class="routing-sources">';
              const sourceIcons = {
                vault: '📚',
                calendar: '📅',
                gmail: '✉️',
                drive: '📁',
                attachment: '📎',
                people: '👤',
                actions: '✅'
              };
              routingSources.forEach(src => {
                const icon = sourceIcons[src] || '📄';
                metaHtml += `<span class="routing-source ${src}">${icon} ${src}</span>`;
              });
              metaHtml += '</div>';
            }

            if (sources.length > 0) {
              // Add collapsed class if more than 3 sources
              const collapsedClass = sources.length > 3 ? ' collapsed' : '';
              metaHtml += `<div class="sources${collapsedClass}">`;
              sources.forEach(src => {
                const fileName = src.file_name || src;
                const sourceType = src.source_type || 'vault';

                if (sourceType === 'calendar' && src.url) {
                  // Calendar sources use Google Calendar URL
                  metaHtml += `<a href="${src.url}" target="_blank" class="source-link">${fileName}</a>`;
                } else {
                  // Vault sources use Obsidian URL
                  const obsidianPath = src.obsidian_path || fileName;
                  const obsidianUrl = `obsidian://open?vault=Notes%202025&file=${encodeURIComponent(obsidianPath)}`;
                  metaHtml += `<a href="${obsidianUrl}" class="source-link">📄 ${fileName}</a>`;
                }
              });
              // Add toggle button if more than 3 sources
              if (sources.length > 3) {
                const hiddenCount = sources.length - 3;
                metaHtml += `<button class="sources-toggle" onclick="toggleSources(this)">Show ${hiddenCount} more...</button>`;
              }
              metaHtml += '</div>';
            }

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
