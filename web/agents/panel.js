// web/agents/panel.js
//
// Shared session-detail panel (#850): header render, inline label edit,
// backfill + live SSE transcript tail, LLM summary fetch, and the
// kill/resume/focus operator actions. Used by BOTH the Graph tab's side
// panel (web/agents/graph.js) and the Board tab's card drawer
// (web/agents/board.js), following the web/chat/ module split from #360.
//
// This is the pre-#850 web/agents.html panel code (renderPanelHeader,
// updatePanelMeta, openPanel/closePanel, loadSessionEvents, appendEvent,
// startLabelEdit, fetchSessionSummary, focusCcSession, resumeCcSession,
// openKillModal) with one structural change: every DOM lookup is scoped to
// a `container` passed in at construction instead of a single global
// `#panel` element and `document.getElementById`, so a Graph-tab panel and
// a Board-tab drawer can each hold their own instance without id collisions.
// Rendering/behavior is otherwise unchanged.

export const STATUS_COLORS = {
  running:   '#34d399',
  claimed:   '#60a5fa',
  yielded:   '#fbbf24',  // agent worker — paused waiting on children
  inactive:  '#fbbf24',  // claude code — paused, resumable
  idle:      '#fbbf24',  // cli session — event-driven, waiting for input (not terminal)
  blocked:   '#fb923c',
  completed: '#6b7280',
  ended:     '#6b7280',  // cli session — event-driven, session ended
  failed:    '#f87171',
  budget_exceeded: '#f87171',
};

// NOTE: 'idle' is a live cli session waiting for input — it must NOT be
// treated as terminal. Only 'ended' (an explicit SessionEnd event) is.
export const TERMINAL = new Set(['completed', 'failed', 'budget_exceeded', 'ended']);

export function routingLabel(routing) {
  if (!routing || routing === 'local') return 'Local';
  if (routing === 'claude_code' || routing === 'code') return 'Claude Code';
  if (routing === 'codex') return 'Codex';
  if (routing === 'remote') return 'Remote';  // #809: #cloud tag, not Anthropic
  if (routing === 'hermes') return 'Hermes';  // #850: was falling through to 'Claude'
  if (routing === 'ask') return 'Ask';  // #863: waiting on the operator, not a model
  return 'Claude';
}

export function sourceLabelFor(d) {
  if (d.source === 'claude_code') return 'Claude Code CLI';
  if (d.source === 'codex') return 'Codex CLI';
  return 'LifeOS agent';
}

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// For values that go into HTML attribute positions via template strings.
// Restricts to a known-safe charset so attribute-context injection is
// structurally impossible.
export function escapeAttr(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, '_');
}

export function showToast(message, isError) {
  const t = document.createElement('div');
  t.className = 'toast' + (isError ? ' error' : '');
  t.textContent = message;
  document.body.appendChild(t);
  setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 3500);
}

// Format a unix-epoch timestamp as "MM/DD h:mm:ssa ET" — explicit Eastern
// tz so it matches the rest of LifeOS (timezone='America/New_York' in
// settings.py) regardless of the browser machine's locale.
export function formatTs(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString('en-US', {
    timeZone: 'America/New_York', month: 'numeric', day: 'numeric',
  });
  const time = d.toLocaleTimeString('en-US', {
    timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit',
    second: '2-digit', hour12: true,
  });
  return `${date} ${time} ET`;
}

// Compact 1.3k / 500k / 1.2M for token counts and similar.
export function formatNumCompact(n) {
  const x = Number(n);
  if (!isFinite(x)) return String(n);
  if (Math.abs(x) >= 1_000_000) return (x / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (Math.abs(x) >= 1_000)     return (x / 1_000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(x);
}

// Minimal markdown — only converts leading `- ` lines into a <ul>. Anything
// else is rendered as escaped text. The Gemma summary prompt only ever
// produces bullets vs. paragraph.
export function renderSummaryBody(text) {
  const lines = (text || '').split(/\r?\n/);
  const isBullet = l => /^\s*[-*]\s+/.test(l);
  if (lines.some(isBullet)) {
    const items = lines.filter(isBullet).map(l => {
      const content = l.replace(/^\s*[-*]\s+/, '');
      return `<li>${escapeHtml(content)}</li>`;
    });
    return `<ul>${items.join('')}</ul>`;
  }
  return escapeHtml(text);
}

// Field-aware payload renderer — maps common transcript-event payload shapes
// to compact human-readable HTML. Anything unrecognized falls through to a
// generic key/value tail; the full raw JSON is always available via
// click-to-expand for diagnostics.
const NOISE_FIELDS = new Set([
  'iterations', 'inference_geo', 'server_tool_use', 'cache_creation',
  'ephemeral_1h_input_tokens', 'ephemeral_5m_input_tokens',
  'thinking_chars',  // surfaced separately when nonzero
]);
const SECONDARY_BADGES = ['model', 'task_id', 'expected_output', 'speed', 'service_tier', 'source', 'parent_session_id', 'child_session_id'];

export function prettyPayload(payload) {
  if (payload == null) return '';
  if (typeof payload !== 'object') {
    return `<div class="pp-text">${escapeHtml(String(payload))}</div>`;
  }
  const parts = [];

  if (payload.routing) {
    let s = `<div class="pp-routing">→ <span class="pp-target">${escapeHtml(String(payload.routing))}</span>`;
    if (payload.routing_reason) s += ` <span class="pp-reason">· ${escapeHtml(payload.routing_reason)}</span>`;
    s += `</div>`;
    parts.push(s);
  }

  if (typeof payload.text === 'string' && payload.text.trim()) {
    parts.push(`<div class="pp-text">${escapeHtml(payload.text)}</div>`);
  } else if (payload.text === '' && Array.isArray(payload.tool_uses) && payload.tool_uses.length) {
    parts.push(`<div class="pp-text muted">(no text — called tools)</div>`);
  }

  if (Array.isArray(payload.tool_uses) && payload.tool_uses.length) {
    const tools = payload.tool_uses.map(t => {
      const name = t && t.name ? String(t.name) : 'tool';
      const argKeys = Array.isArray(t && t.input_keys)
        ? t.input_keys
        : (t && t.input && typeof t.input === 'object' ? Object.keys(t.input) : []);
      const argStr = argKeys.length ? `<span class="pp-tool-args">(${escapeHtml(argKeys.join(', '))})</span>` : '';
      return `<span class="pp-tool"><span class="pp-tool-name">${escapeHtml(name)}</span>${argStr}</span>`;
    }).join('');
    parts.push(`<div class="pp-tools">${tools}</div>`);
  }

  const TEXT_FIELDS = [
    ['question', 'question'], ['answer', 'answer'], ['prompt', 'prompt'],
    ['reason', 'reason'], ['ambiguity', 'ambiguity'], ['sane_reason', 'sane reason'],
    ['description', 'description'], ['label', 'label'],
  ];
  for (const [field, label] of TEXT_FIELDS) {
    const v = payload[field];
    if (typeof v === 'string' && v.trim()) {
      parts.push(`<div class="pp-field"><span class="pp-label">${escapeHtml(label)}</span><span class="pp-value">${escapeHtml(v)}</span></div>`);
    }
  }

  if (payload.budget && typeof payload.budget === 'object') {
    const b = payload.budget;
    const bits = [];
    if (b.wall_seconds != null)  bits.push(`${b.wall_seconds}s`);
    if (b.max_dollars != null)   bits.push(`$${Number(b.max_dollars).toFixed(2)}`);
    if (b.max_tokens != null)    bits.push(`${formatNumCompact(b.max_tokens)} tok`);
    if (bits.length) parts.push(`<div class="pp-budget"><span class="pp-label">budget</span>${escapeHtml(bits.join(' · '))}</div>`);
  }

  if (payload.usage && typeof payload.usage === 'object') {
    const u = payload.usage;
    const bits = [];
    if (u.input_tokens != null)  bits.push(`↓ ${formatNumCompact(u.input_tokens)} in`);
    if (u.output_tokens != null) bits.push(`↑ ${formatNumCompact(u.output_tokens)} out`);
    const cacheR = u.cache_read_input_tokens;
    const cacheC = u.cache_creation_input_tokens;
    if (cacheR || cacheC) {
      const cb = [];
      if (cacheR) cb.push(`${formatNumCompact(cacheR)} read`);
      if (cacheC) cb.push(`${formatNumCompact(cacheC)} create`);
      bits.push(`cache ${cb.join(' / ')}`);
    }
    if (bits.length) parts.push(`<div class="pp-usage"><span class="pp-label">usage</span>${escapeHtml(bits.join(' · '))}</div>`);
  }

  if (typeof payload.thinking_chars === 'number' && payload.thinking_chars > 0) {
    parts.push(`<div class="pp-field"><span class="pp-label">thinking</span><span class="pp-value">${formatNumCompact(payload.thinking_chars)} chars</span></div>`);
  }

  const badges = [];
  for (const f of SECONDARY_BADGES) {
    const v = payload[f];
    if (v != null && typeof v !== 'object') {
      badges.push(`<span class="pp-badge"><span class="pp-label">${escapeHtml(f.replace(/_/g, ' '))}</span>${escapeHtml(String(v))}</span>`);
    }
  }
  if (payload.sane === false) {
    badges.push(`<span class="pp-badge pp-warn">not sane</span>`);
  }
  if (badges.length) parts.push(`<div class="pp-badges">${badges.join('')}</div>`);

  const handled = new Set([
    'routing', 'routing_reason', 'text', 'tool_uses',
    ...TEXT_FIELDS.map(t => t[0]),
    'budget', 'usage', 'thinking_chars',
    ...SECONDARY_BADGES, 'sane',
    ...NOISE_FIELDS,
  ]);
  const remainder = Object.entries(payload).filter(([k, v]) =>
    !handled.has(k) && v != null
    && (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
  );
  if (remainder.length) {
    const items = remainder.map(([k, v]) =>
      `<span class="pp-kv"><span class="pp-label">${escapeHtml(k.replace(/_/g, ' '))}</span>${escapeHtml(String(v))}</span>`
    ).join('');
    parts.push(`<div class="pp-extra">${items}</div>`);
  }

  return parts.length ? parts.join('') : `<div class="pp-empty">(no fields)</div>`;
}

const _EVENTS_RETRY_DELAYS = [800, 1600, 3200];  // ms; ~5.6s before giving up

/**
 * A self-contained session-detail panel mounted into `container`.
 *
 * Options:
 *   container      — element the panel renders into (required)
 *   onLabelSaved(sessionId, customLabel) — called after a label edit saves,
 *                    so a caller with its own session list (the graph) can
 *                    nudge a node's displayed label immediately
 *   onSummaryFetched(sessionId, shortLabel) — same, for the LLM short label
 *   getDescendants(session) -> Session[] — non-terminal descendants to list
 *                    in the kill confirmation modal (graph-only; board
 *                    passes none)
 */
export class SessionPanel {
  constructor(opts = {}) {
    this.container = opts.container || null;
    this.onLabelSaved = opts.onLabelSaved || (() => {});
    this.onSummaryFetched = opts.onSummaryFetched || (() => {});
    this.getDescendants = opts.getDescendants || (() => []);
    this.sessionId = null;
    this.session = null;
    this.transcriptES = null;
    this.eventKindFilter = null;
    this.summaryAbort = null;
  }

  isOpenFor(sessionId) {
    return this.sessionId === sessionId;
  }

  close() {
    this.sessionId = null;
    this.session = null;
    this.eventKindFilter = null;
    if (this.transcriptES) { this.transcriptES.close(); this.transcriptES = null; }
    if (this.summaryAbort) { this.summaryAbort.abort(); this.summaryAbort = null; }
    if (this.container) this.container.innerHTML = '';
  }

  open(session) {
    if (!this.container || !session) return;
    if (this.transcriptES) { this.transcriptES.close(); this.transcriptES = null; }
    this.eventKindFilter = null;
    this.sessionId = session.session_id;
    this.session = session;
    this._renderHeader(session);
    this._fetchSummary(session.session_id);
    this._loadEvents(session.session_id, 0);
  }

  // In-place metadata refresh — keeps the events container intact across
  // polling ticks (snapshot / board). Only updates fields by data-field.
  updateMeta(s) {
    const root = this.container;
    if (!root || this.sessionId !== s.session_id) return;
    const setText = (sel, text) => {
      const el = root.querySelector(`[data-field="${sel}"]`);
      if (el) el.textContent = text;
    };
    const setHtml = (sel, html) => {
      const el = root.querySelector(`[data-field="${sel}"]`);
      if (el) el.innerHTML = html;
    };
    const inferredHint = s.status_inferred ? ' (inferred)' : '';
    const sourceLabel = sourceLabelFor(s);
    if (!root.querySelector('#label-edit-input')) {
      setText('label', s.custom_label || s.label || s.session_id);
    }
    setText('status', s.status + inferredHint);
    setText('source', sourceLabel);
    setText('routing', routingLabel(s.routing));
    setText('cost', '$' + (s.total_dollars || 0).toFixed(4));
    setText('tokens', `${s.total_input_tokens || 0}↓ / ${s.total_output_tokens || 0}↑`);
    setText('cwd-hint', s.decoded_cwd || '');

    const statusBadge = root.querySelector('[data-field="status"]');
    if (statusBadge) statusBadge.className = `badge status-${escapeAttr(s.status)}`;

    const live = !TERMINAL.has(s.status);
    setHtml('live-dot', live ? '<span class="live-dot" title="live"></span>' : '');
    setHtml('depth', s.spawn_depth ? `<span class="badge">depth ${s.spawn_depth}</span>` : '');

    const killBtn = root.querySelector('[data-action="kill"]');
    const isCli = (s.source === 'claude_code' || s.source === 'codex');
    if (live && !isCli && !killBtn) {
      const header = root.querySelector('.panel-header');
      if (header) {
        const closeBtn = header.querySelector('[data-action="close"]');
        const btn = document.createElement('button');
        btn.className = 'panel-kill';
        btn.dataset.action = 'kill';
        btn.textContent = 'Kill';
        btn.onclick = () => this.openKillModal(s);
        header.insertBefore(btn, closeBtn);
      }
    } else if ((!live || isCli) && killBtn) {
      killBtn.remove();
    }
    this.session = s;
  }

  _renderHeader(s) {
    const root = this.container;
    const live = !TERMINAL.has(s.status);
    const safeStatus = escapeAttr(s.status);
    const isCli = (s.source === 'claude_code' || s.source === 'codex');
    const isSubagent = !!s.is_subagent;
    const showKill = live && !isCli;
    const showResume = isCli && !isSubagent && (TERMINAL.has(s.status) || s.status === 'inactive' || s.status === 'yielded');
    const showGoTo = isCli && !isSubagent;
    const sourceLabel = sourceLabelFor(s);
    const inferredHint = s.status_inferred ? ' (inferred)' : '';
    root.innerHTML = `
      <div class="panel-header">
        <button class="panel-close" aria-label="Close" data-action="close">×</button>
        ${showKill ? '<button class="panel-kill" data-action="kill">Kill</button>' : ''}
        ${showResume ? '<button class="panel-resume" data-action="resume" title="Open a new wezterm tab and run claude --resume">Resume</button>' : ''}
        ${showGoTo ? '<button class="panel-focus" data-action="focus" title="Jump to the existing wezterm pane for this session (or run Resume if there isn\'t one).">Go To</button>' : ''}
        <div class="label" data-field="label" title="Click to rename this session">${escapeHtml(s.custom_label || s.label || s.session_id)}</div>
        <div class="cwd-hint" data-field="cwd-hint" style="font-size:0.7rem;color:var(--text-dim);margin-top:0.15rem;word-break:break-all">${s.decoded_cwd ? escapeHtml(s.decoded_cwd) : ''}</div>
        ${s.branch ? `<div class="branch-hint" data-field="branch-hint" style="font-size:0.7rem;color:var(--text-dim);margin-top:0.1rem">branch: ${escapeHtml(s.branch)}</div>` : ''}
        <div class="meta" data-field="meta">
          <span data-field="live-dot">${live ? '<span class="live-dot" title="live"></span>' : ''}</span>
          <span class="badge status-${safeStatus}" data-field="status">${escapeHtml(s.status + inferredHint)}</span>
          <span class="badge" data-field="source">${escapeHtml(sourceLabel)}</span>
          ${s.host ? `<span class="badge" data-field="host" title="Machine this session is running on">${escapeHtml(s.host)}</span>` : ''}
          <span class="badge" data-field="routing">${escapeHtml(routingLabel(s.routing))}</span>
          <span class="badge" data-field="cost">$${(s.total_dollars || 0).toFixed(4)}</span>
          <span class="badge" data-field="tokens">${s.total_input_tokens || 0}↓ / ${s.total_output_tokens || 0}↑</span>
          <span data-field="depth">${s.spawn_depth ? `<span class="badge">depth ${s.spawn_depth}</span>` : ''}</span>
        </div>
        ${s.prompt_preview ? `<div class="prompt-preview-hint" data-field="prompt-preview-hint" style="font-size:0.7rem;color:var(--text-dim);margin-top:0.15rem;word-break:break-word">“${escapeHtml(s.prompt_preview)}”</div>` : ''}
      </div>
      <div class="session-summary loading" data-field="session-summary">
        <div class="ss-label">Summary</div>
        <div class="ss-short" data-field="ss-short">${escapeHtml(s.short_label || '')}</div>
        <div class="ss-body" data-field="ss-body">Summarizing…</div>
      </div>
      <div class="events" data-field="events"></div>
    `;
    root.querySelector('[data-action="close"]').onclick = () => this.close();
    const labelEl = root.querySelector('[data-field="label"]');
    if (labelEl) labelEl.onclick = () => this._startLabelEdit(s);
    if (showKill) root.querySelector('[data-action="kill"]').onclick = () => this.openKillModal(s);
    if (showResume) root.querySelector('[data-action="resume"]').onclick = () => this._resumeSession(s);
    if (showGoTo) root.querySelector('[data-action="focus"]').onclick = () => this._focusSession(s);
  }

  async _focusSession(s) {
    const btn = this.container.querySelector('[data-action="focus"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Locating…'; }
    try {
      const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/focus`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
      });
      if (!r.ok) {
        const text = await r.text();
        let msg = text;
        try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
        if (r.status === 404) {
          showToast(`Couldn't locate pane — session not running, wezterm unreachable, or SessionStart hook not installed.`, true);
        } else if (r.status === 410) {
          showToast(`Pane no longer exists. Click Resume to open a new one.`, true);
        } else {
          showToast(`Go To failed: ${msg}`, true);
        }
        return;
      }
      showToast(`Pane selected in wezterm. Click the wezterm dock icon to bring it forward.`, false);
    } catch (err) {
      showToast(`Go To failed: ${err.message}`, true);
    } finally {
      setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = 'Go To'; } }, 1500);
    }
  }

  async _resumeSession(s) {
    const btn = this.container.querySelector('[data-action="resume"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Resuming…'; }
    try {
      const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/resume`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
      });
      if (!r.ok) {
        const text = await r.text();
        let msg = text;
        try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
        throw new Error(`HTTP ${r.status}: ${msg}`);
      }
      const result = await r.json();
      let copied = !!result.clipboard_copied;
      if (!copied && result.inner_command && navigator.clipboard && navigator.clipboard.writeText) {
        try { await navigator.clipboard.writeText(result.inner_command); copied = true; } catch (_) {}
      }
      if (result.pane_id != null) {
        showToast(`Wezterm tab opened (pane ${result.pane_id}). Focus button will return here.`, false);
      } else if (copied) {
        showToast(`Tab opened. Resume command copied to clipboard — paste it.`, false);
      } else if (result.inner_command) {
        showToast(`Tab opened. Run: ${result.inner_command}`, false);
      } else {
        showToast(`Spawned (pid ${result.pid}) in ${result.cwd}`, false);
      }
    } catch (err) {
      showToast(`Resume failed: ${err.message}`, true);
    } finally {
      setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = 'Resume'; } }, 4000);
    }
  }

  openKillModal(session) {
    const descendants = (this.getDescendants(session) || []).filter(d => !TERMINAL.has(d.status));
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-labelledby="kill-title">
        <h2 id="kill-title">Kill agent session?</h2>
        <div class="target">${escapeHtml(session.label || session.session_id)}</div>
        ${descendants.length > 0 ? `
          <div class="descendants">
            Will also kill ${descendants.length} descendant${descendants.length === 1 ? '' : 's'}:
            ${descendants.slice(0, 5).map(d => `<div>• ${escapeHtml(d.label || d.session_id)}</div>`).join('')}
            ${descendants.length > 5 ? `<div>…and ${descendants.length - 5} more</div>` : ''}
          </div>
        ` : ''}
        <label style="font-size:0.75rem;color:var(--text-secondary)">Reason (optional)</label>
        <textarea id="kill-reason" placeholder="Why are you killing this?"></textarea>
        <div class="actions">
          <button id="kill-cancel">Cancel</button>
          <button class="danger" id="kill-confirm">Kill</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
    backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
    backdrop.querySelector('#kill-cancel').onclick = cleanup;
    backdrop.querySelector('#kill-confirm').onclick = async () => {
      const reason = backdrop.querySelector('#kill-reason').value || '';
      const confirmBtn = backdrop.querySelector('#kill-confirm');
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Killing…';
      try {
        const r = await fetch(`/api/agents/sessions/${encodeURIComponent(session.session_id)}/kill`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }),
        });
        if (!r.ok) {
          const text = await r.text();
          throw new Error(`HTTP ${r.status}: ${text}`);
        }
        const result = await r.json();
        const failures = result.failures || [];
        const killed = result.killed || [];
        if (killed.length === 0 && result.reason) {
          showToast(`Already ${result.reason}`, false);
        } else if (failures.length === 0) {
          showToast(`Killed ${killed.length} session${killed.length === 1 ? '' : 's'}`, false);
        } else {
          showToast(`Killed ${killed.length}; ${failures.length} remote failure(s)`, true);
        }
        cleanup();
      } catch (err) {
        showToast(`Kill failed: ${err.message}`, true);
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'Kill';
      }
    };
  }

  _startLabelEdit(s) {
    const root = this.container;
    const labelEl = root.querySelector('[data-field="label"]');
    if (!labelEl || labelEl.querySelector('#label-edit-input')) return;
    const current = s.custom_label || s.label || '';
    const input = document.createElement('input');
    input.id = 'label-edit-input';
    input.type = 'text';
    input.maxLength = 120;
    input.value = current;
    input.style.cssText =
      'width:100%;box-sizing:border-box;font:inherit;font-weight:600;' +
      'color:var(--text-primary,#fff);background:var(--bg-elev);' +
      'border:1px solid var(--accent);border-radius:4px;padding:2px 6px';
    labelEl.textContent = '';
    labelEl.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (save) this._saveLabelEdit(s, input.value);
      else labelEl.textContent = s.custom_label || s.label || s.session_id;
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  async _saveLabelEdit(s, rawValue) {
    const root = this.container;
    const labelEl = root.querySelector('[data-field="label"]');
    const fallback = () => { if (labelEl) labelEl.textContent = s.custom_label || s.label || s.session_id; };
    try {
      const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/label`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: (rawValue || '').trim() }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const custom = data.custom_label || null;
      s.custom_label = custom;
      if (labelEl) labelEl.textContent = custom || s.label || s.session_id;
      this.onLabelSaved(s.session_id, custom);
    } catch (err) {
      console.warn('label save failed', err);
      fallback();
    }
  }

  async _fetchSummary(sessionId) {
    if (this.summaryAbort) this.summaryAbort.abort();
    const ac = new AbortController();
    this.summaryAbort = ac;
    const timeoutId = setTimeout(() => ac.abort(), 5 * 60 * 1000);
    try {
      const r = await fetch(`/api/agents/sessions/${encodeURIComponent(sessionId)}/summary`, { signal: ac.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (sessionId !== this.sessionId) return;
      const wrap = this.container.querySelector('[data-field="session-summary"]');
      if (!wrap) return;
      wrap.classList.remove('loading');
      const shortEl = wrap.querySelector('[data-field="ss-short"]');
      const bodyEl = wrap.querySelector('[data-field="ss-body"]');
      if (shortEl) shortEl.textContent = data.short_label || '';
      if (bodyEl) bodyEl.innerHTML = renderSummaryBody(data.summary || '');
      if (data.short_label) {
        if (this.session) this.session.short_label = data.short_label;
        this.onSummaryFetched(sessionId, data.short_label);
      }
    } catch (err) {
      if (sessionId !== this.sessionId) return;
      const bodyEl = this.container.querySelector('[data-field="session-summary"] [data-field="ss-body"]');
      if (!bodyEl) return;
      const friendly = err.name === 'AbortError'
        ? 'Timed out — Gemma may be busy.'
        : `Summary unavailable: ${err.message}.`;
      bodyEl.innerHTML = `${escapeHtml(friendly)} <a href="#" data-action="retry-summary" style="color:var(--accent)">Retry</a>`;
      const retry = bodyEl.querySelector('[data-action="retry-summary"]');
      if (retry) {
        retry.addEventListener('click', e => {
          e.preventDefault();
          bodyEl.textContent = 'Summarizing…';
          this.container.querySelector('[data-field="session-summary"]')?.classList.add('loading');
          this._fetchSummary(sessionId);
        });
      }
    } finally {
      clearTimeout(timeoutId);
      if (this.summaryAbort === ac) this.summaryAbort = null;
    }
  }

  _loadEvents(sessionId, attempt) {
    fetch(`/api/agents/sessions/${encodeURIComponent(sessionId)}/events?limit=200`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(payload => {
        if (sessionId !== this.sessionId) return;
        const events = payload.events || [];
        if (events.some(ev => (ev.kind || '') === 'user_message')) {
          this.eventKindFilter = 'user_message';
        }
        const container = this.container.querySelector('[data-field="events"]');
        if (container) container.innerHTML = '';
        events.forEach(ev => this._appendEvent(ev, false));
        this._applyEventFilter();
        const es = new EventSource(`/api/agents/sessions/${encodeURIComponent(sessionId)}/stream?backfill=0`);
        this.transcriptES = es;
        es.addEventListener('transcript_event', e => {
          try { this._appendEvent(JSON.parse(e.data), true); } catch (_) {}
        });
        es.addEventListener('closed', () => { es.close(); });
        es.onerror = () => {};  // browser auto-retries
      })
      .catch(err => {
        if (sessionId !== this.sessionId) return;
        const container = this.container.querySelector('[data-field="events"]');
        if (!container) return;
        if (attempt < _EVENTS_RETRY_DELAYS.length) {
          container.innerHTML = '<div class="event">Loading events… (reconnecting)</div>';
          setTimeout(() => {
            if (sessionId === this.sessionId) this._loadEvents(sessionId, attempt + 1);
          }, _EVENTS_RETRY_DELAYS[attempt]);
        } else {
          container.innerHTML = `<div class="event">Failed to load events: ${escapeHtml(String(err))} <a href="#" data-action="retry-events" style="color:var(--accent)">Retry</a></div>`;
          const retry = container.querySelector('[data-action="retry-events"]');
          if (retry) retry.addEventListener('click', ev => {
            ev.preventDefault();
            container.innerHTML = '<div class="event">Loading events…</div>';
            this._loadEvents(sessionId, 0);
          });
        }
      });
  }

  _appendEvent(ev, _live) {
    const container = this.container.querySelector('[data-field="events"]');
    if (!container) return;
    const kind = String(ev.kind || '');
    const ts = formatTs(ev.ts);
    const payload = ev.payload || null;
    const prettyHtml = payload ? prettyPayload(payload) : '';
    const rawJson = payload ? JSON.stringify(payload, null, 2) : '';
    const div = document.createElement('div');
    div.classList.add('event');
    if (kind) {
      const safeKind = kind.replace(/[^a-zA-Z0-9_-]/g, '_');
      div.classList.add(`kind-${safeKind}`);
      div.dataset.kind = kind;
    }
    div.innerHTML = `
      <div><span class="kind" role="button" tabindex="0" title="Click to filter to this event type">${escapeHtml(kind)}</span><span class="ts">${escapeHtml(ts)}</span></div>
      ${prettyHtml ? `<div class="payload" title="Click to expand">${prettyHtml}</div>` : ''}
      ${rawJson ? `<pre class="payload-raw">${escapeHtml(rawJson)}</pre>` : ''}
    `;
    if (this.eventKindFilter && kind !== this.eventKindFilter) {
      div.style.display = 'none';
    }
    const kindEl = div.querySelector('.kind');
    if (kindEl && kind) {
      kindEl.addEventListener('click', e => { e.stopPropagation(); this._toggleKindFilter(kind); });
      kindEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this._toggleKindFilter(kind); }
      });
    }
    div.addEventListener('click', e => {
      if (e.target.closest('.kind')) return;
      div.classList.toggle('expanded');
    });
    container.prepend(div);
  }

  _toggleKindFilter(kind) {
    this.eventKindFilter = (this.eventKindFilter === kind) ? null : kind;
    this._applyEventFilter();
  }

  _applyEventFilter() {
    const container = this.container.querySelector('[data-field="events"]');
    if (!container) return;
    container.querySelectorAll('.event').forEach(el => {
      const k = el.dataset.kind;
      el.style.display = (!this.eventKindFilter || k === this.eventKindFilter) ? '' : 'none';
    });
    container.querySelectorAll('.kind').forEach(el => {
      const k = el.parentElement?.parentElement?.dataset?.kind;
      el.classList.toggle('kind-active-filter', !!(this.eventKindFilter && k === this.eventKindFilter));
    });
  }
}
