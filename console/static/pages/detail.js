import {$,escape,badge,taskButtons,stableHTML} from '/ui.js';
import {api,watch,toast} from '/core.js';
import {failureHTML,gitHistoryHTML,renderWorkflow,renderConclusions} from '/task-view.js';
import {pagedList,resetPage,onPaginationChange,getPager,pageView,controlsHTML} from '/pagination.js';
const id=document.body.dataset.taskId;
const section=$('#detail-page').dataset.section;
let task,role='all',log='',logSequence=0,logTotal=0;
function render(t){
 task=t;$('#detail-title').textContent=t.title;$('#detail-project').textContent=t.cwd;
 $('#detail-open-direction').dataset.direction=id;$('#detail-open-direction').disabled=!t.can_direct;
 $('#detail-progress-link').href=`/progress?task=${id}`;
 $('#detail-actions').innerHTML=`${badge(t)}${taskButtons(t)}`;
 if(section==='home')renderWorkflow(t,{role});
 if(section==='conclusions')renderConclusions(t,{groupByRound:true});
 if(section==='git')stableHTML($('#detail-git'),gitHistoryHTML(t));
 if(section==='requirements')renderRequirements(t);
 const alerts=[t.control_message,t.resume_unavailable,t.direction_error,t.health.report_warning,t.launch_error].filter(Boolean);
 if(t.health.health==='heartbeat_stale')alerts.push('监督器心跳已超时，请核对 watcher 工作记录。');
 if(t.status==='interrupted')alerts.push('任务执行已中断，可查看工作记录后从检查点恢复。');
 if($('#detail-alerts'))$('#detail-alerts').innerHTML=failureHTML(t)+alerts.map(a=>`<p class="detail-alert">${escape(a)}</p>`).join('');
}
function deliveryDiagnosis(t){
 const d=t.delivery_diagnosis;if(!d?.code)return '';
 const labels={implementation_not_started:'实现尚未开始',watcher_gate_rejected:'watcher 未通过验收门禁',
  no_comparable_review:'没有针对同一候选及范围的有效审核',worker_reviewer_disagree:'worker 与 reviewer 结论不一致',
  both_incomplete:'worker 与 reviewer 均认为尚未完成',candidate_delivered_remaining_work_pending:'该候选已交付，仍有后续工作',
  function_accepted_remaining_work_pending:'该函数已验收，仍有后续工作',joint_claim_delivery_unconfirmed:'双方确认，交付尚未证实'};
 const role=(name,r)=>'<li><strong>'+name+'</strong>：'+(r?.index?'第 '+escape(r.index)+' 轮 · '+escape(r.task_status||r.status||'未记录')+' · '+escape(r.summary||''):'无结论')+'</li>';
 return '<div class="detail-alert"><strong>到时交付诊断：'+escape(labels[d.code]||d.code)+'</strong><ul>'+
  role('Worker',d.worker)+role('Reviewer',d.reviewer)+'<li><strong>Watcher</strong>：'+
  escape((d.watcher?.reasons||[]).join('；')||(d.watcher?.published?'已记录提交':d.watcher?.checks?.length?
   d.watcher.checks.map(c=>c.status).join('、'):'无可用于确认交付的同轮验收记录'))+'</li></ul></div>';
}
function renderRequirements(t){
 const task=t.function_tasks?.current;
 const functionHTML=t.function_dispatch?'<h3>函数任务单</h3><p>已通过 '+escape(t.function_tasks?.accepted?.length||0)+' 项函数审核</p>'+
  (task?'<p><strong>'+escape(task.id)+'</strong> · '+escape(task.file)+' · '+escape(task.symbol)+'</p><p>'+escape(task.objective)+'</p>':'<p>等待 reviewer 下发或任务已结束。</p>'):'';
 const coverage=t.requirement_coverage;
 const coverageHTML=coverage?.requirements?.length?`<h3>上次需求证据覆盖检查</h3><p>此处为历史检查，不代表当前交付确认。核对版本：${escape(coverage.tree||'未记录')}；${coverage.complete?'已满足结构化覆盖门禁':'尚有缺口'}</p>${pagedList('requirement-coverage',coverage.requirements,r=>`<li><span>${escape(r.id)}</span><span>${escape(r.text)} · ${escape((r.evidence||[]).join('、'))}</span><span>${r.covered?'已覆盖':'待核对'}</span></li>`)} `:'';
 const brief=t.requirements_review;
 const briefHTML=brief?.status?`<h3>reviewer 需求完善 · ${escape(brief.status)}</h3><div class="requirement-text">${escape(brief.refined_requirement||brief.summary)}</div>${pagedList('acceptance-criteria',brief.acceptance_criteria||[],c=>`<li>${escape(c)}</li>`)}<p>原始需求保留；这些补充不代表功能已经实现。</p>`:'';
 const plan=t.acceptance_plan,results=t.acceptance_results;
 const checkLabels={passed:'检查通过',check_failed:'检查未通过（需核对输出）',environment_unavailable:'运行环境不可用',timed_out:'检查超时',interrupted:'检查被中断'};
 const acceptanceHTML=plan?.criteria?`<h3>完成标准</h3><div class="requirement-text">${escape(plan.criteria)}</div><p>${t.verification_mode==='planning'?'本任务仅制定验收方案，不代表原目标验收。':t.verification_mode==='review_only'?'仅语义审核：未提供运行测试通过证明。':'检查通过仍须结合独立审核、模块及审计门禁。'}</p><h3>选定检查</h3>${pagedList('acceptance-selected',plan.selected||[],c=>`<li><span>${escape(c.title)}</span><span>${escape(c.purpose)}</span></li>`,{empty:'<li>未选择项目自动检查；如有高级命令，结果在下面单列。</li>'})}`:'';
 const resultHTML=results?.checks?`<h3>上次实际检查结果 · 第 ${escape(results.round)} 轮</h3><p>这是检查快照，不代表当前版本已经最终交付。版本：${escape(results.tree)}${results.source_changed?'；检查改变源码，原审核证据已失效。':''}${results.invalidated?'；业务版本已更新，此结果需要重新验收。':''}</p>${pagedList('acceptance-results',results.checks,c=>`<details><summary>${escape(checkLabels[c.status]||c.status)} · ${escape((plan?.selected||[]).find(x=>x.command===c.command)?.title||'验收检查')}</summary><pre class="requirement-text">${escape(c.command)}</pre><p>退出码 ${escape(c.exit_code)}；输出见原始日志 ${escape(c.log)}</p></details>`,{tag:'div',className:'acceptance-results'})}`:'';
 const modules=pagedList('modules',t.modules||[],m=>`<li><span class="module-id">${escape(m.id)}</span><span>${escape(m.title)}</span><span>${m.status==='accepted'?'已验收':m.invalidated_reason?.startsWith('Revalidation')?'待复核（历史已验收）':'待完成'}</span></li>`);
 stableHTML($('#detail-body'),`${deliveryDiagnosis(t)}${functionHTML}<h3>原始开发需求</h3><div class="requirement-text">${escape(t.requirement)}</div>${t.modules?.length?`<h3>模块验收 · ${t.accepted_modules} / ${t.total_modules}</h3>${modules}`:''}${briefHTML}${t.acceptance_warning?`<p class="detail-alert">${escape(t.acceptance_warning)}</p>`:''}${acceptanceHTML}${resultHTML}${coverageHTML}<h3>审计问题</h3>${pagedList('audit-issues',t.audit_issues?.items||[],i=>`<li><span class="module-id">${escape(i.id)}</span><span>${escape(i.description||i.text||i.summary||'')}</span><span>${['closed','resolved'].includes(i.status)?'已关闭':i.status==='needs_review'?'待复核（保留关闭证据）':'待解决'}</span></li>`,{empty:'<li>暂无审计问题</li>'})}<h3>运行记录目录</h3><div class="requirement-text">${escape(t.run_dir)}</div>`);
}
async function loadLogs(){
 const seq=++logSequence;
 try{
  const pager=getPager('logs'),view=pageView('logs',logTotal,{rowHeight:20});
  const query=new URLSearchParams({page:$('#log-follow').checked?'0':String(pager.page),page_size:String(view.size)});
  if(log)query.set('file',log);
  const data=await api(`/api/tasks/${id}/logs?${query}`);
  if(seq!==logSequence)return;
  $('#log-select').innerHTML=data.files.length?data.files.map(n=>`<option value="${escape(n)}">${escape(n)}</option>`).join(''):'<option>暂无日志</option>';
  $('#log-select').value=data.selected;log=data.selected||'';
  const output=$('#log-output'),scroll=output.scrollTop;
  output.textContent=data.content||'暂无执行日志。';output.scrollTop=$('#log-follow').checked?output.scrollHeight:scroll;
  logTotal=data.total_lines;pager.page=data.page;
  $('#logs-pagination').innerHTML=controlsHTML('logs',pageView('logs',logTotal,{rowHeight:20}),{unit:'行'});
 }catch(error){if(error.status===404&&log){log='';return loadLogs();}throw error;}
}
export function mount(){
 onPaginationChange((key,reason)=>{if(key==='logs'){if(reason==='page')$('#log-follow').checked=false;loadLogs().catch(e=>toast(e.message,true));}else if(task)render(task);});
 document.addEventListener('click',e=>{const el=e.target.closest('[data-role-filter],[data-role-focus]');if(!el)return;role=el.dataset.roleFilter||el.dataset.roleFocus;resetPage('workflow');if(task)renderWorkflow(task,{role});if(el.dataset.roleFocus)$('#workflow-stages').scrollIntoView({behavior:'smooth',block:'start'});});
 $('#log-select')?.addEventListener('change',e=>{log=e.target.value;resetPage('logs');logTotal=0;loadLogs().catch(e=>toast(e.message,true));});
 $('#log-follow')?.addEventListener('change',()=>loadLogs().catch(e=>toast(e.message,true)));
 watch(async()=>{render(await api(`/api/tasks/${id}`));if(section==='logs')await loadLogs();});
}
