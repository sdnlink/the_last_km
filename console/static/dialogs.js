import {$,escape,icon,timeLabel,stableHTML} from './ui.js';
import {paginate,resetPage,onPaginationChange} from './pagination.js';
import {api,config,fragment,toast,runAction,refresh} from './core.js';
const loaded=new Map();
const state={config:null,rolesConfig:null,directionTask:null,directionPending:new Set()};
let directionTimer,stopTarget;
let gitSequence=0,gitController;
let acceptanceScan=null,acceptanceSequence=0;
let acceptanceScanRequest=0,preflightRequest=0;
let createSequence=0;
const createPending=new Set();
const taskDialogs=Object.fromEntries(['roles','resume','delete'].map(name=>[name,{sequence:0,target:null,pending:new Set()}]));
const submitButtons={roles:'roles-submit',resume:'resume-submit',delete:'confirm-delete'};
function beginTaskDialog(name){
 const view=taskDialogs[name];view.target=null;
 const button=$('#'+submitButtons[name]);if(button)button.disabled=true;
 return ++view.sequence;
}
function currentTaskDialog(name,sequence,id){
 const view=taskDialogs[name];
 return sequence===view.sequence&&(id===undefined||(view.target===id&&$('#'+name+'-dialog').open));
}
function showTaskDialog(name,id){
 const view=taskDialogs[name];view.target=id;
 $('#'+submitButtons[name]).disabled=view.pending.has(id);
 if(!$('#'+name+'-dialog').open)$('#'+name+'-dialog').showModal();
}
function bindTaskDialog(name){
 $('#'+name+'-dialog').addEventListener('close',()=>{taskDialogs[name].sequence++;taskDialogs[name].target=null;});
}
function settleTaskDialog(name,id){
 const view=taskDialogs[name];view.pending.delete(id);
 // Reopening the same task must still reflect its outstanding operation.
 if(view.target===id)$('#'+submitButtons[name]).disabled=false;
}
function currentCreate(sequence){return sequence===createSequence&&$('#create-dialog')?.open!==false;}
const CUSTOM_MODEL='__custom_model__';
function providerOptions(cfg,reviewer=false){return cfg.providers.map(p=>`<option value="${escape(p.name)}" ${p.enabled===false||reviewer&&p.can_review===false?'disabled':''}>${escape(p.label||p.name)}${p.enabled===false?'（未启用）':p.available?'':'（未安装）'}${reviewer&&p.can_review===false?'（仅 worker）':''}</option>`).join('');}
export function selectedModels(form,data){
 for(const key of ['model','reviewer_model']){
  if(data[key]===CUSTOM_MODEL){
   const value=form.elements[key+'_custom']?.value.trim();
   if(!value)throw new Error('请填写自定义模型 ID');
   data[key]=value;
  }
  delete data[key+'_custom'];
 }
 return data;
}
export function modelChoices(form,cfg,{preserve=false,values={},roles=['worker','reviewer']}={}){
 const catalog=cfg.agent_catalog||{};
 for(const [role,providerKey,modelKey,backendKey] of [['worker','provider','model','paseo_backend'],['reviewer','reviewer','reviewer_model','reviewer_paseo_backend']]){
  if(!roles.includes(role))continue;
  const field=form.elements[modelKey];if(!field)continue;
  const provider=form.elements[providerKey].value;
  const backend=form.elements[backendKey];
  let rows=catalog.models?.[provider]?.models||[];
  let note=catalog.models?.[provider]?.note||'未枚举到模型，可选择执行器默认或填写自定义 ID。';
  if(provider==='paseo'){
   const entry=(catalog.paseo_backends||[]).find(p=>p.id===backend?.value);
   rows=entry?.models||[];note=entry?.note||(entry?.available?'Paseo 后端目录；账户权限以实际调用为准。':'未确认此 Paseo 后端可用，请刷新目录或选择已发现的后端。');
  }
  const previous=values[modelKey]??field.value;
  const items=[...rows];
  if(preserve&&previous&&previous!==CUSTOM_MODEL&&!items.some(m=>m.id===previous))items.push({id:previous,label:previous+'（已保存，未在当前目录发现）'});
  const label=role==='reviewer'?'自动选择（同执行器及后端沿用 Worker，否则使用默认模型）':'使用执行器默认模型';
  field.innerHTML=`<option value="">${label}</option>`+items.map(m=>`<option value="${escape(m.id)}">${m.label&&m.label!==m.id?escape(m.label)+' — ':''}${escape(m.id)}</option>`).join('')+`<option value="${CUSTOM_MODEL}">自定义模型 ID…</option>`;
  field.value=items.some(m=>m.id===previous)||(preserve&&previous===CUSTOM_MODEL)?previous:'';
  const custom=form.elements[modelKey+'_custom'];if(custom)custom.hidden=field.value!==CUSTOM_MODEL;
  const hint=form.querySelector(`[data-model-note="${role}"]`);if(hint)hint.textContent=note;
 }
}
function backendChoices(form,cfg){
 for(const [key,initial] of [['paseo_backend','paseo'],['reviewer_paseo_backend','claude']]){
  const field=form.elements[key];if(!field)continue;
  const previous=field.value||initial;const items=[...(cfg.agent_catalog?.paseo_backends||[])];
  if(!items.some(p=>p.id===previous))items.unshift({id:previous,label:previous,available:false});
  field.innerHTML=items.map(p=>`<option value="${escape(p.id)}" ${key==='reviewer_paseo_backend'&&p.id!=='claude'?'disabled':''}>${escape(p.label||p.id)} — ${escape(p.id)}${p.available?'':'（未确认可用）'}</option>`).join('');field.value=previous;
 }
}
function bindModels(form,cfg){
 for(const [key,role] of [['provider','worker'],['reviewer','reviewer'],['paseo_backend','worker'],['reviewer_paseo_backend','reviewer']])form.elements[key]?.addEventListener('change',()=>modelChoices(form,cfg(),{roles:[role]}));
 for(const key of ['model','reviewer_model'])form.elements[key]?.addEventListener('change',()=>{
  const custom=form.elements[key+'_custom'];if(custom)custom.hidden=form.elements[key].value!==CUSTOM_MODEL;
 });
}
function acceptanceContext(){const f=$('#create-form');return {cwd:f.elements.cwd.value.trim(),task_type:f.elements.task_type.value,python:f.elements.acceptance_python.value.trim()};}
function acceptanceChanged(){acceptanceSequence++;acceptanceScan=null;$('#acceptance-options').textContent='';$('#acceptance-notice').textContent='项目目录、类型或解释器已改变，请重新扫描。';}
async function scanAcceptance(){
 const context=acceptanceContext(),seq=++acceptanceSequence,windowSequence=createSequence,request=++acceptanceScanRequest;$('#scan-acceptance').disabled=true;
 $('#acceptance-notice').textContent='正在只读扫描项目入口…';
 try{
  const data=await api('/api/acceptance/scan',context);
  if(!currentCreate(windowSequence)||seq!==acceptanceSequence||JSON.stringify(context)!==JSON.stringify(acceptanceContext()))return null;
  acceptanceScan={...data,context};
  $('#acceptance-options').innerHTML=data.checks.map(c=>`<article class="acceptance-option"><label class="checkbox-label"><input type="checkbox" data-check-id="${escape(c.id)}" ${c.recommended?'checked':''}><span>${escape(c.title)}<small>${escape(c.purpose)}</small></span></label><small>耗时：${escape(c.estimate)}。${escape(c.effects)}</small><details><summary>命令与运行前提</summary><pre>${escape(c.command)}</pre><p>程序：${escape(c.executable)}${c.dependencies.length?'；依赖：'+escape(c.dependencies.join('、')):''}</p></details></article>`).join('')||'<p>没有发现可识别的检查入口。可选择“先制定验收方案”，或由熟悉项目的人补充高级命令；不能据此宣称功能已经验证。</p>';
  $('#acceptance-notice').textContent=`找到 ${data.checks.length} 项检查入口。${data.notice}`;
  return acceptanceScan;
 }catch(error){if(currentCreate(windowSequence)&&seq===acceptanceSequence)$('#acceptance-notice').textContent=error.message;return null;}
 finally{if(currentCreate(windowSequence)&&request===acceptanceScanRequest)$('#scan-acceptance').disabled=false;}
}
/** Modes that must not carry executable checks or selected scan IDs. */
export function modeOmitsExecution(mode){
 return mode==='planning'||mode==='review_only';
}
/** Build selected_ids for the create payload from mode and currently checked boxes. */
export function selectedIdsForMode(mode,checkedIds){
 return modeOmitsExecution(mode)?[]:[...checkedIds];
}
/** Drop advanced custom commands when the chosen mode forbids execution. */
export function checksForMode(mode,checks){
 return modeOmitsExecution(mode)?[]:(Array.isArray(checks)?checks.filter(Boolean):[]);
}
/** Non-empty modules[].checks that would schedule watcher execution. */
export function moduleExecutionCommands(modules){
 if(!Array.isArray(modules))return [];
 const out=[];
 for(const item of modules){
  if(!item||typeof item!=='object')continue;
  const checks=Array.isArray(item.checks)?item.checks:[];
  for(const command of checks){if(typeof command==='string'&&command.trim())out.push(command.trim());}
 }
 return out;
}
/**
 * Apply review_only/planning execution boundary to a create-form payload.
 * Rejects modules[].checks instead of silently stripping fixed module commands.
 * Mutates and returns data for the /api/tasks body.
 */
export function applyVerificationModeToCreatePayload(data){
 const mode=data.verification_mode;
 data.checks=checksForMode(mode,data.checks);
 if(data.acceptance_plan){
  data.acceptance_plan={...data.acceptance_plan,selected_ids:selectedIdsForMode(mode,data.acceptance_plan.selected_ids||[])};
 }
 if(modeOmitsExecution(mode)&&moduleExecutionCommands(data.modules).length){
  throw new Error('仅语义审核或制定方案不能附带 modules[].checks 执行命令；请清除模块检查或改用执行验收模式');
 }
 if(mode==='executable'&&!(data.acceptance_plan?.selected_ids||[]).length&&!(data.checks||[]).length){
  throw new Error('请选择至少一个检查项；没有入口时可以先制定验收方案');
 }
 return data;
}
function acceptancePlan(){
 const f=$('#create-form');
 if(!acceptanceScan||JSON.stringify(acceptanceScan.context)!==JSON.stringify(acceptanceContext()))throw new Error('请先扫描当前项目并确认检查项');
 const checked=[...$('#acceptance-options').querySelectorAll('[data-check-id]:checked')].map(c=>c.dataset.checkId);
 return {task_type:f.elements.task_type.value,python:acceptanceScan.python,signature:acceptanceScan.signature,
  criteria:f.elements.acceptance_criteria.value.trim(),selected_ids:selectedIdsForMode(f.elements.verification_mode.value,checked)};
}
/** Test hook: seed acceptance scan state as if scanAcceptance just succeeded. */
export function setAcceptanceScanForTest(scan){acceptanceScan=scan;acceptanceSequence+=1;}
/** Test hook: bind create-form listeners after HTML is inserted. */
export function bindCreateFormForTest(){bind('create');}
export async function confirmDelete(id){
 const sequence=beginTaskDialog('delete');
 try{
  await ensure('delete');if(!currentTaskDialog('delete',sequence))return;
  const task=await api(`/api/tasks/${id}`);if(!currentTaskDialog('delete',sequence))return;
  if(!task.actions.includes('delete'))throw new Error('任务仍在执行或状态尚未确认，请先停止');
  $('#delete-task-title').textContent=task.title;$('#delete-error').hidden=true;
  showTaskDialog('delete',id);
 }catch(error){if(currentTaskDialog('delete',sequence))toast(error.message,true);}
}
export async function openResume(id){
 const sequence=beginTaskDialog('resume');
 try{
  await ensure('resume');if(!currentTaskDialog('resume',sequence))return;
  const task=await api(`/api/tasks/${id}`);if(!currentTaskDialog('resume',sequence))return;
  const u=task.usage||task.health?.usage||{};
  $('#resume-usage').textContent=`累计运行 ${Math.ceil((u.elapsed_s||0)/60)} 分钟，已调用 ${u.agent_calls||0} 轮。`;
  $('#resume-form').reset();$('#resume-error').hidden=true;showTaskDialog('resume',id);
 }catch(error){if(currentTaskDialog('resume',sequence))toast(error.message,true);}
}
export async function openRoles(id){
 const sequence=beginTaskDialog('roles');
 try{
  await ensure('roles');if(!currentTaskDialog('roles',sequence))return;
  const task=await api(`/api/tasks/${id}`);if(!currentTaskDialog('roles',sequence))return;
  const cfg=await config();if(!currentTaskDialog('roles',sequence))return;
  if(!task.actions.includes('roles'))throw new Error('请先停止任务并等待检查点保存，再调整角色');
  state.rolesConfig=cfg;const f=$('#roles-form');
  const native={...cfg,providers:cfg.providers.filter(p=>p.name!=='paseo')};
  f.elements.provider.innerHTML=providerOptions(native);f.elements.reviewer.innerHTML=providerOptions(native,true);
  for(const key of ['provider','reviewer','model','reviewer_model'])f.elements[key].value=task[key]||'';
  modelChoices(f,cfg,{preserve:true,values:task});
  if(!f.dataset.modelsBound){bindModels(f,()=>state.rolesConfig);f.dataset.modelsBound='1';}
  $('#roles-error').hidden=true;showTaskDialog('roles',id);
 }catch(error){if(currentTaskDialog('roles',sequence))toast(error.message,true);}
}
let paginationBound=false;
async function ensure(name){
 if(name==='direction'&&!paginationBound){paginationBound=true;onPaginationChange(key=>{if(key==='directions'&&state.currentDirection&&$('#direction-dialog')?.open)renderDirectionHistory(state.currentDirection);});}
 if(!loaded.has(name))loaded.set(name,(async()=>{
   if(!$('#dialog-styles')){const link=document.createElement('link');link.id='dialog-styles';link.rel='stylesheet';link.href='/dialogs.css';document.head.append(link);}
   const html=await fragment(name);$('#dialog-root').insertAdjacentHTML('beforeend',html);bind(name);
 })().catch(error=>{loaded.delete(name);throw error;}));
 await loaded.get(name);
}
function configureForm() {
  const providers = state.config.providers;
  $("#provider-select").innerHTML = providerOptions(state.config);
  $("#reviewer-select").innerHTML = providerOptions(state.config,true);
  $("#provider-select").value =
    providers.find((p) => p.available && p.enabled!==false && p.name === "codex")?.name ||
    providers.find((p) => p.available && p.enabled!==false)?.name ||
    "codex";
  $("#reviewer-select").value =
    providers.find((p) => p.available && p.enabled!==false && p.can_review!==false && p.name !== $("#provider-select").value)
      ?.name || $("#provider-select").value;
  $("#create-form").elements.cwd.value = state.config.cwd;
  backendChoices($('#create-form'),state.config);
  modelChoices($('#create-form'),state.config);
  const status=$('#agent-catalog-status');if(status)status.textContent=`模型目录：${state.config.agent_catalog?.at?new Date(state.config.agent_catalog.at).toLocaleString():'未查询'}。${state.config.agent_catalog?.note||''}`;
  $("#workspace-name").textContent =
    state.config.cwd.split("/").filter(Boolean).at(-1) || "/";
  updateProviderHint();
  acceptanceChanged();
}
function updateProviderHint() {
  if (!state.config) return;
  const worker = $("#provider-select").value,
    reviewer = $("#reviewer-select").value;
  const missing = [...new Set([worker, reviewer])].filter(
    (n) => !state.config.providers.find((p) => p.name === n)?.available,
  );
  const message = missing.length
    ? `${missing.join("、")} 尚未安装，可先保存为待启动任务。`
    : worker === reviewer
      ? "执行与审查使用同一执行器，将分别运行独立会话。"
      : "执行器负责实现，审查器独立检查完成情况。";
  $("#provider-hint").textContent = message;
  $("#paseo-field").hidden = worker !== "paseo";
  const reviewerField=$('#reviewer-paseo-field');if(reviewerField)reviewerField.hidden=reviewer!=='paseo';
}
function renderDirectionHistory(task) {
  if (!task || state.directionTask !== task.id) return;
  $("#direction-task-title").textContent = task.title;
  const description =
    task.direction_delivery === "restart_required"
      ? "当前任务由旧版监督器执行。补充内容会保存，停止并恢复任务后发送给 AI；仅暂停和继续不会升级监督器。"
      : task.direction_delivery === "on_start"
        ? "要求会保存到当前任务，启动或恢复后的下一轮执行时发送给 AI。"
        : "补充要求在下一轮 AI 调用时发送。当前已经开始的调用不会被中途改写，提交不等于 AI 已完成处理。";
  $("#direction-delivery").textContent = task.direction_error || description;
  $("#direction-pause-label").hidden = !task.actions.includes("pause");
  $("#direction-submit").disabled =
    !task.can_direct || state.directionPending.has(task.id);
  const label = (receipt) =>
    !receipt
      ? "待发送"
      : receipt.responded_at || receipt.status === "responded"
        ? `第 ${receipt.responded_round || receipt.round} 轮已返回结果${receipt.status === "sent" ? `；第 ${receipt.round} 轮处理中` : ""}`
        : `已发送至第 ${receipt.round} 轮，等待结果`;
  const page=paginate('directions',(task.directions||[]).slice().reverse(),{rowHeight:140});
  const html =
    page.items
      .map(
        (item) =>
          `<article><div><strong>你 · ${escape(timeLabel(item.at))}</strong><span>执行器：${escape(label(item.worker))}</span></div><p>${escape(item.text)}</p><small>审查器：${escape(task.reviewer === "—" ? "未配置独立审查器" : label(item.reviewer))}</small></article>`,
      )
      .join("") ||
    '<p class="muted">尚未补充要求。可以根据交互结果直接调整优先级、实现方式或补充验收关注点。</p>';
  const root=$('#direction-history');root.dataset.pageList='directions';stableHTML(root,html+page.controls);
}

export async function openCreate(){
 const sequence=++createSequence;
 try{await ensure('create');const cfg=await config();if(sequence!==createSequence)return;state.config=cfg;configureForm();$('#form-error').hidden=true;$('#create-submit').disabled=false;if(!$('#create-dialog').open)$('#create-dialog').showModal();}
 catch(error){if(sequence===createSequence)toast(error.message,true);}
}
function refreshDirection(task){if(state.directionTask===task.id){state.currentDirection=task;renderDirectionHistory(task);}}
async function pollDirection(){
 clearTimeout(directionTimer);if(!state.directionTask||!$('#direction-dialog')?.open)return;
 if(document.hidden){directionTimer=setTimeout(pollDirection,3000);return;}
 const id=state.directionTask;
 try{refreshDirection(await api(`/api/tasks/${id}`));}catch(error){if(state.directionTask===id){$('#direction-error').hidden=false;$('#direction-error').textContent=error.message;}}
 if(state.directionTask===id&&$('#direction-dialog').open)directionTimer=setTimeout(pollDirection,3000);
}
export async function openDirection(id){
 try{await ensure('direction');resetPage('directions');state.directionTask=id;$('#direction-error').hidden=true;$('#direction-text').value=localStorage.getItem(`auto-moniter-direction-${id}`)||'';$('#direction-pause').checked=false;$('#direction-history').textContent='加载任务要求…';delete $('#direction-history').dataset.html;$('#direction-submit').disabled=true;if(!$('#direction-dialog').open)$('#direction-dialog').showModal();await pollDirection();}
 catch(error){toast(error.message,true);}
}
export async function confirmStop(id){
 try{await ensure('confirm');stopTarget=id;if(!$('#confirm-dialog').open)$('#confirm-dialog').showModal();}
 catch(error){toast(error.message,true);}
}
export async function openGitRecord(id,commit){
 const seq=++gitSequence;gitController?.abort();const request=new AbortController();gitController=request;
 try{
  await ensure('git');if(seq!==gitSequence)return;
  $('#git-record-error').hidden=true;$('#git-record-body').hidden=true;$('#git-record-loading').hidden=false;
  if(!$('#git-record-dialog').open)$('#git-record-dialog').showModal();
  const data=await api(`/api/tasks/${id}/git/${commit}`,undefined,{signal:request.signal});
  if(seq!==gitSequence||!$('#git-record-dialog').open)return;
  $('#git-record-project').textContent=data.project;
  $('#git-record-ref').textContent=data.ref.replace(/^refs\/heads\//,'');
  $('#git-record-current').textContent=data.current_branch||'未检出业务分支';
  $('#git-record-commit').textContent=data.commit;$('#git-record-at').textContent=data.committed_at;
  $('#git-record-file').textContent=data.record;$('#git-record-summary').textContent=data.conclusion.summary;
  const outcome=data.conclusion.watcher_result;
  $('#git-record-outcome').textContent=outcome?.status==='rejected'?'系统处理：关闭被拒绝；结论已提交，审计项仍需复核。':outcome?.closed?.length?`系统已登记关闭：${outcome.closed.join('、')}`:'此处展示从 Git 提交读取的结论原文及系统记录。';
  $('#git-record-json').textContent=JSON.stringify(data.content,null,2);
  $('#git-record-command').textContent=data.log_command+'\n'+data.show_command;
  $('#git-record-body').hidden=false;
 }catch(error){if(seq!==gitSequence||error.name==='AbortError')return;const el=$('#git-record-error');if(el){el.hidden=false;el.textContent=error.message;}else toast(error.message,true);}
 finally{if(seq===gitSequence&&$('#git-record-loading'))$('#git-record-loading').hidden=true;}
}
function bind(name){
 if(taskDialogs[name])bindTaskDialog(name);
 if(name==='roles'){
  $('#roles-form').addEventListener('submit',async e=>{
   e.preventDefault();const view=taskDialogs.roles,{target:id,sequence}=view;if(!id||view.pending.has(id))return;
   view.pending.add(id);
   $('#roles-submit').disabled=true;$('#roles-error').hidden=true;
   try{
    const data=selectedModels(e.target,Object.fromEntries(new FormData(e.target)));await api(`/api/tasks/${id}/roles`,data);
    if(currentTaskDialog('roles',sequence,id))$('#roles-dialog').close();
    toast(`任务 ${id} 的角色配置已保存，恢复执行后生效`);await refresh();
   }catch(error){if(currentTaskDialog('roles',sequence,id)){$('#roles-error').hidden=false;$('#roles-error').textContent=error.message;}else toast(`任务 ${id} 调整角色失败：${error.message}`,true);}
   finally{settleTaskDialog('roles',id);}
  });return;
 }

 if(name==='delete'){
  $('#confirm-delete').addEventListener('click',async()=>{
   const view=taskDialogs.delete,{target:id,sequence}=view;if(!id||view.pending.has(id))return;
   view.pending.add(id);
   $('#confirm-delete').disabled=true;$('#delete-error').hidden=true;
   try{
    await api(`/api/tasks/${id}/delete`,{confirm:id});
    localStorage.removeItem(`auto-moniter-direction-${id}`);
    if(currentTaskDialog('delete',sequence,id)){
     $('#delete-dialog').close();
     if(document.body.dataset.taskId===id||new URLSearchParams(location.search).get('task')===id){location.assign('/tasks');return;}
    }
    toast(`任务 ${id} 及运行日志已删除`);await refresh();
   }catch(error){if(currentTaskDialog('delete',sequence,id)){$('#delete-error').hidden=false;$('#delete-error').textContent=error.message;}else toast(`任务 ${id} 删除失败：${error.message}`,true);}
   finally{settleTaskDialog('delete',id);}
  });return;
 }
 if(name==='resume'){
  $('#resume-form').addEventListener('submit',async e=>{
   e.preventDefault();const view=taskDialogs.resume,{target:id,sequence}=view;if(!id||view.pending.has(id))return;
   const data=Object.fromEntries(new FormData(e.target));
   for(const key of Object.keys(data))data[key]=Number(data[key]);
   view.pending.add(id);
   $('#resume-submit').disabled=true;$('#resume-error').hidden=true;
   try{await api(`/api/tasks/${id}/resume`,data);if(currentTaskDialog('resume',sequence,id))$('#resume-dialog').close();toast(`任务 ${id} 已请求恢复`);await refresh();}
   catch(error){if(currentTaskDialog('resume',sequence,id)){$('#resume-error').hidden=false;$('#resume-error').textContent=error.message;}else toast(`任务 ${id} 恢复失败：${error.message}`,true);}
   finally{settleTaskDialog('resume',id);}
  });return;
 }
 if(name==='git'){$('#git-record-dialog').addEventListener('close',()=>{gitSequence++;gitController?.abort();});return;}
 if(name==='confirm'){$('#confirm-stop').addEventListener('click',()=>{const id=stopTarget;$('#confirm-dialog').close();if(id)runAction(id,'stop');});return;}
 if(name==='create'){
  $('#create-dialog')?.addEventListener('close',()=>{
   createSequence++;acceptanceSequence++;acceptanceScan=null;
   for(const id of ['refresh-agents','create-submit','scan-acceptance','preflight-acceptance'])$('#'+id).disabled=false;
  });
  for(const key of ['cwd','task_type','acceptance_python'])$('#create-form').elements[key].addEventListener('input',acceptanceChanged);
  $('#scan-acceptance').addEventListener('click',scanAcceptance);
  $('#preflight-acceptance').addEventListener('click',async()=>{
   const sequence=acceptanceSequence,windowSequence=createSequence,request=++preflightRequest;
   $('#preflight-acceptance').disabled=true;
   try{
    const plan=acceptancePlan();if(!plan.selected_ids.length)throw new Error('请先选择需要检查运行前提的项目检查项');
    const result=await api('/api/acceptance/preflight',{cwd:acceptanceContext().cwd,acceptance_plan:plan});
    if(!currentCreate(windowSequence)||sequence!==acceptanceSequence)return;
    $('#acceptance-notice').textContent=(result.ready?'运行前提已就绪，尚未执行业务检查。':'运行环境尚未就绪，尚未执行业务检查。')+result.checks.map(c=>`${c.title}：${c.errors.join('；')||'前提就绪'}`).join('。')+(result.coverage_warning||'');
   }catch(error){if(currentCreate(windowSequence)&&sequence===acceptanceSequence)$('#acceptance-notice').textContent=error.message;}
   finally{if(currentCreate(windowSequence)&&request===preflightRequest)$('#preflight-acceptance').disabled=false;}
  });
  $('#provider-select').addEventListener('change',updateProviderHint);$('#reviewer-select').addEventListener('change',updateProviderHint);
  bindModels($('#create-form'),()=>state.config);
  $('#refresh-agents')?.addEventListener('click',async()=>{
   const button=$('#refresh-agents');if(button.disabled)return;button.disabled=true;
   const sequence=createSequence;
   try{
    await api('/api/agents/refresh',{});const cfg=await config(true);
    if(sequence!==createSequence||!$('#create-dialog').open)return;
    const form=$('#create-form');
    // Read live values after the response; users may edit while discovery runs.
    const values=Object.fromEntries(['provider','reviewer','model','reviewer_model'].map(key=>[key,form.elements[key].value]));
    state.config=cfg;
    for(const key of ['provider','reviewer']){form.elements[key].innerHTML=providerOptions(state.config,key==='reviewer');form.elements[key].value=values[key];}
    backendChoices(form,state.config);modelChoices(form,state.config,{preserve:true,values});updateProviderHint();
    const catalog=state.config.agent_catalog||{};
    $('#agent-catalog-status').textContent=`目录已更新：${catalog.at?new Date(catalog.at).toLocaleString():'未查询'}。${catalog.note||'目录不等同于账户鉴权或功能验收通过。'}`;
   }catch(error){if(sequence===createSequence)$('#agent-catalog-status').textContent=error.message;}finally{if(sequence===createSequence)button.disabled=false;}
  });
  $('#create-form').elements.auto_start.addEventListener('change',e=>$('#create-submit').textContent=e.target.checked?'创建并启动任务':'保存待启动任务');
  $('#create-form').addEventListener('submit',async e=>{
   e.preventDefault();const sequence=createSequence;if(createPending.has(sequence))return;
   const form=e.target,data=Object.fromEntries(new FormData(form));data.auto_start=form.elements.auto_start.checked;data.checks=data.checks.split('\n').map(s=>s.trim()).filter(Boolean);
   if(form.elements.function_dispatch)data.function_dispatch=data.verification_mode==='executable'&&form.elements.function_dispatch.checked;
   try{selectedModels(form,data);}catch(error){$('#form-error').hidden=false;$('#form-error').textContent=error.message;return;}
   for(const key of ['max_runtime_minutes','max_agent_calls','max_rounds','review_every','turn_timeout','idle_timeout','check_timeout','keep_rounds','max_run_bytes','max_history_bytes','max_agent_state_bytes'])data[key]=Number(data[key]);
   data.protect_paths=(data.protect_paths||'').split('\n').map(s=>s.trim()).filter(Boolean);
   try{data.modules=JSON.parse(data.modules||'[]');}catch(error){$('#form-error').hidden=false;$('#form-error').textContent='模块清单需要有效 JSON 数组';return;}
   try{data.quality_policy=JSON.parse(data.quality_policy||'{}');}catch(error){$('#form-error').hidden=false;$('#form-error').textContent='质量策略需要有效 JSON 对象';return;}
   createPending.add(sequence);$('#create-submit').disabled=true;$('#form-error').hidden=true;
   let submitted=false;
   try{
    if(!acceptanceScan&&data.verification_mode!=='executable')await scanAcceptance();
    if(!currentCreate(sequence))return;
    data.acceptance_plan=acceptancePlan();
    applyVerificationModeToCreatePayload(data);
    submitted=true;const t=await api('/api/tasks',data);
    if(currentCreate(sequence))location.assign(`/tasks/${t.id}`);
    else toast(`任务“${data.title}”已创建（${t.id}），可在任务列表查看`);
   }catch(error){if(currentCreate(sequence)){$('#form-error').hidden=false;$('#form-error').textContent=error.message;}else if(submitted)toast(`任务“${data.title}”创建失败：${error.message}`,true);}
   finally{createPending.delete(sequence);if(currentCreate(sequence))$('#create-submit').disabled=false;}
  });return;
 }
 $('#direction-text').addEventListener('input',e=>{if(state.directionTask)localStorage.setItem(`auto-moniter-direction-${state.directionTask}`,e.target.value);});
 $('#direction-dialog').addEventListener('close',()=>{state.directionTask=null;clearTimeout(directionTimer);});
 $('#direction-form').addEventListener('submit',async e=>{
  e.preventDefault();const id=state.directionTask;if(!id||state.directionPending.has(id))return;
  const text=$('#direction-text').value,pause=!$('#direction-pause-label').hidden&&$('#direction-pause').checked;
  state.directionPending.add(id);$('#direction-submit').disabled=true;$('#direction-error').hidden=true;
  try{
   const t=await api(`/api/tasks/${id}/directions`,{text,pause});
   if(localStorage.getItem(`auto-moniter-direction-${id}`)===text)localStorage.removeItem(`auto-moniter-direction-${id}`);
   if(state.directionTask===id){if($('#direction-text').value===text){$('#direction-text').value='';$('#direction-pause').checked=false;}refreshDirection(t);}
   toast('补充要求已保存，可查看执行器与审查器回执');await refresh();
  }catch(error){if(state.directionTask===id){$('#direction-error').hidden=false;$('#direction-error').textContent=error.message;}else toast(error.message,true);}
  finally{state.directionPending.delete(id);if(state.directionTask===id&&state.currentDirection)renderDirectionHistory(state.currentDirection);}
 });
}
