import {$,$$,escape,icon,active,attention,phases,duration,percent,bytes,badge,taskButtons} from '/ui.js';
import {api,watch,refresh} from '/core.js';
import {paginate,resetPage,onPaginationChange} from '/pagination.js';
const state = {tasks:[],search:new URLSearchParams(location.search).get('q') || '',filter:new URLSearchParams(location.search).get('status') || 'all'};
function renderTasks() {
  const search = state.search.toLowerCase();
  const tasks = state.tasks
    .filter(
      (t) =>
        ({
          all: true,
          active: ["running", "starting"].includes(t.status),
          paused: t.status === "paused",
          stopped: t.status === "stopped",
          queued: t.status === "queued",
          done: t.status === "done",
          attention: attention(t),
        })[state.filter],
    )
    .filter((t) =>
      `${t.title} ${t.cwd} ${t.provider} ${t.reviewer}`
        .toLowerCase()
        .includes(search),
    );
  const page=paginate('tasks',tasks,{rowHeight:85});
  $("#tasks-pagination").innerHTML=page.controls;
  $("#task-rows").dataset.pageList='tasks';
  $("#task-rows").innerHTML = page.items
    .map((t, index) => {
      const progress = t.total_modules
        ? Math.round((t.accepted_modules / t.total_modules) * 100)
        : t.status === "done"
          ? 100
          : 0;
      const resource = t.resources;
      const phase =
        t.control === "stop" && active(t)
          ? "正在停止"
          : t.control === "pause" && t.status !== "paused"
            ? "本轮结束后暂停"
          : phases[t.health.phase] || "";
      const failure = t.failure ? `<small class="phase" title="${escape(t.failure.reason)}">${escape(t.failure.title)}</small>` : '';
      return `<tr data-task-url="/tasks/${t.id}" tabindex="0" aria-label="查看任务 ${escape(t.title)}"><td><div class="task-name"><span class="task-avatar ${["", "blue", "purple"][index % 3]}">${icon("terminal")}</span><div><a class="task-title" href="/tasks/${t.id}" title="${escape(t.title)}">${escape(t.title)}</a><div class="task-path" title="${escape(t.cwd)}">${icon("folder")}<span>${escape(t.cwd || "目录未知")}</span></div></div></div></td><td>${badge(t)}${failure}${active(t) && phase ? `<small class="phase">${escape(phase)}</small>` : ""}</td><td><span class="provider-name">${escape(t.provider)}</span><span class="reviewer-name">审查 · ${escape(t.reviewer)}</span></td><td><div class="module-progress"><div>${t.total_modules ? `${t.accepted_modules}<small>/ ${t.total_modules} 模块</small>` : `<small>${t.status === "done" ? "已验收" : "等待模块计划"}</small>`}</div><div class="meter"><span data-width="${progress}"></span></div></div></td><td class="duration">${duration(t.health.usage?.elapsed_s)}</td><td><div class="resource-cell"><span>${resource.process_count ? `CPU ${percent(resource.cpu_percent)}` : "—"}</span><span>${resource.process_count ? bytes(resource.rss_bytes) : "无本地进程"}</span></div></td><td><div class="row-actions">${taskButtons(t, true)}<a class="detail-button" href="/tasks/${t.id}">工作详情 →</a></div></td></tr>`;
    })
    .join("");
  $$("#task-rows [data-width]").forEach(
    (el) => (el.style.width = `${el.dataset.width}%`),
  );
  $("#empty-state").hidden = tasks.length > 0;
  const noTasks = state.tasks.length === 0;
  $("#empty-title").textContent = noTasks
    ? "下一个交付，从这里开始"
    : "没有匹配的任务";
  $("#empty-description").textContent = noTasks
    ? "添加一个开发任务，让执行器开始实现，让监督器持续跟进。"
    : "尝试调整状态筛选或搜索关键词。";
  $("#empty-create").hidden = !noTasks;
  $("#table-caption").textContent =
    `显示 ${page.total?page.start+1:0}–${page.end} 项，筛选 ${tasks.length} 项 / 全部 ${state.tasks.length} 个任务`;
}

export function mount() {
  onPaginationChange(key=>{if(key==='tasks')renderTasks();});
  $('#refresh').addEventListener('click',refresh);
  if(!['all','active','paused','stopped','queued','done','attention'].includes(state.filter))state.filter='all';
  $('#search').value=state.search;
  $$('.filter').forEach(b=>b.classList.toggle('active',b.dataset.filter===state.filter));
  $('#search').addEventListener('input',e=>{state.search=e.target.value;resetPage('tasks');renderTasks();});
  $('#filters').addEventListener('click',e=>{const el=e.target.closest('[data-filter]');if(!el)return;state.filter=el.dataset.filter;resetPage('tasks');renderTasks();$$('.filter').forEach(b=>b.classList.toggle('active',b.dataset.filter===state.filter));});
  watch(async()=>{const data=await api('/api/tasks');state.tasks=data.tasks;$('#task-count').textContent=data.tasks.length;$('#filter-active').textContent=data.tasks.filter(t=>['running','starting'].includes(t.status)).length;renderTasks();$('#last-updated').textContent=`更新于 ${new Date(data.at).toLocaleTimeString('zh-CN',{hour12:false})}`;});
}
