import {$,$$,escape,duration,active,roleStatuses,timeLabel,stableHTML} from './ui.js';
import {paginate} from './pagination.js';
function failureHTML(task){
 const f=task.failure;if(!f)return '';
 return `<section class="detail-alert task-failure" role="status"><h3>${escape(f.title)}</h3><p>${escape(f.reason)}</p><p>停止阶段：${escape(f.phase)}${f.round!=null?' · 第 '+escape(f.round)+' 轮':''} · ${escape(f.role||'watcher')}${f.exit_code!=null?' · 监督进程退出码 '+escape(f.exit_code):''}</p>${f.expected_commit?`<p>任务绑定版本：<code>${escape(f.expected_commit)}</code></p>`:''}${f.actions?.length?`<strong>处理建议</strong><ul>${f.actions.map(a=>`<li>${escape(a)}</li>`).join('')}</ul>`:''}${f.raw_summary?`<details data-key="failure-raw"><summary>原始停止原因 · ${escape(f.source||'运行记录')}</summary><pre>${escape(f.raw_summary)}</pre></details>`:''}${f.recent_failures?.length?`<details data-key="failure-history"><summary>此前中断记录（不等同于本次最终原因）</summary><ul>${f.recent_failures.map(r=>`<li>第 ${escape(r.round)} 轮 ${escape(r.role)}：${escape(r.reason)}</li>`).join('')}</ul></details>`:''}<p>worker/reviewer 的历史结论不能替代 watcher 的最终交付确认。完整记录见执行日志。</p></section>`;
}
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
  if(history.approval_only&&!history.head)return '<div class="git-history-empty">尚无审核通过的本地提交。当前改动仅有待审树快照，reviewer 与 watcher 验收通过后才提交业务代码及审计记录；失败反馈保存在任务运行记录。</div>';
  if (!history.enabled)
    return '<div class="git-history-empty">尚无项目 Git 记录；Git 项目使用新版监督器启动或恢复后，将逐轮保存改动和结论。</div>';
  const worker = history.last_worker || {};
  const page=paginate('git-history',(task.git_checkpoints || []).slice().reverse(),{rowHeight:210});
  const checkpoints=page.items;
  const branch = (history.ref || "").replace(/^refs\/heads\//, "");
  const business=history.last_business_commit||{};
  const target=history.business_head?.ref?.replace(/^refs\/heads\//,'');
  const publication=history.business_commits?`<p>业务分支：<code>${escape(target||'尚未绑定')}</code>；${business.commit?`最近业务提交：<code>${escape(business.commit)}</code>`:'尚无通过验收的业务提交'}</p>`:'<p>历史任务仅保存审计快照；业务代码未自动提交到当前分支。</p>';
  return `<div class="git-history"><h3>项目 Git 追溯 · ${worker.commit ? `第 ${worker.round} 轮 · ${escape(worker.commit.slice(0, 12))}` : "已保存初始基线"}</h3>${publication}<p>审计结论保存在独立引用中；下方提交号用于追溯审核记录。</p><div class="git-history-ref">审计引用 <code>${escape(branch)}</code></div><p>记录目录：${escape(history.directory)}</p>${worker.diff_command ? `<p>最近一轮改动</p><pre>${escape(worker.diff_command)}</pre>` : ""}<ol data-page-list="git-history" start="${page.start+1}">${checkpoints.map((item) => `<li><strong>第 ${escape(item.round)} 轮 · ${item.role === "reviewer" ? "reviewer 审查" : "worker 执行"}</strong> · ${escape(item.status)}<p>${escape(item.summary)}</p>${checkpointHTML(task.id,item)}<code>${escape(item.commit)}</code><p>${escape(item.record)}</p>${item.role === "worker" ? `<span>变更文件：${escape((item.changed_files || []).join("、") || "本轮无源码变更")}</span><pre>${escape(item.diff_command)}</pre>` : `<span>审查的 worker 提交：<code>${escape(item.base)}</code></span>`}</li>`).join("")}</ol>${page.controls}<p>更早的轮次可在目标项目目录执行 <code>git log ${escape(branch)}</code> 查看。</p></div>`;
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
  const events = (t.work?.workflow || []).filter(e => !currentOnly || (e.round ? String(e.round) === String(currentRound) : (Date.parse(e.at) || 0) >= currentStart)).filter(e => role === "all" || (role === "watcher" ? e.kind === "phase" : e.role === role && e.kind !== "phase")).slice().reverse();
  const labels = {message:"进度说明",command:"执行命令",file:"文件变更",tool:"工具操作",result:"工具结果",plan:"执行计划",output:"执行输出",phase:"监督事件",error:"执行异常",direction:"用户要求"};
  const plan = role === "all" || role === t.activity?.role ? t.activity?.plan || [] : [];
  const page=paginate('workflow',events,{rowHeight:130});
  const html = `${plan.length ? `<ol class="live-plan">${plan.map(p=>`<li class="${escape(p.status)}">${escape(p.step)}</li>`).join("")}</ol>` : ""}<ol class="work-events" data-page-list="workflow">${page.items.map(e=>`<li><div class="work-event-meta"><time>${escape(timeLabel(e.at))}</time><span>${e.kind === "phase" ? "watcher" : escape(e.role || "worker")} · ${labels[e.kind] || "工作活动"}</span>${e.round ? `<span>第 ${escape(e.round)} 轮</span>` : ""}</div><details data-key="${escape(`${e.at}-${e.role}-${e.kind}-${e.text?.slice(0,60)}`)}" ${["message","phase"].includes(e.kind) ? "open" : ""}><summary>${escape((e.text || "").split("\n")[0].slice(0,200))}</summary><pre>${escape(e.text)}${e.output ? `\n\n${escape(e.output)}` : ""}</pre></details></li>`).join("") || '<li class="live-empty">该角色暂无工作流记录；新活动将在这里自动更新。</li>'}</ol>${page.controls}`;
  stableHTML($("#detail-live"),html);
  $$("[data-role-filter]").forEach(el=>el.classList.toggle("active",el.dataset.roleFilter === role));
}
function conclusionHTML(t, c) {
  return `<article class="conclusion-item" data-conclusion-role="${escape(c.role)}"><div class="conclusion-heading"><span class="conclusion-role ${escape(c.role)}">${c.role === "reviewer" ? "reviewer · 审查结论" : "worker · 执行结论"}</span><span>${c.index == null ? "最近结论" : `第 ${escape(c.index)} 轮`}</span><span class="subtle-badge">${escape(c.status === "done" ? "完成" : c.status === "continue" ? "继续处理" : c.status)}</span></div><p class="conclusion-summary">${escape(c.summary)}</p>${watcherHTML(c)}${c.reported_summary ? `<details><summary>reviewer 原始表述</summary><p>${escape(c.reported_summary)}</p></details>` : ""}${[["已完成",c.done],["待解决",c.gaps],["下一步",c.next_actions],["阻塞",c.blockers]].filter(([,values])=>values?.length).map(([label,values])=>`<div class="conclusion-field"><strong>${label}</strong><ul>${values.map(v=>`<li>${escape(v)}</li>`).join("")}</ul></div>`).join("")}${c.evidence?.length ? `<details data-key="evidence-${escape(c.role)}-${escape(c.index)}"><summary>验证依据 · ${c.evidence.length} 项</summary><ul>${c.evidence.map(e=>`<li>${escape(e)}</li>`).join("")}</ul></details>` : ""}${checkpointHTML(t.id,c)}</article>`;
}
function groupConclusions(items) {
  const groups = new Map();
  items.forEach((item, position) => {
    // Unknown rounds remain separate, so old checkpoints cannot imply a paired review.
    const key = item.index == null ? `unknown-${item.role}-${position}` : String(item.index);
    if(!groups.has(key))groups.set(key,{key,index:item.index,items:[]});
    groups.get(key).items.push(item);
  });
  return [...groups.values()].sort((a,b) => a.index == null ? (b.index == null ? 0 : 1) : b.index == null ? -1 : Number(b.index)-Number(a.index));
}
function roundHTML(t, group) {
  const title=group.index == null ? '最近结论 · 未记录轮次' : `第 ${escape(group.index)} 轮`;
  const roles=['worker','reviewer'];
  const records=roles.flatMap(role=>group.items.filter(c=>c.role===role));
  const badges=roles.map(role=>{
    const record=group.items.filter(c=>c.role===role).at(-1);
    const status=record ? record.status==='done'?'完成':record.status==='continue'?'继续处理':record.status||'已记录' : '暂无结论';
    return `<span class="round-role ${record?role:'missing'}">${role} · ${escape(status)}</span>`;
  }).join('');
  return `<details class="conclusion-round" data-key="round-${escape(group.key)}"><summary><span class="round-title">${title}</span><span class="round-roles">${badges}</span><span class="round-toggle">查看详情</span></summary><div class="round-content">${records.map(c=>conclusionHTML(t,c)).join('')}${roles.filter(role=>!records.some(c=>c.role===role)).map(role=>`<p class="round-empty">此组暂无 ${role} 结论。</p>`).join('')}</div></details>`;
}
function renderConclusions(t, {groupByRound=false} = {}) {
  const items = (t.work?.conclusions || []).filter(c=>['worker','reviewer'].includes(c.role));
  const groups=groupByRound?groupConclusions(items):[];
  if ($('#conclusion-count')) $('#conclusion-count').textContent = groupByRound ? `${groups.filter(g=>g.index!=null).length} 轮${groups.some(g=>g.index==null)?' · 含未标轮次记录':''}` : items.length;
  const page=paginate('conclusions',groupByRound?groups:items.slice().reverse(),{rowHeight:75,unit:groupByRound?'轮':'条'});
  const html = (groupByRound ? page.items.map(g=>roundHTML(t,g)) : page.items.map(c=>conclusionHTML(t,c))).join('') || '<p class="live-empty">尚无任务执行或审查结论。当前工作流会持续更新，轮次结束后在这里发布结论。</p>';
  const root=$('#detail-conclusions');root.dataset.pageList='conclusions';
  stableHTML(root,html+page.controls);
}

export {failureHTML,gitHistoryHTML,roleDetailsHTML,renderWorkflow,renderConclusions};
