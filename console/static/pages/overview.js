import {$,escape,bytes,percent,duration,badge,taskButtons,phases,roleStatuses,taskPriority,stableHTML} from '/ui.js';
import {api,watch} from '/core.js';
let tasks=[];
function renderTasks(){
 const query=$('#board-search').value.toLowerCase();
 const html=tasks.filter(t=>`${t.title} ${t.cwd}`.toLowerCase().includes(query)).sort((a,b)=>taskPriority(a)-taskPriority(b)).map(t=>`<article class="task-board-card" data-task-url="/tasks/${t.id}" tabindex="0"><div class="board-card-head"><a class="board-title" href="/tasks/${t.id}">${escape(t.title)}</a>${badge(t)}</div><div class="board-project">${escape(t.cwd)}</div><p class="board-focus">${escape(t.summary||t.roles?.worker?.current_work||t.roles?.reviewer?.current_work||'等待执行记录')}</p><div class="board-roles">${Object.entries(t.roles||{}).map(([role,r])=>`<span>${role} · ${escape(roleStatuses[r.status]||'等待')}</span>`).join('')}</div><div class="board-meta"><span>${escape(phases[t.health.phase]||t.status)} · 第 ${t.health.rounds||0} 轮</span><span>${duration(t.health.usage?.elapsed_s)}</span></div><div class="board-actions"><a class="button primary small-button" href="/progress?task=${t.id}">实时跟进 →</a>${taskButtons(t,true)}${t.can_direct?`<button class="detail-button" data-direction="${t.id}">调整方向</button>`:""}<a class="detail-button" href="/tasks/${t.id}">任务详情</a></div></article>`).join('')||'<p class="live-empty">暂无匹配的运行任务。可在开发任务列表启动或恢复任务。</p>';
 stableHTML($('#task-board'),html);
}
export function mount(){
 $('#board-search').addEventListener('input',renderTasks);
 watch(async()=>{
  const d=await api('/api/overview');tasks=d.tasks;
  for(const [key,value]of Object.entries(d.counts)){const el=$(`#stat-${key}`);if(el)el.textContent=value;}
  $('#stat-waiting').textContent=d.counts.paused;$('#waiting-note').textContent='暂停任务，可从任务列表恢复';
  $('#board-count').textContent=tasks.length;renderTasks();
  const r=d.resources,memory=r.memory_used/r.memory_total*100,disk=r.disk_used/r.disk_total*100;
  for(const [key,value]of [['cpu',r.cpu_percent],['memory',memory],['disk',disk]]){$(`#${key}-value`).textContent=percent(value);$(`#${key}-meter`).style.width=`${Math.min(100,value||0)}%`;}
  $('#cpu-description').textContent=`${r.cpu_count} 核心 · 本机`;$('#memory-description').textContent=`${bytes(r.memory_used)} / ${bytes(r.memory_total)}`;$('#disk-description').textContent=`${bytes(r.disk_free)} 可用`;
  $('#discovery-note').hidden=!d.discovery?.warnings?.length;$('#discovery-note').textContent=(d.discovery?.warnings||[]).join('；');
 });
}
