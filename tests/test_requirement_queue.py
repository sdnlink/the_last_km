"""Queued requests start after acceptance, never as amendments to the current work."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.requirement_queue import RequirementQueue
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.tasks import TaskError, TaskStore
from tests.test_requirement_follow_up import CitingReviewer
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict
from tests.test_function_dispatch import FunctionWorker, task, plan, result, CHECK


class RequirementQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.queue = RequirementQueue(self.run)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'Fixture'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'fixture@local'], check=True)
        (self.root / 'app.py').write_text('base\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'app.py'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'base'], check=True)

    def monitor(self, worker, reviewer, resume=False, **extra):
        return Supervisor(RunConfig('' if resume else 'Implement current requirement only.', self.root,
            worker, reviewer=reviewer, workflow='auto' if resume else 'review-first', resume=resume,
            run_dir=self.run, git_history='required', checks=['test -f app.py'],
            max_rounds=10, retry_delay_s=0, **extra), log=lambda _: None)

    def reviewer(self, count):
        scripts = []
        for _ in range(count):
            scripts.extend([{'verdict': {}, 'text': json.dumps(BRIEF)},
                            {'verdict': _verdict('done', evidence=['app.py']), 'cite': True}])
        return CitingReviewer(scripts, self.root)

    def worker(self, count):
        return ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']),
                                           'touch': 'app.py'} for _ in range(count)])

    def test_append_first_before_and_tail_persist_without_sending_to_current_worker(self):
        a = self.queue.append('QUEUED-A')
        b = self.queue.append('QUEUED-B')
        c = self.queue.append('QUEUED-C', b['id'])
        d = self.queue.append('QUEUED-D', 'first')
        order = [d, a, c, b]
        self.assertEqual([x['id'] for x in RequirementQueue(self.run).snapshot()['waiting']], [x['id'] for x in order])
        worker, reviewer = self.worker(5), self.reviewer(5)
        result = self.monitor(worker, reviewer).run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(len(worker.calls), 5)
        self.assertNotIn('QUEUED-', worker.calls[0]['prompt'])
        self.assertNotIn('QUEUED-', reviewer.calls[0]['prompt'])
        for index, item in enumerate(order, 1):
            self.assertIn(item['text'], reviewer.calls[index * 2]['prompt'])
            self.assertIn(item['text'], worker.calls[index]['prompt'])
            # Waiting requests never enter the active reviewer/worker context.
            for future in order[index:]:
                self.assertNotIn(future['text'], worker.calls[index]['prompt'])
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot['waiting'], [])
        self.assertIsNone(snapshot['active'])
        self.assertEqual(len(snapshot['completed']), 4)

    def test_rejected_current_requirement_does_not_take_a_queued_request(self):
        item = self.queue.append('AFTER-CURRENT')
        worker = self.worker(1)
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': _verdict('continue', gaps=['missing behavior']), 'cite': True}], self.root)
        monitor = self.monitor(worker, reviewer)
        monitor.config.max_rounds = 1
        result = monitor.run()
        self.assertEqual(result.status, 'max_rounds', result.summary)
        self.assertIsNone(self.queue.snapshot()['active'])
        self.assertEqual(self.queue.snapshot()['waiting'][0]['id'], item['id'])

    def test_unready_queue_head_blocks_implementation_and_does_not_skip_to_next(self):
        first = self.queue.append('NEEDS-REVIEW')
        second = self.queue.append('DO-NOT-SKIP')
        worker = self.worker(1)
        reviewer = self.reviewer(1)
        reviewer.scripts.append({'verdict': {}, 'text': json.dumps({**BRIEF, 'status': 'needs_input',
            'blockers': ['Missing required environment'], 'summary': 'Keep this requirement pending'})})
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(self.queue.snapshot()['active']['id'], first['id'])
        self.assertEqual(self.queue.snapshot()['waiting'][0]['id'], second['id'])

    def test_function_dispatch_reviews_each_queued_request_with_its_own_criteria(self):
        (self.root / 'app.py').write_text('def parse_input():\n    return 0\n')
        self.queue.append('NEW-QUEUED-FUNCTION')
        first = task('F1', 'app.py', scope='requirement')
        first['criterion_ids'] = ['main:1', 'req:1:1', 'req:1:2']
        second = task('F2', 'app.py', scope='requirement')
        second['criterion_ids'] = ['main:1', 'req:2:1', 'req:2:2']
        next_plan = plan(second)
        value = json.loads(next_plan['text']); value['modules'] = []; next_plan['text'] = json.dumps(value)
        worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'},
            {**result('F2', 'done', status='done'), 'source': 'def parse_input():\n    return 43\n'}])
        reviewer = ScriptedProvider('reviewer', [plan(first), result('F1', 'accepted', status='done'),
            next_plan, result('F2', 'accepted', status='done')])
        monitor = self.monitor(worker, reviewer, function_dispatch=True)
        monitor.config.checks = [CHECK]
        monitor.config.git_history = 'off'
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertNotIn('NEW-QUEUED-FUNCTION', worker.calls[0]['prompt'])
        self.assertIn('NEW-QUEUED-FUNCTION', reviewer.calls[2]['prompt'])
        self.assertEqual(len(self.queue.snapshot()['completed']), 1)

    def test_interrupted_queue_review_resumes_the_same_request_before_worker(self):
        first = self.queue.append('FIRST-QUEUED')
        second = self.queue.append('SECOND-QUEUED')
        worker, reviewer = self.worker(1), self.reviewer(1)
        monitor = self.monitor(worker, reviewer)
        monitor.log = lambda message: monitor.request_stop() if 'elaborating next requirement' in message else None
        result = monitor.run()
        self.assertEqual(result.status, 'stopped', result.summary)
        self.assertEqual(self.queue.snapshot()['active']['id'], first['id'])
        self.assertEqual(self.queue.snapshot()['waiting'][0]['id'], second['id'])
        inserted = self.queue.append('INSERTED-AFTER-ACTIVE', 'first')
        self.assertEqual(self.queue.snapshot()['active']['id'], first['id'])
        self.assertEqual(self.queue.snapshot()['waiting'][0]['id'], inserted['id'])
        worker, reviewer = self.worker(3), self.reviewer(3)
        resumed = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(resumed.status, 'done', resumed.summary)
        self.assertIn(first['text'], reviewer.calls[0]['prompt'])
        self.assertIn(inserted['text'], reviewer.calls[2]['prompt'])
        self.assertIn(second['text'], reviewer.calls[4]['prompt'])

    def test_active_or_completed_request_cannot_be_removed_or_used_as_insert_anchor(self):
        first = self.queue.append('ACTIVE')
        second = self.queue.append('WAITING')
        write_json(self.run / 'state.json', {'workflow': 'review-first', 'status': 'running',
                   'active_queued_requirement': first, 'requirement_cycles': []})
        for operation in (lambda: self.queue.remove(first['id']), lambda: self.queue.append('NEW', first['id'])):
            with self.assertRaises(ValueError):
                operation()
        self.queue.remove(second['id'])
        self.assertEqual(self.queue.snapshot()['waiting'], [])
        self.assertEqual(self.queue.snapshot()['active'], first)

    def test_invalid_content_and_finished_runs_are_rejected_without_overwriting_queue(self):
        self.queue.append('KEEP')
        path = self.run / 'requirement-queue.json'
        before = path.read_bytes()
        for text in ('', ' ', 'x' * 4001, None, 'bad\0value'):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                self.queue.append(text)
            self.assertEqual(path.read_bytes(), before)
        write_json(self.run / 'state.json', {'status': 'done'})
        with self.assertRaises(ValueError):
            self.queue.append('TOO-LATE')
        self.assertEqual(path.read_bytes(), before)

    def test_console_appending_queue_does_not_resume_or_amend_current_work(self):
        with patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')}):
            store = TaskStore(self.root / 'data', self.root)
            task = store.create({'title': 'Task', 'requirement': 'CURRENT'})
            with patch.object(store, '_launch') as launch:
                first = store.queued_requirements(task['id'], {'text': 'LATER'})
                second = store.queued_requirements(task['id'], {'text': 'PRIORITY', 'position': 'first'})
                launch.assert_not_called()
            self.assertEqual([x['text'] for x in second['waiting']], ['PRIORITY', 'LATER'])
            self.assertEqual(store.detail(task['id'])['directions'], [])
            self.assertEqual(store.detail(task['id'])['requirement'], 'CURRENT')
            with self.assertRaises(TaskError):
                store.queued_requirements(task['id'], {'text': 'BAD', 'position': 'missing'})
            self.assertEqual(first['waiting'][0]['text'], 'LATER')


if __name__ == '__main__':
    unittest.main()
