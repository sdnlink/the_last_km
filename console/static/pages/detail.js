import {i18n,i18nHTML,locale} from '../i18n.js';
import {$,$$,escape,badge,taskButtons,stableHTML,taskNarrativeHTML,stateLabel,duration} from '/ui.js';
import {api,watch,toast,refresh} from '/core.js';
import {failureHTML,gitHistoryHTML,renderWorkflow,renderConclusions,requirementReviewHTML,fullLogURL} from '/task-view.js';
import {pagedList,resetPage,onPaginationChange,getPager,pageView,controlsHTML} from '/pagination.js';
import {dispatchPlanHTML} from '/dispatch-plan.js';
import {mountSupervision} from '/supervision.js';
const id=document.body.dataset.taskId;
const section=$('#detail-page').dataset.section;
let task,role='all',log='',logSequence=0,logTotal=0;
let dispatchData=null,dispatchFilter='all',dispatchSequence=0;
let previewPrompted=false,previewChanging=false;
let shownCaptcha='', captchaSubmitting=false;
const previewAPI=()=>`/api/tasks/${encodeURIComponent(id)}/local-preview`;

function showPreviewPrompt(preview){
 const dialog=$('#preview-backend-dialog');
 $('#preview-backend-reason').textContent=i18n(preview.message);
 if(dialog.open||$('dialog[open]'))return;
 $('#preview-backend-error').hidden=true;
 if(!$('#dialog-styles')){
  const css=document.createElement('link');css.id='dialog-styles';css.rel='stylesheet';css.href='/dialogs.css';document.head.append(css);
 }
 previewPrompted=true;
 dialog.showModal();
}
function renderPreview(preview){
 const panel=$('#detail-local-preview');
 if(!panel)return;
 panel.hidden=!preview?.status||preview.status==='unavailable';
 if(panel.hidden)return;
 const link=preview.status==='ready'?`<a class="button secondary" href="${escape(preview.url)}" target="_blank" rel="noopener noreferrer">${i18n('打开本地调试')}</a>`:'';
 const starting=preview.status==='starting';
 const label=preview.status==='needs_input'?'填写测试后端':starting?'正在启动本地调试':'启动本地调试';
 const start=link||`<button class="button secondary" data-preview-action="start"${starting?' disabled':''}>${i18n(label)}</button>`;
 const stop=link||starting?`<button class="button secondary" data-preview-action="stop">${i18n('关闭本地调试')}</button>`:'';
 stableHTML(panel,`${start}${stop}${preview.status==='failed'?'<small role="status">'+escape(i18n(preview.message))+'</small>':''}`);
 if(preview.status==='needs_input'&&!previewPrompted)showPreviewPrompt(preview);
 if(preview.status!=='needs_input')previewPrompted=false;
}
async function changePreview(payload){
 if(previewChanging)return;
 previewChanging=true;
 try{
  const result=await api(previewAPI(),payload);
  if(task)task.local_preview=result;
  renderPreview(result);
 }finally{previewChanging=false;}
}
function renderBrowserCaptcha(t, reopen=false){
 const dialog=$('#browser-captcha-dialog');
 if(captchaSubmitting)return;
 const challenge=Object.values(t.field_handoff?.browser_inputs||{}).find(value=>
  (value.kind==='otp'||value.kind==='image')&&value.expires>Date.now()/1000);
 if(!challenge||t.status!=='action_required'){
  if(dialog.open)dialog.close();
  return;
 }
 if(dialog.open&&shownCaptcha===challenge.nonce)return;
 if(!reopen&&shownCaptcha===challenge.nonce)return;
 if(!dialog.open&&$('dialog[open]'))return;
 shownCaptcha=challenge.nonce;
 $('#browser-captcha-role').textContent=challenge.role;
 const otp=challenge.kind==='otp',image=$('#browser-captcha-image');
 image.hidden=otp;
 if(otp)image.removeAttribute('src');
 else image.src=`/api/tasks/${encodeURIComponent(id)}/browser-captcha/${encodeURIComponent(challenge.nonce)}`;
 $('#browser-captcha-title').textContent=otp?'填写六位动态验证码':'填写现场浏览器验证码';
 const form=$('#browser-captcha-form');
 form.reset();
 const code=form.elements.code;
 code.pattern=otp?'[0-9]{6}':'[A-Za-z0-9]{4}';
 code.maxLength=otp?6:4;
 code.inputMode=otp?'numeric':'text';
 $('#browser-captcha-error').hidden=true;
 if(!dialog.open)dialog.showModal();
}
function renderFieldHandoff(t){
 const panel=$('#detail-field-handoff');
 if(!panel)return;
 const handoff=t.field_handoff;
 const pending=(handoff?.items||[]).filter(item=>!item.receipt);
 const inputs=Object.values(handoff?.browser_inputs||{}).filter(input=>input.expires>Date.now()/1000);
 const error=handoff?.resume?.error;
 const rows=pending.map(item=>{
  const needsCaptcha=inputs.some(input=>input.id===item.id);
  const retry=handoff.failed_cases?.[item.id]?.retry_captcha;
  const status=needsCaptcha?'等待填写验证码':
    item.allow_manual&&!item.case_id&&!item.executor_task?'等待登记可执行此项验证的执行器':
    handoff.executor_status?.[item.id]||(item.executor_task?'等待执行器完成':'等待自动验证');
  return `<li>${escape(item.title)} · ${escape(status)}
   ${needsCaptcha?'<button type="button" class="button secondary" data-browser-captcha>填写验证码</button>':''}
   ${retry?`<button type="button" class="button secondary" data-browser-retry="${escape(item.id)}">重新获取验证码</button>`:''}</li>`;
 });
 const html=(rows.length||error)&&t.status==='action_required'
  ?`<section class="detail-alert"><h3>验证进度</h3>${error?`<p>自动续审暂未启动。 ${escape(error)}</p>`:''}${rows.length?`<ul>${rows.join('')}</ul>`:''}</section>`
  :'';
 stableHTML(panel,html);
}
function renderDispatch(){
 if(!dispatchData)return;
 const data=dispatchData,html=dispatchPlanHTML(data);
 $('#dispatch-notice').textContent=html.notice;
 $('#dispatch-count').textContent=i18n`派单总数：${data.counts.all}${data.complete?'':i18n('（计划未完整生成）')}`;
 $$('[data-dispatch-count]').forEach(el=>{const key=el.dataset.dispatchCount;el.textContent=key==='waiting'&&!data.complete?i18n('待生成'):data.counts[key];});
 $$('[data-dispatch-filter]').forEach(el=>el.classList.toggle('active',el.dataset.dispatchFilter===dispatchFilter));
 stableHTML($('#dispatch-plan'),html.rows);
 stableHTML($('#dispatch-pagination'),controlsHTML('dispatch-plan',{page:data.page,pages:data.pages,size:data.page_size,total:data.total,start:data.start,end:data.end},{unit:i18n('项')}));
}
async function loadDispatch(){
 const seq=++dispatchSequence,view=pageView('dispatch-plan',dispatchData?.total||0,{rowHeight:88});
 const query=new URLSearchParams({page:String(view.page),page_size:String(view.size),status:dispatchFilter});
 try{
  const data=await api(`/api/tasks/${id}/dispatch-plan?${query}`);if(seq!==dispatchSequence)return;
  dispatchData=data;getPager('dispatch-plan').page=data.page;
  renderDispatch();pageView('dispatch-plan',data.total,{rowHeight:88});
 }catch(error){if(seq===dispatchSequence){$('#dispatch-notice').textContent=i18n`派单计划读取失败：${error.message}`;if(!dispatchData)$('#dispatch-plan').textContent=i18n('暂时无法读取派单，请稍后重试。');}}
}
function render(t){
 task=t;$('#detail-title').textContent=t.title;$('#detail-project').textContent=t.project_cwd||t.cwd;
 const origin=$('#detail-migrated-from');
 origin.hidden=!t.migrated_from;
 if(t.migrated_from)origin.href=`/tasks/${encodeURIComponent(t.migrated_from)}`;
 const followUp=$('#detail-migrated-to');
 followUp.hidden=!t.migrated_to;
 if(t.migrated_to)followUp.href=`/tasks/${encodeURIComponent(t.migrated_to)}`;
 $('#detail-open-direction').dataset.direction=id;$('#detail-open-direction').disabled=!t.can_direct;
 $('#detail-open-requirement-queue').dataset.requirementQueue=id;
 $('#detail-open-requirement-queue').disabled=!t.can_queue_requirements;
 $('#detail-actions').innerHTML=`${badge(t)}${taskButtons(t)}`;
 stableHTML($('#detail-current-state'),taskNarrativeHTML(t));
 if(!previewChanging)renderPreview(t.local_preview);
 if(section==='home'){
  renderUsage(t.health.usage||{},t.time_estimate||{});
  renderWorkflow(t,{role});
 }
 if(section==='conclusions')renderConclusions(t,{groupByRound:true});
 if(section==='git')stableHTML($('#detail-git'),gitHistoryHTML(t));
 if(section==='requirements')renderRequirements(t);
 const alerts=[t.control_message,t.resume_unavailable,t.direction_error,t.health.report_warning,t.launch_error].filter(Boolean);
 if(t.health.health==='heartbeat_stale')alerts.push(i18n('监督器心跳已超时，请核对 watcher 工作记录。'));
 if(t.status==='interrupted')alerts.push(i18n('任务执行已中断，可查看工作记录后从检查点恢复。'));
 if($('#detail-alerts'))$('#detail-alerts').innerHTML=failureHTML(t)+alerts.map(a=>`<p class="detail-alert">${escape(a)}</p>`).join('');
 renderFieldHandoff(t);
 renderBrowserCaptcha(t);
}
function renderUsage(usage,estimate){
 const stages=usage.stage_elapsed_s||{};
 const meter=usage.provider_reported||{};
 const count=new Intl.NumberFormat(locale());
 const usd=new Intl.NumberFormat(locale(),{style:'currency',currency:'USD',maximumFractionDigits:6});
 const fact=(label,value)=>`<div><dt>${escape(i18n(label))}</dt><dd>${escape(value)}</dd></div>`;
 const hasRuntime=Number.isFinite(usage.elapsed_s);
 const budgetLeft=hasRuntime&&usage.max_runtime_s>0?Math.max(0,usage.max_runtime_s-usage.elapsed_s):null;
 const estimated=estimate.status==='estimated';
 const approximate=seconds=>i18n`约 ${duration(Math.ceil(seconds/60)*60)}`;
 let timingNote='';
 if(estimated){
  timingNote=i18n`按累计运行耗时和已验收的 ${count.format(estimate.accepted_assignments)} 项派单估算，当前计划剩余 ${count.format(estimate.remaining_assignments)} 项。计划变更或外部阻塞会影响实际用时。`;
  if(budgetLeft!==null&&estimate.remaining_s>budgetLeft){
   timingNote+=' '+i18n`按当前速度预计超出剩余运行预算 ${approximate(estimate.remaining_s-budgetLeft)}。`;
  }
 }else if(estimate.status==='plan_pending'){
  timingNote=i18n('完整派单计划就绪后可估算耗时。');
 }else if(estimate.status==='insufficient_history'){
  timingNote=i18n('累计至少验收 3 项派单后可按实际速度估算耗时。');
 }
 const note=$('#detail-timing-note');
 note.textContent=timingNote;
 note.hidden=!timingNote;
 const reported=(value,missing,format)=>{
  const unknown=missing??meter.unreported_turns;
  if(!Number.isFinite(value))return '—';
  const partial=unknown>0?i18n`${count.format(unknown)} 轮未上报`:'';
  const recorded=(meter.reported_turns||0)+(meter.unreported_turns||0);
  if(recorded<=(unknown||0))return partial||'—';
  const amount=format.format(value);
  return partial?amount+' · '+partial:amount;
 };
 stableHTML($('#detail-usage'),[
  fact('累计运行耗时',duration(usage.elapsed_s)),
  ...(estimate.status==='complete'?[]:[
   fact('当前计划预计总耗时',estimated?approximate(estimate.total_s):'—'),
   fact('当前计划预计剩余耗时',estimated?approximate(estimate.remaining_s):'—'),
  ]),
  fact('剩余运行预算',budgetLeft!==null?duration(budgetLeft)+' / '+duration(usage.max_runtime_s):
   usage.max_runtime_s===0?i18n('不限'):'—'),
  fact('Worker 耗时',duration(stages.worker)),
  fact('Reviewer 耗时',duration(stages.reviewer)),
  fact('验收命令耗时',duration(stages.checks)),
  fact('模型调用',Number.isFinite(usage.agent_calls)?
   count.format(usage.agent_calls)+(usage.max_agent_calls?' / '+count.format(usage.max_agent_calls):''):'—'),
  fact('后端已报告 token',reported(meter.tokens,meter.unknown_token_turns,count)),
  fact('后端报告费用（保守累计）',reported(meter.cost_usd,meter.unknown_cost_turns,usd)),
 ].join(''));
}
function deliveryDiagnosis(t){
 const d=t.delivery_diagnosis;if(!d?.code)return '';
 const labels={implementation_not_started:i18n('实现尚未开始'),watcher_gate_rejected:i18n('watcher 未通过验收门禁'),
  no_comparable_review:i18n('没有针对同一候选及范围的有效审核'),worker_reviewer_disagree:i18n('worker 与 reviewer 结论不一致'),
  both_incomplete:i18n('worker 与 reviewer 均认为尚未完成'),candidate_delivered_remaining_work_pending:i18n('该候选已交付，仍有后续工作'),
  function_accepted_remaining_work_pending:i18n('当前派单已验收，仍有后续工作'),joint_claim_delivery_unconfirmed:i18n('双方确认，交付尚未证实')};
 const role=(name,r)=>'<li><strong>'+name+'</strong>'+i18n('：')+(r?.index?i18n`第 ${escape(r.index)} 轮`+' · '+escape(r.task_status||r.status||i18n('未记录'))+' · '+escape(r.summary||''):i18n('无结论'))+'</li>';
 return i18nHTML('<div class="detail-alert"><strong>到时交付诊断：')+escape(labels[d.code]||d.code)+'</strong><ul>'+
  role('Worker',d.worker)+role('Reviewer',d.reviewer)+'<li><strong>Watcher</strong>'+i18n('：')+
  escape((d.watcher?.reasons||[]).join(i18n('；'))||(d.watcher?.published?i18n('已记录提交'):d.watcher?.checks?.length?
   d.watcher.checks.map(c=>c.status).join(i18n('、')):i18n('无可用于确认交付的同轮验收记录')))+'</li></ul></div>';
}
function renderRequirements(t){
 const task=t.function_tasks?.current;
 const functions=t.function_tasks?.accepted||[];
 const functionHTML=t.function_dispatch?i18nHTML('<h3>开发任务单</h3><p>已通过 ')+escape(functions.length)+i18nHTML(' 项派单验收</p>')+
  (task?'<p><strong>'+escape(task.id)+'</strong> · '+(task.kind==='integration'?i18n('集成任务 · ')+escape(task.integration_scope):
     (task.kind==='verification'?i18n('纯验收 · '):'')+escape(task.file)+' · '+escape(task.symbol))+'</p><p>'+escape(task.objective)+'</p>'+
   (task.kind==='verification'?i18nHTML('<p>核对已有实现，不要求新增代码改动；双方只读检查，Watcher 执行固定验收。</p>'):'')+
   (task.kind==='integration'?'<p>'+escape(task.atomic_reason)+'</p><ul>'+task.targets.map(target=>'<li>'+
     escape(target.file)+' · '+escape(target.symbol)+'</li>').join('')+'</ul>':'')+
   (task.criterion_ids?i18nHTML('<p>关联验收条目：')+escape(task.criterion_ids.join(i18n('、')))+i18n('；待执行检查：')+escape(task.check_ids.join(i18n('、')))+'</p>':''):
   i18nHTML('<p>等待 reviewer 下发或任务已结束。</p>'))+
  (functions.length?pagedList('accepted-functions',functions,item=>'<li><span>'+escape(item.task.id)+
    (item.task.kind==='integration'?i18n(' · 集成任务（')+escape(item.task.targets.length)+i18n(' 个函数）'):
     item.task.kind==='verification'?i18n(' · 纯验收'):'')+'</span><span>'+
    escape((item.task.criterion_ids||[]).join(i18n('、')))+'</span><span>'+
    escape(item.verification?Object.entries(item.verification).map(([id,log])=>id+i18n(' 通过（')+log+i18n('）')).join(i18n('、')):i18n('历史语义审核'))+'</span></li>'):''):'';
 const coverage=t.requirement_coverage;
 const coverageHTML=coverage?.requirements?.length?i18nHTML`<h3>上次需求证据覆盖检查</h3><p>此处为历史检查，不代表当前交付确认。核对版本：${escape(coverage.tree||i18n('未记录'))}；${coverage.complete?i18n('已满足结构化覆盖门禁'):i18n('尚有缺口')}</p>${pagedList('requirement-coverage',coverage.requirements,r=>`<li><span>${escape(r.id)}</span><span>${escape(r.text)} · ${escape((r.evidence||[]).join(i18n('、')))}</span><span>${r.covered?i18n('已覆盖'):i18n('待核对')}</span></li>`)} `:'';
 const brief=t.requirements_review;
 const revision=t.requirement_revision;
 const revisionHTML=revision?.version?'<h3>'+i18n('当前需求版本')+'</h3><p>V'+escape(revision.version)+' · '+escape(revision.sha256.slice(0,12))+'</p><p>'+i18n('后续明确的用户变更覆盖此前冲突的要求；其他要求和验收检查继续保留。')+'</p>'+
  pagedList('requirement-revisions',revision.amendments||[],r=>'<li><span>V'+escape(r.version)+'</span><span>'+escape((t.directions||[]).find(d=>d.id===r.direction_id)?.text||r.direction_id)+'</span></li>'):'';
 const parallelHTML=t.parallel_project?i18nHTML`<h3>同工程并行任务</h3><p>原工程：${escape(t.project_cwd)}</p><p>独立工作副本：${escape(t.cwd)}</p><p>交付分支：${escape(t.task_branch)}</p><p>负责范围：${escape((t.task_scope||[]).join(i18n('、'))||i18n('按任务需求确定'))}</p><p>前置任务：${escape((t.task_dependencies||[]).join(i18n('、'))||i18n('无'))}</p><p>共享输入变化由 watcher 检测并交 reviewer 复核；任务分支保留独立交付记录，不覆盖原工作区。</p>`:'';
 const briefHTML=revisionHTML+parallelHTML+requirementReviewHTML(brief);
 const plan=t.acceptance_plan,results=t.acceptance_results;
 const checkLabels={passed:i18n('检查通过'),check_failed:i18n('检查未通过（需核对输出）'),environment_unavailable:i18n('运行环境不可用'),timed_out:i18n('检查超时'),interrupted:i18n('检查被中断')};
 const acceptanceHTML=plan?.criteria?i18nHTML`<h3>完成标准</h3><div class="requirement-text">${escape(plan.criteria)}</div><p>${t.verification_mode==='planning'?i18n('本任务仅制定验收方案，不代表原目标验收。'):t.verification_mode==='review_only'?i18n('仅语义审核：未提供运行测试通过证明。'):i18n('检查通过仍须结合独立审核、模块及审计门禁。')}</p><h3>选定检查</h3>${pagedList('acceptance-selected',plan.selected||[],c=>`<li><span>${escape(c.title)}</span><span>${escape(c.purpose)}</span></li>`,{empty:i18nHTML('<li>未选择项目自动检查；如有高级命令，结果在下面单列。</li>')})}`:'';
 const resultHTML=results?.checks?i18nHTML`<h3>上次实际检查结果 · 第 ${escape(results.round)} 轮</h3><p>这是检查快照，不代表当前版本已经最终交付。版本：${escape(results.tree)}${results.source_changed?i18n('；检查改变源码，原审核证据已失效。'):''}${results.invalidated?i18n('；业务版本已更新，此结果需要重新验收。'):''}</p>${pagedList('acceptance-results',results.checks,c=>i18nHTML`<details><summary>${stateLabel(c.status==='passed'?'complete':c.status==='check_failed'?'error':'attention',checkLabels[c.status]||c.status)} · ${escape((plan?.selected||[]).find(x=>x.command===c.command)?.title||i18n('验收检查'))}</summary><pre class="requirement-text">${escape(c.command)}</pre><p>退出码 ${escape(c.exit_code)}；输出见原始日志 ${escape(c.log)}</p></details>`,{tag:'div',className:'acceptance-results'})}`:'';
 const modules=pagedList('modules',t.modules||[],m=>`<li><span class="module-id">${escape(m.id)}</span><span>${escape(m.title)}</span><span>${stateLabel(m.status==='accepted'?'complete':'waiting',m.status==='accepted'?i18n('已验收'):m.invalidated_reason?.startsWith('Revalidation')?i18n('待复核（历史已验收）'):i18n('待完成'))}</span></li>`);
 stableHTML($('#detail-body'),i18nHTML`${deliveryDiagnosis(t)}${functionHTML}<h3>原始开发需求</h3><div class="requirement-text">${escape(t.requirement)}</div>${t.modules?.length?i18nHTML`<h3>模块验收 · ${t.accepted_modules} / ${t.total_modules}</h3>${modules}`:''}${briefHTML}${t.acceptance_warning?`<p class="detail-alert">${escape(t.acceptance_warning)}</p>`:''}${acceptanceHTML}${resultHTML}${coverageHTML}<h3>审计问题</h3>${pagedList('audit-issues',t.audit_issues?.items||[],i=>`<li><span class="module-id">${escape(i.id)}</span><span>${escape(i.description||i.text||i.summary||'')}</span><span>${stateLabel(['closed','resolved'].includes(i.status)?'complete':'attention',['closed','resolved'].includes(i.status)?i18n('已关闭'):i.status==='needs_review'?i18n('待复核（保留关闭证据）'):i18n('待解决'))}</span></li>`,{empty:i18nHTML('<li>暂无审计问题</li>')})}<h3>运行记录目录</h3><div class="requirement-text">${escape(t.run_dir)}</div>`);
}
async function loadLogs(){
 const seq=++logSequence;
 try{
  const pager=getPager('logs'),view=pageView('logs',logTotal,{rowHeight:20});
  const query=new URLSearchParams({page:$('#log-follow').checked?'0':String(pager.page),page_size:String(view.size)});
  if(log)query.set('file',log);
  const data=await api(`/api/tasks/${id}/logs?${query}`);
  if(seq!==logSequence)return;
  $('#log-select').innerHTML=data.files.length?data.files.map(n=>`<option value="${escape(n)}">${escape(n)}</option>`).join(''):i18nHTML('<option>暂无日志</option>');
  $('#log-select').value=data.selected;log=data.selected||'';
  $('#log-full').hidden=!log;$('#log-full').href=fullLogURL(id,log);
  const output=$('#log-output'),scroll=output.scrollTop;
  output.textContent=data.content||i18n('暂无执行日志。');output.scrollTop=$('#log-follow').checked?output.scrollHeight:scroll;
  logTotal=data.total_lines;pager.page=data.page;
  $('#logs-pagination').innerHTML=controlsHTML('logs',pageView('logs',logTotal,{rowHeight:20}),{unit:i18n('行')});
 }catch(error){if(error.status===404&&log){log='';return loadLogs();}throw error;}
}
export function mount(){
 $('#browser-captcha-image')?.addEventListener('error',()=>{
  const error=$('#browser-captcha-error');
  error.textContent='验证码图片未加载，请等待新的验证码或刷新任务详情。';
  error.hidden=false;
 });
 $('#detail-field-handoff')?.addEventListener('click',async event=>{
  if(event.target.closest('[data-browser-captcha]')&&task)renderBrowserCaptcha(task,true);
  const button=event.target.closest('[data-browser-retry]');
  const handoff=task?.field_handoff;
  if(!button||!handoff)return;
  button.disabled=true;
  try{
   await api(`/api/tasks/${encodeURIComponent(id)}/field-handoff`,{
    action:'retry_browser_captcha',id:button.dataset.browserRetry,binding:handoff.binding,revision:handoff.revision
   });
   toast('浏览器验证已接续，请等待新的验证码。');
  }catch(error){toast(error.message,true);}
  finally{button.disabled=false;}
 });
 $('#browser-captcha-form')?.addEventListener('submit',async event=>{
  event.preventDefault();
  const handoff=task?.field_handoff,challenge=handoff?.browser_inputs?.[shownCaptcha];
  if(!challenge||captchaSubmitting)return;
  const form=event.currentTarget,error=$('#browser-captcha-error');
  captchaSubmitting=true;
  try{
   await api(`/api/tasks/${encodeURIComponent(id)}/field-handoff`,{
    action:'browser_captcha',id:challenge.id,binding:handoff.binding,revision:handoff.revision,
    nonce:challenge.nonce,code:new FormData(form).get('code')
   });
   $('#browser-captcha-dialog').close();
   toast('验证码已提交，浏览器将继续登录。');
  }catch(exc){error.textContent=exc.message;error.hidden=false;}
  finally{captchaSubmitting=false;void refresh();}
 });
 $('#detail-local-preview')?.addEventListener('click',async event=>{
  const button=event.target.closest('[data-preview-action]');
  if(!button)return;
  if(button.dataset.previewAction==='start'&&task?.local_preview?.status==='needs_input'){
   showPreviewPrompt(task.local_preview);return;
  }
  try{await changePreview({action:button.dataset.previewAction});}
  catch(error){toast(error.message,true);}
 });
 $('#preview-backend-form')?.addEventListener('submit',async event=>{
  event.preventDefault();
  if(previewChanging)return;
  const form=event.currentTarget,error=$('#preview-backend-error');
  error.hidden=true;
  form.querySelector('[type=submit]').disabled=true;
  try{
   await changePreview({action:'start',backend:new FormData(form).get('backend')});
   if(task?.local_preview?.status!=='needs_input')$('#preview-backend-dialog').close();
  }catch(exc){error.textContent=exc.message;error.hidden=false;}
  finally{form.querySelector('[type=submit]').disabled=false;}
 });
 const supervision=$('#task-supervision');
 if(supervision){
  const setupFailed=new URLSearchParams(location.search).get('supervision')==='setup-failed';
  let mounted=false;
  const open=()=>{
   if(!supervision.open||mounted)return;
   mounted=true;
   mountSupervision(`/api/tasks/${id}/supervision`,{setupFailed});
  };
  supervision.addEventListener('toggle',open);
  if(setupFailed){supervision.open=true;open();}
 }
 onPaginationChange((key,reason)=>{if(key==='dispatch-plan'){void loadDispatch();}else if(key==='logs'){if(reason==='page')$('#log-follow').checked=false;loadLogs().catch(e=>toast(e.message,true));}else if(task)render(task);});
 $('#dispatch-filters')?.addEventListener('click',event=>{const button=event.target.closest('[data-dispatch-filter]');if(!button)return;dispatchFilter=button.dataset.dispatchFilter;resetPage('dispatch-plan');dispatchData=null;void loadDispatch();});
 document.addEventListener('click',e=>{const el=e.target.closest('[data-role-filter],[data-role-focus]');if(!el)return;role=el.dataset.roleFilter||el.dataset.roleFocus;resetPage('workflow');if(task)renderWorkflow(task,{role});if(el.dataset.roleFocus)$('#workflow-stages').scrollIntoView({behavior:globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});});
 $('#log-select')?.addEventListener('change',e=>{log=e.target.value;resetPage('logs');logTotal=0;loadLogs().catch(e=>toast(e.message,true));});
 $('#log-follow')?.addEventListener('change',()=>loadLogs().catch(e=>toast(e.message,true)));
 watch(async()=>{
  const queueJob=section==='queue'?api(`/api/tasks/${id}/requirement-queue`).catch(error=>({error:error.message})):null;
  const dispatchJob=section==='queue'?loadDispatch():null;
  render(await api(`/api/tasks/${id}`));
  if(queueJob){
   const value=await queueJob;
   if(value.error){$('#detail-queue').textContent=i18n`需求队列读取失败：${value.error}`;$('#queue-count').textContent='—';}
   else {const {requirementQueueMarkup}=await import('/requirement-queue.js');stableHTML($('#detail-queue'),requirementQueueMarkup(value));$('#queue-count').textContent=i18n`${value.waiting.length} 条等待`;}
  }
  if(section==='logs')await loadLogs();
  if(dispatchJob)await dispatchJob;
 });
}
