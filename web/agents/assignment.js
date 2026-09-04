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
//   - `GET /api/agents/hosts` (#883) returns
//     `{hosts: [{name, ssh_target, online, is_api_host}], refreshed_at}`
//     — the source for the host picker's options; `online` is
//     `true`/`false`/`null` (null = unknown, never guessed).
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

// Fetches the host registry — mostly mirrors loadModelCatalog above, but
// differs in two ways the model catalog doesn't need (#901 round 1,
// findings R1/R2):
//   - a short client-side TTL (`_HOSTS_CLIENT_TTL_MS`, matched to
//     host_catalog.py's own `_HOST_CATALOG_TTL_SECONDS`) instead of a
//     once-per-page-load cache — reachability drifts minute to minute,
//     unlike a 24h model list, so an operator who leaves /agents open
//     needs the markers to actually refresh on a later drawer open.
//   - a fetch failure is NOT cached: the caller for that one call gets
//     `null` back, but the cache itself is cleared immediately so the
//     NEXT drawer open retries instead of being stuck with an empty
//     catalog (and thus no way to pick a host at all) for the rest of
//     the page session. `populateHostOptions` below treats a `null`
//     result as "leave the synchronous seed alone" — distinct from a
//     catalog that resolved with a genuinely empty `hosts` list.
//   - (#901 round 2, finding R6) a permanently failing endpoint must not
//     be retried once per drawer open forever — after the SECOND
//     consecutive failure, a short cooldown (`_HOSTS_FAILURE_COOLDOWN_MS`)
//     suppresses further fetches so a dead endpoint costs one request per
//     cooldown window rather than one per drawer open. A single transient
//     blip (round 1's R1 territory) still retries on the very next drawer
//     open — the cooldown only arms after a SECOND failure in a row.
let _hostsCache = null; // { at: number (Date.now() ms), promise: Promise }
const _HOSTS_CLIENT_TTL_MS = 30_000;
let _hostsConsecutiveFailures = 0;
let _hostsCooldownUntil = 0;
const _HOSTS_FAILURE_COOLDOWN_MS = 10_000;
function loadHostCatalog(fetchImpl = fetch) {
  const now = Date.now();
  if (_hostsCache && (now - _hostsCache.at) < _HOSTS_CLIENT_TTL_MS) {
    return _hostsCache.promise;
  }
  if (_hostsConsecutiveFailures >= 2 && now < _hostsCooldownUntil) {
    return Promise.resolve(null); // still cooling down -- skip the fetch entirely
  }
  const promise = fetchImpl('/api/agents/hosts')
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(data => {
      _hostsConsecutiveFailures = 0;
      return data;
    })
    .catch(() => {
      // (#901 round 2, finding M6) Only clear the cache if we're still the
      // CURRENT entry — a slow fetch that fails after the TTL rolled and a
      // newer fetch already cached a success must not clobber that newer
      // entry.
      // (#901 round 3, finding M13) Guard the failure counter the same
      // way the cache clear above is guarded — a fetch that's no longer
      // the CURRENT entry (a newer one already succeeded and replaced it)
      // must not arm the cooldown against a healthy endpoint either.
      if (_hostsCache === mine) {
        _hostsCache = null;
        _hostsConsecutiveFailures += 1;
        if (_hostsConsecutiveFailures >= 2) {
          _hostsCooldownUntil = Date.now() + _HOSTS_FAILURE_COOLDOWN_MS;
        }
      }
      return null;
    });
  const mine = { at: now, promise };
  _hostsCache = mine;
  return promise;
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
 * @param {(fetchImpl?: Function) => Promise} [opts.loadHosts] - override
 *   for tests; defaults to the module's cached `loadHostCatalog`.
 * @param {Function} [opts.fetchImpl] - fetch override for the default
 *   catalog/host loaders, when `opts.loadCatalog`/`opts.loadHosts` isn't
 *   given.
 * @param {() => void} [opts.onSaved] - called after a successful PUT.
 * @param {(message: string) => void} [opts.onError] - called with an error
 *   message on a failed PUT; defaults to a no-op (the caller — board.js's
 *   showToast in the eventual hookup — decides how to surface it).
 */
export function renderAssignmentPickers(container, card, opts = {}) {
  const putTask = opts.putTask || defaultPutTask;
  const loadCatalog = opts.loadCatalog || (() => loadModelCatalog(opts.fetchImpl));
  const loadHosts = opts.loadHosts || (() => loadHostCatalog(opts.fetchImpl));
  const onSaved = opts.onSaved || (() => {});
  const onError = opts.onError || (() => {});

  const currentEngine = (card.assignee || '').toLowerCase();
  const currentModel = fieldValue(card, 'model');
  const currentEffort = fieldValue(card, 'effort');
  const currentHost = fieldValue(card, 'host');
  const ran = card.session || null;

  // (#901 round 2, finding A3) Last-known-good values for the three
  // fields every save() sends, seeded from the card and updated on each
  // SUCCESSFUL save. A rejected save reverts the controls to these —
  // mirroring how every other drawer control (title, notes, context,
  // tags, the Assignee select in board.js) already snaps back on failure
  // — so a host/effort/model change the server REJECTS can never ride
  // along, silently, on the next unrelated save.
  //
  // (#901 round 3, finding A5/M11) These are set from exactly what the
  // succeeding save SENT (captured before its `await`), never re-read
  // from the live controls at some other save's resolution time — with
  // two saves in flight, the live controls can already reflect a THIRD,
  // still-pending change, which would make an unrelated revert clobber a
  // committed value or leave a rejected one in place. See save()'s
  // serialization below for how "two saves in flight" is made impossible
  // in the first place.
  let lastSavedEffort = currentEffort;
  let lastSavedHost = currentHost;
  let lastSavedModel = currentModel;

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
      <select class="assignment-host" data-field="host"></select>
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

  let catalogReady = false;
  function populateModelOptions(catalog) {
    const engine = engineEl.value;
    const models = (catalog.engines && catalog.engines[engine]) || [];
    modelEl.innerHTML = '<option value="">engine default</option>'
      + models.map(m => `<option value="${escapeHtml(m.id)}" ${currentModel === m.id ? 'selected' : ''}>${escapeHtml(m.label || m.id)}</option>`).join('');
    catalogReady = true;
  }
  loadCatalog().then(populateModelOptions);

  // Host select: unlike the model select (which is safe to leave empty
  // until the catalog resolves — a `model` field is simply omitted from
  // the PUT until then, see save()'s comment below), a `host` field is
  // never omitted — an early effort/engine change must still carry the
  // card's saved host along. So the saved host is seeded as a selected
  // option SYNCHRONOUSLY, before `GET /api/agents/hosts` has even been
  // requested, rather than adding a second readiness flag: hostEl.value
  // is correct from the very first render. Once the real host list
  // resolves, populateHostOptions() rebuilds the options list against it
  // — a saved host that IS in the registry gets its real online marker; a
  // saved host that ISN'T stays as the same flagged-unknown option
  // (`data-unknown="true"`) rather than disappearing.
  function seedHostOptions() {
    const optionsHtml = ['<option value="">this machine</option>'];
    if (currentHost) {
      optionsHtml.push(`<option value="${escapeHtml(currentHost)}" selected data-unknown="true">${escapeHtml(currentHost)} (unknown)</option>`);
    }
    hostEl.innerHTML = optionsHtml.join('');
  }
  seedHostOptions();

  function hostLabel(host) {
    if (host.online === false) return `${host.name} (offline)`;
    if (host.online === null || host.online === undefined) return `${host.name} (unknown)`;
    return host.name;
  }
  function populateHostOptions(catalog) {
    // `null` means the fetch failed, or was skipped by the R6 cooldown
    // below (see loadHostCatalog) — leave the synchronous seed exactly as
    // seedHostOptions() rendered it rather than collapsing it to just
    // "this machine", but tell the operator the REGISTRY failed to load
    // (#901 round 2, finding R7) — otherwise a card with no saved host
    // shows exactly one option and reads as "no hosts are registered",
    // not "the registry couldn't be loaded". The marker option is
    // `disabled` so it can never become the selected value, and thus
    // never rides along as `fields.host` on a later save.
    if (!catalog) {
      const unavailable = document.createElement('option');
      unavailable.value = '__hosts_unavailable__';
      unavailable.disabled = true;
      unavailable.textContent = 'hosts unavailable — reopen to retry';
      hostEl.appendChild(unavailable);
      return;
    }
    // Read the LIVE selection, not `currentHost` (the render-time
    // snapshot) — the operator may have already changed the host while
    // this fetch was in flight, and that live choice must win over the
    // catalog landing (#901 round 1, finding A1). `hostEl.value` still
    // equals the seeded `currentHost` when nothing has changed, so the
    // synchronous-seed behavior above is unaffected.
    const current = hostEl.value;
    const hosts = (catalog.hosts) || [];
    const known = hosts.some(h => h.name === current);
    const optionsHtml = ['<option value="">this machine</option>'];
    for (const h of hosts) {
      const selected = current === h.name ? 'selected' : '';
      optionsHtml.push(`<option value="${escapeHtml(h.name)}" ${selected}>${escapeHtml(hostLabel(h))}</option>`);
    }
    if (current && !known) {
      optionsHtml.push(`<option value="${escapeHtml(current)}" selected data-unknown="true">${escapeHtml(current)} (unknown)</option>`);
    }
    hostEl.innerHTML = optionsHtml.join('');
  }
  loadHosts().then(populateHostOptions);

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
    onError(message);
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = '';
  }

  // (#901 round 3, finding R9) Assign `value` to `el`, and if the option
  // that held it has since been removed from the DOM — the DOM silently
  // sets `selectedIndex = -1` and `value = ""`, which for host/effort/
  // model is a REAL, different value ("this machine" / "default" /
  // "engine default"), not "unset" — re-append it as a flagged-unknown
  // option and assign again. Reachable for the host select in particular:
  // round 1's A1 live-value read rebuilds the option list around
  // whatever is currently selected, which can drop the very option a
  // later revert wants to restore.
  function restoreSelect(el, value) {
    el.value = value;
    if (value && el.value !== value) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.dataset.unknown = 'true';
      opt.textContent = `${value} (unknown)`;
      el.appendChild(opt);
      el.value = value;
    }
  }

  // Every picker writes the SAME shape: the assignee tag (if it's the
  // engine picker changing) plus the three inline fields, always stamping
  // `assigned_by: "board"` — that's what keeps preflight's routing
  // corroboration from ever second-guessing a board assignment (see
  // api/services/agent_worker/preflight.py's `_apply_route_corroboration`
  // and assignment.py's module docstring). The `model` field is the one
  // exception: it's omitted (not nulled) until the model catalog has
  // populated the select. Before that, `modelEl.value` is always '' —
  // regardless of what the card has saved — so sending `model: null` would
  // wipe a previously saved model on the very first effort/host change of
  // a page load, before `GET /api/agents/models` has resolved (#861).
  //
  // (#901 round 3, finding A5) Saves are serialized through a single
  // in-flight chain (`saveChain` below) rather than fired independently:
  // with two saves in flight, whichever finished LAST used to decide what
  // "last known good" meant for BOTH, so an accepted change could be
  // reverted by an unrelated later rejection, and a rejected change could
  // be "confirmed" by an unrelated earlier acceptance — both silent, no
  // toast. Chaining each save onto the previous one's settlement means at
  // most one PUT is ever outstanding, so there is no "other save's
  // resolution time" left to read from: `runSave` reads the controls
  // fresh, at the moment it actually starts, and every `lastSaved*`
  // update reflects exactly what THAT save sent.
  async function runSave(extra) {
    const { tags, ...extraFields } = extra;  // `tags` is a top-level PUT key, not a field
    // Capture what's actually being sent BEFORE the await (#901 round 3,
    // finding M11) — `lastSaved*` must reflect the payload this save
    // sent, not whatever the controls happen to hold once the PUT
    // resolves (a later change may already have moved them, even with
    // saves serialized — the operator can still edit while a PUT is in
    // flight).
    const sentEffort = effortEl.value;
    const sentHost = hostEl.value.trim();
    const sentModel = modelEl.value;
    const fields = {
      effort: sentEffort || null,
      host: sentHost || null,
      assigned_by: 'board',
      ...extraFields,
    };
    if (catalogReady) fields.model = sentModel || null;
    const patch = { fields };
    if (tags) patch.tags = tags;
    try {
      await putTask(card.id, patch);
      // (#901 round 2, finding A3) Record what actually stuck, so a LATER
      // failed save has something correct to revert to.
      lastSavedEffort = sentEffort;
      lastSavedHost = sentHost;
      if (catalogReady) lastSavedModel = sentModel;
      clearError();
      onSaved();
    } catch (err) {
      // (#901 round 2, finding A3) The server REJECTED this change —
      // revert the controls to their last-known-good values BEFORE
      // surfacing the error, so the rejected choice can't ride along on
      // the next unrelated save with no toast at all. Every other drawer
      // control already does this on failure (board.js's title/notes/
      // context/tags fields and the Assignee select); the assignment
      // pickers didn't, until now. The engine select is deliberately NOT
      // reverted here (#901 round 3, finding M12) — board.js owns the
      // Assignee select and hides this module's engine row, so there's
      // no user-reachable path that leaves it stale.
      restoreSelect(effortEl, lastSavedEffort);
      restoreSelect(hostEl, lastSavedHost);
      if (catalogReady) restoreSelect(modelEl, lastSavedModel);
      showError(err && err.message ? err.message : String(err));
    }
  }

  // A promise chain, not a boolean flag — each call appends its
  // `runSave` onto whatever the previous call is still doing, so calls
  // made back-to-back (a rapid succession of picker changes) run their
  // PUTs one at a time, in order, and `saveChain` always represents "the
  // most recent save, whether or not it has settled yet." `runSave`
  // never rethrows (matching the original, unserialized behavior), so
  // the chain itself never rejects and a failed save can't break saving
  // for subsequent changes.
  let saveChain = Promise.resolve();
  function save(extra = {}) {
    const task = saveChain.then(() => runSave(extra));
    saveChain = task;
    return task;
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
