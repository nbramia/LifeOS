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
    storePersonaId(config.personaId);
  }

  renderPersonaOptions();
}

function renderPersonaOptions() {
  const picker = elements.personaPicker;
  if (!picker) return;
  // Keep the control usable even if discovery failed.
  const personas = (config.personas && config.personas.length)
    ? config.personas
    : [{ id: DEFAULT_PERSONA_ID, label: 'LifeOS' }];

  picker.innerHTML = '';
  for (const p of personas) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    picker.appendChild(opt);
  }
  picker.value = config.personaId;
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
// check. When the list hasn't loaded yet (or discovery failed) we preserve the
// default handoff behavior so a transient /api/personas outage doesn't silently
// disable handoff for the primary persona.
export function personaSupportsHandoff() {
  const { personas, personaId } = config;
  if (!personas || personas.length === 0) return true;
  const p = personas.find(x => x.id === personaId);
  return !!(p && p.capabilities && p.capabilities.includes('handoff'));
}
