import assert from 'node:assert/strict';
import {test} from 'node:test';
import {failureHTML,requirementReviewHTML,renderWorkflow,selectWorkflowStage} from '../console/static/task-view.js';

test('workspace change explanation preserves unaccepted review and escaped file details',()=>{
 const html=failureHTML({id:'task',failure:{title:'审查期间工作区发生变化',
  workspace_changes:{count:2,files:[{file:'<script>.py',change:'modified'},{file:'added.py',change:'added'}],report_file:'requirements-workspace-change.json'},
  review_output_file:'000-reviewer-text.md'}});
 assert.match(html,/审查期间变化文件 · 2 项/);assert.match(html,/修改 · &lt;script&gt;.py/);
 assert.match(html,/新增 · added.py/);assert.match(html,/查看完整变化记录/);
 assert.match(html,/完整原始结果（未获 watcher 接受）/);assert.doesNotMatch(html,/<script>/);
});

test('requirements include summary, scope, criteria, assumptions, constraints and blockers',()=>{
 const original={document:globalThis.document,window:globalThis.window,requestAnimationFrame:globalThis.requestAnimationFrame};
 globalThis.document={querySelector:()=>null,querySelectorAll:()=>[]};
 globalThis.window={innerHeight:800};globalThis.requestAnimationFrame=()=>{};
 try{
  const html=requirementReviewHTML({status:'needs_input',summary:'Summary',refined_requirement:'Original scope',
   acceptance_criteria:['Criterion'],constraints:['Keep scope'],assumptions:['Existing evidence'],blockers:['<Confirm version>']});
  for(const field of ['Summary','Original scope','验收条件','Criterion','约束','Keep scope','假设','Existing evidence','阻塞条件','&lt;Confirm version&gt;'])assert.ok(html.includes(field),field);
  assert.equal(requirementReviewHTML({}),'');

 }finally{
  for(const [key,value] of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
 }
});


test('step history includes earlier rounds and shows only conclusions',()=>{
 const original={document:globalThis.document,window:globalThis.window,requestAnimationFrame:globalThis.requestAnimationFrame};
 globalThis.document={querySelector:()=>null,querySelectorAll:()=>[]};
 globalThis.window={innerHeight:800};globalThis.requestAnimationFrame=()=>{};
 try{
  const nodes=Object.fromEntries(['#workflow-stages','#detail-live'].map(id=>[id,{innerHTML:'',dataset:{},querySelectorAll:()=>[]} ]));
  globalThis.document.querySelector=selector=>nodes[selector]||null;
  const task={id:'task',workflow:'review-first',status:'running',health:{phase:'reviewing'},activity:{round:8,plan:[{step:'private command'}]},work:{
   workflow:[{role:'reviewer',round:8,kind:'command',text:'git diff private command'}],
   stages:{working:[{role:'worker',summary:'旧轮的实现结论',round:1}],reviewing:[{role:'reviewer',summary:'当前审查结论',round:8}]},
  }};
  const render=()=>renderWorkflow(task,{showRoles:false,role:'reviewer',currentOnly:true});
  render();
  assert.match(nodes['#detail-live'].innerHTML,/当前审查结论/);
  assert.doesNotMatch(nodes['#detail-live'].innerHTML,/private command|旧轮的实现结论/);
  const stages=nodes['#workflow-stages'].innerHTML;
  assert.equal((stages.match(/<button/g)||[]).length,6);
  assert.ok(stages.indexOf('进行检查')<stages.indexOf('独立审查'));
  assert.ok(stages.indexOf('独立审查')<stages.indexOf('保存提交'));
  selectWorkflowStage('working');
  render();
  assert.match(nodes['#detail-live'].innerHTML,/实现需求 · 任务全程/);
  assert.match(nodes['#detail-live'].innerHTML,/旧轮的实现结论/);
  assert.doesNotMatch(nodes['#detail-live'].innerHTML,/当前审查结论|private command/);
  selectWorkflowStage('working');
  render();
  assert.match(nodes['#detail-live'].innerHTML,/当前审查结论/);
 }finally{
  selectWorkflowStage(null);
  for(const [key,value] of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
 }
});
