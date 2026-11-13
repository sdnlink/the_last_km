import assert from 'node:assert/strict';
import {test} from 'node:test';
import {dispatchPlanHTML} from '../console/static/dispatch-plan.js';

test('dispatch rows show full reviewer tasks, dependencies and verification without HTML injection',()=>{
 const value={plan_state:'ready',complete:true,items:[{status:'waiting',position:7,criteria:[{id:'req:1:1',text:'Expected behavior'}],task:{id:'F7',module_id:'main',objective:'<script>bad</script>',file:'app.py',symbol:'run',inputs:'Full inputs',outputs:'Full outputs',boundaries:['Boundary'],verification:['Check negative path'],depends_on:['F6'],check_ids:['check:1'],test_files:['tests/test_app.py']}}]};
 const html=dispatchPlanHTML(value);
 assert.doesNotMatch(html.rows,/<script>/);assert.match(html.rows,/&lt;script&gt;/);
 for(const text of ['F7','待派发','Full inputs','Full outputs','Boundary','Check negative path','F6','Expected behavior','tests/test_app.py'])assert.ok(html.rows.includes(text));
 assert.match(html.rows,/data-key="dispatch:F7"/);
 assert.match(html.notice,/完整函数计划/);
});
test('missing and unready full plans explain why the known tasks are incomplete',()=>{
 assert.match(dispatchPlanHTML({plan_state:'not_generated',complete:false,items:[]}).notice,/尚未生成/);
 assert.match(dispatchPlanHTML({plan_state:'needs_review',complete:true,items:[]}).notice,/暂不启动/);
 assert.match(dispatchPlanHTML({plan_state:'ready',complete:true,items:[]}).rows,/当前筛选下没有派单/);
});
