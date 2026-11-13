"""A candidate is checked, reviewed once, then committed before the next dispatch."""
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import candidate_checks
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()


class CheckedReviewer(ScriptedProvider):
    def __init__(self, root, scripts, inspect=None):
        super().__init__('reviewer',scripts)
        self.root,self.inspect=root,inspect

    def _command(self,session,prompt,read_only):
        text=prompt.read_text()
        match=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',text)
        if match:
            self.scripts[0]['verdict'].setdefault('evidence',[]).append(match[1])
            if self.inspect:self.inspect(text)
        return super()._command(session,prompt,read_only)


class CandidateHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name);self.root=self.parent/'project';self.root.mkdir()
        self.run=self.root/'run';self.counter=self.parent/'checks-count'
        (self.root/'app.py').write_text('def parse_input():\n    return 0\n')
        (self.root/'next.py').write_text('def parse_input():\n    return 0\n')
        (self.root/'.gitignore').write_text('.auto-moniter/\nrun/\n__pycache__/\n')
        git(self.root,'init','-q');git(self.root,'add','.')
        git(self.root,'-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','baseline')
        self.initial=git(self.root,'rev-parse','HEAD')
        self.check=shlex.join([sys.executable,'-B','-c',
            'from pathlib import Path; from app import parse_input; '
            f'p=Path({str(self.counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); '
            'assert parse_input()==42'])

    def monitor(self,worker,reviewer,*,resume=False,max_rounds=2):
        return Supervisor(RunConfig('' if resume else 'Implement both assignments',self.root,worker,
            reviewer=reviewer,workflow='review-first',function_dispatch=not resume,resume=resume,
            run_dir=self.run,git_history='required',checks=[] if resume else [self.check],
            max_rounds=max_rounds,retry_delay_s=0,review_every=0),log=lambda _:None)

    def test_each_assignment_checks_then_reviews_then_commits_before_next_worker(self):
        inspected=[]
        def inspect(prompt):
            snapshot=json.loads((self.run/'acceptance-results.json').read_text())
            self.assertTrue(snapshot['complete'])
            self.assertIn('checks executed BEFORE this review',prompt)
            self.assertIn('"status": "passed"',prompt)
            inspected.append(int(self.counter.read_text()))
        final=final_task('F2','next.py',depends=['F1'])
        worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
            {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
        reviewer=CheckedReviewer(self.root,[plan(task('F1','app.py')),
            result('F1','accepted',next_task=final),result('F2','accepted',status='done',file='next.py')],inspect)
        monitor=self.monitor(worker,reviewer)
        emit=monitor._event;boundaries=[]
        def event(name,**data):
            emit(name,**data)
            if name=='function_assigned' and data['task_id']=='F2':
                boundaries.append(git(self.root,'rev-parse','HEAD'))
                self.assertIn('return 42',git(self.root,'show','HEAD:app.py'))
                self.assertIn('return 0',git(self.root,'show','HEAD:next.py'))
        monitor._event=event
        report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(inspected,[1,2]);self.assertEqual(self.counter.read_text(),'2')
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,3))
        self.assertNotEqual(boundaries,[self.initial])
        self.assertEqual(git(self.root,'rev-list','--count',self.initial+'..HEAD'),'2')
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        for index in [1,2]:
            checked=next(i for i,e in enumerate(events) if e['event']=='candidate_checks_completed' and str(e['round'])==str(index))
            reviewed=next(i for i,e in enumerate(events) if e['event']=='agent_start' and e.get('role')=='reviewer' and str(e['round'])==str(index))
            committed=next(i for i,e in enumerate(events) if e['event']=='approved_local_commit' and str(e['round'])==str(index))
            self.assertLess(checked,reviewed);self.assertLess(reviewed,committed)

    def test_resume_after_approval_runs_neither_checks_nor_models_again(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer,max_rounds=1)
        original=monitor._commit_approved
        def stop_before_publish(*args):
            monitor.request_stop()
            return original(*args)
        with patch.object(monitor,'_commit_approved',side_effect=stop_before_publish):report=monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        self.assertEqual(self.counter.read_text(),'1')
        worker=FunctionWorker('worker',[]);reviewer=CheckedReviewer(self.root,[])
        report=self.monitor(worker,reviewer,resume=True,max_rounds=1).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertFalse(worker.calls or reviewer.calls)
        self.assertEqual(self.counter.read_text(),'1')
        self.assertEqual(git(self.root,'rev-list','--count',self.initial+'..HEAD'),'1')

    def test_resume_after_function_commit_advances_once_without_republishing(self):
        final=final_task('F2','next.py',depends=['F1'])
        worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(task('F1','app.py')),result('F1','accepted',next_task=final)])
        monitor=self.monitor(worker,reviewer)
        with self.assertRaises(SystemExit), patch.object(monitor,'_advance_function',
                side_effect=SystemExit('Interrupted after local commit')):
            monitor.run()
        first=git(self.root,'rev-parse','HEAD')
        self.assertNotEqual(first,self.initial)
        self.assertEqual(self.counter.read_text(),'1')
        worker=FunctionWorker('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
        reviewer=CheckedReviewer(self.root,[result('F2','accepted',status='done',file='next.py')])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,1))
        self.assertEqual(self.counter.read_text(),'2')
        self.assertEqual(git(self.root,'rev-list','--count',self.initial+'..HEAD'),'2')
        self.assertEqual(git(self.root,'rev-parse','HEAD^'),first)

    def test_reviewer_receives_failure_and_assigns_a_real_fix(self):
        seen=[]
        def inspect(prompt):
            receipt=json.loads((self.run/'state.json').read_text())['rounds'][-1]['watcher_result']['candidate_checks']
            seen.append(receipt['passed'])
            self.assertIn('"status": "passed"' if receipt['passed'] else '"status": "check_failed"',prompt)
        rejected=result('F1','changes_required',gaps=['main/parse_input must return 42 rather than 41'])
        approved=result('F1','accepted',status='done')
        approved['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Return value is independently verified as 42','evidence':['app.py']}]
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'source':'def parse_input():\n    return 41\n'},
            {**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),rejected,approved],inspect)
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(seen,[False,True]);self.assertEqual(self.counter.read_text(),'2')
        self.assertEqual(git(self.root,'rev-list','--count',self.initial+'..HEAD'),'1')

    def test_modified_check_log_after_approval_prevents_publication(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer,max_rounds=1);original=monitor._commit_approved
        def tamper(*args):
            (self.run/'001-check-01.log').write_text('replaced evidence')
            return original(*args)
        with patch.object(monitor,'_commit_approved',side_effect=tamper):report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        self.assertEqual(self.counter.read_text(),'1')

    def test_replaced_check_receipt_requires_fresh_approval(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer,max_rounds=1);original=monitor._commit_approved
        def replace_receipt(worker,*args):
            receipt=worker.watcher_result['candidate_checks']
            receipt['checks'][0]['note']='New check observation after review'
            receipt['digest']=candidate_checks.digest({key:value for key,value in receipt.items() if key!='digest'})
            return original(worker,*args)
        with patch.object(monitor,'_commit_approved',side_effect=replace_receipt):report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('approval does not match',report.summary)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        self.assertEqual(self.counter.read_text(),'1')

    def test_check_precondition_failure_without_snapshot_preserves_candidate(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer,max_rounds=1)
        monitor.config.quality_policy={'protect_tests':True}
        original=monitor._delivery_checks
        def reject_baseline(*args,**kwargs):
            with patch.object(monitor.quality,'baseline_errors',return_value=['tests/test_api.py']):
                return original(*args,**kwargs)
        with patch.object(monitor,'_delivery_checks',side_effect=reject_baseline):report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('Protected acceptance baseline changed',report.summary)
        self.assertFalse(self.counter.exists())
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))

    def test_changed_source_after_approval_is_not_committed_or_rechecked_automatically(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer,max_rounds=1);original=monitor._commit_approved
        def change(*args):
            (self.root/'app.py').write_text('def parse_input(): return 99\n')
            return original(*args)
        with patch.object(monitor,'_commit_approved',side_effect=change):report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        self.assertEqual(self.counter.read_text(),'1')
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))

    def test_interrupted_review_keeps_successful_checks_for_resume(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
        reviewer=CheckedReviewer(self.root,[plan(final_task()),
            {'verdict':{},'text':'authentication failed: unauthorized','exit_code':1}])
        report=self.monitor(worker,reviewer,max_rounds=1).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(self.counter.read_text(),'1')
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.initial)
        worker=FunctionWorker('worker',[])
        reviewer=CheckedReviewer(self.root,[result('F1','accepted',status='done')])
        report=self.monitor(worker,reviewer,resume=True,max_rounds=1).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertFalse(worker.calls);self.assertEqual(len(reviewer.calls),1)
        self.assertEqual(self.counter.read_text(),'1')
