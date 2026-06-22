// Per-turn chat model picker (toolbar dropdown).
//
// Lets a chat turn run on a chosen model: 'auto' (the default Haiku orchestrator
// with escalation), 'sonnet' / 'opus' (pin this turn to that cloud model), or
// 'gemma' (run this turn on the local llama-server). The choice persists in
// sessionStorage and rides along on /api/ask/stream as `model_override`; the
// server honors it on the Anthropic backend and falls back to auto otherwise.
// See docs/specs/technical/client-surfaces.md.

import { config, elements } from './session.js';

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

export function initModel() {
  config.model = readStoredModel();
  if (elements.modelPicker) elements.modelPicker.value = config.model;
}
