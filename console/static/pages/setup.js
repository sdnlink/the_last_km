import {$,$$,escape,stableHTML} from '/ui.js';
import {api,config,watch,toast} from '/core.js';
import {pagedList,onPaginationChange} from '/pagination.js';

let loaded=false,busy=false;
const jobs=new Map();
const jobLabels={queued:'等待中',running:'安装中',succeeded:'已完成',failed:'未完成'};
const agentLabels=new Map();
function cardHTML(agent){
 const c=agent.config;
 const protocol=agent.id==='kimi'?`<label>接口类型<select name="provider_type">${['kimi','openai','anthropic'].map(type=>`<option value="${type}" ${c.provider_type===type?'selected':''}>${type}</option>`).join('')}</select></label>`:'';
 const label=escape(agent.label),id=escape(agent.id);
 return `<fieldset class="agent-card ${agent.enabled?'selected':''}" data-agent="${id}">
  <div class="agent-card-head"><span class="agent-avatar" aria-hidden="true">${escape(agent.label.slice(0,2).toUpperCase())}</span><div><button type="button" class="agent-name" data-configure aria-haspopup="dialog" aria-controls="config-${id}">${label}<svg><use href="#i-arrow"/></svg></button><span class="agent-role">${agent.can_review?'Worker · Reviewer':'Worker'}</span></div><label class="agent-select"><input type="checkbox" name="enabled" aria-label="启用 ${label}" ${agent.enabled?'checked':''}/><span>启用</span></label></div>
  <div class="agent-card-summary"><span class="agent-detected ${agent.installed?'installed':''}" data-status>${agent.installed?'已检测到 CLI':'尚未安装'}</span><span class="agent-draft" data-draft hidden>有未保存配置</span></div>
  <button type="button" class="agent-model-summary" data-configure><span>默认模型</span><strong data-model-summary>${escape(c.model||'执行器默认')}</strong><span aria-hidden="true">›</span></button>
  <div class="agent-summary-bottom"><span data-auth-summary>${agent.auth_mode==='api_key'?'API Key / Token':'沿用 CLI 登录'}</span><button type="button" data-configure class="agent-settings-link">配置模型与认证</button></div>
  <dialog class="agent-config-dialog" id="config-${id}" aria-labelledby="config-title-${id}"><div class="dialog-heading"><div><span class="eyebrow">AGENT CONFIGURATION</span><h2 id="config-title-${id}">${label}</h2><p>${agent.can_review?'可用于实现与独立审核':'可作为 Worker；审核请选择支持只读模式的执行器'}</p></div><button type="button" class="icon-button" data-close-agent aria-label="关闭 ${label} 配置"><svg><use href="#i-x"/></svg></button></div>
  <div class="agent-config-body">${agent.note?`<p class="agent-note">${escape(agent.note)}</p>`:''}<div class="agent-config-grid"><label>默认模型<input name="model" value="${escape(c.model)}" maxlength="200" placeholder="留空使用执行器默认模型" /></label><label>认证方式<select name="auth_mode"><option value="existing" ${agent.auth_mode==='existing'?'selected':''}>沿用本机 CLI 登录</option>${agent.key_env?`<option value="api_key" ${agent.auth_mode==='api_key'?'selected':''}>API Key / Token</option>`:''}</select></label>${agent.url_env?`<label class="agent-config-wide">服务地址<input name="endpoint" type="url" value="${escape(c.endpoint)}" placeholder="沿用本机配置" /></label>`:''}${protocol}
  ${agent.key_env?`<label class="agent-config-wide" data-key-field ${agent.auth_mode!=='api_key'?'hidden':''}>API Key / Token<input name="api_key" type="password" autocomplete="new-password" placeholder="${agent.api_key_set?'已保存，留空保留原值':'填写密钥，保存后不回显'}" /><small>${escape(agent.key_env)}</small><button type="button" data-clear-key>清除已保存密钥</button></label>`:`<p class="agent-note agent-config-wide">在终端运行 ${escape(agent.binary)}，按官方引导完成登录。</p>`}</div>
  <details class="agent-executable"><summary>高级设置 · 可执行文件位置</summary><label>自定义 CLI 路径<input name="executable" value="${escape(c.executable)}" placeholder="${escape(agent.detected_executable||'自动检测 PATH')}" /><small>留空自动检测；填写时使用绝对路径。</small></label></details>
  <div class="agent-cli-tools">${agent.install_method!=='manual'?`<button type="button" class="button secondary small-button" data-install>${agent.installed?'检查安装':'安装 CLI'}</button>`:''}<button type="button" class="button secondary small-button" data-probe>检测 CLI</button><a href="${escape(agent.docs)}" target="_blank" rel="noopener noreferrer">官方安装与登录 ↗</a></div><p class="agent-message" data-message role="status"></p></div>
  <div class="dialog-footer"><span class="agent-config-hint">关闭可保留草稿，保存后生效</span><button type="button" class="button secondary" data-close-agent>关闭</button><button type="button" class="button primary" data-save-agent>保存配置</button></div></dialog>
 </fieldset>`;
}
function values(card){
 const value=name=>card.querySelector(`[name="${name}"]`)?.value||'';
 return {id:card.dataset.agent,enabled:card.querySelector('[name=enabled]').checked,model:value('model'),
  endpoint:value('endpoint'),executable:value('executable'),auth_mode:value('auth_mode'),api_key:value('api_key'),
  clear_api_key:card.dataset.clearKey==='true',...(card.dataset.agent==='kimi'?{provider_type:value('provider_type')}:{})};
}
function updateSummary(card,dirty=false){
 card.classList.toggle('selected',card.querySelector('[name=enabled]').checked);
 card.querySelector('[data-model-summary]').textContent=card.querySelector('[name=model]').value||'执行器默认';
 card.querySelector('[data-auth-summary]').textContent=card.querySelector('[name=auth_mode]').value==='api_key'?'API Key / Token':'沿用 CLI 登录';
 if(dirty)card.querySelector('[data-draft]').hidden=false;
 const field=card.querySelector('[data-key-field]');if(field)field.hidden=card.querySelector('[name=auth_mode]').value!=='api_key';
}
async function save(cards,complete=false){
 const result=await api('/api/agents/save',{agents:cards.map(values),complete});
 for(const card of cards){
  const key=card.querySelector('[name=api_key]');
  if(key){key.value='';key.placeholder=result.agents.find(a=>a.id===card.dataset.agent).api_key_set?'已保存，留空保留原值':'填写密钥，保存后不回显';}
  delete card.dataset.clearKey;card.querySelector('[data-draft]').hidden=true;updateSummary(card);
 }
 await config(true);return result;
}
function jobHTML(job){
 const active=['queued','running'].includes(job.status);
 return `<article class="setup-job ${escape(job.status)}"><div class="setup-job-heading"><span class="install-status" aria-hidden="true">${job.status==='succeeded'?'✓':job.status==='failed'?'!':'↻'}</span><strong>${escape(agentLabels.get(job.agent)||job.agent)}</strong><span class="job-state">${escape(jobLabels[job.status]||job.status)}</span><time>${escape((job.started_at||'').replace('T',' ').slice(0,16))}</time></div><p class="job-message">${escape(job.message||'')}</p>${active?'<div class="install-progress" aria-label="安装处理中，尚无百分比进度"><span></span></div>':''}<details class="setup-job-output" data-key="job-${escape(job.id)}"><summary>查看安装输出</summary><pre>${escape(job.output||'等待安装程序输出…')}</pre></details></article>`;
}
function renderJobs(rows,snapshot=false){
 if(snapshot)jobs.clear();
 for(const row of rows)jobs.set(row.id,row);
 $('#setup-jobs-panel').hidden=jobs.size===0;
 const recent=[...jobs.values()].reverse(),active=recent.filter(j=>['queued','running'].includes(j.status)),finished=recent.filter(j=>!['queued','running'].includes(j.status));
 $('#setup-jobs-title').textContent=active.length?'当前安装':'最近安装';
 $('#setup-jobs-summary').textContent=active.length?`${active.length} 项正在处理 · 可以继续配置其他 Agent，输出按需展开。`:`${finished.length} 项已结束 · 展开记录查看结果与日志。`;
 const history=finished.length?`<details class="setup-job-history" data-key="installation-history"><summary>最近安装记录 <span>${finished.length} 项 · ${finished.filter(j=>j.status==='failed').length} 项未完成</span></summary>${pagedList('setup-jobs',finished,jobHTML,{tag:'div',className:'setup-job-list',rowHeight:100})}</details>`:'';
 stableHTML($('#setup-jobs'),active.map(jobHTML).join('')+history);
 const installing=new Set(active.map(j=>j.agent));
 $$('[data-agent]').forEach(card=>{const button=card.querySelector('[data-install]');if(button)button.disabled=installing.has(card.dataset.agent);});
}
function revealJobs(){
 $('#setup-jobs-panel').scrollIntoView({behavior:'smooth',block:'start'});
 $('#setup-jobs-panel').focus({preventScroll:true});
}
async function load(){
 const data=await api('/api/agents/setup');
 for(const agent of data.agents)agentLabels.set(agent.id,agent.label);
 if(!loaded){$('#setup-agents').innerHTML=data.agents.map(cardHTML).join('');loaded=true;$('#setup-title').textContent=data.initialized?'管理你的 Agent':'初始化：选择你的 Agent';}
 for(const agent of data.agents){const status=$(`[data-agent="${agent.id}"] [data-status]`);status.textContent=agent.installed?'已检测到 CLI':'尚未安装';status.classList.toggle('installed',agent.installed);}
 $('#setup-count').textContent=`${data.agents.filter(a=>a.installed).length} / ${data.agents.length} 已检测到 CLI`;
 renderJobs(data.jobs,true);
}
export function mount(){
 onPaginationChange(key=>{if(key==='setup-jobs')renderJobs([]);});
 $('#agent-setup-form').addEventListener('submit',e=>e.preventDefault());
 $('#setup-agents').addEventListener('input',e=>{const card=e.target.closest('[data-agent]');if(!card)return;if(e.target.name==='api_key')delete card.dataset.clearKey;updateSummary(card,true);});
 $('#setup-agents').addEventListener('change',e=>{const card=e.target.closest('[data-agent]');if(card)updateSummary(card,true);});
 $('#setup-detect').addEventListener('click',()=>load().catch(e=>toast(e.message,true)));
 $('#setup-install-selected').addEventListener('click',async()=>{
  if(busy)return;busy=true;$('#setup-error').hidden=true;
  const selected=$$('[data-agent]').filter(card=>card.querySelector('[name=enabled]').checked);
  for(const button of $$('.setup-actions button'))button.disabled=true;
  try{
   const saved=await save(selected),failed=[];
   for(const card of selected){
    const agent=saved.agents.find(a=>a.id===card.dataset.agent);if(agent.installed)continue;
    if(agent.install_method==='manual'){toast(`${agent.label}：请打开官方页面安装，再检测 CLI。`,true);continue;}
    const job=await api('/api/agents/install',{id:agent.id});renderJobs([job]);revealJobs();
    while(['queued','running'].includes(jobs.get(job.id)?.status)){
     await new Promise(resolve=>setTimeout(resolve,1500));await load();
    }
    if(jobs.get(job.id)?.status==='failed')failed.push(agent.label);
   }
   toast(failed.length?'部分安装未完成：'+failed.join('、'):'选中项已处理，可继续配置并进入控制台。',!!failed.length);
  }catch(error){$('#setup-error').textContent=error.message;$('#setup-error').hidden=false;}
  finally{busy=false;for(const button of $$('.setup-actions button'))button.disabled=false;}
 });
 for(const [id,complete] of [['setup-save',false],['setup-complete',true]])$("#"+id).addEventListener('click',async()=>{
  if(busy)return;busy=true;$('#setup-error').hidden=true;
  for(const button of $$('.setup-actions button'))button.disabled=true;
  try{await save($$('[data-agent]'),complete);if(complete)location.assign('/overview');else toast('Agent 配置已保存');}
  catch(error){$('#setup-error').textContent=error.message;$('#setup-error').hidden=false;}
  finally{busy=false;for(const button of $$('.setup-actions button'))button.disabled=false;}
 });
 $('#setup-agents').addEventListener('click',async e=>{
  const button=e.target.closest('[data-configure],[data-close-agent],[data-install],[data-probe],[data-save-agent],[data-clear-key]');if(!button||button.disabled)return;
  const card=button.closest('[data-agent]'),dialog=card.querySelector('dialog'),message=card.querySelector('[data-message]');
  if(button.hasAttribute('data-configure')){dialog.showModal();return;}
  if(button.hasAttribute('data-close-agent')){dialog.close();return;}
  if(button.hasAttribute('data-clear-key')){card.dataset.clearKey='true';card.querySelector('[name=api_key]').value='';message.textContent='保存后清除密钥。';updateSummary(card,true);return;}
  button.disabled=true;message.textContent='正在处理…';
  try{
   await save([card]);
   if(button.hasAttribute('data-install')){
    const job=await api('/api/agents/install',{id:card.dataset.agent});renderJobs([job]);message.textContent=job.message;dialog.close();revealJobs();
   }else if(button.hasAttribute('data-probe')){
    const result=await api('/api/agents/probe',{id:card.dataset.agent});message.textContent=result.message;
   }else{message.textContent='配置已保存。';dialog.close();toast(`${agentLabels.get(card.dataset.agent)} 配置已保存`);}
  }catch(error){message.textContent=error.message;}
  finally{button.disabled=false;renderJobs([]);}
 });
 watch(load);
}
