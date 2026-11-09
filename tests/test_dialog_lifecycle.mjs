/** Delayed reads and writes must retain the dialog's task and editing session. */
import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {config} from '../console/static/core.js';

const originals = Object.fromEntries(['document', 'fetch', 'FormData', 'location', 'localStorage', 'setTimeout', 'clearTimeout'].map(key => [key, globalThis[key]]));
let serial = 0;
afterEach(() => {
  for (const [key, value] of Object.entries(originals)) {
    if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
  }
});
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
function element(initial = '') {
  const listeners = new Map();
  return {
    value: initial, hidden: false, disabled: false, open: false, textContent: '', innerHTML: '', dataset: {},
    classList: {toggle() {}},
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    async emit(type) {
      for (const listener of listeners.get(type) || []) await listener({type, target: this, preventDefault() {}});
    },
    showModal() { this.open = true; },
    close() { this.open = false; return this.emit('close'); },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
}
const response = (value, status = 200) => ({ok: status < 400, status, json: async () => value});
function task(id) {
  return {id, title: `Task ${id}`, actions: ['roles', 'resume', 'delete'],
    provider: id === 'A' ? 'codex' : 'grok', reviewer: 'claude', model: `${id}-model`, reviewer_model: 'opus',
    usage: {elapsed_s: id === 'A' ? 60 : 120, agent_calls: id === 'A' ? 1 : 2}};
}
async function harness() {
  const nodes = {};
  for (const id of ['dialog-styles', 'dialog-root', 'toast', 'roles-dialog', 'roles-form', 'roles-submit', 'roles-error', 'resume-dialog', 'resume-form', 'resume-submit', 'resume-error', 'resume-usage', 'delete-dialog', 'confirm-delete', 'delete-error', 'delete-task-title', 'create-dialog', 'create-form', 'create-submit', 'form-error', 'scan-acceptance', 'preflight-acceptance', 'acceptance-options', 'acceptance-notice', 'provider-hint', 'paseo-field', 'reviewer-paseo-field', 'workspace-name', 'agent-catalog-status', 'refresh-agents']) nodes[id] = element();
  nodes['dialog-root'].insertAdjacentHTML = () => {};
  nodes['roles-form'].elements = Object.fromEntries(['provider', 'reviewer', 'model', 'reviewer_model', 'model_custom', 'reviewer_model_custom'].map(key => [key, element()]));
  nodes['resume-form'].elements = {additional_runtime_minutes: element('0'), additional_agent_calls: element('0')};
  nodes['resume-form'].reset = () => { for (const field of Object.values(nodes['resume-form'].elements)) field.value = '0'; };
  const fields = nodes['create-form'].elements = Object.fromEntries(['title', 'requirement', 'cwd', 'provider', 'reviewer', 'model', 'reviewer_model', 'model_custom', 'reviewer_model_custom', 'paseo_backend', 'reviewer_paseo_backend', 'acceptance_criteria', 'task_type', 'acceptance_python', 'verification_mode', 'checks', 'modules', 'quality_policy', 'protect_paths', 'auto_start', 'max_runtime_minutes', 'max_agent_calls', 'max_rounds', 'review_every', 'turn_timeout', 'idle_timeout', 'check_timeout', 'keep_rounds', 'max_run_bytes', 'max_history_bytes', 'max_agent_state_bytes'].map(key => [key, element()]));
  for (const [key, value] of Object.entries({title: 'draft', requirement: '需求', acceptance_criteria: '标准', task_type: 'code', verification_mode: 'executable', modules: '[]', quality_policy: '{}'})) fields[key].value = value;
  nodes['provider-select'] = fields.provider; nodes['reviewer-select'] = fields.reviewer;
  nodes['acceptance-options'].querySelectorAll = () => [{dataset: {checkId: 'check'}}];
  const routes = new Map(), waiting = new Map(), requests = [], navigations = [], removed = [];
  const cfg = {csrf_token: 'test', cwd: '/tmp/project', providers: [{name: 'codex', available: true}, {name: 'claude', available: true}, {name: 'grok', available: true}], agent_catalog: {models: {}, paseo_backends: []}};
  globalThis.document = {body: {dataset: {}}, querySelector: selector => nodes[selector.slice(1)] || null};
  globalThis.location = {search: '', assign(url) { navigations.push(url); }};
  globalThis.localStorage = {removeItem(key) { removed.push(key); }};
  globalThis.FormData = class {
    constructor(form) { this.rows = Object.entries(form.elements).map(([key, field]) => [key, field.value]); }
    [Symbol.iterator]() { return this.rows[Symbol.iterator](); }
  };
  // Toast timers are incidental UI expiry; request timers still use the real clock.
  globalThis.setTimeout = (callback, delay, ...args) => delay === 5000 ? -1 : originals.setTimeout(callback, delay, ...args);
  globalThis.clearTimeout = timer => { if (timer !== -1) originals.clearTimeout(timer); };
  globalThis.fetch = async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    requests.push({url, options}); waiting.get(key)?.resolve();
    if (routes.has(key)) return routes.get(key);
    if (url === '/api/config') return response(cfg);
    if (url.startsWith('/fragments/')) return {ok: true, text: async () => ''};
    if (options.method !== 'POST' && /^\/api\/tasks\/[AB]$/.test(url)) return response(task(url.at(-1)));
    if (options.method === 'POST') return response({id: 'created-task'});
    throw new Error(`Unexpected request ${key}`);
  };
  await config(true);
  const dialogs = await import(`../console/static/dialogs.js?lifecycle=${++serial}`);
  return {nodes, dialogs, requests, navigations, removed,
    delay(method, url) {
      const result = deferred(), started = deferred(), key = `${method} ${url}`;
      routes.set(key, result.promise); waiting.set(key, started);
      return {resolve: result.resolve, started: started.promise};
    },
    seedScan() {
      dialogs.setAcceptanceScanForTest({context: {cwd: fields.cwd.value, task_type: fields.task_type.value, python: fields.acceptance_python.value}, python: '/usr/bin/python3', signature: 'scan'});
    },
  };
}
const cases = [
  {name: 'roles', open: 'openRoles', submit: 'roles-form', event: 'submit', button: 'roles-submit', error: 'roles-error'},
  {name: 'resume', open: 'openResume', submit: 'resume-form', event: 'submit', button: 'resume-submit', error: 'resume-error'},
  {name: 'delete', open: 'confirmDelete', submit: 'confirm-delete', event: 'click', button: 'confirm-delete', error: 'delete-error'},
];
function edit(h, entry, marker) {
  if (entry.name === 'roles') h.nodes['roles-form'].elements.model.value = marker;
  if (entry.name === 'resume') h.nodes['resume-form'].elements.additional_agent_calls.value = marker;
}
function edited(h, entry) {
  if (entry.name === 'roles') return h.nodes['roles-form'].elements.model.value;
  if (entry.name === 'resume') return h.nodes['resume-form'].elements.additional_agent_calls.value;
  return h.nodes['delete-task-title'].textContent;
}
for (const entry of cases) {
  test(`${entry.name}: late task read cannot overwrite B or change the POST target`, async () => {
    const h = await harness(), delayed = h.delay('GET', '/api/tasks/A');
    const openingA = h.dialogs[entry.open]('A'); await delayed.started;
    await h.dialogs[entry.open]('B'); edit(h, entry, '7');
    const expected = edited(h, entry);
    delayed.resolve(response(task('A'))); await openingA;
    assert.equal(edited(h, entry), expected); assert.equal(h.nodes[`${entry.name}-dialog`].open, true);
    await h.nodes[entry.submit].emit(entry.event);
    const post = h.requests.find(row => row.options.method === 'POST');
    assert.equal(post.url, `/api/tasks/B/${entry.name}`);
    if (entry.name === 'delete') assert.equal(JSON.parse(post.options.body).confirm, 'B');
  });
  test(`${entry.name}: late read errors and close cannot affect another session`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('A');
    const delayed = h.delay('GET', '/api/tasks/A');
    const openingA = h.dialogs[entry.open]('A'); await delayed.started;
    await h.nodes[`${entry.name}-dialog`].close(); await h.dialogs[entry.open]('B'); edit(h, entry, '8');
    const expected = edited(h, entry), beforeToast = h.nodes.toast.textContent;
    delayed.resolve(response({error: 'late A read error'}, 503)); await openingA;
    assert.equal(edited(h, entry), expected); assert.equal(h.nodes.toast.textContent, beforeToast);
    assert.equal(h.nodes[`${entry.name}-dialog`].open, true);
  });
  test(`${entry.name}: closing during read prevents reopening`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('B');
    const delayed = h.delay('GET', '/api/tasks/A');
    const openingA = h.dialogs[entry.open]('A'); await delayed.started;
    await h.nodes[`${entry.name}-dialog`].close();
    delayed.resolve(response(task('A'))); await openingA;
    assert.equal(h.nodes[`${entry.name}-dialog`].open, false);
  });
  for (const status of [200, 503]) test(`${entry.name}: stale POST ${status} cannot close, error or unlock pending B`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('A');
    if (entry.name === 'delete') document.body.dataset.taskId = 'A';
    const old = h.delay('POST', `/api/tasks/A/${entry.name}`);
    const postingA = h.nodes[entry.submit].emit(entry.event); await old.started;
    await h.nodes[`${entry.name}-dialog`].close(); await h.dialogs[entry.open]('B'); edit(h, entry, '9');
    const next = h.delay('POST', `/api/tasks/B/${entry.name}`);
    const postingB = h.nodes[entry.submit].emit(entry.event); await next.started;
    const expected = edited(h, entry);
    old.resolve(response(status === 200 ? {id: 'A'} : {error: 'late A write error'}, status)); await postingA;
    assert.equal(h.nodes[`${entry.name}-dialog`].open, true);
    assert.equal(edited(h, entry), expected); assert.equal(h.nodes[entry.error].hidden, true);
    assert.equal(h.nodes[entry.button].disabled, true); assert.deepEqual(h.navigations, []);
    assert.match(h.nodes.toast.textContent, /A/);
    next.resolve(response({id: 'B'})); await postingB;
    assert.equal(h.nodes[`${entry.name}-dialog`].open, false);
  });
  test(`${entry.name}: same-task pending survives close/reopen and prevents duplicate POST`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('A');
    const delayed = h.delay('POST', `/api/tasks/A/${entry.name}`);
    const posting = h.nodes[entry.submit].emit(entry.event); await delayed.started;
    const operations = [posting, h.nodes[entry.submit].emit(entry.event)];
    try {
      await new Promise(setImmediate);
      await h.nodes[`${entry.name}-dialog`].close(); await h.dialogs[entry.open]('A');
      assert.equal(h.nodes[entry.button].disabled, true);
      operations.push(h.nodes[entry.submit].emit(entry.event)); await new Promise(setImmediate);
      assert.equal(h.requests.filter(row => row.url === `/api/tasks/A/${entry.name}`).length, 1);
    } finally {
      delayed.resolve(response({id: 'A'})); await Promise.all(operations);
    }
    assert.equal(h.nodes[`${entry.name}-dialog`].open, true);
    assert.equal(h.nodes[entry.button].disabled, false);
  });
  test(`${entry.name}: current POST failure preserves input and allows a successful retry`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('B'); edit(h, entry, '10');
    const delayed = h.delay('POST', `/api/tasks/B/${entry.name}`);
    const posting = h.nodes[entry.submit].emit(entry.event); await delayed.started;
    delayed.resolve(response({error: 'current operation failed'}, 503)); await posting;
    assert.equal(h.nodes[entry.error].hidden, false); assert.match(h.nodes[entry.error].textContent, /current operation failed/);
    assert.equal(h.nodes[`${entry.name}-dialog`].open, true); assert.equal(h.nodes[entry.button].disabled, false);
    if (entry.name !== 'delete') assert.equal(edited(h, entry), '10');
    const retry = h.delay('POST', `/api/tasks/B/${entry.name}`);
    const retrying = h.nodes[entry.submit].emit(entry.event); await retry.started;
    retry.resolve(response({id: 'B'})); await retrying;
    assert.equal(h.nodes[`${entry.name}-dialog`].open, false);
  });
  test(`${entry.name}: submitting while a replacement read waits cannot send old fields`, async () => {
    const h = await harness(); await h.dialogs[entry.open]('A');
    const delayed = h.delay('GET', '/api/tasks/B');
    const openingB = h.dialogs[entry.open]('B'); await delayed.started;
    await h.nodes[entry.submit].emit(entry.event);
    assert.equal(h.requests.filter(row => row.options.method === 'POST').length, 0);
    delayed.resolve(response(task('B'))); await openingB;
    assert.equal(h.nodes[entry.button].disabled, false);
  });
}
for (const status of [200, 503]) test(`create: stale POST ${status} preserves new draft and pending submit`, async () => {
  const h = await harness(); await h.dialogs.openCreate(); h.seedScan();
  const old = h.delay('POST', '/api/tasks');
  const postingA = h.nodes['create-form'].emit('submit'); await old.started;
  await h.nodes['create-dialog'].close(); await h.dialogs.openCreate();
  h.nodes['create-form'].elements.requirement.value = 'new edited draft'; h.seedScan();
  const next = h.delay('POST', '/api/tasks');
  const postingB = h.nodes['create-form'].emit('submit'); await next.started;
  old.resolve(response(status === 200 ? {id: 'created-A'} : {error: 'late A create error'}, status)); await postingA;
  assert.equal(h.nodes['create-dialog'].open, true); assert.equal(h.nodes['form-error'].hidden, true);
  assert.equal(h.nodes['create-form'].elements.requirement.value, 'new edited draft');
  assert.equal(h.nodes['create-submit'].disabled, true); assert.deepEqual(h.navigations, []);
  assert.match(h.nodes.toast.textContent, status === 200 ? /created-A/ : /late A create error/);
  next.resolve(response({id: 'created-B'})); await postingB;
  assert.deepEqual(h.navigations, ['/tasks/created-B']);
});

test('create: duplicate submit is ignored and a closed validation cannot create a task', async () => {
  const h = await harness(); await h.dialogs.openCreate();
  h.nodes['create-form'].elements.verification_mode.value = 'review_only';
  const scan = h.delay('POST', '/api/acceptance/scan');
  const posting = h.nodes['create-form'].emit('submit'); await scan.started;
  const duplicate = h.nodes['create-form'].emit('submit'); await new Promise(setImmediate);
  await h.nodes['create-dialog'].close(); await h.dialogs.openCreate();
  h.nodes['create-form'].elements.requirement.value = 'new draft';
  scan.resolve(response({checks: [], python: '/usr/bin/python3', signature: 'old', notice: ''})); await Promise.all([posting, duplicate]);
  assert.equal(h.requests.filter(row => row.url === '/api/acceptance/scan').length, 1);
  assert.equal(h.requests.filter(row => row.url === '/api/tasks').length, 0);
  assert.equal(h.nodes['form-error'].hidden, true);
  assert.equal(h.nodes['create-form'].elements.requirement.value, 'new draft');
  assert.deepEqual(h.navigations, []);
});

test('create: failed POST retains its draft and allows retry', async () => {
  const h = await harness(); await h.dialogs.openCreate(); h.seedScan();
  h.nodes['create-form'].elements.requirement.value = 'retained draft';
  const delayed = h.delay('POST', '/api/tasks');
  const posting = h.nodes['create-form'].emit('submit'); await delayed.started;
  delayed.resolve(response({error: 'create failed'}, 503)); await posting;
  assert.equal(h.nodes['form-error'].hidden, false); assert.equal(h.nodes['form-error'].textContent, 'create failed');
  assert.equal(h.nodes['create-form'].elements.requirement.value, 'retained draft');
  assert.equal(h.nodes['create-submit'].disabled, false);
  const retry = h.delay('POST', '/api/tasks');
  const retrying = h.nodes['create-form'].emit('submit'); await retry.started;
  retry.resolve(response({id: 'retry-task'})); await retrying;
  assert.deepEqual(h.navigations, ['/tasks/retry-task']);
});

test('create: scan context edits discard old results and release the scan button', async () => {
  const h = await harness(); await h.dialogs.openCreate();
  const delayed = h.delay('POST', '/api/acceptance/scan');
  const scanning = h.nodes['scan-acceptance'].emit('click'); await delayed.started;
  h.nodes['create-form'].elements.cwd.value = '/tmp/other'; await h.nodes['create-form'].elements.cwd.emit('input');
  delayed.resolve(response({checks: [], notice: 'obsolete scan'})); await scanning;
  assert.doesNotMatch(h.nodes['acceptance-notice'].textContent, /obsolete scan/);
  assert.equal(h.nodes['scan-acceptance'].disabled, false);
});

test('create: stale preflight result/finally cannot overwrite or unlock a new preflight', async () => {
  const h = await harness(); await h.dialogs.openCreate(); h.seedScan();
  const old = h.delay('POST', '/api/acceptance/preflight');
  const checkingA = h.nodes['preflight-acceptance'].emit('click'); await old.started;
  await h.nodes['create-dialog'].close(); await h.dialogs.openCreate(); h.seedScan();
  const next = h.delay('POST', '/api/acceptance/preflight');
  const checkingB = h.nodes['preflight-acceptance'].emit('click'); await next.started;
  const before = h.nodes['acceptance-notice'].textContent;
  old.resolve(response({error: 'old preflight error'}, 503)); await checkingA;
  assert.equal(h.nodes['acceptance-notice'].textContent, before);
  assert.equal(h.nodes['preflight-acceptance'].disabled, true);
  next.resolve(response({ready: true, checks: []})); await checkingB;
  assert.equal(h.nodes['preflight-acceptance'].disabled, false);
  assert.match(h.nodes['acceptance-notice'].textContent, /尚未执行业务检查/);
});
