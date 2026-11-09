"""Creation rejects invalid input before persistence and preserves validated CLI values."""
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import build_parser
from console.server import ConsoleServer
from console.settings import DEFAULTS
from console.tasks import TaskError, TaskStore


class TaskCreationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'chosen project'
        self.project.mkdir()
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = TaskStore(self.root / 'console', self.project)
        self.payload = {'title': '创建校验', 'requirement': '保留输入和真实执行配置',
                        'cwd': str(self.project)}

    def reject(self, extra, message=None, store=None):
        store = store or self.store
        with patch.object(store, '_save') as save, patch.object(store, '_launch') as launch:
            with self.assertRaises(TaskError) as raised:
                store.create({**self.payload, **extra})
            if message:
                self.assertIn(message, str(raised.exception))
            save.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(list(store.tasks_dir.iterdir()), [])
        self.assertEqual(list(store.runs_dir.iterdir()), [])

    def test_explicit_empty_directory_cannot_select_the_server_working_directory(self):
        for value in ('', ' ', '\t\n'):
            with self.subTest(value=value):
                self.reject({'cwd': value}, '请填写项目目录')

    def test_omitted_directory_preserves_legacy_default_and_saved_task_is_startable(self):
        payload = {key: value for key, value in self.payload.items() if key != 'cwd'}
        task = self.store.create(payload)
        restored = TaskStore(self.store.data_dir, self.project).detail(task['id'])
        self.assertEqual(restored['cwd'], str(self.project))
        self.assertEqual(restored['status'], 'queued')
        self.assertIn('start', restored['actions'])
        self.assertFalse(Path(task['run_dir']).exists())

    def test_directory_errors_are_actionable_and_do_not_save_a_task(self):
        plain_file = self.root / 'file'
        plain_file.write_text('not a directory')
        loop = self.root / 'loop'
        loop.symlink_to(loop)
        for value, message in ((str(self.root / 'missing'), '不存在'),
                               (str(plain_file), '不存在'),
                               (str(loop), '项目目录无法解析'),
                               ('x' * 4001, 'cwd'), ('bad\0path', 'cwd')):
            with self.subTest(value=value[:80]):
                self.reject({'cwd': value}, message)
        with patch('console.tasks.os.access', return_value=False):
            self.reject({}, '不可写')

    def test_whitespace_cannot_bypass_paseo_isolation_restrictions(self):
        self.reject({'reviewer': ' paseo ', 'reviewer_paseo_backend': 'claude'}, 'strict 审核')
        self.reject({'provider': ' paseo ', 'worker_isolation': 'strict'}, 'strict worker')
        self.reject({'reviewer': ' paseo ', 'review_isolation': 'provider',
                     'reviewer_paseo_backend': ' grok '}, 'Claude plan')

    def test_normalized_roles_backends_and_independent_models_reach_the_cli(self):
        task = self.store.create({**self.payload, 'provider': ' paseo ', 'reviewer': ' paseo ',
                                  'review_isolation': 'provider', 'paseo_backend': ' grok ',
                                  'reviewer_paseo_backend': ' claude ', 'model': ' worker-custom ',
                                  'reviewer_model': ' reviewer-custom '})
        meta = self.store._get(task['id'])
        args = build_parser().parse_args(self.store._new_args(meta))
        self.assertEqual((args.provider, args.reviewer), ('paseo', 'paseo'))
        self.assertEqual((args.paseo_backend, args.reviewer_paseo_backend), ('grok', 'claude'))
        self.assertEqual((args.model, args.reviewer_model), ('worker-custom', 'reviewer-custom'))
        self.assertEqual(args.review_isolation, 'provider')

    def test_strict_worker_requires_external_storage_even_when_saved_without_starting(self):
        inside = TaskStore(self.project / '.console', self.project)
        self.reject({'worker_isolation': 'strict'}, '--data-dir', store=inside)
        task = self.store.create({**self.payload, 'worker_isolation': 'strict'})
        self.assertEqual(task['status'], 'queued')
        args = build_parser().parse_args(self.store._new_args(self.store._get(task['id'])))
        self.assertEqual(args.worker_isolation, 'strict')

    def test_protect_path_loop_returns_a_validation_error(self):
        loop = self.project / 'loop'
        loop.symlink_to(loop)
        self.reject({'worker_isolation': 'strict', 'protect_paths': ['loop']}, '保护路径无法解析')

    def test_invalid_structured_settings_are_rejected_before_save(self):
        cases = ({'modules': None}, {'modules': {}}, {'modules': '[not JSON'},
                 {'quality_policy': []}, {'quality_policy': '{not JSON'},
                 {'quality_policy': {'review_tools': 'invalid'}},
                 {'protect_paths': '../outside'}, {'protect_paths': ['../outside']},
                 {'review_isolation': 'unknown'}, {'worker_isolation': 'unknown'},
                 {'requirement': ' \n '}, {'title': ''}, {'auto_start': 'yes'},
                 {'model': None}, {'reviewer_model': 'bad\0id'}, {'paseo_backend': []})
        for extra in cases:
            with self.subTest(extra=extra):
                self.reject(extra)

    def test_number_types_and_limits_cannot_be_coerced_or_overflowed(self):
        maxima = {'max_runtime_minutes': 43200, 'max_agent_calls': 100000,
                  'max_rounds': 100000, 'review_every': 100000,
                  **{key: 2**53 - 1 for key in DEFAULTS if not key.endswith('_isolation')}}
        for key, maximum in maxima.items():
            for value in (-1, True, '1', 1.5, maximum + 1):
                with self.subTest(key=key, value=value):
                    self.reject({key: value})
        self.reject({'check_timeout': 0})

    def test_zero_values_and_maxima_are_persisted_and_forwarded_without_defaults(self):
        zeros = {key: 0 for key in ('max_runtime_minutes', 'max_agent_calls', 'max_rounds',
                                    'review_every', 'turn_timeout', 'idle_timeout', 'keep_rounds',
                                    'max_run_bytes', 'max_history_bytes', 'max_agent_state_bytes')}
        for values in (zeros, {'max_runtime_minutes': 43200, 'max_agent_calls': 100000,
                              'max_rounds': 100000, 'review_every': 100000,
                              'turn_timeout': 2**53 - 1, 'check_timeout': 1}):
            with self.subTest(values=values):
                task = self.store.create({**self.payload, **values})
                meta = TaskStore(self.store.data_dir, self.project)._get(task['id'])
                args = build_parser().parse_args(self.store._new_args(meta))
                for key, value in values.items():
                    self.assertEqual(meta[key], value)
                    self.assertEqual(int(getattr(args, 'max_runtime' if key == 'max_runtime_minutes' else key)),
                                     value * 60 if key == 'max_runtime_minutes' else value)

    def test_automatic_start_preflight_failure_does_not_save_a_half_task(self):
        with patch('console.tasks.discover_providers', return_value={}):
            self.reject({'auto_start': True}, '可执行文件')

    def test_http_errors_for_bad_json_paths_and_role_combinations_keep_the_catalog_empty(self):
        with ConsoleServer(('127.0.0.1', 0), self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                bodies = ['{invalid JSON', '[]', json.dumps({**self.payload, 'cwd': ' '}),
                          json.dumps({**self.payload, 'reviewer': ' paseo ',
                                      'reviewer_paseo_backend': 'claude'})]
                for body in bodies:
                    with self.subTest(body=body):
                        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                        try:
                            conn.request('POST', '/api/tasks', body, {'Content-Type': 'application/json',
                                         'X-Console-Token': server.csrf})
                            response = conn.getresponse()
                            self.assertEqual(response.status, 400)
                            self.assertTrue(json.loads(response.read())['error'])
                        finally:
                            conn.close()
                self.assertEqual(self.store.task_summaries()['tasks'], [])
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
