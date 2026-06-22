// Chat thread rendering (#358): message bubbles, streaming updates, markdown,
// source badges, and the connection-status indicator. Extracted verbatim from
// index.html's inline <script>.

import { elements } from './session.js';

// HTML-escape helper. The classic shell script keeps its own copy (it is used
// pervasively by CRM / agent-thread / review-queue code that cannot import an ES
// module); this exported copy serves the chat modules.
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Local copy of the shell's scroll-to-bottom against the shared messages
// viewport. The shell keeps its own `scrollToBottom` (used by the agent-thread
// view and the inline scroll button); this one serves addMessage.
function scrollToBottom() {
  const { messagesEl } = elements;
  messagesEl.scrollTo({
    top: messagesEl.scrollHeight,
    behavior: 'smooth',
  });
}

// Connection-status pill in the header.
export function setStatus(status, text) {
  elements.statusDot.className = 'status-dot ' + status;
  elements.statusText.textContent = text;
}

export function addMessage(content, type, sources = [], messageId = null, target = null) {
  const { messagesEl } = elements;
  // Remove welcome message if exists
  const welcome = messagesEl.querySelector('.welcome');
  if (welcome) welcome.remove();

  const msg = document.createElement('div');
  msg.className = 'message ' + type;
  if (messageId) msg.id = messageId;

  let html = `<div class="message-content">${formatContent(content)}</div>`;

  if (type === 'assistant' && content && sources.length > 0) {
    // Add collapsed class if more than 3 sources
    const collapsedClass = sources.length > 3 ? ' collapsed' : '';
    html += `<div class="message-meta"><div class="sources${collapsedClass}">`;
    sources.forEach(src => {
      const fileName = src.file_name || src;
      const sourceType = src.source_type || 'vault';

      if (sourceType === 'calendar' && src.url) {
        // Calendar sources use Google Calendar URL
        html += `<a href="${src.url}" target="_blank" class="source-link">${fileName}</a>`;
      } else {
        // Vault sources use Obsidian URL
        const obsidianPath = src.obsidian_path || fileName;
        const obsidianUrl = `obsidian://open?vault=Notes%202025&file=${encodeURIComponent(obsidianPath)}`;
        html += `<a href="${obsidianUrl}" class="source-link">📄 ${fileName}</a>`;
      }
    });
    // Add toggle button if more than 3 sources
    if (sources.length > 3) {
      const hiddenCount = sources.length - 3;
      html += `<button class="sources-toggle" onclick="toggleSources(this)">Show ${hiddenCount} more...</button>`;
    }
    html += `</div></div>`;
  }

  msg.innerHTML = html;
  // When rendering into a detached target (e.g. bulk thread render),
  // append there and skip the per-message scroll; the caller scrolls
  // once after the batch lands.
  (target || messagesEl).appendChild(msg);
  if (!target) scrollToBottom();

  return msg;
}

export function updateMessage(messageId, content) {
  const msg = document.getElementById(messageId);
  if (msg) {
    const contentEl = msg.querySelector('.message-content');
    if (contentEl) {
      contentEl.innerHTML = formatContent(content);
    }
  }
}

export function formatContent(content) {
  return content
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/`(.*?)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');
}

export function toggleSources(btn) {
  const sourcesDiv = btn.closest('.sources');
  const isCollapsed = sourcesDiv.classList.contains('collapsed');
  const sourceLinks = sourcesDiv.querySelectorAll('.source-link');
  const totalCount = sourceLinks.length;
  const hiddenCount = totalCount - 3;

  if (isCollapsed) {
    // Expand
    sourcesDiv.classList.remove('collapsed');
    btn.textContent = 'Show less';
  } else {
    // Collapse
    sourcesDiv.classList.add('collapsed');
    btn.textContent = `Show ${hiddenCount} more...`;
  }
}
