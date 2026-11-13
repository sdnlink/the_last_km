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
  const opened = [...el.querySelectorAll("details[open]")].map(d => d.dataset.key || d.querySelector("summary")?.textContent);
  const scroll = el.scrollTop;
  el.innerHTML = html;
  el.dataset.html = html;
  el.querySelectorAll("details").forEach(d => { if(opened.includes(d.dataset.key || d.querySelector("summary")?.textContent)) d.open = true; });
  el.scrollTop = scroll;
}

export {statuses,phases,actionLabels,active,attention,taskPriority,escape,icon,bytes,percent,duration,badge,taskButtons,roleStatuses,timeLabel,dateTimeLabel,stableHTML};
