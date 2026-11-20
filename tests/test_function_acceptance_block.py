"""Watcher check failures preserve a candidate without automatic implementation retries."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_dispatch import FunctionWorker, plan, result, task, verification_task
from tests.test_requirement_follow_up import CitingReviewer
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

    def test_outside_whitespace_blocks_before_worker_and_recovers_with_original_checks(self):
        app = self.root / 'app.py'
        legacy = self.root / 'legacy notes.md'
        app.write_text('def parse_input():\n    return 1\n')
        legacy.write_bytes(b'Existing notes\n')

        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True)

        git('init', '-q')
        git('add', 'app.py', legacy.name)
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'Initial')
        head = git('rev-parse', 'HEAD')
        legacy.write_bytes(b'Existing notes\r\n')
        dirty_app = 'def parse_input():\n    # Fixture note  \n    return 1\n'
        app.write_text(dirty_app)
        assignment = task('F1', 'app.py', scope='requirement')
        assignment.update(criterion_ids=['main:1','req:1:1','req:1:2'], check_ids=['check:2'])
        checks = ['git diff --check', "python3 -c 'from app import parse_input; assert parse_input() == 42'"]
        run = self.root / 'run'

        def monitor(worker, reviewer, *, resume=False):
            return Supervisor(RunConfig('Implement the original requirement', self.root, worker,
                reviewer=reviewer, workflow='review-first', function_dispatch=not resume,
                resume=resume, git_history='required', run_dir=run, retry_delay_s=0,
                checks=[] if resume else checks), log=lambda _:None)

        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [plan(assignment)])
        report = monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('Fixed Git check blocked outside assignment F1', report.summary)
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(self.state()['function_tasks']['current'], assignment)
        self.assertEqual(git('rev-parse', 'HEAD'), head)
        self.assertEqual(app.read_text(), dirty_app)
        self.assertEqual(legacy.read_bytes(), b'Existing notes\r\n')
        diagnostic = (run/'assignment-preflight.log').read_text()
        self.assertIn(legacy.name, diagnostic)
        self.assertNotIn('app.py', diagnostic)

        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [])
        report = monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertFalse(worker.calls or reviewer.calls)

        legacy.write_bytes(b'Existing notes\n')
        worker = FunctionWorker('worker', [
            {**result('F1', 'done', status='done'), 'touch':'app.py'}])
        reviewer = CitingReviewer([{**result('F1', 'accepted', status='done'), 'cite':True}], self.root)
        report = monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 1))
        self.assertNotEqual(git('rev-parse', 'HEAD'), head)
        acceptance = json.loads((run/'acceptance-results.json').read_text())
        self.assertEqual([item['command'] for item in acceptance['checks']], checks)
        self.assertTrue(acceptance['complete'])

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

    def test_retry_precondition_failure_does_not_relabel_previous_results_as_new(self):
        self.start()
        worker=FunctionWorker('worker',[])
        reviewer=ScriptedProvider('reviewer',[
            result('F1','accepted',next_task=task('F2','next.py',depends=['F1']))])
        monitor=self.monitor(worker,reviewer,resume=True)
        def precondition_failed(index,verdict,**kwargs):
            verdict.gaps.append('Protected acceptance baseline changed: tests/test_api.py')
            return False
        with patch.object(monitor,'_delivery_checks',side_effect=precondition_failed):
            report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('Protected acceptance baseline changed',report.summary)
        receipt=self.state()['rounds'][0]['watcher_result']['candidate_checks']
        self.assertEqual(receipt['checks'],[])
        self.assertFalse(receipt['complete'])
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls),1)

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

    def run_repeated_fixture_failure(self, *, editable=False, changed=False, worker_blocked=True,
                                     unrelated_finding=False, source_repair=False, repair=False):
        name = 'tests/test_drag.py'
        fixture = (
            'import unittest\n'
            'class DragTest(unittest.TestCase):\n'
            '    def test_cancel(self):\n'
            '        fixture = {}\n'
            '        self.assertIsNone(fixture.get("captured", "missing"))\n'
        )
        (self.root / 'tests').mkdir()
        if not editable:
            (self.root / name).write_text(fixture)
        assignment = task('F1', 'app.py')
        if editable:
            assignment['test_files'] = [name]
        first = {
            **result('F1', 'incomplete'),
            'writes': {'app.py': 'def parse_input(): return 42\n', **({name: fixture} if editable else {})},
        }

        def retry(number):
            attempt = result('F1', 'incomplete')
            if worker_blocked:
                attempt['verdict']['blockers'] = [f'AUD-1: fixture requires correction (attempt {number})']
            return attempt

        scripts = [first, retry(2), retry(3)]
        if changed:
            scripts[-1]['source'] = 'def parse_input(): return 43\n'
        first_review = result('F1', 'changes_required', gaps=[f'main/{name}:5 fixture fails the cancellation assertion'])
        later = [
            result('F1', 'changes_required', gaps=[f'AUD-1: cancellation fails in {name}:5; attempt {number}'])
            for number in (2, 3)
        ]
        for finding in later:
            finding['verdict']['next_actions'] = [
                'Correct cancellation in app.py' if source_repair else f'Repair fixture initialization in {name}:5']
        if unrelated_finding:
            later[-1]['verdict']['gaps'].append('main/Another implementation defect in app.py')
        if repair:
            scripts[-1] = {**result('F1', 'done'), 'writes': {name: fixture.replace('{}', '{"captured": None}')}}
            later[-1] = result('F1', 'accepted', next_task=task('F2', 'next.py', depends=['F1']))
            later[-1]['verdict']['issue_resolutions'] = [
                {'id':'AUD-1', 'summary':'Initialized the fixture without changing its assertions', 'evidence':[name]}]
        worker = FunctionWorker('worker', scripts)
        reviewer = ScriptedProvider('reviewer', [plan(assignment), first_review, *later])
        self.check = 'python3 -m unittest discover -s tests -p test_drag.py'
        monitor = self.monitor(worker, reviewer, quality_policy={'blocked_review_limit': 2})
        monitor.config.max_rounds = 3
        monitor.config.review_every = 1
        report = monitor.run()
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 4), report.summary)
        return worker, reviewer, report

    def test_rephrased_audit_gaps_stop_repeated_out_of_scope_failure(self):
        _, _, report = self.run_repeated_fixture_failure()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('outside the assignment', report.summary)
        self.assertIn('F1 / AUD-1', report.summary)
        evidence = json.loads((self.root / 'run/dependency-block.json').read_text())
        self.assertEqual(evidence['review_count'], 2)
        self.assertEqual(evidence['paths'], ['tests/test_drag.py'])
        self.assertEqual(evidence['logs'], ['003-check-01.log'])
        self.assertEqual(self.state()['audit_issues']['items'][0]['status'], 'open')
        self.assertEqual(self.state()['function_tasks']['accepted'], [])

    def test_assignment_test_is_editable_until_accepted(self):
        worker, reviewer, report = self.run_repeated_fixture_failure(editable=True, repair=True)
        for prompt in (worker.calls[-1]['prompt'], reviewer.calls[-1]['prompt']):
            self.assertIn('Editable focused tests: ["tests/test_drag.py"]', prompt)
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(self.state()['function_tasks']['accepted'][0]['task']['id'], 'F1')
        self.assertEqual(self.state()['audit_issues']['items'][0]['status'], 'resolved')
        self.assertFalse((self.root / 'run/dependency-block.json').exists())

    def test_repeated_editable_test_failure_reports_pending_repair(self):
        _, _, report = self.run_repeated_fixture_failure(editable=True)
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('editable assignment test', report.summary)
        evidence = json.loads((self.root / 'run/dependency-block.json').read_text())
        self.assertTrue(evidence['writable'])
        self.assertEqual(self.state()['function_tasks']['accepted'], [])

    def test_code_changes_or_unconfirmed_blockers_do_not_interrupt_repair(self):
        for case in ({'changed': True}, {'worker_blocked': False}, {'unrelated_finding': True},
                     {'source_repair': True}):
            with self.subTest(**case):
                self._reset_project()
                _, _, report = self.run_repeated_fixture_failure(**case)
                self.assertEqual(report.status, 'max_rounds', report.summary)
                self.assertFalse((self.root / 'run/dependency-block.json').exists())
