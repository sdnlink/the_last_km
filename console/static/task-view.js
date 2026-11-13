import {i18n,i18nHTML} from './i18n.js';
import {$,$$,escape,duration,active,roleStatuses,timeLabel,dateTimeLabel,stableHTML} from './ui.js';
import {paginate,pagedList} from './pagination.js';
function fullLogURL(id,file){
 return `/api/tasks/${encodeURIComponent(id)}/logs?file=${encodeURIComponent(file)}&full=1`;
}
function activityTextHTML(t,e){
 const preview=e.truncated?i18n`进度说明已截断${e.text_length?i18n`（共 ${escape(e.text_length)} 字符）`:''}。`:e.preview_only?i18n('当前为进度预览。'):'';
 const link=e.text_file?i18nHTML`<a href="${escape(fullLogURL(t.id,e.text_file))}" target="_blank" rel="noopener">查看本轮完整输出</a>`:'';
 return `<pre>${escape(e.display_text ?? e.text)}${e.output?`\n\n${escape(e.output)}`:''}</pre>${preview||link?`<p class="control-hint">${preview}${link||i18n('完整输出将在本轮结束后保存到执行日志。')}</p>`:''}`;
}
function requirementReviewHTML(brief){
 if(!brief?.status)return '';
 const status=({ready:i18n('已就绪'),reviewing:i18n('独立审查中'),failed:i18n('执行失败'),needs_input:i18n('需要补充信息')})[brief.status]||brief.status;
 const fields=[[i18n('验收条件'),'acceptance_criteria'],[i18n('约束'),'constraints'],[i18n('假设'),'assumptions'],[i18n('阻塞条件'),'blockers']];
 return i18nHTML`<h3>reviewer 需求完善 · ${escape(status)}</h3><div class="requirement-text">${escape(brief.summary)}</div>${brief.refined_requirement?`<div class="requirement-text">${escape(brief.refined_requirement)}</div>`:''}${fields.filter(([,key])=>brief[key]?.length).map(([label,key])=>`<h4>${label}</h4>${pagedList('review-'+key,brief[key],c=>`<li>${escape(c)}</li>`)}`).join('')}<p>原始需求保留；这些补充不代表功能已经实现。</p>`;
}
function failureHTML(task){
 const f=task.failure;if(!f)return '';
 const changes=f.workspace_changes;
 const changedHTML=changes?i18nHTML`<details open data-key="requirement-workspace-changes"><summary>审查期间变化文件 · ${escape(changes.count)} 项</summary><ul>${(changes.files||[]).map(item=>`<li>${escape(({added:i18n('新增'),removed:i18n('删除'),modified:i18n('修改')})[item.change]||item.change)} · ${escape(item.file)}</li>`).join('')}</ul>${changes.report_file?i18nHTML`<a href="${escape(fullLogURL(task.id,changes.report_file))}" target="_blank" rel="noopener">查看完整变化记录</a>`:''}</details>`:'';
 const reviewHTML=f.review_output_file?i18nHTML`<p><a href="${escape(fullLogURL(task.id,f.review_output_file))}" target="_blank" rel="noopener">查看 reviewer 完整原始结果（未获 watcher 接受）</a></p>`:'';
 return i18nHTML`<section class="detail-alert task-failure" role="status"><h3>${escape(f.title)}</h3><p>${escape(f.reason)}</p><p>停止阶段：${escape(f.phase)}${f.round!=null?' · '+i18n`第 ${escape(f.round)} 轮`:''} · ${escape(f.role||'watcher')}${f.exit_code!=null?i18n(' · 监督进程退出码 ')+escape(f.exit_code):''}</p>${f.expected_commit?i18nHTML`<p>任务绑定版本：<code>${escape(f.expected_commit)}</code></p>`:''}${f.actions?.length?i18nHTML`<strong>处理建议</strong><ul>${f.actions.map(a=>`<li>${escape(a)}</li>`).join('')}</ul>`:''}${changedHTML}${reviewHTML}${f.raw_summary?i18nHTML`<details open data-key="failure-raw"><summary>原始停止原因 · ${escape(f.source||i18n('运行记录'))}</summary><pre>${escape(f.raw_summary)}</pre></details>`:''}${f.recent_failures?.length?i18nHTML`<details data-key="failure-history"><summary>此前中断记录（不等同于本次最终原因）</summary><ul>${f.recent_failures.map(r=>i18nHTML`<li>第 ${escape(r.round)} 轮 ${escape(r.role)}：${escape(r.reason)}</li>`).join('')}</ul></details>`:''}<p>worker/reviewer 的历史结论不能替代 watcher 的最终交付确认。完整记录见执行日志。</p></section>`;
}
function checkpointHTML(id, c) {
  const cp=c.git_checkpoint||c;
  if(!cp.commit)return '';
  const branch=(cp.ref||'').replace(/^refs\/heads\//,'');
  return i18nHTML`<div class="conclusion-git"><button class="detail-button" data-id="${escape(id)}" data-git-record="${escape(cp.commit)}">查看本地提交 · ${escape(cp.commit.slice(0,12))} ↗</button><small>提交分支：${escape(branch||i18n('未记录分支'))}</small></div>`;
}
function watcherHTML(c) {
  const result=c.watcher_result||{};
  if(c.role!=='reviewer'||!result.status)return '';
  const rejected=result.status==='rejected';
  const text=rejected?i18n('系统未接受审计项关闭，仍需复核'):result.closed?.length?i18n`watcher 已登记关闭 ${result.closed.join(i18n('、'))}`:result.status==='applied'?i18n('watcher 已记录本轮审计反馈'):i18n('本轮未应用审计项关闭');
  return `<div class="watcher-outcome ${rejected?'rejected':'applied'}"><strong>${escape(text)}</strong>${result.reasons?.length?i18nHTML`<details><summary>查看系统处理原因</summary><ul>${result.reasons.map(r=>`<li>${escape(r)}</li>`).join('')}</ul></details>`:''}</div>`;
}
function gitHistoryHTML(task) {
  const history = task.git_history || {};
  const inputs=paginate('business-inputs',(history.business_updates||[]).slice().reverse(),{rowHeight:72});
  const updates=inputs.total?i18nHTML`<section class="business-inputs"><h3>已自动衔接业务新提交 · ${escape(history.business_update_count||inputs.total)} 次</h3><p>原需求和未完成审核问题保留，变更后的代码重新开发、审核和验收。</p><ol data-page-list="business-inputs" start="${inputs.start+1}">${inputs.items.map(change=>`<li><code>${escape(change.before?.commit?.slice(0,12)||i18n('初始分支'))}</code> → <code>${escape(change.after?.commit?.slice(0,12))}</code><p>${escape(dateTimeLabel(change.at))}</p></li>`).join('')}</ol>${inputs.controls}</section>`:'';
  if(history.approval_only&&!history.head)return updates+i18nHTML('<div class="git-history-empty">尚无审核通过的本地提交。当前改动仅有待审树快照，reviewer 与 watcher 验收通过后才提交业务代码及审计记录；失败反馈保存在任务运行记录。</div>');
  if (!history.enabled)
    return i18nHTML('<div class="git-history-empty">尚无项目 Git 记录；Git 项目使用新版监督器启动或恢复后，将逐轮保存改动和结论。</div>');
  const worker = history.last_worker || {};
  const page=paginate('git-history',(task.git_checkpoints || []).slice().reverse(),{rowHeight:210});
  const checkpoints=page.items;
  const branch = (history.ref || "").replace(/^refs\/heads\//, "");
  const business=history.last_business_commit||{};
  const target=history.business_head?.ref?.replace(/^refs\/heads\//,'');
  const publication=history.business_commits?i18nHTML`<p>业务分支：<code>${escape(target||i18n('尚未绑定'))}</code>；${business.commit?i18nHTML`最近业务提交：<code>${escape(business.commit)}</code>`:i18n('尚无通过验收的业务提交')}</p>`:i18nHTML('<p>历史任务仅保存审计快照；业务代码未自动提交到当前分支。</p>');
  return i18nHTML`${updates}<div class="git-history"><h3>项目 Git 追溯 · ${worker.commit ? i18n`第 ${worker.round} 轮 · ${escape(worker.commit.slice(0, 12))}` : i18n("已保存初始基线")}</h3>${publication}<p>审计结论保存在独立引用中；下方提交号用于追溯审核记录。</p><div class="git-history-ref">审计引用 <code>${escape(branch)}</code></div><p>记录目录：${escape(history.directory)}</p>${worker.diff_command ? i18nHTML`<p>最近一轮改动</p><pre>${escape(worker.diff_command)}</pre>` : ""}<ol data-page-list="git-history" start="${page.start+1}">${checkpoints.map((item) => i18nHTML`<li><strong>第 ${escape(item.round)} 轮 · ${item.role === "reviewer" ? i18n("reviewer 审查") : i18n("worker 执行")}</strong> · ${escape(item.status)}<p>${escape(item.summary)}</p>${checkpointHTML(task.id,item)}<code>${escape(item.commit)}</code><p>${escape(item.record)}</p>${item.role === "worker" ? i18nHTML`<span>变更文件：${escape((item.changed_files || []).join(i18n("、")) || i18n("本轮无源码变更"))}</span><pre>${escape(item.diff_command)}</pre>` : i18nHTML`<span>审查的 worker 提交：<code>${escape(item.base)}</code></span>`}</li>`).join("")}</ol>${page.controls}<p>更早的轮次可在目标项目目录执行 <code>git log ${escape(branch)}</code> 查看。</p></div>`;
}
function roleDetailsHTML(t) {
  return ["worker", "reviewer", "watcher"].map(name => {
    const r = t.work?.roles?.[name] || {status:"not_started"};
    const titles = {worker:i18n("任务实现"), reviewer:i18n("独立审查"), watcher:i18n("监督与调度")};
    const last = r.last_conclusion;
    const work = (name === 'reviewer' ? r.wait_reason : '') || r.current_work || (r.status === "waiting" ? i18n("等待本轮调度") : last?.summary || i18n("暂无工作记录"));
    const facts = name === "watcher"
      ? [[i18n("心跳"), r.heartbeat_age_s == null ? "—" : i18n`${duration(r.heartbeat_age_s)} 前`], [i18n("监督进程"), r.pid || "—"], [i18n("守护进程"), r.guardian_pid || "—"], [i18n("模型调用"), `${r.usage?.agent_calls || 0} / ${r.usage?.max_agent_calls || i18n("不限")}`], [i18n("恢复次数"), r.recovery_count || 0]]
      : [[i18n("执行器"), r.provider || "—"], [i18n("工作轮次"), r.round == null ? "—" : i18n`第 ${r.round} 轮`], [i18n("执行进程"), r.pid || "—"], [i18n("会话"), r.session_id || i18n("尚无会话")]];
    if(name === 'watcher'){
      const meter=r.usage?.provider_reported;
      if(meter)facts.push([i18n('后端已报告 token'),meter.tokens],[i18n('后端报告费用（保守累计）'),`$${meter.cost_usd}`],[i18n('未报告用量的调用轮次'),meter.unreported_turns]);
      if(t.quality_policy?.verified_progress)facts.push([i18n('无验证进展轮次'),t.quality_state?.no_progress_turns??t.health?.no_progress_turns??i18n('见监督状态')]);
    }
    if(name === 'reviewer'){
      facts[1][0] = r.status === 'running' ? i18n('当前审核轮次') : i18n('最近审核轮次');
      facts.push([i18n('周期审核'), r.every_worker_rounds == null ? i18n('未记录') : r.every_worker_rounds === 0 ? i18n('仅完成候选及阻塞') : i18n`每 ${r.every_worker_rounds} 轮`]);
      facts.push([i18n('审核间隔已累计'), r.rounds_since_review == null ? i18n('未记录') : i18n`${r.rounds_since_review} 轮${r.due ? i18n(' · 已达到阈值') : ''}`]);
    }
    return i18nHTML`<article class="role-card role-${name}" data-role="${name}"><div class="role-card-title"><strong>${name}</strong><span class="role-state ${escape(r.status)}">${escape(name === 'reviewer' && r.status === 'waiting' ? i18n('等待 worker 结果') : roleStatuses[r.status] || r.status)}</span></div><h3>${titles[name]}</h3><p class="role-current">${escape(work)}</p><dl>${facts.map(([k,v])=>`<div><dt>${k}</dt><dd>${escape(v)}</dd></div>`).join("")}</dl>${last ? i18nHTML`<div class="role-last"><span>最近结论</span><p>${escape(last.summary)}</p>${watcherHTML(last)}${checkpointHTML(t.id,last)}</div>` : ""}<button class="detail-button" data-role-focus="${name}">查看${name === "watcher" ? i18n("监督记录") : i18n("工作记录")} ↓</button></article>`;
  }).join("");
}
function renderWorkflow(t, {role = "all", currentOnly = false, showRoles = true} = {}) {
  if (showRoles && $("#role-details")) stableHTML($("#role-details"), roleDetailsHTML(t));
  const phase = t.health.phase;
  const stages = [...(t.workflow==='review-first'?[["reviewer",i18n("reviewer 完善需求"),"refining_requirements"]]:[]),["worker", i18n("worker 实现"), "working"], ["reviewer", i18n("reviewer 审查"), "reviewing"], ["watcher", i18n("watcher 验收与本地提交"), "validating"], ["watcher", i18n("归档交付"), "closing_agents"]];
  $("#workflow-stages").innerHTML = stages.map(([role,label,stage],i) => {
    const skipped = stage === "reviewing" && t.work?.roles?.reviewer?.status === "disabled";
    const complete = t.status === "done" && !skipped;
    const current = active(t) && t.status !== "paused" && phase === stage && !skipped;
    return `<div class="workflow-stage ${complete ? "complete" : current ? "current" : ""}"><span>${skipped ? "—" : complete ? "✓" : i+1}</span><div><strong>${label}</strong><small>${skipped ? i18n("未启用") : complete ? i18n("已完成") : phase === stage && t.status === "paused" ? i18n("已暂停") : current ? i18n("正在执行") : ""}</small></div></div>`;
  }).join("");
  const currentRound = t.activity?.round;
  const currentStart = Date.parse(t.activity?.started_at) || 0;
  const events = (t.work?.workflow || []).filter(e => !currentOnly || (e.round ? String(e.round) === String(currentRound) : (Date.parse(e.at) || 0) >= currentStart)).filter(e => role === "all" || (role === "watcher" ? e.kind === "phase" : e.role === role && e.kind !== "phase")).slice().reverse();
  const labels = {message:i18n("进度说明"),command:i18n("执行命令"),file:i18n("文件变更"),tool:i18n("工具操作"),result:i18n("工具结果"),plan:i18n("执行计划"),output:i18n("执行输出"),phase:i18n("监督事件"),error:i18n("执行异常"),direction:i18n("用户要求")};
  const plan = role === "all" || role === t.activity?.role ? t.activity?.plan || [] : [];
  const page=paginate('workflow',events,{rowHeight:130});
  const html = `${plan.length ? `<ol class="live-plan">${plan.map(p=>`<li class="${escape(p.status)}">${escape(p.step)}</li>`).join("")}</ol>` : ""}<ol class="work-events" data-page-list="workflow">${page.items.map(e=>`<li><div class="work-event-meta"><time>${escape(timeLabel(e.at))}</time><span>${e.kind === "phase" ? "watcher" : escape(e.role || "worker")} · ${labels[e.kind] || i18n("工作活动")}</span>${e.round ? i18nHTML`<span>第 ${escape(e.round)} 轮</span>` : ""}</div><details data-key="${escape(`${e.at}-${e.role}-${e.kind}-${e.text?.slice(0,60)}`)}" ${["message","phase"].includes(e.kind) ? "open" : ""}><summary>${escape((e.display_text ?? e.text ?? "").split("\n")[0].slice(0,200))}</summary>${activityTextHTML(t,e)}</details></li>`).join("") || i18nHTML('<li class="live-empty">该角色暂无工作流记录；新活动将在这里自动更新。</li>')}</ol>${page.controls}`;
  stableHTML($("#detail-live"),html);
  $$("[data-role-filter]").forEach(el=>el.classList.toggle("active",el.dataset.roleFilter === role));
}
function conclusionHTML(t, c) {
  return `<article class="conclusion-item" data-conclusion-role="${escape(c.role)}"><div class="conclusion-heading"><span class="conclusion-role ${escape(c.role)}">${c.role === "reviewer" ? i18n("reviewer · 审查结论") : i18n("worker · 执行结论")}</span><span>${c.index == null ? i18n("最近结论") : i18n`第 ${escape(c.index)} 轮`}</span><span class="subtle-badge">${escape(c.status === "done" ? i18n("完成") : c.status === "continue" ? i18n("继续处理") : c.status)}</span></div><p class="conclusion-summary">${escape(c.summary)}</p>${watcherHTML(c)}${c.reported_summary ? i18nHTML`<details><summary>reviewer 原始表述</summary><p>${escape(c.reported_summary)}</p></details>` : ""}${[[i18n("已完成"),c.done],[i18n("待解决"),c.gaps],[i18n("下一步"),c.next_actions],[i18n("阻塞"),c.blockers]].filter(([,values])=>values?.length).map(([label,values])=>`<div class="conclusion-field"><strong>${label}</strong><ul>${values.map(v=>`<li>${escape(v)}</li>`).join("")}</ul></div>`).join("")}${c.evidence?.length ? i18nHTML`<details data-key="evidence-${escape(c.role)}-${escape(c.index)}"><summary>验证依据 · ${c.evidence.length} 项</summary><ul>${c.evidence.map(e=>`<li>${escape(e)}</li>`).join("")}</ul></details>` : ""}${checkpointHTML(t.id,c)}</article>`;
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
  const title=group.index == null ? i18n('最近结论 · 未记录轮次') : i18n`第 ${escape(group.index)} 轮`;
  const roles=['worker','reviewer'];
  const records=roles.flatMap(role=>group.items.filter(c=>c.role===role));
  const badges=roles.map(role=>{
    const record=group.items.filter(c=>c.role===role).at(-1);
    const status=record ? record.status==='done'?i18n('完成'):record.status==='continue'?i18n('继续处理'):record.status||i18n('已记录') : i18n('暂无结论');
    return `<span class="round-role ${record?role:'missing'}">${role} · ${escape(status)}</span>`;
  }).join('');
  return i18nHTML`<details class="conclusion-round" data-key="round-${escape(group.key)}"><summary><span class="round-title">${title}</span><span class="round-roles">${badges}</span><span class="round-toggle">查看详情</span></summary><div class="round-content">${records.map(c=>conclusionHTML(t,c)).join('')}${roles.filter(role=>!records.some(c=>c.role===role)).map(role=>i18nHTML`<p class="round-empty">此组暂无 ${role} 结论。</p>`).join('')}</div></details>`;
}
function renderConclusions(t, {groupByRound=false} = {}) {
  const items = (t.work?.conclusions || []).filter(c=>['worker','reviewer'].includes(c.role));
  const groups=groupByRound?groupConclusions(items):[];
  if ($('#conclusion-count')) $('#conclusion-count').textContent = groupByRound ? i18n`${groups.filter(g=>g.index!=null).length} 轮${groups.some(g=>g.index==null)?i18n(' · 含未标轮次记录'):''}` : items.length;
  const page=paginate('conclusions',groupByRound?groups:items.slice().reverse(),{rowHeight:75,unit:groupByRound?i18n('轮'):i18n('条')});
  const html = (groupByRound ? page.items.map(g=>roundHTML(t,g)) : page.items.map(c=>conclusionHTML(t,c))).join('') || i18nHTML('<p class="live-empty">尚无任务执行或审查结论。当前工作流会持续更新，轮次结束后在这里发布结论。</p>');
  const root=$('#detail-conclusions');root.dataset.pageList='conclusions';
  stableHTML(root,html+page.controls);
}

export {failureHTML,gitHistoryHTML,roleDetailsHTML,renderWorkflow,renderConclusions,activityTextHTML,requirementReviewHTML,fullLogURL};
