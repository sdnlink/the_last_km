import {$,escape,stableHTML} from './ui.js';
import {api,fragment,toast,refresh} from './core.js';

let loaded,sequence=0,readSequence=0,target=null,timer,data=null;
const pending=new Set();
const draftKey=id=>`auto-moniter-requirement-queue-${id}`;
const current=(seq,id)=>sequence===seq&&target===id&&$('#requirement-queue-dialog')?.open;

export function positionOptions(waiting) {
  return '<option value="last">队尾（默认）</option><option value="first">队首（当前需求之后）</option>'+waiting.map(item=>
    `<option value="${escape(item.id)}">第 ${item.position} 位 · 在「${escape(item.text.slice(0,45))}」之前</option>`).join('');
}
export function requirementList(items,{editable=false}={}) {
  return items.map(item=>`<article><div>${item.position?`<strong>第 ${item.position} 位</strong>`:'<strong>已验收</strong>'}${editable?`<button type="button" class="button secondary small-button" data-remove-requirement="${escape(item.id)}">移除</button>`:''}</div><p>${escape(item.text)}</p></article>`).join('')||'<p class="muted">暂无需求</p>';
}
export function requirementQueueMarkup(value) {
  const current=value.active?.text||value.original_requirement;
  return `<h3>当前需求</h3><div class="requirement-text">${value.status==='done'?'所有需求已验收，当前没有正在处理的需求。':escape(current||'暂无')}</div>`
    + `<h3>等待处理 · ${value.waiting.length} 条</h3><p>按以下顺序执行，当前需求未完成时不会提前领取。</p><div class="requirement-queue-list">${requirementList(value.waiting)}</div>`
    + `<details><summary>已验收后续需求 · ${value.completed.length} 条</summary><div class="requirement-queue-list">${requirementList(value.completed)}</div></details>`
    + (value.delivery==='restart_required'?'<p class="detail-alert">当前监督器需停止并恢复后，才能领取需求队列。</p>':'');
}
function render(value) {
  data=value;
  $('#requirement-queue-title').textContent=value.title;
  const warning=value.delivery==='restart_required'?'当前监督器需先停止并恢复，才会领取排队需求。':'当前需求通过审核与验收后，按以下顺序逐条处理。';
  $('#requirement-queue-notice').textContent=value.can_append?warning:'此任务已结束或不支持需求队列，可查看已保存记录。';
  const currentText=value.active?.text||value.original_requirement;
  stableHTML($('#requirement-queue-current'),`<details class="queued-current"><summary>${value.active?'当前后续需求':'原始需求'} · ${value.status==='done'?'已验收':'保留当前处理'}</summary><p>${escape(currentText||'')}</p></details>`);
  stableHTML($('#requirement-queue-waiting'),requirementList(value.waiting,{editable:value.can_append&&!pending.has(target)}));
  stableHTML($('#requirement-queue-completed'),requirementList(value.completed));
  $('#requirement-queue-completed-title').textContent=`已验收的后续需求（${value.completed.length}）`;
  const select=$('#requirement-queue-position'),previous=select.value||'last';
  let options=positionOptions(value.waiting);
  if(!['first','last',...value.waiting.map(item=>item.id)].includes(previous))options+=`<option value="${escape(previous)}">所选需求已离队，请重新选择位置</option>`;
  stableHTML(select,options);select.value=previous;
  $('#requirement-queue-submit').disabled=!value.can_append||pending.has(target);
}
async function poll(seq=sequence,id=target) {
  clearTimeout(timer);if(!current(seq,id))return;
  if(!document.hidden&&!pending.has(id)){
    const read=++readSequence;
    try{const value=await api(`/api/tasks/${id}/requirement-queue`);if(current(seq,id)&&read===readSequence)render(value);}
    catch(error){if(current(seq,id)&&read===readSequence){$('#requirement-queue-error').hidden=false;$('#requirement-queue-error').textContent=error.message;}}
  }
  if(current(seq,id))timer=setTimeout(()=>poll(seq,id),3000);
}
async function mutate(body) {
  const id=target,seq=sequence;if(!data?.can_append||pending.has(id))return;
  pending.add(id);readSequence++;render(data);$('#requirement-queue-error').hidden=true;
  try{
    const value=await api(`/api/tasks/${id}/requirement-queue`,body);
    if(body.action!=='remove'&&localStorage.getItem(draftKey(id))===body.text)localStorage.removeItem(draftKey(id));
    if(current(seq,id)){
      if(body.action!=='remove'&&$('#requirement-queue-text').value===body.text)$('#requirement-queue-text').value='';
      render(value);
    }
    toast(body.action==='remove'?'已移出等待队列':'需求已排队，当前需求验收完成后处理');
    await refresh();
  }catch(error){if(current(seq,id)){$('#requirement-queue-error').hidden=false;$('#requirement-queue-error').textContent=error.message;}else toast(`任务 ${id}：${error.message}`,true);}
  finally{pending.delete(id);if(target===id&&data)render(data);}
}
async function ensure() {
  if(!loaded)loaded=(async()=>{
    if(!$('#dialog-styles')){const link=document.createElement('link');link.id='dialog-styles';link.rel='stylesheet';link.href='/dialogs.css';document.head.append(link);}
    $('#dialog-root').insertAdjacentHTML('beforeend',await fragment('requirement-queue'));
    $('#requirement-queue-dialog').addEventListener('close',()=>{sequence++;target=null;data=null;clearTimeout(timer);});
    $('#requirement-queue-text').addEventListener('input',event=>{if(target)localStorage.setItem(draftKey(target),event.target.value);});
    $('#requirement-queue-form').addEventListener('submit',async event=>{
      event.preventDefault();const text=$('#requirement-queue-text').value,position=$('#requirement-queue-position').value;
      if(!data||!['first','last',...data.waiting.map(item=>item.id)].includes(position)){$('#requirement-queue-error').hidden=false;$('#requirement-queue-error').textContent='所选需求已离开等待队列，请重新选择位置';return;}
      await mutate({text,position});
    });
    $('#requirement-queue-waiting').addEventListener('click',async event=>{const button=event.target.closest('[data-remove-requirement]');if(button)await mutate({action:'remove',id:button.dataset.removeRequirement});});
  })().catch(error=>{loaded=null;throw error;});
  await loaded;
}
export async function openRequirementQueue(id) {
  const seq=++sequence;target=null;data=null;clearTimeout(timer);
  try{
    await ensure();if(seq!==sequence)return;
    target=id;$('#requirement-queue-error').hidden=true;$('#requirement-queue-text').value=localStorage.getItem(draftKey(id))||'';
    $('#requirement-queue-title').textContent='读取任务…';$('#requirement-queue-notice').textContent='正在读取需求队列…';
    for(const selector of ['#requirement-queue-current','#requirement-queue-waiting','#requirement-queue-completed'])stableHTML($(selector),'');
    $('#requirement-queue-position').value='last';$('#requirement-queue-submit').disabled=true;
    if(!$('#requirement-queue-dialog').open)$('#requirement-queue-dialog').showModal();
    await poll(seq,id);
  }catch(error){if(seq===sequence)toast(error.message,true);}
}
