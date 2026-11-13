"""Watcher check failures preserve a candidate without automatic implementation retries."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_dispatch import FunctionWorker, plan, result, task, verification_task
from tests.test_supervisor import ScriptedProvider


class FunctionAcceptanceBlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent/'project'
        self.root.mkdir()
        self.ready = self.parent/'check-ready'
        self.check = "test -f '"+str(self.ready)+"'"

    def monitor(self, worker, reviewer, *, resume=False, **kwargs):
        return Supervisor(RunConfig('' if resume else 'Implement the original requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', function_dispatch=not resume, resume=resume,
            git_history='off', run_dir=self.root/'run', retry_delay_s=0, review_every=0,
            max_rounds=2, checks=[] if resume else [self.check], **kwargs), log=lambda _:None)

    def state(self):
        return json.loads((self.root/'run/state.json').read_text())

    def start(self, *, scope='function', verification=False):
        if verification:
            (self.root/'app.py').write_text('def parse_input(): return 42\n')
            assignment = verification_task('F1')
        else:
            assignment = task('F1','app.py',scope=scope)
            if scope != 'function':
                assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
        final = assignment['delivery_scope'] == 'requirement'
        next_task = None if final else task('F2','next.py',depends=['F1'])
        submitted = result('F1','done',status='done' if scope != 'function' or verification else 'continue')
        approved = result('F1','accepted',status='done' if scope != 'function' or verification else 'continue',next_task=next_task)
        if scope=='module':
            submitted['verdict']['scope']=approved['verdict']['scope']='module'
        worker = FunctionWorker('worker',[{**submitted, **({} if verification else {'touch':'app.py'})}])
        reviewer = ScriptedProvider('reviewer',[plan(assignment),
            approved])
        monitor = self.monitor(worker,reviewer)
        report = monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
        saved = self.state()
        self.assertEqual(saved['function_tasks']['current'],assignment)
        self.assertEqual(saved['function_tasks']['accepted'],[])
        self.assertEqual(saved['rounds'][-1]['task_status'],'accepted')
        self.assertEqual(saved['rounds'][-1]['watcher_result']['function_gate'],'checks')
        self.assertIn('candidate_approval',saved['rounds'][-1]['watcher_result'])
        self.assertIn('checks executed BEFORE',reviewer.calls[-1]['prompt'])
        self.assertIn('function_acceptance_blocked',(self.root/'run/events.jsonl').read_text())
        return monitor,report

    def test_unchanged_failure_stops_each_resume_without_spending_model_calls(self):
        self.start()
        original_log=(self.root/'run/001-check-01.log').read_bytes()
        before = self.state()['usage']['agent_calls']
        for _ in range(2):
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[])
            report=self.monitor(worker,reviewer,resume=True).run()
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertFalse(worker.calls or reviewer.calls)
            self.assertEqual(self.state()['usage']['agent_calls'],before)
            self.assertEqual(self.state()['function_tasks']['current']['id'],'F1')
            self.assertEqual(self.state()['function_tasks']['accepted'],[])
        history=self.state()['rounds'][0]['watcher_result']['candidate_checks_history']
        self.assertEqual(len(history),2)
        archived=json.loads((self.root/'run'/history[0]).read_text())
        self.assertEqual((self.root/'run'/archived['checks'][0]['log']).read_bytes(),original_log)
        self.ready.touch()
        worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[
            result('F1','accepted',next_task=task('F2','next.py',depends=['F1']))])
        # Stop at the next assignment boundary so this test never implements F2.
        monitor=self.monitor(worker,reviewer,resume=True)
        emit=monitor._event
        def event(name,**data):
            emit(name,**data)
            if name=='function_assigned' and data['task_id']=='F2':monitor.request_stop()
        monitor._event=event
        report=monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls),1)
        self.assertEqual(self.state()['usage']['agent_calls'],before+1)
        self.assertEqual(self.state()['function_tasks']['current']['id'],'F2')
        accepted,=self.state()['function_tasks']['accepted']
        self.assertEqual(accepted['task']['id'],'F1')
        self.assertEqual(accepted['verification'],{'check:1':'001-check-01.log'})

    def test_module_final_and_verification_failures_never_dispatch_worker_again(self):
        for scope,verification in [('module',False),('requirement',False),('requirement',True)]:
            with self.subTest(scope=scope,verification=verification):
                self.start(scope=scope,verification=verification)
                self.ready.touch()
                approved=result('F1','accepted',status='done',next_task=task('F2','next.py',depends=['F1']) if scope=='module' else None)
                if scope=='module':approved['verdict']['scope']='module'
                worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[approved])
                monitor=self.monitor(worker,reviewer,resume=True)
                if scope=='module':
                    emit=monitor._event
                    def event(name,**data):
                        emit(name,**data)
                        if name=='function_assigned':monitor.request_stop()
                    monitor._event=event
                report=monitor.run()
                self.assertEqual(report.status,'stopped' if scope=='module' else 'done',report.summary)
                self.assertFalse(worker.calls)
                self.assertEqual(len(reviewer.calls),1)
            # Each scope uses a fresh project and checkpoint.
            self._reset_project()

    def _reset_project(self):
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.ready.unlink(missing_ok=True)

    def test_legacy_check_rejection_reuses_worker_but_requires_fresh_review(self):
        self.start()
        path=self.root/'run/state.json';state=self.state()
        review=state['rounds'][-1]
        review['watcher_result'].pop('candidate_approval')
        state['rounds'][0]['watcher_result'].pop('candidate_checks')
        review['watcher_result'].update(status='rejected',function_gate='repair')
        review.update(task_status='changes_required',next_task=None)
        path.write_text(json.dumps(state))
        worker=FunctionWorker('worker',[])
        reviewer=ScriptedProvider('reviewer',[result('F1','accepted',next_task=task('F2','next.py',depends=['F1']))])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls),1)
        self.assertEqual(self.state()['rounds'][-1]['watcher_result']['function_gate'],'checks')

    def test_candidate_change_never_reuses_saved_review(self):
        monitor,report=self.start()
        (self.root/'app.py').write_text('def parse_input(): return 99\n')
        self.assertIsNone(monitor._check_retry_review(report.records[-1]))
        self.assertIsNone(monitor._pending_review(report.records))

    def test_legacy_scope_defect_is_not_treated_as_check_retry(self):
        monitor,report=self.start()
        record=report.records[-1]
        record.watcher_result.pop('candidate_approval')
        record.watcher_result.update(status='rejected',function_gate='repair')
        record.watcher_result['reasons'].append('Restore edits outside the assigned function: app.py')
        self.assertIsNone(monitor._pending_review(report.records))

    def test_new_instructions_or_role_change_invalidates_saved_approval(self):
        monitor,report=self.start()
        review=report.records[-1]
        monitor.config.reviewer_model='different-model'
        self.assertIsNone(monitor._check_retry_review(review))
        monitor.config.reviewer_model=''
        monitor.directions.append('New requirement to independently review')
        self.assertIsNone(monitor._check_retry_review(review))
