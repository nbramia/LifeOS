// Persona picker for /chat (#359).
//
// The user chooses which LifeOS persona to talk to — `primary` or any
// configured specialized bot. The selection scopes the conversation sidebar
// (`?persona_id=`), rides along on /api/ask/stream as `persona_id`, and gates
// the claude_intent handoff on the persona's advertised `capabilities`. The
// choice persists across refresh in sessionStorage. Implements the persona
// contract in docs/specs/technical/client-surfaces.md.

import { config, elements, endpoints } from './session.js';
import { newChat, loadConversations } from './conversations.js';

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

  let personas = [];
  try {
    const response = await fetch(endpoints.personas);
    if (response.ok) {
      const data = await response.json();
      personas = data.personas || [];
    }
  } catch (e) {
    console.log('Could not load personas');
  }
  config.personas = personas;

  // If the stored persona is no longer offered, fall back to primary.
  if (personas.length > 0 && !personas.some(p => p.id === config.personaId)) {
    config.personaId = DEFAULT_PERSONA_ID;
  }
  storePersonaId(config.personaId);

  renderPersonaOptions();
  // Load the sidebar once the persona is resolved so it is correctly scoped.
  // initChat no longer calls loadConversations itself — this is the single
  // initial load, which keeps the picker and the sidebar consistent even when
  // the stored persona had to be reset.
  loadConversations();
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
}

export function onPersonaChange() {
  const picker = elements.personaPicker;
  if (!picker) return;
  config.personaId = picker.value;
  storePersonaId(config.personaId);
  // Switching persona starts a fresh, persona-scoped conversation — the
  // previous conversation belongs to the previous persona. newChat() re-fetches
  // the sidebar with the new persona filter.
  newChat();
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
  // Neither the agent nor the hermes backend has handoff; hermes keeps the
  // persona picker visible (#587) but persona pass-through isn't wired yet.
  if (config.backend === 'agent' || config.backend === 'hermes') return false;
  if (!personas || personas.length === 0) return personaId === DEFAULT_PERSONA_ID;
  const p = personas.find(x => x.id === personaId);
  return !!(p && p.capabilities && p.capabilities.includes('handoff'));
}

// True iff the selected persona is an *orchestrating* bot — one that spawns a
// background Claude Code session on send (e.g. doctor) rather than answering
// inline. Mirrors the server's `persona_orchestrates`: the `primary` persona is
// the inline orchestrator (it carries handoff capability but never spawns), so
// it is excluded; orchestrating bots are the non-primary personas that advertise
// handoff. Used to decide whether a turn should poll for a `[CLARIFY]`/`[GOAL]`
// on surfaces (voice) whose stream doesn't expose the `claude_code` routing the
// text path keys off (#412).
export function personaOrchestrates() {
  const { personas, personaId } = config;
  // Neither backend orchestrates via persona yet — see personaSupportsHandoff().
  if (config.backend === 'agent' || config.backend === 'hermes') return false;
  if (!personaId || personaId === DEFAULT_PERSONA_ID) return false;  // primary answers inline
  const p = personas && personas.find(x => x.id === personaId);
  return !!(p && p.capabilities && p.capabilities.includes('handoff'));
}
