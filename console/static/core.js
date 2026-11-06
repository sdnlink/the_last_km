import {$,$$,escape,pending} from './ui.js';
let controller = new AbortController();
let leaving = false, configPromise, refreshJob, timer, busy = false, toastTimer;
export function toast(message, error = false) {
  const el = $('#toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000);
}
export async function config() {
  if(!configPromise) configPromise=api('/api/config').catch(error=>{configPromise=null;throw error;});
  return configPromise;
}
export async function api(path, body, {signal} = {}) {
  const headers = body ? {'Content-Type':'application/json','X-Console-Token':(await config()).csrf_token} : {};
  const signals=[controller.signal,AbortSignal.timeout(15000)];if(signal)signals.push(signal);
  const response=await fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store',signal:AbortSignal.any(signals)});
  let value;try{value=await response.json();}catch{throw new Error(`服务返回异常（${response.status}）`);}
  if(!response.ok){const error=new Error(value.error||`请求失败（${response.status}）`);error.status=response.status;throw error;}
  return value;
}
export async function fragment(name){
  const response=await fetch(`/fragments/${name}.html`,{cache:'no-store',signal:controller.signal});
  if(!response.ok)throw new Error('无法加载操作窗口');
  return response.text();
}
export async function refresh() {
  clearTimeout(timer);
  if(leaving||busy||!refreshJob)return;
  if(document.hidden){timer=setTimeout(refresh,3000);return;}
  busy=true;
  try {
    await refreshJob();
    $('#connection').innerHTML='<span class="live-dot"></span>本地服务在线';$('#connection').classList.remove('offline');$('#connection-error').hidden=true;
  }catch(error){
    if(!leaving&&error.name!=='AbortError'){$('#connection').textContent='同步失败';$('#connection').classList.add('offline');$('#connection-error').textContent=`无法同步当前页面：${error.message}。正在自动重连…`;$('#connection-error').hidden=false;}
  }finally{busy=false;if(!leaving)timer=setTimeout(refresh,3000);}
}
export function watch(job){refreshJob=job;refresh();}
export async function runAction(id, action) {
  if(pending.has(id))return;
  pending.add(id);$$(`[data-action][data-id="${id}"]`).forEach(el=>el.disabled=true);
  try {const task=await api(`/api/tasks/${id}/${action}`,{});document.dispatchEvent(new CustomEvent('task-updated',{detail:task}));toast(action==='pause'?'已请求暂停，当前轮次结束后生效':action==='stop'?'已请求停止并保存进度':'操作已提交');await refresh();}
  catch(error){toast(error.message,true);}
  finally{pending.delete(id);$$(`[data-action][data-id="${id}"]`).forEach(el=>el.disabled=false);}
}
export function initialize() {
  $('#workspace-name').textContent=document.body.dataset.workspace.split('/').filter(Boolean).at(-1)||'/';
  const nav=document.body.dataset.page==='detail'?'tasks':document.body.dataset.page;
  $$('[data-nav]').forEach(el=>{el.classList.toggle('active',el.dataset.nav===nav);if(el.dataset.nav===nav)el.setAttribute('aria-current','page');});
  document.addEventListener('click',async e=>{
    const close=e.target.closest('[data-close]');if(close){$(`#${close.dataset.close}`)?.close();return;}
    const action=e.target.closest('[data-action]');if(action){if(action.dataset.action==='stop'){(await import('./dialogs.js')).confirmStop(action.dataset.id);}else runAction(action.dataset.id,action.dataset.action);return;}
    const direction=e.target.closest('[data-direction]');if(direction){(await import('./dialogs.js')).openDirection(direction.dataset.direction);return;}
    if(e.target.closest('#new-task,#empty-create')){(await import('./dialogs.js')).openCreate();return;}
    const page=e.target.closest('[data-page]');if(page&&page!==document.body){location.assign('/'+page.dataset.page);return;}
    const row=e.target.closest('[data-task-url]');if(row&&!e.target.closest('a,button,input,select'))location.assign(row.dataset.taskUrl);
  });
  document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-task-url]')){e.preventDefault();location.assign(e.target.dataset.taskUrl);}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  window.addEventListener('pagehide',()=>{leaving=true;clearTimeout(timer);controller.abort();});
  window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
}
