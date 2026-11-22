import {i18n,i18nHTML} from './i18n.js';
import {$,$$,escape,pending,stateLabel} from './ui.js';
let controller = new AbortController();
let leaving = false, configPromise, refreshJob, timer, busy = false, toastTimer;
export function toast(message, error = false) {
  const el = $('#toast');if(!el)return;el.textContent=message;el.classList.toggle('error',error);el.hidden=false;
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
    try{value=await response.json();}catch(error){if(scope.signal.aborted)throw error;throw new Error(i18n`服务返回异常（${response.status}）`);}
    if(!response.ok){if(body&&response.status===403)configPromise=null;const error=new Error(value.error||i18n`请求失败（${response.status}）`);error.status=response.status;throw error;}
    return value;
  }catch(error){
    if(scope.timedOut())throw new Error(i18n('请求超时，请稍后重试'));
    throw error;
  }finally{scope.dispose();}
}
export async function fragment(name, {signal} = {}){
  const scope=requestScope(signal);
  try {
    const response=await fetch(`/fragments/${name}.html`,{cache:'no-store',signal:scope.signal});
    if(!response.ok)throw new Error(i18n('无法加载操作窗口'));
    return await response.text();
  }catch(error){
    if(scope.timedOut())throw new Error(i18n('请求超时，请稍后重试'));
    throw error;
  }finally{scope.dispose();}
}
export async function refresh() {
  clearTimeout(timer);
  if(leaving||busy||!refreshJob)return;
  if(document.hidden){timer=setTimeout(refresh,3000);return;}
  busy=true;
  if(document.body.dataset.sync!=='offline')document.body.dataset.sync='syncing';
  try {
    await refreshJob();
    document.body.dataset.sync='online';
    $('#connection').innerHTML=stateLabel('complete',i18n('已同步 · 每 3 秒更新'));$('#connection').classList.remove('offline');$('#connection-error').hidden=true;
  }catch(error){
    if(!leaving&&error.name!=='AbortError'){document.body.dataset.sync='offline';$('#connection').innerHTML=stateLabel('attention',i18n('同步中断 · 显示上次记录'));$('#connection').classList.add('offline');$('#connection-error').textContent=i18n`无法同步当前页面：${error.message}。正在自动重连…`;$('#connection-error').hidden=false;}
  }finally{busy=false;if(!leaving)timer=setTimeout(refresh,3000);}
}
export function watch(job){refreshJob=job;refresh();}
export async function runAction(id, action) {
  if(pending.has(id))return;
  pending.add(id);$$(`[data-action][data-id="${id}"]`).forEach(el=>el.disabled=true);
  try {const task=await api(`/api/tasks/${id}/${action}`,{});document.dispatchEvent(new CustomEvent('task-updated',{detail:task}));toast(action==='pause'?i18n('已请求暂停，当前轮次结束后生效'):action==='stop'?i18n('已请求停止并保存进度'):i18n('操作已提交'));await refresh();}
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
  document.body.dataset.sync='connecting';
  document.body.dataset.hidden=String(document.hidden);
  $('#connection').innerHTML=stateLabel('running',i18n('正在连接本地服务'));
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
    const action=e.target.closest('[data-action]');
    if(action){
      const {id,action:name}=action.dataset;
      if(name==='migrate'){(await import('./dialogs.js')).openMigration(id);}
      else if(name==='roles'){(await import('./dialogs.js')).openRoles(id);}
      else if(name==='delete'){(await import('./dialogs.js')).confirmDelete(id);}
      else if(name==='stop'){(await import('./dialogs.js')).confirmStop(id);}
      else if(['resume','checks'].includes(name)){(await import('./dialogs.js')).openResume(id);}
      else runAction(id,name);
      return;
    }
    const direction=e.target.closest('[data-direction]');if(direction){(await import('./dialogs.js')).openDirection(direction.dataset.direction);return;}
    const requirementQueue=e.target.closest('[data-requirement-queue]');if(requirementQueue){(await import('./requirement-queue.js')).openRequirementQueue(requirementQueue.dataset.requirementQueue);return;}
    if(e.target.closest('#new-task,#empty-create,[data-create-task]')){(await import('./dialogs.js')).openCreate();return;}
    const page=e.target.closest('[data-page]');if(page&&page!==document.body){location.assign('/'+page.dataset.page);return;}
    const row=e.target.closest('[data-task-url]');if(row&&!e.target.closest('a,button,input,select'))location.assign(row.dataset.taskUrl);
  });
  document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-task-url]')){e.preventDefault();location.assign(e.target.dataset.taskUrl);}});
  document.addEventListener('visibilitychange',()=>{document.body.dataset.hidden=String(document.hidden);if(!document.hidden)refresh();});
  window.addEventListener('pagehide',()=>{leaving=true;clearTimeout(timer);controller.abort();});
  window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
}
