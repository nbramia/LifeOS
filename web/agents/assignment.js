// web/agents/assignment.js
//
// Card-assignment pickers (#851): engine, model, effort, and host — for a
// board card's drawer. Isolated module rather than a patch to
// web/agents/board.js because the Kanban board UI (#850) hadn't merged
// when this landed (see this PR's description) — hooking it in is a
// one-line `import { renderAssignmentPickers } from './assignment.js'`
// plus one call inside board.js's `renderDrawer()` once it does. Matches
// that branch's card/drawer shapes exactly (verified against its
// `git show feat/kanban-board:web/agents/board.js`):
//   - card: {id, title, notes, tags, assignee, fields, session, ...}
//     `fields` is the task's `[key:: value]` inline-field map (#853);
//     `session` (when a session exists) carries `host`/`model`/`effort`
//     — "what actually ran", read-only, distinct from the assignment.
//   - `PUT /api/tasks/{id}` with `{fields: {...}}` patches inline fields;
//     a field value of `null` clears it (#853's fields API contract).
//   - `GET /api/agents/models` returns
//     `{engines: {claude: [...], codex: [...], local: [...], hermes: [...]},
//     refreshed_at, stale}`, each entry `{id, label, pricing}` — the
//     source for the model picker's options.
//
// No build step, no framework — plain DOM, matching every other file in
// this directory.

const ENGINES = ['claude', 'codex', 'local', 'hermes'];
const EFFORTS = ['low', 'medium', 'high', 'max'];

// Engines whose executor actually reads a `model` field (claude_code_executor
// / codex_executor's `--model` flag — see api/services/agent_worker/
// assignment.py). Local and Hermes never take a model override: local
// always runs the on-box model, Hermes reports whatever model it served a
// turn with (model_readout.py) rather than accepting a request for one.
const ENGINES_WITH_MODEL_PICKER = new Set(['claude', 'codex']);
// Engines whose executor reads an `effort` field. Hermes has no effort
// override (see assignment.py's module docstring).
const ENGINES_WITH_EFFORT_PICKER = new Set(['claude', 'codex', 'local']);
// Engines a `host` field can steer over ssh (api/services/agent_worker/
// remote_spawn.py) — local/Hermes always run wherever the API process does.
const ENGINES_WITH_HOST_PICKER = new Set(['claude', 'codex']);

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fieldValue(card, key) {
  return (card && card.fields && card.fields[key]) || '';
}

// Fetches the model catalog once per page load and caches it — every card
// drawer opened afterward reuses the same list rather than re-fetching.
// A fetch failure degrades to an empty catalog (the model picker still
// renders, just with no options besides "let the engine choose").
let _catalogPromise = null;
function loadModelCatalog(fetchImpl = fetch) {
  if (!_catalogPromise) {
    _catalogPromise = fetchImpl('/api/agents/models')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .catch(() => ({ engines: { claude: [], codex: [], local: [], hermes: [] } }));
  }
  return _catalogPromise;
}

/**
 * Render the four assignment pickers into `container` for `card`.
 *
 * @param {HTMLElement} container - emptied and populated with the pickers.
 * @param {object} card - the board card (see this file's header comment).
 * @param {object} [opts]
 * @param {(id: string, patch: object) => Promise} [opts.putTask] - PUT
 *   /api/tasks/{id} caller; defaults to a real fetch. Tests inject a fake.
 * @param {(fetchImpl?: Function) => Promise} [opts.loadCatalog] - override
 *   for tests; defaults to the module's cached `loadModelCatalog`.
 * @param {Function} [opts.fetchImpl] - fetch override for the default
 *   catalog loader, when `opts.loadCatalog` isn't given.
 * @param {() => void} [opts.onSaved] - called after a successful PUT.
 * @param {(message: string) => void} [opts.onError] - called with an error
 *   message on a failed PUT; defaults to a no-op (the caller — board.js's
 *   showToast in the eventual hookup — decides how to surface it).
 */
export function renderAssignmentPickers(container, card, opts = {}) {
  const putTask = opts.putTask || defaultPutTask;
  const loadCatalog = opts.loadCatalog || (() => loadModelCatalog(opts.fetchImpl));
  const onSaved = opts.onSaved || (() => {});
  const onError = opts.onError || (() => {});

  const currentEngine = (card.assignee || '').toLowerCase();
  const currentModel = fieldValue(card, 'model');
  const currentEffort = fieldValue(card, 'effort');
  const currentHost = fieldValue(card, 'host');
  const ran = card.session || null;

  container.innerHTML = `
    <div class="assignment-row" data-row="engine">
      <label class="assignment-label">Engine</label>
      <select class="assignment-engine" data-field="assignee">
        <option value="">unassigned</option>
        ${ENGINES.map(e => `<option value="${e}" ${currentEngine === e ? 'selected' : ''}>${e}</option>`).join('')}
      </select>
    </div>
    <div class="assignment-row" data-row="model" hidden>
      <label class="assignment-label">Model</label>
      <select class="assignment-model" data-field="model">
        <option value="">engine default</option>
      </select>
    </div>
    <div class="assignment-row" data-row="effort" hidden>
      <label class="assignment-label">Effort</label>
      <select class="assignment-effort" data-field="effort">
        <option value="">default</option>
        ${EFFORTS.map(e => `<option value="${e}" ${currentEffort === e ? 'selected' : ''}>${e}</option>`).join('')}
      </select>
    </div>
    <div class="assignment-row" data-row="host" hidden>
      <label class="assignment-label">Host</label>
      <input class="assignment-host" data-field="host" type="text"
             value="${escapeHtml(currentHost)}" placeholder="this machine" />
    </div>
    <div class="assignment-ran" data-field="ran"></div>
    <div class="assignment-error" data-field="error" hidden></div>
  `;

  const engineEl = container.querySelector('[data-field="assignee"]');
  const modelRow = container.querySelector('[data-row="model"]');
  const modelEl = container.querySelector('[data-field="model"]');
  const effortRow = container.querySelector('[data-row="effort"]');
  const effortEl = container.querySelector('[data-field="effort"]');
  const hostRow = container.querySelector('[data-row="host"]');
  const hostEl = container.querySelector('[data-field="host"]');
  const ranEl = container.querySelector('[data-field="ran"]');
  const errorEl = container.querySelector('[data-field="error"]');

  function renderRan() {
    if (!ran || (!ran.model && !ran.effort && !ran.host)) {
      ranEl.textContent = '';
      return;
    }
    const parts = [];
    if (ran.model) parts.push(`model ${ran.model}`);
    if (ran.effort) parts.push(`effort ${ran.effort}`);
    if (ran.host) parts.push(`host ${ran.host}`);
    ranEl.textContent = `Actually ran: ${parts.join(', ')}`;
  }
  renderRan();

  function updateVisibility() {
    const engine = engineEl.value;
    modelRow.hidden = !ENGINES_WITH_MODEL_PICKER.has(engine);
    effortRow.hidden = !ENGINES_WITH_EFFORT_PICKER.has(engine);
    hostRow.hidden = !ENGINES_WITH_HOST_PICKER.has(engine);
  }
  updateVisibility();

  function populateModelOptions(catalog) {
    const engine = engineEl.value;
    const models = (catalog.engines && catalog.engines[engine]) || [];
    modelEl.innerHTML = '<option value="">engine default</option>'
      + models.map(m => `<option value="${escapeHtml(m.id)}" ${currentModel === m.id ? 'selected' : ''}>${escapeHtml(m.label || m.id)}</option>`).join('');
  }
  loadCatalog().then(populateModelOptions);

  function showError(message) {
    if (message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    } else {
      errorEl.hidden = true;
      errorEl.textContent = '';
    }
    onError(message);
  }

  // Every picker writes the SAME shape: the assignee tag (if it's the
  // engine picker changing) plus the three inline fields, always stamping
  // `assigned_by: "board"` — that's what keeps preflight's routing
  // corroboration from ever second-guessing a board assignment (see
  // api/services/agent_worker/preflight.py's `_apply_route_corroboration`
  // and assignment.py's module docstring).
  async function save(extra = {}) {
    const { tags, ...extraFields } = extra;  // `tags` is a top-level PUT key, not a field
    const fields = {
      model: modelEl.value || null,
      effort: effortEl.value || null,
      host: hostEl.value.trim() || null,
      assigned_by: 'board',
      ...extraFields,
    };
    const patch = { fields };
    if (tags) patch.tags = tags;
    try {
      await putTask(card.id, patch);
      showError('');
      onSaved();
    } catch (err) {
      showError(err && err.message ? err.message : String(err));
    }
  }

  engineEl.addEventListener('change', () => {
    updateVisibility();
    loadCatalog().then(populateModelOptions);
    const engine = engineEl.value;
    const nonAssigneeTags = (card.tags || []).filter(t => !ENGINES.includes((t || '').toLowerCase()));
    const tags = engine ? [engine, ...nonAssigneeTags] : nonAssigneeTags;
    save({ tags });
  });
  modelEl.addEventListener('change', () => save());
  effortEl.addEventListener('change', () => save());
  hostEl.addEventListener('change', () => save());

  return { engineEl, modelEl, effortEl, hostEl };
}

async function defaultPutTask(taskId, patch) {
  const r = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text;
    try { const j = JSON.parse(text); msg = j.detail || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json();
}
