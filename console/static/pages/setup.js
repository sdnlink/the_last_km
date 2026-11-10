import {$,$$,escape,stableHTML} from '/ui.js';
import {api,config,watch,toast} from '/core.js';
import {pagedList,onPaginationChange} from '/pagination.js';

let loaded=false,busy=false;
const jobs=new Map();
function cardHTML(agent){
 const c=agent.config;
 const protocol=agent.id==='kimi'?`<label>接口类型<select name="provider_type">${['kimi','openai','anthropic'].map(type=>`<option value="${type}" ${c.provider_type===type?'selected':''}>${type}</option>`).join('')}</select></label>`:'';
 return `<fieldset class="agent-card ${agent.enabled?'selected':''}" data-agent="${agent.id}"><div class="agent-card-head"><label class="agent-select"><input type="checkbox" name="enabled" ${agent.enabled?'checked':''}/>${escape(agent.label)}</label><span class="agent-detected ${agent.installed?'installed':''}" data-status>${agent.installed?'已检测到 CLI':'尚未安装'}</span></div>${agent.note?`<p class="agent-note">${escape(agent.note)}</p>`:''}${!agent.can_review?'<p class="agent-note">可作为 worker；独立审核请选择支持只读模式的执行器。</p>':''}<label>默认模型<input name="model" value="${escape(c.model)}" maxlength="200" placeholder="留空使用执行器默认模型" /></label>${agent.url_env?`<label>服务地址<input name="endpoint" type="url" value="${escape(c.endpoint)}" placeholder="沿用本机配置" /></label>`:''}${protocol}<label>认证方式<select name="auth_mode"><option value="existing" ${agent.auth_mode==='existing'?'selected':''}>沿用本机 CLI 登录</option>${agent.key_env?`<option value="api_key" ${agent.auth_mode==='api_key'?'selected':''}>API Key / Token</option>`:''}</select></label>${agent.key_env?`<label data-key-field ${agent.auth_mode!=='api_key'?'hidden':''}>API Key / Token<input name="api_key" type="password" autocomplete="new-password" placeholder="${agent.api_key_set?'已保存，留空保留原值':'填写密钥，保存后不回显'}" /><small>${escape(agent.key_env)}</small><button type="button" data-clear-key>清除已保存密钥</button></label>`:`<small>在终端运行 ${escape(agent.binary)}，按官方引导完成登录。</small>`}<details><summary class="agent-note">可执行文件位置</summary><label>自定义 CLI 路径<input name="executable" value="${escape(c.executable)}" placeholder="${escape(agent.detected_executable||'自动检测 PATH')}" /><small>留空自动检测；填写时使用绝对路径。</small></label></details><div class="agent-card-actions">${agent.install_method!=='manual'?`<button type="button" class="button secondary small-button" data-install>${agent.installed?'检查安装':'安装 CLI'}</button>`:''}<button type="button" class="button secondary small-button" data-probe>检测 CLI</button><button type="button" class="button secondary small-button" data-save-agent>保存此项</button><a href="${escape(agent.docs)}" target="_blank" rel="noopener noreferrer">官方安装与登录 ↗</a></div><p class="agent-message" data-message></p></fieldset>`;
}
function values(card){
 const value=name=>card.querySelector(`[name="${name}"]`)?.value||'';
 return {id:card.dataset.agent,enabled:card.querySelector('[name=enabled]').checked,model:value('model'),
  endpoint:value('endpoint'),executable:value('executable'),auth_mode:value('auth_mode'),api_key:value('api_key'),
  clear_api_key:card.dataset.clearKey==='true',...(card.dataset.agent==='kimi'?{provider_type:value('provider_type')}:{})};
}
async function save(cards,complete=false){
 const result=await api('/api/agents/save',{agents:cards.map(values),complete});
 for(const card of cards){
  const key=card.querySelector('[name=api_key]');
  if(key){key.value='';key.placeholder=result.agents.find(a=>a.id===card.dataset.agent).api_key_set?'已保存，留空保留原值':'填写密钥，保存后不回显';}
  delete card.dataset.clearKey;
 }
 await config(true);return result;
}
function renderJobs(rows){
 for(const row of rows)jobs.set(row.id,row);
 $('#setup-jobs-panel').hidden=jobs.size===0;
 stableHTML($('#setup-jobs'),pagedList('setup-jobs',[...jobs.values()].reverse(),j=>`<details class="setup-job" data-key="job-${j.id}" ${j.status==='failed'?'open':''}><summary>${escape(j.agent)} · ${{queued:'等待中',running:'安装中',succeeded:'已完成',failed:'未完成'}[j.status]} · ${escape(j.message)}</summary><pre>${escape(j.output||'等待安装程序输出…')}</pre></details>`,{tag:'div',className:'setup-job-list',rowHeight:64}));
 const active=new Set([...jobs.values()].filter(j=>['queued','running'].includes(j.status)).map(j=>j.agent));
 $$('[data-agent]').forEach(card=>{const button=card.querySelector('[data-install]');if(button)button.disabled=active.has(card.dataset.agent);});
}
async function load(){
 const data=await api('/api/agents/setup');
 if(!loaded){$('#setup-agents').innerHTML=data.agents.map(cardHTML).join('');loaded=true;$('#setup-title').textContent=data.initialized?'管理已安装的 Agent':'初始化：选择你的 Agent';}
 for(const agent of data.agents){const status=$(`[data-agent="${agent.id}"] [data-status]`);status.textContent=agent.installed?'已检测到 CLI':'尚未安装';status.classList.toggle('installed',agent.installed);}
 renderJobs(data.jobs);
}
export function mount(){
 onPaginationChange(key=>{if(key==='setup-jobs')renderJobs([]);});
 $('#agent-setup-form').addEventListener('submit',e=>e.preventDefault());
 $('#setup-agents').addEventListener('input',e=>{if(e.target.name==='api_key')delete e.target.closest('[data-agent]').dataset.clearKey;});
 $('#setup-detect').addEventListener('click',()=>load().catch(e=>toast(e.message,true)));
 $('#setup-install-selected').addEventListener('click',async()=>{
  if(busy)return;busy=true;$('#setup-error').hidden=true;
  const selected=$$('[data-agent]').filter(card=>card.querySelector('[name=enabled]').checked);
  for(const button of $$('.setup-actions button'))button.disabled=true;
  try{
   const saved=await save(selected),failed=[];
   for(const card of selected){
    const agent=saved.agents.find(a=>a.id===card.dataset.agent);if(agent.installed)continue;
    if(agent.install_method==='manual'){card.querySelector('[data-message]').textContent='请打开官方页面安装，再检测 CLI。';continue;}
    const job=await api('/api/agents/install',{id:agent.id});renderJobs([job]);
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
 $('#setup-agents').addEventListener('change',e=>{
  const card=e.target.closest('[data-agent]');if(!card)return;
  card.classList.toggle('selected',card.querySelector('[name=enabled]').checked);
  const field=card.querySelector('[data-key-field]');if(field)field.hidden=card.querySelector('[name=auth_mode]').value!=='api_key';
 });
 $('#setup-agents').addEventListener('click',async e=>{
  const button=e.target.closest('[data-install],[data-probe],[data-save-agent],[data-clear-key]');if(!button||button.disabled)return;
  const card=button.closest('[data-agent]'),message=card.querySelector('[data-message]');
  if(button.hasAttribute('data-clear-key')){card.dataset.clearKey='true';card.querySelector('[name=api_key]').value='';message.textContent='保存后清除密钥。';return;}
  button.disabled=true;message.textContent='正在处理…';
  try{
   await save([card]);
   if(button.hasAttribute('data-install')){
    const job=await api('/api/agents/install',{id:card.dataset.agent});renderJobs([job]);message.textContent=job.message;
   }else if(button.hasAttribute('data-probe')){
    const result=await api('/api/agents/probe',{id:card.dataset.agent});message.textContent=result.message;
   }else message.textContent='配置已保存。';
  }catch(error){message.textContent=error.message;}
  finally{button.disabled=false;}
 });
 watch(load);
}
