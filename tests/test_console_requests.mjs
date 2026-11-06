import assert from 'node:assert/strict';
import {test, beforeEach, afterEach} from 'node:test';

const original = {fetch, setTimeout, clearTimeout, timeout:AbortSignal.timeout, any:AbortSignal.any};
let timers, nextTimer = 0, moduleId = 0;
beforeEach(() => {
  AbortSignal.timeout = undefined;
  AbortSignal.any = undefined;
  timers = new Map();
  globalThis.setTimeout = (callback, delay) => {const id=++nextTimer;timers.set(id,{callback,delay});return id;};
  globalThis.clearTimeout = id => timers.delete(id);
});
afterEach(() => {
  globalThis.fetch = original.fetch;
  globalThis.setTimeout = original.setTimeout;
  globalThis.clearTimeout = original.clearTimeout;
  AbortSignal.timeout = original.timeout;
  AbortSignal.any = original.any;
  delete globalThis.document;
  delete globalThis.window;
});
const load = () => import(`../console/static/core.js?compat=${++moduleId}`);
const response = value => ({ok:true,status:200,json:async()=>value});
function pendingFetch(_path, {signal}) {
  return new Promise((_resolve,reject) => {
    const abort = () => reject(new DOMException('Aborted','AbortError'));
    if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true});
  });
}
function expire() {
  const timer=[...timers.values()].find(t=>t.delay===15000);
  assert.ok(timer,'Each request has a 15 second timeout');
  timer.callback();
}

test('GET works without AbortSignal static helpers and releases listeners', async () => {
  const source = new AbortController();
  let subscriptions = 0;
  const add=source.signal.addEventListener.bind(source.signal),remove=source.signal.removeEventListener.bind(source.signal);
  source.signal.addEventListener=(...args)=>{subscriptions++;add(...args);};
  source.signal.removeEventListener=(...args)=>{subscriptions--;remove(...args);};
  globalThis.fetch=async (_path,{signal})=>{assert.equal(signal.aborted,false);return response({tasks:[]});};
  const {api}=await load();
  assert.deepEqual(await api('/api/tasks',undefined,{signal:source.signal}),{tasks:[]});
  assert.equal(subscriptions,0);
  assert.equal(timers.size,0);
});

test('POST preserves token lookup and the JSON body', async () => {
  const calls=[];
  globalThis.fetch=async (path,options)=>{calls.push({path,...options});return response(path==='/api/config'?{csrf_token:'fixture-token'}:{status:'paused'});};
  const {api}=await load();
  assert.equal((await api('/api/tasks/example/pause',{})).status,'paused');
  assert.equal(calls[1].headers['X-Console-Token'],'fixture-token');
  assert.equal(calls[1].method,'POST');
  assert.equal(calls[1].body,'{}');
  assert.equal(timers.size,0);
});

test('network failure releases the timeout', async () => {
  globalThis.fetch=async ()=>{throw new Error('offline');};
  const {api}=await load();
  await assert.rejects(api('/api/tasks'),/offline/);
  assert.equal(timers.size,0);
});

test('a stalled request times out with a readable error', async () => {
  globalThis.fetch=pendingFetch;
  const {api}=await load();
  const request=api('/api/tasks');expire();
  await assert.rejects(request,/请求超时/);
  assert.equal(timers.size,0);
});

test('the timeout also covers reading the response body', async () => {
  globalThis.fetch=async (path,options)=>({ok:true,status:200,json:()=>pendingFetch(path,options)});
  const {api}=await load();
  const request=api('/api/tasks');await Promise.resolve();expire();
  await assert.rejects(request,/请求超时/);
  assert.equal(timers.size,0);
});

test('caller cancellation remains AbortError', async () => {
  globalThis.fetch=pendingFetch;
  const {api}=await load(), source=new AbortController();
  const request=api('/api/tasks',undefined,{signal:source.signal});source.abort();
  await assert.rejects(request,{name:'AbortError'});
  assert.equal(timers.size,0);
});

test('an already canceled request is not revived', async () => {
  globalThis.fetch=pendingFetch;
  const {api}=await load(), source=new AbortController();source.abort();
  await assert.rejects(api('/api/tasks',undefined,{signal:source.signal}),{name:'AbortError'});
  assert.equal(timers.size,0);
});

test('leaving the page cancels pending requests', async () => {
  const events=new Map();
  globalThis.document={body:{dataset:{workspace:'/tmp',page:'overview'}},querySelector:()=>({}),querySelectorAll:()=>[],addEventListener:()=>{}};
  globalThis.window={addEventListener:(type,callback)=>events.set(type,callback)};
  globalThis.fetch=pendingFetch;
  const {api,initialize}=await load();initialize();
  const request=api('/api/tasks');events.get('pagehide')();
  await assert.rejects(request,{name:'AbortError'});
  assert.equal(timers.size,0);
});

test('malformed JSON keeps the HTTP diagnostic', async () => {
  globalThis.fetch=async ()=>({ok:true,status:200,json:async()=>{throw new SyntaxError('invalid JSON');}});
  const {api}=await load();
  await assert.rejects(api('/api/tasks'),/服务返回异常（200）/);
  assert.equal(timers.size,0);
});
