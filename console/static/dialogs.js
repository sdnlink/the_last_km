import {$,escape,icon,timeLabel} from './ui.js';
import {api,config,fragment,toast,runAction,refresh} from './core.js';
const loaded=new Map();
const state={config:null,directionTask:null,directionPending:new Set()};
let directionTimer,stopTarget;
async function ensure(name){
 if(!loaded.has(name))loaded.set(name,(async()=>{
   if(!$('#dialog-styles')){const link=document.createElement('link');link.id='dialog-styles';link.rel='stylesheet';link.href='/dialogs.css';document.head.append(link);}
   const html=await fragment(name);$('#dialog-root').insertAdjacentHTML('beforeend',html);bind(name);
 })().catch(error=>{loaded.delete(name);throw error;}));
 await loaded.get(name);
}
function configureForm() {
  const providers = state.config.providers;
  const options = providers
    .map(
      (p) =>
        `<option value="${p.name}">${p.name}${p.available ? "" : "（未安装）"}</option>`,
    )
    .join("");
  $("#provider-select").innerHTML = options;
  $("#reviewer-select").innerHTML = options;
  $("#provider-select").value =
    providers.find((p) => p.available && p.name === "codex")?.name ||
    providers.find((p) => p.available)?.name ||
    "codex";
  $("#reviewer-select").value =
    providers.find((p) => p.available && p.name !== $("#provider-select").value)
      ?.name || $("#provider-select").value;
  $("#create-form").elements.cwd.value = state.config.cwd;
  $("#workspace-name").textContent =
    state.config.cwd.split("/").filter(Boolean).at(-1) || "/";
  updateProviderHint();
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
  $("#paseo-field").hidden = worker !== "paseo" && reviewer !== "paseo";
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
  const html =
    (task.directions || [])
      .map(
        (item) =>
          `<article><div><strong>你 · ${escape(timeLabel(item.at))}</strong><span>执行器：${escape(label(item.worker))}</span></div><p>${escape(item.text)}</p><small>审查器：${escape(task.reviewer === "—" ? "未配置独立审查器" : label(item.reviewer))}</small></article>`,
      )
      .join("") ||
    '<p class="muted">尚未补充要求。可以根据交互结果直接调整优先级、实现方式或补充验收关注点。</p>';
  $("#direction-history").innerHTML = html;
}

export async function openCreate(){
 try{await ensure('create');state.config=await config();configureForm();$('#form-error').hidden=true;if(!$('#create-dialog').open)$('#create-dialog').showModal();}
 catch(error){toast(error.message,true);}
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
 try{await ensure('direction');state.directionTask=id;$('#direction-error').hidden=true;$('#direction-text').value=localStorage.getItem(`auto-moniter-direction-${id}`)||'';$('#direction-pause').checked=false;$('#direction-history').textContent='加载任务要求…';$('#direction-submit').disabled=true;if(!$('#direction-dialog').open)$('#direction-dialog').showModal();await pollDirection();}
 catch(error){toast(error.message,true);}
}
export async function confirmStop(id){
 try{await ensure('confirm');stopTarget=id;if(!$('#confirm-dialog').open)$('#confirm-dialog').showModal();}
 catch(error){toast(error.message,true);}
}
function bind(name){
 if(name==='confirm'){$('#confirm-stop').addEventListener('click',()=>{const id=stopTarget;$('#confirm-dialog').close();if(id)runAction(id,'stop');});return;}
 if(name==='create'){
  $('#provider-select').addEventListener('change',updateProviderHint);$('#reviewer-select').addEventListener('change',updateProviderHint);
  $('#create-form').elements.auto_start.addEventListener('change',e=>$('#create-submit').textContent=e.target.checked?'创建并启动任务':'保存待启动任务');
  $('#create-form').addEventListener('submit',async e=>{
   e.preventDefault();const form=e.target,data=Object.fromEntries(new FormData(form));data.auto_start=form.elements.auto_start.checked;data.checks=data.checks.split('\n').map(s=>s.trim()).filter(Boolean);
   for(const key of ['max_runtime_minutes','max_agent_calls','max_rounds'])data[key]=Number(data[key]);
   $('#create-submit').disabled=true;$('#form-error').hidden=true;
   try{const t=await api('/api/tasks',data);location.assign(`/tasks/${t.id}`);}
   catch(error){$('#form-error').hidden=false;$('#form-error').textContent=error.message;}
   finally{$('#create-submit').disabled=false;}
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
