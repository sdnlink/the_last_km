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
 const modules=(t.modules||[]).map(m=>`<li><span class="module-id">${escape(m.id)}</span><span>${escape(m.title)}</span><span>${m.status==='accepted'?'已验收':'待完成'}</span></li>`).join('');
 $('#detail-body').innerHTML=`<h3>原始开发需求</h3><div class="requirement-text">${escape(t.requirement)}</div>${modules?`<h3>模块验收 · ${t.accepted_modules} / ${t.total_modules}</h3><ul class="module-list">${modules}</ul>`:''}<h3>审计问题</h3><ul class="module-list">${(t.audit_issues?.items||[]).map(i=>`<li><span class="module-id">${escape(i.id)}</span><span>${escape(i.description||i.text||i.summary||'')}</span><span>${i.status==='closed'?'已关闭':'待解决'}</span></li>`).join('')||'<li>暂无审计问题</li>'}</ul><h3>运行记录目录</h3><div class="requirement-text">${escape(t.run_dir)}</div>`;
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
