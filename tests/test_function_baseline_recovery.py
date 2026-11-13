"""Requirement review must not masquerade as implementation during recovery."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.directions import Directions
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


class FunctionBaselineRecoveryTests(unittest.TestCase):
    def checkpoint_before_second_worker(self, root):
        second = task('F2', 'next.py', depends=['F1'], scope='requirement')
        worker = FunctionWorker('worker', [{**result('F1', 'done'), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py')),
            result('F1', 'accepted', next_task=second)])
        monitor = Supervisor(RunConfig('implement two functions', root, worker, reviewer=reviewer,
            workflow='review-first', function_dispatch=True, git_history='off', run_dir=root/'run',
            checks=[CHECK], retry_delay_s=0), log=lambda _: None)
        event = monitor._event

        def stop(kind, **data):
            event(kind, **data)
            if kind == 'function_assigned' and data['task_id'] == 'F2':
                monitor.request_stop()

        monitor._event = stop
        self.assertEqual(monitor.run().status, 'stopped')
        with gzip.open(root/'run/function-baseline.json.gz', 'rt') as handle:
            self.assertEqual(json.load(handle)['id'], 'F1')
        return second

    def resume(self, root, worker, reviewer):
        return Supervisor(RunConfig('', root, worker, reviewer=reviewer, workflow='review-first',
            resume=True, git_history='off', run_dir=root/'run', retry_delay_s=0), log=lambda _: None)

    def test_amended_next_assignment_starts_worker_with_a_fresh_baseline(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                second = self.checkpoint_before_second_worker(root)
                if legacy:
                    path = root/'run/state.json'
                    state = json.loads(path.read_text())
                    state.pop('last_worker_invocation', None)
                    path.write_text(json.dumps(state))
                amendment = Directions(root/'run').append('Preserve F2; add the report view after it.')
                worker = FunctionWorker('worker', [{**result('F2', 'done', status='done', file='next.py'),
                                                   'touch': 'next.py'}])
                reviewer = ScriptedProvider('reviewer', [plan(second),
                    result('F2', 'accepted', status='done', file='next.py')])
                report = self.resume(root, worker, reviewer).run()
                self.assertEqual(report.status, 'done', report.summary)
                self.assertEqual(len(worker.calls), 1)
                self.assertEqual(len(reviewer.calls), 2)
                state = json.loads((root/'run/state.json').read_text())
                self.assertEqual(state['last_invocation']['role'], 'reviewer')
                self.assertEqual(state['last_worker_invocation']['task_id'], 'F2')
                self.assertEqual(state['worker_direction_ids'], [amendment['id']])
                self.assertEqual([x['task']['id'] for x in state['function_tasks']['accepted']], ['F1', 'F2'])
                with gzip.open(root/'run/function-baseline.json.gz', 'rt') as handle:
                    self.assertEqual(json.load(handle)['id'], 'F2')

    def test_missing_baseline_after_worker_and_review_is_not_recreated(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                worker = FunctionWorker('worker', [{**result('F1', 'done'), 'touch': 'app.py'}])
                reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py')),
                    result('F1', 'changes_required', gaps=['Correct the return value'])])
                monitor = Supervisor(RunConfig('implement function', root, worker, reviewer=reviewer,
                    workflow='review-first', function_dispatch=True, git_history='off', run_dir=root/'run',
                    checks=[CHECK], retry_delay_s=0), log=lambda _: None)
                control = monitor._process_control

                def stop_after_review(active=False):
                    if any(r.role == 'reviewer' and r.verdict.task_status == 'changes_required'
                           for r in monitor._records):
                        monitor.request_stop()
                    return control(active)

                monitor._process_control = stop_after_review
                self.assertEqual(monitor.run().status, 'stopped')
                path = root/'run/state.json'
                state = json.loads(path.read_text())
                self.assertEqual(state['last_invocation']['role'], 'reviewer')
                self.assertEqual(state['last_worker_invocation']['task_id'], 'F1')
                if legacy:
                    state.pop('last_worker_invocation')
                    path.write_text(json.dumps(state))
                (root/'run/function-baseline.json.gz').unlink()
                worker = FunctionWorker('worker', [])
                reviewer = ScriptedProvider('reviewer', [])
                monitor = self.resume(root, worker, reviewer)
                report = monitor.run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertIn('baseline is missing', report.summary)
                self.assertEqual(worker.calls, [])
                self.assertEqual(reviewer.calls, [])
                self.assertFalse((root/'run/function-baseline.json.gz').exists())

    def test_interrupted_worker_without_result_still_requires_its_original_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.checkpoint_before_second_worker(root)
            path = root/'run/state.json'
            state = json.loads(path.read_text())
            state['last_worker_invocation'] = {'role': 'worker', 'round': 2, 'call': 4, 'task_id': 'F2'}
            state['last_invocation'] = {'role': 'reviewer', 'round': 2, 'call': 5, 'task_id': 'F2'}
            path.write_text(json.dumps(state))
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [])
            report = self.resume(root, worker, reviewer).run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('baseline is missing', report.summary)
            self.assertEqual(worker.calls, [])
            self.assertEqual(reviewer.calls, [])
            with gzip.open(root/'run/function-baseline.json.gz', 'rt') as handle:
                self.assertEqual(json.load(handle)['id'], 'F1')
