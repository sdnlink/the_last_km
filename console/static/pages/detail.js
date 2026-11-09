import {$,$$,escape,badge,taskButtons,stableHTML} from '/ui.js';
import {api,watch,toast} from '/core.js';
import {gitHistoryHTML,renderWorkflow,renderConclusions} from '/task-view.js';
const id=document.body.dataset.taskId;
let task,role='all',log='',logSequence=0;
function render(t){
 task=t;$('#detail-title').textContent=t.title;$('#detail-project').textContent=t.cwd;
 $('#detail-open-direction').dataset.direction=id;$('#detail-open-direction').disabled=!t.can_direct;
 $('#detail-progress-link').href=`/progress?task=${id}`;
 $('#detail-actions').innerHTML=`${badge(t)}${taskButtons(t)}`;
 renderWorkflow(t,{role});renderConclusions(t);stableHTML($('#detail-git'),gitHistoryHTML(t));
 const alerts=[t.control_message,t.resume_unavailable,t.direction_error,t.health.report_warning,t.launch_error].filter(Boolean);
 if(t.health.health==='heartbeat_stale')alerts.push('监督器心跳已超时，请核对 watcher 工作记录。');
 if(t.status==='interrupted')alerts.push('任务执行已中断，可查看工作记录后从检查点恢复。');
 $('#detail-alerts').innerHTML=alerts.map(a=>`<p class="detail-alert">${escape(a)}</p>`).join('');
 const coverage=t.requirement_coverage;
 const coverageHTML=coverage?.requirements?.length?`<h3>上次需求证据覆盖检查</h3><p>此处为历史检查，不代表当前交付确认。核对版本：${escape(coverage.tree||'未记录')}；${coverage.complete?'已满足结构化覆盖门禁':'尚有缺口'}</p><ul class="module-list">${coverage.requirements.map(r=>`<li><span>${escape(r.id)}</span><span>${escape(r.text)} · ${escape((r.evidence||[]).join('、'))}</span><span>${r.covered?'已覆盖':'待核对'}</span></li>`).join('')}</ul>`:'';
 const brief=t.requirements_review;
 const briefHTML=brief?.status?`<h3>reviewer 需求完善 · ${escape(brief.status)}</h3><div class="requirement-text">${escape(brief.refined_requirement||brief.summary)}</div><ul>${(brief.acceptance_criteria||[]).map(c=>`<li>${escape(c)}</li>`).join('')}</ul><p>原始需求保留；这些补充不代表功能已经实现。</p>`:'';
 const plan=t.acceptance_plan,results=t.acceptance_results;
 const checkLabels={passed:'检查通过',check_failed:'检查未通过（需核对输出）',environment_unavailable:'运行环境不可用',timed_out:'检查超时',interrupted:'检查被中断'};
 const acceptanceHTML=plan?.criteria?`<h3>完成标准</h3><div class="requirement-text">${escape(plan.criteria)}</div><p>${t.verification_mode==='planning'?'本任务仅制定验收方案，不代表原目标验收。':t.verification_mode==='review_only'?'仅语义审核：未提供运行测试通过证明。':'检查通过仍须结合独立审核、模块及审计门禁。'}</p><h3>选定检查</h3><ul class="module-list">${(plan.selected||[]).map(c=>`<li><span>${escape(c.title)}</span><span>${escape(c.purpose)}</span></li>`).join('')||'<li>未选择项目自动检查；如有高级命令，结果在下面单列。</li>'}</ul>`:'';
 const resultHTML=results?.checks?`<h3>上次实际检查结果 · 第 ${escape(results.round)} 轮</h3><p>这是检查快照，不代表当前版本已经最终交付。版本：${escape(results.tree)}${results.source_changed?'；检查改变源码，原审核证据已失效。':''}</p>${results.checks.map(c=>`<details><summary>${escape(checkLabels[c.status]||c.status)} · ${escape((plan?.selected||[]).find(x=>x.command===c.command)?.title||'验收检查')}</summary><pre class="requirement-text">${escape(c.command)}</pre><p>退出码 ${escape(c.exit_code)}；输出见原始日志 ${escape(c.log)}</p></details>`).join('')}`:'';
 const modules=(t.modules||[]).map(m=>`<li><span class="module-id">${escape(m.id)}</span><span>${escape(m.title)}</span><span>${m.status==='accepted'?'已验收':'待完成'}</span></li>`).join('');
 $('#detail-body').innerHTML=`<h3>原始开发需求</h3><div class="requirement-text">${escape(t.requirement)}</div>${modules?`<h3>模块验收 · ${t.accepted_modules} / ${t.total_modules}</h3><ul class="module-list">${modules}</ul>`:''}${briefHTML}${acceptanceHTML}${resultHTML}${coverageHTML}<h3>审计问题</h3><ul class="module-list">${(t.audit_issues?.items||[]).map(i=>`<li><span class="module-id">${escape(i.id)}</span><span>${escape(i.description||i.text||i.summary||'')}</span><span>${['closed','resolved'].includes(i.status)?'已关闭':'待解决'}</span></li>`).join('')||'<li>暂无审计问题</li>'}</ul><h3>运行记录目录</h3><div class="requirement-text">${escape(t.run_dir)}</div>`;
}
async function loadLogs(){
 const seq=++logSequence;
 try{
  const data=await api(`/api/tasks/${id}/logs${log?'?file='+encodeURIComponent(log):''}`);
  if(seq!==logSequence||!$('#detail-logs').open)return;
  $('#log-select').innerHTML=data.files.length?data.files.map(n=>`<option value="${escape(n)}">${escape(n)}</option>`).join(''):'<option>暂无日志</option>';
  $('#log-select').value=data.selected;$('#log-output').textContent=data.content||'暂无执行日志。';if($('#log-follow').checked)$('#log-output').scrollTop=$('#log-output').scrollHeight;
 }catch(error){if(error.status===404&&log){log='';return loadLogs();}throw error;}
}
export function mount(){
 document.addEventListener('click',e=>{const el=e.target.closest('[data-role-filter],[data-role-focus]');if(!el)return;role=el.dataset.roleFilter||el.dataset.roleFocus;if(task)renderWorkflow(task,{role});if(el.dataset.roleFocus)$('#workflow-stages').scrollIntoView({behavior:'smooth',block:'start'});});
 $('#detail-logs').addEventListener('toggle',()=>{if($('#detail-logs').open)loadLogs().catch(e=>toast(e.message,true));else logSequence++;});
 $('#log-select').addEventListener('change',e=>{log=e.target.value;loadLogs().catch(e=>toast(e.message,true));});
 watch(async()=>{render(await api(`/api/tasks/${id}`));if($('#detail-logs').open)await loadLogs();});
}
