import assert from 'node:assert/strict';
import {test} from 'node:test';
import {failureHTML} from '../console/static/task-view.js';

test('terminal explanation shows cause, stage and recovery without trusting worker done',()=>{
 const html=failureHTML({status:'failed',last_worker:{status:'done'},failure:{title:'业务分支在任务外发生变化',
  reason:'版本不一致',phase:'版本检查',role:'watcher',round:3,raw_summary:'HEAD changed',source:'report.json',
  actions:['保留改动并复核'],recent_failures:[{role:'worker',round:1,reason:'no output'}]}});
 for(const value of ['业务分支在任务外发生变化','停止阶段','watcher','第 3 轮','原始停止原因','保留改动并复核','此前中断记录','不能替代'])assert.ok(html.includes(value),value);
});
test('diagnostic fields are escaped and missing diagnostics render nothing',()=>{
 const html=failureHTML({failure:{title:'<script>x</script>',phase:'<img>',reason:'<svg>',raw_summary:'<iframe>',actions:['<button>']}});
 assert.doesNotMatch(html,/<script>|<img>|<svg>|<iframe>|<button>/);
 assert.match(html,/&lt;script&gt;/);assert.equal(failureHTML({status:'running'}),'');
});
