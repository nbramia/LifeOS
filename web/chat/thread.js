// Chat thread rendering: message bubbles, streaming updates, markdown,
// source badges, and the connection-status indicator. Extracted verbatim from
// index.html's inline <script>.

import { elements } from './session.js';

// HTML-escape helper. The classic shell script keeps its own copy (it is used
// pervasively by CRM / agent-thread / review-queue code that cannot import an ES
// module); this exported copy serves the chat modules.
// Safe in element text and in quoted attribute values.
export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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
    html += `<div class="message-meta">${buildSourcesHtml(sources)}</div>`;
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

// Link targets chat will render: web pages and mail addresses only.
const SAFE_HREF = /^(https?:|mailto:)/i;

// Returns the href when it is allowed (input already HTML-escaped), else ''.
export function safeHref(escapedUrl) {
  // Ignore whitespace and control characters a browser strips before
  // reading the scheme.
  const probe = String(escapedUrl).replace(/[\u0000-\u0020\u007f-\u009f]/g, '');
  return SAFE_HREF.test(probe) ? probe : '';
}

// Chat renders message text as text: the whole message is HTML-escaped first,
// then a small markdown subset (links, bold, italics, code, line breaks) is
// applied to the escaped text. Markdown images render as links, so no remote
// resource is loaded by message content.
export function formatContent(content) {
  const links = [];
  const stash = (html) => `\u0000${links.push(html) - 1}\u0000`;
  let text = escapeHtml(String(content ?? '').replace(/\u0000/g, ''));

  text = text.replace(/!?\[([^\]\n]*)\]\(([^)\s]+)\)/g, (match, label, url) => {
    const href = safeHref(url);
    if (!href) return stash(label || '');
    return stash(`<a href="${href}" target="_blank" rel="noopener noreferrer">${label || href}</a>`);
  });

  text = text
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/`(.*?)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');

  return text.replace(/\u0000(\d+)\u0000/g, (m, i) => links[Number(i)]);
}

// Build the compact, collapsed-by-default "📄 N sources" block shared by the
// live stream (ask-stream.js) and the history render (addMessage). The file
// chips are hidden until the user clicks the toggle; returns '' when there are
// no sources so no empty block is rendered.
export function buildSourcesHtml(sources) {
  if (!sources || sources.length === 0) return '';
  const label = sources.length === 1 ? '1 source' : `${sources.length} sources`;
  let html = `<div class="sources collapsed">`
    + `<button class="sources-toggle" onclick="toggleSources(this)">📄 ${label} <span class="sources-caret">▾</span></button>`
    + `<div class="source-chips">`;
  sources.forEach(src => {
    const fileName = src.file_name || src;
    const sourceType = src.source_type || 'vault';

    if (sourceType === 'calendar' && src.url) {
      // Calendar sources use Google Calendar URL
      const calendarHref = safeHref(escapeHtml(src.url));
      html += calendarHref
        ? `<a href="${calendarHref}" target="_blank" rel="noopener noreferrer" class="source-link">${escapeHtml(fileName)}</a>`
        : `<span class="source-link">${escapeHtml(fileName)}</span>`;
    } else {
      // Vault sources use Obsidian URL
      const obsidianPath = src.obsidian_path || fileName;
      const obsidianUrl = `obsidian://open?vault=Notes%202025&file=${encodeURIComponent(obsidianPath)}`;
      html += `<a href="${escapeHtml(obsidianUrl)}" class="source-link">📄 ${escapeHtml(fileName)}</a>`;
    }
  });
  html += `</div></div>`;
  return html;
}

export function toggleSources(btn) {
  const sourcesDiv = btn.closest('.sources');
  const isCollapsed = sourcesDiv.classList.toggle('collapsed');
  const caret = btn.querySelector('.sources-caret');
  if (caret) caret.textContent = isCollapsed ? '▾' : '▴';
}
