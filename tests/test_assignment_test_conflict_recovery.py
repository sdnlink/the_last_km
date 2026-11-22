"""An unstarted later assignment can repair test declarations without replaying work."""
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.supervisor import RunConfig, Supervisor
from tests import test_function_plan as planning
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result
from tests.test_supervisor import ScriptedProvider


class AssignmentTestConflictRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('a.py', 'b.py'):
            (self.root / name).write_text('def parse_input(): return 0\n')

    def monitor(self, worker, reviewer, *, resume=False, full=True):
        return Supervisor(RunConfig('Implement both functions.', self.root, worker,
            reviewer=reviewer, workflow='review-first', function_dispatch=True,
            full_function_plan=full, resume=resume, git_history='off',
            run_dir=self.root / 'run', checks=[CHECK], retry_delay_s=0, max_rounds=3),
            log=lambda _: None)

    def final_plan(self, final):
        response = plan(final)
        body = json.loads(response['text'])
        body['function_plan'] = [final]
        response['text'] = json.dumps(body)
        return response

    def test_full_plan_collision_is_replanned_without_repeating_accepted_work(self):
        self.assert_later_collision_replanned(full=True)

    def test_incremental_collision_is_replanned_without_repeating_accepted_work(self):
        self.assert_later_collision_replanned(full=False)

    def assert_later_collision_replanned(self, *, full):
        first, final = planning.tasks()
        occupied = self.root / 'test_existing.py'
        occupied.write_text('import unittest\n')
        conflicting = {**final, 'test_files': [occupied.name]}
        worker = FunctionWorker('worker', [
            {**result('F1', 'done', file='a.py'), 'touch': 'a.py'},
            {**result('F2', 'done', file='b.py', status='done'), 'touch': 'b.py'}])
        reviewer = ScriptedProvider('reviewer', [
            planning.complete_plan(first, conflicting) if full else plan(first),
            result('F1', 'accepted', file='a.py', next_task=conflicting),
            self.final_plan(final) if full else plan(final),
            result('F2', 'accepted', file='b.py', status='done')])
        monitor = self.monitor(worker, reviewer, full=full)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 4)
        self.assertEqual([item['task']['id'] for item in monitor.function_tasks.accepted], ['F1', 'F2'])
        self.assertEqual(occupied.read_text(), 'import unittest\n')
        self.assertIn('test_files already exist', reviewer.calls[2]['prompt'])

    def test_resume_repairs_later_collision_even_with_an_earlier_scope_baseline(self):
        first, final = planning.tasks()
        final = {**final, 'test_files': ['test_existing.py'], 'check_ids': ['check:1', 'auto:1']}
        worker = FunctionWorker('worker', [{**result('F1', 'done', file='a.py'), 'touch': 'a.py'}])
        reviewer = ScriptedProvider('reviewer', [planning.complete_plan(first, final),
            result('F1', 'accepted', file='a.py')])
        monitor = self.monitor(worker, reviewer)
        emit = monitor._event

        def stop_at_second_assignment(kind, **data):
            emit(kind, **data)
            if kind == 'function_assigned' and data['task_id'] == 'F2':
                monitor.request_stop()

        monitor._event = stop_at_second_assignment
        self.assertEqual(monitor.run().status, 'stopped')
        self.assertTrue((self.root / 'run/function-baseline.json.gz').exists())
        occupied = self.root / 'test_existing.py'
        occupied.write_text('import unittest\n')
        repaired = {**final, 'test_files': [], 'check_ids': ['check:1']}
        worker = FunctionWorker('worker', [
            {**result('F2', 'done', file='b.py', status='done'), 'touch': 'b.py'}])
        reviewer = ScriptedProvider('reviewer', [self.final_plan(repaired),
            result('F2', 'accepted', file='b.py', status='done')])
        monitor = self.monitor(worker, reviewer, resume=True)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual([item['task']['id'] for item in monitor.function_tasks.accepted], ['F1', 'F2'])
        self.assertEqual(occupied.read_text(), 'import unittest\n')
        self.assertIn('current assignment', reviewer.calls[0]['prompt'])

    def test_started_assignment_keeps_its_own_test_and_baseline_on_resume(self):
        first, final = planning.tasks()
        first = {**first, 'test_files': ['test_owned.py']}
        source = ('import unittest\nfrom a import parse_input\n'
                  'class TestOwned(unittest.TestCase):\n'
                  '    def test_behavior(self): self.assertEqual(parse_input(), 42)\n')
        worker = FunctionWorker('worker', [{**result('F1', 'done', file='a.py'),
            'writes': {'a.py': 'def parse_input(): return 42\n', 'test_owned.py': source}}])
        monitor = self.monitor(worker, ScriptedProvider('reviewer', [planning.complete_plan(first, final)]))
        monitor.log = lambda message: monitor.request_stop() if 'worker status=' in message else None
        self.assertEqual(monitor.run().status, 'stopped')
        baseline = (self.root / 'run/function-baseline.json.gz').read_bytes()
        worker = FunctionWorker('worker', [
            {**result('F2', 'done', file='b.py', status='done'), 'touch': 'b.py'}])
        reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted', file='a.py'),
            result('F2', 'accepted', file='b.py', status='done')])
        monitor = self.monitor(worker, reviewer, resume=True)
        invoke = monitor._invoke_review

        def inspect_baseline(*args, **kwargs):
            if kwargs['index'] == 1:
                self.assertEqual((self.root / 'run/function-baseline.json.gz').read_bytes(), baseline)
            return invoke(*args, **kwargs)

        monitor._invoke_review = inspect_baseline
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual((self.root / 'test_owned.py').read_text(), source)
