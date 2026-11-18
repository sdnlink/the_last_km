"""Invalid control files must not kill a run or silently release a pause."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_moniter.guardian import Guardian
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_lock
from console.tasks import TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class ControlRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root/'run'
        self.run.mkdir()
        self.config = RunConfig('fixture', self.root, ScriptedProvider('worker', []),
                                no_review=True, run_dir=self.run)
        self.monitor = Supervisor(self.config, log=lambda _: None)
        self.messages = []
        self.guardian = Guardian(self.config, log=self.messages.append)
        self.control = self.run/'control.json'

    def test_invalid_requests_do_not_terminate_the_monitor_or_guardian(self):
        for raw in (b'{', b'[]', b'null', b'"pause"', b'{"action":[]}',
                    b'{"action":"invalid"}', b'\xff', b' '*5000, b'['*1500+b']'*1500):
            with self.subTest(raw=raw[:50]):
                self.control.write_bytes(raw)
                self.monitor._process_control(active=True)
                self.guardian._check_control()
                self.assertFalse(self.monitor._stop)
                self.assertFalse(self.guardian._stop.is_set())
        write_json(self.control, {'action':'stop', 'reason':'valid request after corruption'})
        self.monitor._process_control(active=True)
        self.guardian._check_control()
        self.assertTrue(self.monitor._stop_requested)
        self.assertTrue(self.guardian._stop.is_set())
        self.assertIn('valid request after corruption', self.monitor._stop_reason)

    def test_transient_read_failure_does_not_stop_the_guardian(self):
        with patch('auto_moniter.control.Path.open', side_effect=PermissionError('fixture read unavailable')):
            self.guardian._check_control()
        self.assertFalse(self.guardian._stop.is_set())
        self.assertIn('fixture read unavailable', self.messages[0])
        write_json(self.control, {'action':'stop'})
        self.guardian._check_control()
        self.assertTrue(self.guardian._stop.is_set())

    def test_console_keeps_paused_status_and_offers_repair_actions(self):
        store = TaskStore(self.root/'console', self.root)
        task = store.create({'title':'paused fixture', 'requirement':'fixture', 'provider':'codex',
                             'reviewer':'codex', 'workflow':'legacy'})
        run = Path(task['run_dir'])
        run.mkdir()
        write_json(run/'state.json', {'cwd':str(self.root), 'status':'running', 'rounds':[]})
        write_json(run/'heartbeat', {'at':datetime.now(timezone.utc).isoformat(), 'note':'paused at a turn boundary'})
        (run/'control.json').write_text('{')
        with file_lock(run/'supervisor.lock'):
            view = store.detail(task['id'])
            self.assertEqual(view['status'], 'paused')
            self.assertIn('continue', view['actions'])
            self.assertIn('stop', view['actions'])
            self.assertIn('控制请求暂不可用', view['control_message'])
            continued = store.action(task['id'], 'continue')
        self.assertFalse((run/'control.json').exists())
        self.assertEqual(continued['status'], 'running')
        self.assertNotIn('控制请求暂不可用', continued['control_message'])

    def test_repeated_invalid_control_is_logged_once_until_repaired(self):
        self.control.write_text('{')
        for _ in range(3):
            self.monitor._process_control(active=True)
            self.guardian._check_control()
        events = [json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(event['event']=='control_request_invalid' for event in events), 1)
        self.assertEqual(len(self.messages), 1)
        self.control.unlink()
        self.monitor._process_control(active=True)
        self.guardian._check_control()
        self.control.write_text('{')
        self.monitor._process_control(active=True)
        self.guardian._check_control()
        self.assertEqual(len(self.messages), 2)

    def test_a_corrupt_control_during_pause_does_not_resume_work(self):
        write_json(self.control, {'action':'pause'})
        paused = threading.Event()
        finished = threading.Event()
        errors = []
        original_event = self.monitor._event
        def event(kind, **details):
            original_event(kind, **details)
            if kind == 'paused':
                paused.set()
        def wait_for_control():
            try:
                self.monitor._process_control()
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with patch.object(self.monitor, '_event', side_effect=event), \
             patch.object(self.monitor, '_check_budget'), patch.object(self.monitor, '_beat'):
            thread = threading.Thread(target=wait_for_control)
            thread.start()
            try:
                self.assertTrue(paused.wait(2))
                self.control.write_text('{')
                self.assertFalse(finished.wait(.5), 'corruption released or crashed the pause')
                write_json(self.control, {'action':'stop'})
                self.assertTrue(finished.wait(2))
            finally:
                self.monitor.request_stop()
                thread.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(self.monitor._stop_requested)

    def test_bad_control_does_not_discard_the_current_candidate(self):
        self.config.provider = ScriptedProvider('worker', [
            {'verdict':_verdict('done', evidence=['app.py']), 'touch':'app.py'}])
        self.control.write_text('[]')
        result = Supervisor(self.config, log=lambda _: None).run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertTrue((self.root/'app.py').is_file())
        self.assertEqual(json.loads((self.run/'report.json').read_text())['status'], 'done')
