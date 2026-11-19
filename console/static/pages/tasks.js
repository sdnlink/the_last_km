import {i18n,i18nHTML,locale} from '../i18n.js';
import {$,$$,escape,icon,duration,percent,bytes,badge,taskButtons,taskNarrative,stableHTML,rolesHTML,dateTimeLabel} from '/ui.js';
import {api,watch,refresh,selectNavigation} from '/core.js';
import {paginate,resetPage,onPaginationChange} from '/pagination.js';
const params=new URLSearchParams(location.search);
const state={tasks:[],search:params.get('q')||'',filter:params.get('status')||'all',sort:params.get('sort')||'priority'};
const needsAttention=t=>!['queued','running','starting','paused','done','cancelled','stopped'].includes(t.status);
const running=t=>['running','starting'].includes(t.status);
const labels=()=>({all:i18n('全部任务'),queued:i18n('待启动'),active:i18n('运行中'),paused:i18n('已暂停'),stopped:i18n('已停止'),done:i18n('已验收'),attention:i18n('需要处理')});
function syncTaskLocation(){
 const url=new URL(location.href);
 for(const [key,value,fallback] of [['status',state.filter,'all'],['q',state.search,''],['sort',state.sort,'priority']]){
  if(value===fallback)url.searchParams.delete(key);else url.searchParams.set(key,value);
 }
 history.replaceState(null,'',url);selectNavigation('tasks');
 $('#page-title').textContent=i18n('开发任务');$('#breadcrumb').textContent=i18n('开发任务');
 document.title=i18n('开发任务')+i18n(' · Auto Moniter 开发控制台');
 $$('.filter').forEach(el=>{const selected=el.dataset.filter===state.filter;el.classList.toggle('active',selected);el.setAttribute('aria-pressed',String(selected));});
 $$('[data-task-scope]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.taskScope===state.filter)));
}
function filterTasks(){
 const query=state.search.trim().toLowerCase();
 const priority=t=>needsAttention(t)?0:running(t)?1:t.status==='paused'?2:t.status==='queued'?3:t.status==='stopped'?4:t.status==='done'?5:6;
 const created=t=>Date.parse(t.created_at)||0;
 return state.tasks.filter(t=>({all:true,active:running(t),attention:needsAttention(t),paused:t.status==='paused',queued:t.status==='queued',done:t.status==='done',stopped:t.status==='stopped'})[state.filter])
  .filter(t=>`${t.title} ${t.project_cwd||''} ${t.cwd} ${t.provider} ${t.reviewer}`.toLowerCase().includes(query))
  .slice().sort((a,b)=>(state.sort==='priority'?priority(a)-priority(b):0)||(state.sort==='oldest'?created(a)-created(b):created(b)-created(a))||String(a.id).localeCompare(String(b.id)));
}
function taskRow(t){
 const n=taskNarrative(t),resource=t.resources||{},health=t.health||{};
 const project=(t.project_cwd||t.cwd||'').split('/').filter(Boolean).at(-1)||i18n('目录未知');
 const focus=t.failure?.reason||t.control_message||Object.values(t.roles||{}).find(r=>r.status==='running')?.current_work||t.summary||n.next;
 const total=Math.max(0,Number(t.total_modules)||0),accepted=Math.max(0,Number(t.accepted_modules)||0);
 const progress=total?Math.min(100,Math.round(accepted/total*100)):t.status==='done'?100:0;
 const primary=(t.actions||[]).find(action=>['start','resume','continue','pause'].includes(action));
 const mainAction=primary?taskButtons({...t,actions:[primary]},true):'';
 const mainLink=running(t)?i18n('实时跟进'):needsAttention(t)?i18n('查看原因'):i18n('查看详情');
 const link=running(t)?`/progress?task=${t.id}`:`/tasks/${t.id}`;
 const secondary=taskButtons({...t,actions:(t.actions||[]).filter(action=>action!==primary)},false);
 return `<article class="task-list-item" data-task-id="${escape(t.id)}" data-motion="${n.state}"><div class="task-row-main"><div class="task-row-title">${badge(t)}<a class="task-title" href="/tasks/${escape(t.id)}">${escape(t.title)}</a></div><div class="task-stage">${escape(t.failure?.title||n.title)}</div><p class="task-focus">${escape(focus)}</p><div class="task-row-meta"><span title="${escape(t.project_cwd||t.cwd)}">${icon('folder')}${escape(project)}</span><span>${i18n`第 ${escape(health.rounds||0)} 轮`}</span><span>${i18n('已运行')} ${duration(health.usage?.elapsed_s)}</span></div></div><div class="task-row-progress"><div class="task-progress-value">${total?`<strong>${accepted}<small> / ${total}</small></strong><span>${i18n('模块已验收')}</span>`:`<strong class="task-progress-empty">${t.status==='done'?i18n('已交付'):i18n('尚无模块计划')}</strong>`}</div>${total?`<div class="meter" role="meter" aria-label="${escape(i18n('模块验收进度'))}" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${Math.min(total,accepted)}"><span data-width="${progress}"></span></div>`:''}<p class="task-progress-note">${i18n('以实际验收结果为准')}</p></div><div class="task-row-actions">${mainAction}<a class="button ${running(t)||!primary?'primary':'secondary'} small-button" href="${escape(link)}">${mainLink} →</a></div><details class="task-more" data-key="task-more-${escape(t.id)}"><summary>${i18n('运行详情与更多操作')}</summary><div class="task-more-content"><dl><div><dt>${i18n('项目目录')}</dt><dd>${escape(t.project_cwd||t.cwd)}</dd></div><div><dt>${i18n('执行器 / 审查器')}</dt><dd>${escape(t.provider||'—')} / ${escape(t.reviewer||'—')}</dd></div><div><dt>${i18n('创建时间')}</dt><dd>${escape(dateTimeLabel(t.created_at))}</dd></div><div><dt>${i18n('本地资源')}</dt><dd>${resource.process_count?`CPU ${percent(resource.cpu_percent)} · ${bytes(resource.rss_bytes)}`:i18n('无本地进程')}</dd></div></dl>${Object.keys(t.roles||{}).length?`<div class="board-roles">${rolesHTML(t)}</div>`:''}${t.resume_unavailable?`<p class="task-resume-warning">${escape(t.resume_unavailable)}</p>`:''}<div class="task-more-actions">${secondary}${t.can_direct?`<button class="button secondary small-button" data-direction="${escape(t.id)}">${i18n('调整方向')}</button>`:''}<a class="detail-button" href="/tasks/${escape(t.id)}">${i18n('完整任务详情')} →</a></div></div></details></article>`;
}
function renderTasks(){
 const tasks=filterTasks(),page=paginate('tasks',tasks,{rowHeight:190});
 stableHTML($('#task-rows'),page.items.map(taskRow).join(''));
 $$('#task-rows [data-width]').forEach(el=>el.style.width=el.dataset.width+'%');
 $('#tasks-pagination').innerHTML=page.controls;
 $('#empty-state').hidden=tasks.length>0;
 const noTasks=state.tasks.length===0;
 $('#empty-title').textContent=noTasks?i18n('下一个交付，从这里开始'):i18n('没有匹配的任务');
 $('#empty-description').textContent=noTasks?i18n('添加一个开发任务，让执行器开始实现，让监督器持续跟进。'):i18n('尝试调整状态筛选或搜索关键词。');
 $('#empty-create').hidden=!noTasks;
 $('#table-caption').textContent=i18n`显示 ${page.total?page.start+1:0}–${page.end} 项，筛选 ${tasks.length} 项 / 全部 ${state.tasks.length} 个任务`;
}
function selectFilter(filter){if(!Object.hasOwn(labels(),filter))return;state.filter=filter;resetPage('tasks');syncTaskLocation();renderTasks();}
export function mount(){
 if(!Object.hasOwn(labels(),state.filter))state.filter='all';
 if(!['priority','newest','oldest'].includes(state.sort))state.sort='priority';
 $('#search').value=state.search;$('#task-sort').value=state.sort;syncTaskLocation();
 onPaginationChange(key=>{if(key==='tasks')renderTasks();});$('#refresh').addEventListener('click',refresh);
 $('#search').addEventListener('input',e=>{state.search=e.target.value;resetPage('tasks');syncTaskLocation();renderTasks();});
 $('#filters').addEventListener('click',e=>{const button=e.target.closest('[data-filter]');if(button)selectFilter(button.dataset.filter);});
 $$('[data-task-scope]').forEach(el=>el.addEventListener('click',()=>selectFilter(el.dataset.taskScope)));
 $('#task-sort').addEventListener('change',e=>{state.sort=e.target.value;resetPage('tasks');syncTaskLocation();renderTasks();});
 watch(async()=>{
  const data=await api('/api/tasks');state.tasks=data.tasks;$('#task-count').textContent=data.tasks.length;
  const counts={active:data.tasks.filter(running).length,attention:data.tasks.filter(needsAttention).length,queued:data.tasks.filter(t=>t.status==='queued').length,done:data.tasks.filter(t=>t.status==='done').length};
  for(const [key,value] of Object.entries(counts))$(`#metric-${key}`).textContent=value;
  $('#filter-active').textContent=counts.active;renderTasks();$('#last-updated').textContent=i18n`更新于 ${new Date(data.at).toLocaleTimeString(locale())}`;
 });
}
