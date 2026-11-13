import {$,$$,escape,pending} from './ui.js';
let controller = new AbortController();
let leaving = false, configPromise, refreshJob, timer, busy = false, toastTimer;
export function toast(message, error = false) {
  const el = $('#toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000);
}
export async function config(force=false) {
  if(force)configPromise=null;
  if(!configPromise) configPromise=api('/api/config').catch(error=>{configPromise=null;throw error;});
  return configPromise;
}
function requestScope(signal) {
  // Support browsers with AbortController but without AbortSignal.timeout/any.
  const request = new AbortController();
  const sources = [...new Set([controller.signal, signal].filter(Boolean))];
  const cancel = () => request.abort();
  let timedOut = false;
  const timeout = setTimeout(() => {timedOut=true;cancel();},15000);
  for(const source of sources){
    source.addEventListener('abort',cancel,{once:true});
    if(source.aborted)cancel();
  }
  return {
    signal:request.signal,
    timedOut:()=>timedOut,
    dispose(){clearTimeout(timeout);for(const source of sources)source.removeEventListener('abort',cancel);},
  };
}
export async function api(path, body, {signal} = {}) {
  const headers = body ? {'Content-Type':'application/json','X-Console-Token':(await config()).csrf_token} : {};
  const scope=requestScope(signal);
  try {
    const response=await fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store',signal:scope.signal});
    let value;
    try{value=await response.json();}catch(error){if(scope.signal.aborted)throw error;throw new Error(`服务返回异常（${response.status}）`);}
    if(!response.ok){const error=new Error(value.error||`请求失败（${response.status}）`);error.status=response.status;throw error;}
    return value;
  }catch(error){
    if(scope.timedOut())throw new Error('请求超时，请稍后重试');
    throw error;
  }finally{scope.dispose();}
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
export function selectNavigation(page, filter='all', section='home') {
  const selected=page==='detail'?'tasks':page;
  $$('[data-nav-key]').forEach(el=>{
    const current=el.dataset.navKey===selected;
    el.classList.toggle('active',current);
    if(current)el.setAttribute('aria-current','page');
    else el.removeAttribute('aria-current');
  });
}
export function initialize() {
  $('#workspace-name').textContent=document.body.dataset.workspace.split('/').filter(Boolean).at(-1)||'/';
  const filter=new URLSearchParams(globalThis.location?.search||'').get('status')||'all';
  selectNavigation(document.body.dataset.page,filter,document.querySelector('[data-section]')?.dataset?.section||'home');
  let backdropStart=null;
  const outside=(dialog,event)=>{
    const rect=dialog.getBoundingClientRect();
    return event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom;
  };
  document.addEventListener('pointerdown',event=>{
    const dialog=$$('dialog[open]').at(-1);
    backdropStart=dialog&&event.button===0&&outside(dialog,event)?dialog:null;
  });
  document.addEventListener('click',async e=>{
    const backdrop=backdropStart;backdropStart=null;
    if(backdrop?.open&&outside(backdrop,e)){backdrop.close();return;}
    const close=e.target.closest('[data-close]');if(close){$(`#${close.dataset.close}`)?.close();return;}
    const toggle=e.target.closest('[data-toggle-navigation]');if(toggle){const open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));$('#primary-navigation').dataset.expanded=String(open);return;}
    const gitRecord=e.target.closest('[data-git-record]');if(gitRecord){(await import('./dialogs.js')).openGitRecord(gitRecord.dataset.id,gitRecord.dataset.gitRecord);return;}
    const action=e.target.closest('[data-action]');if(action){if(action.dataset.action==='roles'){(await import('./dialogs.js')).openRoles(action.dataset.id);}else if(action.dataset.action==='delete'){(await import('./dialogs.js')).confirmDelete(action.dataset.id);}else if(action.dataset.action==='stop'){(await import('./dialogs.js')).confirmStop(action.dataset.id);}else if(['resume','checks'].includes(action.dataset.action)){(await import('./dialogs.js')).openResume(action.dataset.id);}else runAction(action.dataset.id,action.dataset.action);return;}
    const direction=e.target.closest('[data-direction]');if(direction){(await import('./dialogs.js')).openDirection(direction.dataset.direction);return;}
    const requirementQueue=e.target.closest('[data-requirement-queue]');if(requirementQueue){(await import('./requirement-queue.js')).openRequirementQueue(requirementQueue.dataset.requirementQueue);return;}
    if(e.target.closest('#new-task,#empty-create,[data-create-task]')){(await import('./dialogs.js')).openCreate();return;}
    const page=e.target.closest('[data-page]');if(page&&page!==document.body){location.assign('/'+page.dataset.page);return;}
    const row=e.target.closest('[data-task-url]');if(row&&!e.target.closest('a,button,input,select'))location.assign(row.dataset.taskUrl);
  });
  document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-task-url]')){e.preventDefault();location.assign(e.target.dataset.taskUrl);}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  window.addEventListener('pagehide',()=>{leaving=true;clearTimeout(timer);controller.abort();});
  window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
}
