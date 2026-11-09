import {$,$$,escape,duration,active,roleStatuses,timeLabel,stableHTML} from './ui.js';
function checkpointHTML(id, c) {
  const cp=c.git_checkpoint||c;
  if(!cp.commit)return '';
  const branch=(cp.ref||'').replace(/^refs\/heads\//,'');
  return `<div class="conclusion-git"><button class="detail-button" data-id="${escape(id)}" data-git-record="${escape(cp.commit)}">查看本地提交 · ${escape(cp.commit.slice(0,12))} ↗</button><small>提交分支：${escape(branch||'未记录分支')}</small></div>`;
}
function watcherHTML(c) {
  const result=c.watcher_result||{};
  if(c.role!=='reviewer'||!result.status)return '';
  const rejected=result.status==='rejected';
  const text=rejected?'系统未接受审计项关闭，仍需复核':result.closed?.length?`watcher 已登记关闭 ${result.closed.join('、')}`:result.status==='applied'?'watcher 已记录本轮审计反馈':'本轮未应用审计项关闭';
  return `<div class="watcher-outcome ${rejected?'rejected':'applied'}"><strong>${escape(text)}</strong>${result.reasons?.length?`<details><summary>查看系统处理原因</summary><ul>${result.reasons.map(r=>`<li>${escape(r)}</li>`).join('')}</ul></details>`:''}</div>`;
}
function gitHistoryHTML(task) {
  const history = task.git_history || {};
  if(history.approval_only&&!history.head)return '<div class="git-history-empty">尚无审核通过的本地提交。当前改动仅有待审树快照，reviewer 与 watcher 验收通过后才创建审计分支提交；失败反馈保存在任务运行记录。</div>';
  if (!history.enabled)
    return '<div class="git-history-empty">尚无项目 Git 记录；Git 项目使用新版监督器启动或恢复后，将逐轮保存改动和结论。</div>';
  const worker = history.last_worker || {};
  const checkpoints = (task.git_checkpoints || []).slice().reverse();
  const branch = (history.ref || "").replace(/^refs\/heads\//, "");
  return `<details class="git-history" data-key="git-history"><summary>项目 Git 追溯 · ${worker.commit ? `第 ${worker.round} 轮 · ${escape(worker.commit.slice(0, 12))}` : "已保存初始基线"}</summary><p>每轮改动与工作结论保存在此分支，审核关联对应的 worker 提交。</p><div class="git-history-ref">分支 <code>${escape(branch)}</code></div><p>记录目录：${escape(history.directory)}</p>${worker.diff_command ? `<p>最近一轮改动</p><pre>${escape(worker.diff_command)}</pre>` : ""}<ol>${checkpoints.map((item) => `<li><strong>第 ${escape(item.round)} 轮 · ${item.role === "reviewer" ? "reviewer 审查" : "worker 执行"}</strong> · ${escape(item.status)}<p>${escape(item.summary)}</p>${checkpointHTML(task.id,item)}<code>${escape(item.commit)}</code><p>${escape(item.record)}</p>${item.role === "worker" ? `<span>变更文件：${escape((item.changed_files || []).join("、") || "本轮无源码变更")}</span><pre>${escape(item.diff_command)}</pre>` : `<span>审查的 worker 提交：<code>${escape(item.base)}</code></span>`}</li>`).join("")}</ol><p>更早的轮次可在目标项目目录执行 <code>git log ${escape(branch)}</code> 查看。</p></details>`;
}
function roleDetailsHTML(t) {
  return ["worker", "reviewer", "watcher"].map(name => {
    const r = t.work?.roles?.[name] || {status:"not_started"};
    const titles = {worker:"任务实现", reviewer:"独立审查", watcher:"监督与调度"};
    const last = r.last_conclusion;
    const work = (name === 'reviewer' ? r.wait_reason : '') || r.current_work || (r.status === "waiting" ? "等待本轮调度" : last?.summary || "暂无工作记录");
    const facts = name === "watcher"
      ? [["心跳", r.heartbeat_age_s == null ? "—" : `${duration(r.heartbeat_age_s)} 前`], ["监督进程", r.pid || "—"], ["守护进程", r.guardian_pid || "—"], ["模型调用", `${r.usage?.agent_calls || 0} / ${r.usage?.max_agent_calls || "不限"}`], ["恢复次数", r.recovery_count || 0]]
      : [["执行器", r.provider || "—"], ["工作轮次", r.round == null ? "—" : `第 ${r.round} 轮`], ["执行进程", r.pid || "—"], ["会话", r.session_id || "尚无会话"]];
    if(name === 'watcher'){
      const meter=r.usage?.provider_reported;
      if(meter)facts.push(['后端已报告 token',meter.tokens],['后端报告费用（保守累计）',`$${meter.cost_usd}`],['未报告用量的调用轮次',meter.unreported_turns]);
      if(t.quality_policy?.verified_progress)facts.push(['无验证进展轮次',t.quality_state?.no_progress_turns??t.health?.no_progress_turns??'见监督状态']);
    }
    if(name === 'reviewer'){
      facts[1][0] = r.status === 'running' ? '当前审核轮次' : '最近审核轮次';
      facts.push(['周期审核', r.every_worker_rounds == null ? '未记录' : r.every_worker_rounds === 0 ? '仅完成候选及阻塞' : `每 ${r.every_worker_rounds} 轮`]);
      facts.push(['审核间隔已累计', r.rounds_since_review == null ? '未记录' : `${r.rounds_since_review} 轮${r.due ? ' · 已达到阈值' : ''}`]);
    }
    return `<article class="role-card role-${name}" data-role="${name}"><div class="role-card-title"><strong>${name}</strong><span class="role-state ${escape(r.status)}">${escape(name === 'reviewer' && r.status === 'waiting' ? '等待 worker 结果' : roleStatuses[r.status] || r.status)}</span></div><h3>${titles[name]}</h3><p class="role-current">${escape(work)}</p><dl>${facts.map(([k,v])=>`<div><dt>${k}</dt><dd>${escape(v)}</dd></div>`).join("")}</dl>${last ? `<div class="role-last"><span>最近结论</span><p>${escape(last.summary)}</p>${watcherHTML(last)}${checkpointHTML(t.id,last)}</div>` : ""}<button class="detail-button" data-role-focus="${name}">查看${name === "watcher" ? "监督记录" : "工作记录"} ↓</button></article>`;
  }).join("");
}
function renderWorkflow(t, {role = "all", currentOnly = false, showRoles = true} = {}) {
  if (showRoles && $("#role-details")) stableHTML($("#role-details"), roleDetailsHTML(t));
  const phase = t.health.phase;
  const stages = [...(t.workflow==='review-first'?[["reviewer","reviewer 完善需求","refining_requirements"]]:[]),["worker", "worker 实现", "working"], ["reviewer", "reviewer 审查", "reviewing"], ["watcher", "watcher 验收与本地提交", "validating"], ["watcher", "归档交付", "closing_agents"]];
  $("#workflow-stages").innerHTML = stages.map(([role,label,stage],i) => {
    const skipped = stage === "reviewing" && t.work?.roles?.reviewer?.status === "disabled";
    const complete = t.status === "done" && !skipped;
    const current = active(t) && t.status !== "paused" && phase === stage && !skipped;
    return `<div class="workflow-stage ${complete ? "complete" : current ? "current" : ""}"><span>${skipped ? "—" : complete ? "✓" : i+1}</span><div><strong>${label}</strong><small>${skipped ? "未启用" : complete ? "已完成" : phase === stage && t.status === "paused" ? "已暂停" : current ? "正在执行" : ""}</small></div></div>`;
  }).join("");
  const currentRound = t.activity?.round;
  const currentStart = Date.parse(t.activity?.started_at) || 0;
  const events = (t.work?.workflow || []).filter(e => !currentOnly || (e.round ? String(e.round) === String(currentRound) : (Date.parse(e.at) || 0) >= currentStart)).filter(e => role === "all" || (role === "watcher" ? e.kind === "phase" : e.role === role && e.kind !== "phase")).slice(-40).reverse();
  const labels = {message:"进度说明",command:"执行命令",file:"文件变更",tool:"工具操作",result:"工具结果",plan:"执行计划",output:"执行输出",phase:"监督事件",error:"执行异常",direction:"用户要求"};
  const plan = role === "all" || role === t.activity?.role ? t.activity?.plan || [] : [];
  const html = `${plan.length ? `<ol class="live-plan">${plan.map(p=>`<li class="${escape(p.status)}">${escape(p.step)}</li>`).join("")}</ol>` : ""}<ol class="work-events">${events.map(e=>`<li><div class="work-event-meta"><time>${escape(timeLabel(e.at))}</time><span>${e.kind === "phase" ? "watcher" : escape(e.role || "worker")} · ${labels[e.kind] || "工作活动"}</span>${e.round ? `<span>第 ${escape(e.round)} 轮</span>` : ""}</div><details data-key="${escape(`${e.at}-${e.role}-${e.kind}-${e.text?.slice(0,60)}`)}" ${["message","phase"].includes(e.kind) ? "open" : ""}><summary>${escape((e.text || "").split("\n")[0].slice(0,200))}</summary><pre>${escape(e.text)}${e.output ? `\n\n${escape(e.output)}` : ""}</pre></details></li>`).join("") || '<li class="live-empty">该角色暂无工作流记录；新活动将在这里自动更新。</li>'}</ol>`;
  stableHTML($("#detail-live"),html);
  $$("[data-role-filter]").forEach(el=>el.classList.toggle("active",el.dataset.roleFilter === role));
}
function renderConclusions(t) {
  const items = [...(t.work?.conclusions || [])].reverse();
  if ($("#conclusion-count")) $("#conclusion-count").textContent = items.length;
  const html = items.map(c=>`<article class="conclusion-item" data-conclusion-role="${escape(c.role)}"><div class="conclusion-heading"><span class="conclusion-role ${escape(c.role)}">${c.role === "reviewer" ? "reviewer · 审查结论" : "worker · 执行结论"}</span><span>${c.index == null ? "最近结论" : `第 ${escape(c.index)} 轮`}</span><span class="subtle-badge">${escape(c.status === "done" ? "完成" : c.status === "continue" ? "继续处理" : c.status)}</span></div><p class="conclusion-summary">${escape(c.summary)}</p>${watcherHTML(c)}${c.reported_summary ? `<details><summary>reviewer 原始表述</summary><p>${escape(c.reported_summary)}</p></details>` : ""}${[["已完成",c.done],["待解决",c.gaps],["下一步",c.next_actions],["阻塞",c.blockers]].filter(([,values])=>values?.length).map(([label,values])=>`<div class="conclusion-field"><strong>${label}</strong><ul>${values.map(v=>`<li>${escape(v)}</li>`).join("")}</ul></div>`).join("")}${c.evidence?.length ? `<details data-key="evidence-${escape(c.role)}-${escape(c.index)}"><summary>验证依据 · ${c.evidence.length} 项</summary><ul>${c.evidence.map(e=>`<li>${escape(e)}</li>`).join("")}</ul></details>` : ""}${checkpointHTML(t.id,c)}</article>`).join("") || '<p class="live-empty">尚无任务执行或审查结论。当前工作流会持续更新，轮次结束后在这里发布结论。</p>';
  stableHTML($("#detail-conclusions"),html);
}

export {gitHistoryHTML,roleDetailsHTML,renderWorkflow,renderConclusions};
