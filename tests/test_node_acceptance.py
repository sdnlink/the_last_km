"""Node discovery and guided execution, using isolated project/server fixtures."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from auto_moniter.cli import build_parser
from auto_moniter.supervisor import RunConfig, Supervisor
from console.acceptance import preflight, scan, select
from console.server import ConsoleServer
from console.tasks import TaskError, TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class NodeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.store = TaskStore(self.root / 'console', self.project)

    def asset(self, name='tests/test_console_requests.mjs', content=''):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def plan(self):
        found = scan(self.project)
        return {'task_type': 'code', 'python': found['python'], 'signature': found['signature'],
                'criteria': '请求成功与失败路径均有断言', 'selected_ids': ['node-test']}

    def test_discovers_builtin_runner_without_package_or_install_and_discloses_effects(self):
        names = ['tests/test_console_requests.mjs', 'test/api.test.js', 'test.cjs', 'unit/foo_test.mjs']
        for name in names:
            self.asset(name)
        self.asset('app.mjs')
        self.asset('tests/test_other.py', 'import unittest')
        found = scan(self.project)
        check = next(c for c in found['checks'] if c['id'] == 'node-test')
        self.assertEqual(set(check['artifacts']), set(names))
        self.assertEqual(shlex.split(check['command']), ['node', '--test', *check['artifacts']])
        self.assertEqual(check['dependencies'], [])
        self.assertTrue(check['purpose'])
        self.assertIn('不安装依赖', check['effects'])
        self.assertIn('python-unittest', [c['id'] for c in found['checks']])
        self.assertFalse((self.project / 'package.json').exists())

    def test_scan_does_not_execute_import_or_change_test_assets(self):
        self.asset(content="throw new Error('must not execute');")
        before = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        with patch('subprocess.Popen', side_effect=AssertionError('scan must only read')):
            self.assertIn('node-test', [c['id'] for c in scan(self.project)['checks']])
        after = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_no_matching_asset_and_external_symlinks_are_not_entries(self):
        self.asset('tests/helper.mjs')
        outside = self.root / 'test_outside.mjs'
        outside.write_text('')
        self.asset().unlink()
        (self.project / 'tests/test_link.mjs').symlink_to(outside)
        (self.project / 'linked-tests').symlink_to(outside.parent, target_is_directory=True)
        self.assertNotIn('node-test', [c['id'] for c in scan(self.project)['checks']])

    def test_traversal_excludes_hidden_dependencies_deep_paths_and_caps_assets(self):
        for name in ['.hidden/test_hidden.mjs', 'node_modules/test_dep.mjs',
                     'build/test_build.mjs', 'a/b/c/d/e/test_deep.mjs']:
            self.asset(name)
        self.assertNotIn('node-test', [c['id'] for c in scan(self.project)['checks']])
        for i in range(85):
            self.asset(f'tests/test_{i:03d}.mjs')
        check = next(c for c in scan(self.project)['checks'] if c['id'] == 'node-test')
        self.assertEqual(len(check['artifacts']), 80)

    def test_directory_budget_stops_before_501st_directory(self):
        self.asset('test_last.mjs')
        visits = [(str(self.project), [], []) for _ in range(500)]
        visits.append((str(self.project), [], ['test_last.mjs']))
        with patch('console.acceptance.os.walk', return_value=iter(visits)):
            self.assertNotIn('node-test', [c['id'] for c in scan(self.project)['checks']])

    def test_node_is_found_even_when_python_asset_limit_is_reached_first(self):
        for i in range(80):
            self.asset(f'a/test_{i:03d}.py', 'import unittest')
        self.asset('z/test_last.mjs')
        self.assertIn('node-test', [c['id'] for c in scan(self.project)['checks']])

    def test_added_deleted_and_modified_assets_invalidate_selection(self):
        path = self.asset()
        for mutation in ('modify', 'add', 'delete'):
            with self.subTest(mutation=mutation):
                plan = self.plan()
                if mutation == 'modify':
                    path.write_text('// changed')
                elif mutation == 'add':
                    self.asset('tests/extra.test.cjs')
                else:
                    path.unlink()
                with self.assertRaisesRegex(ValueError, '重新扫描'):
                    select(self.project, plan)

    def test_signatures_hash_exact_node_bytes_and_oversized_files_are_excluded(self):
        path = self.asset()
        path.write_bytes(b'//\xff')
        plan = self.plan()
        path.write_bytes(b'//\xfe')
        with self.assertRaisesRegex(ValueError, '重新扫描'):
            select(self.project, plan)
        path.write_bytes(b' ' * (512 * 1024 + 1))
        self.assertNotIn('node-test', [c['id'] for c in scan(self.project)['checks']])

    def test_server_rebuilds_commands_and_rejects_unknown_ids(self):
        self.asset()
        plan = {**self.plan(), 'selected': [{'command': 'false'}]}
        self.assertEqual(shlex.split(select(self.project, plan)['selected'][0]['command']),
                         [shutil.which('node') or 'node', '--test', 'tests/test_console_requests.mjs'])
        with self.assertRaisesRegex(ValueError, '不存在'):
            select(self.project, {**plan, 'selected_ids': ['node-injected']})

    def test_missing_node_preflight_is_environment_unavailable_without_execution(self):
        self.asset()
        chosen = select(self.project, self.plan())
        with patch('console.acceptance.shutil.which', return_value=None), \
                patch('subprocess.Popen', side_effect=AssertionError('preflight must not run tests')):
            result = preflight(chosen)
        self.assertFalse(result['ready'])
        self.assertFalse(result['executed'])
        self.assertEqual(result['checks'][0]['status'], 'environment_unavailable')
        self.assertIn('node', result['checks'][0]['errors'][0])

    @unittest.skipUnless(shutil.which('node'), 'Node preflight requires node')
    def test_available_node_preflight_is_ready_without_running_tests(self):
        self.asset(content="throw new Error('must not run in preflight');")
        chosen = select(self.project, self.plan())
        with patch('subprocess.Popen', side_effect=AssertionError('preflight must not run tests')):
            result = preflight(chosen)
        self.assertTrue(result['ready'])
        self.assertFalse(result['executed'])
        self.assertEqual(result['checks'][0]['status'], 'ready')

    def test_start_rechecks_signature_and_missing_node_before_launch(self):
        path = self.asset()
        task = self.store.create({'title': 'queued', 'requirement': 'implement',
                                  'verification_mode': 'executable', 'acceptance_plan': self.plan()})
        meta = self.store._get(task['id'])
        with patch('console.tasks.discover_providers', return_value={'codex': True, 'claude': True}), \
                patch('console.acceptance.shutil.which', return_value=None), \
                patch.object(self.store, '_launch') as launch:
            with self.assertRaisesRegex(TaskError, '环境不可用'):
                self.store._check_start(meta)
            path.write_text('// changed')
            with self.assertRaisesRegex(TaskError, '重新扫描'):
                self.store._check_start(meta)
            launch.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node execution requires node; discovery remains testable')
    def test_quoted_filename_is_safe_when_selected_shell_command_executes(self):
        name = "tests/test_space ' ; $(touch INJECTED); &.mjs"
        self.asset(name, "import {test} from 'node:test'; test('ok', () => {});")
        command = select(self.project, self.plan())['selected'][0]['command']
        self.assertEqual(shlex.split(command), [shutil.which('node'), '--test', name])
        result = subprocess.run(command, shell=True, cwd=self.project, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.project / 'INJECTED').exists())

    def test_authenticated_web_scan_preflight_create_and_cli_wiring(self):
        self.asset()
        with ConsoleServer(('127.0.0.1', 0), self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, data=None, token=''):
                conn = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
                conn.request(method, path, json.dumps(data) if data is not None else None,
                             {'Content-Type': 'application/json', 'X-Console-Token': token})
                response = conn.getresponse()
                status, body = response.status, json.loads(response.read())
                conn.close()
                return status, body

            try:
                token = request('GET', '/api/config')[1]['csrf_token']
                payload = {'cwd': str(self.project)}
                self.assertEqual(request('POST', '/api/acceptance/scan', payload)[0], 403)
                status, found = request('POST', '/api/acceptance/scan', payload, token)
                self.assertEqual(status, 200)
                self.assertIn('node-test', [c['id'] for c in found['checks']])
                plan = {**self.plan(), 'signature': found['signature']}
                with patch('console.acceptance.shutil.which', return_value=None):
                    status, result = request('POST', '/api/acceptance/preflight',
                                             {**payload, 'acceptance_plan': plan}, token)
                self.assertEqual(status, 200)
                self.assertFalse(result['executed'])
                self.assertFalse(result['ready'])
                status, task = request('POST', '/api/tasks', {**payload, 'title': 'Node guided',
                    'requirement': 'implement', 'verification_mode': 'executable', 'acceptance_plan': plan}, token)
                self.assertEqual(status, 201)
                args = build_parser().parse_args(self.store._new_args(self.store._get(task['id'])))
                self.assertEqual(shlex.split(args.check[0]),
                                 [shutil.which('node') or 'node', '--test', 'tests/test_console_requests.mjs'])
                self.assertEqual(task['acceptance_plan']['selected_ids'], ['node-test'])
                with patch('console.tasks.discover_providers', return_value={'codex': True, 'claude': True}), \
                        patch('console.acceptance.shutil.which', return_value=None), \
                        patch.object(self.store, '_launch') as launch:
                    status, result = request('POST', '/api/tasks', {**payload, 'title': 'must not start',
                        'requirement': 'implement', 'verification_mode': 'executable',
                        'acceptance_plan': plan, 'auto_start': True}, token)
                    self.assertEqual(status, 409)
                    self.assertIn('环境不可用', result['error'])
                    launch.assert_not_called()
                self.assertEqual(len(self.store.task_summaries()['tasks']), 1)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    @unittest.skipUnless(shutil.which('node'), 'Node execution requires node')
    def test_selected_node_command_runs_in_watcher_check_pipeline(self):
        self.asset(content="import {test} from 'node:test'; import assert from 'node:assert/strict'; "
                           "test('actual assertion', () => assert.equal(2 + 2, 4));")
        chosen = select(self.project, self.plan())
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        run = self.project / 'fixture-run'
        report = Supervisor(RunConfig('fixture only', self.project, worker, no_review=True,
            checks=[chosen['selected'][0]['command']], run_dir=run, git_history='off', retry_delay_s=0),
            log=lambda _: None).run()
        self.assertEqual(report.status, 'done', report)
        results = json.loads((run / 'acceptance-results.json').read_text())
        self.assertTrue(results['complete'])
        self.assertEqual(results['checks'][0]['status'], 'passed')
        self.assertIn('actual assertion', (run / '001-check-01.log').read_text())
