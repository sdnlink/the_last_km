import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.providers import PreparedCommand
from auto_moniter.supervisor import RunConfig, Supervisor
from console.tasks import TaskStore, TaskError
from tests.test_supervisor import ScriptedProvider, _verdict


class TaskDirectionsTests(unittest.TestCase):
    def test_amendment_reaches_real_worker_and_reviewer_prompts_with_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            Directions(run).append('先完善登录失败路径，再优化页面')
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            result = Supervisor(RunConfig('实现登录', root, worker, reviewer=reviewer, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, 'done')
            for provider in (worker, reviewer):
                self.assertIn('先完善登录失败路径，再优化页面', provider.calls[0]['prompt'])
            message = Directions(run).snapshot()[0]
            self.assertEqual(message['worker']['status'], 'responded')
            self.assertEqual(message['reviewer']['status'], 'responded')
            self.assertEqual(message['worker']['round'], 1)

    def test_direction_arriving_during_review_forces_another_worker_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            worker = ScriptedProvider('worker', [
                {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
                {'verdict': _verdict('done', evidence=['app.py'])},
            ])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': _verdict('done', evidence=['app.py'])},
                {'verdict': _verdict('done', evidence=['app.py'])},
            ])
            original = reviewer.start_command
            def review(*args, **kwargs):
                if not reviewer.calls:
                    Directions(run).append('请补充错误处理和拒绝用例')
                return original(*args, **kwargs)
            with patch.object(reviewer, 'start_command', side_effect=review):
                result = Supervisor(RunConfig('实现功能', root, worker, reviewer=reviewer, run_dir=run,
                                              retry_delay_s=0, max_rounds=3), log=lambda _: None).run()
            self.assertEqual(result.status, 'done')
            self.assertEqual(result.rounds, 2)
            self.assertNotIn('请补充错误处理和拒绝用例', worker.calls[0]['prompt'])
            self.assertIn('请补充错误处理和拒绝用例', worker.calls[1]['prompt'])
            self.assertIn('请补充错误处理和拒绝用例', reviewer.calls[1]['prompt'])

    def test_failed_process_launch_does_not_acknowledge_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            Directions(run).append('优先修复测试')
            provider = ScriptedProvider('worker', [])
            with patch.object(provider, 'start_command', return_value=PreparedCommand(['/nonexistent/fixture'])):
                report = Supervisor(RunConfig('实现功能', root, provider, no_review=True, run_dir=run,
                                              failure_limit=1), log=lambda _: None).run()
            self.assertNotEqual(report.status, 'done')
            self.assertIsNone(Directions(run).snapshot()[0]['worker'])

    def test_queued_task_amendments_persist_and_other_tasks_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': tmp + '/state'}):
            root = Path(tmp)
            store = TaskStore(root / 'data', root)
            payload = {'title': 'Task A', 'requirement': '实现功能', 'cwd': str(root)}
            first = store.create(payload)
            second = store.create({**payload, 'title': 'Task B'})
            result = store.add_direction(first['id'], {'text': '先检查错误路径'})
            self.assertEqual(result['directions'][0]['text'], '先检查错误路径')
            self.assertEqual(result['direction_delivery'], 'on_start')
            self.assertEqual(store.detail(second['id'])['directions'], [])
            restored = TaskStore(root / 'data', root)
            self.assertEqual(restored.detail(first['id'])['directions'][0]['text'], '先检查错误路径')
            for invalid in ['', 'x' * 4001, None]:
                with self.assertRaises(TaskError):
                    store.add_direction(first['id'], {'text': invalid})
            store.action(second['id'], 'cancel')
            with self.assertRaises(TaskError):
                store.add_direction(second['id'], {'text': '不能修改已取消任务'})

    def test_overview_includes_every_task_and_live_result_updates(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': tmp + '/state'}):
            root = Path(tmp)
            store = TaskStore(root / 'data', root)
            first = store.create({'title': 'Task A', 'requirement': 'A'})
            second = store.create({'title': 'Task B', 'requirement': 'B'})
            store.action(second['id'], 'cancel')
            run = Path(first['run_dir'])
            run.mkdir()
            (run / 'live.jsonl').write_text(json.dumps({'kind': 'result', 'at': '2026-11-06T03:00:00Z',
                                                      'role': 'worker', 'round': 1, 'text': '第一次检查结果'}) + '\n')
            overview = store.overview()
            self.assertEqual(len(overview['tasks']), 2)
            self.assertEqual({t['status'] for t in overview['tasks']}, {'queued', 'cancelled'})
            with (run / 'live.jsonl').open('a') as stream:
                stream.write(json.dumps({'kind': 'result', 'at': '2026-11-06T03:00:01Z', 'role': 'worker',
                                         'round': 1, 'text': '新的检查结果'}) + '\n')
            updated = next(t for t in store.overview()['tasks'] if t['id'] == first['id'])
            self.assertEqual(updated['activity']['events'][-1]['text'], '新的检查结果')

    def test_directions_have_bounded_storage_without_dropping_prior_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            directions = Directions(Path(tmp))
            for index in range(64):
                directions.append(f'要求 {index}')
            with self.assertRaises(ValueError):
                directions.append('overflow')
            self.assertEqual(len(directions.messages()), 64)

    def test_damaged_task_does_not_hide_other_task_statuses(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': tmp + '/state'}):
            root = Path(tmp)
            store = TaskStore(root / 'data', root)
            first = store.create({'title': 'Damaged', 'requirement': 'A'})
            second = store.create({'title': 'Healthy', 'requirement': 'B'})
            run = Path(first['run_dir'])
            run.mkdir()
            (run / 'state.json').write_text(json.dumps({'status': 'running', 'usage': 'invalid-usage'}))
            views = {task['id']: task for task in store.overview()['tasks']}
            self.assertEqual(len(views), 2)
            self.assertEqual(views[first['id']]['status'], 'unknown')
            self.assertEqual(views[second['id']]['status'], 'queued')

    def test_previous_response_receipt_is_retained_while_next_turn_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directions = Directions(Path(tmp))
            message = directions.append('先完善错误路径')
            directions.receipt([message['id']], 'worker', 1, responded=True)
            directions.receipt([message['id']], 'worker', 2)
            receipt = directions.snapshot()[0]['worker']
            self.assertEqual(receipt['status'], 'sent')
            self.assertEqual(receipt['round'], 2)
            self.assertEqual(receipt['responded_round'], 1)
            self.assertTrue(receipt['responded_at'])

    def test_submission_after_delivery_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state.json').write_text(json.dumps({'status': 'done'}))
            with self.assertRaisesRegex(ValueError, '任务已结束'):
                Directions(root).append('不能悄悄丢失的要求')
            self.assertEqual(Directions(root).messages(), [])
