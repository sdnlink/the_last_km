import {i18n,i18nHTML} from '../i18n.js';
import {$,escape,badge,taskButtons,active,taskPriority,phases,roleStatuses,duration,stableHTML} from '/ui.js';
import {api,watch} from '/core.js';
import {failureHTML,renderWorkflow,renderConclusions} from '/task-view.js';
import {paginate,resetPage,onPaginationChange} from '/pagination.js';
const selected=new URLSearchParams(location.search).get('task');
let tasks=[],current=null,firstList=true;
function renderPicker(){
 const query=$('#progress-search').value.toLowerCase();
 const visible=tasks.filter(t=>($('#progress-state').value==='all'||active(t)||t.id===selected)&&`${t.title} ${t.project_cwd||''} ${t.cwd}`.toLowerCase().includes(query)).sort((a,b)=>taskPriority(a)-taskPriority(b));
 $('#progress-task-count').textContent=i18n`${visible.length} 个可跟进任务`;
 const page=paginate('progress-tasks',visible,{rowHeight:135});
 $('#progress-pagination').innerHTML=page.controls;
 stableHTML($('#progress-tasks'),page.items.map(t=>`<a class="progress-task ${t.id===selected?'selected':''}" href="/progress?task=${t.id}" ${t.id===selected?'aria-current="true"':''}><div><strong>${escape(t.title)}</strong>${badge(t)}</div><small>${escape(t.project_cwd||t.cwd)}</small><p>${escape(t.failure?.title||phases[t.health.phase]||t.summary||i18n('等待启动'))}</p></a>`).join('')||i18nHTML('<div class="live-empty">暂无匹配任务。可切换“全部任务”查看停止或已完成的工作。</div>'));
}
function renderCurrent(t){
 current=t;$('#progress-empty').hidden=true;$('#progress-selected').hidden=false;
 $('#progress-title').textContent=t.title;$('#progress-project').textContent=t.project_cwd||t.cwd;
 $('#progress-actions').innerHTML=i18nHTML`${badge(t)}${taskButtons(t)}${t.can_direct?i18nHTML`<button class="button secondary" data-direction="${t.id}">调整任务方向</button>`:''}<a class="detail-button" href="/tasks/${t.id}">完整任务详情 →</a>`;
 const isLive=['running','starting'].includes(t.status),roles=t.work?.roles||{};
 const role=Object.values(roles).find(r=>r.status==='running');
 const work=t.failure?.reason||role?.current_work||roles.watcher?.current_work||(isLive?i18n('等待当前轮次发出公开进展'):i18n('当前任务未在执行'));
 $('#progress-notice').innerHTML=failureHTML(t)+`${!isLive?`<p class="detail-alert">${t.status==='paused'?i18n('任务已暂停，当前展示最近一轮记录。'):t.status==='done'?i18n('任务已完成，当前展示最终工作记录。'):i18n('任务当前未在执行，以下为最近工作记录。')}</p>`:''}${t.control_message?`<p class="control-hint">${escape(t.control_message)}</p>`:''}`;
 if(roles.reviewer?.wait_reason)$('#progress-notice').insertAdjacentHTML('beforeend',i18nHTML`<p class="control-hint"><strong>审核调度：</strong>${escape(roles.reviewer.wait_reason)}</p>`);
 $('#progress-focus').innerHTML=i18nHTML`<div class="progress-focus-label">${isLive?i18n('当前工作'):i18n('最近工作')} · 第 ${t.activity?.round||t.health.rounds||0} 轮</div><h3>${escape(work)}</h3><div class="board-roles">${Object.entries(roles).map(([name,r])=>`<span>${name} · ${escape(roleStatuses[r.status]||r.status)}</span>`).join('')}</div>`;
 $('#progress-freshness').textContent=t.activity?.last_output_at?i18n`最近输出 ${duration(t.activity.output_age_s)} 前 · 每 3 秒更新`:i18n('尚未收到本轮公开输出；有新活动时自动显示');
 renderWorkflow(t,{role:$('#progress-role').value,currentOnly:$('#progress-round').value==='current',showRoles:false});renderConclusions(t);
}
export function mount(){
 onPaginationChange(key=>{if(key==='progress-tasks')renderPicker();else if(current)renderCurrent(current);});
 const pickerChanged=()=>{resetPage('progress-tasks');renderPicker();};
 $('#progress-state').addEventListener('change',pickerChanged);$('#progress-search').addEventListener('input',pickerChanged);
 for(const id of ['progress-round','progress-role'])$(`#${id}`).addEventListener('change',()=>{resetPage('workflow');if(current)renderCurrent(current);});
 watch(async()=>{
  const list=await api('/api/tasks');tasks=list.tasks;if(firstList&&!tasks.some(active))$('#progress-state').value='all';firstList=false;renderPicker();
  if(selected){try{renderCurrent(await api(`/api/tasks/${encodeURIComponent(selected)}/progress`));}catch(error){if(error.status===404){$('#progress-empty').hidden=false;$('#progress-selected').hidden=true;$('#progress-empty h2').textContent=i18n('此任务已不存在');$('#progress-empty p').textContent=i18n('请从左侧重新选择要跟进的任务。');}else throw error;}}
 });
}
