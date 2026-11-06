import {$,escape,bytes,percent} from '/ui.js';
import {api,watch} from '/core.js';
const state={resources:null,tasks:[],history:[]};
function renderResources() {
  const r = state.resources;
  if (!r) return;
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

export function mount(){watch(async()=>{const data=await api('/api/resources');state.resources=data.resources;state.tasks=data.tasks;state.history.push({cpu:data.resources.cpu_percent,memory:data.resources.memory_used/data.resources.memory_total*100});state.history=state.history.slice(-60);renderResources();});}
