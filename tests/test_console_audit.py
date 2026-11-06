import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.live import LiveOutput, public_events
from auto_moniter.providers import PreparedCommand, resolve_provider
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from console.recovery import resume_arguments
from console.tasks import TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class ConsoleAuditTests(unittest.TestCase):
    def test_business_state_file_does_not_hide_nested_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': tmp + '/standard'}):
            root = Path(tmp)
            custom = root / 'custom'
            run = custom / 'runs/actual-task'
            run.mkdir(parents=True)
            write_json(custom / 'state.json', {'last_project': 'business-cache'})
            write_json(run / 'state.json', {'cwd': str(root), 'status': 'stopped', 'rounds': []})
            (run / 'requirement.md').write_text('Actual task')
            (run / 'supervisor.lock').touch()
            store = TaskStore(root / 'data', root, [custom])
            tasks = store.overview()['tasks']
            self.assertEqual([task['run_dir'] for task in tasks], [str(run)])

    def test_corrupt_receipt_keeps_running_task_visible_and_controllable(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': tmp + '/standard'}):
            root = Path(tmp)
            run = root / '.auto-moniter/run'
            run.mkdir(parents=True)
            write_json(run / 'state.json', {'cwd': str(root), 'status': 'running', 'rounds': [], 'usage': {}})
            (run / 'requirement.md').write_text('Task with damaged receipt')
            Directions(run).append('Check error paths')
            write_json(run / 'direction-receipts.json', {'worker': ['invalid']})
            with (run / 'supervisor.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                store = TaskStore(root / 'data', root)
                task = store.overview()['tasks'][0]
                self.assertEqual(task['status'], 'running')
                self.assertIn('stop', task['actions'])
                self.assertTrue(task['direction_error'])
                self.assertFalse(task['can_direct'])
                stopped = store.action(task['id'], 'stop')
                self.assertEqual(stopped['control'], 'stop')

    def test_malformed_public_events_cannot_interrupt_worker_process(self):
        records = [{'type': 'event_msg', 'payload': ['unexpected']},
                   {'type': 'item.completed', 'item': {'type': 'message', 'role': 'assistant', 'content': 3}},
                   {'type': 'item.completed', 'item': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': None}]}},
                   {'type': [], 'item': {'type': 'file_change', 'changes': 5}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = LiveOutput(root, 'worker', 1, 'fixture')
            code = 'import json; records=' + repr(records) + '; [print(json.dumps(x), flush=True) for x in records]; print("worker completed", flush=True)'
            result = run_command(PreparedCommand([sys.executable, '-c', code]), root,
                                 detect_input_requests=False, on_output=output.feed)
            output.close()
            self.assertEqual(result.exit_code, 0)
            self.assertIn('worker completed', (root / 'live.jsonl').read_text())
            for record in records:
                self.assertEqual(public_events(record), [])

    def test_oversized_record_is_discarded_until_its_line_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = LiveOutput(root, 'worker', 1, 'fixture')
            event = json.dumps({'type': 'response_item', 'payload': {'type': 'reasoning',
                                  'text': 'private-data-' * 30000}}).encode() + b'\n'
            for start in range(0, len(event), 65536):
                output.feed(0, event[start:start + 65536])
            output.feed(0, b'public progress after large record\n')
            output.close()
            contents = (root / 'live.jsonl').read_text()
            self.assertNotIn('private-data-', contents)
            self.assertIn('public progress after large record', contents)

    def test_legacy_provider_defaults_match_supervisor_resume_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = RunConfig('', root, resolve_provider('codex'), reviewer=resolve_provider('codex'))
            saved = Supervisor._provider_config(SimpleNamespace(config=config))
            for key in ('worker_isolation', 'review_isolation', 'protect_paths'):
                saved.pop(key)
            args = resume_arguments({'argv': ['run', '--cwd', str(root), '--run-dir', str(root / 'run'),
                                               '--provider', 'codex', '--reviewer', 'codex']},
                                    root / 'run', {'cwd': str(root), 'provider_config': saved})
            self.assertIn('--resume', args)

    def test_corrupt_receipt_cannot_cancel_an_actual_agent_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            Directions(run).append('Verify the error path')
            write_json(run / 'direction-receipts.json', {'worker': ['invalid']})
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']),
                                                 'touch': 'app.py'}])
            result = Supervisor(RunConfig('Implement behavior', root, worker, no_review=True,
                                          run_dir=run, max_rounds=1), log=lambda _: None).run()
            self.assertEqual(result.status, 'done')
            self.assertIn('Verify the error path', worker.calls[0]['prompt'])
            with self.assertRaises(ValueError):
                Directions(run).snapshot()


if __name__ == '__main__':
    unittest.main()
