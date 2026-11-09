"""CYCLE-2: explicit review_only must not schedule executable checks."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from console.acceptance import scan
from console.server import ConsoleServer
from console.tasks import TaskError, TaskStore


class ReviewOnlyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'tests').mkdir()
        (self.project / 'tests' / 'test_api.py').write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_ok(self): self.assertEqual(1, 1)\n"
        )
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.env.start()
        self.store = TaskStore(self.root / 'console', self.project)

    def tearDown(self):
        for child in list(self.store._children.values()):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=12)
        self.env.stop()
        self.temp.cleanup()

    def plan(self, ids=None):
        found = scan(self.project)
        return {
            'task_type': 'code',
            'python': found['python'],
            'signature': found['signature'],
            'criteria': '合法与非法输入有清晰结果',
            'selected_ids': list(ids or []),
        }

    def test_explicit_review_only_rejects_selected_ids(self):
        before = list(self.store.task_summaries()['tasks'])
        with self.assertRaisesRegex(TaskError, '仅独立语义审核不能附带执行检查'):
            self.store.create({
                'title': 'review only selected',
                'requirement': 'review semantics',
                'verification_mode': 'review_only',
                'acceptance_plan': self.plan(['python-unittest']),
            })
        self.assertEqual(self.store.task_summaries()['tasks'], before)

    def test_explicit_review_only_rejects_custom_checks(self):
        before_dirs = set((self.root / 'console' / 'runs').glob('*')) if (self.root / 'console' / 'runs').exists() else set()
        with self.assertRaisesRegex(TaskError, '不能附带执行检查|高级命令'):
            self.store.create({
                'title': 'review only custom',
                'requirement': 'review semantics',
                'verification_mode': 'review_only',
                'checks': ['echo should-not-run'],
            })
        after_dirs = set((self.root / 'console' / 'runs').glob('*')) if (self.root / 'console' / 'runs').exists() else set()
        self.assertEqual(after_dirs, before_dirs)
        self.assertEqual(self.store.task_summaries()['tasks'], [])

    def test_explicit_review_only_rejects_both_and_auto_start_does_not_launch(self):
        launches = []
        with patch.object(self.store, '_launch', side_effect=lambda meta: launches.append(meta['id'])):
            with self.assertRaisesRegex(TaskError, '仅独立语义审核不能附带执行检查'):
                self.store.create({
                    'title': 'review only both',
                    'requirement': 'review',
                    'verification_mode': 'review_only',
                    'acceptance_plan': self.plan(['python-unittest']),
                    'checks': ['false'],
                    'auto_start': True,
                })
        self.assertEqual(launches, [])
        self.assertEqual(self.store.task_summaries()['tasks'], [])

    def test_explicit_review_only_with_empty_plan_succeeds(self):
        task = self.store.create({
            'title': 'pure review',
            'requirement': 'semantic only',
            'verification_mode': 'review_only',
            'acceptance_plan': self.plan([]),
        })
        self.assertEqual(task['checks'], [])
        self.assertEqual(task['verification_mode'], 'review_only')
        self.assertEqual(task['acceptance_plan']['selected_ids'], [])
        self.assertEqual(task['acceptance_plan']['selected'], [])
        self.assertEqual(task['status'], 'queued')

    def test_executable_still_requires_and_keeps_selected_checks(self):
        with self.assertRaisesRegex(TaskError, '至少需要一条'):
            self.store.create({
                'title': 'exec empty',
                'requirement': 'implement',
                'verification_mode': 'executable',
                'acceptance_plan': self.plan([]),
            })
        task = self.store.create({
            'title': 'exec ok',
            'requirement': 'implement',
            'verification_mode': 'executable',
            'acceptance_plan': self.plan(['python-unittest']),
        })
        self.assertTrue(task['checks'])
        self.assertEqual(task['acceptance_plan']['selected_ids'], ['python-unittest'])
        self.assertIn('unittest', task['checks'][0])

    def test_planning_still_discards_selected_and_custom_checks(self):
        task = self.store.create({
            'title': 'plan mode',
            'requirement': 'implement',
            'verification_mode': 'planning',
            'checks': ['false'],
            'acceptance_plan': self.plan(['python-unittest']),
        })
        self.assertEqual(task['checks'], [])
        self.assertEqual(task['acceptance_plan']['selected_ids'], [])
        self.assertIn('仅制定可执行的验收方案', task['requirement'])

    def test_legacy_api_without_verification_mode_keeps_checks(self):
        """Omit verification_mode: default label review_only but preserve prior merge semantics."""
        payload = {
            'title': 'legacy client',
            'requirement': 'implement',
            'checks': ['true'],
            'acceptance_plan': self.plan(['python-unittest']),
        }
        self.assertNotIn('verification_mode', payload)
        task = self.store.create(payload)
        self.assertEqual(task['verification_mode'], 'review_only')
        self.assertTrue(any('unittest' in c or c == 'true' for c in task['checks']))
        self.assertGreaterEqual(len(task['checks']), 1)

    def _module(self, checks=('echo marker',)):
        return [{'id': 'M', 'title': '检查', 'acceptance': ['完成'], 'depends_on': [], 'checks': list(checks)}]

    def test_explicit_review_only_rejects_modules_checks_alone(self):
        """AUD-5: top-level empty checks/selected_ids must not allow modules[].checks."""
        before = list(self.store.task_summaries()['tasks'])
        runs = self.root / 'console' / 'runs'
        before_dirs = set(runs.glob('*')) if runs.exists() else set()
        launches = []
        with patch.object(self.store, '_launch', side_effect=lambda meta: launches.append(meta['id'])):
            with self.assertRaisesRegex(TaskError, 'modules\\[\\]\\.checks|不能附带执行检查'):
                self.store.create({
                    'title': 'review only modules',
                    'requirement': 'review',
                    'verification_mode': 'review_only',
                    'acceptance_plan': self.plan([]),
                    'checks': [],
                    'modules': self._module(['echo marker']),
                    'auto_start': True,
                })
        self.assertEqual(launches, [])
        self.assertEqual(self.store.task_summaries()['tasks'], before)
        after_dirs = set(runs.glob('*')) if runs.exists() else set()
        self.assertEqual(after_dirs, before_dirs)

    def test_explicit_review_only_rejects_modules_combined_with_top_level(self):
        with self.assertRaisesRegex(TaskError, '不能附带执行检查'):
            self.store.create({
                'title': 'combo',
                'requirement': 'review',
                'verification_mode': 'review_only',
                'acceptance_plan': self.plan(['python-unittest']),
                'checks': ['false'],
                'modules': self._module(['echo marker']),
            })
        self.assertEqual(self.store.task_summaries()['tasks'], [])

    def test_planning_rejects_modules_checks_does_not_silent_strip(self):
        with self.assertRaisesRegex(TaskError, '制定验收方案模式不能附带模块执行检查'):
            self.store.create({
                'title': 'plan modules',
                'requirement': 'implement',
                'verification_mode': 'planning',
                'acceptance_plan': self.plan([]),
                'checks': [],
                'modules': self._module(['echo marker']),
            })
        self.assertEqual(self.store.task_summaries()['tasks'], [])

    def test_planning_allows_modules_without_checks(self):
        task = self.store.create({
            'title': 'plan no checks',
            'requirement': 'implement',
            'verification_mode': 'planning',
            'acceptance_plan': self.plan(['python-unittest']),
            'modules': [{'id': 'M', 'title': '检查', 'acceptance': ['完成'], 'depends_on': [], 'checks': []}],
        })
        self.assertEqual(task['checks'], [])
        meta = self.store._get(task['id'])
        self.assertEqual(meta['modules'][0]['checks'], [])

    def test_executable_keeps_modules_checks(self):
        task = self.store.create({
            'title': 'exec modules',
            'requirement': 'implement',
            'verification_mode': 'executable',
            'acceptance_plan': self.plan(['python-unittest']),
            'modules': self._module(['true']),
        })
        meta = self.store._get(task['id'])
        self.assertEqual(meta['modules'][0]['checks'], ['true'])
        self.assertTrue(task['checks'])

    def test_legacy_api_without_mode_keeps_modules_checks(self):
        payload = {
            'title': 'legacy modules',
            'requirement': 'implement',
            'checks': ['true'],
            'modules': self._module(['echo legacy-mod']),
            'acceptance_plan': self.plan(['python-unittest']),
        }
        self.assertNotIn('verification_mode', payload)
        task = self.store.create(payload)
        meta = self.store._get(task['id'])
        self.assertEqual(meta['modules'][0]['checks'], ['echo legacy-mod'])

    def test_http_contradictory_review_only_returns_error_without_task(self):
        with ConsoleServer(('127.0.0.1', 0), self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]

            def request(method, path, data=None, token=None):
                conn = HTTPConnection('127.0.0.1', port, timeout=5)
                headers = {'Content-Type': 'application/json'}
                if token:
                    headers['X-Console-Token'] = token
                body = json.dumps(data) if data is not None else None
                conn.request(method, path, body, headers)
                response = conn.getresponse()
                raw = response.read()
                status = response.status
                conn.close()
                return status, json.loads(raw) if raw else {}

            try:
                token = request('GET', '/api/config')[1]['csrf_token']
                before = request('GET', '/api/tasks', token=token)[1].get('tasks', [])
                status, body = request('POST', '/api/tasks', {
                    'title': 'http review only bad',
                    'requirement': 'review',
                    'cwd': str(self.project),
                    'verification_mode': 'review_only',
                    'acceptance_plan': self.plan(['python-unittest']),
                    'checks': ['echo injected'],
                    'auto_start': True,
                    'provider': 'codex',
                    'reviewer': 'claude',
                }, token)
                self.assertEqual(status, 400, body)
                self.assertIn('仅独立语义审核', body.get('error', body.get('message', str(body))))
                after = request('GET', '/api/tasks', token=token)[1].get('tasks', [])
                self.assertEqual(after, before)
                # modules[].checks alone also rejected over HTTP with no half task.
                status, body = request('POST', '/api/tasks', {
                    'title': 'http review only modules',
                    'requirement': 'review',
                    'cwd': str(self.project),
                    'verification_mode': 'review_only',
                    'acceptance_plan': self.plan([]),
                    'checks': [],
                    'modules': self._module(['echo http-marker']),
                    'auto_start': True,
                    'provider': 'codex',
                    'reviewer': 'claude',
                }, token)
                self.assertEqual(status, 400, body)
                self.assertIn('不能附带执行检查', body.get('error', body.get('message', str(body))))
                self.assertEqual(request('GET', '/api/tasks', token=token)[1].get('tasks', []), before)
                # Clean review_only still works over HTTP.
                status, body = request('POST', '/api/tasks', {
                    'title': 'http review only ok',
                    'requirement': 'review',
                    'cwd': str(self.project),
                    'verification_mode': 'review_only',
                    'acceptance_plan': self.plan([]),
                    'checks': [],
                    'auto_start': False,
                    'provider': 'codex',
                    'reviewer': 'claude',
                }, token)
                self.assertIn(status, (200, 201), body)
                self.assertEqual(body['checks'], [])
                self.assertEqual(body['verification_mode'], 'review_only')
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_frontend_helpers_via_node(self):
        """Run create-form submit + helper tests under node --test (not source greps)."""
        import subprocess
        root = Path(__file__).resolve().parents[1]
        script = root / 'tests' / 'test_review_only_boundary.mjs'
        result = subprocess.run(
            ['node', '--test', str(script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            self.fail(result.stdout + '\n' + result.stderr)
        combined = result.stdout + result.stderr
        self.assertRegex(combined, r'# pass [4-9]|# pass [1-9][0-9]')
        self.assertIn('# fail 0', combined)

if __name__ == '__main__':
    unittest.main()