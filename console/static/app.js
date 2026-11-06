"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  config: null,
  tasks: [],
  resources: null,
  page: "overview",
  filter: "all",
  search: "",
  detail: null,
  log: "",
  pending: new Set(),
  history: [],
  fetching: false,
  detailBusy: false,
  lastSample: 0,
  liveTask: null,
};
const statuses = {
  queued: "待启动",
  starting: "启动中",
  running: "执行中",
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
  validating: "验收检查中",
  retry_wait: "等待重试",
  closing_agents: "结束会话中",
  paused: "已在轮次边界暂停",
  recovering: "正在恢复",
  waiting_for_input: "等待输入",
  heartbeat_stale: "心跳超时",
};
const actionLabels = {
  start: "启动任务",
  cancel: "取消任务",
  pause: "暂停",
  continue: "继续执行",
  stop: "停止任务",
  resume: "从检查点恢复",
};
const active = (t) => ["running", "starting", "paused"].includes(t.status);
const attention = (t) =>
  !["queued", "running", "starting", "done", "cancelled"].includes(t.status);
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
const badge = (t) =>
  `<span class="status ${escape(t.status)}">${escape(statuses[t.status] || t.status)}</span>`;
let toastTimer;
function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5000);
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    cache: "no-store",
    headers: body
      ? {
          "Content-Type": "application/json",
          "X-Console-Token": state.config?.csrf_token || "",
        }
      : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15000),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`服务返回异常（${response.status}）`);
  }
  if (!response.ok) {
    const error = new Error(data.error || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function switchPage(page) {
  state.page = page;
  const names = {
    overview: ["工作概览", "从一个想法，到一次交付。在这里掌握每一步进展。"],
    tasks: ["开发任务", "让每一份需求都有进展，让每一次交付有据可查。"],
    resources: ["资源监控", "实时查看运行环境，了解任务的每一份资源投入。"],
    progress: [
      "实时进展",
      "了解 AI 正在做什么，跟踪每一次实现、检查与独立审查。",
    ],
  };
  $("#breadcrumb").textContent = names[page][0];
  $("#page-title").innerHTML =
    `${names[page][0]}<span class="heading-dot">.</span>`;
  $("#page-subtitle").textContent = names[page][1];
  $$(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
    el.setAttribute(
      "aria-current",
      el.dataset.page === page ? "page" : "false",
    );
  });
  $("#stats").hidden = ["resources", "progress"].includes(page);
  $("#tasks-panel").hidden = ["resources", "progress"].includes(page);
  $("#resource-strip").hidden = page === "progress";
  $("#live-panel").hidden = page === "resources";
  $("#live-panel").classList.toggle("expanded", page === "progress");
  $("#resources-panel").hidden = page !== "resources";
  if (page === "resources") renderResources();
  else renderLive();
}
function render() {
  const tasks = state.tasks;
  const counts = {
    total: tasks.length,
    running: tasks.filter((t) => ["running", "starting"].includes(t.status))
      .length,
    done: tasks.filter((t) => t.status === "done").length,
    waiting: tasks.filter((t) => t.status === "queued" || attention(t)).length,
  };
  for (const key of ["total", "done", "waiting"])
    $(`#stat-${key}`).textContent = counts[key];
  $("#stat-running").innerHTML =
    `${counts.running}<span class="stat-unit">个任务</span>`;
  $("#nav-count").textContent = tasks.length;
  $("#task-count").textContent = tasks.length;
  $("#filter-active").textContent = tasks.filter(active).length;
  renderTasks();
  renderResources();
  renderLive();
}

function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}
function activityHTML(task, detail = false) {
  const activity = task.activity || {};
  const role =
    activity.role === "check"
      ? "验收检查"
      : activity.role === "reviewer"
        ? "独立审查器"
        : "执行器";
  const phase =
    phases[task.health.phase] || statuses[task.status] || "等待执行";
  const age = activity.output_age_s;
  const latest = activity.last_output_at
    ? `最近输出 ${duration(age)} 前`
    : "尚未收到本轮输出";
  const focus =
    activity.focus ||
    (activity.operation
      ? activity.operation.text
      : task.status === "queued"
        ? "任务已保存，启动后显示执行过程。"
        : "等待 AI 发出进度说明或工具操作。监督器仍会持续检查执行状态。");
  const labels = {
    message: "进度说明",
    command: "执行命令",
    file: "文件变更",
    tool: "工具操作",
    result: "工具结果",
    plan: "执行计划",
    error: "执行提示",
    output: "实时输出",
    phase: "监督事件",
  };
  const timeline = (activity.events || [])
    .slice(detail || state.page === "progress" ? -40 : -12)
    .reverse();
  const plan = activity.plan || [];
  const openIssues = (task.audit_issues?.items || []).filter(
    (i) => i.status !== "closed",
  );
  return `<div class="live-summary"><div class="live-status-row">${badge(task)}<strong>${escape(role)} · 第 ${activity.round || 0} 轮</strong><span>${escape(phase)}</span><span class="live-age">${escape(latest)}</span></div><div class="live-focus"><span class="live-label">${activity.is_running ? "当前工作" : "最近工作"}</span><p>${escape(focus)}</p></div>${activity.operation && activity.focus ? `<div class="live-operation"><span>${escape(labels[activity.operation.kind] || "当前操作")}</span><code>${escape(activity.operation.text)}</code></div>` : ""}<div class="live-facts"><span>模块验收 <strong>${task.accepted_modules} / ${task.total_modules}</strong></span><span>待解决审查问题 <strong>${openIssues.length}</strong></span><span>模型调用 <strong>${task.health.usage?.agent_calls || 0}</strong></span><span>心跳 <strong>${task.health.heartbeat_age_s == null ? "—" : `${duration(task.health.heartbeat_age_s)} 前`}</strong></span></div>${plan.length ? `<ol class="live-plan">${plan.map((p) => `<li class="${p.status === "completed" ? "completed" : p.status === "in_progress" ? "in-progress" : "pending"}"><span>${p.status === "completed" ? "✓" : p.status === "in_progress" ? "●" : "○"}</span>${escape(p.step)}</li>`).join("")}</ol>` : ""}${task.last_review?.summary ? `<div class="live-review"><strong>最近审查反馈</strong><p>${escape(task.last_review.summary)}</p></div>` : ""}</div><div class="live-timeline-wrap"><div class="live-timeline-heading"><h3>最近活动</h3><span>${activity.source === "codex_session" ? (activity.is_running ? "已连接当前 AI 会话" : "已读取最近 AI 会话") : activity.source === "live" ? "执行输出实时更新" : "监督事件更新中"}</span></div>${timeline.length ? `<ol class="live-timeline">${timeline.map((e) => `<li class="event-${escape(e.kind)}"><div class="live-event-meta"><time>${escape(timeLabel(e.at))}</time><span>${escape(labels[e.kind] || "活动")}</span>${e.round ? `<small>第 ${escape(e.round)} 轮${e.role === "reviewer" ? " · 审查" : ""}</small>` : ""}</div><details class="live-event" ${e.kind === "message" || e.kind === "phase" ? "open" : ""}><summary>${escape(e.text.split("\n")[0].slice(0, 180))}</summary><pre>${escape(e.text)}${e.output ? `\n\n${escape(e.output)}` : ""}</pre></details></li>`).join("")}</ol>` : `<div class="live-empty">${task.status === "queued" ? "启动任务后，这里将持续显示执行器的进度说明和工具操作。" : "本轮尚未发布可显示的输出；有新消息或工具结果时会自动更新。"}</div>`}</div>`;
}
function updateActivity(container, task, detail = false) {
  const opened = [...container.querySelectorAll(".live-event[open]")].map(
    (el) => el.querySelector("summary").textContent,
  );
  const timeline = container.querySelector(".live-timeline");
  const scroll = timeline?.scrollTop || 0;
  const html = activityHTML(task, detail);
  if (container.dataset.html === html) return;
  container.innerHTML = html;
  container.dataset.html = html;
  container.querySelectorAll(".live-event").forEach((el) => {
    if (opened.includes(el.querySelector("summary").textContent))
      el.open = true;
  });
  if (container.querySelector(".live-timeline"))
    container.querySelector(".live-timeline").scrollTop = scroll;
}
function renderLive() {
  const tasks = [...state.tasks].sort(
    (a, b) => Number(active(b)) - Number(active(a)),
  );
  if (!tasks.some((t) => t.id === state.liveTask))
    state.liveTask = tasks[0]?.id || null;
  const picker = $("#live-task-select");
  const options = tasks
    .map(
      (t) =>
        `<option value="${t.id}">${escape(t.title.slice(0, 60))} · ${escape(statuses[t.status] || t.status)}</option>`,
    )
    .join("");
  if (picker.dataset.options !== options) {
    picker.innerHTML = options;
    picker.dataset.options = options;
  }
  picker.value = state.liveTask || "";
  picker.disabled = !tasks.length;
  const task = tasks.find((t) => t.id === state.liveTask);
  if (task) updateActivity($("#live-content"), task);
  else
    $("#live-content").innerHTML =
      '<p class="live-empty">还没有开发任务。创建并启动任务后，即可查看实时项目进展。</p>';
}
function renderTasks() {
  const search = state.search.toLowerCase();
  const tasks = state.tasks
    .filter(
      (t) =>
        ({
          all: true,
          active: active(t),
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
  $("#task-rows").innerHTML = tasks
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
      const rowAction = t.actions.includes("start")
        ? "start"
        : t.actions.includes("continue")
          ? "continue"
          : null;
      return `<tr><td><div class="task-name"><span class="task-avatar ${["", "blue", "purple"][index % 3]}">${icon("terminal")}</span><div><button class="task-title" data-detail="${t.id}" title="${escape(t.title)}">${escape(t.title)}</button><div class="task-path" title="${escape(t.cwd)}">${icon("folder")}<span>${escape(t.cwd || "目录未知")}</span></div></div></div></td><td>${badge(t)}${active(t) && phase ? `<small class="phase">${escape(phase)}</small>` : ""}</td><td><span class="provider-name">${escape(t.provider)}</span><span class="reviewer-name">审查 · ${escape(t.reviewer)}</span></td><td><div class="module-progress"><div>${t.total_modules ? `${t.accepted_modules}<small>/ ${t.total_modules} 模块</small>` : `<small>${t.status === "done" ? "已验收" : "等待模块计划"}</small>`}</div><div class="meter"><span data-width="${progress}"></span></div></div></td><td class="duration">${duration(t.health.usage?.elapsed_s)}</td><td><div class="resource-cell"><span>${resource.process_count ? `CPU ${percent(resource.cpu_percent)}` : "—"}</span><span>${resource.process_count ? bytes(resource.rss_bytes) : "无本地进程"}</span></div></td><td><div class="row-actions">${rowAction ? `<button class="row-start" data-action="${rowAction}" data-id="${t.id}" ${state.pending.has(t.id) ? "disabled" : ""}>${rowAction === "start" ? "启动" : "继续"}</button>` : ""}<button class="detail-button" data-detail="${t.id}">详情 ↗</button></div></td></tr>`;
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
    `显示 ${tasks.length} 项，共 ${state.tasks.length} 个任务`;
}
function renderResources() {
  const r = state.resources;
  if (!r) return;
  const memory = (r.memory_used / r.memory_total) * 100,
    disk = (r.disk_used / r.disk_total) * 100;
  $("#cpu-value").textContent = percent(r.cpu_percent);
  $("#memory-value").textContent = percent(memory);
  $("#disk-value").textContent = percent(disk);
  $("#cpu-meter").style.width = `${Math.min(100, r.cpu_percent || 0)}%`;
  $("#memory-meter").style.width = `${memory}%`;
  $("#disk-meter").style.width = `${disk}%`;
  $("#cpu-description").textContent = `${r.cpu_count} 核心 · 本机`;
  $("#memory-description").textContent =
    `${bytes(r.memory_used)} / ${bytes(r.memory_total)}`;
  $("#disk-description").textContent = `${bytes(r.disk_free)} 可用`;
  $("#chart-cpu-value").textContent = percent(r.cpu_percent);
  $("#chart-memory-value").textContent = percent(memory);
  drawChart(
    $("#cpu-chart"),
    state.history.map((s) => s.cpu),
  );
  drawChart(
    $("#memory-chart"),
    state.history.map((s) => s.memory),
  );
  const taskMemory = state.tasks.reduce(
    (sum, t) => sum + t.resources.rss_bytes,
    0,
  );
  const detail = [
    ["CPU 核心数", `${r.cpu_count} 核`],
    [
      "1 / 5 / 15 分钟负载",
      r.load_average.map((n) => n.toFixed(2)).join(" / "),
    ],
    ["任务本地进程内存", bytes(taskMemory)],
    [
      "任务本地进程数",
      state.tasks.reduce((n, t) => n + t.resources.process_count, 0),
    ],
  ];
  $("#resource-details").innerHTML = detail
    .map(
      ([label, value]) =>
        `<div><small>${label}</small><strong>${escape(value)}</strong></div>`,
    )
    .join("");
}
function drawChart(el, values) {
  const valid = values
    .map((v, i) => (Number.isFinite(v) ? [i, v] : null))
    .filter(Boolean);
  let html = [0, 25, 50, 75, 100]
    .map((n) => `<path class="grid-line" d="M0 ${155 - n * 1.5}H600"/>`)
    .join("");
  if (valid.length) {
    const points = valid.map(([i, v]) => [
      values.length > 1 ? (i / (values.length - 1)) * 600 : 0,
      155 - Math.max(0, Math.min(100, v)) * 1.5,
    ]);
    const d = points
      .map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`)
      .join(" ");
    html += `<path class="area" d="${d} L${points.at(-1)[0]} 155 L${points[0][0]} 155Z"/><path class="trend" d="${d}"/>`;
  }
  el.innerHTML = html;
}
async function refresh() {
  if (state.fetching) return;
  state.fetching = true;
  try {
    if (!state.config) {
      state.config = await api("/api/config");
      configureForm();
    }
    const data = await api("/api/overview");
    state.tasks = data.tasks;
    state.resources = data.resources;
    if (Date.now() - state.lastSample > 2000) {
      state.history.push({
        cpu: data.resources.cpu_percent,
        memory:
          (data.resources.memory_used / data.resources.memory_total) * 100,
      });
      state.history = state.history.slice(-60);
      state.lastSample = Date.now();
    }
    render();
    $("#connection").innerHTML = '<span class="live-dot"></span>本地服务在线';
    $("#connection").classList.remove("offline");
    $("#connection-error").hidden = true;
    $("#last-updated").textContent =
      `更新于 ${new Date(data.at).toLocaleTimeString("zh-CN", { hour12: false })}`;
    if (state.detail && $("#detail-dialog").open) await refreshDetail();
  } catch (error) {
    $("#connection").innerHTML = '<span class="live-dot"></span>连接已断开';
    $("#connection").classList.add("offline");
    $("#connection-error").textContent =
      `无法同步数据：${error.message}。正在自动重连…`;
    $("#connection-error").hidden = false;
  } finally {
    state.fetching = false;
  }
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
function openCreate() {
  if (!state.config) {
    toast("正在连接服务，请稍后重试", true);
    return;
  }
  $("#form-error").hidden = true;
  $("#create-dialog").showModal();
}
function feedback(title, value) {
  if (!value) return "";
  const items = [
    value.summary,
    ...(value.gaps || []).map((x) => `待完善：${x}`),
    ...(value.next_actions || []).map((x) => `下一步：${x}`),
  ].filter(Boolean);
  return `<div class="detail-feedback"><strong>${title}</strong>${escape(items.join("\n"))}</div>`;
}
function renderDetail(t) {
  updateActivity($("#detail-live"), t, true);
  $("#detail-title").textContent = t.title;
  $("#detail-project").textContent = t.cwd;
  const alerts = [];
  if (t.control === "pause" && t.status !== "paused")
    alerts.push("已请求暂停，将在当前轮次结束后生效。");
  if (t.control === "stop" && active(t))
    alerts.push("正在停止执行并保存状态，请等待监督器确认。");
  if (t.status === "interrupted")
    alerts.push("监督器已退出，尚无确认的最终报告。可以查看日志定位原因。");
  if (t.status === "failed")
    alerts.push("任务未成功启动或执行失败，请查看日志检查安装、登录和配置。");
  if (t.health.report_warning)
    alerts.push(`运行报告需要核对：${t.health.report_warning}`);
  if (t.health.health === "heartbeat_stale")
    alerts.push("监督器仍持有运行锁，但心跳已超时，请查看执行日志。");
  const modules = t.modules
    .map(
      (m) =>
        `<li><span class="module-id">${escape(m.id)}</span><span>${escape(m.title)}</span><span>${m.status === "accepted" ? "已验收" : "待完成"}</span></li>`,
    )
    .join("");
  const issues = t.audit_issues?.items || [];
  $("#detail-body").innerHTML =
    `${alerts.map((a) => `<div class="detail-alert">${escape(a)}</div>`).join("")}<div class="detail-summary">${badge(t)}<div class="detail-stats"><span>轮次<strong>${t.health.rounds || 0}</strong></span><span>模型调用<strong>${t.health.usage?.agent_calls || 0}</strong></span><span>运行<strong>${duration(t.health.usage?.elapsed_s)}</strong></span><span>CPU<strong>${t.resources.process_count ? percent(t.resources.cpu_percent) : "—"}</strong></span><span>内存<strong>${t.resources.process_count ? bytes(t.resources.rss_bytes) : "—"}</strong></span></div></div><h3>原始开发需求</h3><div class="requirement-text">${escape(t.requirement)}</div>${t.summary ? `<h3>最新进展</h3><div class="requirement-text">${escape(t.summary)}</div>` : ""}${modules ? `<h3>模块验收 · ${t.accepted_modules} / ${t.total_modules}</h3><ul class="module-list">${modules}</ul>` : ""}${feedback("执行器反馈", t.last_worker)}${feedback("独立审查反馈", t.last_review)}${issues.length ? `<h3>审计问题</h3><ul class="module-list">${issues.map((i) => `<li><span class="module-id">${escape(i.id)}</span><span>${escape(i.description || i.text || i.summary || "")}</span><span>${i.status === "closed" ? "已关闭" : "待解决"}</span></li>`).join("")}</ul>` : ""}<h3>运行记录目录</h3><div class="requirement-text">${escape(t.run_dir)}</div>`;
  $("#detail-actions").innerHTML =
    `${!t.managed ? '<span class="muted">已发现的 CLI 任务</span>' : ""}${t.actions.map((a) => `<button class="button ${a === "stop" ? "danger" : a === "start" || a === "resume" || a === "continue" ? "primary" : "secondary"}" data-action="${a}" data-id="${t.id}" ${state.pending.has(t.id) ? "disabled" : ""}>${actionLabels[a]}</button>`).join("")}`;
}
async function openDetail(ident) {
  state.detail = ident;
  state.log = "";
  $("#detail-title").textContent = "加载任务…";
  $("#detail-project").textContent = "";
  $("#detail-body").replaceChildren();
  $("#detail-live").replaceChildren();
  delete $("#detail-live").dataset.html;
  $("#detail-actions").replaceChildren();
  $("#log-output").textContent = "加载中…";
  $("#log-select").replaceChildren();
  $("#detail-dialog").showModal();
  try {
    await refreshDetail();
  } catch (error) {
    toast(error.message, true);
  }
}
async function refreshDetail() {
  if (state.detailBusy) return;
  state.detailBusy = true;
  const ident = state.detail;
  try {
    const t = await api(`/api/tasks/${ident}`);
    if (state.detail !== ident) return;
    renderDetail(t);
    await loadLogs(ident);
  } finally {
    state.detailBusy = false;
  }
}
async function loadLogs(ident) {
  const selected = state.log;
  let data;
  try {
    data = await api(
      `/api/tasks/${ident}/logs${selected ? `?file=${encodeURIComponent(selected)}` : ""}`,
    );
  } catch (error) {
    // Retention can remove a selected round while the detail panel remains open.
    if (
      selected &&
      error.status === 404 &&
      state.detail === ident &&
      state.log === selected
    ) {
      state.log = "";
      return loadLogs(ident);
    }
    throw error;
  }
  if (state.detail !== ident || state.log !== selected) return;
  const el = $("#log-select");
  el.innerHTML = data.files.length
    ? data.files
        .map((n) => `<option value="${escape(n)}">${escape(n)}</option>`)
        .join("")
    : "<option>暂无日志</option>";
  el.value = data.selected || el.options[0]?.value;
  const output = $("#log-output");
  if (output.textContent !== data.content)
    output.textContent =
      data.content || "暂无执行日志。启动任务后，输出将在这里显示。";
  if ($("#log-follow").checked) output.scrollTop = output.scrollHeight;
}
let stopTarget = null;
async function act(ident, action) {
  if (state.pending.has(ident)) return;
  if (action === "stop") {
    stopTarget = ident;
    $("#confirm-dialog").showModal();
    return;
  }
  state.pending.add(ident);
  renderTasks();
  if (state.detail === ident) {
    const t = state.tasks.find((t) => t.id === ident);
    if (t) renderDetail(t);
  }
  try {
    await api(`/api/tasks/${ident}/${action}`, {});
    toast(
      {
        start: "任务已启动",
        resume: "正在从检查点恢复",
        cancel: "任务已取消",
        pause: "已请求暂停，将在本轮结束后生效",
        continue: "任务已继续",
        stopNow: "已请求停止",
      }[action] || "操作已提交",
    );
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.pending.delete(ident);
    renderTasks();
    if (state.detail === ident && $("#detail-dialog").open)
      await refreshDetail().catch((e) => toast(e.message, true));
  }
}
document.addEventListener("click", (event) => {
  const page = event.target.closest("[data-page]");
  if (page) switchPage(page.dataset.page);
  const close = event.target.closest("[data-close]");
  if (close) $(`#${close.dataset.close}`).close();
  const detail = event.target.closest("[data-detail]");
  if (detail) openDetail(detail.dataset.detail);
  const action = event.target.closest("[data-action]");
  if (action) act(action.dataset.id, action.dataset.action);
});
$("#new-task").addEventListener("click", openCreate);
$("#empty-create").addEventListener("click", openCreate);
$("#refresh").addEventListener("click", refresh);
$("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderTasks();
});
$("#live-task-select").addEventListener("change", (event) => {
  state.liveTask = event.target.value;
  delete $("#live-content").dataset.html;
  renderLive();
});
$("#filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  $$(".filter").forEach((el) => el.classList.toggle("active", el === button));
  renderTasks();
});
$("#provider-select").addEventListener("change", updateProviderHint);
$("#reviewer-select").addEventListener("change", updateProviderHint);
$("#create-form").elements.auto_start.addEventListener(
  "change",
  (event) =>
    ($("#create-submit").innerHTML =
      `${icon("plus")}${event.target.checked ? "创建并启动任务" : "保存待启动任务"}`),
);
$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  data.auto_start = form.elements.auto_start.checked;
  data.checks = data.checks
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
  for (const key of ["max_runtime_minutes", "max_agent_calls", "max_rounds"])
    data[key] = Number(data[key]);
  $("#create-submit").disabled = true;
  $("#form-error").hidden = true;
  try {
    const task = await api("/api/tasks", data);
    $("#create-dialog").close();
    form.reset();
    configureForm();
    $("#create-submit").innerHTML = `${icon("plus")}创建并启动任务`;
    toast(data.auto_start ? "任务已创建，正在启动" : "已保存为待启动任务");
    state.filter = "all";
    state.search = "";
    $("#search").value = "";
    $$(".filter").forEach((el) =>
      el.classList.toggle("active", el.dataset.filter === "all"),
    );
    switchPage("tasks");
    await refresh();
    await openDetail(task.id);
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  } finally {
    $("#create-submit").disabled = false;
  }
});
$("#detail-dialog").addEventListener("close", () => {
  state.detail = null;
  state.log = "";
});
$("#log-select").addEventListener("change", async (event) => {
  state.log = event.target.value;
  try {
    await loadLogs(state.detail);
  } catch (error) {
    toast(error.message, true);
  }
});
$("#confirm-stop").addEventListener("click", async () => {
  const ident = stopTarget;
  $("#confirm-dialog").close();
  if (!ident || state.pending.has(ident)) return;
  state.pending.add(ident);
  try {
    await api(`/api/tasks/${ident}/stop`, {});
    toast("已请求停止，正在保存执行状态");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.pending.delete(ident);
    stopTarget = null;
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);
