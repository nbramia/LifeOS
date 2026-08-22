// Persona picker for /chat (#359).
//
// The user chooses which LifeOS persona to talk to — `primary` or any
// configured specialized bot. The selection scopes the conversation sidebar
// (`?persona_id=`), rides along on /api/ask/stream as `persona_id`, and gates
// the claude_intent handoff on the persona's advertised `capabilities`. The
// choice persists across refresh in sessionStorage. Implements the persona
// contract in docs/specs/technical/client-surfaces.md.

import { config, elements, endpoints } from './session.js';
import { newChat } from './conversations.js';

const PERSONA_STORAGE_KEY = 'lifeos:chat:persona_id';
const DEFAULT_PERSONA_ID = 'primary';

function readStoredPersonaId() {
  try {
    return window.sessionStorage.getItem(PERSONA_STORAGE_KEY) || DEFAULT_PERSONA_ID;
  } catch (e) {
    // sessionStorage unavailable (private mode / disabled)
    return DEFAULT_PERSONA_ID;
  }
}

function storePersonaId(id) {
  try {
    window.sessionStorage.setItem(PERSONA_STORAGE_KEY, id);
  } catch (e) {
    // sessionStorage unavailable — the selection just won't survive a refresh
  }
}

export async function loadPersonas() {
  // Restore the persisted selection synchronously (before the await) so the
  // first turn and the first conversation fetch already carry the right id.
  config.personaId = readStoredPersonaId();

  // Testability hook only (same pattern as backend.js's STATUS_TIMEOUT_MS):
  // lets a browser test force this function's promise to reject, so it can
  // assert that initBackend()'s single listing still fires even when persona
  // resolution fails outright (#607) — rather than trusting that every path
  // in this function stays guarded forever.
  if (typeof window !== 'undefined' && window.__LIFEOS_TEST_FORCE_PERSONAS_REJECT__) {
    throw new Error('forced persona-resolution failure (test only)');
  }

  let personas = [];
  let discovered = false;  // did /api/personas actually answer, successfully?
  try {
    const response = await fetch(endpoints.personas);
    if (response.ok) {
      const data = await response.json();
      personas = data.personas || [];
      discovered = true;
    }
  } catch (e) {
    console.log('Could not load personas');
  }
  config.personas = personas;

  // Every caller needs *some* in-memory answer right now (the sidebar listing
  // below, `personaId` on a turn, the persona-scoped conversation key), so
  // fall back to primary whenever we can't confirm the stored id is still
  // offered — both when discovery genuinely returned a list without it, and
  // when discovery failed outright (a failure to confirm gets treated the
  // same as confirmation that it's gone, in memory, for this boot only).
  const confirmed = discovered && personas.some(p => p.id === config.personaId);
  if (!confirmed) {
    config.personaId = DEFAULT_PERSONA_ID;
  }
  // Persisting is a separate decision from the in-memory fallback above: a
  // transient /api/personas failure must not permanently overwrite the
  // user's stored preference — they may be back on a working network next
  // load, and the id itself was never actually shown to be gone. Only write
  // through when discovery succeeded, so `sessionStorage` only ever records
  // an id we've actually validated (or actually confirmed invalid).
  if (discovered) {
    storePersonaId(config.personaId);
  }

  renderPersonaOptions();
  // Deliberately does NOT call loadConversations() here (#607): config.backend
  // hasn't been resolved yet at this point (initBackend() runs after this),
  // so a fetch fired now would always ask for the `lifeos` fallback and could
  // still be in flight — and resolve arbitrarily late — when initBackend()'s
  // own listing lands, racing it for the last write to state.allConversations.
  // config.personaId is only FULLY resolved (validated against the fetched
  // list, above) once this whole async function's promise settles — a caller
  // that only waits for the synchronous assignment at the top of this
  // function (before this function's first await) would still race the
  // validation above. initBackend() awaits this function's promise (passed in
  // as personasReady) before its single listing, precisely to avoid that.
}

function renderPersonaOptions() {
  const picker = elements.personaPicker;
  if (!picker) return;
  // Keep the control usable even if discovery failed.
  const personas = (config.personas && config.personas.length)
    ? config.personas
    : [{ id: DEFAULT_PERSONA_ID, label: 'Primary' }];

  picker.innerHTML = '';
  for (const p of personas) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    picker.appendChild(opt);
  }
  picker.value = config.personaId;
  // If the stored id isn't among the offered options (e.g. discovery failed and
  // a non-primary persona was previously chosen), fall back to the first option
  // and keep config in sync so the picker is never blank.
  if (picker.selectedIndex < 0 && picker.options.length > 0) {
    picker.selectedIndex = 0;
    config.personaId = picker.value;
  }
  updateOrchestratesBadge();
}

export function onPersonaChange() {
  const picker = elements.personaPicker;
  if (!picker) return;
  config.personaId = picker.value;
  storePersonaId(config.personaId);
  updateOrchestratesBadge();
  // Switching persona starts a fresh, persona-scoped conversation — the
  // previous conversation belongs to the previous persona. newChat() re-fetches
  // the sidebar with the new persona filter.
  newChat();
}

// Shows/hides the toolbar badge telling the user an orchestrating persona
// (e.g. doctor) always runs on LifeOS (#596) — visible whenever
// personaOrchestrates() is true, which by construction already excludes the
// agent backend (no persona pass-through there) and includes both lifeos and
// hermes. Called on persona change and whenever the backend selector changes
// (backend.js), since personaOrchestrates() depends on both.
export function updateOrchestratesBadge() {
  const badge = elements.orchestratesBadge;
  if (!badge) return;
  badge.hidden = !personaOrchestrates();
}

// True iff the selected persona advertises the `handoff` capability. Gates the
// claude_intent handoff on capabilities rather than a hardcoded `primary`
// check. Until the list loads (or if discovery failed) we fail open ONLY for
// the default persona — preserving its pre-#359 handoff behavior — and fail
// closed for any explicitly-selected persona whose capabilities we can't yet
// confirm (a returning non-handoff persona restored from sessionStorage must
// not trigger a handoff during the /api/personas load window).
export function personaSupportsHandoff() {
  const { personas, personaId } = config;
  // Neither the agent nor the hermes backend has handoff. Hermes now carries
  // the full persona (preamble, voice rules) to its backend server-side via
  // the `lifeos_context` envelope (#590), but that backend has no claude_intent
  // classifier to hand off to — so this stays false regardless of persona.
  // Restoring orchestration on Hermes (#596, personaOrchestrates() below) does
  // NOT restore this: handoff and orchestration are different mechanisms, and
  // an orchestrating persona's Hermes turn is diverted to LifeOS wholesale
  // rather than handed off mid-stream.
  if (config.backend === 'agent' || config.backend === 'hermes') return false;
  if (!personas || personas.length === 0) return personaId === DEFAULT_PERSONA_ID;
  const p = personas.find(x => x.id === personaId);
  return !!(p && p.capabilities && p.capabilities.includes('handoff'));
}

// True iff the selected persona is an *orchestrating* bot — one that spawns a
// background Claude Code session on send (e.g. doctor) rather than answering
// inline. Reads the server's own `orchestrates` flag (#643 — sourced from
// `settings.persona_orchestrates()`) rather than inferring it from
// `capabilities`, which look identical for `primary` and an orchestrating bot
// like `doctor`. Used to decide whether a turn should poll for a
// `[CLARIFY]`/`[GOAL]` on surfaces (voice) whose stream doesn't expose the
// `claude_code` routing the text path keys off (#412), and (askStream, #596)
// whether a Hermes-selected turn must be diverted to the LifeOS endpoint
// instead of the Hermes proxy — the spawn path this gates is LifeOS-only, so
// Hermes no longer excludes it.
//
// Before `/api/personas` resolves (or if discovery failed), `personas` is
// empty and the lookup below finds nothing — this fails closed for every
// persona, including a restored non-primary selection, which is the intended
// load-window behavior. `primary` never orchestrates regardless (it answers
// inline), so there's no separate "fail open" case needed here the way
// `personaSupportsHandoff()` has one.
export function personaOrchestrates() {
  const { personas, personaId } = config;
  // The agent backend has no persona pass-through at all (no persona_id is
  // ever sent, see askStream), so it never orchestrates — unlike hermes,
  // which is deliberately NOT excluded here (#596): an orchestrating
  // persona's Hermes turn still orchestrates, just via a diverted LifeOS
  // turn rather than the Hermes proxy.
  if (config.backend === 'agent') return false;
  if (!personaId || personaId === DEFAULT_PERSONA_ID) return false;  // primary answers inline
  const p = personas && personas.find(x => x.id === personaId);
  return !!(p && p.orchestrates);
}
