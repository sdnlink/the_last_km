import {i18n} from '../i18n.js';
import {$,escape,bytes,percent,stateLabel} from '/ui.js';
import {api,watch} from '/core.js';
const state={resources:null,tasks:[],history:[]};
function renderResources() {
  const r = state.resources;
  if (!r) return;
  $('#resource-sampling').innerHTML=stateLabel('complete',i18n('已采样 · 每 3 秒更新'));
  const memory = (r.memory_used / r.memory_total) * 100,
    disk = (r.disk_used / r.disk_total) * 100;
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
    [i18n("CPU 核心数"), i18n`${r.cpu_count} 核`],
    [
      i18n("1 / 5 / 15 分钟负载"),
      r.load_average.map((n) => n.toFixed(2)).join(" / "),
    ],
    [i18n("任务本地进程内存"), bytes(taskMemory)],
    [
      i18n("任务本地进程数"),
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
    const [x,y]=points.at(-1);
    html += `<path class="area" d="${d} L${x} 155 L${points[0][0]} 155Z"/><path class="trend" d="${d}"/><circle class="sample-point" cx="${Math.max(4,Math.min(596,x))}" cy="${y}" r="3"/>`;
  }
  el.innerHTML = html;
}

export function mount(){watch(async()=>{const data=await api('/api/resources');state.resources=data.resources;state.tasks=data.tasks;state.history.push({cpu:data.resources.cpu_percent,memory:data.resources.memory_used/data.resources.memory_total*100});state.history=state.history.slice(-60);renderResources();});}
