import {i18n,locale} from './i18n.js';
export const $ = selector => document.querySelector(selector);
export const $$ = selector => [...document.querySelectorAll(selector)];
export const pending = new Set();
const statuses = {
  queued: i18n("待启动"),
  starting: i18n("启动中"),
  running: i18n("运行中"),
  paused: i18n("已暂停"),
  done: i18n("已交付"),
  stopped: i18n("已停止"),
  cancelled: i18n("已取消"),
  interrupted: i18n("执行中断"),
  failed: i18n("执行失败"),
  blocked: i18n("受阻"),
  budget_exceeded: i18n("预算已用尽"),
  action_required: i18n("需要处理"),
  recovery_failed: i18n("恢复失败"),
  max_rounds: i18n("已达轮次上限"),
  stalled: i18n("进度停滞"),
  invalid: i18n("配置无效"),
  locked: i18n("工作区锁定"),
  unknown: i18n("状态未知"),
};
const phases = {
  preparing: i18n("准备环境"),
  working: i18n("实现需求中"),
  reviewing: i18n("独立审查中"),
  refining_requirements: i18n("reviewer 完善需求中"),
  validating: i18n("验收检查中"),
  publishing: i18n("提交已审核代码中"),
  retry_wait: i18n("等待重试"),
  closing_agents: i18n("结束会话中"),
  paused: i18n("已在轮次边界暂停"),
  recovering: i18n("正在恢复"),
  waiting_for_input: i18n("等待输入"),
  heartbeat_stale: i18n("心跳超时"),
  resuming: i18n("恢复执行中"),
};
const actionLabels = {
  start: i18n("启动任务"),
  cancel: i18n("取消任务"),
  pause: i18n("暂停执行"),
  continue: i18n("恢复执行"),
  stop: i18n("停止任务"),
  resume: i18n("恢复执行"),
  roles: i18n("临时调整角色"),
  checks: i18n("补充验收并恢复"),
  delete: i18n("删除任务"),
};
const active = (t) => ["running", "starting", "paused"].includes(t.status);
const attention = (t) =>
  !["queued", "running", "starting", "done", "cancelled"].includes(t.status);
const taskPriority = (task) =>
  ({
    running: 0,
    starting: 1,
    paused: 2,
    action_required: 3,
    interrupted: 3,
    failed: 3,
    unknown: 3,
    blocked: 3,
    queued: 4,
    stopped: 5,
    done: 6,
    cancelled: 7,
  })[task.status] ?? 4;
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const bytes = (n) => {
  if (!Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  const e = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** e).toFixed(e > 1 ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][e]}`;
};
const percent = (n) => (Number.isFinite(n) ? `${n.toFixed(1)}%` : i18n("采样中"));
const duration = (seconds) => {
  if (!Number.isFinite(seconds)) return "—";
  const n = Math.max(0, Math.floor(seconds));
  if (n < 60) return `${n}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m ${n % 60}s`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
};
const badge = (t) => {
  const label =
    t.control === "pause" && t.status === "running"
      ? i18n("暂停中")
      : t.control === "stop" && active(t)
        ? i18n("停止中")
        : active(t)&&taskMotion(t)==='attention'?i18n('状态待确认'):statuses[t.status] || t.status;
  return `<span class="status ${escape(t.status)}" data-motion="${taskMotion(t)}">${stateGlyph()}${escape(label)}</span>`;
};
function taskButtons(task, compact = false) {
  return task.actions
    .filter(
      (action) =>
        !compact || ["start", "pause", "continue", "resume", "delete", "roles"].includes(action),
    )
    .map((action) => {
      const cancellingPause =
        action === "continue" &&
        task.status === "running" &&
        task.control === "pause";
      const title =
        action === "pause"
          ? i18n("当前轮次结束后暂停，保留任务上下文")
          : action === "resume"
            ? i18n("从检查点恢复，保留原模型、审核、隔离与预算配置")
            : "";
      const label = cancellingPause ? i18n("取消暂停") : actionLabels[action];
      return `<button class="button ${compact ? "small-button" : ""} ${["stop","delete"].includes(action) ? "danger" : ["start", "resume", "continue"].includes(action) ? "primary" : "secondary"}" data-action="${action}" data-id="${task.id}" title="${title}" ${pending.has(task.id) ? "disabled" : ""}>${escape(label)}</button>`;
    })
    .join("");
}

const roleStatuses = {monitoring:i18n("监督中"), running:i18n("执行中"), waiting:i18n("等待执行"), paused:i18n("已暂停"), stopped:i18n("已结束"), completed:i18n("已完成"), disabled:i18n("未启用"), not_started:i18n("未启动")};

// Presentation follows observed states. Silence alone never implies a failure.
function motionState(status) {
  if(['running','starting','monitoring'].includes(status))return 'running';
  if(['done','completed','accepted','succeeded','passed'].includes(status))return 'complete';
  if(status==='paused')return 'paused';
  if(['queued','waiting','not_started','pending'].includes(status))return 'waiting';
  if(['failed','invalid','recovery_failed','check_failed'].includes(status))return 'error';
  if(['blocked','action_required','budget_exceeded','max_rounds','stalled','locked','interrupted','unknown','heartbeat_stale'].includes(status))return 'attention';
  return 'idle';
}
function taskMotion(t) {
  const state=motionState(t.status);
  if(state!=='running')return state;
  if(t.health?.health==='heartbeat_stale'||t.health?.phase==='heartbeat_stale')return 'attention';
  if(t.control==='pause'||t.control==='stop')return 'waiting';
  if(['retry_wait','waiting_for_input'].includes(t.health?.phase))return 'waiting';
  return state;
}
function activityState(t,item,{plan=false}={}) {
  if(item.status==='failed')return 'error';
  if(item.status==='completed')return plan||item.exit_code===0?'complete':Number.isInteger(item.exit_code)?'error':'idle';
  if(!['running','in_progress'].includes(item.status))return 'idle';
  if(t.status==='paused')return 'paused';
  if(taskMotion(t)!=='running'||!t.activity?.is_running)return 'idle';
  if(plan)return 'running';
  const operation=t.activity.operation;
  const sameOperation=operation&&item.id&&operation.id===item.id&&operation.role===item.role&&String(operation.round)===String(item.round);
  return sameOperation?(operation.status==='completed'?activityState(t,operation):motionState(operation.status)):'idle';
}
function stateGlyph(){return '<span class="state-glyph" aria-hidden="true"></span>';}
function stateLabel(state,label){return `<span class="component-state" data-motion="${escape(state)}">${stateGlyph()}${escape(label)}</span>`;}
const roleName=name=>({worker:i18n('执行器'),reviewer:i18n('审查器'),watcher:i18n('监督器')})[name]||name;
function rolePresentation(t,name,r={}) {
  const taskState=taskMotion(t);
  const state=taskState==='running'?motionState(r.status):taskState;
  let label=i18n(roleStatuses[r.status]||'状态未知');
  if(r.status==='disabled')return {state:'idle',label:i18n('未启用')};
  if(t.status==='done')label=i18n('已结束');
  else if(t.status==='paused')label=i18n('已暂停');
  else if(!active(t))label=i18n('当前未执行');
  else if(taskState==='attention')label=i18n('连接待确认');
  else if(r.status==='waiting')label=name==='reviewer'?i18n('等待执行结果'):name==='worker'?i18n('等待派单'):i18n('等待调度');
  return {state,label};
}
function rolesHTML(t,roles=t.work?.roles||t.roles||{}) {
  return Object.entries(roles).map(([name,r])=>{const p=rolePresentation(t,name,r);return stateLabel(p.state,`${roleName(name)} · ${p.label}`);}).join('');
}
function taskNarrative(t) {
  const state=taskMotion(t);
  const terminal={
    queued:[i18n('任务已保存，等待启动'),i18n('启动后先核对需求与运行环境。')],
    paused:[i18n('任务已暂停，进度已保留'),i18n('恢复后从当前检查点继续。')],
    done:[i18n('交付已确认'),i18n('可查看验收结果与本地提交记录。')],
    stopped:[i18n('任务已停止'),i18n('查看停止原因，再决定是否恢复。')],
    cancelled:[i18n('任务已取消'),i18n('需要继续时可创建后续任务。')],
  };
  let words=terminal[t.status];
  if(!words&&!active(t))words=[statuses[t.status]||i18n('状态未知'),i18n('查看处理建议，解决原因后再恢复。')];
  if(!words&&state==='attention')words=[i18n('暂时无法确认执行状态'),i18n('监督器心跳已超时，请查看运行记录。')];
  if(!words&&t.control==='stop')words=[i18n('正在停止并保存进度'),i18n('等待进程退出与检查点保存。')];
  if(!words&&t.control==='pause')words=[i18n('已请求暂停，等待本轮结束'),i18n('本轮结束后保留上下文并暂停。')];
  const phasesText={
    refining_requirements:[i18n('审查器正在梳理需求'),i18n('明确范围和验收标准后，再交给执行器。')],
    working:[i18n('执行器正在实现需求'),i18n('提交候选后，监督器运行检查，再交独立审核。')],
    reviewing:[i18n('审查器正在核对候选'),i18n('结合代码与检查结果给出结论，再决定提交或修复。')],
    validating:[i18n('监督器正在运行验收检查'),i18n('检查结果将连同候选代码交给审查器。')],
    publishing:[i18n('监督器正在保存已审核代码'),i18n('本地提交完成后，继续下一派单或确认交付。')],
    retry_wait:[i18n('正在等待重试'),i18n('等待期结束后，在原预算内重试。')],
    waiting_for_input:[i18n('正在等待必要输入'),i18n('查看所需信息，补充后再继续。')],
    closing_agents:[i18n('正在结束会话并整理记录'),i18n('最终交付状态以监督器确认结果为准。')],
    recovering:[i18n('正在恢复已保存的进度'),i18n('核对检查点与当前代码后继续。')],
    resuming:[i18n('正在恢复已保存的进度'),i18n('核对检查点与当前代码后继续。')],
  };
  words=words||phasesText[t.health?.phase]||[i18n('正在准备任务'),i18n('核对环境与任务配置后开始执行。')];
  return {state,title:words[0],next:words[1]};
}
function taskNarrativeHTML(t){
  const n=taskNarrative(t);
  return `<section class="task-narrative" data-motion="${n.state}" aria-label="${escape(i18n('当前阶段与下一步'))}"><div class="narrative-icon">${stateGlyph()}</div><div><strong>${escape(n.title)}</strong><p>${escape(n.next)}</p></div></section>`;
}

function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString(locale());
}

function dateTimeLabel(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(locale());
}

function stableHTML(el, html) {
  if (el.dataset.html === html) return;
  // Polling updates heartbeat text without restarting the role illustrations.
  const animationKey=animation=>{
    const target=animation.effect?.target,card=target?.closest?.('.role-card');
    return card?`${card.dataset.role}:${card.dataset.motion}:${animation.animationName}:${target.getAttribute('class')}`:null;
  };
  const times=new Map((el.getAnimations?.({subtree:true})||[]).map(animation=>[animationKey(animation),animation.currentTime]));
  const opened = [...el.querySelectorAll("details[open]")].map(d => d.dataset.key || d.querySelector("summary")?.textContent);
  const scroll = el.scrollTop;
  el.innerHTML = html;
  el.dataset.html = html;
  for(const animation of el.getAnimations?.({subtree:true})||[]){const key=animationKey(animation);if(key&&times.has(key))animation.currentTime=times.get(key);}
  el.querySelectorAll("details").forEach(d => { if(opened.includes(d.dataset.key || d.querySelector("summary")?.textContent)) d.open = true; });
  el.scrollTop = scroll;
}

export {statuses,phases,actionLabels,active,attention,taskPriority,escape,icon,bytes,percent,duration,badge,taskButtons,roleStatuses,timeLabel,dateTimeLabel,stableHTML,motionState,taskMotion,stateGlyph,stateLabel,roleName,rolePresentation,rolesHTML,taskNarrative,taskNarrativeHTML,activityState};
