// web/agents/session_actions.js
//
// Single source of truth for the drawer/panel action row: which actions
// apply to a given session (+ its linked board card, when the surface has
// one), whether each is enabled or disabled-with-a-reason, and how each
// one actually behaves. Both the Board tab's card drawer
// (web/agents/board.js) and the shared session panel (web/agents/panel.js,
// mounted by both the Board drawer and the Graph tab's side panel) render
// their action row by calling `renderActionRow` here — neither surface
// keeps its own copy of the eligibility rules or reimplements Kill's
// cascade-preview modal, Resume's host select, or Go To's pane lookup.
//
// `decideActions` is the pure decision function — no DOM, directly
// unit-testable (see tests/test_agents_action_parity_browser.py) — so the
// eligibility rules for both surfaces can be verified without a rendered
// drawer or panel. `card` may be omitted/undefined: a session with no
// linked board card (the Graph tab's panel before the linked card is
// known, or a session the board never tracked at all) skips any decision
// that needs `card` (Open, Accept, Reject, Reassign, Mark Done, Cancel, Delete) rather than
// inventing one; Answer reads a pending question off `card` when present,
// off the session directly otherwise (both carry the identical
// `_pending_question_view` shape from the server).
//
// `renderActionRow` builds the actual buttons into a caller-supplied
// container. Kill, Resume, and Go To are rendered with the panel's
// existing chrome (`panel-kill` / `panel-resume` / `panel-resume-host` /
// `panel-focus` — those classes don't float-position correctly unless
// their container establishes its own block formatting context, which the
// caller's own `.panel-header-actions` / `.drawer-actions` wrapper does)
// and their behaviour is built in here; the card-only actions (Open,
// Accept, Reject, Reassign, Mark Done, Cancel, Delete) have no generic cross-card behaviour
// (they need a task manager id and, for Delete, a confirmation modal) so
// the caller supplies a handler per id via `handlers` — both the Board
// drawer (web/agents/board.js) and a card-linked Graph tab panel
// (web/agents/panel.js, via ./card_actions.js) do, since a card can now
// reach either surface. Rename likewise has no generic behaviour (it needs
// the caller's own label-edit UI) and is always supplied via `handlers`.

import { nodeLabel } from './graph_encoding.js';

export const TERMINAL = new Set(['completed', 'failed', 'budget_exceeded', 'ended']);

export function sourceLabelFor(d) {
  if (d.source === 'claude_code') return 'Claude Code CLI';
  if (d.source === 'codex') return 'Codex CLI';
  return 'LifeOS agent';
}

// A session is a subagent either because the worker flagged it directly
// (`is_subagent`) or, for a routing-derived CLI subagent, because it
// carries `parent_session_id` with no such flag at all — the Graph tab's
// node rendering (web/agents/graph.js) already tests both; this is the one
// place the drawer/panel action decisions do too, so a routing-derived
// subagent can't offer Resume or Focus on one surface and correctly
// refuse it on another.
export function isSubagentSession(s) {
  return !!(s && (s.is_subagent || s.parent_session_id));
}

function isCliSession(s) {
  return !!(s && (s.source === 'claude_code' || s.source === 'codex'));
}

// Single source of truth for whether a session should offer Resume + the
// resume-host select.
export function showResumeFor(s) {
  return isCliSession(s) && !isSubagentSession(s)
    && (TERMINAL.has(s.status) || s.status === 'inactive' || s.status === 'yielded');
}

// Single source of truth for whether a session should offer Focus
// ("Go To" the existing wezterm pane) — every CLI session that isn't a
// subagent, live or not (unlike Resume, which only wants a stopped one).
export function canFocusFor(s) {
  return isCliSession(s) && !isSubagentSession(s);
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

export function showToast(message, isError, options = {}) {
  const t = document.createElement('div');
  t.className = 'toast' + (isError ? ' error' : '');
  const text = document.createElement('span');
  text.textContent = message;
  t.appendChild(text);
  const duration = options.duration || 3500;
  if (options.actionLabel && typeof options.onAction === 'function') {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'toast-action';
    action.textContent = options.actionLabel;
    action.setAttribute('aria-label', options.actionAriaLabel || options.actionLabel);
    action.addEventListener('click', async () => {
      action.disabled = true;
      try {
        await options.onAction({ toast: t, action });
      } finally {
        action.disabled = false;
      }
    });
    t.appendChild(action);
  }
  document.body.appendChild(t);
  setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, duration);
  // Callers that need to replace or dismiss a toast can use the returned
  // element; the timer remains owned by this shared helper.
  t.dataset.duration = String(duration);
  return t;
}

// ---------------------------------------------------------------------
// Decision — the ordered, canonical action set. Both surfaces render
// exactly this order: Open, Rename, Go To, Resume, Kill, Answer, Accept,
// Reject, Reassign, Mark Done, Cancel, Delete.
// ---------------------------------------------------------------------

export function decideActions(session, card) {
  const s = session || null;
  const c = card || null;
  const out = [];

  // Open — a task card assigned to a CLI engine sitting in the assigned
  // lane. Card-only: a bare session (Graph tab, no linked card) never
  // offers it.
  if (c && c.lane === 'assigned' && (c.assignee === 'claude' || c.assignee === 'codex')) {
    out.push({ id: 'open', label: 'Open', enabled: true, reason: null, danger: false });
  }

  // Rename — inline-edit the session's own label. Session-level, not
  // card-only: offered wherever a session is linked at all, same as Go To.
  if (s) {
    out.push({ id: 'rename', label: 'Rename', enabled: true, reason: null, danger: false });
  }

  // Go To — jump to the existing wezterm pane. Every CLI session that
  // isn't a subagent, whether or not it's still live.
  if (s && canFocusFor(s)) {
    out.push({ id: 'focus', label: 'Go To', enabled: true, reason: null, danger: false });
  }

  // Resume — the button and host select are decided (mounted) the moment
  // the session is CLI and not a subagent, same as Go To, so a caller that
  // keeps re-rendering in place as a session's status changes (the Graph
  // panel's `updateMeta`, on every poll tick) already has the elements to
  // reveal once the session actually becomes resumable, rather than
  // creating/discarding them on every status flip. `visible` (not
  // `enabled` — Resume is never shown disabled-with-a-reason, only shown
  // or hidden) carries the moment-to-moment "is it actually resumable
  // right now" signal `showResumeFor` computes.
  if (s && canFocusFor(s)) {
    out.push({ id: 'resume', label: 'Resume', enabled: true, reason: null, danger: false, visible: showResumeFor(s) });
  }

  // Kill — disabled-with-reason for a live CLI-backed session (this
  // endpoint can't tear one down yet — the operator has to close it by
  // hand); offered live for anything else non-terminal.
  if (s && !TERMINAL.has(s.status)) {
    if (isCliSession(s)) {
      out.push({
        id: 'kill', label: 'Kill', enabled: false, danger: false,
        reason: `killing a live ${sourceLabelFor(s)} session isn't supported yet — close it manually`,
      });
    } else {
      out.push({ id: 'kill', label: 'Kill', enabled: true, reason: null, danger: false });
    }
  }

  // Answer — a pending operator question. The card's own view wins when a
  // card is present (matches the server's `_task_card` view exactly); a
  // bare session on the Graph tab falls back to its own `pending_question`
  // field, which every snapshot row already carries.
  const pendingQuestion = c ? c.pending_question : (s && s.pending_question);
  if (pendingQuestion) {
    out.push({ id: 'answer', label: 'Answer', enabled: true, reason: null, danger: false });
  }

  // Accept — a card sitting in Review. Card-only.
  if (c && c.lane === 'review') {
    out.push({ id: 'accept', label: 'Accept', enabled: true, reason: null, danger: false });
    // Reject resumes the prior session with the operator's note, so it needs
    // one; Reassign only needs a target assignee and carries whatever
    // prior-run context exists, so it stands on its own. Both stay on the row
    // either way — a Review card with no session reports why Reject is
    // unavailable, the same disabled-and-explained treatment Kill and Cancel
    // give their own refusals, rather than leaving the card's own Notes field
    // looking like the place to type feedback.
    out.push({
      id: 'reject', label: 'Reject', enabled: !!s, danger: false,
      reason: s ? null : 'no prior agent session to resume with your note',
    });
    out.push({ id: 'reassign', label: 'Reassign', enabled: true, reason: null, danger: false });
  }

  // Mark Done — a manually-filed Human queue card with no pending question
  // behind it, and only when the equivalent drop-onto-Done move is itself
  // allowed. `policy.lanes` lists ONLY refused lanes — an absent `.done`
  // entry means allowed. Card-only.
  if (c) {
    const doneEntry = c.policy && c.policy.lanes && c.policy.lanes.done;
    const doneAllowed = !doneEntry || doneEntry.allowed !== false;
    if (c.lane === 'human_queue' && !pendingQuestion && doneAllowed) {
      out.push({ id: 'resolve', label: 'Mark Done', enabled: true, reason: null, danger: false });
    }
  }

  // Cancel — every task card that carries a policy block, disabled-and-
  // explained when refused, never hidden. Card-only (a schedule card
  // never carries `policy`).
  if (c && c.policy && c.policy.cancel) {
    const allowed = c.policy.cancel.allowed === true;
    out.push({
      id: 'cancel', label: 'Cancel', enabled: allowed, danger: false,
      reason: allowed ? null : (c.policy.cancel.reason || "Cancel isn't available for this card."),
    });
  }

  // Delete — offered for every card the drawer can open (task or
  // schedule), always last, styled danger. Card-only: a bare session on
  // the Graph tab has nothing here to delete.
  if (c) {
    out.push({ id: 'delete', label: 'Delete', enabled: true, reason: null, danger: true });
  }

  return out;
}

// ---------------------------------------------------------------------
// Answer — generic: needs only the pending-question object itself
// (`{id, question, ...}`, the shape `_pending_question_view` emits on both
// a card and a session), not the whole card, so both surfaces can use it.
// ---------------------------------------------------------------------

export function openAnswerPrompt(pendingQuestion, onSent) {
  if (!pendingQuestion) return;
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal" role="dialog" aria-labelledby="answer-title">
      <h2 id="answer-title">Answer</h2>
      <div class="target">${escapeHtml(pendingQuestion.question)}</div>
      <textarea id="answer-text" placeholder="Your answer…"></textarea>
      <div class="actions">
        <button id="answer-cancel">Cancel</button>
        <button class="danger" id="answer-send">Send</button>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);
  const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
  backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
  backdrop.querySelector('#answer-cancel').onclick = cleanup;
  backdrop.querySelector('#answer-send').onclick = async () => {
    const answer = backdrop.querySelector('#answer-text').value.trim();
    if (!answer) return;
    const btn = backdrop.querySelector('#answer-send');
    btn.disabled = true;
    btn.textContent = 'Sending…';
    try {
      const r = await fetch(`/api/agents/pending-questions/${encodeURIComponent(pendingQuestion.id)}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer }),
      });
      if (!r.ok) throw new Error(await r.text());
      showToast('Answer sent.', false);
      cleanup();
      if (onSent) onSent();
    } catch (err) {
      showToast(`Couldn't send answer: ${err.message}`, true);
      btn.disabled = false;
      btn.textContent = 'Send';
    }
  };
}

// ---------------------------------------------------------------------
// Kill — the confirmation modal with its cascade preview of non-terminal
// descendants. `getDescendants` may return either an array (the Graph
// tab's side panel already holds every known session, so it resolves
// synchronously) or a Promise of one (the Board drawer fetches
// `/api/agents/snapshot` on demand — see web/agents/board.js) — the modal
// itself renders immediately either way, so a fetch never delays the
// modal's appearance, but its destructive confirm button stays disabled
// until `getDescendants` settles: an operator must see the preview (or an
// explicit "couldn't check" notice, on a failed lookup — never silently
// treated as "no descendants") before being able to confirm.
// `getDescendants` defaults to an empty list.
// ---------------------------------------------------------------------

export function openKillModal(session, { getDescendants = () => [], onKilled } = {}) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal" role="dialog" aria-labelledby="kill-title">
      <h2 id="kill-title">Kill agent session?</h2>
      <div class="target">${escapeHtml(nodeLabel(session))}</div>
      <div class="descendants" data-field="kill-descendants" hidden></div>
      <label style="font-size:0.75rem;color:var(--text-secondary)">Reason (optional)</label>
      <textarea id="kill-reason" placeholder="Why are you killing this?"></textarea>
      <div class="actions">
        <button id="kill-cancel">Cancel</button>
        <button class="danger" id="kill-confirm" disabled>Kill</button>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);
  const confirmBtn = backdrop.querySelector('#kill-confirm');
  // The destructive confirm stays disabled until the descendant-preview
  // lookup settles, one way or the other — an operator must never be able
  // to confirm what could be a cascading kill before the preview has had
  // a chance to say so. A failed lookup is disclosed explicitly rather
  // than silently rendered as "no descendants" — see
  // web/agents/board.js's `fetchDescendantsForKill`, which propagates a
  // fetch failure instead of swallowing it into an empty list.
  Promise.resolve().then(() => getDescendants(session)).then(list => {
    const descendants = (list || []).filter(d => !TERMINAL.has(d.status));
    if (!descendants.length) return;
    const el = backdrop.querySelector('[data-field="kill-descendants"]');
    if (!el) return;  // modal already dismissed
    el.hidden = false;
    el.innerHTML = `
      Will also kill ${descendants.length} descendant${descendants.length === 1 ? '' : 's'}:
      ${descendants.slice(0, 5).map(d => `<div>• ${escapeHtml(nodeLabel(d))}</div>`).join('')}
      ${descendants.length > 5 ? `<div>…and ${descendants.length - 5} more</div>` : ''}
    `;
  }).catch(() => {
    const el = backdrop.querySelector('[data-field="kill-descendants"]');
    if (!el) return;  // modal already dismissed
    el.hidden = false;
    el.textContent = "Couldn't check for descendant sessions — if any exist, they'll be killed too.";
  }).finally(() => {
    if (confirmBtn.isConnected) confirmBtn.disabled = false;
  });
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
      if (onKilled) onKilled();
    } catch (err) {
      showToast(`Kill failed: ${err.message}`, true);
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Kill';
    }
  };
}

// ---------------------------------------------------------------------
// Resume + Focus — operate on a `root` element that must contain, by
// `data-action`/`data-field`, the elements `renderActionRow` (below)
// creates: `[data-action="resume"]`, `[data-action="resume-host"]`,
// `[data-action="focus"]`, `[data-field="resume-command"]` (with a
// `[data-field="resume-command-text"]` `<code>` and a
// `[data-action="copy-resume-command"]` button inside it).
// ---------------------------------------------------------------------

// "Resume here" host list — same across every panel instance, so fetch it
// once per page load and cache the promise rather than re-fetching on
// every render. If GET /api/agents/hosts is unavailable (404), takes too
// long, or the request fails, `_resumeHosts()` returns null and
// `populateResumeHosts` builds a fallback list itself. A null/empty result
// re-arms the cache so a later interaction retries instead of being stuck
// with a permanently empty select for the page's whole life.
let _resumeHostsPromise = null;

async function _resumeHosts() {
  if (!_resumeHostsPromise) {
    _resumeHostsPromise = fetch('/api/agents/hosts', { signal: AbortSignal.timeout(5000) })
      .then(r => (r.ok ? r.json() : null))
      .then(data => (data && Array.isArray(data.hosts) ? data.hosts : null))
      .then(hosts => {
        const valid = (hosts || []).filter(h => h && typeof h.name === 'string' && h.name);
        return valid.length ? valid : null;
      })
      .catch(() => null);
    _resumeHostsPromise.then(hosts => {
      if (!hosts) _resumeHostsPromise = null;
    });
  }
  return _resumeHostsPromise;
}

// The API host's own name, for the fallback list when
// `/api/agents/hosts` isn't available — `GET /api/agents/snapshot` (which
// every page already polls) carries it. Cached the same way, with the
// same retry-on-failure re-arming as `_resumeHosts()`.
let _apiHostPromise = null;

async function _apiHostName() {
  if (!_apiHostPromise) {
    _apiHostPromise = fetch('/api/agents/snapshot', { signal: AbortSignal.timeout(5000) })
      .then(r => (r.ok ? r.json() : null))
      .then(data => (data && typeof data.api_host === 'string' && data.api_host) ? data.api_host : null)
      .catch(() => null);
    _apiHostPromise.then(name => {
      if (!name) _apiHostPromise = null;
    });
  }
  return _apiHostPromise;
}

export async function populateResumeHosts(root, s) {
  const select = root.querySelector('[data-action="resume-host"]');
  if (!select) return;
  let hosts = await _resumeHosts();
  if (!hosts || !hosts.length) {
    const apiHost = await _apiHostName();
    hosts = [];
    const seen = new Set();
    if (apiHost) { hosts.push({ name: apiHost, is_api_host: true }); seen.add(apiHost); }
    if (s.host && !seen.has(s.host)) hosts.push({ name: s.host, is_api_host: false });
    // No API host is knowable and this session carries no recorded host
    // either — there is genuinely nothing to offer. Use an EMPTY value,
    // not the human-readable placeholder "this host": that string would
    // get sent verbatim as `target_host`, a machine identifier the backend
    // 400s on. An empty value omits `target_host` from the request body.
    if (!hosts.length) hosts = [{ name: '', label: 'this host', is_api_host: false }];
  } else {
    hosts = hosts.slice().sort((a, b) => (b.is_api_host ? 1 : 0) - (a.is_api_host ? 1 : 0));
    if (s.host && !hosts.some(h => h.name === s.host)) {
      hosts.push({ name: s.host, is_api_host: false });
    }
  }
  // Build options as real elements and assign `.value` / `.textContent` as
  // PROPERTIES — a template-string `value="${escapeAttr(...)}"` mangles a
  // dotted/hyphenated host (e.g. "mac-mini.local").
  select.innerHTML = '';
  for (const h of hosts) {
    const suffix = h.is_api_host ? ' (this machine)' : '';
    const opt = document.createElement('option');
    opt.value = h.name;
    opt.textContent = h.label || (h.name + suffix);
    select.appendChild(opt);
  }
  if (s.host && hosts.some(h => h.name === s.host)) select.value = s.host;
}

function _showResumeCommand(root, command, note) {
  const box = root.querySelector('[data-field="resume-command"]');
  const codeEl = root.querySelector('[data-field="resume-command-text"]');
  if (box && codeEl && command) {
    codeEl.textContent = command;
    box.hidden = false;
  }
  showToast(note || 'Copy the command below to run it on that host.', true);
}

export function hideResumeCommand(root) {
  const box = root.querySelector('[data-field="resume-command"]');
  if (box) box.hidden = true;
}

async function _copyResumeCommand(root) {
  const codeEl = root.querySelector('[data-field="resume-command-text"]');
  const text = codeEl ? codeEl.textContent : '';
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); showToast('Command copied.', false); return; } catch (_) {}
  }
  showToast('Select and copy the command manually.', true);
}

export async function focusSession(root, s) {
  const btn = root.querySelector('[data-action="focus"]');
  const select = root.querySelector('[data-action="resume-host"]');
  const targetHost = select ? select.value : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Locating…'; }
  try {
    const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/focus`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(targetHost ? { target_host: targetHost } : {}),
    });
    if (!r.ok) {
      const text = await r.text();
      let detail = text;
      try { const j = JSON.parse(text); detail = (j.detail !== undefined) ? j.detail : text; } catch (_) {}
      if (r.status === 400 && detail && typeof detail === 'object' && detail.command) {
        _showResumeCommand(root, detail.command, detail.error);
        return;
      }
      const msg = (detail && typeof detail === 'object') ? (detail.error || JSON.stringify(detail)) : detail;
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

export async function resumeSession(root, s) {
  const btn = root.querySelector('[data-action="resume"]');
  const select = root.querySelector('[data-action="resume-host"]');
  const targetHost = select ? select.value : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Resuming…'; }
  hideResumeCommand(root);
  try {
    const r = await fetch(`/api/agents/sessions/${encodeURIComponent(s.session_id)}/resume`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(targetHost ? { target_host: targetHost } : {}),
    });
    if (!r.ok) {
      const text = await r.text();
      let detail = text;
      try { const j = JSON.parse(text); detail = (j.detail !== undefined) ? j.detail : text; } catch (_) {}
      if (r.status === 400 && detail && typeof detail === 'object' && detail.command) {
        _showResumeCommand(root, detail.command, detail.error);
        return;
      }
      const msg = (detail && typeof detail === 'object') ? (detail.error || JSON.stringify(detail)) : detail;
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

// ---------------------------------------------------------------------
// The shared row renderer — turns `decideActions(session, card)` into
// buttons inside `container`. Skips rebuilding when nothing about the
// decided set has changed since the last call (same ids, enabled states,
// and reasons) so a caller that re-renders on every poll tick (the Graph
// panel's `updateMeta`) doesn't blow away an in-progress interaction — a
// chosen resume host, a visible resume-command box — for no reason.
// ---------------------------------------------------------------------

export function renderActionRow(container, opts = {}) {
  const { session = null, card = null, handlers = {}, getDescendants = () => [], onChange = () => {} } = opts;
  const descriptors = decideActions(session, card);
  // A pending question replaced by a new one (answered elsewhere while the
  // drawer/panel stays open) keeps Answer's id/enabled/reason identical —
  // include the question's own id so that case still rebuilds and rebinds
  // Answer to the new question instead of continuing to post to the old
  // one's now-stale id.
  const pendingQuestion = card ? card.pending_question : (session && session.pending_question);
  const pendingId = pendingQuestion ? pendingQuestion.id : null;
  // `visible` (Resume's own moment-to-moment show/hide signal, decided by
  // `showResumeFor`) is deliberately NOT part of this signature — Resume's
  // button/select/command-box are mounted once, the instant the session is
  // eligible at all, precisely so a status flip can reveal or hide them in
  // place (below) without rebuilding the row and discarding a chosen
  // resume host or a visible copy-command box.
  const sig = JSON.stringify([
    pendingId,
    descriptors.map(d => [d.id, d.enabled, d.reason]),
  ]);
  if (container.dataset.actionsSig === sig) {
    const resumeDescriptor = descriptors.find(d => d.id === 'resume');
    if (resumeDescriptor) {
      const hideForNow = resumeDescriptor.visible === false;
      const resumeBtn = container.querySelector('[data-action="resume"]');
      const resumeHost = container.querySelector('[data-action="resume-host"]');
      if (resumeBtn) resumeBtn.hidden = hideForNow;
      if (resumeHost) resumeHost.hidden = hideForNow;
    }
    return;
  }
  container.dataset.actionsSig = sig;
  container.innerHTML = '';

  for (const d of descriptors) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.action = d.id;
    if (d.id === 'kill') btn.className = 'panel-kill';
    else if (d.id === 'resume') btn.className = 'panel-resume';
    else if (d.id === 'focus') btn.className = 'panel-focus';
    else btn.className = 'drawer-action' + (d.danger ? ' danger' : '');
    btn.textContent = d.label;

    if (!d.enabled) {
      btn.disabled = true;
      container.appendChild(btn);
      const reasonEl = document.createElement('div');
      reasonEl.className = 'drawer-field-reason';
      reasonEl.dataset.field = `${d.id}-reason`;
      reasonEl.textContent = d.reason;
      container.appendChild(reasonEl);
      continue;
    }

    container.appendChild(btn);

    if (d.id === 'resume') {
      // `visible` (not `enabled`) is Resume's own moment-to-moment
      // show/hide signal — mounted the instant the session is eligible at
      // all (see `decideActions`) so an in-place refresh (`updateMeta`
      // polling) can reveal it later without recreating it, hidden until
      // `showResumeFor` actually says so.
      const hideForNow = d.visible === false;
      btn.hidden = hideForNow;
      const select = document.createElement('select');
      select.className = 'panel-resume-host';
      select.dataset.action = 'resume-host';
      select.hidden = hideForNow;
      container.appendChild(select);
      const box = document.createElement('div');
      box.className = 'resume-command';
      box.dataset.field = 'resume-command';
      box.hidden = true;
      const code = document.createElement('code');
      code.dataset.field = 'resume-command-text';
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.dataset.action = 'copy-resume-command';
      copyBtn.textContent = 'Copy';
      box.appendChild(code);
      box.appendChild(copyBtn);
      container.appendChild(box);
      copyBtn.onclick = () => _copyResumeCommand(container);
      btn.onclick = () => resumeSession(container, session);
      populateResumeHosts(container, session);
    } else if (d.id === 'focus') {
      btn.onclick = () => focusSession(container, session);
    } else if (d.id === 'kill') {
      btn.onclick = () => openKillModal(session, { getDescendants, onKilled: onChange });
    } else if (d.id === 'answer') {
      btn.onclick = () => openAnswerPrompt(pendingQuestion, onChange);
    } else if (handlers[d.id]) {
      btn.onclick = async () => {
        // Accept is destructive enough that duplicate clicks must not issue
        // overlapping requests while the server is committing its CAS.
        if (d.id === 'accept') btn.disabled = true;
        try { await handlers[d.id](); }
        finally { if (d.id === 'accept' && btn.isConnected) btn.disabled = false; }
      };
    }
  }
}
