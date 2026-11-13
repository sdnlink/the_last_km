import assert from 'node:assert/strict';
import {test} from 'node:test';
import {activityTextHTML,failureHTML,requirementReviewHTML,renderWorkflow} from '../console/static/task-view.js';

test('preview labels and complete output links distinguish pending and completed turns',()=>{
 const pending=activityTextHTML({id:'task'},{text:'<summary>',truncated:true,text_length:7290});
 assert.match(pending,/已截断/);assert.match(pending,/7290/);assert.match(pending,/本轮结束后/);
 assert.doesNotMatch(pending,/<summary>|<a /);
 const completed=activityTextHTML({id:'task'},{text:'preview',preview_only:true,text_file:'000-reviewer-text.md'});
 assert.match(completed,/进度预览/);
 assert.match(completed,/href="\/api\/tasks\/task\/logs\?file=000-reviewer-text.md&amp;full=1"/);
 assert.match(completed,/target="_blank" rel="noopener"/);
 assert.doesNotMatch(activityTextHTML({id:'task'},{text:'complete'}),/截断|进度预览|<a /);
});

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
  const nodes=Object.fromEntries(['#workflow-stages','#detail-live'].map(id=>[id,{innerHTML:'',dataset:{},querySelectorAll:()=>[]} ]));
  globalThis.document.querySelector=selector=>nodes[selector]||null;
  renderWorkflow({id:'task',workflow:'review-first',status:'action_required',health:{phase:'finished'},work:{workflow:[{role:'reviewer',round:0,kind:'message',text:'partial',truncated:true,text_file:'000-reviewer-text.md'}]}},{showRoles:false});
  assert.match(nodes['#detail-live'].innerHTML,/进度说明已截断/);
  assert.match(nodes['#detail-live'].innerHTML,/查看本轮完整输出/);
  const stages=nodes['#workflow-stages'].innerHTML;
  assert.ok(stages.indexOf('watcher 运行派单检查')<stages.indexOf('reviewer 审查'));
  assert.ok(stages.indexOf('reviewer 审查')<stages.indexOf('watcher 提交已审核代码'));
 }finally{
  for(const [key,value] of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
 }
});
