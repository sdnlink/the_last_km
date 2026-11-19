import assert from 'node:assert/strict';
import {test} from 'node:test';
import {taskMotion,taskNarrative,rolePresentation,rolesHTML,badge,activityState} from '../console/static/ui.js';
import {roleDetailsHTML,renderWorkflow} from '../console/static/task-view.js';

const task=(status='running',phase='working')=>({status,health:{phase},activity:{is_running:true}});

test('terminal states override stale running roles and pending control requests',()=>{
 for(const [status,state] of [['paused','paused'],['stopped','idle'],['done','complete'],['failed','error'],['budget_exceeded','attention']]){
  const t={...task(status),control:'stop'};
  assert.equal(taskMotion(t),state);
  assert.equal(rolePresentation(t,'worker',{status:'running'}).state,state);
  assert.doesNotMatch(rolesHTML(t,{worker:{status:'running'}}),/data-motion="running"/);
  assert.doesNotMatch(roleDetailsHTML(t),/data-motion="running"/);
 }
 assert.deepEqual(rolePresentation(task('done'),'reviewer',{status:'disabled'}),{state:'idle',label:'未启用'});
});

test('waiting, control requests and stale heartbeat never show an active task spinner',()=>{
 for(const phase of ['retry_wait','waiting_for_input'])assert.equal(taskMotion(task('running',phase)),'waiting');
 for(const control of ['stop','pause'])assert.equal(taskMotion({...task(),control}),'waiting');
 const stale={...task(),health:{phase:'working',health:'heartbeat_stale'}};
 assert.equal(taskMotion(stale),'attention');
 assert.match(taskNarrative(stale).title,/无法确认/);
 // Output silence is not proof of a stalled process.
 assert.equal(taskMotion({...task(),activity:{output_age_s:900}}),'running');
});

test('narrative explains the current stage and what happens next in both languages',()=>{
 const previous=globalThis.document;
 try{
  for(const lang of ['zh-CN','en']){
   globalThis.document={documentElement:{lang}};
   for(const phase of ['working','reviewing','validating','publishing','refining_requirements','retry_wait']){
    const value=taskNarrative(task('running',phase));
    assert.ok(value.title&&value.next);
    if(lang==='en')assert.doesNotMatch(value.title+value.next,/[\u3400-\u9fff]/);
   }
  }
 }finally{globalThis.document=previous;}
});

test('only the current observed operation animates; historical commands retain their result',()=>{
 const command={id:'tool-1',role:'worker',round:2,status:'running'};
 const t={...task(),activity:{is_running:true,operation:command}};
 assert.equal(activityState(t,command),'running');
 assert.equal(activityState(t,{...command,id:'old'}),'idle');
 assert.equal(activityState(t,{...command,round:1}),'idle');
 assert.equal(activityState({...t,status:'paused'},command),'paused');
 assert.equal(activityState({...t,status:'stopped'},command),'idle');
 assert.equal(activityState(t,{...command,status:'completed',exit_code:0}),'complete');
 assert.equal(activityState(t,{...command,status:'completed',exit_code:1}),'error');
 assert.equal(activityState(t,{...command,status:'completed'}),'idle');
 assert.equal(activityState(t,{...command,status:'failed'}),'error');
 assert.equal(activityState({...t,activity:{is_running:false}},command),'idle');
 assert.equal(activityState(t,{status:'in_progress'},{plan:true}),'running');
});

test('paused and stopped workflow stages cannot retain live animation after polling',()=>{
 const previous={document:globalThis.document,window:globalThis.window,requestAnimationFrame:globalThis.requestAnimationFrame};
 const nodes=Object.fromEntries(['#workflow-stages','#detail-live'].map(id=>[id,{innerHTML:'',dataset:{},querySelectorAll:()=>[]}]));
 globalThis.document={querySelector:key=>nodes[key]||null,querySelectorAll:()=>[]};
 globalThis.window={innerHeight:800};globalThis.requestAnimationFrame=()=>{};
 try{
  for(const status of ['running','paused','stopped','failed','done']){
   renderWorkflow({...task(status),workflow:'review-first'},{showRoles:false});
   const html=nodes['#workflow-stages'].innerHTML;
   if(status==='running')assert.match(html,/data-motion="running"/);
   else assert.doesNotMatch(html,/data-motion="running"/);
  }
 }finally{Object.assign(globalThis,previous);}
});

test('state labels escape untrusted values and preserve text alternatives for motion',()=>{
 const html=badge({...task('<img src=x onerror=alert(1)>')});
 assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
 assert.match(badge(task()),/aria-hidden="true"/);
 assert.match(badge(task()),/运行中/);
});

test('role cards distinguish current review work from its earlier waiting explanation',()=>{
 const t={...task('running','reviewing'),work:{roles:{
  reviewer:{status:'running',current_work:'Reviewing the changed recovery branch',wait_reason:'Old waiting explanation',provider:'codex'},
  worker:{status:'waiting',wait_reason:'Waiting for review'},watcher:{status:'monitoring',usage:{agent_calls:4,max_agent_calls:20}},
 }}};
 const html=roleDetailsHTML(t);
 assert.match(html,/Reviewing the changed recovery branch/);
 assert.doesNotMatch(html,/Old waiting explanation/);
 assert.match(html,/scene-scan/);assert.match(html,/scene-sweep/);assert.match(html,/scene-cursor/);
 assert.match(html,/<details class="role-runtime" data-key="role-runtime-reviewer">/);
 assert.match(html,/Waiting for review/);
 assert.doesNotMatch(roleDetailsHTML({...t,status:'paused'}),/data-motion="running"/);
 assert.equal(rolePresentation({...t,health:{phase:'retry_wait'}},'reviewer',{status:'running'}).state,'waiting');
});

test('role explanations and current operations are localized and escape tool text',()=>{
 const previous=globalThis.document;
 globalThis.document={documentElement:{lang:'en'}};
 try{
  const html=roleDetailsHTML({...task(),activity:{role:'worker',is_running:true,operation:{kind:'command',status:'running',text:'<script>bad</script>'}},work:{roles:{worker:{status:'running'},reviewer:{status:'waiting'},watcher:{status:'monitoring'}}}});
  assert.match(html,/Running a command/);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html,/[\u3400-\u9fff]/);
 }finally{globalThis.document=previous;}
});
