/**
 * CYCLE-2: review_only / planning execution boundary.
 * Pure helpers + real create-form submit handler (captures /api/tasks body).
 */
import assert from 'node:assert/strict';
import {test, beforeEach, afterEach} from 'node:test';

const original = {
  document: globalThis.document,
  window: globalThis.window,
  fetch: globalThis.fetch,
  location: globalThis.location,
  localStorage: globalThis.localStorage,
  FormData: globalThis.FormData,
};
let moduleId = 0;
let taskPosts = [];
let formErrorText = '';

function field(tag, name, value = '', extra = {}) {
  const listeners = new Map();
  const node = {
    tagName: String(tag).toUpperCase(),
    name,
    type: extra.type || (tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select-one' : 'text'),
    value,
    checked: !!extra.checked,
    disabled: false,
    hidden: false,
    textContent: '',
    innerHTML: '',
    children: [],
    dataset: {...(extra.dataset || {})},
    attrs: {name, ...(extra.id ? {id: extra.id} : {}), ...(extra.type ? {type: extra.type} : {})},
    style: {},
    classList: {toggle() {}, add() {}, remove() {}},
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) || []) fn(event);
      return true;
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  if (extra.id) node.attrs.id = extra.id;
  return node;
}

function buildCreateDom() {
  const byId = {};
  const register = (node) => {
    if (node.attrs?.id) byId[node.attrs.id] = node;
    return node;
  };
  const formListeners = new Map();
  const form = {
    tagName: 'FORM',
    id: 'create-form',
    attrs: {id: 'create-form'},
    children: [],
    addEventListener(type, fn) {
      if (!formListeners.has(type)) formListeners.set(type, []);
      formListeners.get(type).push(fn);
    },
    dispatchEvent(event) {
      event.target = event.target || form;
      for (const fn of formListeners.get(event.type) || []) fn(event);
      return !event.defaultPrevented;
    },
    querySelector(sel) {
      if (sel.startsWith('#')) return byId[sel.slice(1)] || null;
      return null;
    },
    querySelectorAll(sel) {
      if (sel.includes('data-check-id')) {
        const opts = byId['acceptance-options'];
        return (opts?.children || []).filter((c) => {
          if (!c.dataset?.checkId && c.attrs?.['data-check-id'] == null) return false;
          if (sel.includes(':checked') && !c.checked) return false;
          return true;
        });
      }
      return this.children.filter((c) => c.name || c.attrs?.id);
    },
  };
  Object.defineProperty(form, 'elements', {
    get() {
      const named = {};
      for (const child of form.children) {
        if (child.name) named[child.name] = child;
        if (child.attrs?.id) named[child.attrs.id] = child;
      }
      named.namedItem = (n) => named[n];
      return named;
    },
  });
  register(form);

  const add = (node) => {
    form.children.push(register(node));
    return node;
  };

  add(field('input', 'title', 'demo'));
  add(field('textarea', 'requirement', 'need work'));
  add(field('input', 'cwd', '/tmp/proj', {id: undefined}));
  form.children[form.children.length - 1].attrs.id = undefined;
  // cwd must be findable as elements.cwd
  add(field('select', 'task_type', 'code'));
  add(field('textarea', 'acceptance_criteria', 'done well'));
  add(field('select', 'verification_mode', 'executable'));
  add(field('input', 'acceptance_python', ''));
  add(field('textarea', 'checks', ''));
  add(field('textarea', 'modules', '[]'));
  add(field('textarea', 'quality_policy', '{}'));
  add(field('textarea', 'protect_paths', ''));
  add(field('input', 'provider', 'codex'));
  add(field('input', 'reviewer', 'claude'));
  add(field('input', 'model', ''));
  add(field('input', 'reviewer_model', ''));
  add(field('input', 'paseo_backend', 'claude'));
  add(field('select', 'review_isolation', 'strict'));
  add(field('select', 'worker_isolation', 'provider'));
  for (const [name, value] of [
    ['max_runtime_minutes', '120'], ['max_agent_calls', '10'], ['max_rounds', '5'],
    ['review_every', '5'], ['turn_timeout', '1800'], ['idle_timeout', '900'],
    ['check_timeout', '600'], ['keep_rounds', '50'], ['max_run_bytes', '1'],
    ['max_history_bytes', '1'], ['max_agent_state_bytes', '1'],
  ]) add(field('input', name, value, {type: 'number'}));
  add(field('input', 'auto_start', 'on', {type: 'checkbox', checked: false}));

  const acceptanceOptions = {
    tagName: 'DIV',
    attrs: {id: 'acceptance-options'},
    children: [],
    textContent: '',
    innerHTML: '',
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      if (!sel.includes('data-check-id')) return [];
      return this.children.filter((c) => {
        if (sel.includes(':checked') && !c.checked) return false;
        return true;
      });
    },
  };
  byId['acceptance-options'] = acceptanceOptions;

  const notice = field('p', '', '');
  notice.attrs = {id: 'acceptance-notice'};
  byId['acceptance-notice'] = notice;

  const formError = field('div', '', '');
  formError.attrs = {id: 'form-error'};
  Object.defineProperty(formError, 'textContent', {
    get() { return formErrorText; },
    set(v) { formErrorText = String(v); },
    configurable: true,
  });
  formError.hidden = true;
  byId['form-error'] = formError;

  const createSubmit = field('button', '', 'submit', {id: 'create-submit', type: 'submit'});
  createSubmit.attrs.id = 'create-submit';
  byId['create-submit'] = createSubmit;

  byId['scan-acceptance'] = field('button', '', 'scan', {id: 'scan-acceptance', type: 'button'});
  byId['scan-acceptance'].attrs.id = 'scan-acceptance';
  byId['preflight-acceptance'] = field('button', '', 'pre', {id: 'preflight-acceptance', type: 'button'});
  byId['preflight-acceptance'].attrs.id = 'preflight-acceptance';
  byId['provider-select'] = field('select', 'provider', 'codex', {id: 'provider-select'});
  byId['provider-select'].attrs.id = 'provider-select';
  byId['provider-select'].name = 'provider';
  byId['reviewer-select'] = field('select', 'reviewer', 'claude', {id: 'reviewer-select'});
  byId['reviewer-select'].attrs.id = 'reviewer-select';
  byId['reviewer-select'].name = 'reviewer';
  byId['provider-hint'] = field('div', '', '');
  byId['provider-hint'].attrs.id = 'provider-hint';
  byId['paseo-field'] = field('div', '', '');
  byId['paseo-field'].attrs.id = 'paseo-field';
  byId['paseo-field'].hidden = true;
  byId['workspace-name'] = field('span', '', '');
  byId['workspace-name'].attrs.id = 'workspace-name';

  // form.elements must resolve provider-select via id key used in bind? bind uses #provider-select
  form.children.push(byId['provider-select'], byId['reviewer-select'], byId['scan-acceptance'], byId['preflight-acceptance'], createSubmit, acceptanceOptions);

  // Fix cwd name (earlier push may have broken) — ensure elements.cwd exists
  if (!form.elements.cwd) {
    const cwd = field('input', 'cwd', '/tmp/proj');
    form.children.push(cwd);
  }

  const doc = {
    body: {appendChild() {}, children: []},
    head: {append() {}, appendChild() {}},
    hidden: false,
    createElement(tag) {
      return field(tag, '', '');
    },
    querySelector(sel) {
      if (sel === '#create-form') return form;
      if (sel.startsWith('#')) return byId[sel.slice(1)] || null;
      return null;
    },
    querySelectorAll(sel) {
      if (sel.startsWith('#')) {
        const n = doc.querySelector(sel);
        return n ? [n] : [];
      }
      if (sel.includes('data-check-id')) return acceptanceOptions.querySelectorAll(sel);
      return [];
    },
    getElementById(id) { return byId[id] || null; },
    addEventListener() {},
  };

  return {doc, form, byId, acceptanceOptions};
}

function setCheckedOptions(acceptanceOptions, ids) {
  acceptanceOptions.children = ids.map((id) => {
    const node = field('input', '', '', {type: 'checkbox', checked: true, dataset: {checkId: id}});
    node.dataset.checkId = id;
    node.attrs['data-check-id'] = id;
    node.checked = true;
    return node;
  });
}

beforeEach(() => {
  taskPosts = [];
  formErrorText = '';
  const built = buildCreateDom();
  globalThis.__testDom = built;
  globalThis.document = built.doc;
  globalThis.window = globalThis;
  globalThis.localStorage = {
    _d: new Map(),
    getItem(k) { return this._d.has(k) ? this._d.get(k) : null; },
    setItem(k, v) { this._d.set(k, String(v)); },
    removeItem(k) { this._d.delete(k); },
  };
  globalThis.location = {href: '', assign(url) { this.href = url; }};
  globalThis.FormData = class FormData {
    constructor(form) {
      this._pairs = [];
      const seen = new Set();
      for (const child of form.children) {
        const name = child.name;
        if (!name || seen.has(name)) continue;
        seen.add(name);
        if (child.type === 'checkbox') {
          if (child.checked) this._pairs.push([name, 'on']);
          continue;
        }
        if (child.tagName === 'BUTTON') continue;
        this._pairs.push([name, child.value ?? '']);
      }
    }
    *[Symbol.iterator]() { yield* this._pairs; }
  };
  globalThis.fetch = async (path, options = {}) => {
    if (String(path).startsWith('/api/config')) {
      return {
        ok: true, status: 200,
        json: async () => ({
          csrf_token: 'tok', cwd: '/tmp/proj',
          providers: [{name: 'codex', available: true}, {name: 'claude', available: true}],
        }),
      };
    }
    if (String(path).startsWith('/api/acceptance/scan')) {
      return {
        ok: true, status: 200,
        json: async () => ({
          python: '/usr/bin/python3', signature: 'sig-test', notice: 'ok',
          checks: [
            {id: 'python-unittest', title: 'Python', purpose: 't', command: 'py', executable: 'py', dependencies: [], recommended: true, estimate: '1s', effects: 'r'},
          ],
        }),
      };
    }
    if (String(path) === '/api/tasks' && options.method === 'POST') {
      const body = JSON.parse(options.body || '{}');
      taskPosts.push(body);
      return {ok: true, status: 201, json: async () => ({id: 'task-1'})};
    }
    return {ok: true, status: 200, json: async () => ({}), text: async () => ''};
  };
});

afterEach(() => {
  if (original.document === undefined) delete globalThis.document; else globalThis.document = original.document;
  if (original.window === undefined) delete globalThis.window; else globalThis.window = original.window;
  globalThis.fetch = original.fetch;
  if (original.location === undefined) delete globalThis.location; else globalThis.location = original.location;
  if (original.localStorage === undefined) delete globalThis.localStorage; else globalThis.localStorage = original.localStorage;
  if (original.FormData === undefined) delete globalThis.FormData; else globalThis.FormData = original.FormData;
  delete globalThis.__testDom;
});

async function loadDialogs() {
  return import(`../console/static/dialogs.js?boundary=${++moduleId}`);
}

async function submitForm(form) {
  const event = {
    type: 'submit',
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    target: form,
  };
  form.dispatchEvent(event);
  await new Promise((r) => setTimeout(r, 40));
}

test('selectedIdsForMode and checksForMode clear review_only and planning', async () => {
  const {selectedIdsForMode, checksForMode, modeOmitsExecution, moduleExecutionCommands} = await loadDialogs();
  assert.equal(modeOmitsExecution('review_only'), true);
  assert.deepEqual(selectedIdsForMode('review_only', ['python-unittest']), []);
  assert.deepEqual(checksForMode('review_only', ['echo x']), []);
  assert.deepEqual(moduleExecutionCommands([{id: 'M', checks: ['echo marker']}]), ['echo marker']);
});

test('applyVerificationModeToCreatePayload rejects modules checks for review_only', async () => {
  const {applyVerificationModeToCreatePayload} = await loadDialogs();
  assert.throws(() => applyVerificationModeToCreatePayload({
    verification_mode: 'review_only',
    checks: [],
    acceptance_plan: {selected_ids: []},
    modules: [{id: 'M', checks: ['echo marker']}],
  }), /modules\[\]\.checks/);
  const ok = applyVerificationModeToCreatePayload({
    verification_mode: 'review_only',
    checks: ['echo leftover'],
    acceptance_plan: {selected_ids: ['python-unittest']},
    modules: [{id: 'M', checks: []}],
  });
  assert.deepEqual(ok.checks, []);
  assert.deepEqual(ok.acceptance_plan.selected_ids, []);
});

test('create-form submit: residual checks cleared when mode is review_only; restored for executable', async () => {
  const dialogs = await loadDialogs();
  dialogs.bindCreateFormForTest();
  const {form, acceptanceOptions} = globalThis.__testDom;
  assert.ok(form.elements.cwd);
  assert.ok(form.elements.auto_start);
  assert.ok(form.elements.verification_mode);

  form.elements.checks.value = 'echo leftover\nfalse';
  form.elements.modules.value = '[]';
  form.elements.quality_policy.value = '{}';
  form.elements.auto_start.checked = false;
  setCheckedOptions(acceptanceOptions, ['python-unittest', 'node-test']);
  dialogs.setAcceptanceScanForTest({
    python: '/usr/bin/python3',
    signature: 'sig-test',
    context: {cwd: '/tmp/proj', task_type: 'code', python: ''},
    checks: [],
  });

  form.elements.verification_mode.value = 'review_only';
  taskPosts = [];
  formErrorText = '';
  await submitForm(form);
  assert.equal(formErrorText, '', formErrorText);
  assert.equal(taskPosts.length, 1, `posts=${JSON.stringify(taskPosts)}`);
  assert.equal(taskPosts[0].verification_mode, 'review_only');
  assert.deepEqual(taskPosts[0].checks, []);
  assert.deepEqual(taskPosts[0].acceptance_plan.selected_ids, []);

  form.elements.verification_mode.value = 'executable';
  form.elements.checks.value = 'echo leftover';
  setCheckedOptions(acceptanceOptions, ['python-unittest']);
  taskPosts = [];
  await submitForm(form);
  assert.equal(taskPosts.length, 1);
  assert.equal(taskPosts[0].verification_mode, 'executable');
  assert.deepEqual(taskPosts[0].acceptance_plan.selected_ids, ['python-unittest']);
  assert.deepEqual(taskPosts[0].checks, ['echo leftover']);
});

test('create-form submit: modules[].checks with review_only blocks POST and keeps user input', async () => {
  const dialogs = await loadDialogs();
  dialogs.bindCreateFormForTest();
  const {form, acceptanceOptions} = globalThis.__testDom;
  const modulesJson = JSON.stringify([{id: 'M', title: '检查', acceptance: ['完成'], checks: ['echo marker']}]);
  form.elements.modules.value = modulesJson;
  form.elements.checks.value = '';
  form.elements.verification_mode.value = 'review_only';
  form.elements.auto_start.checked = false;
  form.elements.quality_policy.value = '{}';
  setCheckedOptions(acceptanceOptions, ['python-unittest']);
  dialogs.setAcceptanceScanForTest({
    python: '/usr/bin/python3',
    signature: 'sig-test',
    context: {cwd: '/tmp/proj', task_type: 'code', python: ''},
    checks: [],
  });
  taskPosts = [];
  formErrorText = '';
  await submitForm(form);
  assert.equal(taskPosts.length, 0, 'must not call /api/tasks');
  assert.match(formErrorText, /modules\[\]\.checks/);
  assert.equal(form.elements.modules.value, modulesJson);
  assert.equal(form.elements.verification_mode.value, 'review_only');
  assert.equal(form.elements.title.value, 'demo');
});
