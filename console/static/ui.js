export const $ = selector => document.querySelector(selector);
export const $$ = selector => [...document.querySelectorAll(selector)];
export const pending = new Set();
const statuses = {
  queued: "待启动",
  starting: "启动中",
  running: "运行中",
  paused: "已暂停",
  done: "已交付",
  stopped: "已停止",
  cancelled: "已取消",
  interrupted: "执行中断",
  failed: "执行失败",
  blocked: "受阻",
  budget_exceeded: "预算已用尽",
  action_required: "需要处理",
  recovery_failed: "恢复失败",
  max_rounds: "已达轮次上限",
  stalled: "进度停滞",
  invalid: "配置无效",
  locked: "工作区锁定",
  unknown: "状态未知",
};
const phases = {
  preparing: "准备环境",
  working: "实现需求中",
  reviewing: "独立审查中",
  refining_requirements: "reviewer 完善需求中",
  validating: "验收检查中",
  retry_wait: "等待重试",
  closing_agents: "结束会话中",
  paused: "已在轮次边界暂停",
  recovering: "正在恢复",
  waiting_for_input: "等待输入",
  heartbeat_stale: "心跳超时",
  resuming: "恢复执行中",
};
const actionLabels = {
  start: "启动任务",
  cancel: "取消任务",
  pause: "暂停执行",
  continue: "恢复执行",
  stop: "停止任务",
  resume: "恢复执行",
  roles: "临时调整角色",
  checks: "补充验收并恢复",
  delete: "删除任务",
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
const percent = (n) => (Number.isFinite(n) ? `${n.toFixed(1)}%` : "采样中");
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
      ? "暂停中"
      : t.control === "stop" && active(t)
        ? "停止中"
        : statuses[t.status] || t.status;
  return `<span class="status ${escape(t.status)}">${escape(label)}</span>`;
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
          ? "当前轮次结束后暂停，保留任务上下文"
          : action === "resume"
            ? "从检查点恢复，保留原模型、审核、隔离与预算配置"
            : "";
      const label = cancellingPause ? "取消暂停" : actionLabels[action];
      return `<button class="button ${compact ? "small-button" : ""} ${["stop","delete"].includes(action) ? "danger" : ["start", "resume", "continue"].includes(action) ? "primary" : "secondary"}" data-action="${action}" data-id="${task.id}" title="${title}" ${pending.has(task.id) ? "disabled" : ""}>${escape(label)}</button>`;
    })
    .join("");
}

const roleStatuses = {monitoring:"监督中", running:"执行中", waiting:"等待执行", paused:"已暂停", stopped:"已结束", completed:"已完成", disabled:"未启用", not_started:"未启动"};

function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function stableHTML(el, html) {
  if (el.dataset.html === html) return;
  const opened = [...el.querySelectorAll("details[open]")].map(d => d.dataset.key || d.querySelector("summary")?.textContent);
  const scroll = el.scrollTop;
  el.innerHTML = html;
  el.dataset.html = html;
  el.querySelectorAll("details").forEach(d => { if(opened.includes(d.dataset.key || d.querySelector("summary")?.textContent)) d.open = true; });
  el.scrollTop = scroll;
}

export {statuses,phases,actionLabels,active,attention,taskPriority,escape,icon,bytes,percent,duration,badge,taskButtons,roleStatuses,timeLabel,stableHTML};
