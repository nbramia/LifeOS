// Per-turn chat model picker (toolbar dropdown).
//
// Lets a chat turn run on a chosen model: 'auto' (the default Haiku orchestrator
// with escalation), 'sonnet' / 'opus' (pin this turn to that cloud model),
// 'gemma' (run this turn on the local llama-server), or 'remote' (#654 — a
// configured paid OpenAI-compatible provider, e.g. Fireworks; an explicit pick
// only, never reachable via auto-escalation). 'claude_code' is special: it
// isn't an inline model but a handoff — the turn is routed to a background
// Claude Code worker session (the same handoff the orchestrator emits for an
// inferred "use claude code" directive). The choice persists in sessionStorage
// and rides along on /api/ask/stream as `model_override`; the server honors the
// model picks on the Anthropic backend (the 'claude_code' handoff works on any
// backend) and falls back to auto otherwise.
// See docs/specs/technical/client-surfaces.md.

import { config, elements, endpoints } from './session.js';

const MODEL_STORAGE_KEY = 'lifeos:chat:model';
const DEFAULT_MODEL = 'auto';

function readStoredModel() {
  try {
    return window.sessionStorage.getItem(MODEL_STORAGE_KEY) || DEFAULT_MODEL;
  } catch (e) {
    return DEFAULT_MODEL;
  }
}

export function onModelChange() {
  const picker = elements.modelPicker;
  if (!picker) return;
  config.model = picker.value || DEFAULT_MODEL;
  try {
    window.sessionStorage.setItem(MODEL_STORAGE_KEY, config.model);
  } catch (e) {
    // sessionStorage unavailable — the choice just won't survive a refresh
  }
}

// (#654) Show/hide the "Remote" option based on GET /api/chat/config, which
// reports whether the server has a remote provider configured at all (base
// URL + model + key). Unconfigured is the default for a fresh clone — the
// option stays hidden and the picker looks exactly like it did before this
// option existed. A network failure degrades the same way (hidden), never
// showing an option that can't actually run.
async function applyRemoteAvailability() {
  const picker = elements.modelPicker;
  const option = picker && picker.querySelector('option[value="remote"]');
  if (!option) return;
  let data = {};
  try {
    const resp = await fetch(endpoints.chatConfig);
    data = resp.ok ? await resp.json() : {};
  } catch (e) {
    data = {};
  }
  option.hidden = !data.remote_model_available;
  if (data.remote_model_available && data.remote_model_label) {
    option.textContent = data.remote_model_label;
  }
  if (option.hidden && picker.value === 'remote') {
    // A stale sessionStorage pick from before the server lost its config
    // (or a browser that never had it) — fall back rather than leaving a
    // hidden option selected.
    picker.value = DEFAULT_MODEL;
    onModelChange();
  }
}

export function initModel() {
  config.model = readStoredModel();
  if (elements.modelPicker) elements.modelPicker.value = config.model;
  applyRemoteAvailability();
}
