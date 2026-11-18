"""Fault-injection evidence for review of 72ee46f; temporary fixtures only.

Run from the source root: PYTHONPATH=. .venv/bin/python docs/review-evidence/2026-11-18/reproduce.py
The launch case uses FakeChild; no real model CLI is launched.
"""
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RoundRecord
from auto_moniter.verdict import parse_verdict
from console.tasks import TaskStore, TaskError
from tests.test_function_module_delivery import FunctionModuleDeliveryTests, conclusion
from tests.test_function_dispatch import FunctionWorker
from tests.test_supervisor import ScriptedProvider


def module_dependency():
    fixture = FunctionModuleDeliveryTests()
    fixture.setUp()
    try:
        module_task = copy.deepcopy(fixture.final)
        module_task['delivery_scope'] = 'module'
        worker_report = conclusion('F2','done',['B'],final=True,file='next.py')
        worker_report['verdict']['scope'] = 'module'
        review_report = conclusion('F2','accepted',['B'],final=True,file='next.py')
        review_report['verdict']['scope'] = 'module'
        worker = FunctionWorker('worker',[
            {**conclusion('F1','done',['A']),'touch':'app.py'},
            {**worker_report,'touch':'next.py'},
        ])
        reviewer = ScriptedProvider('reviewer',[fixture.planning(),
            conclusion('F1','accepted',[],next_task=module_task),review_report,copy.deepcopy(review_report)])
        monitor = fixture.monitor(worker,reviewer)
        monitor.config.max_runtime_s=2
        result = monitor.run()
        events=[json.loads(line) for line in (fixture.root/'run/events.jsonl').read_text().splitlines()]
        return {'status':result.status,'summary':result.summary,
                'worker_calls':len(worker.calls),'reviewer_calls':len(reviewer.calls),
                'cached_review_reuses':sum(x.get('event')=='acceptance_review_reused' for x in events),
                'pending_modules':[m['id'] for m in monitor.modules.items.values() if m['status']!='accepted']}
    finally:
        fixture.doCleanups()


def cached_report():
    fixture = FunctionModuleDeliveryTests()
    fixture.setUp()
    try:
        worker,reviewer = fixture.workers(),fixture.reviewers()
        monitor = fixture.monitor(worker,reviewer)
        invoke = monitor._invoke_review
        def seed_cached_legacy_report(provider,prompt,session_id,**kwargs):
            if kwargs['index']==2:
                record = next(x for x in reversed(monitor._records) if x.role=='worker' and x.index==2)
                receipt = record.watcher_result['candidate_checks']
                old = parse_verdict(json.dumps(conclusion('F2','accepted',['B'],final=True,file='next.py')['verdict']),function_gate=True)
                cached = RoundRecord(2,'reviewer',old,0,False,0,'','',tree=record.tree,
                    watcher_result={'candidate_approval':{'checks_digest':receipt['digest'],
                        'context':receipt['context'],'review':old.to_dict()}})
                monitor._records.append(cached)
            return invoke(provider,prompt,session_id,**kwargs)
        with patch.object(monitor,'_invoke_review',side_effect=seed_cached_legacy_report):
            result = monitor.run()
        return {'status':result.status,'summary':result.summary,
                'worker_calls':len(worker.calls),'reviewer_calls':len(reviewer.calls),
                'unused_correct_review':len(reviewer.scripts)}
    finally:
        fixture.doCleanups()


def launch_metadata_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = TaskStore(root/'data',root)
        task = store.create({'title':'Review fixture','requirement':'No real model launch',
                             'provider':'codex','reviewer':'codex'})
        meta = store._get(task['id'])
        class FakeChild:
            pid = 99999999
            terminated = False
            communicated = False
            def poll(self):return None
            def terminate(self):self.terminated=True
            def communicate(self,*args):self.communicated=True
        child = FakeChild()
        save = store._save
        count = 0
        def fail_first_save(value):
            nonlocal count
            count += 1
            if count==1:raise OSError('simulated catalog disk error after Popen')
            return save(value)
        error = ''
        with patch('console.tasks.subprocess.Popen',return_value=child), \
             patch('console.tasks.process_identity',return_value={'pid':child.pid}), \
             patch.object(store,'_save',side_effect=fail_first_save):
            try:store._launch(meta)
            except TaskError as exc:error=str(exc)
        return {'error':error,'saved_status':store._get(task['id'])['status'],
                'child_terminated':child.terminated,'wait_thread_started':child.communicated,
                'child_retained':task['id'] in store._children}


def main():
    for name,call in [('module_dependency',module_dependency),('cached_report',cached_report),
                      ('launch_metadata_failure',launch_metadata_failure)]:
        print('START',name,flush=True)
        print(json.dumps({name:call()},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
