import {i18n,i18nHTML} from './i18n.js';
import {escape} from './ui.js';

const labels={accepted:i18n('已验收'),active:i18n('当前派单'),waiting:i18n('待派发')};
const list=items=>items?.length?'<ul>'+items.map(text=>`<li>${escape(text)}</li>`).join('')+'</ul>':i18nHTML('<p>无</p>');
export function dispatchPlanHTML(data) {
  const notice=data.plan_state==='disabled'?i18n('此任务未启用逐函数派单。'):!data.complete?
    i18n('完整函数计划尚未生成。下方展示已保存的派单；启用完整计划并恢复任务后，reviewer 会在安全审核边界补齐剩余计划。'):
    data.plan_state==='needs_review'?i18n('已保存完整计划，当前审核尚未就绪，待派发项暂不启动。'):
    i18n('reviewer 已保存完整函数计划。按计划顺序逐项验收后派发；待派发项尚未通过验收。');
  const rows=data.items.map(row=>{
    const task=row.task;
    const targets=task.targets||[{file:task.file,symbol:task.symbol}];
    return i18nHTML`<details class="dispatch-task" data-key="dispatch:${escape(task.id)}"><summary><span class="dispatch-status ${escape(row.status)}">${labels[row.status]}</span><strong>${escape(task.id)}</strong><span>${escape(task.objective)}</span></summary><div class="dispatch-task-body"><p>执行顺序 ${row.position} · 模块 ${escape(task.module_id)} · ${task.kind==='verification'?i18n('纯验收'):task.kind==='integration'?i18n('集成派单'):i18n('函数派单')}${row.accepted_round?i18n` · 第 ${row.accepted_round} 轮验收`:''}</p><h4>目标文件与函数</h4>${list(targets.map(target=>`${target.file} · ${target.symbol}`))}<h4>输入</h4><p>${escape(task.inputs)}</p><h4>预期输出</h4><p>${escape(task.outputs)}</p><h4>边界与约束</h4>${list(task.boundaries)}<h4>验证要求</h4>${list(task.verification)}<h4>前置依赖</h4>${list(task.depends_on)}<h4>关联验收条目</h4>${list((row.criteria||[]).map(item=>`${item.id}${item.text?' · '+item.text:''}`))}<h4>批准检查编号</h4>${list(task.check_ids)}<h4>测试文件</h4>${list(task.test_files)}${row.evidence?.length?i18nHTML`<h4>验收证据</h4>${list(row.evidence)}`:''}${row.verification&&Object.keys(row.verification).length?i18nHTML`<h4>Watcher 检查记录</h4>${list(Object.entries(row.verification).map(([id,log])=>`${id} · ${log}`))}`:''}</div></details>`;
  }).join('');
  return {notice,rows:rows||i18nHTML('<p class="muted">当前筛选下没有派单。</p>')};
}
