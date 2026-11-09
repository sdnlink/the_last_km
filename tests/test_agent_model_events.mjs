/** Role changes and delayed catalog responses exercise the real dialog handlers. */
import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {config} from '../console/static/core.js';

const original = Object.fromEntries(['document', 'fetch', 'FormData', 'location'].map(key => [key, globalThis[key]]));
let serial = 0;
afterEach(() => {
  for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
  }
});

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
const decode = text => text.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
function element(select = false, initial = '') {
  const listeners = new Map();
  let value = initial, html = '', options = [];
  return {
    hidden: false, disabled: false, textContent: '', dataset: {}, checked: false,
    get value() { return value; },
    set value(next) { value = !select || options.includes(String(next)) ? String(next) : ''; },
    get innerHTML() { return html; },
    set innerHTML(next) {
      html = next;
      if (select) {
        options = [...next.matchAll(/<option value="([^"]*)"/g)].map(match => decode(match[1]));
        value = options[0] || '';
      }
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    async emit(type, detail = {}) {
      for (const listener of listeners.get(type) || []) await listener({type, target: this, preventDefault() {}, ...detail});
    },
    showModal() { this.open = true; },
    async close() { this.open = false; await this.emit('close'); },
    querySelectorAll() { return []; },
  };
}
function catalog() {
  return {
    cwd: '/tmp/project', csrf_token: 'fixture-token',
    providers: ['codex', 'grok', 'claude', 'paseo'].map(name => ({name, available: true})),
    agent_catalog: {
      at: '2026-11-09T00:00:00Z', note: '目录不等同于账户鉴权或功能验收通过',
      models: {codex: {models: [{id: 'sol'}]}, grok: {models: [{id: 'grok-model'}]}, claude: {models: [{id: 'opus'}]}},
      paseo_backends: [{id: 'claude', available: true, models: [{id: 'paseo-opus'}]}, {id: 'grok', available: true, models: [{id: 'paseo-grok'}]}],
    },
  };
}
async function harness() {
  const fields = {};
  for (const key of ['provider', 'reviewer', 'model', 'reviewer_model', 'paseo_backend', 'reviewer_paseo_backend']) fields[key] = element(true);
  for (const key of ['title', 'requirement', 'cwd', 'model_custom', 'reviewer_model_custom', 'acceptance_criteria', 'task_type', 'acceptance_python', 'verification_mode', 'checks', 'modules', 'quality_policy', 'auto_start']) fields[key] = element();
  Object.assign(fields.title, {value: 'demo'});
  Object.assign(fields.requirement, {value: '需求'});
  Object.assign(fields.acceptance_criteria, {value: '完成标准'});
  Object.assign(fields.task_type, {value: 'code'});
  Object.assign(fields.verification_mode, {value: 'executable'});
  fields.modules.value = '[]'; fields.quality_policy.value = '{}';
  for (const key of ['max_runtime_minutes', 'max_agent_calls', 'max_rounds', 'review_every', 'turn_timeout', 'idle_timeout', 'check_timeout', 'keep_rounds', 'max_run_bytes', 'max_history_bytes', 'max_agent_state_bytes']) fields[key] = element(false, '1');
  const form = element(); form.elements = fields;
  const notes = {worker: element(), reviewer: element()};
  form.querySelector = selector => notes[selector.includes('"worker"') ? 'worker' : 'reviewer'];
  const nodes = {'create-form': form, 'provider-select': fields.provider, 'reviewer-select': fields.reviewer};
  for (const id of ['create-dialog', 'dialog-styles', 'dialog-root', 'form-error', 'scan-acceptance', 'preflight-acceptance', 'acceptance-options', 'acceptance-notice', 'provider-hint', 'paseo-field', 'reviewer-paseo-field', 'workspace-name', 'agent-catalog-status', 'refresh-agents', 'create-submit']) nodes[id] = element();
  nodes['dialog-root'].insertAdjacentHTML = () => {};
  const roleForm = element();
  roleForm.elements = Object.fromEntries(['provider', 'reviewer', 'model', 'reviewer_model', 'model_custom', 'reviewer_model_custom'].map(key => [key, element(!key.endsWith('_custom'))]));
  roleForm.querySelector = () => null;
  nodes['roles-form'] = roleForm;
  for (const id of ['roles-dialog', 'roles-error', 'roles-submit']) nodes[id] = element();
  globalThis.document = {querySelector: selector => nodes[selector.slice(1)] || null};
  globalThis.FormData = class {
    constructor(target) { this.pairs = Object.entries(target.elements).map(([key, field]) => [key, field.value]); }
    [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
  };
  globalThis.location = {assign(url) { this.url = url; }};
  const requests = [], refreshStarted = deferred(), refreshResult = deferred();
  let currentConfig = catalog(), configResult = null, refreshResponse = refreshResult.promise;
  const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
  globalThis.fetch = async (url, options = {}) => {
    requests.push({url, options});
    if (url === '/api/config') return configResult ? configResult() : response(currentConfig);
    if (url === '/fragments/create.html' || url === '/fragments/roles.html') return {ok: true, text: async () => ''};
    if (url === '/api/agents/refresh') { refreshStarted.resolve(); return refreshResponse; }
    if (url === '/api/tasks') return response({id: 'saved-task'});
    if (url === '/api/tasks/stopped-task') return response({id: 'stopped-task', actions: ['roles'], provider: 'codex', reviewer: 'claude', model: 'saved-private', reviewer_model: 'opus'});
    throw new Error(`Unexpected request ${url}`);
  };
  await config(true);
  const dialogs = await import(`../console/static/dialogs.js?events=${++serial}`);
  await dialogs.openCreate();
  return {fields, form, roleForm, nodes, notes, dialogs, requests, refreshStarted, refreshResult, response,
    setConfig(value) { currentConfig = value; }, onConfig(callback) { configResult = callback; },
    setRefreshResponse(value) { refreshResponse = value; }};
}
async function choose(h, key, value) { h.fields[key].value = value; await h.fields[key].emit('change'); }
async function custom(h, key, value) {
  await choose(h, key, '__custom_model__'); h.fields[key + '_custom'].value = value;
}

test('role events preserve the other role custom model and reset invalid owned models', async () => {
  const h = await harness();
  await custom(h, 'model', 'worker-private');
  await choose(h, 'reviewer', 'paseo');
  assert.equal(h.fields.model.value, '__custom_model__');
  assert.equal(h.fields.model_custom.hidden, false);
  assert.equal(h.fields.model_custom.value, 'worker-private');
  await custom(h, 'reviewer_model', 'reviewer-private');
  await choose(h, 'paseo_backend', 'grok');
  assert.equal(h.fields.reviewer_model.value, '__custom_model__');
  assert.equal(h.fields.reviewer_model_custom.hidden, false);
  await choose(h, 'provider', 'grok');
  assert.equal(h.fields.model.value, '');
  await choose(h, 'model', 'grok-model');
  await choose(h, 'provider', 'paseo');
  assert.equal(h.fields.model.value, '');
  await choose(h, 'model', 'paseo-grok');
  await choose(h, 'paseo_backend', 'claude');
  assert.equal(h.fields.model.value, '');
  assert.match(h.fields.model.innerHTML, /paseo-opus/);
  assert.doesNotMatch(h.fields.model.innerHTML, /paseo-grok/);
  assert.equal(h.fields.reviewer_model.value, '__custom_model__');
});

test('delayed refresh preserves current roles, backends, custom models and create payload', async () => {
  const h = await harness();
  const pending = h.nodes['refresh-agents'].emit('click');
  await h.refreshStarted.promise;
  await choose(h, 'provider', 'paseo'); await choose(h, 'paseo_backend', 'grok');
  await custom(h, 'model', 'worker-after-request');
  await choose(h, 'reviewer', 'paseo'); await custom(h, 'reviewer_model', 'reviewer-after-request');
  h.fields.requirement.value = '刷新中修改的需求'; h.fields.acceptance_criteria.value = '刷新中修改的标准';
  const scan = {context: {cwd: h.fields.cwd.value, task_type: 'code', python: ''}, python: '/usr/bin/python3', signature: 'scan'};
  h.dialogs.setAcceptanceScanForTest(scan);
  h.nodes['acceptance-options'].querySelectorAll = () => [{dataset: {checkId: 'chosen-check'}}];
  h.refreshResult.resolve(h.response({})); await pending;
  assert.equal(h.fields.provider.value, 'paseo'); assert.equal(h.fields.reviewer.value, 'paseo');
  assert.equal(h.fields.paseo_backend.value, 'grok'); assert.equal(h.fields.reviewer_paseo_backend.value, 'claude');
  assert.equal(h.fields.model.value, '__custom_model__'); assert.equal(h.fields.reviewer_model.value, '__custom_model__');
  assert.equal(h.fields.model_custom.hidden, false); assert.equal(h.fields.reviewer_model_custom.hidden, false);
  assert.equal(h.nodes['paseo-field'].hidden, false); assert.equal(h.nodes['reviewer-paseo-field'].hidden, false);
  await h.form.emit('submit');
  const post = h.requests.find(row => row.url === '/api/tasks'); assert.ok(post);
  const body = JSON.parse(post.options.body);
  assert.deepEqual([body.provider, body.reviewer, body.paseo_backend, body.reviewer_paseo_backend, body.model, body.reviewer_model], ['paseo', 'paseo', 'grok', 'claude', 'worker-after-request', 'reviewer-after-request']);
  assert.equal(body.requirement, '刷新中修改的需求'); assert.equal(body.acceptance_criteria, '刷新中修改的标准');
  assert.deepEqual(body.acceptance_plan.selected_ids, ['chosen-check']);
});

test('refresh retains selected models removed from the discovered catalog, with escaped labels', async () => {
  const h = await harness();
  const next = catalog(); next.agent_catalog.models.codex.models = [{id: 'private<&"'}];
  h.setConfig(next);
  // A refreshed catalog can add and later remove an ID while the form is open.
  const pending = h.nodes['refresh-agents'].emit('click');
  await h.refreshStarted.promise; h.refreshResult.resolve(h.response({})); await pending;
  await choose(h, 'model', 'private<&"');
  h.setConfig(catalog());
  await h.nodes['refresh-agents'].emit('click');
  assert.equal(h.fields.model.value, 'private<&"');
  assert.match(h.fields.model.innerHTML, /private&lt;&amp;&quot;/);
  assert.match(h.fields.model.innerHTML, /已保存，未在当前目录发现/);
});

test('empty catalog and unavailable CLI keep current choices without claiming authentication', async () => {
  const h = await harness(); await choose(h, 'model', 'sol');
  const next = catalog(); next.providers.forEach(provider => { provider.available = false; });
  next.agent_catalog.models = {}; next.agent_catalog.paseo_backends = [];
  h.setConfig(next);
  const pending = h.nodes['refresh-agents'].emit('click'); await h.refreshStarted.promise;
  h.refreshResult.resolve(h.response({})); await pending;
  assert.equal(h.fields.model.value, 'sol');
  assert.match(h.fields.provider.innerHTML, /未安装/); assert.match(h.nodes['provider-hint'].textContent, /可先保存/);
  assert.match(h.notes.worker.textContent, /未枚举到模型/);
  assert.match(h.nodes['agent-catalog-status'].textContent, /不等同于账户鉴权/);
});

test('refresh error preserves edits and permits retry', async () => {
  const h = await harness(); await custom(h, 'model', 'retry-model');
  const pending = h.nodes['refresh-agents'].emit('click'); await h.refreshStarted.promise;
  h.fields.requirement.value = '失败时保留';
  h.refreshResult.resolve(h.response({error: '目录查询失败，请重试'}, 503)); await pending;
  assert.equal(h.fields.model.value, '__custom_model__'); assert.equal(h.fields.requirement.value, '失败时保留');
  assert.match(h.nodes['agent-catalog-status'].textContent, /目录查询失败/);
  assert.equal(h.nodes['refresh-agents'].disabled, false);
  h.setRefreshResponse(Promise.resolve(h.response({})));
  await h.nodes['refresh-agents'].emit('click');
  assert.match(h.nodes['agent-catalog-status'].textContent, /目录已更新/);
  assert.equal(h.fields.model.value, '__custom_model__'); assert.equal(h.fields.model_custom.value, 'retry-model');
  assert.equal(h.fields.requirement.value, '失败时保留');
});

test('closing and reopening create ignores the old refresh response', async () => {
  const h = await harness();
  const pending = h.nodes['refresh-agents'].emit('click'); await h.refreshStarted.promise;
  await h.nodes['create-dialog'].close(); await h.dialogs.openCreate();
  await choose(h, 'provider', 'grok'); await custom(h, 'model', 'new-dialog-model');
  h.fields.requirement.value = '新窗口内容';
  const beforeStatus = h.nodes['agent-catalog-status'].textContent;
  const changed = catalog(); changed.agent_catalog.models.grok.models = [{id: 'stale-model'}]; h.setConfig(changed);
  h.refreshResult.resolve(h.response({})); await pending;
  assert.equal(h.fields.provider.value, 'grok'); assert.equal(h.fields.model.value, '__custom_model__');
  assert.equal(h.fields.model_custom.value, 'new-dialog-model'); assert.equal(h.fields.requirement.value, '新窗口内容');
  assert.equal(h.nodes['agent-catalog-status'].textContent, beforeStatus);
  assert.doesNotMatch(h.fields.model.innerHTML, /stale-model/);
  assert.equal(h.nodes['refresh-agents'].disabled, false);
});

test('changes while the refreshed config GET waits preserve the final model namespace', async () => {
  const h = await harness();
  const configStarted = deferred(), configResult = deferred();
  h.onConfig(() => { configStarted.resolve(); return configResult.promise; });
  const pending = h.nodes['refresh-agents'].emit('click'); await h.refreshStarted.promise;
  h.refreshResult.resolve(h.response({})); await configStarted.promise;
  await choose(h, 'provider', 'paseo'); await choose(h, 'paseo_backend', 'grok'); await choose(h, 'model', 'paseo-grok');
  await choose(h, 'reviewer', 'paseo'); await choose(h, 'reviewer_model', 'paseo-opus');
  configResult.resolve(h.response(catalog())); await pending;
  assert.equal(h.fields.provider.value, 'paseo'); assert.equal(h.fields.paseo_backend.value, 'grok');
  assert.equal(h.fields.model.value, 'paseo-grok'); assert.doesNotMatch(h.fields.model.innerHTML, /paseo-opus/);
  assert.equal(h.fields.reviewer.value, 'paseo'); assert.equal(h.fields.reviewer_model.value, 'paseo-opus');
  assert.doesNotMatch(h.fields.reviewer_model.innerHTML, /paseo-grok/);
});

test('roles dialog preserves independent custom/saved models and uses its current catalog', async () => {
  const h = await harness();
  // A new cached config can exist while a create dialog still has an older one.
  const next = catalog(); next.agent_catalog.models.grok.models = [{id: 'new-grok'}];
  h.setConfig(next); await config(true);
  await h.dialogs.openRoles('stopped-task');
  const fields = h.roleForm.elements;
  assert.equal(fields.model.value, 'saved-private');
  assert.match(fields.model.innerHTML, /已保存，未在当前目录发现/);
  fields.reviewer_model.value = '__custom_model__'; await fields.reviewer_model.emit('change');
  fields.reviewer_model_custom.value = 'independent-review';
  fields.provider.value = 'grok'; await fields.provider.emit('change');
  assert.match(fields.model.innerHTML, /new-grok/);
  assert.equal(fields.model.value, '');
  assert.equal(fields.reviewer_model.value, '__custom_model__');
  assert.equal(fields.reviewer_model_custom.value, 'independent-review');
  assert.equal(fields.reviewer_model_custom.hidden, false);
  fields.model.value = '__custom_model__'; await fields.model.emit('change');
  fields.model_custom.value = 'independent-work';
  fields.reviewer.value = 'codex'; await fields.reviewer.emit('change');
  assert.equal(fields.reviewer_model.value, ''); assert.equal(fields.reviewer_model_custom.hidden, true);
  assert.equal(fields.model.value, '__custom_model__'); assert.equal(fields.model_custom.hidden, false);
  assert.deepEqual(h.dialogs.selectedModels(h.roleForm, {model: fields.model.value, reviewer_model: fields.reviewer_model.value}), {model: 'independent-work', reviewer_model: ''});
});
