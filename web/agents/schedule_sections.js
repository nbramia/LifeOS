// web/agents/schedule_sections.js
//
// Per-action input sections for a schedule — a schedule's fire
// behavior depends entirely on its `action` (notify / prompt / endpoint /
// agent), so the inputs worth showing do too. This module renders exactly
// the inputs a given action uses into a container, and returns a handle
// that reads the live values back out, switches which section is shown,
// and locally validates the endpoint action's JSON params. It never saves
// anything itself — the caller (the scheduled-card drawer today, a
// create-schedule composer later) decides when and how a value reaches
// the server.
//
// Reused by:
//   - web/agents/board.js's scheduled-card drawer — one field saves
//     through `PUT /api/scheduler/{id}` per blur/change.
//   - a future create-schedule composer — collects every section's values
//     once, for a single `POST /api/scheduler`.
//
// No build step, no framework — plain DOM, matching every other file in
// this directory.

import { escapeHtml } from './panel.js';
import { loadModelCatalog, loadHostCatalog, EFFORTS } from './assignment.js';

// Action a schedule fires when it's due. Mirrors VALID_ACTIONS in
// api/services/scheduler_store.py.
export const SCHEDULE_ACTIONS = ['notify', 'prompt', 'endpoint', 'agent'];
// Executor tag a schedule's `agent` action hands off to the agent worker
// with. Mirrors the executor values accepted by api/routes/scheduler.py.
export const SCHEDULE_EXECUTORS = ['local', 'cloud', 'cloud-haiku', 'cloud-sonnet'];
// HTTP methods a schedule's `endpoint` action may call with. Mirrors the
// methods api/routes/scheduler.py's server-side validation accepts.
export const SCHEDULE_ENDPOINT_METHODS = ['GET', 'POST'];

// Bot-registry cache backing every action's Bot select — a plain
// once-per-page-load cache on success, not re-cached on failure (so the
// next drawer/composer open retries), mirroring
// web/agents/assignment.js's loadHostCatalog but without its reachability
// TTL/cooldown: the bot registry doesn't drift minute to minute the way
// host online/offline status does.
let _botsCatalogPromise = null;
export function loadBotCatalog(fetchImpl = fetch) {
  if (_botsCatalogPromise) return _botsCatalogPromise;
  const promise = fetchImpl('/api/scheduler/bots')
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .catch(() => { _botsCatalogPromise = null; return null; });
  _botsCatalogPromise = promise;
  return promise;
}

function paramsToText(params) {
  if (params === undefined || params === null) return '';
  try { return JSON.stringify(params, null, 2); }
  catch (_) { return ''; }
}

function sectionHtml(action, values) {
  const cfg = values.endpoint_config || {};
  const method = String(cfg.method || 'GET').toUpperCase();
  const path = cfg.endpoint || '';
  const paramsText = paramsToText(cfg.params);

  const showMessage = action !== 'endpoint';
  const showEndpoint = action === 'endpoint';
  const showExecutor = action === 'agent';
  const showBot = action !== 'agent';
  const showExecContext = action === 'agent';

  const messageLabel = action === 'prompt' ? 'Prompt (run through chat)'
    : action === 'agent' ? 'Task description'
    : 'Message (sent as-is)';
  const messagePlaceholder = action === 'prompt' ? 'Prompt…'
    : action === 'agent' ? 'Task description…'
    : 'Message…';

  return `
    ${showMessage ? `
      <label class="drawer-label">${messageLabel}</label>
      <textarea class="drawer-notes" data-field="message-content" placeholder="${messagePlaceholder}">${escapeHtml(values.message_content || '')}</textarea>
      ${action === 'prompt' ? '<div class="drawer-schedule-info">Reply with <code>NO_ACTION</code> to stay silent.</div>' : ''}
    ` : ''}
    ${showEndpoint ? `
      <div class="drawer-row">
        <div>
          <label class="drawer-label">Method</label>
          <select class="drawer-select" data-field="endpoint-method">
            ${SCHEDULE_ENDPOINT_METHODS.map(m => `<option value="${m}" ${method === m ? 'selected' : ''}>${m}</option>`).join('')}
          </select>
        </div>
        <div>
          <label class="drawer-label">Path</label>
          <input class="drawer-input" data-field="endpoint-path" value="${escapeHtml(path)}" placeholder="/api/..." />
        </div>
      </div>
      <label class="drawer-label">Params (JSON)</label>
      <textarea class="drawer-notes" data-field="endpoint-params" placeholder="{}">${escapeHtml(paramsText)}</textarea>
      <div class="drawer-field-error" data-field="endpoint-params-error" hidden></div>
      <div class="drawer-schedule-info">The route's own <code>scheduler_message</code> controls what is actually sent.</div>
    ` : ''}
    <div data-row="executor" ${showExecutor ? '' : 'hidden'}>
      <label class="drawer-label">Executor</label>
      <select class="drawer-select" data-field="executor">
        <option value="" ${!values.executor ? 'selected' : ''}>default route</option>
        ${SCHEDULE_EXECUTORS.map(e => `<option value="${e}" ${values.executor === e ? 'selected' : ''}>${e}</option>`).join('')}
      </select>
    </div>
    <div data-row="bot" ${showBot ? '' : 'hidden'}>
      <label class="drawer-label">Bot</label>
      <select class="drawer-select" data-field="bot" disabled></select>
      <div class="drawer-field-reason" data-field="bot-reason" hidden></div>
    </div>
    ${showExecContext ? `
      <details class="drawer-exec-context" data-field="exec-context">
        <summary class="drawer-label drawer-exec-context-summary">Execution context</summary>
        <div class="drawer-row">
          <div>
            <label class="drawer-label">Persona</label>
            <input class="drawer-input" data-field="persona-id" value="${escapeHtml(values.persona_id || '')}" placeholder="persona id" />
          </div>
          <div>
            <label class="drawer-label">Model</label>
            <select class="drawer-select" data-field="model-id"></select>
          </div>
        </div>
        <div class="drawer-row">
          <div>
            <label class="drawer-label">Effort</label>
            <select class="drawer-select" data-field="effort-id">
              <option value="">default</option>
              ${EFFORTS.map(e => `<option value="${e}" ${values.effort === e ? 'selected' : ''}>${e}</option>`).join('')}
            </select>
          </div>
          <div>
            <label class="drawer-label">Host</label>
            <select class="drawer-select" data-field="host-id"></select>
          </div>
        </div>
        <label class="drawer-label">Working directory</label>
        <input class="drawer-input" data-field="working-dir" value="${escapeHtml(values.working_dir || '')}" placeholder="/tmp/example-project" />
      </details>
    ` : ''}
  `;
}

/**
 * Render `action`'s input section into `container` for `values`, and
 * return a handle to read it back, switch actions, and validate the
 * endpoint action's JSON params. Renders fresh on every call and on every
 * `setAction()` — there is no incremental diffing, matching every other
 * drawer section in this directory.
 *
 * @param {HTMLElement} container - emptied and populated.
 * @param {string} action - one of SCHEDULE_ACTIONS.
 * @param {object} values - the schedule's current values: `message_content`
 *   (string), `endpoint_config` ({method, endpoint, params} or null/undefined),
 *   `executor`, `bot`, `persona_id`, `model_id`, `effort`, `host`,
 *   `working_dir` (all strings, default "").
 * @param {object} [opts]
 * @param {Function} [opts.fetchImpl] - fetch override for the default
 *   bot/model/host catalog loaders, when the loader options below aren't given.
 * @param {(fetchImpl?: Function) => Promise} [opts.loadBots] - override
 *   for tests; defaults to this module's cached `loadBotCatalog`.
 * @param {(fetchImpl?: Function) => Promise} [opts.loadModels] - override
 *   for tests; defaults to assignment.js's cached `loadModelCatalog`.
 * @param {(fetchImpl?: Function) => Promise} [opts.loadHosts] - override
 *   for tests; defaults to assignment.js's cached `loadHostCatalog`.
 */
export function renderScheduleActionSections(container, action, values, opts = {}) {
  const fetchImpl = opts.fetchImpl || fetch;
  const loadBots = opts.loadBots || (() => loadBotCatalog(fetchImpl));
  const loadModels = opts.loadModels || (() => loadModelCatalog(fetchImpl));
  const loadHosts = opts.loadHosts || (() => loadHostCatalog(fetchImpl));

  let currentAction = action;
  // Mutated in place (never reassigned) so the `elements` object handed
  // back to the caller below stays the same reference across a later
  // `setAction()` rebuild — the caller re-reads its properties (now
  // pointing at the freshly rendered elements) rather than holding a
  // stale object frozen at the first render.
  const els = {};

  function query() {
    els.message = container.querySelector('[data-field="message-content"]');
    els.method = container.querySelector('[data-field="endpoint-method"]');
    els.path = container.querySelector('[data-field="endpoint-path"]');
    els.params = container.querySelector('[data-field="endpoint-params"]');
    els.paramsError = container.querySelector('[data-field="endpoint-params-error"]');
    els.executorRow = container.querySelector('[data-row="executor"]');
    els.executor = container.querySelector('[data-field="executor"]');
    els.botRow = container.querySelector('[data-row="bot"]');
    els.bot = container.querySelector('[data-field="bot"]');
    els.botReason = container.querySelector('[data-field="bot-reason"]');
    els.personaId = container.querySelector('[data-field="persona-id"]');
    els.modelId = container.querySelector('[data-field="model-id"]');
    els.effort = container.querySelector('[data-field="effort-id"]');
    els.host = container.querySelector('[data-field="host-id"]');
    els.workingDir = container.querySelector('[data-field="working-dir"]');
  }

  // Bot select: offers only the names GET /api/scheduler/bots returns
  // (plus an empty "default (primary)" option), matching the scheduled
  // card drawer's original behavior verbatim.
  function populateBots() {
    if (!els.bot) return;
    const stored = values.bot || '';
    loadBots().then(catalog => {
      if (!els.bot || !els.bot.isConnected) return; // the container is showing a different action's section
      if (!catalog) {
        const optionsHtml = ['<option value="">default (primary)</option>'];
        if (stored) optionsHtml.push(`<option value="${escapeHtml(stored)}" selected>${escapeHtml(stored)}</option>`);
        els.bot.innerHTML = optionsHtml.join('');
        els.bot.value = stored;
        els.bot.disabled = true;
        if (els.botReason) {
          els.botReason.textContent = 'bot registry unavailable — reopen to retry';
          els.botReason.hidden = false;
        }
        return;
      }
      const names = catalog.bots || [];
      const known = names.includes(stored);
      const optionsHtml = ['<option value="">default (primary)</option>'];
      for (const name of names) {
        optionsHtml.push(`<option value="${escapeHtml(name)}" ${stored === name ? 'selected' : ''}>${escapeHtml(name)}</option>`);
      }
      if (stored && !known) {
        optionsHtml.push(`<option value="${escapeHtml(stored)}" selected data-unknown="true">${escapeHtml(stored)} (unknown)</option>`);
      }
      els.bot.innerHTML = optionsHtml.join('');
      els.bot.value = stored;
      els.bot.disabled = false;
      if (els.botReason) { els.botReason.hidden = true; els.botReason.textContent = ''; }
    });
  }

  // Model select: every model any engine's catalog lists, deduplicated by
  // id — a schedule's `agent` action doesn't commit to one engine the way
  // a task's Assignee does (its `executor` is local/cloud/cloud-haiku/
  // cloud-sonnet, a different vocabulary), so there is no single engine
  // catalog to filter against.
  function populateModels() {
    if (!els.modelId) return;
    const stored = values.model_id || '';
    const seedOptions = ['<option value="">engine default</option>'];
    if (stored) seedOptions.push(`<option value="${escapeHtml(stored)}" selected data-unknown="true">${escapeHtml(stored)} (unknown)</option>`);
    els.modelId.innerHTML = seedOptions.join('');
    loadModels().then(catalog => {
      if (!els.modelId || !els.modelId.isConnected) return;
      const engines = catalog.engines || {};
      const seen = new Set();
      const allModels = [];
      for (const list of Object.values(engines)) {
        for (const m of (list || [])) {
          if (!seen.has(m.id)) { seen.add(m.id); allModels.push(m); }
        }
      }
      const current = els.modelId.value;
      const known = allModels.some(m => m.id === current);
      const optionsHtml = ['<option value="">engine default</option>'];
      for (const m of allModels) {
        optionsHtml.push(`<option value="${escapeHtml(m.id)}" ${current === m.id ? 'selected' : ''}>${escapeHtml(m.label || m.id)}</option>`);
      }
      if (current && !known) {
        optionsHtml.push(`<option value="${escapeHtml(current)}" selected data-unknown="true">${escapeHtml(current)} (unknown)</option>`);
      }
      els.modelId.innerHTML = optionsHtml.join('');
    });
  }

  function populateHosts() {
    if (!els.host) return;
    const stored = values.host || '';
    const seedOptions = ['<option value="">this machine</option>'];
    if (stored) seedOptions.push(`<option value="${escapeHtml(stored)}" selected data-unknown="true">${escapeHtml(stored)} (unknown)</option>`);
    els.host.innerHTML = seedOptions.join('');
    loadHosts().then(catalog => {
      if (!els.host || !els.host.isConnected) return;
      if (!catalog) return; // fetch failed or cooling down — leave the synchronous seed alone
      const current = els.host.value;
      const hosts = catalog.hosts || [];
      const known = hosts.some(h => h.name === current);
      const optionsHtml = ['<option value="">this machine</option>'];
      for (const h of hosts) {
        optionsHtml.push(`<option value="${escapeHtml(h.name)}" ${current === h.name ? 'selected' : ''}>${escapeHtml(h.name)}</option>`);
      }
      if (current && !known) {
        optionsHtml.push(`<option value="${escapeHtml(current)}" selected data-unknown="true">${escapeHtml(current)} (unknown)</option>`);
      }
      els.host.innerHTML = optionsHtml.join('');
    });
  }

  function render() {
    container.innerHTML = sectionHtml(currentAction, values);
    query();
    populateBots();
    populateModels();
    populateHosts();
  }
  render();

  function showParamsError(message) {
    if (!els.paramsError) return;
    els.paramsError.textContent = message;
    els.paramsError.hidden = false;
  }
  function clearParamsError() {
    if (!els.paramsError) return;
    els.paramsError.hidden = true;
    els.paramsError.textContent = '';
  }

  // Parses the endpoint action's Method/Path/Params fields into the
  // `endpoint_config` shape the server expects, or shows a field-level
  // error and returns `null` on invalid JSON or a non-object value —
  // never sends anything itself. Returns `null` immediately (no error
  // shown) when the endpoint section isn't currently rendered.
  function readEndpointConfig() {
    if (!els.method || !els.path || !els.params) return null;
    clearParamsError();
    const method = (els.method.value || 'GET').toUpperCase();
    const endpoint = els.path.value.trim();
    const raw = els.params.value.trim();
    let params;
    if (raw) {
      try {
        params = JSON.parse(raw);
      } catch (e) {
        showParamsError(`Invalid JSON: ${e.message}`);
        return null;
      }
      if (typeof params !== 'object' || params === null || Array.isArray(params)) {
        showParamsError('Params must be a JSON object');
        return null;
      }
    }
    const cfg = { method, endpoint };
    if (params !== undefined) cfg.params = params;
    return cfg;
  }

  return {
    get action() { return currentAction; },
    // Rebuilds the container's contents for a new action — synchronously,
    // so the visible section is already switched by the time any save
    // fires. Any field value typed into the outgoing section but not
    // carried by `values` is lost on the rebuild, matching how the
    // drawer's own Action select swap always works.
    setAction(newAction, newValues) {
      currentAction = newAction;
      if (newValues) Object.assign(values, newValues);
      render();
    },
    elements: els,
    readEndpointConfig,
    showParamsError,
    clearParamsError,
    // Reads every field the CURRENT action's section renders into one
    // plain object — the shape a create-schedule composer collects at
    // submit time. Returns `null` (with the params field error already
    // shown) when the endpoint action's params don't parse.
    getValues() {
      const out = {};
      if (els.message) out.message_content = els.message.value;
      if (els.bot) out.bot = els.bot.value;
      if (els.executor) out.executor = els.executor.value;
      if (els.method || els.path || els.params) {
        const cfg = readEndpointConfig();
        if (cfg === null) return null;
        out.endpoint_config = cfg;
      }
      if (els.personaId) out.persona_id = els.personaId.value.trim();
      if (els.modelId) out.model_id = els.modelId.value;
      if (els.effort) out.effort = els.effort.value;
      if (els.host) out.host = els.host.value;
      if (els.workingDir) out.working_dir = els.workingDir.value.trim();
      return out;
    },
  };
}
