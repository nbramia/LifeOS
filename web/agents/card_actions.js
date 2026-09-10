// web/agents/card_actions.js
//
// The network call, toast, and (for Delete) the kill-then-delete
// confirmation modal behind each card-only action in the shared action row
// (Open, Accept, Reject, Reassign, Mark Done, Cancel, Delete — see ./session_actions.js's
// `decideActions`) — the part that's identical wherever a card-aware panel
// offers them: the Board drawer (web/agents/board.js) and a card-linked
// Graph tab side panel (web/agents/panel.js). A caller supplies `onChanged`
// (refresh this surface's own view of the card after a write succeeds) and,
// for Delete, `findCard` (resolve the freshest copy of the card at confirm
// time, since a card can change lane/session between the button being drawn
// and the operator confirming — defaults to the card captured when the
// button was clicked, when a caller has no live lookup of its own).

import { TERMINAL, sourceLabelFor, escapeHtml, showToast } from './session_actions.js';
import { LANES } from './lanes.js';
import { refreshAfterFailure } from './action_refresh.js';

function laneLabelFor(laneId) {
  const lane = LANES.find(l => l.id === laneId);
  return lane ? lane.label : laneId;
}

export async function openCard(card, onChanged) {
  try {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/open`, { method: 'POST' });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    showToast('Opened.', false);
    if (onChanged) await onChanged();
  } catch (err) { showToast(`Open failed: ${err.message}`, true); }
}

export async function undoAcceptedCard(cardId, onChanged) {
  try {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(cardId)}/undo-accept`, { method: 'POST' });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    showToast('Acceptance undone.', false);
    if (onChanged) await onChanged();
  } catch (err) {
    showToast(`Undo failed: ${err.message}`, true);
    // The write may have failed because another actor changed the card. Keep
    // the original failure visible, but refresh the caller's board so the
    // stale card/action row is not left on screen.
    await refreshAfterFailure(onChanged);
    throw err;
  }
}

export async function acceptCard(card, onChanged, onAccepted) {
  try {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/accept`, { method: 'POST' });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    showToast('Accepted.', false, {
      duration: 3500 * 1.5,
      actionLabel: 'Undo',
      actionAriaLabel: 'Undo acceptance',
      onAction: ({ toast, action }) => {
        action.textContent = 'Undoing…';
        return undoAcceptedCard(card.id, onChanged)
          .then(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); })
          .catch(() => { action.textContent = 'Undo'; });
      },
    });
    if (onAccepted) onAccepted();
    if (onChanged) await onChanged();
  } catch (err) { showToast(`Accept failed: ${err.message}`, true); }
}

/**
 * Reject a completed review with operator context, or reassign it while
 * retaining the prior session transcript as the next run's context.
 */
export function openReviewActionModal(card, action, onChanged) {
  const isReject = action === 'reject';
  const title = isReject ? 'Reject review' : 'Reassign review';
  const assignees = ['me', 'claude', 'codex', 'hermes', 'local', 'cloud'];
  const assigneeHtml = isReject ? '' : `
    <label for="review-assignee">Assignee</label>
    <select id="review-assignee">
      ${assignees.map(a => `<option value="${a}" ${a === card.assignee ? 'selected' : ''}>${a}</option>`).join('')}
    </select>
  `;
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal" role="dialog" aria-labelledby="review-action-title">
      <h2 id="review-action-title">${title}</h2>
      <div class="target">${escapeHtml(card.title || card.id)}</div>
      ${assigneeHtml}
      <label for="review-note">${isReject ? 'Note (required)' : 'Context note (optional)'}</label>
      <textarea id="review-note" placeholder="${isReject ? 'What should be changed?' : 'What should the next run know?'}"></textarea>
      <div class="actions">
        <button id="review-action-cancel">Cancel</button>
        <button class="danger" id="review-action-submit">${isReject ? 'Reject' : 'Reassign'}</button>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);
  const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
  backdrop.addEventListener('click', e => { if (e.target === backdrop) cleanup(); });
  backdrop.querySelector('#review-action-cancel').onclick = cleanup;
  backdrop.querySelector('#review-action-submit').onclick = async () => {
    const note = backdrop.querySelector('#review-note').value.trim();
    if (isReject && !note) return;
    const btn = backdrop.querySelector('#review-action-submit');
    btn.disabled = true;
    btn.textContent = isReject ? 'Rejecting…' : 'Reassigning…';
    const payload = { action, note };
    if (!isReject) payload.assignee = backdrop.querySelector('#review-assignee').value;
    try {
      const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/review-action`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const text = await r.text();
        let msg = text;
        try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
        throw new Error(msg || `HTTP ${r.status}`);
      }
      showToast(isReject ? 'Review rejected; resuming with your note.' : 'Review reassigned.', false);
      cleanup();
      if (onChanged) await onChanged(await r.json());
    } catch (err) {
      showToast(`${isReject ? 'Reject' : 'Reassign'} failed: ${err.message}`, true);
      btn.disabled = false;
      btn.textContent = isReject ? 'Reject' : 'Reassign';
    }
  };
}

// Mark Done is a drop onto Done under the hood.
export async function resolveCard(card, onChanged) {
  try {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/lane`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lane: 'done' }),
    });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    const data = await r.json();
    // The server-landed lane can differ from what was requested (e.g. a
    // Human-queue card assigned to someone stays in Human queue) — surface
    // that instead of leaving the operator to notice the card "snapped
    // back" on its own.
    if (data && data.lane && data.lane !== 'done') {
      showToast(`Card landed in ${laneLabelFor(data.lane)}, not Done.`, false);
    }
    if (onChanged) await onChanged(data);
  } catch (err) { showToast(`Couldn't resolve card: ${err.message}`, true); }
}

export async function cancelCard(card, onChanged) {
  try {
    const r = await fetch(`/api/agents/board/cards/${encodeURIComponent(card.id)}/cancel`, { method: 'POST' });
    if (!r.ok) {
      const text = await r.text();
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    const data = await r.json();
    // A live cc:/cx: CLI session can't be torn down by Cancel yet — the
    // endpoint still marks the card cancelled, but reports it under
    // `failures` instead of silently claiming a teardown it didn't perform.
    const untorn = (data && data.failures) || [];
    if (untorn.length) {
      showToast(`Cancelled, but couldn't stop: ${untorn.map(f => f.reason || f.session_id).join('; ')}`, true);
    } else {
      showToast('Cancelled.', false);
    }
    if (onChanged) await onChanged(data);
  } catch (err) { showToast(`Cancel failed: ${err.message}`, true); }
}

// Delete confirmation — title, `.target` naming the card, cancel + danger
// confirm that disables and relabels itself while the request is in flight
// and re-enables on failure. A task card with a live, killable session (not
// a CLI-backed one, which this endpoint can't tear down) kills that session
// and its subagents first and only deletes once the kill succeeds; a
// CLI-backed live session is deleted without a kill attempt, since the
// operator has to close that pane by hand. A scheduled card never carries a
// session, so it always deletes straight through.
export function openDeleteCardModal(card, { findCard, onDeleted } = {}) {
  const resolveCard_ = (id) => (findCard ? findCard(id) : card);
  const isTask = card.kind === 'task';
  const label = isTask ? (card.title || card.id) : (card.name || card.id);
  // Maps a card to its kill decision and note text — called both here
  // (against the live card, not the possibly-stale card captured at click
  // time) and again at confirm time.
  function killDecision(c) {
    const hasLiveSession = !!(c.session && !TERMINAL.has(c.session.status));
    const isCliSession = !!(c.session && (c.session.source === 'claude_code' || c.session.source === 'codex'));
    const needsKill = isTask && hasLiveSession && !isCliSession;
    let noteHtml;
    if (needsKill) {
      noteHtml = `<div class="descendants">Deleting this card will kill the running session and its subagents first, then remove the card. This can't be undone.</div>`;
    } else if (isTask && hasLiveSession && isCliSession) {
      noteHtml = `<div class="descendants">This card has a live ${escapeHtml(sourceLabelFor(c.session))} session that can't be killed from here — close its pane manually. Deleting removes the card. This can't be undone.</div>`;
    } else {
      noteHtml = `<div class="descendants">This can't be undone.</div>`;
    }
    return { needsKill, noteHtml };
  }
  let { needsKill, noteHtml } = killDecision(resolveCard_(card.id) || card);
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal" role="dialog" aria-labelledby="delete-title">
      <h2 id="delete-title">Delete card?</h2>
      <div class="target">${escapeHtml(label)}</div>
      ${noteHtml}
      <div class="actions">
        <button id="delete-cancel">Cancel</button>
        <button class="danger" id="delete-confirm">Delete</button>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);
  // Guards dismissal (backdrop click / Cancel) while a confirm is in
  // flight — without it, clicking the backdrop mid-request removes the
  // modal out from under the confirm handler, which then re-enables a
  // detached button on failure instead of the modal staying open.
  let pending = false;
  const cleanup = () => { if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop); };
  backdrop.addEventListener('click', e => { if (!pending && e.target === backdrop) cleanup(); });
  backdrop.querySelector('#delete-cancel').onclick = () => { if (!pending) cleanup(); };
  backdrop.querySelector('#delete-confirm').onclick = async () => {
    const confirmBtn = backdrop.querySelector('#delete-confirm');
    // Re-resolve the card from the live source rather than trusting the
    // one captured when the modal opened — a card another agent claims
    // while this modal is open can still show a session-less snapshot
    // here. Falls back to the captured `card` if it's vanished entirely.
    const fresh = resolveCard_(card.id) || card;
    const { needsKill: freshNeedsKill, noteHtml: freshNoteHtml } = killDecision(fresh);
    if (freshNeedsKill && !needsKill) {
      // The disclosed note didn't promise a kill but one is now required —
      // update the note in place and make the operator confirm again
      // against accurate text rather than killing a session they were
      // never told about.
      needsKill = freshNeedsKill;
      backdrop.querySelector('.descendants').outerHTML = freshNoteHtml;
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Delete';
      return;
    }
    pending = true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Deleting…';
    try {
      if (freshNeedsKill) {
        const kr = await fetch(`/api/agents/sessions/${encodeURIComponent(fresh.session.session_id)}/kill`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: '' }),
        });
        if (!kr.ok) {
          const text = await kr.text();
          let msg = text;
          try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
          throw new Error(`Kill failed: HTTP ${kr.status}: ${msg}`);
        }
        const killResult = await kr.json();
        const failures = killResult.failures || [];
        if (failures.length > 0) {
          throw new Error(`Kill failed: ${failures.map(f => f.reason || f.session_id).join('; ')}`);
        }
      }
      const deleteUrl = isTask
        ? `/api/tasks/${encodeURIComponent(card.id)}`
        : `/api/scheduler/${encodeURIComponent(card.id)}`;
      const dr = await fetch(deleteUrl, { method: 'DELETE' });
      if (!dr.ok) {
        const text = await dr.text();
        let msg = text;
        try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) {}
        throw new Error(msg || `HTTP ${dr.status}`);
      }
      pending = false;
      cleanup();
      showToast('Deleted.', false);
      if (onDeleted) await onDeleted();
    } catch (err) {
      showToast(`Delete failed: ${err.message}`, true);
      pending = false;
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Delete';
    }
  };
}

// Convenience bundle for `renderActionRow`'s `handlers` option — a caller
// that needs to override one action's behaviour (the Board drawer's own
// Cancel and Delete, which do extra drawer-specific bookkeeping around the
// generic network call) spreads this and replaces just that key.
export function cardActionHandlers(card, { findCard, onChanged, onAccepted } = {}) {
  const changed = onChanged || (() => {});
  return {
    open: () => openCard(card, changed),
    accept: () => acceptCard(card, changed, onAccepted),
    reject: () => openReviewActionModal(card, 'reject', changed),
    reassign: () => openReviewActionModal(card, 'reassign', changed),
    resolve: () => resolveCard(card, changed),
    cancel: () => cancelCard(card, changed),
    delete: () => openDeleteCardModal(card, { findCard, onDeleted: changed }),
  };
}
