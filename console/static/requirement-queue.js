import {i18n,i18nHTML} from './i18n.js';
import {$,escape,dateTimeLabel,newestFirst,stableHTML} from './ui.js';
import {api,fragment,toast,refresh} from './core.js';

let loaded,sequence=0,readSequence=0,target=null,timer,data=null;
const pending=new Set();
const draftKey=id=>`auto-moniter-requirement-queue-${id}`;
const readDraft=id=>{try{return localStorage.getItem(draftKey(id))||'';}catch{return '';}};
const writeDraft=(id,value)=>{try{localStorage.setItem(draftKey(id),value);}catch{}};
const clearDraft=(id,expected)=>{try{if(localStorage.getItem(draftKey(id))===expected)localStorage.removeItem(draftKey(id));}catch{}};
const current=(seq,id)=>sequence===seq&&target===id&&$('#requirement-queue-dialog')?.open;
const positions=value=>['current','first','last',...value.waiting.map(item=>item.id)];

export function positionOptions(waiting) {
  return i18nHTML('<option value="last">队尾（默认）</option><option value="first">队首（当前需求之后）</option><option value="current">合入当前需求，由 Reviewer 规划</option>')+waiting.map(item=>
    i18nHTML`<option value="${escape(item.id)}">第 ${item.position} 位 · 在「${escape(item.text.slice(0,45))}」之前</option>`).join('');
}
export function requirementList(items,{editable=false}={}) {
  return newestFirst(items).map(item=>`<article><div>${item.position?i18nHTML`<strong>第 ${item.position} 位</strong>`:i18nHTML('<strong>已验收</strong>')}${itemTime(item)}${editable?i18nHTML`<button type="button" class="button secondary small-button" data-remove-requirement="${escape(item.id)}">移除</button>`:''}</div><p>${escape(item.text)}</p></article>`).join('')||i18nHTML('<p class="muted">暂无需求</p>');
}
function itemTime(item) {
  return item.at?`<time datetime="${escape(item.at)}">${escape(dateTimeLabel(item.at))}</time>`:'';
}
function amendmentsMarkup(items=[]) {
  return items.length?i18nHTML`<details open><summary>需求补充记录 · ${items.length} 条</summary><div class="requirement-queue-list">${newestFirst(items).map(item=>
    i18nHTML`<article><div><strong>${item.planned?i18n('已纳入评审计划'):i18n('等待 Reviewer 规划')}</strong>${itemTime(item)}</div><p>${escape(item.text)}</p></article>`).join('')}</div></details>`:'';
}
function supplementsMarkup(items=[]) {
  return items.length?i18nHTML`<details><summary>补充验收检查 · ${items.length} 次</summary><ul>${newestFirst(items).map(item=>
    i18nHTML`<li>第 ${item.cycle} 个需求 · ${escape(item.task_id||i18n('整体验收'))} · ${item.count} 条命令${item.criterion_ids.length?' · '+escape(item.criterion_ids.join('、')):''} · <code title="${escape(item.tree)}">${escape(item.tree.slice(0,12))}</code></li>`).join('')}</ul></details>`:'';
}
export function requirementQueueMarkup(value) {
  const current=value.active?.text||value.original_requirement;
  return i18nHTML`<h3>当前需求</h3><div class="requirement-text">${value.status==='done'?i18n('所有需求已验收，当前没有正在处理的需求。'):escape(current||i18n('暂无'))}</div>`
    + amendmentsMarkup(value.amendments)
    + supplementsMarkup(value.check_supplements)
    + i18nHTML`<h3>等待处理 · ${value.waiting.length} 条</h3><p>最新追加的需求显示在前。当前需求验收后，按标注的队列位置依次处理。</p><div class="requirement-queue-list">${requirementList(value.waiting)}</div>`
    + i18nHTML`<details><summary>已验收后续需求 · ${value.completed.length} 条</summary><div class="requirement-queue-list">${requirementList(value.completed)}</div></details>`
    + (value.delivery==='restart_required'?i18nHTML('<p class="detail-alert">当前监督器需停止并恢复后，才能领取需求队列。</p>'):'');
}
function render(value) {
  data=value;
  $('#requirement-queue-title').textContent=value.title;
  const warning=value.delivery==='restart_required'?i18n('当前监督器需先停止并恢复，才会领取排队需求。'):i18n('最新追加的需求显示在前。当前需求验收后，按标注的队列位置依次处理。');
  $('#requirement-queue-notice').textContent=value.can_append?warning:i18n('此任务已结束或不支持需求队列，可查看已保存记录。');
  const currentText=value.active?.text||value.original_requirement;
  stableHTML($('#requirement-queue-current'),`<details class="queued-current"><summary>${value.active?i18n('当前后续需求'):i18n('原始需求')} · ${value.status==='done'?i18n('已验收'):i18n('保留当前处理')}</summary><p>${escape(currentText||'')}</p></details>`+amendmentsMarkup(value.amendments));
  stableHTML($('#requirement-queue-waiting'),requirementList(value.waiting,{editable:value.can_append&&!pending.has(target)}));
  stableHTML($('#requirement-queue-completed'),requirementList(value.completed));
  $('#requirement-queue-completed-title').textContent=i18n`已验收的后续需求（${value.completed.length}）`;
  const select=$('#requirement-queue-position'),previous=select.value||'last';
  let options=positionOptions(value.waiting);
  if(!positions(value).includes(previous))options+=i18nHTML`<option value="${escape(previous)}">所选需求已离队，请重新选择位置</option>`;
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
    if(body.action!=='remove')clearDraft(id,body.text);
    if(current(seq,id)){
      if(body.action!=='remove'&&$('#requirement-queue-text').value===body.text)$('#requirement-queue-text').value='';
      render(value);
    }
    toast(body.action==='remove'?i18n('已移出等待队列'):body.position==='current'?i18n('补充需求已保存，等待 Reviewer 规划'):i18n('需求已排队，当前需求验收完成后处理'));
    await refresh();
  }catch(error){if(current(seq,id)){$('#requirement-queue-error').hidden=false;$('#requirement-queue-error').textContent=error.message;}else toast(i18n`任务 ${id}：${error.message}`,true);}
  finally{pending.delete(id);if(target===id&&data)render(data);}
}
async function ensure() {
  if(!loaded)loaded=(async()=>{
    if(!$('#dialog-styles')){const link=document.createElement('link');link.id='dialog-styles';link.rel='stylesheet';link.href='/dialogs.css';document.head.append(link);}
    $('#dialog-root').insertAdjacentHTML('beforeend',await fragment('requirement-queue'));
    $('#requirement-queue-dialog').addEventListener('close',()=>{sequence++;target=null;data=null;clearTimeout(timer);});
    $('#requirement-queue-text').addEventListener('input',event=>{if(target)writeDraft(target,event.target.value);});
    $('#requirement-queue-form').addEventListener('submit',async event=>{
      event.preventDefault();const text=$('#requirement-queue-text').value,position=$('#requirement-queue-position').value;
      if(!data||!positions(data).includes(position)){$('#requirement-queue-error').hidden=false;$('#requirement-queue-error').textContent=i18n('所选需求已离开等待队列，请重新选择位置');return;}
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
    target=id;$('#requirement-queue-error').hidden=true;$('#requirement-queue-text').value=readDraft(id);
    $('#requirement-queue-title').textContent=i18n('读取任务…');$('#requirement-queue-notice').textContent=i18n('正在读取需求队列…');
    for(const selector of ['#requirement-queue-current','#requirement-queue-waiting','#requirement-queue-completed'])stableHTML($(selector),'');
    $('#requirement-queue-position').value='last';$('#requirement-queue-submit').disabled=true;
    if(!$('#requirement-queue-dialog').open)$('#requirement-queue-dialog').showModal();
    await poll(seq,id);
  }catch(error){if(seq===sequence)toast(error.message,true);}
}
