import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_moniter.providers import resolve_provider
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.recovery import resume_arguments
from console.server import ConsoleServer
from console.tasks import TaskError, TaskStore


class ResumeArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = RunConfig('', self.root, resolve_provider('codex'), reviewer=resolve_provider('codex'),
                           worker_isolation='strict', review_isolation='strict', protect_paths=['tests'])
        self.state = {'cwd': str(self.root), 'checks': ['python -m unittest'],
                      'provider_config': Supervisor._provider_config(SimpleNamespace(config=config))}
        self.argv = ['run', '--cwd', str(self.root), '--run-dir', str(self.root / 'run'),
                     '--provider', 'codex', '--reviewer', 'codex', '--worker-isolation', 'strict',
                     '--review-isolation', 'strict', '--protect', 'tests', '--model', 'worker-model',
                     '--reviewer-model', 'review-model', '--max-runtime', '2h', '--max-agent-calls', '60',
                     '--max-rounds', '40', '--idle-timeout', '0', '--check', 'python -m unittest',
                     '--file', '/no-longer-present/requirement.md', '--modules', '/no-longer-present/modules.json',
                     '--guard']

    def tearDown(self):
        self.temp.cleanup()

    def test_preserves_launch_policy_and_uses_saved_requirement_and_modules(self):
        for prefix in ([], [sys.executable, '-m', 'auto_moniter']):
            with self.subTest(prefix=prefix):
                args = resume_arguments({'argv': prefix + self.argv}, self.root / 'run', self.state)
                self.assertIn('--resume', args)
                for flag in ['--model=worker-model', '--reviewer-model=review-model', '--max-runtime=2h',
                             '--max-agent-calls=60', '--max-rounds=40', '--idle-timeout=0', '--protect=tests', '--guard']:
                    self.assertIn(flag, args)
                self.assertNotIn('--file', args)
                self.assertNotIn('--modules', args)
                self.assertEqual(args[args.index('--worker-isolation') + 1], 'strict')
                self.assertEqual(args[args.index('--review-isolation') + 1], 'strict')

    def test_rejects_mismatched_workspaces_policy_and_invalid_commands(self):
        cases = [self.argv + ['--run-dir', '/other/run'], self.argv + ['--cwd', '/other/project'],
                 self.argv + ['--worker-isolation', 'provider'], self.argv + ['--no-review'],
                 self.argv + ['--check', 'true'], self.argv + ['--unsupported-option'],
                 ['bash', '-c', 'echo do-not-run'], ['run', '--help']]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                resume_arguments({'argv': argv}, self.root / 'run', self.state)


class ImportedTaskControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='console-recovery-')
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        binaries = self.root / 'bin'
        binaries.mkdir()
        agent = binaries / 'codex'
        agent.write_text(f'''#!{sys.executable}
import json, sys, time
sys.stdin.read()
print('working on local recovery fixture', flush=True)
time.sleep(1)
print(json.dumps({{'status':'continue','scope':'requirement','summary':'继续实现本地测试',
    'done':[],'gaps':['尚有工作'],'next_actions':['继续实现'],'blockers':[],'evidence':[]}}), flush=True)
''')
        agent.chmod(0o700)
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state'),
                                           'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        self.env.start()
        self.original = TaskStore(self.root / 'original-data', self.project)
        self.imported = None

    def tearDown(self):
        for store in [self.original, self.imported]:
            if store:
                for child in list(store._children.values()):
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=12)
        self.env.stop()
        self.temp.cleanup()

    def wait_for(self, predicate):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            if result := predicate():
                return result
            time.sleep(0.03)
        self.fail('task did not reach expected state')

    def stopped_task(self):
        task = self.original.create({'title': 'Imported recovery fixture', 'requirement': 'local fixture', 'workflow':'legacy',
                                     'cwd': str(self.project), 'provider': 'codex', 'reviewer': 'codex',
                                     'model': 'worker-fixture', 'reviewer_model': 'review-fixture',
                                     'review_isolation': 'provider', 'auto_start': True})
        self.wait_for(lambda: self.original.detail(task['id'])['status'] == 'running')
        self.original.action(task['id'], 'stop')
        self.wait_for(lambda: task['id'] not in self.original._children)
        run = Path(task['run_dir'])
        self.imported = TaskStore(self.root / 'imported-data', self.project, [run.parent])
        found = self.imported.overview()['tasks'][0]
        self.assertFalse(found['managed'])
        self.assertIn('resume', found['actions'])
        return found, run

    def test_http_resume_pause_cancel_pause_continue_and_restart_persistence(self):
        task, run = self.stopped_task()
        ident = task['id']
        with ConsoleServer(('127.0.0.1', 0), self.imported) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(action=None):
                    connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
                    if action:
                        connection.request('POST', f'/api/tasks/{ident}/{action}', '{}',
                                           {'Content-Type': 'application/json', 'X-Console-Token': server.csrf})
                    else:
                        connection.request('GET', f'/api/tasks/{ident}')
                    response = connection.getresponse()
                    data = json.loads(response.read())
                    connection.close()
                    return response.status, data

                status, resumed = request('resume')
                self.assertEqual(status, 200, resumed)
                self.assertEqual(resumed['run_dir'], str(run))
                self.assertEqual(request('resume')[0], 409)  # No duplicate process.
                self.wait_for(lambda: request()[1]['status'] == 'running')
                launcher = self.imported._get(ident)['launcher']
                status, pending = request('pause')
                self.assertEqual(status, 200)
                self.assertEqual(pending['control'], 'pause')
                self.assertIn('continue', pending['actions'])
                self.assertEqual(request('continue')[0], 200)  # Cancel pending pause.
                self.assertFalse((run / 'control.json').exists())
                self.assertEqual(request('pause')[0], 200)
                self.wait_for(lambda: request()[1]['status'] == 'paused')
                self.assertEqual(request('continue')[0], 200)
                self.wait_for(lambda: request()[1]['status'] == 'running')
                self.assertEqual(self.imported._get(ident)['launcher'], launcher)
                restored = TaskStore(self.root / 'imported-data', self.project, [run.parent])
                tasks = restored.overview()['tasks']
                self.assertEqual([t['id'] for t in tasks], [ident])
                self.assertTrue(tasks[0]['managed'])
                saved_args = self.imported._get(ident)['resume_argv']
                self.assertIn('--model=worker-fixture', saved_args)
                self.assertIn('--reviewer-model=review-fixture', saved_args)
                self.assertEqual(request('stop')[0], 200)
                self.wait_for(lambda: ident not in self.imported._children)
                self.assertIn('resume', request()[1]['actions'])
                self.assertEqual(request('resume')[0], 200)
                self.wait_for(lambda: request()[1]['status'] == 'running')
                self.assertEqual(request('stop')[0], 200)
                self.wait_for(lambda: ident not in self.imported._children)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_missing_or_mismatched_manifest_does_not_remove_stop_request(self):
        task, run = self.stopped_task()
        manifest = json.loads((run / 'launch.json').read_text())
        for saved in ({}, {'argv': manifest['argv'] + ['--worker-isolation', 'strict']}):
            write_json(run / 'launch.json', saved)
            view = self.imported.detail(task['id'])
            self.assertNotIn('resume', view['actions'])
            self.assertTrue(view['resume_unavailable'])
            with self.assertRaises(TaskError):
                self.imported.action(task['id'], 'resume')
            self.assertEqual(json.loads((run / 'control.json').read_text())['action'], 'stop')

    def test_spawn_failure_preserves_checkpoint_and_stop_control(self):
        task, run = self.stopped_task()
        checkpoint = (run / 'state.json').read_bytes()
        with patch('console.tasks.subprocess.Popen', side_effect=OSError('fixture spawn failure')):
            with self.assertRaises(TaskError):
                self.imported.action(task['id'], 'resume')
        view = self.imported.detail(task['id'])
        self.assertEqual((run / 'state.json').read_bytes(), checkpoint)
        self.assertEqual(json.loads((run / 'control.json').read_text())['action'], 'stop')
        self.assertIn('fixture spawn failure', view['launch_error'])
        self.assertIn('resume', view['actions'])
